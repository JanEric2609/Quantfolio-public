from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text


class QuantRun(Base):
    __tablename__ = "quant_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    type: Mapped[str] = mapped_column(String(40), index=True)
    input_json: Mapped[str] = mapped_column(Text)
    output_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class QuantExperiment(Base):
    __tablename__ = "quant_experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(24), default="long_term")
    universe_json: Mapped[str] = mapped_column(Text, default="[]")
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    rebalance_frequency: Mapped[str] = mapped_column(String(24), default="monthly")
    strategy_type: Mapped[str] = mapped_column(String(80), default="momentum")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class QuantExperimentRun(Base):
    __tablename__ = "quant_experiment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("quant_experiments.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_curve_json: Mapped[str] = mapped_column(Text, default="[]")
    trades_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str | None] = mapped_column(Text)


class QuantSignal(Base):
    __tablename__ = "quant_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("quant_experiment_runs.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    signal_name: Mapped[str] = mapped_column(String(80))
    signal_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    as_of_date: Mapped[date | None] = mapped_column(Date)


class QuantFactorScore(Base):
    __tablename__ = "quant_factor_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("quant_experiment_runs.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    factor_name: Mapped[str] = mapped_column(String(80))
    factor_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    factor_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    as_of_date: Mapped[date | None] = mapped_column(Date)


class QuantSavedScenario(Base):
    __tablename__ = "quant_saved_scenarios"
    __table_args__ = (Index("ix_quant_saved_scenarios_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    annual_return: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    annual_volatility: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    years: Mapped[int] = mapped_column()
    simulations: Mapped[int] = mapped_column()
    start_value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    p05_terminal: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    median_terminal: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    p95_terminal: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fan_data_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class StrategyGraveyardEntry(Base):
    __tablename__ = "strategy_graveyard"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(Text)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    failed_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class QuantMlModel(Base):
    __tablename__ = "quant_ml_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24), default="lgbm", index=True)  # lgbm|xgb|mlp
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    artefact_path: Mapped[str | None] = mapped_column(Text)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which symbol this model was trained on — nullable for models trained
    # before this column existed. Lets stage_ml_signal (discover pipeline)
    # look up "is there a validated model for this candidate's ticker?".
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    # Bumped (app.lab.quant_ml.training.CURRENT_FEATURE_SCHEMA_VERSION)
    # whenever build_features' output column set changes in a way that
    # invalidates an already-fitted pipeline artefact. Defaults to 0 for
    # every row trained before this column existed (pre-Phase-5) — readers
    # loading a persisted artefact (stage_ml_signal, /ml/models/{id}/predict)
    # treat a version mismatch as "no model", never attempt to predict with it.
    feature_schema_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class QuantRlPolicy(Base):
    __tablename__ = "quant_rl_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    env_id: Mapped[str] = mapped_column(String(80), index=True)
    algo: Mapped[str] = mapped_column(String(24), default="ppo")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    artefact_path: Mapped[str | None] = mapped_column(Text)
    training_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
