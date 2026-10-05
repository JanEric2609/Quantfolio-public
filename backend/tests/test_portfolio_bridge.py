"""Tests for portfolio_bridge snapshot creation with manual holdings."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    User,
)
from app.foundation.portfolio_service import snapshot_book_positions


def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str) -> Portfolio:
    portfolio = Portfolio(id=uuid4().hex, user_id=user_id, name="Test", currency="EUR")
    db.add(portfolio)
    db.flush()
    return portfolio


def _make_dkb_account(db, user_id: str, account_type: str = "depot") -> DkbAccount:
    account = DkbAccount(
        id=uuid4().hex,
        user_id=user_id,
        type=account_type,
        iban=f"DE{uuid4().hex[:20]}",
        balance=Decimal("1000"),
        currency="EUR",
    )
    db.add(account)
    db.flush()
    return account


def _make_dkb_position(db, account_id: str, isin: str = "DE0000000001") -> DkbPosition:
    position = DkbPosition(
        id=uuid4().hex,
        account_id=account_id,
        isin=isin,
        ticker="TEST",
        name="Test Position",
        quantity=Decimal("10"),
        avg_buy_price=Decimal("100"),
        current_price=Decimal("110"),
        current_value=Decimal("1100"),
    )
    db.add(position)
    db.flush()
    return position


def _make_manual_holding(db, portfolio_id: str, ticker: str = "AAPL") -> Holding:
    holding = Holding(
        id=uuid4().hex,
        portfolio_id=portfolio_id,
        ticker=ticker,
        name=f"{ticker} Inc.",
        asset_type="stock",
        quantity=Decimal("5"),
        avg_buy_price=Decimal("200"),
        currency="EUR",
        source="manual",
    )
    db.add(holding)
    db.flush()
    return holding


class TestSnapshotBookPositionsWithManualHoldings:

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_includes_manual_holdings_in_portfolio_snapshot(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        dkb_account = _make_dkb_account(db, user.id)
        _make_dkb_position(db, dkb_account.id)
        _make_manual_holding(db, portfolio.id, ticker="AAPL")

        result = snapshot_book_positions(db, user.id)

        assert result["manual_holdings_count"] == 1
        assert result["portfolio_snapshot_created"] is True

        portfolio_snapshot = (
            db.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.user_id == user.id,
                PortfolioSnapshot.source == "dkb_mirror",
            )
            .one_or_none()
        )
        assert portfolio_snapshot is not None
        assert portfolio_snapshot.security_value == Decimal("1100") + Decimal("1000")
        assert portfolio_snapshot.total_value == Decimal("1100") + Decimal("1000")

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_creates_snapshot_without_dkb_accounts(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        _make_manual_holding(db, portfolio.id, ticker="MSFT")

        result = snapshot_book_positions(db, user.id)

        assert result["manual_holdings_count"] == 1
        assert result["positions_count"] == 0

        portfolio_snapshot = (
            db.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.user_id == user.id,
                PortfolioSnapshot.source == "dkb_mirror",
            )
            .one_or_none()
        )
        assert portfolio_snapshot is not None
        assert portfolio_snapshot.security_value == Decimal("1000")
        assert portfolio_snapshot.total_value == Decimal("1000")

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_multiple_manual_holdings_summed(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        _make_manual_holding(db, portfolio.id, ticker="AAPL")
        _make_manual_holding(db, portfolio.id, ticker="GOOGL")

        result = snapshot_book_positions(db, user.id)

        assert result["manual_holdings_count"] == 2

        portfolio_snapshot = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.user_id == user.id)
            .one()
        )
        assert portfolio_snapshot.security_value == Decimal("2000")

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_dkb_sync_holdings_excluded(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        dkb_holding = Holding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            ticker="DKB",
            name="DKB Holding",
            asset_type="stock",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("50"),
            currency="EUR",
            source="dkb_sync",
        )
        db.add(dkb_holding)
        db.flush()

        result = snapshot_book_positions(db, user.id)

        assert result["manual_holdings_count"] == 0

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_existing_snapshot_updated_not_duplicated(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        _make_manual_holding(db, portfolio.id)

        today = date.today()
        existing = PortfolioSnapshot(
            user_id=user.id, date=today, source="dkb_mirror",
            total_value=Decimal("0"), cash_value=Decimal("0"),
            security_value=Decimal("0"),
        )
        db.add(existing)
        db.commit()

        snapshot_book_positions(db, user.id)

        snapshots = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.user_id == user.id)
            .all()
        )
        assert len(snapshots) == 1
        assert snapshots[0].security_value == Decimal("1000")

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_dkb_positions_alongside_manual(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        dkb_account = _make_dkb_account(db, user.id)
        _make_dkb_position(db, dkb_account.id)
        _make_manual_holding(db, portfolio.id, ticker="MSFT")

        result = snapshot_book_positions(db, user.id)

        assert result["positions_count"] == 1
        assert result["manual_holdings_count"] == 1

        portfolio_snapshot = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.user_id == user.id)
            .one()
        )
        assert portfolio_snapshot.security_value == Decimal("1100") + Decimal("1000")

    @patch("app.foundation.portfolio_service.main_portfolio")
    def test_dkb_cash_account_included(self, mock_main_portfolio):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        mock_main_portfolio.return_value = portfolio
        cash_account = _make_dkb_account(db, user.id, account_type="girokonto")
        cash_account.balance = Decimal("5000")
        _make_manual_holding(db, portfolio.id)

        snapshot_book_positions(db, user.id)

        portfolio_snapshot = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.user_id == user.id)
            .one()
        )
        assert portfolio_snapshot.cash_value == Decimal("5000")
        assert portfolio_snapshot.total_value == Decimal("5000") + Decimal("1000")
