"""The nightly forward-test lab (plan 2026-11-09).

``ForwardLabSnapshot``: one row per signal per scoring day, holding every
universe name's score and the portfolio weights fixed from it *before* the
next day's prices exist. Written once and never updated: the row's
``created_at`` is the proof that the prediction came first.

``ForwardLabDaily``: the realised next-day return of each snapshot's
portfolio, one row per signal per scoring day.

``NewsToneScore``: the overnight LLM reading of one stock's headlines of one
day, recorded going forward only (a backtest of LLM news scores is inflated by
what the model memorised; Glasserman & Lin 2023, arXiv 2309.17322).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class ForwardLabSnapshot(Base):
    __tablename__ = "forward_lab_snapshots"
    __table_args__ = (UniqueConstraint("as_of", "signal", name="uq_forward_lab_snapshot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    signal: Mapped[str] = mapped_column(String(48))
    #: ``{symbol: score}``, higher = predicted better.
    scores_json: Mapped[dict] = mapped_column(JSON, default=dict)
    #: ``{"rank": {symbol: w}, "efficient": {symbol: w}}``; each leg sums to +1 / -1.
    weights_json: Mapped[dict] = mapped_column(JSON, default=dict)
    n: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ForwardLabDaily(Base):
    __tablename__ = "forward_lab_daily"
    __table_args__ = (UniqueConstraint("signal", "as_of", name="uq_forward_lab_daily"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    signal: Mapped[str] = mapped_column(String(48), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    #: The day whose return was earned (the next trading day after ``as_of``).
    day: Mapped[date] = mapped_column(Date)
    value_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_efficient: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Spearman correlation of the scores with the day's returns.
    rank_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class NewsToneScore(Base):
    __tablename__ = "news_tone_scores"
    __table_args__ = (UniqueConstraint("symbol", "day", name="uq_news_tone_score"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    #: The night the headlines were read; only headlines published before it count.
    day: Mapped[date] = mapped_column(Date, index=True)
    #: Mean of the per-headline tones in [-1, 1].
    tone: Mapped[float] = mapped_column(Float)
    n_headlines: Mapped[int] = mapped_column(Integer, default=0)
    #: ``{event_type: count}`` (earnings, guidance, deal, legal, management, product, macro, other).
    events_json: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
