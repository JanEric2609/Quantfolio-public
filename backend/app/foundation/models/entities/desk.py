"""The guarded derivatives desk's tables (ADR 0027), paper only in Phase 1.

``desk_events`` is an append-only, hash-chained ledger: every state change of
a position is one row, ``row_hash = sha256(prev_hash + canonical_json(...))``,
and PostgreSQL refuses UPDATE and DELETE on it. Payloads are canonical JSON
stored as ``Text`` with ``ensure_ascii=True`` (the prod database is SQL_ASCII).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
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
    # What exactly is bought: the issuer's product name from the quote, the WKN typed into the
    # broker app (German ISINs), the barrier (knock-out) or strike (warrant) and the expiry
    # (None = open end). A CFD row (Trading 212, manual) has a fractional ``quantity`` in
    # reference-price units instead of ``units``; quantity x entry_price is its EUR exposure.
    product_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    wkn: Mapped[str | None] = mapped_column(String(6), nullable=True)
    barrier_or_strike: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
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


class NewsWireItem(Base):
    """One headline or release from the news wire (ADR 0027 amendment 2026-11-20): logged, rated, maybe sent.

    Alert-only, except that the event desk's hype and news-drift cells read it to open *paper* positions (ADR 0028). ``id`` is
    ``<source>:<source id>`` so a re-delivered item is stored once.
    """

    __tablename__ = "news_wire_items"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # alpaca | fed | ecb | sec_8k
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    headline: Mapped[str] = mapped_column(String(400))
    symbols: Mapped[str] = mapped_column(String(200), default="")  # comma-separated
    url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Why it matters to the owner: "holding:NVDA", "desk:SPX", "central_bank", "" (not watched).
    relevance: Mapped[str] = mapped_column(String(64), default="")
    # The local model's reading: 0 (noise) to 3 (moves the price today); None = not rated.
    materiality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # up | down | unclear
    scope: Mapped[str | None] = mapped_column(String(8), nullable=True)  # company | sector | market
    rated_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sent | rate_capped | quiet | "" (not sent)
    alert: Mapped[str] = mapped_column(String(12), default="")


class DeskCfdJournal(Base):
    """The owner's own record of a manual Trading 212 CFD trade (one row per paper CFD signal).

    Owner input, outside the hash-chained ledger: nothing here changes a paper position or is scored.
    """

    __tablename__ = "desk_cfd_journal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    position_id: Mapped[str] = mapped_column(
        ForeignKey("desk_positions.id", ondelete="CASCADE"), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    market_key: Mapped[str] = mapped_column(String(24))  # DAX | ES50 | SPX | NDX, or US:NVDA (event desk)
    direction: Mapped[str] = mapped_column(String(5))
    currency: Mapped[str] = mapped_column(String(3))
    r1: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    index_level_est: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    eur_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    quantity_suggested: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    exposure_eur: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    stop_points: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    typical_30m_points: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    spread_points: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    enter_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # sent | entered | skipped | closed
    status: Mapped[str] = mapped_column(String(10), default="sent")
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_level: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_level: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    real_pnl_eur: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
