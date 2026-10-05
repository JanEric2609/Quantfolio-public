from unittest.mock import MagicMock, patch

from conftest import _memory_db

import app.foundation.news as news_service
from app.foundation.models.entities import Asset, Holding, NewsItem, Portfolio, User, WatchlistItem
from app.foundation.news import refresh_news_for_user
from app.foundation.news import store_news_item as _store_news_item


def test_store_news_item_dedupes_by_url():
    db = _memory_db()
    payload = {
        "title": "Portfolio company update",
        "url": "https://example.test/news/1",
        "source": "example",
        "summary": "A useful summary",
        "published_at": "20260518T120000",
    }

    assert _store_news_item(db, payload, "test", "IWDA.AS") is True
    db.commit()
    assert _store_news_item(db, payload, "test", "IWDA.AS") is False

    assert db.query(NewsItem).count() == 1



def test_store_news_item_dedupes_within_one_uncommitted_refresh():
    """EUNL.DE and IWDA.L (the same fund) match the same RSS article in one
    refresh. The session does not autoflush, so the second lookup used to
    miss the pending insert and the URL was stored twice; every later
    refresh then raised MultipleResultsFound (prod, 2026-09)."""
    db = _memory_db()
    payload = {"title": "China exports surplus", "url": "https://example.test/news/2", "source": "cnbc"}

    assert _store_news_item(db, payload, "rss", "EUNL.DE") is True
    assert _store_news_item(db, payload, "rss", "IWDA.L") is False
    db.commit()

    assert db.query(NewsItem).count() == 1


def test_store_news_item_skips_urls_too_long_to_index():
    db = _memory_db()
    payload = {"title": "Tracking link", "url": "https://example.test/?" + "x" * news_service.MAX_NEWS_URL_CHARS}

    assert _store_news_item(db, payload, "rss", "EUNL.DE") is False

def test_full_refresh_stores_items_without_inbox_writes():
    """A full news refresh stores items and writes no inbox rows (audit §4 spam fix).

    The refresh data flow itself is unchanged: one stock holding, one provider
    item stored. The per-run inbox summary insert was deleted with the review
    inbox (audit-fixes todo 8) — the writer no longer exists.
    """
    db = _memory_db()
    user = User(id="u-news-1", username="bob", password_hash="x")
    portfolio = Portfolio(id="p-news-1", user_id="u-news-1", name="Main")
    asset = Asset(
        id="a-news-1",
        symbol="AAPL",
        name="Apple Inc",
        asset_type="stock",
        currency="USD",
    )
    holding = Holding(
        id="h-news-1",
        portfolio_id=portfolio.id,
        ticker="AAPL",
        name="Apple Inc",
        quantity=5,
        avg_buy_price=150,
        currency="USD",
    )
    db.add_all([user, portfolio, asset, holding])
    db.commit()

    mock_registry = MagicMock()
    mock_registry.get_news.return_value = {
        "ok": True,
        "data": [
            {
                "title": "Apple announces new product",
                "url": "https://example.test/news/spam-guard",
                "source": "TestNews",
                "published_at": "2026-06-01T10:00:00Z",
            }
        ],
        "provider": "test",
    }

    with patch("app.foundation.news.fetch_all_feeds"):
        with patch("app.foundation.news.build_provider_registry", return_value=mock_registry):
            result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)

    # Refresh data flow still works: the item was stored.
    assert result["created"] == 1
    assert result["failed"] == []


def test_refresh_news_for_user_stores_watchlist_items(monkeypatch):
    """A full refresh run stores watchlist news (inbox writer removed, audit §4)."""
    db = _memory_db()
    user = User(username="news-refresh-user", password_hash="x")
    db.add(user)
    db.commit()
    db.add(WatchlistItem(user_id=user.id, ticker="ACME", name="Acme Corp"))
    db.commit()

    class _StubRegistry:
        def get_news(self, symbol, limit=8):
            return {
                "ok": True,
                "provider": "stub",
                "data": [
                    {
                        "title": "Acme beats earnings",
                        "url": "https://example.test/acme/1",
                        "source": "example",
                        "summary": "Record quarter",
                    }
                ],
            }

    monkeypatch.setattr(
        news_service, "build_provider_registry", lambda _db: _StubRegistry()
    )

    result = news_service.refresh_news_for_user(db, user.id)

    assert result["created"] == 1
    assert db.query(NewsItem).count() == 1
