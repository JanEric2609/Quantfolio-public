from datetime import date, timedelta
from decimal import Decimal

from conftest import _memory_db

from app.interface.api.portfolio import snapshots
from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    DkbTransaction,
    Holding,
    PortfolioSnapshot,
    User,
)
from app.foundation.dkb import dkb_transaction_hash
from app.foundation.portfolio_service import main_portfolio, sync_dkb_to_wealth_ledger, wealth_summary


def test_dkb_mirror_creates_connected_account_activity_and_summary():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="giro", iban="DE001", balance=Decimal("1200.50"), currency="EUR")
    db.add(account)
    db.commit()
    tx_date = date.today() - timedelta(days=5)
    tx_hash = dkb_transaction_hash(tx_date, Decimal("-42.10"), "Groceries")
    db.add(
        DkbTransaction(
            account_id=account.id,
            date=tx_date,
            amount=Decimal("-42.10"),
            currency="EUR",
            reference="Groceries",
            dedupe_hash=tx_hash,
        )
    )
    db.add(
        DkbPosition(
            account_id=account.id,
            isin="IE00B4L5Y983",
            name="iShares Core MSCI World",
            quantity=Decimal("10"),
            current_price=Decimal("85"),
            current_value=Decimal("850"),
        )
    )
    db.commit()

    sync_result = sync_dkb_to_wealth_ledger(db, user.id)
    summary = wealth_summary(db, user.id)

    assert sync_result["accounts"] == 1
    assert sync_result["ledger_created"] == 1
    assert summary["total_value"] == 2050.5
    assert summary["cashflow_30d"]["outflow"] == 42.1
    assert summary["positions"][0]["source"] == "dkb"


def test_wealth_snapshot_includes_manual_holdings():
    db = _memory_db()
    user = User(username="manual", password_hash="hash")
    db.add(user)
    db.commit()
    portfolio = main_portfolio(db, user.id)
    db.add(
        Holding(
            portfolio_id=portfolio.id,
            name="Manual ETF",
            ticker="ETF.DE",
            quantity=Decimal("3"),
            avg_buy_price=Decimal("25"),
            currency="EUR",
        )
    )
    db.commit()

    summary = wealth_summary(db, user.id)
    rows = snapshots(days=30, user=user, db=db)

    assert summary["total_value"] == 75
    assert rows[-1]["total_value"] == 75
    assert rows[-1]["security_value"] == 75


def test_snapshots_filter_sparse_rows_by_calendar_window():
    db = _memory_db()
    user = User(username="range", password_hash="hash")
    db.add(user)
    db.commit()
    today = date.today()
    db.add_all(
        [
            PortfolioSnapshot(user_id=user.id, date=today, source="manual", total_value=Decimal("20")),
            PortfolioSnapshot(user_id=user.id, date=today.replace(year=today.year - 1), source="manual", total_value=Decimal("10")),
        ]
    )
    db.commit()

    rows = snapshots(days=7, user=user, db=db)

    assert [row["date"] for row in rows] == [today.isoformat()]


def test_dkb_sync_preserves_unknown_avg_buy_price_on_new_holding():
    db = _memory_db()
    user = User(username="unknown-avg-buy", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE-DEPOT-3", balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(
        DkbPosition(
            account_id=account.id,
            isin="IE00B4L5Y983",
            name="iShares Core MSCI World",
            quantity=Decimal("120.5"),
            avg_buy_price=None,
            current_price=Decimal("85.40"),
            current_value=Decimal("10290.70"),
        )
    )
    db.commit()

    sync_dkb_to_wealth_ledger(db, user.id)

    portfolio = main_portfolio(db, user.id)
    holding = next(h for h in portfolio.holdings if h.isin == "IE00B4L5Y983")
    assert holding.avg_buy_price is None


def test_dkb_sync_preserves_known_zero_avg_buy_price_on_new_holding():
    db = _memory_db()
    user = User(username="known-zero-avg-buy", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE-DEPOT-4", balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(
        DkbPosition(
            account_id=account.id,
            isin="IE00B4L5Y983",
            name="iShares Core MSCI World",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("0"),
            current_price=Decimal("85.40"),
            current_value=Decimal("8540"),
        )
    )
    db.commit()

    sync_dkb_to_wealth_ledger(db, user.id)

    portfolio = main_portfolio(db, user.id)
    holding = next(h for h in portfolio.holdings if h.isin == "IE00B4L5Y983")
    assert holding.avg_buy_price == Decimal("0")


def test_dkb_resync_clears_stale_avg_buy_price_when_now_unknown():
    db = _memory_db()
    user = User(username="resync-stale-avg-buy", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE-DEPOT-5", balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    position = DkbPosition(
        account_id=account.id,
        isin="IE00B4L5Y983",
        name="iShares Core MSCI World",
        quantity=Decimal("100"),
        avg_buy_price=Decimal("0"),
        current_price=Decimal("85.40"),
        current_value=Decimal("8540"),
    )
    db.add(position)
    db.commit()

    # First sync creates the holding with the stale (pre-fix) cost basis of 0.
    sync_dkb_to_wealth_ledger(db, user.id)
    portfolio = main_portfolio(db, user.id)
    holding = next(h for h in portfolio.holdings if h.isin == "IE00B4L5Y983")
    assert holding.avg_buy_price == Decimal("0")

    # Re-sync with the position now reporting an unknown cost basis must clear
    # the stale value on the existing Holding, not leave it stuck at 0.
    position.avg_buy_price = None
    db.commit()
    sync_dkb_to_wealth_ledger(db, user.id)

    db.refresh(holding)
    assert holding.avg_buy_price is None


def test_dkb_snapshot_writers_attach_the_main_portfolio():
    # Verification's risk/stress/alert modules read snapshots by portfolio_id;
    # both DKB writers used to leave it NULL, hiding every snapshot from them.
    from app.foundation.portfolio_service import snapshot_book_positions

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE002", balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(DkbPosition(account_id=account.id, isin="IE00B4L5Y983", name="MSCI World",
                       quantity=Decimal("2"), current_price=Decimal("100"), current_value=Decimal("200")))
    db.commit()
    portfolio = main_portfolio(db, user.id)

    sync_dkb_to_wealth_ledger(db, user.id)
    snapshot_book_positions(db, user.id)

    rows = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.user_id == user.id).all()
    assert rows
    assert {r.portfolio_id for r in rows} == {portfolio.id}


def test_cashflow_window_ignores_imported_trades():
    from app.foundation.models.entities import ActivityLedgerEntry

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    today = date.today()
    db.add_all([
        ActivityLedgerEntry(user_id=user.id, source="csv_import", dedupe_hash="t1", activity_type="buy",
                            date=today, amount=Decimal("500"), currency="EUR", description="MSCI World"),
        ActivityLedgerEntry(user_id=user.id, source="dkb", dedupe_hash="c1", activity_type="cashflow",
                            date=today, amount=Decimal("-40"), currency="EUR", description="Groceries"),
    ])
    db.commit()

    summary = wealth_summary(db, user.id)

    assert summary["cashflow_30d"]["income"] == 0
    assert summary["cashflow_30d"]["outflow"] == 40
