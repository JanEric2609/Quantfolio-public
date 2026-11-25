"""News alerts: what reaches the owner, how it reads, and the morning holdings digest."""
import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import news_digest, newswire
from app.foundation import news_wire
from app.foundation.core.db import Base
from app.foundation.models.entities import NewsItem, NewsWireItem

BERLIN = ZoneInfo("Europe/Berlin")
R = newswire.Rating
NOW = datetime(2026, 11, 25, 8, 17, tzinfo=BERLIN).astimezone(UTC)  # a Wednesday


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _item(**kw):
    base = {"source": "alpaca", "source_id": "1", "headline": "h", "published_at": NOW - timedelta(minutes=5),
            "symbols": ()}
    return news_wire.WireItem(**{**base, **kw})


def test_market_recap_from_handelsblatt_is_never_an_alert():
    db = _memory_db()
    item = _item(source="handelsblatt", headline="Börsen: Neue Flucht aus Risikoanlagen: Warum sich der Ausverkauf nun beschleunigt")
    assert newswire.alert_decision(db, item, "", R(3, "down", "market", 500), NOW) == ""
    # a wire that publishes events still may, once per cooldown
    wire = _item(headline="Fed emergency cut")
    assert newswire.alert_decision(db, wire, "", R(3, "down", "market", 500), NOW) == "send"
    db.add(NewsWireItem(id="alpaca:x", source="alpaca", received_at=NOW - timedelta(hours=1), headline="x",
                        relevance="", alert="sent"))
    db.flush()
    assert newswire.alert_decision(db, wire, "", R(3, "down", "market", 500), NOW) == "market_dup"


def test_handelsblatt_headline_finds_a_holding_by_name():
    watch = newswire.Watch()
    for name, label in [("SAP SE", "SAP"), ("Deutsche Telekom AG", "DTE"), ("iShares Core MSCI World UCITS ETF", "EUNL")]:
        phrase = newswire.name_phrase(name)
        if phrase:
            watch.phrases[phrase] = f"holding:{label}"
    assert "deutsche telekom" in watch.phrases and "sap" in watch.phrases
    assert not any("ishares" in k or "msci" in k for k in watch.phrases)  # a fund never matches every headline
    hit = _item(source="handelsblatt", headline="SAP: Cloud-Geschäft überrascht positiv")
    assert newswire.relevance(hit, watch) == "holding:SAP"
    miss = _item(source="handelsblatt", headline="Deutsche Bank kündigt Stellenabbau an")
    assert newswire.relevance(miss, watch) == ""  # "deutsche" alone is not Deutsche Telekom
    # only Handelsblatt is read by name: an Alpaca item keeps its own ticker rule
    assert newswire.relevance(_item(headline="SAP rallies"), watch) == ""


def test_same_headline_is_stored_once_and_not_alerted_twice(monkeypatch):
    db = _memory_db()
    sent = []
    monkeypatch.setattr("app.decision.desk.telegram.alert", lambda db, uid, text, markup=None, **kw: sent.append(text))

    async def fake_rate(db, item):
        return R(3, "down", "company", 10)

    monkeypatch.setattr(newswire, "rate", fake_rate)
    watch = {"NVDA": "holding:NVDA"}
    a = _item(source_id="a", symbols=("NVDA",), headline="Nvidia cuts guidance")
    b = _item(source_id="b", symbols=("NVDA",), headline="Nvidia  cuts guidance!")
    assert asyncio.run(newswire.ingest(db, "u", a, watch, now=NOW)) == "sent"
    assert asyncio.run(newswire.ingest(db, "u", b, watch, now=NOW)) == "dup_headline"
    assert len(sent) == 1


def test_digest_groups_holdings_and_is_silent_when_empty(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(newswire, "watchlist", lambda db, uid: newswire.Watch({"NVDA": "holding:NVDA"}))
    monkeypatch.setattr(news_digest, "watchlist", lambda db, uid: newswire.Watch({"NVDA": "holding:NVDA"}))
    assert news_digest.build(db, "u", NOW) is None
    got = NOW - timedelta(hours=5)
    db.add_all([
        NewsWireItem(id="alpaca:1", source="alpaca", received_at=got, headline="Nvidia cuts guidance", relevance="holding:NVDA",
                     materiality=3, direction="down", scope="company", alert="sent"),
        NewsWireItem(id="alpaca:2", source="alpaca", received_at=got, headline="Nvidia opens office", relevance="holding:NVDA",
                     materiality=0, direction="unclear", scope="company", alert=""),
        NewsWireItem(id="alpaca:3", source="alpaca", received_at=got, headline="Old backlog", relevance="holding:NVDA",
                     materiality=2, alert="baseline"),
        NewsWireItem(id="ecb:1", source="ecb", received_at=got, headline="ECB holds rates", relevance="central_bank",
                     materiality=2, alert=""),
        NewsItem(title="Nvidia supplier warns", url="https://example.com/n", source="Reuters", ticker="NVDA",
                 published_at=got),
    ])
    db.flush()
    text = news_digest.build(db, "u", NOW)
    assert text is not None
    assert "Nvidia cuts guidance" in text and "alerted" in text
    assert "Nvidia supplier warns" in text and "ECB holds rates" in text
    assert "opens office" not in text and "Old backlog" not in text


def test_digest_due_weekday_morning_only():
    assert news_digest.digest_due(NOW)
    assert not news_digest.digest_due(NOW + timedelta(hours=2))
    assert not news_digest.digest_due(NOW + timedelta(days=3))  # Saturday
