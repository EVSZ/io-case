"""Ticket issuance: the core of Part 3.

Turns an entitlement into a QR ticket in one database transaction:
idempotency check, ownership check, validity window (Europe/Amsterdam business date,
stored as UTC, exclusive expiry), ES256-signed credential, insert, commit.
See the Part 2 document (2-qr-ticket-service.md, "Retries and concurrency") for the flow
and the failure cases.

Author:  Mateusz <mateusz@transcibe.ai>
Created: 2026-09-30
Project: IO Case - cloud ticketing for Dutch public transport, Part 3
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db import Entitlement, Ticket

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
# How far ahead a ticket may be created (assumption, see 2-qr-ticket-service.md, "Validity window").
BOOKING_HORIZON_DAYS = 60
# Product rules: number of Amsterdam business days a ticket covers.
PRODUCT_DAYS = {"DAY_2ND": 1}
# "iss" claim; validators reject tokens from any other issuer.
ISSUER = "nl-ov-tickets"


class TicketError(Exception):
    """A request the API should reject. Mapped to application/problem+json by the app."""

    def __init__(self, status: int, code: str, detail: str, **extra):
        """Keep status, problem code, message and any extra fields for the problem+json response."""
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


class DevSigner:
    """DEVELOPMENT ONLY: ES256 key from a PEM string (DEV_SIGNING_KEY), or a random one.

    With a PEM, every process and restart uses the same key, so earlier tickets still
    verify. Without one, the key is random per process and lost on restart unless it is
    retained somewhere (fine for tests). Production signs with a non-exportable HSM key in
    Azure Key Vault (CryptographyClient.sign on the SHA-256 digest of the JWS signing input),
    and keeps each verification key available until the last ticket signed with it expires.
    """

    def __init__(self, private_key_pem: str | None = None):
        """Load or generate a P-256 key; the kid is derived from the public key."""
        if private_key_pem:
            # Env vars often carry PEM on one line with literal "\n" separators.
            pem = private_key_pem.replace("\\n", "\n").encode()
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError("DEV_SIGNING_KEY must be an EC P-256 private key (PEM).")
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        self._key = key
        self.public_key = key.public_key()
        # kid = truncated SHA-256 fingerprint of the public key (64 bits), so accidental
        # collisions between different keys are very unlikely.
        spki = self.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = "dev-" + hashlib.sha256(spki).hexdigest()[:16]

    def sign(self, claims: dict) -> str:
        """Return a compact JWS (header.payload.signature) over the given claims."""
        return jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": self.kid})


def validity_window(product_code: str, travel_date: date) -> tuple[datetime, datetime]:
    """[valid_from, valid_until) in UTC: Amsterdam midnight to Amsterdam midnight.

    Computed in Amsterdam local time, then converted, so DST days last 23 or 25 hours.
    Adding 24 hours to a UTC instant would be wrong on those days.
    """
    days = PRODUCT_DAYS.get(product_code)
    if days is None:
        raise TicketError(422, "unknown-product", f"No validity rule for product {product_code}.")
    start = datetime.combine(travel_date, time(0), AMSTERDAM)
    end = datetime.combine(travel_date + timedelta(days=days), time(0), AMSTERDAM)
    return start.astimezone(UTC), end.astimezone(UTC)


def issue_ticket(
    session: Session,
    signer: DevSigner,
    account_id: uuid.UUID,
    entitlement_id: uuid.UUID,
    travel_date: date,
    idempotency_key: str,
    request_hash: str,
    now: datetime | None = None,
) -> tuple[Ticket, bool]:
    """Create a ticket, or return the one already created for this idempotency key.

    Returns (ticket, created). created=False means an idempotent replay.
    """
    # "now" is injectable so tests can pin the clock; production uses the server clock.
    now = now or datetime.now(UTC)

    # Step 1 - idempotency: a retry of an earlier request returns the stored ticket.
    existing = _find_by_key(session, account_id, idempotency_key)
    if existing:
        return _replay(existing, request_hash), False

    # Step 2 - ownership. Filter by the authenticated account, so another account's entitlement
    # looks exactly like a missing one (404), and IDs cannot be probed.
    entitlement = session.scalar(
        select(Entitlement)
        .where(Entitlement.id == entitlement_id, Entitlement.account_id == account_id)
        .with_for_update()  # PostgreSQL row lock; SQLite ignores it, the unique constraint still holds
    )
    if entitlement is None:
        raise TicketError(404, "entitlement-not-found", "Entitlement not found.")
    # Repeat the idempotency lookup now that we hold the lock. An identical request may have
    # missed the Step 1 lookup, waited here for the lock, and should find the ticket the
    # first request committed, not a 409. (Under READ COMMITTED each statement sees new commits.)
    existing = _find_by_key(session, account_id, idempotency_key)
    if existing:
        return _replay(existing, request_hash), False
    # Fast path only; the unique constraint on tickets.entitlement_id is the real guard.
    _reject_if_used(session, entitlement_id)

    # Step 3 - validity. The client sends only a date; the server derives the window.
    today = now.astimezone(AMSTERDAM).date()
    if not today <= travel_date <= today + timedelta(days=BOOKING_HORIZON_DAYS):
        raise TicketError(
            422, "invalid-travel-date",
            f"travel_date must be between {today} and {BOOKING_HORIZON_DAYS} days ahead.",
        )
    valid_from, valid_until = validity_window(entitlement.product_code, travel_date)

    # Step 4 - sign. Only non-personal claims: the QR may be printed and left on a seat.
    ticket_id = uuid.uuid4()
    try:
        credential = signer.sign({
            "iss": ISSUER,
            "jti": str(ticket_id),
            "prd": entitlement.product_code,
            "iat": int(now.timestamp()),
            "nbf": int(valid_from.timestamp()),  # inclusive (RFC 7519)
            "exp": int(valid_until.timestamp()),  # exclusive (RFC 7519)
        })
    except Exception as exc:  # signer outage: nothing written yet, safe to retry with same key
        session.rollback()
        raise TicketError(503, "signing-unavailable", "Signing failed; retry later.") from exc

    # Step 5 - persist. Credential is stored with the ticket so a retry returns identical bytes.
    ticket = Ticket(
        id=ticket_id, account_id=account_id, entitlement_id=entitlement_id,
        idempotency_key=idempotency_key, request_hash=request_hash,
        product_code=entitlement.product_code, travel_date=travel_date,
        valid_from=valid_from, valid_until=valid_until,
        kid=signer.kid, credential=credential, created_at=now,
    )
    session.add(ticket)
    try:
        # The credential is only returned after this commit succeeds.
        session.commit()
    except IntegrityError:
        # A concurrent request won the race. The constraints decide; re-read and answer as if
        # we had arrived second. The credential signed above is discarded, never sent.
        session.rollback()
        existing = _find_by_key(session, account_id, idempotency_key)
        if existing:
            return _replay(existing, request_hash), False
        _reject_if_used(session, entitlement_id)
        raise
    return ticket, True


def get_ticket(session: Session, account_id: uuid.UUID, ticket_id: uuid.UUID) -> Ticket:
    """Re-display a ticket. Scoped to the caller's account, like issuance."""
    ticket = session.scalar(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.account_id == account_id)
    )
    if ticket is None:
        raise TicketError(404, "ticket-not-found", "Ticket not found.")
    return ticket


def _find_by_key(session: Session, account_id: uuid.UUID, key: str) -> Ticket | None:
    """Look up the ticket an earlier request created with this account's idempotency key."""
    return session.scalar(
        select(Ticket).where(Ticket.account_id == account_id, Ticket.idempotency_key == key)
    )


def _replay(ticket: Ticket, request_hash: str) -> Ticket:
    """Same key and same body is a retry; same key and a different body is a client error."""
    if ticket.request_hash != request_hash:
        raise TicketError(
            409, "idempotency-key-conflict",
            "Use a new Idempotency-Key for a different ticket request.",
        )
    return ticket


def _reject_if_used(session: Session, entitlement_id: uuid.UUID) -> None:
    """Raise 409 with the existing ticket ID if this entitlement already has a ticket."""
    used = session.scalar(select(Ticket.id).where(Ticket.entitlement_id == entitlement_id))
    if used:
        raise TicketError(
            409, "entitlement-already-used", "This entitlement already has a ticket.",
            ticket_id=str(used),
        )


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; values were stored as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
