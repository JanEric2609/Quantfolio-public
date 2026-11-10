"""E2 (plan 2026-10-14): Discover's warm-up fetches only the missing days of a freshly re-pulled series."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.decision.discover import pipeline
from app.foundation.models.entities import BarSeriesMeta
from tests.test_discover_benchmark_persist import _test_db

NOW = datetime(2026, 10, 19, 5, 0)  # a Monday, the morning after the Sunday re-pull


def _db(*, years: float = 5.2, last_age_days: int = 3, repulled_days_ago: float | None = 1.0, quarantine=None):
    engine, factory = _test_db()
    db = factory()
    last = NOW - timedelta(days=last_age_days)
    first = NOW - timedelta(days=round(365 * years))
    db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('AAA', :a, 1), ('AAA', :b, 2)"),
               {"a": first, "b": last})
    db.add(BarSeriesMeta(
        symbol="AAA", provider="yfinance", quarantine_reason=quarantine,
        last_full_repull_at=(NOW - timedelta(days=repulled_days_ago)).replace(tzinfo=UTC)
        if repulled_days_ago is not None else None,
    ))
    db.commit()
    return db, factory


def test_a_freshly_repulled_full_series_fetches_from_the_day_before_its_last_bar():
    db, _ = _db()
    assert pipeline._preingest_start(db, "AAA", NOW) == (NOW - timedelta(days=4)).date().isoformat()


def test_the_full_window_is_fetched_otherwise():
    assert pipeline._preingest_start(_db(repulled_days_ago=None)[0], "AAA", NOW) is None  # never re-pulled
    assert pipeline._preingest_start(_db(repulled_days_ago=9)[0], "AAA", NOW) is None     # missed a Sunday
    assert pipeline._preingest_start(_db(years=2)[0], "AAA", NOW) is None                 # shallow
    assert pipeline._preingest_start(_db(last_age_days=20)[0], "AAA", NOW) is None        # stale tail
    assert pipeline._preingest_start(_db(quarantine="split_unadjusted")[0], "AAA", NOW) is None
    assert pipeline._preingest_start(_db()[0], "BBB", NOW) is None                        # no series


def test_preingest_one_passes_the_start_or_the_window(monkeypatch):
    _, factory = _db()
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    calls: list[dict] = []

    class _Fake:
        def __init__(self, db):
            pass

        def ingest_bar_prices(self, symbol, **kw):
            calls.append({"symbol": symbol, **kw})
            return {"success": True}

    monkeypatch.setattr(pipeline, "DataIngester", _Fake)
    monkeypatch.setattr(pipeline, "_preingest_start", lambda db, s: "2026-10-15" if s == "AAA" else None)
    assert pipeline._preingest_one("AAA") == ("AAA", True)
    assert pipeline._preingest_one("BBB") == ("BBB", True)
    assert calls == [{"symbol": "AAA", "start_date": "2026-10-15"},
                     {"symbol": "BBB", "days": pipeline._PREINGEST_DAYS}]


def test_a_two_bar_fetch_does_not_pin_the_series():
    import pandas as pd

    from app.foundation.data_backbone.ingest import DataIngester

    short = pd.DataFrame({"ts": pd.to_datetime(["2026-11-09", "2026-11-10"])})
    assert DataIngester._series_too_short(short, None, 1260) is True
    long = pd.DataFrame({"ts": pd.date_range("2025-01-01", periods=300, freq="B")})
    assert DataIngester._series_too_short(long, None, 1260) is False
    assert DataIngester._series_too_short(short, None, None) is False  # nothing asked for by date
    near = pd.DataFrame({"ts": pd.to_datetime(["2026-11-05", "2026-11-10"])})
    assert DataIngester._series_too_short(near, "2026-11-04", None) is False


def test_a_short_series_counts_as_a_failed_warm_up(monkeypatch):
    _, factory = _db()
    monkeypatch.setattr(pipeline, "SessionLocal", factory)

    class _Short:
        def __init__(self, db):
            pass

        def ingest_bar_prices(self, symbol, **kw):
            return {"success": True, "short": True, "message": "short history: 2 bars, series not pinned"}

    monkeypatch.setattr(pipeline, "DataIngester", _Short)
    monkeypatch.setattr(pipeline, "_preingest_start", lambda db, s: None)
    assert pipeline._preingest_one("AAA") == ("AAA", False)
