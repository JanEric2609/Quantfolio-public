"""The paper engine in EUR: valuation, seeding at market, resets that archive, dividends, benchmark."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.paper_portfolio import (
    _current_cash_balance,
    credit_dividends,
    execute_trade,
    get_holdings,
    get_summary,
    passive_benchmark,
    reset_portfolio,
    snapshot_paper_portfolio,
)
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    ListingCurrency,
    PaperCashFlow,
    PaperHolding,
    PaperPortfolio,
    PaperPortfolioArchive,
    PaperSnapshot,
    PaperTrade,
    User,
)

QUOTE = "app.decision.paper_portfolio.market_quote"
RATES = "app.foundation.eur_prices.eur_per_unit_by_date"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _listing(db, symbol: str, currency: str) -> None:
    db.add(ListingCurrency(symbol=symbol, currency=currency, source="test"))
    db.commit()


def _portfolio(db, user_id: str, cash: str = "1000", mandate: str = "manual") -> PaperPortfolio:
    p = PaperPortfolio(
        user_id=user_id, name="P", initial_cash=Decimal(cash), baseline_value=Decimal(cash), mandate=mandate,
        managed_by="manual", inception_at=datetime.now(UTC) - timedelta(days=30),
    )
    db.add(p)
    db.commit()
    return p


def _real_book(db, user_id: str, *, cash: str, isin: str, ticker: str, qty: str, cost: str) -> None:
    db.add(DkbAccount(id=uuid4().hex, user_id=user_id, type="giro", iban="DE" + uuid4().hex[:20],
                      balance=Decimal(cash), currency="EUR"))
    depot = DkbAccount(id=uuid4().hex, user_id=user_id, type="depot", iban="DE" + uuid4().hex[:20],
                       balance=Decimal("0"), currency="EUR")
    db.add(depot)
    db.commit()
    db.add(DkbPosition(id=uuid4().hex, account_id=depot.id, isin=isin, ticker=ticker, name="World",
                       quantity=Decimal(qty), avg_buy_price=Decimal(cost), current_price=Decimal(cost),
                       current_value=Decimal(qty) * Decimal(cost)))
    db.commit()


def test_a_dollar_listing_is_valued_in_euros():
    db = _memory_db()
    p = _portfolio(db, _user(db).id)
    _listing(db, "AAPL", "USD")
    db.add(PaperHolding(portfolio_id=p.id, ticker="AAPL", name="Apple", quantity=Decimal("2"),
                        avg_buy_price=Decimal("80"), currency="EUR"))
    db.commit()
    today = date.today().isoformat()
    with patch(QUOTE, return_value={"price": 100.0, "currency": "USD"}), patch(RATES, return_value={today: 0.9}):
        row = get_holdings(db, p.id)[0]
    assert row["current_price"] == 90.0  # $100 at EUR 0.90 per dollar
    assert row["market_value"] == 180.0 and row["price_local"] == 100.0 and row["priced"] is True


def test_without_a_rate_the_line_is_valued_at_cost_and_flagged():
    db = _memory_db()
    p = _portfolio(db, _user(db).id)
    _listing(db, "AAPL", "USD")
    db.add(PaperHolding(portfolio_id=p.id, ticker="AAPL", name="Apple", quantity=Decimal("2"),
                        avg_buy_price=Decimal("80"), currency="EUR"))
    db.commit()
    with patch(QUOTE, return_value={"price": 100.0}), patch(RATES, return_value=None):
        row = get_holdings(db, p.id)[0]
    assert row["current_price"] == 80.0 and row["priced"] is False


def test_reset_archives_the_run_and_starts_at_market_value():
    db = _memory_db()
    user = _user(db)
    _real_book(db, user.id, cash="500", isin="IE00B4L5Y983", ticker="EUNL.DE", qty="10", cost="50")
    _listing(db, "EUNL.DE", "EUR")
    p = _portfolio(db, user.id, cash="2000")
    db.add(PaperHolding(portfolio_id=p.id, ticker="OLD", name="Old", quantity=Decimal("1"),
                        avg_buy_price=Decimal("10"), currency="EUR"))
    db.add(PaperTrade(portfolio_id=p.id, ticker="OLD", side="buy", quantity=Decimal("1"), price=Decimal("10"),
                      value=Decimal("10"), fee=Decimal("1")))
    db.add(PaperSnapshot(portfolio_id=p.id, date=date.today() - timedelta(days=1), total_value=Decimal("2000"),
                         cash_balance=Decimal("1990"), securities_value=Decimal("10"), total_return_pct=Decimal("0.1")))
    db.commit()
    old_inception = p.inception_at

    with patch(QUOTE, return_value={"price": 100.0}):
        result = reset_portfolio(db, p.id, reason="test")
        summary = get_summary(db, p.id)

    assert result["archived"]["trades"] == 1 and result["archived"]["snapshots"] == 1
    archive = db.query(PaperPortfolioArchive).one()
    payload = json.loads(archive.payload_json)
    assert payload["trades"][0]["ticker"] == "OLD" and payload["portfolio"]["initial_cash"] == "2000.00"
    assert db.query(PaperTrade).count() == 0 and db.query(PaperSnapshot).count() == 0
    held = db.query(PaperHolding).one()
    # Seeded at today's price, not at the broker's EUR 50 cost: the run starts at 0.
    assert held.ticker == "EUNL.DE" and held.avg_buy_price == Decimal("100")
    db.refresh(p)
    assert p.inception_at > old_inception
    assert float(p.baseline_value) == 500 + 10 * 100
    assert abs(summary["total_return_pct"]) < 1e-12


def test_dividends_are_credited_once_for_the_units_held_at_the_ex_date():
    db = _memory_db()
    p = _portfolio(db, _user(db).id, cash="1000")
    _listing(db, "DIST.DE", "EUR")
    db.add(PaperHolding(portfolio_id=p.id, ticker="DIST.DE", isin="IE00B4L5Y983", name="iShares Dist UCITS ETF", quantity=Decimal("10"),
                        avg_buy_price=Decimal("20"), currency="EUR"))
    db.commit()
    ex = date.today() - timedelta(days=5)
    # 4 of the 10 units were bought on the ex-date itself: no dividend for them.
    db.add(PaperTrade(portfolio_id=p.id, ticker="DIST.DE", side="buy", quantity=Decimal("4"), price=Decimal("20"),
                      value=Decimal("80"), fee=Decimal("0"), date=datetime.combine(ex, datetime.min.time(), UTC)))
    db.commit()
    with patch("app.foundation.market.dividend_history", return_value=[{"ex_date": ex.isoformat(), "amount": 0.5}]):
        first = credit_dividends(db, p.id)
        again = credit_dividends(db, p.id)
    assert first == [{"ticker": "DIST.DE", "ex_date": ex.isoformat(), "quantity": 6.0, "amount_eur": 3.0}]
    assert again == []
    assert db.query(PaperCashFlow).count() == 1
    assert _current_cash_balance(p, db) == Decimal("1000") - Decimal("80") + Decimal("3.00")


def test_snapshot_survives_a_dividend_provider_failure():
    db = _memory_db()
    p = _portfolio(db, _user(db).id, cash="1000")
    with patch("app.decision.paper_portfolio.credit_dividends", side_effect=RuntimeError("down")):
        out = snapshot_paper_portfolio(db, p.id)
    assert out["total_value"] == 1000.0


def test_passive_benchmark_is_the_same_start_in_msci_world_eur():
    db = _memory_db()
    p = _portfolio(db, _user(db).id)
    start = p.inception_at.date()
    closes = {(start - timedelta(days=1)).isoformat(): 100.0, start.isoformat(): 100.0,
              date.today().isoformat(): 110.0}
    with patch("app.foundation.eur_prices.eur_closes", return_value=closes), \
            patch("app.foundation.eur_prices.benchmark_ticker", return_value="EUNL.DE"), \
            patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": None}):
        bench = passive_benchmark(db, p, Decimal("1000"), series=True)
    assert bench["available"] and bench["symbol"] == "EUNL.DE"
    assert abs(bench["total_return_pct"] - 0.10) < 1e-12 and abs(bench["value"] - 1100.0) < 1e-9
    assert bench["series"][start.isoformat()] == 1000.0


def test_trades_are_booked_in_euros_and_fees_leave_cash():
    db = _memory_db()
    p = _portfolio(db, _user(db).id, cash="1000")
    execute_trade(db, p.id, "EUNL.DE", "buy", 2, 100.0, fee=1.5)
    assert _current_cash_balance(p, db) == Decimal("798.5")


def test_an_emptied_mandate_sleeve_is_not_reseeded():
    from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio

    db = _memory_db()
    user = _user(db)
    _real_book(db, user.id, cash="500", isin="IE00B4L5Y983", ticker="EUNL.DE", qty="10", cost="50")
    p = _portfolio(db, user.id, cash="900", mandate="A")
    with patch(QUOTE, return_value={"price": 100.0}):
        again = ensure_mandate_portfolio(db, user.id, "A")
    assert again.id == p.id
    assert db.query(PaperHolding).filter(PaperHolding.portfolio_id == p.id).count() == 0


def test_reset_endpoint_needs_confirmation_and_ownership():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import create_app

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    me, other = _user(db), _user(db)
    me_id = me.id
    theirs = _portfolio(db, other.id, mandate="A")
    mine = _portfolio(db, me_id, mandate="A")
    mine_id, theirs_id = mine.id, theirs.id
    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, me_id)
    client = TestClient(app)
    assert client.post(f"/api/paper-portfolio/{theirs_id}/reset", json={"confirm": True}).status_code == 404
    assert client.post(f"/api/paper-portfolio/{mine_id}/reset", json={"confirm": False}).status_code == 422
    body = client.post(f"/api/paper-portfolio/{mine_id}/reset", json={"confirm": True}).json()
    assert body["seeded_from_real_book"] is False and body["baseline_value"] == 100000.0
    archives = client.get(f"/api/paper-portfolio/{mine_id}/archives").json()
    assert len(archives) == 1 and archives[0]["reason"] == "reset"
