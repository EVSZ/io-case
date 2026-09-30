"""Focused tests for ticket issuance: idempotency, ownership, validity boundaries, credential.

Author:  Mateusz <mateusz@transcibe.ai>
Created: 2026-09-30
Project: IO Case - cloud ticketing for Dutch public transport, Part 3
"""

import json
import os
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import jwt
import jwt.api_jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from jwt.utils import base64url_encode
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.app import app, get_session, signer
from src.db import Base, Entitlement, Ticket
from src.tickets import AMSTERDAM, DevSigner, issue_ticket, validity_window

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def env():
    # In-memory SQLite shared across connections. Substitute for PostgreSQL, see README.
    """Fresh in-memory database with one entitlement owned by ALICE; yields (client, Session, entitlement_id)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    ent = uuid.uuid4()
    with Session() as s:
        s.add(Entitlement(id=ent, account_id=ALICE, product_code="DAY_2ND"))
        s.commit()

    def override():
        """Replace the app's session dependency with one bound to the test database."""
        with Session() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app), Session, ent
    app.dependency_overrides.clear()


def tomorrow() -> str:
    """Tomorrow's Amsterdam business date: always inside the allowed booking window."""
    return (datetime.now(AMSTERDAM).date() + timedelta(days=1)).isoformat()


def post(client, account, key, entitlement, travel_date):
    """Call POST /v1/tickets as the given account, with the given idempotency key."""
    return client.post(
        "/v1/tickets",
        headers={"Authorization": f"Bearer dev:{account}", "Idempotency-Key": key},
        json={"entitlement_id": str(entitlement), "travel_date": travel_date},
    )


def test_retry_with_same_key_returns_same_ticket(env):
    """A retry with the same key and body returns the same ticket and stores only one row."""
    client, Session, ent = env
    first = post(client, ALICE, "k1", ent, tomorrow())
    retry = post(client, ALICE, "k1", ent, tomorrow())

    assert first.status_code == retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()  # same ticket, same credential bytes
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 1


def test_same_key_with_different_request_conflicts(env):
    """Reusing a key for a different request is a client error, not a silent replay."""
    client, _, ent = env
    post(client, ALICE, "k1", ent, tomorrow())
    later = (datetime.now(AMSTERDAM).date() + timedelta(days=2)).isoformat()

    r = post(client, ALICE, "k1", ent, later)
    assert r.status_code == 409
    assert r.json()["type"].endswith("/idempotency-key-conflict")


def test_other_accounts_entitlement_looks_missing(env):
    """Another account's entitlement gets 404, the same answer as a missing one."""
    client, _, ent = env
    r = post(client, BOB, "k1", ent, tomorrow())
    assert r.status_code == 404
    assert r.json()["type"].endswith("/entitlement-not-found")


def test_entitlement_yields_one_ticket_even_with_new_key(env):
    """A new idempotency key cannot create a second ticket from the same entitlement."""
    client, _, ent = env
    first = post(client, ALICE, "k1", ent, tomorrow())
    r = post(client, ALICE, "k2", ent, tomorrow())
    assert r.status_code == 409
    assert r.json()["ticket_id"] == first.json()["ticket_id"]


def test_past_travel_date_rejected(env):
    """Tickets cannot be created for a date that has already passed in Amsterdam."""
    client, _, ent = env
    yesterday = (datetime.now(AMSTERDAM).date() - timedelta(days=1)).isoformat()
    assert post(client, ALICE, "k1", ent, yesterday).status_code == 422


@pytest.mark.parametrize(
    "travel_date, start_utc, hours",
    [
        (date(2027, 1, 2), datetime(2027, 1, 1, 23, tzinfo=UTC), 24),    # winter, UTC+1
        (date(2027, 7, 2), datetime(2027, 7, 1, 22, tzinfo=UTC), 24),    # summer, UTC+2
        (date(2027, 3, 28), datetime(2027, 3, 27, 23, tzinfo=UTC), 23),  # DST starts
        (date(2027, 10, 31), datetime(2027, 10, 30, 22, tzinfo=UTC), 25),  # DST ends
    ],
)
def test_validity_window_is_amsterdam_day_in_utc(travel_date, start_utc, hours):
    """Window runs from Amsterdam midnight to the next, in UTC, with 23/25-hour DST days."""
    valid_from, valid_until = validity_window("DAY_2ND", travel_date)
    assert valid_from == start_utc
    assert valid_until - valid_from == timedelta(hours=hours)
    # Exclusive expiry: valid_until is the next day's start, so the last valid instant is before it.
    assert valid_until == validity_window("DAY_2ND", travel_date + timedelta(days=1))[0]


def test_credential_verifies_with_public_key_and_matches_ticket(env):
    """The credential verifies with the public key, matches the ticket, and rejects tampering."""
    client, _, ent = env
    body = post(client, ALICE, "k1", ent, tomorrow()).json()
    token = body["credential"]["value"]

    assert jwt.get_unverified_header(token)["kid"] == signer.kid
    # Validity window is checked below against the ticket, not against the test clock.
    claims = jwt.decode(token, signer.public_key, algorithms=["ES256"],
                        options={"verify_nbf": False, "verify_exp": False})
    assert claims["jti"] == body["ticket_id"]
    assert claims["nbf"] == int(datetime.fromisoformat(body["valid_from"]).timestamp())
    assert claims["exp"] == int(datetime.fromisoformat(body["valid_until"]).timestamp())

    # Tampering: extend the validity by a day, keep the original signature.
    header, _, sig = token.split(".")
    forged = base64url_encode(json.dumps({**claims, "exp": claims["exp"] + 86400}).encode()).decode()
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(f"{header}.{forged}.{sig}", signer.public_key, algorithms=["ES256"],
                   options={"verify_nbf": False, "verify_exp": False})


def _pem(key) -> str:
    """PEM text of a private key, as it would be put in DEV_SIGNING_KEY."""
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def test_shared_dev_key_verifies_across_processes():
    """Two signers from the same DEV_SIGNING_KEY (two processes, or a restart) are interchangeable."""
    pem = _pem(ec.generate_private_key(ec.SECP256R1()))
    first, second = DevSigner(pem), DevSigner(pem.replace("\n", "\\n"))  # one-line env form too
    assert first.kid == second.kid
    token = first.sign({"jti": "t1"})
    assert jwt.decode(token, second.public_key, algorithms=["ES256"])["jti"] == "t1"


def test_different_dev_keys_get_different_kids():
    """Random per-process keys can no longer share a key ID, so a validator can tell them apart."""
    assert DevSigner().kid != DevSigner().kid


def test_dev_key_must_be_p256():
    """A key of the wrong type fails at startup instead of producing unverifiable tickets."""
    with pytest.raises(ValueError):
        DevSigner(_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))


@pytest.mark.parametrize(
    "body",
    [
        b'{"entitlement_id": ',  # malformed JSON
        {"entitlement_id": "ENT"},  # missing travel_date
        {"entitlement_id": "ENT", "travel_date": "2027-01-02", "account_id": "secret-account-value"},
    ],
    ids=["malformed-json", "missing-field", "unknown-field"],
)
def test_invalid_request_is_problem_json(env, body):
    """Validation failures use the documented problem format and do not echo submitted values."""
    client, _, ent = env
    if isinstance(body, dict):
        body = json.dumps(body).replace("ENT", str(ent)).encode()
    headers = {"Authorization": f"Bearer dev:{ALICE}", "Idempotency-Key": "k1",
               "Content-Type": "application/json"}

    r = client.post("/v1/tickets", headers=headers, content=body)

    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"].endswith("/invalid-request") and body["status"] == 422
    assert {"title", "detail", "errors"} <= body.keys()
    assert "secret-account-value" not in r.text


def test_signing_failure_saves_nothing_and_retry_succeeds(env, monkeypatch):
    """A signer outage returns 503 without a ticket; the same key works once signing recovers."""
    client, Session, ent = env

    def broken(claims):
        raise RuntimeError("signer unavailable")

    with monkeypatch.context() as m:
        m.setattr(signer, "sign", broken)
        r = post(client, ALICE, "k1", ent, tomorrow())
    assert r.status_code == 503
    assert r.json()["type"].endswith("/signing-unavailable")
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 0

    assert post(client, ALICE, "k1", ent, tomorrow()).status_code == 201


def test_get_ticket_only_for_owner(env):
    """GET returns the owner's ticket and 404 for anyone else."""
    client, _, ent = env
    created = post(client, ALICE, "k1", ent, tomorrow()).json()
    url = f"/v1/tickets/{created['ticket_id']}"

    mine = client.get(url, headers={"Authorization": f"Bearer dev:{ALICE}"})
    assert mine.status_code == 200 and mine.json() == created
    assert client.get(url, headers={"Authorization": f"Bearer dev:{BOB}"}).status_code == 404


NBF, EXP = 1_798_844_400, 1_798_930_800  # 2027-01-01T23:00Z, 2027-01-02T23:00Z


@pytest.mark.parametrize(
    "clock, error",
    [
        (NBF - 1, jwt.ImmatureSignatureError),
        (NBF, None),
        (EXP - 1, None),
        (EXP, jwt.ExpiredSignatureError),
    ],
    ids=["before-nbf", "at-nbf", "before-exp", "at-exp"],
)
def test_time_claims_enforced_at_boundaries(monkeypatch, clock, error):
    """A standard JWT verifier (PyJWT, standing in for validator software) accepts
    nbf <= t < exp: rejected one second before nbf and exactly at exp."""
    token = signer.sign({"jti": "t", "nbf": NBF, "exp": EXP})

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock, tz)

    monkeypatch.setattr(jwt.api_jwt, "datetime", FixedClock)  # PyJWT reads its clock here
    if error:
        with pytest.raises(error):
            jwt.decode(token, signer.public_key, algorithms=["ES256"])
    else:
        assert jwt.decode(token, signer.public_key, algorithms=["ES256"])["jti"] == "t"


PG_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.mark.skipif(not PG_URL, reason="set TEST_POSTGRES_URL to a disposable PostgreSQL database")
def test_postgres_concurrent_identical_requests_return_same_ticket():
    """Two identical requests race: both miss the first lookup, the second waits for the
    entitlement lock, then must return the first request's ticket instead of a 409."""
    schema = "io_case_test"
    signing, release = threading.Event(), threading.Event()
    engine = create_engine(PG_URL, connect_args={"options": f"-c search_path={schema}"})
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(engine, expire_on_commit=False)
        account, ent = uuid.uuid4(), uuid.uuid4()
        with Session() as s:
            s.add(Entitlement(id=ent, account_id=account, product_code="DAY_2ND"))
            s.commit()

        class HeldSigner(DevSigner):
            """First request: pause inside signing while holding the entitlement lock."""
            def sign(self, claims):
                signing.set()
                assert release.wait(10)
                return super().sign(claims)

        class MustNotSign(DevSigner):
            """Second request must replay the stored ticket, never sign a new one."""
            def sign(self, claims):
                raise AssertionError("second request signed a new credential")

        travel = datetime.now(AMSTERDAM).date() + timedelta(days=1)
        results = {}

        def call(name, request_signer):
            with Session() as s:
                try:
                    results[name] = issue_ticket(s, request_signer, account, ent, travel, "same-key", "same-hash")
                except Exception as exc:  # surfaced by the asserts below
                    results[name] = exc

        first = threading.Thread(target=call, args=("first", HeldSigner()))
        first.start()
        assert signing.wait(10)  # first request holds the lock, not yet committed
        second = threading.Thread(target=call, args=("second", MustNotSign()))
        second.start()

        deadline = time.monotonic() + 10  # wait until the second request is blocked on the lock
        with engine.connect() as c:
            while not c.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )).scalar():
                assert time.monotonic() < deadline, "second request never waited for the lock"
                time.sleep(0.05)

        release.set()
        first.join(10)
        second.join(10)

        for name in ("first", "second"):
            assert not isinstance(results.get(name), Exception), f"{name} request failed: {results.get(name)!r}"
        (t1, created1), (t2, created2) = results["first"], results["second"]
        assert (created1, created2) == (True, False)
        assert t1.id == t2.id and t1.credential == t2.credential
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(Ticket)) == 1
    finally:
        release.set()  # never leave the first request holding its lock after a failure
        engine.dispose()
        with create_engine(PG_URL).begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
