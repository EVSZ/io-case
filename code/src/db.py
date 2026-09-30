"""Database schema and session.

PostgreSQL is the authoritative store in production; SQLite is used for local runs and
tests because PostgreSQL was not available in the development workspace. Correctness
rules (idempotency, one ticket per entitlement) live in constraints here, so they hold
no matter how many API replicas run concurrently.

Author:  Mateusz <mateusz@transcibe.ai>
Created: 2026-09-30
Project: IO Case - cloud ticketing for Dutch public transport, Part 3
"""

import os
import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tickets.db")

# One engine per process; it owns the connection pool. In production, point this at
# PgBouncer (port 6432) so scaled-out replicas cannot exhaust PostgreSQL connections.
engine = create_engine(DATABASE_URL)
# expire_on_commit=False: returned Ticket objects stay readable after commit.
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Common base for all ORM models; holds the table metadata used by create_all."""


class Entitlement(Base):
    """A paid right to one product. Created by the commerce system (not implemented here).

    Only the fields ticket issuance needs: who owns it and which product it grants.
    """

    __tablename__ = "entitlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    product_code: Mapped[str] = mapped_column(String(32))


class Ticket(Base):
    """An issued QR ticket: the entitlement turned into a signed, time-limited credential."""

    __tablename__ = "tickets"
    __table_args__ = (
        # A retry with the same key finds the original ticket instead of creating a second one.
        UniqueConstraint("account_id", "idempotency_key"),
        CheckConstraint("valid_from < valid_until"),
    )

    # Random UUIDv4 (122 random bits): unguessable, and doubles as the JWS "jti" claim.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # Copied from the authenticated caller, never from the request body.
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # One entitlement yields at most one ticket, enforced by the database, not by a prior check.
    entitlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entitlements.id"), unique=True)
    # Client-chosen retry key plus a SHA-256 of the request body, to tell a genuine retry
    # (same hash) from a reused key with a different request (conflict).
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    product_code: Mapped[str] = mapped_column(String(32))
    travel_date: Mapped[date] = mapped_column(Date)
    # UTC instants; valid_until is exclusive. SQLite drops the timezone, see tickets._utc().
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Which signing key was used, so validators can pick the right public key after rotation.
    kid: Mapped[str] = mapped_column(String(64))
    # Stored so a retry returns the same bytes; ECDSA signatures differ on every signing.
    credential: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
