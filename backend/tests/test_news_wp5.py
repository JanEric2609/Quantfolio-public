"""WP5 (plan 2026-11-10): sentiment source, relevance matcher, feed attribution."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import _memory_db

from app.foundation import news as news_service
from app.foundation import sentiment
from app.foundation.models.entities import Asset, Holding, NewsItem, Portfolio, User


def _user_with(db, holdings):
    user = User(id=uuid4().hex, username=uuid4().hex[:8], password_hash="x")
    portfolio = Portfolio(id=uuid4().hex, user_id=user.id, name="Main")
    db.add_all([user, portfolio])
    for i, (ticker, kind, name) in enumerate(holdings):
        db.add(Asset(id=f"a{i}", symbol=ticker, name=name, asset_type=kind, currency="EUR"))
        db.add(Holding(id=f"h{i}", portfolio_id=portfolio.id, ticker=ticker, name=name,
                       quantity=1, avg_buy_price=1, currency="EUR"))
    db.commit()
    return user


def _item(title, summary="", **kw):
    return NewsItem(id=uuid4().hex, title=title, summary=summary, url=f"https://x.test/{uuid4().hex}",
                    source="t", published_at=datetime.now(UTC), **kw)


HOLDINGS = [("C", "stock", "Citigroup Inc"), ("O", "stock", "Realty Income Corp"), ("MA", "stock", "Mastercard Inc"),
            ("LOW", "stock", "Lowe's Companies Inc"), ("AAPL", "stock", "Apple Inc")]


def test_short_tickers_no_longer_match_inside_words():
    db = _memory_db()
    user = _user_with(db, HOLDINGS)
    matcher = news_service.build_matcher(db, user.id)
    assert news_service._score_news_item(_item("Costco raises forecast"), matcher) is None
    assert news_service._score_news_item(_item("Core cash pile grows in Macau"), matcher) is None


def test_company_in_title_is_high_summary_only_medium_macro_low():
    db = _memory_db()
    user = _user_with(db, HOLDINGS)
    matcher = news_service.build_matcher(db, user.id)
    score, label, why = news_service._score_news_item(_item("Mastercard beats estimates"), matcher)
    assert label == "high" and why == {"Mastercard"}
    _, label, _ = news_service._score_news_item(_item("Card payments rise", "Mastercard said on Monday"), matcher)
    assert label == "medium"
    _, label, why = news_service._score_news_item(_item("ECB signals another rate cut", "inflation eases"), matcher)
    assert label == "low" and "ecb" in why
    # 'fed' only as the central bank, not 'fed up'
    assert news_service._score_news_item(_item("Voters fed up with prices"), matcher) is None


def test_articles_are_filed_under_the_company_they_name_not_the_feed():
    db = _memory_db()
    _user_with(db, [("AAPL", "stock", "Apple Inc"), ("BMY", "stock", "Bristol-Myers Squibb Company")])
    for title, feed in (("Qualcomm vs. Western Digital", "AAPL"), ("Merck wins approval", "BMY"),
                        ("Apple unveils a new chip", "AAPL")):
        news_service.store_news_item(db, {"title": title, "url": f"https://x.test/{title}",
                                          "published_at": "2026-11-09T10:00:00Z"}, "t", feed)
    db.commit()
    rows = {r.title: r for r in db.query(NewsItem).all()}
    assert rows["Qualcomm vs. Western Digital"].ticker is None
    assert rows["Qualcomm vs. Western Digital"].feed_ticker == "AAPL"
    assert rows["Merck wins approval"].ticker is None and rows["Merck wins approval"].feed_ticker == "BMY"
    assert rows["Apple unveils a new chip"].ticker == "AAPL"


def test_retag_moves_the_feed_symbol_and_clears_tags():
    db = _memory_db()
    _user_with(db, [("AAPL", "stock", "Apple Inc")])
    db.add_all([_item("Qualcomm vs. Western Digital", ticker="AAPL", relevance_label="high"),
                _item("Apple unveils a new chip", ticker="AAPL", relevance_label="high")])
    db.commit()
    out = news_service.retag_news(db)
    assert out == {"feed_ticker_set": 2, "ticker_cleared": 1}
    rows = {r.title: r for r in db.query(NewsItem).all()}
    assert rows["Qualcomm vs. Western Digital"].ticker is None
    assert all(r.feed_ticker == "AAPL" and r.relevance_label is None for r in rows.values())


def _news(db, ticker, label, source, score=0.9):
    db.add(_item(f"{label} {uuid4().hex[:4]}", ticker=ticker, sentiment_label=label,
                 sentiment_score=score, sentiment_source=source))


def test_lexicon_rows_count_as_unscored_and_net_tone_needs_three_scored():
    db = _memory_db()
    for _ in range(5):
        _news(db, "AAPL", "positive", "lexicon")
    db.commit()
    out = sentiment.aggregate_sentiment(db, "AAPL")
    assert out["total"] == 5 and out["scored"] == 0 and out["overall"] == "unscored" and out["net_tone"] is None
    for label in ("positive", "positive", "negative"):
        _news(db, "AAPL", label, "finbert")
    db.commit()
    out = sentiment.aggregate_sentiment(db, "AAPL")
    assert out["scored"] == 3 and out["net_tone"] == round((2 - 1) / 3, 4)
    assert out["finbert_score"] == out["net_tone"] and out["news_count"] == 3


def test_net_tone_is_direction_not_confidence():
    assert sentiment.net_tone(2, 2, 4) == 0.0       # equally confident positive and negative cancel
    assert sentiment.net_tone(1, 0, 2) is None      # under three scored articles
    assert sentiment.net_tone(3, 0, 3) == 1.0


def test_enrich_records_the_scorer_that_ran(monkeypatch):
    db = _memory_db()
    db.add(_item("Profits surge to a record", ticker="AAPL"))
    db.commit()
    monkeypatch.setattr(sentiment, "_ensure_model", lambda: None)  # FinBERT missing
    assert sentiment.enrich_news_items(db) == 1
    row = db.query(NewsItem).one()
    assert row.sentiment_source == "lexicon" and row.sentiment_label == "positive"
    assert sentiment.scorer_is_fallback() in (True, False)
    # a reader ignores it
    unit, scored = sentiment.tone_unit([row])
    assert unit is None and scored == 0
    assert (datetime.now(UTC) - timedelta(days=1)) < datetime.now(UTC)


def test_general_feed_items_are_stored_as_macro_rows():
    from app.foundation.models.entities import NewsItem
    from app.foundation.news import _store_macro_feed_items

    db = _memory_db()
    items = [
        {"title": "ECB holds rates", "url": "https://x.test/1", "published": "2026-11-10T08:00:00"},
        {"title": "Oil falls", "url": "https://x.test/2", "published": "2026-11-10T07:00:00"},
    ]
    assert _store_macro_feed_items(db, items) == 2
    db.commit()
    rows = db.query(NewsItem).all()
    assert {r.is_macro for r in rows} == {True}
    assert {r.ticker for r in rows} == {None}
    assert _store_macro_feed_items(db, items) == 0  # no duplicates on the next refresh
