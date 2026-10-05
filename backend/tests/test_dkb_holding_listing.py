"""A DKB-synced holding follows its position's listing.

On prod the holding for IE00B4L5Y983 stayed on IWDA.L (the USD London line
yfinance picked before PR #273 pinned EUNL.DE): the pin re-pointed the DKB
position, but the sync only filled a holding's ticker when it was empty.
"""
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import DkbAccount, DkbPosition, Holding, User
from app.foundation.portfolio_service import main_portfolio, sync_dkb_positions_to_holdings

WORLD = "IE00B4L5Y983"
SAP = "DE0007164600"


def _seed(db, positions):
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE00DEPOT", balance=0)
    db.add(account)
    db.flush()
    for isin, ticker in positions:
        db.add(DkbPosition(
            account_id=account.id, isin=isin, ticker=ticker, name=isin,
            quantity=Decimal("10"), avg_buy_price=Decimal("50"),
        ))
    db.commit()
    return user


def _holding(db, user, isin, ticker, source="dkb_sync"):
    db.add(Holding(
        portfolio_id=main_portfolio(db, user.id).id, isin=isin, ticker=ticker, name=isin,
        asset_type="etf", quantity=Decimal("10"), currency="EUR", source=source,
    ))
    db.commit()


def test_a_synced_holding_moves_to_the_pinned_listing():
    db = _memory_db()
    user = _seed(db, [(WORLD, "IWDA.L")])
    _holding(db, user, WORLD, "IWDA.L")

    sync_dkb_positions_to_holdings(db, user.id)

    assert db.query(DkbPosition).one().ticker == "EUNL.DE"
    assert db.query(Holding).one().ticker == "EUNL.DE"


def test_a_synced_holding_follows_a_changed_position_ticker():
    db = _memory_db()
    user = _seed(db, [(SAP, "SAP.DE")])
    _holding(db, user, SAP, "SAP")

    sync_dkb_positions_to_holdings(db, user.id)

    assert db.query(Holding).one().ticker == "SAP.DE"


def test_a_manual_holding_for_the_same_isin_keeps_its_ticker():
    db = _memory_db()
    user = _seed(db, [(WORLD, "EUNL.DE")])
    _holding(db, user, WORLD, "IWDA.L", source="manual")

    sync_dkb_positions_to_holdings(db, user.id)

    # One row per ISIN (uq_holdings_portfolio_isin): the manual row stands for
    # the position. Adding a synced row beside it used to fail the whole sync.
    [holding] = db.query(Holding).all()
    assert (holding.source, holding.ticker) == ("manual", "IWDA.L")
