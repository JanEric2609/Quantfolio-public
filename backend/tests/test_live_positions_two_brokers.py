"""Phase 2: every reader of the real book sees DKB and Scalable alike.

The fixture is a two-broker set-up in miniature: a legacy MSCI World at DKB
(with its synced copy in the holdings table), a little of the same fund and an
All-World ETF at Scalable, cash at both, and the Scalable Tagesgeld.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    BrokerPosition,
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    Holding,
    PaperHolding,
    PaperPortfolio,
    Portfolio,
    User,
)

MSCI_WORLD = "IE00B4L5Y983"
ALL_WORLD = "IE00BK5BQT80"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _book(db) -> str:
    user = User(id=str(uuid4()), username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    giro = DkbAccount(user_id=user.id, type="giro", balance=Decimal("500"))
    depot = DkbAccount(user_id=user.id, type="depot", balance=Decimal("10000"))
    db.add_all([giro, depot])
    db.flush()
    db.add(DkbPosition(
        account_id=depot.id, isin=MSCI_WORLD, ticker="EUNL.DE", name="iShares Core MSCI World",
        quantity=Decimal("100"), avg_buy_price=Decimal("70"), current_price=Decimal("100"),
        current_value=Decimal("10000"),
    ))
    sc_depot = ConnectedAccount(user_id=user.id, source="scalable", name="Scalable depot", account_type="depot",
                                balance=Decimal("0"))
    sc_cash = ConnectedAccount(user_id=user.id, source="scalable", name="Scalable cash", account_type="cash",
                               balance=Decimal("42.10"))
    sc_overnight = ConnectedAccount(user_id=user.id, source="scalable", name="Scalable overnight",
                                    account_type="savings", balance=Decimal("812.40"))
    # DKB's own accounts are mirrored into connected_accounts too; synced_cash
    # must not count them a second time.
    dkb_mirror = ConnectedAccount(user_id=user.id, source="dkb", name="DKB giro", account_type="cash",
                                  balance=Decimal("500"))
    db.add_all([sc_depot, sc_cash, sc_overnight, dkb_mirror])
    db.flush()
    db.add_all([
        BrokerPosition(
            user_id=user.id, connected_account_id=sc_depot.id, source="scalable", isin=MSCI_WORLD,
            ticker=None, name="iShares Core MSCI World", security_type="ETF", quantity=Decimal("1"),
            avg_buy_price=Decimal("95"), current_price=Decimal("100"), current_value=Decimal("100"),
        ),
        BrokerPosition(
            user_id=user.id, connected_account_id=sc_depot.id, source="scalable", isin=ALL_WORLD,
            ticker="VWCE.DE", name="Vanguard FTSE All-World", security_type="ETF", quantity=Decimal("0.2"),
            avg_buy_price=Decimal("100"), current_price=Decimal("100"), current_value=Decimal("20"),
        ),
    ])
    portfolio = Portfolio(user_id=user.id, name="Main Portfolio", currency="EUR")
    db.add(portfolio)
    db.flush()
    # The synced copy the DKB sync mirrors into the holdings table.
    db.add(Holding(
        portfolio_id=portfolio.id, isin=MSCI_WORLD, ticker="EUNL.DE", name="iShares Core MSCI World",
        asset_type="etf", quantity=Decimal("100"), avg_buy_price=Decimal("70"), currency="EUR", source="dkb_sync",
    ))
    db.commit()
    return user.id


def test_combined_positions_sums_depots_per_isin():
    from app.foundation.live_positions import combined_positions

    db = _memory_db()
    user_id = _book(db)
    by_isin = {p.isin: p for p in combined_positions(db, user_id)}

    world = by_isin[MSCI_WORLD]
    assert world.quantity == Decimal("101")
    assert world.value == Decimal("10100")
    assert world.ticker == "EUNL.DE"  # DKB's listing, Scalable has none yet
    assert world.sources == ("dkb", "scalable")
    assert world.avg_buy_price is not None
    assert abs(world.avg_buy_price - Decimal(100 * 70 + 95) / 101) < Decimal("0.0001")
    assert by_isin[ALL_WORLD].sources == ("scalable",)


def test_synced_cash_counts_both_banks_once_and_skips_depots():
    from app.foundation.live_positions import synced_cash

    db = _memory_db()
    user_id = _book(db)
    total, has_accounts = synced_cash(db, user_id)
    assert has_accounts is True
    assert total == Decimal("500") + Decimal("42.10") + Decimal("812.40")

    assert synced_cash(db, str(uuid4())) == (Decimal("0"), False)


def test_set_position_ticker_writes_the_broker_row():
    from app.foundation.live_positions import live_positions, set_position_ticker

    db = _memory_db()
    user_id = _book(db)
    scalable_world = next(p for p in live_positions(db, user_id) if p.source == "scalable" and p.isin == MSCI_WORLD)
    set_position_ticker(db, scalable_world, "EUNL.DE")
    db.commit()
    row = db.get(BrokerPosition, scalable_world.id)
    assert row is not None and row.ticker == "EUNL.DE"


def test_quant_lab_holdings_summary_includes_scalable():
    from app.foundation.portfolio.bridge import get_real_holdings_summary

    db = _memory_db()
    user_id = _book(db)
    summary = get_real_holdings_summary(db, user_id)
    assert summary["position_count"] == 3
    assert summary["total_value"] == 10120.0
    assert {p["broker"] for p in summary["positions"]} == {"DKB", "Scalable Capital"}
    assert summary["by_ticker"][ALL_WORLD]["current_value"] == 20.0


def test_quant_lab_holdings_summary_lists_depots_per_isin_and_totals_per_broker():
    from app.foundation.portfolio.bridge import get_real_holdings_summary

    db = _memory_db()
    user_id = _book(db)
    summary = get_real_holdings_summary(db, user_id)

    # The MSCI World at both brokers is one ticker row with both depots under it.
    world = summary["by_ticker"][MSCI_WORLD]
    assert world["ticker"] == "EUNL.DE"
    assert world["current_value"] == 10100.0
    assert [(d["source"], d["broker"], d["quantity"], d["current_value"]) for d in world["depots"]] == [
        ("dkb", "DKB", 100.0, 10000.0),
        ("scalable", "Scalable Capital", 1.0, 100.0),
    ]
    assert all(d["account_id"] for d in world["depots"])

    dkb = summary["by_broker"]["dkb"]
    assert dkb["broker"] == "DKB"
    assert dkb["total_value"] == 10000.0
    assert dkb["position_count"] == 1
    assert dkb["cost_basis"] == 7000.0
    assert dkb["unrealized_pnl"] == 3000.0
    assert abs(dkb["unrealized_pnl_pct"] - 3000 / 7000) < 1e-9

    sc = summary["by_broker"]["scalable"]
    assert sc["broker"] == "Scalable Capital"
    assert sc["total_value"] == 120.0
    assert sc["position_count"] == 2
    assert sc["cost_basis"] == 115.0
    assert sc["unrealized_pnl"] == 5.0
    assert abs(dkb["weight"] + sc["weight"] - 1.0) < 1e-9


def test_quant_lab_broker_pnl_counts_only_positions_with_a_cost():
    from app.foundation.portfolio.bridge import get_real_holdings_summary

    db = _memory_db()
    user_id = _book(db)
    depot = db.query(ConnectedAccount).filter_by(user_id=user_id, account_type="depot").one()
    # A transferred-in position Scalable knows no cost for, and one it reports
    # a value but no price for.
    db.add_all([
        BrokerPosition(
            user_id=user_id, connected_account_id=depot.id, source="scalable", isin="US67066G1040",
            ticker="NVD.DE", name="NVIDIA", security_type="STOCK", quantity=Decimal("2"),
            avg_buy_price=None, current_price=Decimal("150"), current_value=Decimal("300"),
        ),
        BrokerPosition(
            user_id=user_id, connected_account_id=depot.id, source="scalable", isin="NL0010273215",
            ticker="ASML.AS", name="ASML", security_type="STOCK", quantity=Decimal("1"),
            avg_buy_price=Decimal("600"), current_price=None, current_value=Decimal("650"),
        ),
    ])
    db.commit()
    sc = get_real_holdings_summary(db, user_id)["by_broker"]["scalable"]
    assert sc["total_value"] == 1070.0
    assert sc["position_count"] == 4
    assert sc["cost_basis"] == 715.0
    assert sc["costed_value"] == 770.0
    assert sc["unrealized_pnl"] == 55.0

    asml = next(p for p in get_real_holdings_summary(db, user_id)["positions"] if p["isin"] == "NL0010273215")
    assert asml["unrealized_pnl"] == 50.0
    assert asml["cost_basis"] == 600.0


def test_wealth_positions_carry_broker_depot_and_asset_type():
    from app.foundation.portfolio_service import _wealth_cache, wealth_summary

    db = _memory_db()
    user_id = _book(db)
    _wealth_cache.pop(user_id, None)
    with patch("app.foundation.portfolio_service.sync_dkb_to_wealth_ledger"):
        summary = wealth_summary(db, user_id)
    synced = {(p["source"], p["isin"]): p for p in summary["positions"] if p["source"] in ("dkb", "scalable")}
    assert synced[("dkb", MSCI_WORLD)]["broker"] == "DKB"
    assert synced[("scalable", MSCI_WORLD)]["broker"] == "Scalable Capital"
    # The bridged holding's asset type, for both depots holding the ISIN.
    assert synced[("dkb", MSCI_WORLD)]["asset_type"] == "etf"
    assert synced[("scalable", MSCI_WORLD)]["asset_type"] == "etf"
    # No holding was bridged for the All-World ETF here.
    assert synced[("scalable", ALL_WORLD)]["asset_type"] is None
    assert synced[("dkb", MSCI_WORLD)]["account_id"] != synced[("scalable", MSCI_WORLD)]["account_id"]


def test_wealth_splits_the_combined_book_by_broker():
    from app.foundation.portfolio_service import _wealth_cache, wealth_summary

    db = _memory_db()
    user_id = _book(db)
    _wealth_cache.pop(user_id, None)
    with patch("app.foundation.portfolio_service.sync_dkb_to_wealth_ledger"):
        summary = wealth_summary(db, user_id)
    split = {row["source"]: row for row in summary["by_broker"]}
    assert split["dkb"]["securities"] == 10000.0 and split["dkb"]["cash"] == 500.0
    assert split["scalable"]["securities"] == 120.0
    assert split["scalable"]["cash"] == pytest.approx(42.10 + 812.40)
    assert sum(row["total"] for row in summary["by_broker"]) == pytest.approx(summary["total_value"])


def test_advisor_real_book_includes_scalable():
    from app.decision.advisor.recommendations import _real_book

    db = _memory_db()
    user_id = _book(db)
    book = _real_book(db, user_id)
    assert book["VWCE.DE"]["value"] == 20.0
    # One instrument, one row: Scalable's lot has no ticker yet but shares the ISIN.
    assert book["EUNL.DE"]["value"] == 10100.0
    assert MSCI_WORLD not in book


def test_price_matrix_weights_each_depot_once_at_market_value():
    from app.foundation.portfolio_price_service import PortfolioPriceService

    db = _memory_db()
    user_id = _book(db)
    today = date.today()
    bars = [{"date": today - timedelta(days=4 - i), "close": 100.0 + i} for i in range(5)]
    with (
        patch("app.foundation.portfolio_price_service.market_service") as market,
        patch("app.foundation.portfolio.isin_resolver._try_resolve_isin", return_value="EUNL.DE"),
    ):
        market.history.side_effect = lambda _db, ticker, days=730: bars
        matrix, weights, diagnostics = PortfolioPriceService(db, user_id).price_matrix()

    assert set(matrix) == {"EUNL.DE", "VWCE.DE"}
    # 10,000 at DKB + 100 at Scalable; the synced holding copy (cost 7,000) is
    # not added on top.
    assert weights["EUNL.DE"] == 10100.0
    assert weights["VWCE.DE"] == 20.0
    assert diagnostics["manual_ticker_holdings"] == 0
    assert diagnostics["dkb_position_count"] == 3
    # The resolved listing is stored on Scalable's row.
    assert db.query(BrokerPosition).filter_by(isin=MSCI_WORLD).one().ticker == "EUNL.DE"


def test_divergence_counts_real_positions_once_across_brokers():
    from app.decision.llm_portfolio.divergence import compute_divergence

    db = _memory_db()
    user_id = _book(db)
    paper = PaperPortfolio(user_id=user_id, name="Paper", initial_cash=Decimal("0"))
    db.add(paper)
    db.commit()
    result = compute_divergence(db, user_id, paper.id)
    missing = {row["isin"]: row for row in result["missing_in_paper"]}
    assert Decimal(missing[MSCI_WORLD]["quantity"]) == Decimal("101")  # not 201
    assert Decimal(missing[ALL_WORLD]["quantity"]) == Decimal("0.2")


def _eur_listings(db) -> None:
    from app.foundation.models.entities import ListingCurrency

    for symbol in ("EUNL.DE", "VWCE.DE"):
        db.add(ListingCurrency(symbol=symbol, currency="EUR", source="test"))
    db.commit()


def test_paper_seed_takes_both_brokers_one_row_per_isin():
    from app.decision.paper_portfolio import _seed_holdings

    db = _memory_db()
    user_id = _book(db)
    _eur_listings(db)
    paper = PaperPortfolio(user_id=user_id, name="Paper", initial_cash=Decimal("0"))
    db.add(paper)
    db.commit()
    with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 110.0}):
        _seed_holdings(db, paper.id, user_id)
    rows = {h.isin: h for h in db.query(PaperHolding).filter_by(portfolio_id=paper.id)}
    assert set(rows) == {MSCI_WORLD, ALL_WORLD}
    assert rows[MSCI_WORLD].quantity == Decimal("101")
    # Seeded at today's price, not at either broker's cost: the run starts at 0.
    assert rows[MSCI_WORLD].avg_buy_price == Decimal("110")


def test_news_symbols_include_scalable_positions():
    from app.foundation.news import portfolio_symbols

    db = _memory_db()
    user_id = _book(db)
    with patch("app.foundation.news._resolve_isin_to_ticker", return_value="EUNL.DE"):
        symbols = portfolio_symbols(db, user_id, resolve_isins=True)
    assert "VWCE.DE" in symbols and "EUNL.DE" in symbols


def test_discover_never_suggests_a_scalable_holding():
    from app.decision.discover.universe import _user_holdings_tickers

    db = _memory_db()
    user_id = _book(db)
    held = _user_holdings_tickers(db, user_id)
    assert {"VWCE.DE", ALL_WORLD, MSCI_WORLD} <= held


def _manual_twin(db, user_id: str, isin: str, ticker: str | None, avg: str | None = "100") -> None:
    """A hand-entered holding of an ISIN Scalable also reports, not yet reviewed."""
    portfolio = db.query(Portfolio).filter_by(user_id=user_id).one()
    db.add(Holding(
        portfolio_id=portfolio.id, isin=isin, ticker=ticker, name="entered by hand", asset_type="etf",
        quantity=Decimal("0.2"), avg_buy_price=Decimal(avg) if avg is not None else None,
        currency="EUR", source="manual",
    ))
    db.commit()


def test_readers_without_manual_holdings_keep_an_unreconciled_broker_position():
    from app.decision.advisor.recommendations import _real_book
    from app.decision.paper_portfolio import _seed_holdings
    from app.foundation.news import portfolio_symbols

    db = _memory_db()
    user_id = _book(db)
    _manual_twin(db, user_id, ALL_WORLD, ticker=None)

    assert _real_book(db, user_id)["VWCE.DE"]["value"] == 20.0
    assert "VWCE.DE" in portfolio_symbols(db, user_id)
    paper = PaperPortfolio(user_id=user_id, name="Paper", initial_cash=Decimal("0"))
    db.add(paper)
    db.commit()
    _seed_holdings(db, paper.id, user_id)
    assert db.query(PaperHolding).filter_by(portfolio_id=paper.id, isin=ALL_WORLD).count() == 1


def test_paper_seed_without_a_quote_falls_back_to_the_known_cost():
    from app.decision.paper_portfolio import _seed_holdings

    db = _memory_db()
    user_id = _book(db)
    _eur_listings(db)
    row = db.query(BrokerPosition).filter_by(isin=MSCI_WORLD).one()
    row.avg_buy_price = None  # Scalable reported no cost for its unit
    paper = PaperPortfolio(user_id=user_id, name="Paper", initial_cash=Decimal("0"))
    db.add(paper)
    db.commit()
    with patch("app.decision.paper_portfolio.market_quote", return_value={"price": None}):
        _seed_holdings(db, paper.id, user_id)
    world = db.query(PaperHolding).filter_by(portfolio_id=paper.id, isin=MSCI_WORLD).one()
    assert world.quantity == Decimal("101") and world.avg_buy_price > 0


def test_a_manual_holding_without_cost_does_not_hide_the_synced_position_from_the_matrix():
    from app.foundation.portfolio_price_service import PortfolioPriceService

    db = _memory_db()
    user_id = _book(db)
    _manual_twin(db, user_id, "IE00B3RBWM25", ticker="EUNL.DE", avg=None)
    today = date.today()
    bars = [{"date": today - timedelta(days=4 - i), "close": 100.0 + i} for i in range(5)]
    with (
        patch("app.foundation.portfolio_price_service.market_service") as market,
        patch("app.foundation.portfolio.isin_resolver._try_resolve_isin", return_value="EUNL.DE"),
    ):
        market.history.side_effect = lambda _db, ticker, days=730: bars
        matrix, weights, diagnostics = PortfolioPriceService(db, user_id).price_matrix()

    # The synced 10,100 € plus the 0.2 units held by hand at the last close.
    assert "EUNL.DE" in matrix and weights["EUNL.DE"] == pytest.approx(10100.0 + 0.2 * 104.0)
    assert "EUNL.DE" not in diagnostics["missing_history"]


def test_a_zero_value_with_a_price_is_valued_at_price_times_quantity():
    from app.decision.advisor.recommendations import _real_book

    db = _memory_db()
    user_id = _book(db)
    row = db.query(DkbPosition).filter_by(isin=MSCI_WORLD).one()
    row.current_value = Decimal("0")  # the bank sent a price but no value
    db.commit()
    assert _real_book(db, user_id)["EUNL.DE"]["value"] == 10100.0


def test_a_second_depot_without_a_price_is_weighted_at_cost():
    from app.foundation.portfolio_price_service import PortfolioPriceService

    db = _memory_db()
    user_id = _book(db)
    row = db.query(BrokerPosition).filter_by(isin=MSCI_WORLD).one()
    row.ticker, row.current_value, row.current_price = "EUNL.DE", None, None
    db.commit()
    today = date.today()
    bars = [{"date": today - timedelta(days=4 - i), "close": 100.0 + i} for i in range(5)]
    with patch("app.foundation.portfolio_price_service.market_service") as market:
        market.history.side_effect = lambda _db, ticker, days=730: bars
        _, weights, _ = PortfolioPriceService(db, user_id).price_matrix()
    assert weights["EUNL.DE"] == 10000.0 + 95.0
