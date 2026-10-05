"""Scalable rows in a foreign currency reach the tax estimate at the ECB rate of their own day."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import ActivityLedgerEntry, ConnectedAccount, TaxLot, User
from app.foundation.scalable.service import ingest_tax_events

US = "US0378331005"
BUY_DAY, SELL_DAY = date(2026, 3, 2), date(2026, 9, 1)
RATES = {BUY_DAY: 0.90, SELL_DAY: 0.80}  # EUR per USD


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _setup(db, *, sell: bool = True):
    user = User(username="fx", password_hash="x")
    db.add(user)
    db.commit()
    depot = ConnectedAccount(user_id=user.id, source="scalable", external_id="pf", name="Scalable",
                             institution="Scalable Capital", account_type="depot", currency="EUR",
                             balance=Decimal("0"), raw_json="{}")
    db.add(depot)
    db.flush()
    db.add(ActivityLedgerEntry(
        user_id=user.id, connected_account_id=depot.id, source="scalable", external_id="b1", dedupe_hash="b1",
        activity_type="buy", date=BUY_DAY, isin=US, symbol="AAPL", quantity=Decimal("10"), price=Decimal("100"),
        amount=Decimal("-1000"), fees=Decimal("0"), currency="USD", description="Apple"))
    if sell:
        db.add(ActivityLedgerEntry(
            user_id=user.id, connected_account_id=depot.id, source="scalable", external_id="s1", dedupe_hash="s1",
            activity_type="sell", date=SELL_DAY, isin=US, symbol="AAPL", quantity=Decimal("-10"),
            price=Decimal("120"), amount=Decimal("1200"), fees=Decimal("0"), currency="USD", description="Apple"))
    db.commit()
    return user


def _rate(_db, ccy, on):
    return (RATES.get(on), "ecb") if ccy == "USD" else (1.0, "identity")


def test_each_leg_is_converted_at_its_own_days_rate():
    db = _memory_db()
    user = _setup(db)
    with patch("app.foundation.ecb_fx.eur_per_unit", side_effect=_rate):
        result = ingest_tax_events(db, user.id)

    assert result["lots"] == 1 and result["sales"] == 1 and result["converted"] == 2
    from app.foundation.models.entities import TaxLedgerEvent

    sale = db.query(TaxLedgerEvent).filter_by(event_type="sale").one()
    # Bought for $1,000 at 0.90 (EUR 900), sold for $1,200 at 0.80 (EUR 960): a EUR 60
    # gain, though the dollar gain was $200 (and at one rate it would have been EUR 160 or 180).
    assert sale.gross_eur == Decimal("960")
    assert sale.realised_gain_eur == Decimal("60")
    assert db.query(TaxLot).one().quantity_remaining == 0


def test_a_day_without_any_rate_is_named_not_booked_at_par():
    db = _memory_db()
    user = _setup(db, sell=False)
    with patch("app.foundation.ecb_fx.eur_per_unit", return_value=(None, "none")):
        result = ingest_tax_events(db, user.id)

    assert result["lots"] == 0
    assert db.query(TaxLot).count() == 0
    assert any("no USD/EUR rate for 2026-03-02" in w for w in result["tax_warnings"])
