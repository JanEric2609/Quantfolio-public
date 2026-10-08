"""Regression tests: endpoints returning ORM rows under dict annotations.

These endpoints used to return SQLAlchemy rows directly from `-> list[dict]`
handlers, which passes with an empty table but raises ResponseValidationError
(HTTP 500) as soon as real rows exist. Each test inserts one row and asserts
a 200 with explicitly serialized fields.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import ActivityLedgerEntry, ConnectedAccount, NewsItem, User
from app.foundation.auth import current_user


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    return TestClient(app), maker, user.id


def test_activity_returns_200_with_rows():
    client, maker, user_id = _client()
    db = maker()
    db.add(
        ActivityLedgerEntry(
            user_id=user_id,
            source="dkb_auto",
            dedupe_hash="hash-1",
            activity_type="cashflow",
            date=date(2026, 6, 1),
            amount=Decimal("-42.50"),
            currency="EUR",
            description="REWE sagt danke",
        )
    )
    db.commit()

    resp = client.get("/api/portfolio/activity")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["amount"] == -42.5
    assert row["date"] == "2026-06-01"
    assert row["description"] == "REWE sagt danke"
    assert row["review_state"] == "trusted"


def test_accounts_returns_200_with_rows():
    client, maker, user_id = _client()
    db = maker()
    db.add(
        ConnectedAccount(
            user_id=user_id,
            source="dkb",
            external_id="acc-1",
            name="Giro",
            institution="DKB",
            account_type="cash",
            iban="DE00120300000000001234",
            currency="EUR",
            balance=Decimal("2000.00"),
            last_synced=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        )
    )
    db.commit()

    resp = client.get("/api/portfolio/accounts")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "cash"
    assert row["balance"] == 2000.0
    assert row["last_synced"].startswith("2026-06-10T12:00:00")


def test_news_list_returns_200_with_rows():
    client, maker, _user_id = _client()
    db = maker()
    db.add(
        NewsItem(
            title="iShares Core MSCI World hits new high",
            summary="IWDA continues its run.",
            url="https://example.test/news/iwda",
            source="example",
            ticker="IWDA.AS",
            sentiment_score=Decimal("0.8123"),
            sentiment_label="positive",
            published_at=datetime.now(UTC),
            is_macro=False,
        )
    )
    db.commit()

    resp = client.get("/api/news")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["sentiment_score"] == 0.8123
    assert row["sentiment_label"] == "positive"
    assert row["is_macro"] is False

    resp = client.get("/api/news/ticker/iwda.as")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_news_macro_returns_200_with_rows():
    client, maker, _user_id = _client()
    db = maker()
    db.add(
        NewsItem(
            title="ECB holds rates",
            url="https://example.test/news/ecb",
            source="example",
            published_at=datetime.now(UTC),
            is_macro=True,
        )
    )
    db.commit()

    resp = client.get("/api/news/macro")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["is_macro"] is True
