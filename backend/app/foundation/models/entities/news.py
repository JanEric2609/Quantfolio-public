from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, Float, ForeignKey, Index, Numeric, String, Text, UniqueConstraint


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (Index("uq_news_items_url", "url", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80))
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    sentiment_label: Mapped[str | None] = mapped_column(String(24))
    relevance_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    relevance_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    is_macro: Mapped[bool] = mapped_column(default=False)


class UserNewsRelevance(Base):
    __tablename__ = "user_news_relevance"
    __table_args__ = (UniqueConstraint("user_id", "news_item_id", name="uq_user_news_relevance"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    news_item_id: Mapped[str] = mapped_column(
        ForeignKey("news_items.id", ondelete="CASCADE"), index=True
    )
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    relevance_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
