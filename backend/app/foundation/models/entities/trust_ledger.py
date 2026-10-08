"""Append-only evidence ledgers behind "Can I trust it?" (ADR 0018).

Three tables, all written once and never updated (a Postgres trigger rejects
any UPDATE, see migration ``0130_trust_evidence_ledgers``):

* ``discover_candidate_snapshot`` — every universe member of every Discover
  run, frozen as issued: composite, rank among evaluable stocks, whether it was
  shortlisted and picked, and the entry close. The ``DiscoverCandidate`` rows
  are working state that later stages rewrite; this table is the evidence.
* ``candidate_outcome`` — one resolved forward return per snapshot and horizon
  (5, 10, 21, 63 trading days), in EUR, with the passive core's return over the
  same window.
* ``trust_daily_active_returns`` — the calendar-time daily series the
  pre-registered tests bet on: the mean daily active return of every open call
  (``ideas``, ``advisor``) and the rank-weighted long-short return of the open
  ranking cohorts (``ranking``).

Run ids are plain strings, not foreign keys: deleting a ``DiscoverRun`` must
not delete evidence about it. Ownership still cascades from ``users``.
"""
from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class DiscoverCandidateSnapshot(Base):
    """One universe member of one Discover run, frozen at issue."""

    __tablename__ = "discover_candidate_snapshot"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: UTC date of issue: the call enters at this day's close (ADR 0018 §3).
    issue_date: Mapped[date] = mapped_column(Date, index=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: "stock", "etf" or "other": ranks and the F2 test use stocks only.
    instrument_group: Mapped[str] = mapped_column(String(8))
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Passed every pipeline gate (no pipeline ``reject_stage``).
    evaluable: Mapped[bool] = mapped_column(Boolean)
    reject_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    composite: Mapped[float | None] = mapped_column(Float, nullable=True)
    composite_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    components_json: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Rank among evaluable stocks of the run, 1 = highest composite; NULL otherwise.
    stock_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_evaluable_stocks: Mapped[int] = mapped_column(Integer, default=0)

    shortlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    sector_capped: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Survived the tradeability gate and received a dossier slot.
    picked: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Last cached close known at issue, in the listing's quote currency.
    entry_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    cohort_id: Mapped[str] = mapped_column(String(64), index=True)
    provenance_json: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Rebuilt later from ``DiscoverCandidate`` rows: exploratory, never in a verdict.
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("run_id", "symbol", name="uq_candidate_snapshot_run_symbol"),
    )


class CandidateOutcome(Base):
    """A snapshot's forward return over one horizon, in EUR (ADR 0018 §5)."""

    __tablename__ = "candidate_outcome"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("discover_candidate_snapshot.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    horizon_days: Mapped[int] = mapped_column(Integer)
    #: The horizon's trading day on the benchmark calendar.
    target_date: Mapped[date] = mapped_column(Date)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ret_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_ret_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    excess_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: "resolved", "delisted" (scored at the last close) or "no_price".
    status: Mapped[str] = mapped_column(String(16))
    benchmark: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("snapshot_id", "horizon_days", name="uq_candidate_outcome_horizon"),
    )


class TrustDailyActiveReturn(Base):
    """One frozen day of a calendar-time series (ADR 0018 §3, §5)."""

    __tablename__ = "trust_daily_active_returns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: "ideas", "advisor" or "ranking".
    series: Mapped[str] = mapped_column(String(16))
    day: Mapped[date] = mapped_column(Date)
    #: Open calls (ideas, advisor) or open cohorts (ranking) on the day.
    n_open: Mapped[int] = mapped_column(Integer)
    #: Members priced at a carried-forward close (no fresh close that day).
    n_stale: Mapped[int] = mapped_column(Integer, default=0)
    #: The day's observation; NULL when nothing was open.
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark: Mapped[str] = mapped_column(String(32))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "series", "day", name="uq_trust_daily_series_day"),
    )


class TrustFactorStudy(Base):
    """The one result of the pre-registered factor study (ADR 0018 §8), per user and spec version.

    Written once, when the study first has enough out-of-sample days, and never
    updated: the decision it records (build, drop, or neither) is binding for
    its spec version. A new factor set is a new spec version, decided by a
    person, not a re-run.
    """

    __tablename__ = "trust_factor_study"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer)
    spec_json: Mapped[dict] = mapped_column(JSON, default=dict)
    #: "build" (R² >= 0.30), "drop" (< 0.15) or "neither".
    decision: Mapped[str] = mapped_column(String(16))
    oos_r2: Mapped[float] = mapped_column(Float)
    n_days: Mapped[int] = mapped_column(Integer)
    n_oos_days: Mapped[int] = mapped_column(Integer)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "spec_version", name="uq_trust_factor_study_spec"),
    )
