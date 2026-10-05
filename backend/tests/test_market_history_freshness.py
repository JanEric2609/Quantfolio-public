"""M2 freshness tests: real staleness on market.history's bar_prices branch.

Audit F16/F21: the bar branch hardcoded ``"stale": False``, so a benchmark
series frozen for weeks (SPY, 5 weeks in the audit) was served as fresh and
silently truncated discover anchors. These tests pin:

- vintage semantics: ``stale = (now_utc - max(bar ts)) > history_stale_days``
  with the public setting clamped to [1, 30] (default 7 calendar days);
- rows are still RETURNED when stale — read-path shape unchanged, no
  synchronous provider fetch introduced;
- the flag reaches ``stage_backtest_vs_benchmark`` concerns.

Vintage-vs-TTL distinction (audit F21): the PriceCache branch's
``_is_fresh(fetched_at, HISTORY_TTL)`` measures *fetch recency* of cached
quotes; this gate measures *data vintage* (how old the newest bar is). They
are deliberately different concepts and must not be "aligned".

bar_prices is a raw-SQL hypertable accessed via BarStore; create it explicitly
with UNIQUE(symbol, ts) per tests/test_alphacrafter_panel.py's shim pattern.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.market import history
from app.foundation.settings import upsert_public_settings


# One-minute under-margin keeps the strict ``age > N days`` boundary
# deterministic against test-execution latency (a literally exact boundary
# would flip stale the moment evaluation happens microseconds after seeding).
_BOUNDARY_GUARD = timedelta(minutes=1)


def _memory_db_bars():
    """_memory_db variant that also creates the raw-SQL bar_prices table."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bar_prices (
                symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                close REAL, volume REAL, currency TEXT, provider TEXT,
                UNIQUE (symbol, ts)
            )
        """))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_bars(db, ticker: str, n: int = 30, newest_age_days: float = 1.0) -> None:
    """Insert *n* calendar-daily bars ending *newest_age_days* before now (naive ts)."""
    end = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=newest_age_days)
    dates = [end - timedelta(days=i) for i in range(n)][::-1]
    for i, dd in enumerate(dates):
        db.execute(text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :o, :h, :l, :c, :v, 'USD', 'test')
        """), {
            "symbol": ticker.upper(), "ts": dd, "o": 100.0, "h": 101.0,
            "l": 99.0, "c": 100.0 + i * 0.01, "v": 1000.0,
        })
    db.commit()


def _seed_price_cache(db, ticker: str, closes: list[float]) -> None:
    """Seed PriceCache business-day closes ending yesterday (fresh fetch)."""
    end = date.today()
    days: list[date] = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    for dd, close in zip(days, closes):
        db.add(_price_row(ticker, dd, close, now))
    db.commit()


def _price_row(ticker: str, dd: date, close: float, now: datetime):
    from app.foundation.models.entities import PriceCache

    return PriceCache(
        id=uuid4().hex, ticker=ticker.upper(), date=dd,
        close=Decimal(str(round(close, 4))), fetched_at=now,
        source="test", stale=False, currency="USD",
    )


# ---------------------------------------------------------------------------
# market.history bar-branch staleness
# ---------------------------------------------------------------------------


def test_series_ending_far_past_flagged_stale():
    """THE acceptance test: newest bar 45 days old → every row stale=True."""
    db = _memory_db_bars()
    _seed_bars(db, "DEAD.CO", n=30, newest_age_days=45)

    rows = history(db, "DEAD.CO", days=365)

    assert rows, "bar path should still return cached rows"
    assert all(r["stale"] is True for r in rows)


def test_fresh_series_remains_stale_false():
    db = _memory_db_bars()
    _seed_bars(db, "LIVE.CO", n=30, newest_age_days=1)

    rows = history(db, "LIVE.CO", days=365)

    assert rows
    assert all(r["stale"] is False for r in rows)


def test_boundary_pins_default_seven_days():
    """Documents the default constant: 7-day-old bars fresh, 8-day-old stale."""
    db = _memory_db_bars()
    # Just inside the 7-day line (one minute of guard against exec latency).
    end = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7) + _BOUNDARY_GUARD
    for i in range(10):
        db.execute(text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES ('EDGE7', :ts, 100, 101, 99, 100, 1000, 'USD', 'test')
        """), {"ts": end - timedelta(days=i)})
    db.commit()

    rows = history(db, "EDGE7", days=365)
    assert rows
    assert all(r["stale"] is False for r in rows)

    db_stale = _memory_db_bars()
    _seed_bars(db_stale, "EDGE8", n=10, newest_age_days=8)

    rows = history(db_stale, "EDGE8", days=365)
    assert all(r["stale"] is True for r in rows)


def test_allow_live_false_respects_staleness_without_fetch(monkeypatch):
    """Stale bars + allow_live=False → rows returned flagged stale, no provider call."""
    def _no_registry(db):
        raise AssertionError("provider registry must not be built in the read path")

    monkeypatch.setattr("app.foundation.market.build_provider_registry", _no_registry)

    db = _memory_db_bars()
    _seed_bars(db, "NOLIVE", n=30, newest_age_days=45)

    rows = history(db, "NOLIVE", days=365, allow_live=False)

    assert rows
    assert all(r["stale"] is True for r in rows)


def test_settings_value_respected_and_clamped():
    """history_stale_days is honoured and clamped to [1, 30]."""
    # 30 respected: 10-day-old bars are fresh at tolerance 30.
    db = _memory_db_bars()
    upsert_public_settings(db, {"history_stale_days": 30})
    _seed_bars(db, "SET30", n=10, newest_age_days=10)
    assert all(r["stale"] is False for r in history(db, "SET30", days=365))

    # 0 clamps to 1: 2-day-old bars are stale again.
    db = _memory_db_bars()
    upsert_public_settings(db, {"history_stale_days": 0})
    _seed_bars(db, "SET0", n=10, newest_age_days=2)
    assert all(r["stale"] is True for r in history(db, "SET0", days=365))

    # 99 clamps to 30: 10-day-old bars stay fresh (default 7 would flag them).
    db = _memory_db_bars()
    upsert_public_settings(db, {"history_stale_days": 99})
    _seed_bars(db, "SET99", n=10, newest_age_days=10)
    assert all(r["stale"] is False for r in history(db, "SET99", days=365))


def test_naive_ts_normalized_no_crash():
    """Naive timestamps (SQLite drops tzinfo) are treated as UTC, not crashing."""
    db = _memory_db_bars()
    _seed_bars(db, "NAIVE", n=30, newest_age_days=45)

    rows = history(db, "NAIVE", days=365)

    assert rows
    assert all(r["stale"] is True for r in rows)


# ---------------------------------------------------------------------------
# Discover wiring: stage_backtest_vs_benchmark surfaces staleness concerns
# ---------------------------------------------------------------------------


def test_backtest_stage_flags_stale_benchmark_concern():
    """A frozen benchmark series must surface as an advisory concern mentioning
    the benchmark symbol (audit F16), while excess return still computes."""
    from app.decision.discover.pipeline import stage_backtest_vs_benchmark

    db = _memory_db_bars()
    _seed_price_cache(db, "CAND", [100.0 + i * 0.05 for i in range(300)])
    # SPY: ~450 calendar-daily bars ending 45 days ago → >=250 aligned obs.
    _seed_bars(db, "SPY", n=450, newest_age_days=45)

    scores, reject = stage_backtest_vs_benchmark(db, "CAND", is_etf=False)

    assert reject is None
    assert scores is not None
    assert scores["excess_return_annual"] is not None  # scored path, not neutral
    assert any(c.startswith("SPY data stale since") for c in scores["concerns"])


# ---------------------------------------------------------------------------
# refresh_stale_bars: explicit catch-up for series nothing re-ingests
# ---------------------------------------------------------------------------


class _FakeIngester:
    calls: list[tuple[str, str | None]] = []

    def __init__(self, db):
        self.db = db

    def ingest_bar_prices(self, symbol, start_date=None, end_date=None, days=None):
        _FakeIngester.calls.append((symbol, start_date))
        self.db.execute(text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:s, :ts, 1, 1, 1, 0.86, 0, 'EUR', 'fake')
        """), {"s": symbol, "ts": datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)})
        self.db.commit()
        return {"success": True, "symbol": symbol}


@pytest.fixture
def fake_ingester(monkeypatch):
    import app.foundation.market as market_module

    _FakeIngester.calls = []
    market_module._bar_refresh_attempts.clear()
    monkeypatch.setattr("app.foundation.data_backbone.ingest.DataIngester", _FakeIngester)
    yield _FakeIngester
    market_module._bar_refresh_attempts.clear()


def test_refresh_stale_bars_catches_up_from_last_bar(fake_ingester):
    """The prod USDEUR=X case: bars frozen 25 days ago are ingested from the
    day before the newest bar, and history() then serves a fresh series."""
    from app.foundation.market import refresh_stale_bars

    db = _memory_db_bars()
    _seed_bars(db, "USDEUR=X", n=30, newest_age_days=25)
    newest = (datetime.now(UTC) - timedelta(days=25)).date()

    assert refresh_stale_bars(db, "usdeur=x") is True
    assert fake_ingester.calls == [("USDEUR=X", (newest - timedelta(days=1)).isoformat())]
    rows = history(db, "USDEUR=X", days=365)
    assert rows[-1]["close"] == pytest.approx(0.86)
    assert rows[-1]["stale"] is False


def test_refresh_stale_bars_skips_fresh_and_empty_series(fake_ingester):
    from app.foundation.market import refresh_stale_bars

    db = _memory_db_bars()
    _seed_bars(db, "FRESH", n=10, newest_age_days=1)

    assert refresh_stale_bars(db, "FRESH") is False
    # No stored bars: history()'s own live fallback owns that case.
    assert refresh_stale_bars(db, "NOBARS") is False
    assert fake_ingester.calls == []


def test_refresh_stale_bars_retries_once_per_window(fake_ingester, monkeypatch):
    """A symbol providers no longer serve costs one live call per window."""
    from app.foundation.market import refresh_stale_bars

    monkeypatch.setattr(
        _FakeIngester, "ingest_bar_prices",
        lambda self, symbol, **kw: (_FakeIngester.calls.append((symbol, kw.get("start_date"))), {"success": False})[1],
    )
    db = _memory_db_bars()
    _seed_bars(db, "GONE", n=10, newest_age_days=20)

    assert refresh_stale_bars(db, "GONE") is False
    assert refresh_stale_bars(db, "GONE") is False
    assert len(fake_ingester.calls) == 1


def test_discover_fx_series_is_refreshed_and_cached_per_day(fake_ingester, monkeypatch):
    """Discover's FX leg catches the pair up, and its cache is keyed by day so
    a long-lived worker does not replay the first run's series forever."""
    import app.decision.discover.pipeline as pipeline_module

    db = _memory_db_bars()
    _seed_bars(db, "USDEUR=X", n=30, newest_age_days=25)
    pipeline_module._FX_SERIES_CACHE.clear()
    try:
        df = pipeline_module._get_fx_series(db, "EUR")
        assert df is not None
        assert df["close"].iloc[-1] == pytest.approx(0.86)
        assert list(pipeline_module._FX_SERIES_CACHE) == [("USDEUR=X", date.today())]
        pipeline_module._get_fx_series(db, "EUR")
        assert len(fake_ingester.calls) == 1
    finally:
        pipeline_module._FX_SERIES_CACHE.clear()
