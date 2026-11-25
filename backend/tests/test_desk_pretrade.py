"""Pre-trade context lines (display only): trend, range, volatility regime, stretch, events, live news."""
import math
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import pretrade
from app.foundation import technical
from app.foundation.core.db import Base
from app.foundation.models.entities import NewsWireItem

NOW = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _bars(closes):
    d = date(2025, 1, 1)
    out = []
    for i, c in enumerate(closes):
        out.append(technical.Bar(d + timedelta(days=i), c, c * 1.005, c * 0.995, c))
    return out


def test_too_few_bars_gives_nothing():
    assert pretrade.snapshot(_bars([100.0] * 30)) is None
    assert pretrade.lines(None, "long", 100.0, [], None) == []


def test_uptrend_is_up_and_says_whether_the_trade_goes_with_it():
    snap = pretrade.snapshot(_bars([100 + 0.4 * i for i in range(300)]))
    assert snap["trend"] == "up" and snap["atr_points"] > 0
    with_it = pretrade.lines(snap, "long", snap["atr_points"], [], None)
    against = pretrade.lines(snap, "short", snap["atr_points"], [], None)
    assert "goes with it" in with_it[0] and "goes against it" in against[0]
    assert "stop is 1.0 times that" in with_it[1]


def test_volatility_regime_high_after_a_calm_year_then_wild_weeks():
    calm = [100 + 0.5 * math.sin(i / 3) for i in range(250)]
    wild = [calm[-1] * (1 + (0.03 if i % 2 else -0.03)) for i in range(25)]
    assert pretrade.snapshot(_bars(calm + wild))["vol_regime"] == "high"
    assert pretrade.snapshot(_bars(calm + calm))["vol_regime"] in ("normal", "low", "high")


def test_events_from_the_desk_calendar_and_news_from_the_wire(monkeypatch):
    monkeypatch.setattr(pretrade.cal, "macro_events_between", lambda a, b: [("CPI", "2026-11-25")])
    assert pretrade.events(date(2026, 11, 25)) == ["CPI today"]
    db = _memory_db()
    db.add(NewsWireItem(id="alpaca:1", source="alpaca", headline="Fed speaker says cuts may pause", symbols="", relevance="central_bank",
                        received_at=NOW - timedelta(minutes=12), materiality=2, scope="market"))
    db.add(NewsWireItem(id="alpaca:2", source="alpaca", headline="Old news", symbols="", relevance="central_bank",
                        received_at=NOW - timedelta(hours=3), materiality=3, scope="market"))
    db.add(NewsWireItem(id="alpaca:3", source="alpaca", headline="Unrelated small cap", symbols="XYZ", relevance="",
                        received_at=NOW - timedelta(minutes=2), materiality=3, scope="company"))
    db.commit()
    assert pretrade.latest_news(db, "SPX", NOW) == ("Fed speaker says cuts may pause", 12)
    text = pretrade.lines(None, "long", 100.0, ["CPI today"], ("Fed speaker says cuts may pause", 12))
    assert text == ["Event: CPI today (prices can jump).", "News 12 min ago: Fed speaker says cuts may pause"]


def test_rsi_is_shown_in_plain_words():
    snap = pretrade.snapshot(_bars([100 + 0.4 * i for i in range(300)]))
    text = " ".join(pretrade.lines(snap, "long", snap["atr_points"], [], None))
    assert "RSI" in text and "overbought" in text
