"""Tests for macro indicator ingestion into dedicated table."""

from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import MacroIndicator
from app.foundation.data_backbone.ingest import DataIngester


def _memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    # Create hypertables manually (SQLite doesn't support TimescaleDB)
    with engine.begin() as conn:
        # Create regime_snapshots table
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

        # Create bar_prices table (not used in this test but may be needed)
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

        # Create provider_health_history table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_health_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                capability TEXT NOT NULL,
                ok BOOLEAN NOT NULL,
                latency_ms REAL,
                message TEXT
            )
        """))

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def db():
    """Provide an in-memory test database."""
    return _memory_db()


def test_ingest_macro_indicators_creates_entries(db, monkeypatch):
    """Test that ingest_macro_indicators creates MacroIndicator entries."""
    # Create a stub registry that returns canned FRED data
    class StubRegistry:
        def first_success(self, capability):
            if capability == "get_macro_indicators":
                return {
                    "ok": True,
                    "data": {
                        "VIXCLS": {"value": 18.5, "timestamp": "2026-05-29T00:00:00"},
                        "DGS10": {"value": 4.2, "timestamp": "2026-05-29T00:00:00"},
                    },
                }
            return {"ok": False, "error": "Not implemented"}

    # Patch build_provider_registry to return stub
    def mock_build_registry(db_session):
        return StubRegistry()

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    # Create ingester and run ingest
    ingester = DataIngester(db)
    result = ingester.ingest_macro_indicators()

    # Assert success result
    assert result["fred"]["success"] is True
    assert result["fred"]["series_count"] == 2

    # Assert macro indicators were created
    count = db.query(MacroIndicator).count()
    assert count == 2

    # Assert specific values
    vix = db.query(MacroIndicator).filter(MacroIndicator.name == "VIXCLS").first()
    assert vix is not None
    assert float(vix.value) == 18.5
    assert vix.source == "fred"
    assert vix.date == date(2026, 5, 29)

    dgs = db.query(MacroIndicator).filter(MacroIndicator.name == "DGS10").first()
    assert dgs is not None
    assert float(dgs.value) == 4.2


def test_ingest_no_macro_indicators_in_regime_snapshots(db, monkeypatch):
    """Test that macro indicators do NOT go into regime_snapshots."""
    class StubRegistry:
        def first_success(self, capability):
            if capability == "get_macro_indicators":
                return {
                    "ok": True,
                    "data": {
                        "VIXCLS": {"value": 18.5, "timestamp": "2026-05-29T00:00:00"},
                    },
                }
            return {"ok": False, "error": "Not implemented"}

    def mock_build_registry(db_session):
        return StubRegistry()

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    ingester = DataIngester(db)
    ingester.ingest_macro_indicators()

    # Assert regime_snapshots has ZERO rows with macro_indicator labels
    count = db.execute(
        text("SELECT COUNT(*) FROM regime_snapshots WHERE label LIKE 'macro_indicator%'")
    ).scalar()
    assert count == 0


def test_ingest_macro_indicators_idempotency(db, monkeypatch):
    """Test that ingest_macro_indicators is idempotent."""
    class StubRegistry:
        def first_success(self, capability):
            if capability == "get_macro_indicators":
                return {
                    "ok": True,
                    "data": {
                        "VIXCLS": {"value": 18.5, "timestamp": "2026-05-29T00:00:00"},
                        "DGS10": {"value": 4.2, "timestamp": "2026-05-29T00:00:00"},
                    },
                }
            return {"ok": False, "error": "Not implemented"}

    def mock_build_registry(db_session):
        return StubRegistry()

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    ingester = DataIngester(db)

    # Run twice
    result1 = ingester.ingest_macro_indicators()
    result2 = ingester.ingest_macro_indicators()

    # Both should succeed
    assert result1["fred"]["success"] is True
    assert result2["fred"]["success"] is True

    # Count should stay at 2 (not 4)
    count = db.query(MacroIndicator).count()
    assert count == 2

    # Values should be updated from second run
    vix = db.query(MacroIndicator).filter(MacroIndicator.name == "VIXCLS").first()
    assert float(vix.value) == 18.5


def test_ingest_macro_indicators_with_series_ids_routes_to_fred_directly(db, monkeypatch):
    """Explicit series_ids (e.g. the regime classifier's T10Y2Y/BAA10Y) must go
    straight to FredProvider's get_history (full backfill), rather than
    through the generic first_success fallback chain or the latest-value-only
    get_macro_indicators — those are FRED-specific series codes that other
    providers (ECB, openbb) either don't understand or would silently
    mis-serve, and a single latest point isn't enough for the regime
    classifier's rolling window (see MACRO_BACKFILL_LOOKBACK_DAYS)."""
    from app.foundation.providers.fred_provider import FredProvider
    from app.foundation.providers.registry import ProviderRegistry

    fred = FredProvider(api_key=None, enabled=True)

    captured = {}

    def fake_get_history(self, symbol, start=None, end=None, days=None):
        captured.setdefault("symbols", []).append(symbol)
        captured["start"] = start
        captured["end"] = end
        return {
            "ok": True,
            "provider": "fred",
            "data": [
                {"date": "2026-08-09T00:00:00", "value": 0.40},
                {"date": "2026-08-10T00:00:00", "value": 0.42},
                {"date": "2026-08-11T00:00:00", "value": 0.45},
            ],
        }

    monkeypatch.setattr(FredProvider, "get_history", fake_get_history)

    def mock_build_registry(db_session):
        return ProviderRegistry([fred])

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    ingester = DataIngester(db)
    result = ingester.ingest_macro_indicators(series_ids=["T10Y2Y"])

    assert captured["symbols"] == ["T10Y2Y"]
    assert captured["start"] is not None and captured["end"] is not None
    assert result["fred"]["success"] is True
    assert result["fred"]["series_count"] == 1

    # Full history for the series lands as one row per date, not just the latest.
    rows = (
        db.query(MacroIndicator)
        .filter(MacroIndicator.name == "T10Y2Y")
        .order_by(MacroIndicator.date)
        .all()
    )
    assert len(rows) == 3
    assert [float(r.value) for r in rows] == [0.40, 0.42, 0.45]
    assert rows[-1].date == date(2026, 8, 11)


def test_ingest_macro_indicators_with_series_ids_idempotent(db, monkeypatch):
    """Re-running the series_ids (full history) branch must not duplicate rows
    — same (name, date, source) upsert as the generic path."""
    from app.foundation.providers.fred_provider import FredProvider
    from app.foundation.providers.registry import ProviderRegistry

    fred = FredProvider(api_key=None, enabled=True)

    def fake_get_history(self, symbol, start=None, end=None, days=None):
        return {
            "ok": True,
            "provider": "fred",
            "data": [
                {"date": "2026-08-10T00:00:00", "value": 0.42},
                {"date": "2026-08-11T00:00:00", "value": 0.45},
            ],
        }

    monkeypatch.setattr(FredProvider, "get_history", fake_get_history)

    def mock_build_registry(db_session):
        return ProviderRegistry([fred])

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    ingester = DataIngester(db)
    ingester.ingest_macro_indicators(series_ids=["T10Y2Y"])
    ingester.ingest_macro_indicators(series_ids=["T10Y2Y"])

    rows = db.query(MacroIndicator).filter(MacroIndicator.name == "T10Y2Y").all()
    assert len(rows) == 2


def test_ingest_macro_indicators_with_series_ids_no_fred_configured(db, monkeypatch):
    """No FRED provider registered/enabled: fail cleanly instead of routing
    FRED-specific series codes to an unrelated provider."""
    from app.foundation.providers.registry import ProviderRegistry

    def mock_build_registry(db_session):
        return ProviderRegistry([])

    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        mock_build_registry,
    )

    ingester = DataIngester(db)
    result = ingester.ingest_macro_indicators(series_ids=["T10Y2Y"])

    assert result["fred"]["success"] is False
    assert db.query(MacroIndicator).count() == 0


def test_ingest_macro_indicators_retries_a_failed_series(db, monkeypatch):
    """FRED answered VIXCLS with a 500 at 07:00 UTC for a week (2026-09-22..28)
    while later requests worked; one failure must not cost the day's refresh."""
    from app.foundation.providers.fred_provider import FredProvider
    from app.foundation.providers.registry import ProviderRegistry

    fred = FredProvider(api_key=None, enabled=True)
    calls: list[str] = []

    def flaky_get_history(self, symbol, start=None, end=None, days=None):
        calls.append(symbol)
        if len(calls) == 1:
            return {"ok": False, "provider": "fred", "error": "Internal Server Error"}
        return {"ok": True, "provider": "fred", "data": [{"date": "2026-09-22T00:00:00", "value": 14.21}]}

    monkeypatch.setattr(FredProvider, "get_history", flaky_get_history)
    monkeypatch.setattr("app.foundation.data_backbone.ingest.MACRO_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        lambda db_session: ProviderRegistry([fred]),
    )

    DataIngester(db).ingest_macro_indicators(series_ids=["VIXCLS"])

    assert calls == ["VIXCLS", "VIXCLS"]
    row = db.query(MacroIndicator).filter(MacroIndicator.name == "VIXCLS").one()
    assert float(row.value) == 14.21


def test_vix_days_fred_lacks_are_filled_from_the_index(db, monkeypatch):
    """FRED failed every 07:00 VIXCLS request for two weeks and its series
    lagged the CBOE close by days. The days after FRED's newest value come
    from the ^VIX bars (identical closes on every shared day)."""
    from datetime import date as _date, timedelta

    from app.foundation.providers.base import MarketDataProvider
    from app.foundation.providers.fred_provider import FredProvider
    from app.foundation.providers.registry import ProviderRegistry

    today = _date.today()
    fred_last = today - timedelta(days=6)

    def fred_history(self, symbol, start=None, end=None, days=None):
        if symbol != "VIXCLS":
            raise AssertionError("FRED must not be asked for the index")
        return {"ok": True, "provider": "fred", "data": [{"date": f"{fred_last}T00:00:00", "value": 14.21}]}

    class IndexProvider(MarketDataProvider):
        name = "yfinance"
        capabilities = {"get_history"}

        def get_history(self, symbol, start=None, end=None, days=None):
            assert symbol == "^VIX"
            rows = [
                {"date": fred_last, "close": 14.21},
                {"date": today - timedelta(days=2), "close": 15.5},
                {"date": today - timedelta(days=1), "close": 16.25},
            ]
            return {"ok": True, "provider": self.name, "data": rows}

    monkeypatch.setattr(FredProvider, "get_history", fred_history)
    monkeypatch.setattr(
        "app.foundation.data_backbone.ingest.build_provider_registry",
        lambda db_session: ProviderRegistry([FredProvider(api_key=None, enabled=True), IndexProvider()]),
    )

    result = DataIngester(db).ingest_macro_indicators(series_ids=["VIXCLS"])

    rows = db.query(MacroIndicator).filter(MacroIndicator.name == "VIXCLS").order_by(MacroIndicator.date).all()
    assert [(r.date, float(r.value), r.source) for r in rows] == [
        (fred_last, 14.21, "fred"),
        (today - timedelta(days=2), 15.5, "cboe_index"),
        (today - timedelta(days=1), 16.25, "cboe_index"),
    ]
    assert result["vix_index"]["added"] == 2
