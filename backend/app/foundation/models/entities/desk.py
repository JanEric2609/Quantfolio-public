"""The guarded derivatives desk's tables (ADR 0027), paper only in Phase 1.

``desk_events`` is an append-only, hash-chained ledger: every state change of
a position is one row, ``row_hash = sha256(prev_hash + canonical_json(...))``,
and PostgreSQL refuses UPDATE and DELETE on it. Payloads are canonical JSON
stored as ``Text`` with ``ensure_ascii=True`` (the prod database is SQL_ASCII).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk

DESK_EVENT_KINDS = (
    "registered", "proposal", "candidates", "guard_verdict", "tap", "expired", "fill", "stop_set",
    "quote_tick", "news_seen", "exit", "halt", "resume", "external_trade", "signal", "error",
)
GENESIS_HASH = "0" * 64


class DeskEvent(Base):
    __tablename__ = "desk_events"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_desk_events_seq"),
        UniqueConstraint("prev_hash", name="uq_desk_events_prev_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    kind: Mapped[str] = mapped_column(String(24))
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    position_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))


class DeskPosition(Base):
    __tablename__ = "desk_positions"
    __table_args__ = (
        # Phase 2 only: one live position at a time. Unused while the desk is paper.
        Index(
            "uq_desk_positions_one_live_open", "mode", unique=True,
            postgresql_where=text("mode = 'live' AND status = 'open'"),
            sqlite_where=text("mode = 'live' AND status = 'open'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    cell: Mapped[str] = mapped_column(String(48), index=True)
    # owner_call | index_momentum | external
    signal: Mapped[str] = mapped_column(String(24))
    product_type: Mapped[str] = mapped_column(String(12))
    isin: Mapped[str] = mapped_column(String(12))
    issuer: Mapped[str | None] = mapped_column(String(32), nullable=True)
    underlying_isin: Mapped[str] = mapped_column(String(12))
    underlying_label: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[str] = mapped_column(String(5))
    liquid: Mapped[bool] = mapped_column(Boolean, default=False)
    leverage_or_omega: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    entry_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    max_exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    exit_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    net_eur: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # Round-trip context for scoring: the quoted spread at entry (fraction of mid), the underlying's
    # reference price at entry, and the last bid seen (with when) for the lost-quote rule.
    spread_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    underlying_entry: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    last_bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The underlying's own return over the holding, signed in the taken direction (a favourable
    # move is positive); the placebo flips it.
    underlying_move: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    # proposed | expired | rejected | open | closed
    status: Mapped[str] = mapped_column(String(10), default="proposed", index=True)
    would_take_live: Mapped[bool] = mapped_column(Boolean, default=False)
    prereg_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class DeskRegistration(Base):
    __tablename__ = "desk_registrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    prereg_hash: Mapped[str] = mapped_column(String(64), unique=True)
    spec: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
