"""Tests for price backfill service (T1: keystone)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbPosition, Holding, Portfolio, User
from app.foundation.price_backfill import backfill_user_prices, _backfill_one_symbol, _last_bar_date


def _memory_db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # Create raw bar_prices hypertable (DDL pattern from test_alphacrafter_data_ingestion)
    with session.bind.begin() as conn:
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
        # Business-key unique index (mirror migration 0075) so ON CONFLICT upserts work.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_bar_prices_symbol_ts "
                "ON bar_prices (symbol, ts)"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS provider_health_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    ts TIMESTAMP NOT NULL,
                    capability TEXT NOT NULL,
                    ok BOOLEAN NOT NULL,
                    latency_ms REAL,
                    message TEXT
                )
                """
            )
        )
    return session


def _seed_user(db: Session) -> str:
    user = User(id="test-user-backfill", username="testuser", password_hash="x", role="user", risk_profile="moderate")
    db.add(user)
    db.commit()
    return user.id


_portfolio_counter = 0

def _seed_holding(db: Session, user_id: str, ticker: str) -> None:
    global _portfolio_counter
    _portfolio_counter += 1
    pid = f"p{_portfolio_counter}"
    portfolio = Portfolio(id=pid, user_id=user_id, name="Test")
    db.add(portfolio)
    db.flush()
    db.add(Holding(portfolio_id=pid, ticker=ticker, name=ticker, quantity=10, avg_buy_price=100))
    db.commit()


_account_counter = 0

def _seed_dkb_position(db: Session, user_id: str, ticker: str | None, isin: str) -> None:
    global _account_counter
    _account_counter += 1
    aid = f"a{_account_counter}"
    account = DkbAccount(id=aid, user_id=user_id, type="giro", iban=f"DE{_account_counter}", balance=1000)
    db.add(account)
    db.flush()
    name = ticker or "Unnamed Position"
    db.add(DkbPosition(account_id=aid, isin=isin, ticker=ticker, name=name, quantity=5, current_value=500))
    db.commit()


def _seed_bar(db: Session, symbol: str, days_ago: int, close: float = 100.0) -> None:
    db.execute(
        text(
            "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
            "VALUES (:sym, :ts, :o, :h, :l, :c, :v, :cur, :prov)"
        ),
        {
            "sym": symbol,
            "ts": datetime.now(UTC) - timedelta(days=days_ago),
            "o": close, "h": close, "l": close, "c": close,
            "v": 1000, "cur": "EUR", "prov": "test",
        },
    )
    db.commit()


# --- _last_bar_date ---


def test_last_bar_date_no_data():
    db = _memory_db()
    assert _last_bar_date(db, "NONEXISTENT") is None


def test_last_bar_date_with_data():
    db = _memory_db()
    _seed_bar(db, "TEST.DE", days_ago=1)
    dt = _last_bar_date(db, "TEST.DE")
    assert dt is not None
    assert dt > datetime.now(UTC) - timedelta(days=2)


# --- _backfill_one_symbol ---


@patch("app.foundation.data_backbone.ingest.DataIngester.ingest_bar_prices")
def test_backfill_one_symbol_full(mock_ingest):
    """Symbol with no bars triggers full backfill (mode='full')."""
    mock_ingest.return_value = {"success": True, "symbol": "TEST.DE", "rows_inserted": 1200}
    db = _memory_db()
    ingester = MagicMock()
    ingester.ingest_bar_prices = mock_ingest

    result = _backfill_one_symbol(db, ingester, "TEST.DE", 1825)
    assert result["mode"] == "full"
    mock_ingest.assert_called_once_with(symbol="TEST.DE", days=1825)


@patch("app.foundation.data_backbone.ingest.DataIngester.ingest_bar_prices")
def test_backfill_one_symbol_incremental(mock_ingest):
    """Symbol with existing bars triggers incremental (mode='incremental')."""
    mock_ingest.return_value = {"success": True, "symbol": "TEST.DE", "rows_inserted": 5}
    db = _memory_db()
    _seed_bar(db, "TEST.DE", days_ago=3)

    ingester = MagicMock()
    ingester.ingest_bar_prices = mock_ingest

    result = _backfill_one_symbol(db, ingester, "TEST.DE", 1825)
    assert result["mode"] == "incremental"
    # Should call with start_date (not days)
    call_args = mock_ingest.call_args
    assert "start_date" in call_args[1] or "start_date" in call_args[0]


# --- backfill_user_prices ---


def test_resolution_chain_called():
    """Verify that backfill_user_prices calls resolve_isin_to_ticker."""
    db = _memory_db()
    user_id = _seed_user(db)
    _seed_holding(db, user_id, "IWDA.L")

    # Just verify no crash for a ticker with no data
    with patch("app.foundation.price_backfill.DataIngester") as MockIngester:
        inst = MockIngester.return_value
        inst.ingest_bar_prices.return_value = {"success": False, "message": "No data"}
        result = backfill_user_prices(db, user_id, days=1825)
        assert "total" in result
        assert "succeeded" in result
        assert "results" in result


@patch("app.foundation.price_backfill.DataIngester")
def test_backfill_mixed_results(MockIngester):
    """Some tickers succeed, some fail — results reflect correctly."""
    inst = MockIngester.return_value

    def _mock_ingest(**kwargs):
        sym = kwargs.get("symbol", "")
        if "OK" in sym:
            return {"success": True, "symbol": sym, "rows_inserted": 100}
        return {"success": False, "symbol": sym, "message": "No provider"}

    inst.ingest_bar_prices.side_effect = _mock_ingest

    db = _memory_db()
    user_id = _seed_user(db)
    _seed_holding(db, user_id, "OK.DE")
    _seed_holding(db, user_id, "FAIL.DE")

    result = backfill_user_prices(db, user_id, days=1825)
    # The two holdings plus the passive core (EUNL.DE, which fails here).
    assert result["total"] == 3
    assert result["succeeded"] == 1
    assert result["failed"] == 2


@patch("app.foundation.price_backfill.DataIngester")
@patch("app.foundation.price_backfill.resolve_isin_to_ticker", return_value={"resolved": 0, "unresolved": 0, "details": []})
def test_unresolved_ticker_not_raised(mock_resolve, MockIngester):
    """A position with no ticker (None/empty) doesn't crash — just skipped."""
    inst = MockIngester.return_value
    inst.ingest_bar_prices.return_value = {"success": False, "message": "No data"}

    db = _memory_db()
    user_id = _seed_user(db)
    _seed_dkb_position(db, user_id, ticker=None, isin="IE00B4L5Y983")

    result = backfill_user_prices(db, user_id, days=1825)
    assert result["total"] == 0  # No resolvable ticker
    assert result["succeeded"] == 0
    assert result["failed"] == 0


# --- Regression: SA 2.0 autobegin conflict ---


def test_last_bar_date_then_ingest_no_autobegin_conflict():
    """Calling _last_bar_date (which autobegins) then ingest_bar_prices must not raise.

    Regression for: DataIngester.ingest_bar_prices used ``with self.db.begin()``
    which conflicts with SA 2.0 autobegin when a transaction is already active.
    """
    from app.foundation.data_backbone.ingest import DataIngester

    db = _memory_db()
    _seed_bar(db, "TEST.DE", days_ago=3, close=100.0)

    # _last_bar_date triggers SA 2.0 autobegin
    last_dt = _last_bar_date(db, "TEST.DE")
    assert last_dt is not None

    # Build a real DataIngester with mocked registry
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True,
        "provider": "test",
        "data": [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 101, "volume": 1000},
            {"date": "2026-01-02", "open": 101, "high": 106, "low": 96, "close": 102, "volume": 1100},
        ],
    }

    # This must NOT raise an autobegin conflict
    result = ingester.ingest_bar_prices("TEST.DE")
    assert result["success"] is True
    assert result["rows_inserted"] == 2

    # Verify the new rows were actually committed
    rows = db.execute(
        text("SELECT COUNT(*) FROM bar_prices WHERE symbol = :sym"),
        {"sym": "TEST.DE"},
    ).scalar()
    assert rows == 3  # 1 seeded + 2 inserted


# --- ISIN pins: explicit mappings beat yfinance's arbitrary listing ---


def test_pinned_isin_wins_over_yfinance():
    """IE00B4L5Y983 must resolve to the EUR XETRA line, not yfinance's USD IWDA.L."""
    from app.foundation.portfolio.isin_resolver import _try_resolve_isin

    db = _memory_db()
    with patch("yfinance.Ticker") as yf_ticker:
        yf_ticker.return_value.info = {"symbol": "IWDA.L"}
        assert _try_resolve_isin("IE00B4L5Y983", db) == "EUNL.DE"
        yf_ticker.assert_not_called()


def test_user_override_beats_builtin_pin_and_yfinance():
    from app.foundation.portfolio.isin_resolver import _try_resolve_isin
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    upsert_public_settings(db, {"isin_ticker_overrides": {"IE00B4L5Y983": "iwda.as", "US0378331005": "AAPL"}})
    with patch("yfinance.Ticker") as yf_ticker:
        yf_ticker.return_value.info = {"symbol": "WRONG"}
        assert _try_resolve_isin("IE00B4L5Y983", db) == "IWDA.AS"
        assert _try_resolve_isin("US0378331005", db) == "AAPL"
        yf_ticker.assert_not_called()


def test_stored_ticker_is_repointed_to_pin():
    """A position resolved to IWDA.L before the pin existed is corrected in place."""
    from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker

    db = _memory_db()
    user_id = _seed_user(db)
    _seed_dkb_position(db, user_id, ticker="IWDA.L", isin="IE00B4L5Y983")
    _seed_dkb_position(db, user_id, ticker="SAP.DE", isin="DE0007164600")

    result = resolve_isin_to_ticker(db, user_id)

    assert result["remapped"] == 1
    tickers = {p.isin: p.ticker for p in db.query(DkbPosition).all()}
    assert tickers == {"IE00B4L5Y983": "EUNL.DE", "DE0007164600": "SAP.DE"}
    # Idempotent: a second pass finds nothing to change.
    assert resolve_isin_to_ticker(db, user_id)["remapped"] == 0


@patch("app.foundation.price_backfill.DataIngester")
def test_passive_core_is_refreshed_alongside_holdings(MockIngester):
    """The benchmark stays fresh even when no holding carries its listing."""
    inst = MockIngester.return_value
    inst.ingest_bar_prices.return_value = {"success": True, "rows_inserted": 1}

    db = _memory_db()
    user_id = _seed_user(db)
    _seed_holding(db, user_id, "SAP.DE")

    result = backfill_user_prices(db, user_id, days=1825)

    refreshed = {c.kwargs["symbol"] for c in inst.ingest_bar_prices.call_args_list}
    assert refreshed == {"SAP.DE", "EUNL.DE"}
    assert result["total"] == 2
