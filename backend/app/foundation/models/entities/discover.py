import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text


class DiscoverRun(Base):
    """Discover pipeline run (Phase 3 — recommendation pipeline)."""

    __tablename__ = "discover_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stage_json: Mapped[str] = mapped_column(Text, default="{}")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped on every stage_json write — the reap/staleness logic uses this to
    # tell "still actively progressing" apart from "orphaned by a dead
    # process", independent of any in-process wall-clock state (e.g. the
    # orchestrator's local `started_at`) that a restarted process can't see.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=True
    )


class DiscoverCandidate(Base):
    """Candidate discovered during a Discover pipeline run."""

    __tablename__ = "discover_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("discover_runs.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reject_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scores_json: Mapped[str] = mapped_column(Text, default="{}")
    tradeable_json: Mapped[str] = mapped_column(Text, default="{}")
    dossier_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendation_dossiers.id", ondelete="SET NULL"), nullable=True
    )
    recommendation_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True
    )


class DiscoveryPrediction(Base):
    """Immutable forward-prediction ledger row (Discovery P0 — #111).

    One row is written per shortlist candidate on each discovery run. The row
    captures the point-in-time signal snapshot (``features_json``) and price
    (``price_at_prediction``) so outcomes can later be scored without
    lookahead bias. ``predicted_at`` is immutable; outcome fields
    (``realised_return`` / ``score_json``) stay ``NULL`` until the P1
    resolution job fills them.

    Forward-predictor fields (``conviction_calibrated``, ``expected_return*``,
    ``thesis`` / ``risks``) are nullable at P0 — they are populated once the P2
    LLM predictor + calibrator land. At P0 ``conviction`` holds the raw
    composite signal score so the ledger is never empty.
    """

    __tablename__ = "discovery_prediction"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Groups a batch of predictions from one discovery run (DiscoverRun.id).
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    # Point-in-time identity.
    asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)

    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    horizon_days: Mapped[int] = mapped_column(Integer)
    resolve_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    direction: Mapped[str] = mapped_column(String(8), default="buy")
    conviction: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction_calibrated: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_high: Mapped[float | None] = mapped_column(Float, nullable=True)

    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Signal snapshot at prediction time (auditability) + outcome computation.
    features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    price_at_prediction: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Filled at horizon resolution by the P1 scoring job.
    realised_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Same window, same EUR basis: the passive core's move and the
    # direction-signed return in excess of it. ``excess_return > 0`` is a hit.
    benchmark_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    excess_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    score_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Compliance flags — every prediction output is an estimate, not advice.
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa.true())
    is_financial_advice: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false()
    )
    config_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Paper sleeve attribution (advisor loop PR2): which paper portfolio's
    # cycle produced this prediction. NULL for pure discover predictions.
    portfolio_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # What produced this call (ADR 0018 §10): cohort id, code sha, config and
    # prompt hashes, served model, calibrator version, degradation flags.
    # NULL on rows written before the stamp existed (the legacy cohort).
    provenance_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)


class DiscoverySkillSnapshot(Base):
    """Periodic aggregate skill snapshot for Discovery predictions (P1 — #112).

    Stores rolling aggregate metrics computed from resolved
    ``DiscoveryPrediction`` rows — Rank IC, Brier score, hit-rate, ECE, and
    (despite the ``mincer_a0``/``mincer_a1`` column names) a logistic
    (Platt-scaling) fit of hit-probability on conviction, not a real
    Mincer-Zarnowitz regression — see
    ``app.decision.discover.scoring._fit_hit_probability_logit`` (F14) —
    so the Discover UI can surface "is the system actually getting better
    at predicting?" as a trend chart.
    """

    __tablename__ = "discovery_skill_snapshot"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )
    metric_type: Mapped[str] = mapped_column(String(32), default="rolling_12m")
    total_predictions: Mapped[int] = mapped_column(Integer, default=0)
    total_resolved: Mapped[int] = mapped_column(Integer, default=0)
    hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    brier_score_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    mincer_a0: Mapped[float | None] = mapped_column(Float, nullable=True)
    mincer_a1: Mapped[float | None] = mapped_column(Float, nullable=True)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    config_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class DiscoveryConfig(Base):
    """Configuration registry for Discovery self-improvement loop (P3a)."""

    __tablename__ = "discovery_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    config_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="challenger")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    parent_config_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("discovery_config.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    champion_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # sha256[:12] of the code-level default (DEFAULT_SIGNAL_WEIGHTS/etc. in
    # services/discover/config.py) this row was seeded from, or last approved
    # against at activation time. NULL means "never reviewed against a code
    # default" (e.g. a manual challenger never promoted). Compared against the
    # *current* code-default hash to detect drift — see
    # services/discover/config.py:is_config_stale (Phase 5 of
    # unified-portfolio-engine-implementation.md).
    code_default_hash: Mapped[str | None] = mapped_column(String(12), nullable=True)

    __table_args__ = (sa.Index("ix_discovery_config_type_status", "config_type", "status"),)


class DiscoveryConfigReview(Base):
    """Review record for a DiscoveryConfig promotion decision (P3a)."""

    __tablename__ = "discovery_config_review"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    config_type: Mapped[str] = mapped_column(String(32), nullable=False)
    champion_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("discovery_config.id"), nullable=False
    )
    challenger_results: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    promoted_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("discovery_config.id"), nullable=True
    )
    rejected_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AcTrialLedger(Base):
    """Immutable AlphaCrafter trial row from the discover alpha-miner stage.

    One row per ``(run_id, factor_name, symbol, config_hash)`` evaluation,
    written on the success path of ``stage_alpha_miner`` (M3a). ``ic`` is
    SIGNED — negative means the factor is anti-predictive over the evaluation
    window. Audit F13 (docs/archive/audits/2026-08-discover-maths): the pipeline
    previously aggregated ``abs(ic)``, which made a strongly anti-predictive
    factor score identically to a predictive one and corrupted composite
    scoring whenever AlphaCrafter was available; persisting the signed value
    keeps the ledger faithful to what the miner measured.

    No rows have been written since 2026-09-28: ``stage_alpha_miner`` now
    scores the candidate's exposure to already-validated FactorsLibrary
    factors, which evaluates no new trial. Existing rows stay readable
    (``/api/evidence``).

    Idempotency is enforced by the unique index ``uq_ac_trial_append`` so
    retried runs cannot double-count evidence. ``evaluated_at`` is
    deliberately NOT part of uniqueness — re-evaluating the same factor under
    the same config within one run is a duplicate, not new history.
    """

    __tablename__ = "ac_trial_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    # No plain run_id index: migration 0096 relies on uq_ac_trial_append
    # (which leads on run_id) for lookups — keep the model schema-identical.
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    factor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    # SIGNED information coefficient — negative = anti-predictive (F13).
    ic: Mapped[float] = mapped_column(Float, nullable=False)
    icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_obs: Mapped[int] = mapped_column(Integer, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )

    __table_args__ = (
        sa.Index(
            "uq_ac_trial_append",
            "run_id",
            "factor_name",
            "symbol",
            "config_hash",
            unique=True,
        ),
        sa.Index("ix_ac_trial_lookup", "factor_name", "config_hash", "evaluated_at"),
    )
