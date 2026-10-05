from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk


class MetricsSnapshot(Base):
    """Unified risk/return snapshot, shared across every caller that used to
    keep its own ad hoc sharpe/max_drawdown columns (``PaperSnapshot``,
    ``AdvisorScorecard``).

    ``context`` distinguishes independently-computed snapshots of the same
    portfolio on the same day (e.g. daily NAV tracking vs. the advisor loop's
    rolling-window scorecard) that would otherwise collide on
    ``(portfolio_id, as_of)``.
    """

    __tablename__ = "metrics_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "as_of", "context", name="uq_metrics_snapshot_portfolio_asof_context"
        ),
        Index("ix_metrics_snapshots_portfolio_context", "portfolio_id", "context"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[date] = mapped_column(Date, index=True)
    context: Mapped[str] = mapped_column(String(32))

    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino: Mapped[float | None] = mapped_column(Float, nullable=True)
    calmar: Mapped[float | None] = mapped_column(Float, nullable=True)
    cvar_95: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
