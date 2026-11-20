"""Tests for AlphaCrafter Data Ingestion wrapper with provider cascade."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.lab.alphacrafter.data_ingestion import (
    AlphaCrafterDataIngestion,
    DataAvailability,
)


def _memory_db():
    """Create in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bar_prices (
                    symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                    close REAL, volume REAL, currency TEXT, provider TEXT
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# --- DataAvailability tests ---


def test_data_availability_creation_defaults():
    """DataAvailability should have sensible defaults."""
    avail = DataAvailability()
    assert avail.price_symbols == 0
    assert avail.fundamental_symbols == 0
    assert avail.news_symbols == 0
    assert avail.failed_symbols == []
    assert avail.provider_used == {}


def test_data_availability_creation_with_values():
    """DataAvailability should accept custom values."""
    avail = DataAvailability(
        price_symbols=5,
        fundamental_symbols=3,
        news_symbols=2,
        failed_symbols=["AAPL", "GOOGL"],
        provider_used={"AAPL": "openbb", "GOOGL": "yfinance"},
    )
    assert avail.price_symbols == 5
    assert avail.fundamental_symbols == 3
    assert avail.news_symbols == 2
    assert avail.failed_symbols == ["AAPL", "GOOGL"]
    assert avail.provider_used == {"AAPL": "openbb", "GOOGL": "yfinance"}


# --- ensure_data tests ---


def test_ensure_data_uses_stored_bars_that_cover_the_window():
    db = _memory_db()
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    for i in range(31):
        db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('AAPL', :ts, 1.0)"), {"ts": start + timedelta(days=i)})
    db.commit()

    with patch("app.foundation.data_backbone.ingest.DataIngester") as ingester:
        result = asyncio.run(AlphaCrafterDataIngestion().ensure_data(db, ["AAPL"], start, end))

    ingester.assert_not_called()
    assert result.price_symbols == 1
    assert result.provider_used == {"AAPL": "stored"}


def test_ensure_data_ingests_a_missing_history_and_catches_up_a_stale_one():
    db = _memory_db()
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    for i in range(20):  # starts in time, ends 10 days early
        db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('MSFT', :ts, 1.0)"), {"ts": start + timedelta(days=i)})
    db.commit()

    calls = []

    class _Ingester:
        def __init__(self, _db):
            pass

        def ingest_bar_prices(self, symbol, start_date=None, end_date=None):
            calls.append((symbol, start_date))
            return {"success": symbol != "DEAD", "provider": "tiingo", "message": "no data"}

    with patch("app.foundation.data_backbone.ingest.DataIngester", _Ingester):
        result = asyncio.run(AlphaCrafterDataIngestion().ensure_data(db, ["AAPL", "MSFT", "DEAD"], start, end))

    assert calls[0] == ("AAPL", start.date().isoformat())  # nothing stored: whole window
    assert calls[1][0] == "MSFT" and calls[1][1] > start.date().isoformat()  # from its last bar
    assert result.price_symbols == 2
    assert result.provider_used == {"AAPL": "tiingo", "MSFT": "tiingo"}
    assert result.failed_symbols == ["DEAD"]


def test_ensure_data_counts_price_symbols():
    """ensure_data should count successful price symbol downloads."""
    db = _memory_db()
    ingestion = AlphaCrafterDataIngestion()
    universe = ["AAPL", "MSFT", "GOOGL"]
    start_date = datetime.now(UTC) - timedelta(days=30)
    end_date = datetime.now(UTC)

    # Mock _download_symbol to succeed for all symbols
    with patch.object(ingestion, "_download_symbol", new_callable=AsyncMock) as mock_download:
        mock_download.return_value = {"success": True, "symbol": "TEST", "provider": "openbb"}

        result = asyncio.run(ingestion.ensure_data(db, universe, start_date, end_date))

        assert result.price_symbols == 3
        assert result.failed_symbols == []


def test_ensure_data_records_failed_symbols():
    """ensure_data should record symbols that fail all providers."""
    db = _memory_db()
    ingestion = AlphaCrafterDataIngestion()
    universe = ["AAPL", "MSFT"]
    start_date = datetime.now(UTC) - timedelta(days=30)
    end_date = datetime.now(UTC)

    # Mock _download_symbol to fail for one symbol
    async def mock_download(db, symbol, start, end):
        if symbol == "AAPL":
            return {"success": False, "symbol": symbol, "error": "Provider failed"}
        return {"success": True, "symbol": symbol, "provider": "openbb"}

    with patch.object(ingestion, "_download_symbol", side_effect=mock_download):
        result = asyncio.run(ingestion.ensure_data(db, universe, start_date, end_date))

        assert result.price_symbols == 1  # Only MSFT succeeded
        assert "AAPL" in result.failed_symbols


def test_ensure_data_records_provider_used():
    """ensure_data should track which provider was used for each symbol."""
    db = _memory_db()
    ingestion = AlphaCrafterDataIngestion()
    universe = ["AAPL", "MSFT"]
    start_date = datetime.now(UTC) - timedelta(days=30)
    end_date = datetime.now(UTC)

    async def mock_download(db, symbol, start, end):
        provider = "openbb" if symbol == "AAPL" else "yfinance"
        return {"success": True, "symbol": symbol, "provider": provider}

    with patch.object(ingestion, "_download_symbol", side_effect=mock_download):
        result = asyncio.run(ingestion.ensure_data(db, universe, start_date, end_date))

        assert result.provider_used["AAPL"] == "openbb"
        assert result.provider_used["MSFT"] == "yfinance"


def test_never_fail_silently_always_returns_data_availability():
    """ensure_data should never raise, always return DataAvailability."""
    db = _memory_db()
    ingestion = AlphaCrafterDataIngestion()
    universe = ["AAPL", "MSFT", "GOOGL"]
    start_date = datetime.now(UTC) - timedelta(days=30)
    end_date = datetime.now(UTC)

    # Mock _download_symbol to fail for all symbols
    async def mock_download(db, symbol, start, end):
        return {"success": False, "symbol": symbol, "error": "All providers failed"}

    with patch.object(ingestion, "_download_symbol", side_effect=mock_download):
        # Should not raise, should return DataAvailability with all failures
        result = asyncio.run(ingestion.ensure_data(db, universe, start_date, end_date))

        assert isinstance(result, DataAvailability)
        assert result.price_symbols == 0
        assert result.failed_symbols == ["AAPL", "MSFT", "GOOGL"]
