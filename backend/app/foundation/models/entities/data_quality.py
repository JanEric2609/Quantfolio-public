"""Second-source checks of stored prices, and event/news records (plan 2026-11-09).

``PriceCrossCheck``: one row per symbol per night it was compared with a
second provider: how far the daily returns disagreed and which known error
pattern (if any) the disagreement matched.

``Sec8kEvent``: one row per 8-K (or 8-K/A) a universe member filed, with its
item codes and EDGAR's acceptance time — structured events, not headlines.

``NewsMention``: one row per article GDELT matched to a universe member,
de-duplicated per title and day; the input of the non-US news tone.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class PriceCrossCheck(Base):
    __tablename__ = "price_cross_checks"
    __table_args__ = (UniqueConstraint("symbol", "checked_on", "reference", name="uq_price_cross_check"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    checked_on: Mapped[date] = mapped_column(Date, index=True)
    #: The second provider the stored series was compared with.
    reference: Mapped[str] = mapped_column(String(24))
    n_days: Mapped[int] = mapped_column(Integer, default=0)
    #: Largest absolute difference of one day's log return, stored minus reference.
    max_return_diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: ``ok`` | ``warn`` | ``error`` | ``no_reference``.
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    #: ``split_unadjusted`` | ``scale_x100`` | ``dividend_mismatch`` | ``stale`` | ``spike`` | ``level_break``.
    pattern: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: What was done about it: ``none`` | ``repulled`` | ``tie_break_disagreed``.
    action: Mapped[str] = mapped_column(String(24), default="none")
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Sec8kEvent(Base):
    __tablename__ = "sec_8k_events"

    accession: Mapped[str] = mapped_column(String(24), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cik: Mapped[str] = mapped_column(String(10), index=True)
    form: Mapped[str] = mapped_column(String(8))
    filed_on: Mapped[date] = mapped_column(Date, index=True)
    #: EDGAR's acceptance time: when the market could first read it.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Comma-separated item codes, e.g. ``"2.02,9.01"``.
    items: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class NewsMention(Base):
    __tablename__ = "news_mentions"
    __table_args__ = (UniqueConstraint("symbol", "day", "title_key", name="uq_news_mention"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    #: UTC day GDELT saw the article.
    day: Mapped[date] = mapped_column(Date, index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="gdelt")
    outlet: Mapped[str | None] = mapped_column(String(120), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    #: Lower-cased, punctuation-free title: syndicated copies collapse onto it.
    title_key: Mapped[str] = mapped_column(String(200))
    #: GDELT's own lexicon tone (V2Tone, first field); kept for comparison only.
    gdelt_tone: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
