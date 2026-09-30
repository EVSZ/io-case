"""HTTP layer for the QR ticket service.

Thin by design: parses and validates requests, resolves the caller's account, and maps
domain and validation errors to application/problem+json. Business rules live in
src/tickets.py.

Run: uvicorn src.app:app --reload --env-file .env   (OpenAPI docs at /docs)

Author:  Mateusz <mateusz@transcibe.ai>
Created: 2026-09-30
Project: IO Case - cloud ticketing for Dutch public transport, Part 3
"""

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from src import tickets
from src.db import Base, Entitlement, SessionLocal, engine
from src.tickets import TicketError, _utc

# DEVELOPMENT ONLY: fixed demo data so the API can be tried by hand.
DEMO_ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000001")
DEMO_ENTITLEMENT = uuid.UUID("00000000-0000-4000-8000-0000000000e1")

# One signer per process. DEVELOPMENT ONLY key: set DEV_SIGNING_KEY (PEM) so all processes
# and restarts share it; unset means a random key per process. See DevSigner.
signer = tickets.DevSigner(os.getenv("DEV_SIGNING_KEY"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup: create tables and the DEVELOPMENT ONLY demo entitlement, then serve."""
    Base.metadata.create_all(engine)  # production: Alembic migrations
    with SessionLocal() as s:
        if s.get(Entitlement, DEMO_ENTITLEMENT) is None:
            s.add(Entitlement(id=DEMO_ENTITLEMENT, account_id=DEMO_ACCOUNT, product_code="DAY_2ND"))
            s.commit()
    yield


app = FastAPI(title="QR Ticket API", lifespan=lifespan)


class TicketRequest(BaseModel):
    """Body of POST /v1/tickets. Only the entitlement and the travel date; the server derives the rest."""
    model_config = ConfigDict(extra="forbid")  # no account_id, product or timestamps accepted

    entitlement_id: uuid.UUID
    travel_date: date


class Credential(BaseModel):
    """The signed QR payload the app renders, with the key ID needed to verify it."""
    format: str = "jws-es256"
    kid: str
    value: str


class TicketResponse(BaseModel):
    """Ticket as returned to the app, for both creation and re-display."""
    ticket_id: uuid.UUID
    entitlement_id: uuid.UUID
    product_code: str
    travel_date: date
    valid_from: datetime
    valid_until: datetime
    status: str = "issued"
    credential: Credential
    created_at: datetime

    @classmethod
    def of(cls, t: tickets.Ticket) -> "TicketResponse":
        """Build the response from a stored ticket, restoring UTC on timestamps SQLite returns naive."""
        return cls(
            ticket_id=t.id, entitlement_id=t.entitlement_id, product_code=t.product_code,
            travel_date=t.travel_date, valid_from=_utc(t.valid_from),
            valid_until=_utc(t.valid_until), credential=Credential(kid=t.kid, value=t.credential),
            created_at=_utc(t.created_at),
        )


def get_session():
    """One session (and so one transaction scope) per request, closed when the request ends."""
    with SessionLocal() as session:
        yield session


def current_account(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """DEVELOPMENT ONLY: accepts 'Bearer dev:<account-uuid>'.

    Production verifies an Entra External ID access token (signature, issuer, audience,
    expiry) and reads the account from its claims. The account never comes from the body.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("dev:"):
        raise TicketError(401, "unauthenticated", "Missing or invalid bearer token.")
    try:
        return uuid.UUID(token.removeprefix("dev:"))
    except ValueError:
        raise TicketError(401, "unauthenticated", "Missing or invalid bearer token.") from None


def _problem(status: int, code: str, detail: str, **extra) -> JSONResponse:
    """Build an RFC 9457 application/problem+json response."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://tickets.example.nl/problems/{code}",
            "title": code.replace("-", " ").capitalize(),
            "status": status,
            "detail": detail,
            **extra,
        },
    )


# Domain errors and request-validation errors use the problem format, so the app can branch
# on "type". Other framework errors (unknown route, wrong method) keep FastAPI's default JSON.
@app.exception_handler(TicketError)
async def problem(_: Request, exc: TicketError) -> JSONResponse:
    """Convert a TicketError into an RFC 9457 problem+json response."""
    return _problem(exc.status, exc.code, exc.detail, **exc.extra)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Malformed body, missing or unknown fields: 422 problem+json.

    Only the location, message and error type of each problem are returned; the submitted
    values ("input") are left out so request data is not echoed back.
    """
    errors = [
        {"loc": [str(part) for part in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return _problem(422, "invalid-request", "The request is not valid.", errors=errors)


@app.post("/v1/tickets", status_code=201, response_model=TicketResponse)
def create_ticket(
    body: TicketRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    account_id: uuid.UUID = Depends(current_account),
    session=Depends(get_session),
):
    """Create a ticket from an entitlement, or replay the earlier result for a retried Idempotency-Key."""
    if not idempotency_key or len(idempotency_key) > 100:
        raise TicketError(400, "missing-idempotency-key", "Idempotency-Key header (1-100 chars) is required.")
    # Fingerprint of the validated body, so formatting differences do not count as a change.
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    ticket, created = tickets.issue_ticket(
        session, signer, account_id, body.entitlement_id, body.travel_date,
        idempotency_key, hashlib.sha256(canonical.encode()).hexdigest(),
    )
    # Tell the client this is a replay of an earlier success, not a new ticket.
    if not created:
        response.headers["Idempotent-Replayed"] = "true"
    return TicketResponse.of(ticket)


@app.get("/v1/tickets/{ticket_id}", response_model=TicketResponse)
def read_ticket(
    ticket_id: uuid.UUID,
    account_id: uuid.UUID = Depends(current_account),
    session=Depends(get_session),
):
    """Return one of the caller's tickets, e.g. to show it again on a new phone."""
    return TicketResponse.of(tickets.get_ticket(session, account_id, ticket_id))
