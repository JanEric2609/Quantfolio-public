"""Tests for regime job registration and refit_regime_model end-to-end."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


# ---------------------------------------------------------------------------
# Minimal fake scheduler (avoids starting real APScheduler)
# ---------------------------------------------------------------------------

class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, fn, **kw):
        self.calls.append((fn, kw))

        class _J:
            id = kw.get("id")

        return _J()


# ---------------------------------------------------------------------------
# In-memory DB helper (same pattern as test_regime_crisis_classifier.py)
# ---------------------------------------------------------------------------

def _memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL,
                label TEXT NOT NULL,
                score REAL NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bar_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                currency TEXT DEFAULT 'USD',
                provider TEXT NOT NULL
            )
        """))

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# Test 1: register_regime_daily_job returns correct id + cron params
# ---------------------------------------------------------------------------

def test_register_regime_daily_job_cron_params():
    """register_regime_daily_job registers with the right trigger/hour/minute/id."""
    from app.lab.regime.jobs import register_regime_daily_job

    sched = FakeScheduler()
    job_id = register_regime_daily_job(sched)

    assert job_id == "regime_daily"
    assert len(sched.calls) == 1
    _fn, kw = sched.calls[0]
    assert kw["trigger"] == "cron"
    assert kw["hour"] == 7
    assert kw["minute"] == 15
    assert kw["id"] == "regime_daily"


# ---------------------------------------------------------------------------
# Test 2: register_regime_refit_job returns correct id + cron params
# ---------------------------------------------------------------------------

def test_register_regime_refit_job_cron_params():
    """register_regime_refit_job registers with the right trigger/day/hour/minute/id."""
    from app.lab.regime.jobs import register_regime_refit_job

    sched = FakeScheduler()
    job_id = register_regime_refit_job(sched)

    assert job_id == "regime_refit"
    assert len(sched.calls) == 1
    _fn, kw = sched.calls[0]
    assert kw["trigger"] == "cron"
    assert kw["day_of_week"] == "sun"
    assert kw["hour"] == 6
    assert kw["minute"] == 0
    assert kw["id"] == "regime_refit"


# ---------------------------------------------------------------------------
# Test 3: Daily inner fn calls classify_and_store
# ---------------------------------------------------------------------------

def test_daily_inner_fn_calls_classify_and_store(monkeypatch):
    """The closure registered by register_regime_daily_job calls classify_and_store."""
    from app.lab.regime.jobs import register_regime_daily_job

    # Capture call
    calls = []

    def mock_classify(db):
        calls.append(db)
        return {"written": True}

    # Monkeypatch at the import site the closure uses
    monkeypatch.setattr(
        "app.lab.regime.classifier.classify_and_store",
        mock_classify,
    )

    # Monkeypatch SessionLocal so no real DB is needed
    mock_session = MagicMock()
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: mock_session)

    sched = FakeScheduler()
    register_regime_daily_job(sched)

    assert len(sched.calls) == 1
    fn, _kw = sched.calls[0]

    # Invoke the registered closure
    fn()

    # classify_and_store was called once with the mock session
    assert len(calls) == 1
    assert calls[0] is mock_session


def _capture_inner_job_fn(monkeypatch):
    """register_regime_daily_job hands its closure to register_cron_job,
    which wraps it in _track_job before the scheduler ever sees it — so
    FakeScheduler.add_job only ever captures that opaque wrapper, not the
    closure itself. Intercept register_cron_job instead to get the real
    regime_daily_inner function, so tests can exercise its return value
    and exception behavior directly."""
    captured = {}

    def fake_register_cron_job(job_id, job_fn, **kw):
        captured["fn"] = job_fn
        return job_id

    monkeypatch.setattr(
        "app.lab.regime.jobs.register_cron_job", fake_register_cron_job
    )
    return captured


def test_daily_inner_fn_returns_warning_when_not_written(monkeypatch):
    """When classify_and_store refuses (written=False), the closure returns
    the {"status": "warning", "reason": ...} sentinel instead of silently
    returning None — so _track_job records status="warning", not "success"."""
    from app.lab.regime.jobs import register_regime_daily_job

    def mock_classify(db):
        return {"written": False, "reason": "insufficient_clean_features"}

    monkeypatch.setattr(
        "app.lab.regime.classifier.classify_and_store",
        mock_classify,
    )
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: MagicMock())

    captured = _capture_inner_job_fn(monkeypatch)
    register_regime_daily_job(None)
    fn = captured["fn"]

    outcome = fn()

    assert outcome == {"status": "warning", "reason": "insufficient_clean_features"}


def test_daily_inner_fn_returns_none_when_written(monkeypatch):
    """Successful classification returns None (unchanged) so _track_job keeps
    recording status="success"."""
    from app.lab.regime.jobs import register_regime_daily_job

    def mock_classify(db):
        return {"written": True, "label": "bull"}

    monkeypatch.setattr(
        "app.lab.regime.classifier.classify_and_store",
        mock_classify,
    )
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: MagicMock())

    sched = FakeScheduler()
    register_regime_daily_job(sched)
    fn, _kw = sched.calls[0]

    assert fn() is None


def test_daily_inner_fn_reraises_unexpected_exception(monkeypatch):
    """A genuinely unexpected crash must propagate (so _track_job records
    status="error"), not be swallowed into a silent success."""
    from app.lab.regime.jobs import register_regime_daily_job

    def mock_classify(db):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.lab.regime.classifier.classify_and_store",
        mock_classify,
    )
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: MagicMock())

    captured = _capture_inner_job_fn(monkeypatch)
    register_regime_daily_job(None)
    fn = captured["fn"]

    try:
        fn()
        raised = False
    except RuntimeError:
        raised = True

    assert raised, "regime_daily_inner must re-raise unexpected exceptions, not swallow them"


# ---------------------------------------------------------------------------
# Test 4: refit_regime_model end-to-end with in-memory SQLite
# ---------------------------------------------------------------------------

def _make_synthetic_bars(symbol: str, days: int = 450) -> pd.DataFrame:
    """Create synthetic ascending OHLCV bars ending near today."""
    np.random.seed(42)
    # End near today so the bars fall within the default 750-day lookback
    end_date = datetime.now(timezone.utc).date()
    start_date = pd.Timestamp(end_date) - pd.Timedelta(days=days - 1)
    dates = pd.date_range(start=start_date, periods=days, freq="D")
    returns = np.random.randn(days) * 0.01
    close = 100 * np.exp(np.cumsum(returns))
    return pd.DataFrame({
        "symbol": symbol,
        "ts": dates,
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": 1_000_000,
        "currency": "USD",
        "provider": "test",
    })


def test_refit_regime_model_end_to_end(tmp_path, monkeypatch):
    """refit_regime_model fits and persists both models; returns saved=True."""
    from app.lab.regime.classifier import refit_regime_model

    monkeypatch.setattr(
        "app.lab.regime.classifier._ensure_index_history",
        lambda db, symbol, bars_df, *, start, end: bars_df,
    )
    db = _memory_db()

    # Seed bar_prices with ~450 days of synthetic data for the default regime index.
    bars_df = _make_synthetic_bars("^STOXX50E", days=450)
    for _, row in bars_df.iterrows():
        row_dict = dict(row)
        row_dict["ts"] = row_dict["ts"].to_pydatetime()
        db.execute(text("""
            INSERT INTO bar_prices
            (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
        """), row_dict)
    db.commit()

    result = refit_regime_model(db, base=str(tmp_path))

    assert result["saved"] is True, f"Expected saved=True, got: {result}"
    assert result["n_rows"] >= 120
    assert "model_id" in result
    assert result["jump"]["saved"] is True, result["jump"]
    assert (tmp_path / "regime_jump").exists()
    assert (tmp_path / "regime_hmm").exists()

    # Verify the model file was written somewhere under tmp_path
    all_files = list(tmp_path.rglob("*"))
    assert any(f.is_file() for f in all_files), (
        f"No model file found under {tmp_path}. Files: {all_files}"
    )


# ---------------------------------------------------------------------------
# Test 5: refit_regime_model defensive — empty bar_prices → saved=False
# ---------------------------------------------------------------------------

def test_refit_regime_model_defensive_empty_bars():
    """refit_regime_model returns saved=False and does not raise when bars missing."""
    from app.lab.regime.classifier import refit_regime_model

    db = _memory_db()

    # Don't seed any bar data
    result = refit_regime_model(db, base="/tmp/quantfolio-test-unreachable")

    assert result["saved"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# Tests 6-8: price_backfill_daily keeps the regime index symbol fed (T3.E)
#
# Prod evidence (audit D2): regime snapshots froze because NOTHING scheduled
# ingested regime_index_symbol — the classifier's Wave-1 freshness guard was
# starved. These tests pin the daily job's contract: exactly one ingestion of
# the CONFIGURED symbol per run, zero ingestion when bars are already fresh,
# and log-and-continue when ingestion blows up.
# ---------------------------------------------------------------------------

def _make_ingester_stub(monkeypatch, calls: list, raise_exc: Exception | None = None) -> None:
    """Replace DataIngester at the network/ingestion boundary with a recorder."""

    class _StubIngester:
        def __init__(self, db):
            self.db = db

        def ingest_bar_prices(self, *, symbol, start_date=None, end_date=None, days=None):
            calls.append({"symbol": symbol, "start_date": start_date, "end_date": end_date})
            if raise_exc is not None:
                raise raise_exc
            return {"success": True, "symbol": symbol, "rows_inserted": 1}

    monkeypatch.setattr("app.foundation.data_backbone.ingest.DataIngester", _StubIngester)


def _register_price_backfill_job(monkeypatch, db):
    """Register price_backfill_daily against an in-memory DB.

    The user-holdings refresh is stubbed (not under test here); it must be
    patched BEFORE registration because the job closure binds it at
    registration time.
    """
    from app.worker import register_price_backfill_daily_job

    monkeypatch.setattr(
        "app.foundation.price_backfill.refresh_all_user_prices",
        lambda: {"succeeded": 0, "total": 0, "failed": 0, "user_results": {}},
    )
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)

    sched = FakeScheduler()
    register_price_backfill_daily_job(sched)
    assert len(sched.calls) == 1
    fn, kw = sched.calls[0]
    assert kw["id"] == "price_backfill_daily"
    return fn


def test_price_backfill_daily_ingests_configured_regime_index_symbol(monkeypatch):
    """Daily job backfills the CONFIGURED regime_index_symbol exactly once."""
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    # Non-default symbol proves the setting is honored, not hardcoded SPY.
    upsert_public_settings(db, {"regime_index_symbol": "ACME"})

    calls: list = []
    _make_ingester_stub(monkeypatch, calls)
    run_job = _register_price_backfill_job(monkeypatch, db)

    run_job()  # must not raise

    assert len(calls) == 1
    assert calls[0]["symbol"] == "ACME"
    # Window covers the classifier lookback (~400 days) up to today.
    assert calls[0]["start_date"] is not None
    assert calls[0]["end_date"] is not None


def test_price_backfill_daily_skips_ingest_when_regime_bars_fresh(monkeypatch):
    """No redundant ingestion when the newest bar is already within max age."""
    db = _memory_db()
    db.execute(
        text("""
            INSERT INTO bar_prices
            (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES ('^STOXX50E', :ts, 1.0, 1.0, 1.0, 1.0, 0, 'USD', 'test')
        """),
        {"ts": datetime.now(timezone.utc)},
    )
    db.commit()

    calls: list = []
    _make_ingester_stub(monkeypatch, calls)
    run_job = _register_price_backfill_job(monkeypatch, db)

    run_job()

    assert calls == []  # step already ran / data fresh → zero ingestions


def test_price_backfill_daily_tolerates_regime_ingest_failure(monkeypatch, caplog):
    """Ingestion blow-up is logged; the daily job still completes."""
    import logging

    db = _memory_db()
    calls: list = []
    _make_ingester_stub(monkeypatch, calls, raise_exc=RuntimeError("provider down"))
    run_job = _register_price_backfill_job(monkeypatch, db)

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        run_job()  # must NOT raise

    assert len(calls) == 1  # attempted exactly once, then gave up cleanly
    assert any("regime_index_backfill failed" in r.message for r in caplog.records)


def test_refit_backfills_an_index_history_that_starts_late(monkeypatch):
    """Prod held ^STOXX50E only from 2025-08 (282 bars), so the 750-day refit
    fitted three regimes on one calm year; a late start triggers one backfill."""
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from app.lab.regime import classifier

    calls = []

    class _Ingester:
        def __init__(self, db):
            pass

        def ingest_bar_prices(self, symbol, start_date, end_date):
            calls.append((symbol, start_date))
            return {"success": True}

    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    start = now - timedelta(days=750)
    short = pd.DataFrame({"ts": pd.date_range("2025-08-11", periods=10, freq="D", tz="UTC"), "close": 1.0})
    full = pd.DataFrame({"ts": pd.date_range(start, periods=500, freq="D"), "close": 1.0})

    class _Store:
        def __init__(self, db):
            pass

        def get_bars(self, symbol, start, end):
            return full

    monkeypatch.setattr(classifier, "DataIngester", _Ingester)
    monkeypatch.setattr(classifier, "BarStore", _Store)

    out = classifier._ensure_index_history(None, "^STOXX50E", short, start=start, end=now)
    assert calls == [("^STOXX50E", start.date().isoformat())]
    assert len(out) == 500

    calls.clear()
    assert classifier._ensure_index_history(None, "^STOXX50E", full, start=start, end=now) is full
    assert calls == []

