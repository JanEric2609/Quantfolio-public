from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.foundation.models.entities import Asset, Holding, Portfolio, User
from app.foundation.news import refresh_news_for_user


def _seed_user_and_portfolio(db):
    user = User(id="u-1", username="alice", password_hash="x")
    portfolio = Portfolio(id="p-1", user_id="u-1", name="Main")
    db.add_all([user, portfolio])
    db.commit()
    return user, portfolio


def _seed_etf_holding(db, portfolio, ticker="IWDA.AS", name="iShares Core MSCI World UCITS ETF"):
    asset = Asset(
        id="a-1",
        symbol=ticker,
        name=name,
        asset_type="etf",
        currency="EUR",
    )
    holding = Holding(
        id="h-1",
        portfolio_id=portfolio.id,
        ticker=ticker,
        name=name,
        quantity=10,
        avg_buy_price=100,
        currency="EUR",
    )
    db.add_all([asset, holding])
    db.commit()


def _seed_stock_holding(db, portfolio, ticker="AAPL", name="Apple Inc"):
    asset = Asset(
        id="a-2",
        symbol=ticker,
        name=name,
        asset_type="stock",
        currency="USD",
    )
    holding = Holding(
        id="h-2",
        portfolio_id=portfolio.id,
        ticker=ticker,
        name=name,
        quantity=5,
        avg_buy_price=150,
        currency="USD",
    )
    db.add_all([asset, holding])
    db.commit()


def test_etf_symbol_keyword_matched_rss_news_stored():
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    _seed_etf_holding(db, portfolio, ticker="IWDA.AS", name="iShares Core MSCI World UCITS ETF")

    rss_items = [
        {
            "title": "iShares Core MSCI World hits record inflows",
            "link": "https://example.test/rss/1",
            "summary": "The fund saw massive growth this quarter.",
            "published": "2026-06-01T10:00:00Z",
            "source": "TestFeed",
        }
    ]

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {"ok": True, "data": [], "provider": "test"}

    with patch("app.foundation.news.fetch_all_feeds", return_value={"items": rss_items, "sources": {}, "errors": []}) as mock_fetch:
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    assert result["created"] == 1
    assert result["failed"] == []
    mock_fetch.assert_called_once_with(db)
    mock_registry.get_news.assert_not_called()


def test_etf_symbol_no_keyword_match_silently_skipped():
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    _seed_etf_holding(db, portfolio, ticker="IWDA.AS", name="iShares Core MSCI World UCITS ETF")

    rss_items = [
        {
            "title": "Fed raises interest rates again",
            "link": "https://example.test/rss/2",
            "summary": "Macro news unrelated to the ETF.",
            "published": "2026-06-01T10:00:00Z",
            "source": "TestFeed",
        }
    ]

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {"ok": True, "data": [], "provider": "test"}

    with patch("app.foundation.news.fetch_all_feeds", return_value={"items": rss_items, "sources": {}, "errors": []}) as mock_fetch:
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    assert result["created"] == 0
    assert result["failed"] == []
    mock_fetch.assert_called_once_with(db)
    mock_registry.get_news.assert_not_called()


def test_non_etf_symbol_company_news_flow_unchanged():
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    _seed_stock_holding(db, portfolio, ticker="AAPL", name="Apple Inc")

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {
        "ok": True,
        "data": [
            {
                "title": "Apple announces new product",
                "url": "https://example.test/news/3",
                "source": "TestNews",
                "published_at": "2026-06-01T10:00:00Z",
            }
        ],
        "provider": "test",
    }

    with patch("app.foundation.news.fetch_all_feeds") as mock_fetch:
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    assert result["created"] == 1
    assert result["failed"] == []
    mock_fetch.assert_not_called()
    mock_registry.get_news.assert_called_once_with("AAPL", limit=8)


def test_no_asset_row_falls_through_to_company_news():
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    # Holding with no matching Asset row
    holding = Holding(
        id="h-3",
        portfolio_id=portfolio.id,
        ticker="UNKNOWN",
        name="Unknown Corp",
        quantity=1,
        avg_buy_price=1,
        currency="EUR",
    )
    db.add(holding)
    db.commit()

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {
        "ok": True,
        "data": [
            {
                "title": "Unknown Corp update",
                "url": "https://example.test/news/4",
                "source": "TestNews",
                "published_at": "2026-06-01T10:00:00Z",
            }
        ],
        "provider": "test",
    }

    with patch("app.foundation.news.fetch_all_feeds") as mock_fetch:
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    assert result["created"] == 1
    assert result["failed"] == []
    mock_fetch.assert_not_called()
    mock_registry.get_news.assert_called_once_with("UNKNOWN", limit=8)


def test_multiple_etf_items_matched_all_stored():
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    _seed_etf_holding(db, portfolio, ticker="VWCE.DE", name="Vanguard FTSE All-World UCITS ETF")

    rss_items = [
        {
            "title": "Vanguard FTSE All-World sees strong flows",
            "link": "https://example.test/rss/5a",
            "summary": "First article about the fund.",
            "published": "2026-06-01T10:00:00Z",
            "source": "TestFeed",
        },
        {
            "title": "Global markets rally",
            "link": "https://example.test/rss/5b",
            "summary": "Vanguard FTSE All-World benefits from global uptrend.",
            "published": "2026-06-01T11:00:00Z",
            "source": "TestFeed",
        },
        {
            "title": "Unrelated macro news",
            "link": "https://example.test/rss/5c",
            "summary": "Nothing about the ETF here.",
            "published": "2026-06-01T12:00:00Z",
            "source": "TestFeed",
        },
    ]

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {"ok": True, "data": [], "provider": "test"}

    with patch("app.foundation.news.fetch_all_feeds", return_value={"items": rss_items, "sources": {}, "errors": []}) as mock_fetch:
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    assert result["created"] == 2
    assert result["failed"] == []
    mock_fetch.assert_called_once_with(db)
    mock_registry.get_news.assert_not_called()
