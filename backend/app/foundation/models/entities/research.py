from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, String, Text


class StockResearchReport(Base):
    """Cached LLM-generated stock research report (24h TTL)."""

    __tablename__ = "stock_research_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_json: Mapped[str] = mapped_column(Text)  # Full LLM output (markdown + structured)
    executive_summary: Mapped[str] = mapped_column(Text, server_default="")
    data_snapshot_json: Mapped[str] = mapped_column(Text, server_default="{}")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_stock_research_reports_ticker_user", "ticker", "user_id"),)


class MultiHorizonVerdict(Base):
    """Per-stock multi-horizon verdicts (JSON array of horizon verdicts)."""

    __tablename__ = "multi_horizon_verdicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    horizons_json: Mapped[str] = mapped_column(Text, server_default="[]")

    __table_args__ = (Index("ix_multi_horizon_verdicts_ticker_user", "ticker", "user_id"),)
