from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, JSON, Numeric, String, Text


class FactorsLibrary(Base):
    """Factor definitions in the AlphaCrafter Miner (Phase 4)."""

    __tablename__ = "factors_library"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    name: Mapped[str] = mapped_column(String(160), index=True)
    formula_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    ic_summary_json: Mapped[str] = mapped_column(Text)
    regime_applicability: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AlphaSignal(Base):
    """Alpha signal values per factor and symbol (Phase 4, hypertable with 7-day chunks)."""

    __tablename__ = "alpha_signals"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, index=True)
    factor_id: Mapped[str] = mapped_column(
        ForeignKey("factors_library.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    ic_window_value: Mapped[float | None] = mapped_column(Float, nullable=True)


class ScreenerRun(Base):
    """Screener run result: regime-gated factor selection (Phase 4)."""

    __tablename__ = "screener_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    regime_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    selected_factor_ids: Mapped[str] = mapped_column(Text)
    scores_json: Mapped[str] = mapped_column(Text)


class TraderBacktest(Base):
    """Trader vectorbt backtest result (Phase 4)."""

    __tablename__ = "trader_backtests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    screener_run_id: Mapped[str] = mapped_column(
        ForeignKey("screener_runs.id", ondelete="CASCADE"), index=True
    )
    spec_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )


class RecommendationDossier(Base):
    """Research-grade recommendation dossier from AlphaCrafter (Phase 4)."""

    __tablename__ = "recommendation_dossiers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    # Nullable for legacy rows created before user scoping; NULL rows are admin-visible only.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    conviction: Mapped[float] = mapped_column(Float, index=True)
    dossier_json: Mapped[str] = mapped_column(Text)
    attribution_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("attribution_runs.id", ondelete="SET NULL"), nullable=True
    )
    mc_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("mc_runs.id", ondelete="SET NULL"), nullable=True
    )
    goal_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    aspect_ids: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlphacrafterJobRun(Base):
    """Async AlphaCrafter pipeline job run (Phase 4 v2 — async progress tracking)."""

    __tablename__ = "alphacrafter_job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    # Nullable for legacy rows created before user scoping; NULL rows are admin-visible only.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    shared_memory: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlphacrafterTuningTrial(Base):
    """One grid-search configuration evaluation from the quarterly tuning job.

    EVERY evaluated combination is logged here (audit-fixes-2026-08 todo 20,
    per AFML ch.12 "log every trial") so selection bias is auditable after
    the fact: ``dsr`` holds the IC-Sharpe-analog deflated probability (see
    services/alphacrafter/tuning.py — returns-based DSR does not apply to IC
    series), and only the configuration finally chosen by the plateau rule
    carries ``accepted=True``. Disposition vs ``AcTrialLedger`` (discover.py):
    that ledger records PER-FACTOR miner evaluations keyed by (run_id,
    factor, symbol); this table records PER-CONFIG trader-sweep trials —
    different grain, complementary ledgers, no duplication.
    """

    __tablename__ = "alphacrafter_tuning_trials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    job_run_id: Mapped[str] = mapped_column(
        ForeignKey("alphacrafter_job_runs.id", ondelete="CASCADE"), index=True
    )
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_mean_ic: Mapped[float] = mapped_column(Float, nullable=False)
    oos_icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    wfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    dsr: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )
