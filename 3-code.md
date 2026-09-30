# 3. Example code: ticket creation

A small FastAPI service implementing `POST /v1/tickets` and `GET /v1/tickets/{ticket_id}` from Part 2. Code in [code/](code/). Development-grade, not production-ready.

| File | What |
|---|---|
| [code/src/tickets.py](code/src/tickets.py) | Core logic: `issue_ticket` |
| [code/src/db.py](code/src/db.py) | Database tables and constraints |
| [code/src/app.py](code/src/app.py) | HTTP routes, problem+json errors |
| [code/tests/test_tickets.py](code/tests/test_tickets.py) | Tests |

## How `issue_ticket` works

One database transaction:

1. **Retry?** If this account already used this idempotency key, return the stored ticket (or `409` if the request body differs).
2. **Ownership and lock:** load the entitlement with `SELECT ... FOR UPDATE`, only if it belongs to the signed-in account; otherwise `404`.
3. **Retry check again, under the lock.** An identical request that raced this one and committed first is found here and returned, instead of a `409`. Then an entitlement that already has a ticket gets `409`.
4. **Validity:** turn the travel date into an Amsterdam midnight-to-midnight window in UTC, on the server.
5. **Sign** the ticket claims (ES256).
6. **Save and commit.** The ticket and credential are returned only after the commit succeeds.

If the insert still hits a unique constraint, the code rolls back, re-reads, and returns the existing ticket or the matching `409`. The constraints are the backstop.

**Errors.** Domain errors and request-validation errors (malformed JSON, missing or unknown fields) are returned as `application/problem+json`. Validation errors list the field location and message but not the submitted values. Other framework errors, such as an unknown route, keep FastAPI's default JSON.

## Rationale

- **The database is the real guard.** Unique constraints on `(account, idempotency key)` and on `entitlement` prevent duplicates, even with many API instances running.
- **The signed credential is stored** with the ticket, so a retry returns identical bytes (ES256 signatures differ each time).
- **The account comes from the login**, never from the request body.

## Run and test

```bash
cd code
pip install -r requirements.txt   # or: pip install -e ".[dev]"
pytest
```

## Demo

The app seeds a demo account and one demo entitlement at startup.

```bash
cp .env.example .env                              # optional: set DEV_SIGNING_KEY, see below
uvicorn src.app:app --env-file .env               # terminal 1; API docs at http://127.0.0.1:8000/docs
```

In a second terminal (bash):

```bash
TRAVEL_DATE=$(python -c "from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo('Europe/Amsterdam')) + timedelta(days=1)).date())")
AUTH="Authorization: Bearer dev:00000000-0000-4000-8000-000000000001"
BODY="{\"entitlement_id\": \"00000000-0000-4000-8000-0000000000e1\", \"travel_date\": \"$TRAVEL_DATE\"}"

# 1. Issue a ticket: 201
curl -i -X POST http://127.0.0.1:8000/v1/tickets -H "$AUTH" -H "Idempotency-Key: demo-1" -H "Content-Type: application/json" -d "$BODY"
# 2. Retry with the same key: 201, same ticket, header Idempotent-Replayed: true
curl -i -X POST http://127.0.0.1:8000/v1/tickets -H "$AUTH" -H "Idempotency-Key: demo-1" -H "Content-Type: application/json" -d "$BODY"
# 3. New key, same entitlement: 409 entitlement-already-used, with the existing ticket_id
curl -i -X POST http://127.0.0.1:8000/v1/tickets -H "$AUTH" -H "Idempotency-Key: demo-2" -H "Content-Type: application/json" -d "$BODY"
# 4. Read it again (use the ticket_id from step 1)
curl -i http://127.0.0.1:8000/v1/tickets/<ticket_id> -H "$AUTH"
```

The seeded entitlement can issue **only one ticket**. After the first run, retrieve it with `GET` or by repeating the original request with the same key. The ticket is kept in the local `tickets.db`; delete that file to start the demo over.

## Development signing key

- Set `DEV_SIGNING_KEY` to an EC P-256 private key (PEM, on one line with `\n` separators). Every process and restart then signs with the same key, so earlier tickets keep verifying.
- If it is unset, each process makes a random key. That key is lost on restart unless it is retained somewhere, so earlier tickets can no longer be verified. The tests use this mode.
- The key ID is a truncated SHA-256 fingerprint of the public key (64 bits), so accidental collisions between different keys are very unlikely.
- A private key in an environment variable is acceptable for development only. Production keeps the signing key in Key Vault and keeps each verification key available until the last ticket signed with it has expired.

Generate a key:

```bash
python -c "from cryptography.hazmat.primitives.asymmetric import ec; from cryptography.hazmat.primitives import serialization as s; print(ec.generate_private_key(ec.SECP256R1()).private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode().replace(chr(10), r'\n'))"
```

## Tests

23 tests cover:
- **Idempotency and ownership:** a retry returns the same ticket; the same key with a different body gets `409`; another account's entitlement gets `404`; a new key for an issued entitlement gets `409`.
- **Validity:** a past date gets `422`; windows on normal and daylight-saving days; a JWT verifier (PyJWT, standing in for validator software) rejects a credential one second before `nbf` and exactly at `exp`, using a patched clock.
- **Credential:** signature verification and tamper detection.
- **API behavior:** validation errors as problem+json without echoed input; a signing failure returns `503`, saves nothing, and the same key succeeds afterwards; `GET` works for the owner and returns `404` for others.
- **Development key:** a shared key verifies across signers, different keys get different key IDs, a wrong key type is rejected.
- **PostgreSQL race (skipped unless configured):** two identical requests both miss the first lookup, the second waits for the entitlement lock, and both return the same ticket and credential with one row stored.

To run the PostgreSQL test, point it at a **disposable** database. It creates and drops its own schema, `io_case_test`.

```bash
pip install -e ".[dev,postgres]"
TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/dbname pytest -k postgres
```

**Results, 2026-09-30, Python 3.11.6:**
- 23 tests collected.
- Without `TEST_POSTGRES_URL`: 22 passed, 1 skipped.
- Against a disposable local PostgreSQL 16 container: 23 passed.
- With the re-check under the lock removed, the PostgreSQL test fails (the second request gets `409`), so the test detects this regression.

## Implemented vs proposed production

| Aspect | In this example | Proposed production |
|---|---|---|
| Sign-in | Development stub: `Bearer dev:<account-id>`, no verification | Verify Entra External ID tokens |
| Signing key | `DEV_SIGNING_KEY` environment variable, or random per process | Key Vault HSM key, never exported; each key version has its own key ID; public keys published to validators |
| Database | SQLite by default; issuance logic also tested on PostgreSQL 16 (race test). The HTTP app itself was only run on SQLite | PostgreSQL via `DATABASE_URL` |
| Schema | `create_all` at startup | Alembic migrations |
| Events | None published | `TicketIssued` outbox row in the same transaction (Part 1) |

**Not covered:** the unique-constraint backstop on PostgreSQL (with the lock in place, the race test does not reach it); a real validator; production authentication and signing.
