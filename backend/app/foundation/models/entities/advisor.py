from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk


class AdvisorScorecard(Base):
    """Per-cohort 4-axis improvement scorecard for the advisor loop (PR1 E1).

    One row per (portfolio, window_end) upserted by the outcome job once
    predictions in the cohort mature. Axes:

    1. Risk-adjusted return — sharpe / sortino / calmar of the paper book
       over the window.
    2. Calibration — brier_avg / log_loss_avg of stated confidence vs
       realised hit.
    3. Magnitude accuracy — Mincer-Zarnowitz slope + R² of predicted vs
       realised return.
    4. Downside discipline — max_drawdown / cvar_95 of the paper book over
       the window.

    Axes that cannot be computed yet stay ``NULL`` — the UI renders those as
    "pending", never a placeholder value.
    """

    __tablename__ = "advisor_scorecards"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "window_end", name="uq_advisor_scorecard_window"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date, index=True)

    n_predictions: Mapped[int] = mapped_column(Integer, default=0)
    n_resolved: Mapped[int] = mapped_column(Integer, default=0)

    # Axis 1 — risk-adjusted return
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino: Mapped[float | None] = mapped_column(Float, nullable=True)
    calmar: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Probabilistic/Deflated Sharpe Ratio (Bailey & Lopez de Prado) — the
    # composite's risk_adjusted component uses psr directly (already a
    # probability in [0,1]) instead of sigmoid(sharpe), which treats a
    # short, noisy window's raw Sharpe as if it were already skill-adjusted.
    psr: Mapped[float | None] = mapped_column(Float, nullable=True)
    dsr: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Axis 2 — calibration
    brier_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_loss_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Bucketed Ranked Probability Score (F11) — the composite's calibration
    # component uses rps_avg when available (checks whether the predicted
    # p5/p95 magnitude band was calibrated) instead of brier_avg (sign
    # only), falling back to brier_avg for rows computed before this existed.
    rps_avg: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Axis 3 — magnitude accuracy (Mincer-Zarnowitz)
    mz_slope: Mapped[float | None] = mapped_column(Float, nullable=True)
    mz_r2: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Axis 4 — downside discipline
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    cvar_95: Mapped[float | None] = mapped_column(Float, nullable=True)

    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AdvisorStrategy(Base):
    """A named strategy = config + prompt variant + lessons + calibration state (PR2 C1).

    Exactly one ``champion`` per user at a time; at most one active
    ``challenger``. Promotion flips roles and records lineage via
    ``parent_strategy_id``. Retired strategies are kept for audit.
    """

    __tablename__ = "advisor_strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="champion", index=True)
    # The paper sleeve this strategy drives (advisor or challenger mandate).
    portfolio_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Signal weights / prompt framing / risk-ceiling overrides for the cycle.
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class StrategyLesson(Base):
    """One distilled reflection lesson from a scored cycle (PR2 A1).

    Bounded by rank decay + an active cap so the decision prompt never grows
    unbounded. ``tags_json`` carries structured signals/regime/sector tags.
    """

    __tablename__ = "strategy_lessons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("advisor_strategies.id", ondelete="CASCADE"), index=True
    )
    portfolio_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="SET NULL"), nullable=True
    )
    lesson_text: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_scorecard_id: Mapped[str | None] = mapped_column(
        ForeignKey("advisor_scorecards.id", ondelete="SET NULL"), nullable=True
    )
    rank: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AdvisorGraduationState(Base):
    """Current graduation switch for a user's champion (PR2 D2).

    One row per user. ``graduated`` is THE switch that lets the divergence
    diff emit real rec cards; de-graduation flips it back and pauses emission.
    """

    __tablename__ = "advisor_graduation_state"
    __table_args__ = (UniqueConstraint("user_id", name="uq_advisor_grad_state_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str | None] = mapped_column(
        ForeignKey("advisor_strategies.id", ondelete="SET NULL"), nullable=True
    )
    graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_transition_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AdvisorGraduationTransition(Base):
    """Audit trail of graduation flips (graduate / de-graduate) with reasons."""

    __tablename__ = "advisor_grad_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    to_graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    criteria_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
