"""The "keyword list" banner (plan 2026-11-12, WP13): the Control Center warns when recent news was scored only by
the keyword fallback because FinBERT did not load, and stays quiet once FinBERT scores anything."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.control_center import _sentiment_items
from app.foundation.models.entities import NewsItem

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _news(db, source, *, days_ago=1):
    at = NOW - timedelta(days=days_ago)
    n = db.query(NewsItem).count()
    db.add(NewsItem(title="t", url=f"https://example.test/{n}", source="feed", sentiment_source=source, published_at=at, fetched_at=at))
    db.commit()


def _items(db):
    items = []
    _sentiment_items(db, NOW, items)
    return items


def test_keyword_only_scoring_raises_the_banner():
    db = _memory_db()
    _news(db, "lexicon")
    _news(db, "lexicon")
    (item,) = _items(db)
    assert item.title == "News sentiment runs on a keyword list, not FinBERT"
    assert item.severity == "warning" and "2 articles" in item.detail


def test_finbert_scoring_anything_keeps_it_quiet():
    db = _memory_db()
    _news(db, "lexicon")
    _news(db, "finbert")
    assert _items(db) == []


def test_old_keyword_scores_and_unscored_news_raise_nothing():
    db = _memory_db()
    _news(db, "lexicon", days_ago=4)
    _news(db, None)
    assert _items(db) == []
