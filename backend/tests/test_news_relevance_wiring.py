"""Todo 9 (audit-fixes-2026-08): wire tag_relevance into the refresh loop.

Covers the audit §5 lane: stored news items must get honest relevance scores
(cheap keyword matching — no embeddings/LLM) when keyword/entity matches
exist, items without matches keep NULL scores, legacy rows are never
backfilled, and a throwing scorer degrades per-item instead of failing the
refresh.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import _memory_db

import app.foundation.news as news_service
from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import (
    Asset,
    Holding,
    NewsItem,
    Portfolio,
    User,
    UserNewsRelevance,
)
from app.foundation.auth import current_user
from app.foundation.news import refresh_news_for_user


def _seed_user_with_holdings(db, holdings):
    """Seed a user + portfolio with (ticker, asset_type, name) holdings."""
    user = User(id="u-rel-1", username="reluser", password_hash="x")
    portfolio = Portfolio(id="p-rel-1", user_id=user.id, name="Main")
    db.add_all([user, portfolio])
    for i, (ticker, asset_type, name) in enumerate(holdings):
        db.add(
            Asset(
                id=f"a-rel-{i}",
                symbol=ticker,
                name=name,
                asset_type=asset_type,
                currency="EUR",
            )
        )
        db.add(
            Holding(
                id=f"h-rel-{i}",
                portfolio_id=portfolio.id,
                ticker=ticker,
                name=name,
                quantity=10,
                avg_buy_price=100,
                currency="EUR",
            )
        )
    db.commit()
    return user


# Headline that matches the IWDA.AS ETF keywords AND macro domains.
_IWDA_HEADLINE = "iShares Core MSCI World hits record inflows amid ECB rate cut hopes"
# Celebrity gossip: contains none of the portfolio/ETF/macro keywords.
_GOSSIP_HEADLINE = "Reality star weds pizza chef in lavish ceremony"
_GOSSIP_SUMMARY = "The couple celebrated with a three-tier cake on the beach."


def _refresh_with_mocks(db, user, *, rss_items, provider_items):
    registry = MagicMock()
    registry.get_news.return_value = {
        "ok": True,
        "data": provider_items,
        "provider": "test",
    }
    with patch(
        "app.foundation.news.fetch_all_feeds",
        return_value={"items": rss_items, "sources": {}, "errors": []},
    ):
        with patch("app.foundation.news.build_provider_registry", return_value=registry):
            return refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)


def _api_client(db, user):
    """TestClient wired to *db*'s engine with current_user overridden."""
    maker = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    return TestClient(app)


def test_refresh_scores_matching_item_and_leaves_unmatched_null():
    """Refresh populates relevance for keyword matches; non-matches stay NULL."""
    db = _memory_db()
    user = _seed_user_with_holdings(db, [("AAPL", "stock", "Apple Inc")])

    result = _refresh_with_mocks(
        db,
        user,
        rss_items=[],
        provider_items=[
            {
                # Contains the literal ticker keyword -> scored.
                "title": "AAPL surges as Apple beats earnings expectations",
                "url": "https://example.test/news/aapl-match",
                "source": "TestNews",
                "summary": "Strong quarter driven by services.",
                "published_at": "2026-06-01T10:00:00Z",
            },
            {
                # Matches nothing -> must be stored but stay unscored.
                "title": _GOSSIP_HEADLINE,
                "url": "https://example.test/news/gossip",
                "source": "TestNews",
                "summary": _GOSSIP_SUMMARY,
                "published_at": "2026-06-01T11:00:00Z",
            },
        ],
    )

    assert result["created"] == 2

    scored = db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/aapl-match").one()
    assert scored.relevance_score is not None
    assert scored.relevance_label is not None
    assert scored.relevance_reason is not None
    rel_row = (
        db.query(UserNewsRelevance)
        .filter(
            UserNewsRelevance.user_id == user.id,
            UserNewsRelevance.news_item_id == scored.id,
        )
        .one_or_none()
    )
    assert rel_row is not None
    assert rel_row.relevance_score is not None and rel_row.relevance_score > 0

    unscored = db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/gossip").one()
    assert unscored.relevance_score is None
    assert unscored.relevance_label is None
    assert unscored.relevance_reason is None
    assert (
        db.query(UserNewsRelevance)
        .filter(UserNewsRelevance.news_item_id == unscored.id)
        .count()
        == 0
    )


def test_scoring_quality_iwda_headline_beats_celebrity_gossip():
    """Regression fixture (audit §5 junk-match): IWDA-relevant macro/ETF
    headline must score STRICTLY HIGHER than a celebrity-gossip headline."""
    db = _memory_db()
    user = _seed_user_with_holdings(
        db,
        [
            ("IWDA.AS", "etf", "iShares Core MSCI World UCITS ETF"),
            ("AAPL", "stock", "Apple Inc"),
        ],
    )

    _refresh_with_mocks(
        db,
        user,
        rss_items=[
            {
                "title": _IWDA_HEADLINE,
                "link": "https://example.test/rss/iwda-relevant",
                "summary": "The UCITS fund saw massive growth this quarter.",
                "published": "2026-06-01T10:00:00Z",
                "source": "TestFeed",
            }
        ],
        provider_items=[
            {
                "title": _GOSSIP_HEADLINE,
                "url": "https://example.test/news/gossip-regression",
                "source": "TestNews",
                "summary": _GOSSIP_SUMMARY,
                "published_at": "2026-06-01T11:00:00Z",
            },
        ],
    )

    relevant = (
        db.query(NewsItem)
        .filter(NewsItem.url == "https://example.test/rss/iwda-relevant")
        .one()
    )
    gossip = (
        db.query(NewsItem)
        .filter(NewsItem.url == "https://example.test/news/gossip-regression")
        .one()
    )

    relevant_rel = (
        db.query(UserNewsRelevance)
        .filter(UserNewsRelevance.news_item_id == relevant.id)
        .one_or_none()
    )
    gossip_rel = (
        db.query(UserNewsRelevance)
        .filter(UserNewsRelevance.news_item_id == gossip.id)
        .one_or_none()
    )

    assert relevant_rel is not None, "IWDA-relevant headline must be scored"
    assert relevant_rel.relevance_score is not None and relevant_rel.relevance_score > 0
    assert gossip_rel is None or (
        relevant_rel.relevance_score > gossip_rel.relevance_score
    ), "relevant headline must score strictly higher than gossip"


def test_get_api_news_returns_both_shapes_scored_listed_first():
    """GET /api/news returns scored and unscored shapes without error; NULL
    (unscored) sorts after scored even when it is newer."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = _seed_user_with_holdings(db, [("AAPL", "stock", "Apple Inc")])

    newer_gossip = datetime.now(UTC)
    older_match = newer_gossip - timedelta(hours=2)
    _refresh_with_mocks(
        db,
        user,
        rss_items=[],
        provider_items=[
            {
                "title": "AAPL surges as Apple beats earnings expectations",
                "url": "https://example.test/news/api-scored",
                "source": "TestNews",
                "summary": "Strong quarter.",
                "published_at": older_match.isoformat(),
            },
            {
                "title": _GOSSIP_HEADLINE,
                "url": "https://example.test/news/api-gossip",
                "source": "TestNews",
                "summary": _GOSSIP_SUMMARY,
                "published_at": newer_gossip.isoformat(),
            },
        ],
    )

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    client = TestClient(app)

    resp = client.get("/api/news")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2

    by_url = {r["url"]: r for r in rows}
    scored_shape = by_url["https://example.test/news/api-scored"]
    unscored_shape = by_url["https://example.test/news/api-gossip"]

    # Scored shape: non-null score/label/reason.
    assert scored_shape["relevance_score"] is not None
    assert scored_shape["relevance_label"] is not None
    assert scored_shape["relevance_reason"] is not None
    # Unscored shape: all-null relevance fields serialize cleanly.
    assert unscored_shape["relevance_score"] is None
    assert unscored_shape["relevance_label"] is None
    assert unscored_shape["relevance_reason"] is None

    # Ordering contract: scored first, unscored after — despite gossip being newer.
    assert rows[0]["url"] == "https://example.test/news/api-scored"
    assert rows[1]["url"] == "https://example.test/news/api-gossip"

    # Relevance label filter ignores unscored rows instead of erroring.
    scored_label = scored_shape["relevance_label"]
    resp = client.get(f"/api/news?relevance={scored_label}")
    assert resp.status_code == 200
    assert [r["url"] for r in resp.json()] == ["https://example.test/news/api-scored"]


def test_legacy_rows_keep_null_no_backfill():
    """Legacy unscored rows keep NULL forever: neither the read path nor an
    unrelated refresh backfills them."""
    db = _memory_db()
    user = _seed_user_with_holdings(db, [("AAPL", "stock", "Apple Inc")])

    legacy = NewsItem(
        title=_GOSSIP_HEADLINE,
        summary=_GOSSIP_SUMMARY,
        url="https://example.test/news/legacy",
        source="OldNews",
        published_at=datetime.now(UTC) - timedelta(days=30),
        fetched_at=datetime.now(UTC) - timedelta(days=30),
        is_macro=False,
    )
    db.add(legacy)
    db.commit()

    # Read path must not mutate scores.
    client = _api_client(db, user)
    resp = client.get("/api/news")
    assert resp.status_code == 200

    db.expire_all()
    legacy = db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/legacy").one()
    assert legacy.relevance_score is None
    assert legacy.relevance_label is None

    # A refresh whose feed does not match the legacy item must not backfill it.
    _refresh_with_mocks(
        db,
        user,
        rss_items=[],
        provider_items=[
            {
                "title": "AAPL announces buyback",
                "url": "https://example.test/news/buyback",
                "source": "TestNews",
                "summary": "Capital return program.",
                "published_at": datetime.now(UTC).isoformat(),
            },
        ],
    )

    db.expire_all()
    legacy = db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/legacy").one()
    assert legacy.relevance_score is None
    assert legacy.relevance_label is None
    assert (
        db.query(UserNewsRelevance)
        .filter(UserNewsRelevance.news_item_id == legacy.id)
        .count()
        == 0
    )


def test_scorer_exception_caught_per_item(caplog):
    """A throwing scorer on one malformed item stores it unscored, logs a
    warning, and still scores the remaining items."""
    db = _memory_db()
    user = _seed_user_with_holdings(db, [("AAPL", "stock", "Apple Inc")])

    real_score = news_service._score_news_item

    def flaky_score(item, keywords):
        if item.url == "https://example.test/news/malformed":
            raise ValueError("malformed item")
        return real_score(item, keywords)

    with caplog.at_level(logging.WARNING, logger="app.foundation.news"):
        with patch.object(news_service, "_score_news_item", side_effect=flaky_score):
            result = _refresh_with_mocks(
                db,
                user,
                rss_items=[],
                provider_items=[
                    {
                        "title": "AAPL malformed item triggers scorer crash",
                        "url": "https://example.test/news/malformed",
                        "source": "TestNews",
                        "summary": "boom",
                        "published_at": "2026-06-01T10:00:00Z",
                    },
                    {
                        "title": "AAPL recovers with strong guidance",
                        "url": "https://example.test/news/recovered",
                        "source": "TestNews",
                        "summary": "Guidance raised.",
                        "published_at": "2026-06-01T10:30:00Z",
                    },
                ],
            )

    assert result["created"] == 2  # refresh itself succeeded despite the scorer crash

    malformed = (
        db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/malformed").one()
    )
    assert malformed.relevance_score is None
    assert (
        db.query(UserNewsRelevance)
        .filter(UserNewsRelevance.news_item_id == malformed.id)
        .count()
        == 0
    )

    recovered = (
        db.query(NewsItem).filter(NewsItem.url == "https://example.test/news/recovered").one()
    )
    assert recovered.relevance_score is not None
    assert any("malformed item" in rec.getMessage() for rec in caplog.records), (
        "per-item scorer failure must be logged as a warning"
    )
