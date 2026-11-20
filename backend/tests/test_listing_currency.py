"""The currency stored prices are quoted in (data_backbone.listing_currency).

On 2026-09-28, 429 of the 780 symbols with bars carried a wrong label:
AAPL, MU, NESN.SW and SHEL.L read "EUR" (the ingester's fallback) and
EUNL.DE read "USD" (the assets table holds a fund's base currency). The
prices themselves were native. yfinance names the real unit in every history
response, including the cases a listing suffix cannot know (IWDA.L and
IHG.L are USD lines on the LSE).
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.data_backbone.listing_currency import (
    audit_listing_currencies,
    record_listing_currency,
    relabel_stored_prices,
    resolve_currency,
    resolve_quote_currency,
)
from app.foundation.fx_rates import convert
from app.foundation.models.entities import ListingCurrency, PriceCache
from app.foundation.providers.utils import currency_unit, listing_currency, listing_quote_currency


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bar_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                volume INTEGER NOT NULL, currency TEXT DEFAULT 'USD', provider TEXT NOT NULL,
                UNIQUE(symbol, ts)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_health_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, ts TIMESTAMP NOT NULL,
                capability TEXT NOT NULL, ok BOOLEAN NOT NULL, latency_ms REAL, message TEXT
            )
        """))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _bar(db, symbol, day, currency):
    db.execute(
        text(
            "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
            "VALUES (:s, :ts, 1, 1, 1, 1, 0, :c, 'unknown')"
        ),
        {"s": symbol, "ts": day, "c": currency},
    )


def _labels(db, symbol):
    return {r[0] for r in db.execute(text("SELECT currency FROM bar_prices WHERE symbol = :s"), {"s": symbol})}


@pytest.mark.parametrize(
    "symbol, quote, iso",
    [
        ("AAPL", "USD", "USD"),
        ("SAP.DE", "EUR", "EUR"),
        ("SHEL.L", "GBp", "GBP"),
        ("NESN.SW", "CHF", "CHF"),
        ("NOVO-B.CO", "DKK", "DKK"),
        ("7203.T", "JPY", "JPY"),
        ("USDEUR=X", "EUR", "EUR"),
        ("EURUSD=X", "USD", "USD"),
        ("^STOXX50E", "EUR", "EUR"),
        ("^VIX", "USD", "USD"),
    ],
)
def test_suffix_rule(symbol, quote, iso):
    assert listing_quote_currency(symbol) == quote
    assert listing_currency(symbol) == iso


def test_currency_unit_keeps_pence_apart_from_pounds():
    assert currency_unit("GBp") == ("GBP", 100.0)
    assert currency_unit("GBX") == ("GBP", 100.0)
    assert currency_unit("GBP") == ("GBP", 1.0)
    assert currency_unit("usd") == ("USD", 1.0)
    assert currency_unit(None) == ("", 1.0)


def test_fx_convert_reads_pence_as_hundredths():
    """SHEL.L at 3,611p is GBP 36.11, not GBP 3,611."""
    assert convert(3611.0, "GBp", "GBP", None) == pytest.approx(36.11)
    assert convert(36.11, "GBP", "GBp", None) == pytest.approx(3611.0)


def test_resolution_order_table_then_hint_then_suffix():
    db = _memory_db()
    assert resolve_quote_currency(db, "IWDA.L") == "GBp"          # suffix rule
    assert resolve_quote_currency(db, "IWDA.L", hint="USD") == "USD"  # a live quote's label
    record_listing_currency(db, "IWDA.L", "USD", "yfinance_metadata")
    db.commit()
    assert resolve_quote_currency(db, "iwda.l", hint="EUR") == "USD"  # provider answer wins
    assert resolve_currency(db, "SHEL.L") == "GBP"                   # ISO for FX lookups
    assert resolve_quote_currency(None, "AAPL") == "USD"


def test_ingest_records_the_provider_currency_and_relabels_older_bars():
    db = _memory_db()
    _bar(db, "IWDA.L", "2026-01-01 00:00:00", "EUR")
    db.commit()
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True,
        "provider": "yfinance",
        "data": [
            {"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "currency": "USD"},
        ],
    }

    ingester.ingest_bar_prices("IWDA.L")

    assert _labels(db, "IWDA.L") == {"USD"}
    row = db.get(ListingCurrency, "IWDA.L")
    assert row is not None and row.currency == "USD" and row.source == "yfinance_metadata"


def test_ingest_without_a_provider_currency_uses_the_resolver_not_eur():
    """The old fallback labelled every such bar "EUR" (or the assets table's
    fund currency)."""
    db = _memory_db()
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True,
        "provider": "twelvedata",
        "data": [{"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
    }

    ingester.ingest_bar_prices("AAPL")

    assert _labels(db, "AAPL") == {"USD"}
    assert db.get(ListingCurrency, "AAPL") is None


def test_audit_records_provider_answers_and_relabels_stored_prices():
    db = _memory_db()
    _bar(db, "AAPL", "2026-01-01 00:00:00", "EUR")
    _bar(db, "IHG.L", "2026-01-01 00:00:00", "EUR")
    _bar(db, "DELISTED.DE", "2026-01-01 00:00:00", "USD")
    db.add(PriceCache(ticker="SHEL.L", date=datetime(2026, 1, 1).date(), close=3611, currency="EUR"))
    db.commit()
    answers = {"AAPL": "USD", "IHG.L": "USD", "SHEL.L": "GBp"}

    counts = audit_listing_currencies(db, fetch=answers.get)

    assert _labels(db, "AAPL") == {"USD"}
    assert _labels(db, "IHG.L") == {"USD"}          # the LSE line trades in dollars
    assert _labels(db, "DELISTED.DE") == {"EUR"}    # no answer: suffix rule
    assert db.query(PriceCache).filter_by(ticker="SHEL.L").one().currency == "GBp"
    assert db.get(ListingCurrency, "DELISTED.DE") is None
    assert counts["provider"] == 3 and counts["suffix_rule"] == 1


def test_audit_skips_symbols_checked_recently():
    db = _memory_db()
    _bar(db, "AAPL", "2026-01-01 00:00:00", "USD")
    now = datetime(2026, 9, 28, tzinfo=UTC)
    db.add(ListingCurrency(symbol="AAPL", currency="USD", source="yfinance_metadata", checked_at=now - timedelta(days=3)))
    db.commit()

    def fail(symbol):
        raise AssertionError("must not probe a recently checked symbol")

    counts = audit_listing_currencies(db, fetch=fail, now=now)
    assert counts["skipped_recent"] == 1 and counts["checked"] == 0

    probed: list[str] = []
    audit_listing_currencies(db, fetch=lambda s: probed.append(s) or "USD", now=now + timedelta(days=40))
    assert probed == ["AAPL"]


def test_relabel_skips_a_missing_table_without_undoing_pending_work():
    """It used to roll back when bar_prices was absent, which also discarded
    everything the caller had not committed yet."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)  # no bar_prices: migrations create it
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    db.add(PriceCache(ticker="AAPL", date=datetime(2026, 1, 1).date(), close=1, currency="EUR"))
    db.flush()
    record_listing_currency(db, "AAPL", "USD", "yfinance_metadata")

    assert relabel_stored_prices(db, "AAPL", "USD") == 1
    db.commit()

    assert db.get(ListingCurrency, "AAPL") is not None
    assert db.query(PriceCache).filter_by(ticker="AAPL").one().currency == "USD"


def test_ingest_reports_failure_when_the_relabel_fails(monkeypatch):
    """The relabel runs between inserting the bars and committing them; an
    error there must fail the ingest, not silently drop the new bars."""
    db = _memory_db()
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True,
        "provider": "yfinance",
        "data": [
            {"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "currency": "USD"},
        ],
    }

    def boom(*_args, **_kwargs):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr("app.foundation.data_backbone.listing_currency.relabel_stored_prices", boom)
    result = ingester.ingest_bar_prices("AAPL")

    assert result["success"] is False
    assert _labels(db, "AAPL") == set()


def test_audit_continues_past_a_symbol_whose_write_fails(monkeypatch):
    db = _memory_db()
    _bar(db, "AAPL", "2026-01-01 00:00:00", "EUR")
    _bar(db, "MU", "2026-01-01 00:00:00", "EUR")
    db.commit()
    real = relabel_stored_prices

    def flaky(session, symbol, currency):
        if symbol == "AAPL":
            raise RuntimeError("lock timeout")
        return real(session, symbol, currency)

    monkeypatch.setattr("app.foundation.data_backbone.listing_currency.relabel_stored_prices", flaky)
    counts = audit_listing_currencies(db, fetch=lambda s: "USD")

    assert counts["failed"] == 1 and counts["provider"] == 1
    assert db.get(ListingCurrency, "AAPL") is None   # rolled back, retried next run
    assert _labels(db, "MU") == {"USD"}
