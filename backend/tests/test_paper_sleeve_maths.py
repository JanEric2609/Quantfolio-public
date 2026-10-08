"""Paper-sleeve maths: sell-cap ledger, execution price, fees, net dividends."""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
from conftest import _memory_db

from app.decision.advisor import cycle as cycle_mod
from app.decision.advisor.cycle import (
    _record_sale,
    _resolve_execution_prices,
    run_advisor_cycle,
)
from app.decision.paper_portfolio import (
    credit_dividends,
    current_cash_balance,
    execute_trade,
)
from app.foundation.models.entities import (
    ListingCurrency,
    PaperCashFlow,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.foundation.quant_proposal import McSummary, TradeProposal
from app.foundation.withholding_tax import withholding_rate
from test_advisor_trade_flow import (
    _seed_discover_run,
    _seed_fully_invested_book,
    _seed_prices,
    _stub_llm,
    _user,
    _wiggly,
)

QUOTE = "app.decision.paper_portfolio.market_quote"


# --- withholding table -------------------------------------------------------

def test_withholding_rates_by_isin_prefix():
    assert withholding_rate("US0378331005") == 0.15
    assert withholding_rate("CH0012005267") == 0.35
    assert withholding_rate("FR0000120271") == 0.128
    assert withholding_rate("IE00B4L5Y983", is_fund=True) == 0.0
    assert withholding_rate("IE00B4L5Y983") == 0.25  # an Irish equity: 25 % DWT
    assert withholding_rate("DE0007164600") == 0.0
    assert withholding_rate("JP3633400001") == 0.15315
    assert withholding_rate("LU0274208692", is_fund=True) == 0.0  # a Luxembourg fund
    assert withholding_rate("LU1598757687") == 0.15  # a Luxembourg share
    assert withholding_rate("TW0002330008") == 0.21
    assert withholding_rate("KR7005930003") == 0.22
    assert withholding_rate("ZZ0000000000") == 0.15
    assert withholding_rate(None) == 0.15
    assert withholding_rate("") == 0.15


def test_looks_like_fund_by_type_or_name():
    from app.foundation.withholding_tax import looks_like_fund

    assert looks_like_fund("iShares Core MSCI World UCITS ETF")
    assert looks_like_fund("Something", "etf")
    assert not looks_like_fund("Accenture plc", "stock")
    assert not looks_like_fund(None)


def _paper(db, user_id, cash="1000"):
    p = PaperPortfolio(user_id=user_id, name="t", initial_cash=Decimal(cash), managed_by="user",
                       inception_at=datetime.now(UTC) - timedelta(days=30))
    db.add(p)
    db.commit()
    return p


def _credit(db, ticker, isin, name="X", asset_type="stock"):
    p = _paper(db, _user(db).id)
    db.add(ListingCurrency(symbol=ticker, currency="EUR", source="test"))
    db.add(PaperHolding(portfolio_id=p.id, ticker=ticker, isin=isin, name=name, asset_type=asset_type,
                        quantity=Decimal("10"), avg_buy_price=Decimal("20"), currency="EUR"))
    db.commit()
    ex = date.today() - timedelta(days=5)
    with patch("app.foundation.market.dividend_history",
               return_value=[{"ex_date": ex.isoformat(), "amount": 1.0}]):
        out = credit_dividends(db, p.id)
    return p, out


def test_us_dividend_is_credited_net_of_15_percent():
    db = _memory_db()
    p, out = _credit(db, "USA", "US0378331005")
    assert out[0]["amount_eur"] == 8.5
    row = db.query(PaperCashFlow).one()
    assert row.amount_eur == Decimal("8.50") and row.amount_per_unit == Decimal("1")


def test_swiss_dividend_loses_35_percent_and_irish_fund_nothing():
    db = _memory_db()
    assert _credit(db, "SWI", "CH0012005267")[1][0]["amount_eur"] == 6.5
    db2 = _memory_db()
    assert _credit(db2, "FUND", "IE00B4L5Y983", name="iShares UCITS ETF", asset_type="etf")[1][0]["amount_eur"] == 10.0
    db3 = _memory_db()
    assert _credit(db3, "ACN", "IE00B4BNMY34", name="Accenture plc")[1][0]["amount_eur"] == 7.5


def test_unknown_isin_uses_the_default_rate():
    db = _memory_db()
    assert _credit(db, "UNK", None)[1][0]["amount_eur"] == 8.5


# --- fees: sleeves A/B go through execute_trade ------------------------------

def test_mandate_sell_is_booked_with_fee_and_needs_a_quote(monkeypatch):
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)
    p = PaperPortfolio(user_id=user.id, name="A", initial_cash=Decimal("1000"), managed_by="llm",
                       mandate="A", mandate_config_json="{}")
    db.add(p)
    db.commit()
    db.add(ListingCurrency(symbol="SAP", currency="EUR", source="test"))
    db.commit()
    execute_trade(db, p.id, "SAP", "buy", 5, 100.0, fee=0.99)
    cash_before = current_cash_balance(p, db)

    sell = json.dumps({"decision": {"action": "sell", "ticker": "SAP", "quantity": 2, "thesis": "t",
                                    "confidence": 0.5, "key_risks": [], "alternatives_considered": [],
                                    "expectation": {}}})
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: sell)

    # No quote: the sell must be skipped, never booked at the average buy price.
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": None}):
        res = review_mod.run_mandate_review(db, user.id, "A")
    assert res["trade_created"] is False
    assert "no current EUR price" in res["gate_result"]["reason"]
    assert current_cash_balance(p, db) == cash_before

    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 110.0}):
        review_mod.run_mandate_review(db, user.id, "A")
    db.expire_all()
    assert current_cash_balance(p, db) == cash_before + Decimal("220") - Decimal("0.99")


# --- execution price ---------------------------------------------------------

def _proposal(spot=100.0, last_bar=None):
    series = {}
    if last_bar is not None:
        series["AAA"] = pd.Series([spot, spot], index=pd.to_datetime([last_bar - timedelta(days=1), last_bar]))
    return TradeProposal(
        portfolio_id="p", suggested_weights={}, mc_summaries={"AAA": McSummary("AAA", "equity", 0, 0, 0, 0.5, spot)},
        risk_envelope={}, current_book={}, optimizer_status="x", price_series=series,
    )


def test_fills_use_the_valuation_quote():
    db = _memory_db()
    with patch("app.decision.advisor.cycle.paper_quote_eur", return_value={"price": 123.0}):
        assert _resolve_execution_prices(db, _proposal(100.0), {"AAA"}) == {"AAA": 123.0}


def test_no_quote_falls_back_to_a_fresh_bar_only():
    db = _memory_db()
    none = {"price": None}
    today = datetime.now(UTC).date()
    with patch("app.decision.advisor.cycle.paper_quote_eur", return_value=none):
        fresh = _resolve_execution_prices(db, _proposal(100.0, today - timedelta(days=1)), {"AAA"})
        old = _resolve_execution_prices(db, _proposal(100.0, today - timedelta(days=14)), {"AAA"})
        unknown = _resolve_execution_prices(db, _proposal(100.0), {"AAA"})
    assert fresh["AAA"] == 100.0
    assert old["AAA"] is None and unknown["AAA"] is None


def test_plan_without_a_current_price_is_skipped():
    from app.decision.advisor.cycle import _plan_decision
    from app.decision.advisor.llm_decision import TradeDecision

    d = TradeDecision(ticker="AAA", action="buy", target_weight=0.5, thesis="t", confidence=0.9)
    planned, reason = _plan_decision(d, _proposal(), 10_000.0, 10.0, prices={"AAA": None})
    assert planned is None and reason == "no current price"


# --- sell-cap ledger ---------------------------------------------------------

def test_record_sale_decrements_cap_and_book():
    proposal = _proposal()
    proposal.current_book = {"X": 1_000.0}
    caps = {"X": 300.0}
    _record_sale(proposal, caps, "X", 200.0)
    assert caps["X"] == 100.0 and proposal.current_book["X"] == 800.0
    _record_sale(proposal, caps, "X", 500.0)
    assert caps["X"] == 0.0


def test_model_sell_plus_funding_shortfall_never_exceeds_the_cap():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        config={"max_turnover_pct": 0.9, "min_ticket_pct": 0.001},
        llm_call=_stub_llm([
            {"ticker": "HELD", "action": "sell", "target_weight": 0.0, "thesis": "Exit.", "confidence": 0.9},
            # Bigger than the capped sell can fund: a funding raise follows.
            {"ticker": "CAND", "action": "buy", "target_weight": 0.5, "thesis": "In.", "confidence": 0.8},
        ]),
    )
    sold = sum(t["value"] for t in result["trades"] if t["ticker"] == "HELD" and t["side"] == "sell")
    assert sold > 0
    assert sold <= 40_000.0 / 3.0 + 0.01, sold


# --- stale quotes ------------------------------------------------------------

def _price_row(db, ticker, day, close=100.0):
    from uuid import uuid4

    from app.foundation.models.entities import PriceCache

    db.add(PriceCache(id=uuid4().hex, ticker=ticker, date=day, close=Decimal(str(close)),
                      fetched_at=datetime.now(UTC), source="test", stale=True, currency="EUR"))
    db.commit()


def test_a_quote_older_than_five_trading_days_is_stale_but_still_values_the_holding():
    from app.decision.paper_portfolio import get_holdings, get_summary, paper_quote_eur

    db = _memory_db()
    p = _paper(db, _user(db).id)
    db.add(ListingCurrency(symbol="OLD", currency="EUR", source="test"))
    db.add(PaperHolding(portfolio_id=p.id, ticker="OLD", name="Old", quantity=Decimal("2"),
                        avg_buy_price=Decimal("80"), currency="EUR"))
    db.commit()
    _price_row(db, "OLD", date.today() - timedelta(days=20))
    stale_quote = {"price": 100.0, "stale": True, "currency": "EUR"}
    with patch(QUOTE, return_value=stale_quote):
        q = paper_quote_eur(db, "OLD")
        row = get_holdings(db, p.id)[0]
        summary = get_summary(db, p.id)
    assert q["stale"] is True and q["price"] == 100.0
    assert row["stale"] is True and row["priced"] is False and row["market_value"] == 200.0
    assert summary["stale_quotes"] == ["OLD"]


def test_a_recent_cached_quote_is_not_stale():
    from app.decision.paper_portfolio import paper_quote_eur

    db = _memory_db()
    db.add(ListingCurrency(symbol="NEW", currency="EUR", source="test"))
    db.commit()
    _price_row(db, "NEW", date.today())
    with patch(QUOTE, return_value={"price": 100.0, "stale": True}):
        assert paper_quote_eur(db, "NEW")["stale"] is False


def test_no_fill_at_a_stale_quote():
    db = _memory_db()
    with patch("app.decision.advisor.cycle.paper_quote_eur", return_value={"price": 100.0, "stale": True}):
        prices = _resolve_execution_prices(db, _proposal(100.0), {"AAA"})
    assert prices["AAA"] is None


# --- dividends: pence, inception day, idempotence ---------------------------

def _divs(db, p, rows):
    with patch("app.foundation.market.dividend_history", return_value=rows):
        return credit_dividends(db, p.id)


def test_pence_dividends_are_converted_like_pence_prices():
    db = _memory_db()
    p = _paper(db, _user(db).id)
    db.add(ListingCurrency(symbol="SHEL.L", currency="GBp", source="test"))
    db.add(PaperHolding(portfolio_id=p.id, ticker="SHEL.L", isin="GB00BP6MXD84", name="Shell plc",
                        asset_type="stock", quantity=Decimal("10"), avg_buy_price=Decimal("25"), currency="EUR"))
    db.commit()
    ex = date.today() - timedelta(days=3)
    with patch("app.foundation.eur_prices.eur_per_unit_by_date", return_value={ex.isoformat(): 1.2}):
        out = _divs(db, p, [{"ex_date": ex.isoformat(), "amount": 50.0}])  # 50 pence per share
    assert out[0]["amount_eur"] == 6.0  # 50p = 0.50 GBP * 1.2 EUR * 10 shares; GB has no source tax


def test_an_ex_date_on_the_inception_day_is_not_credited():
    db = _memory_db()
    p = _paper(db, _user(db).id)
    db.add(ListingCurrency(symbol="DIV", currency="EUR", source="test"))
    db.add(PaperHolding(portfolio_id=p.id, ticker="DIV", isin="DE0000000001", name="Div AG",
                        quantity=Decimal("10"), avg_buy_price=Decimal("20"), currency="EUR"))
    p.inception_at = datetime.now(UTC) - timedelta(days=10)
    db.commit()
    inc = p.inception_at.date()
    later = inc + timedelta(days=1)
    out = _divs(db, p, [{"ex_date": inc.isoformat(), "amount": 1.0}, {"ex_date": later.isoformat(), "amount": 1.0}])
    assert [o["ex_date"] for o in out] == [later.isoformat()]


def test_a_dividend_is_never_credited_twice():
    db = _memory_db()
    p = _paper(db, _user(db).id)
    db.add(ListingCurrency(symbol="DIV", currency="EUR", source="test"))
    db.add(PaperHolding(portfolio_id=p.id, ticker="DIV", isin="DE0000000001", name="Div AG",
                        quantity=Decimal("10"), avg_buy_price=Decimal("20"), currency="EUR"))
    db.commit()
    ex = date.today() - timedelta(days=3)
    row = {"ex_date": ex.isoformat(), "amount": 1.0}
    assert len(_divs(db, p, [row, row])) == 1
    assert _divs(db, p, [row]) == []
    assert db.query(PaperCashFlow).count() == 1


# --- benchmark base and end --------------------------------------------------

def test_benchmark_uses_the_seed_quote_and_the_live_quote():
    from app.decision.paper_portfolio import passive_benchmark

    db = _memory_db()
    p = _paper(db, _user(db).id)
    p.benchmark_base_price = Decimal("100")
    db.commit()
    start = p.inception_at.date()
    closes = {start.isoformat(): 90.0, date.today().isoformat(): 95.0}  # closes differ from the quotes
    with patch("app.foundation.eur_prices.eur_closes", return_value=closes), \
            patch("app.foundation.eur_prices.benchmark_ticker", return_value="EUNL.DE"), \
            patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 110.0}):
        b = passive_benchmark(db, p, Decimal("1000"))
    assert b["base_source"] == "seed_quote"
    assert abs(b["total_return_pct"] - 0.10) < 1e-12 and abs(b["value"] - 1100.0) < 1e-9


def test_benchmark_end_falls_back_to_the_last_close_when_the_quote_is_stale():
    from app.decision.paper_portfolio import passive_benchmark

    db = _memory_db()
    p = _paper(db, _user(db).id)
    p.benchmark_base_price = Decimal("100")
    db.commit()
    closes = {date.today().isoformat(): 105.0}
    with patch("app.foundation.eur_prices.eur_closes", return_value=closes), \
            patch("app.foundation.eur_prices.benchmark_ticker", return_value="EUNL.DE"), \
            patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 999.0, "stale": True}):
        b = passive_benchmark(db, p, Decimal("1000"))
    assert abs(b["total_return_pct"] - 0.05) < 1e-12


def test_reset_stamps_the_benchmark_base_and_archives_the_final_nav():
    from app.decision.paper_portfolio import reset_portfolio

    db = _memory_db()
    p = _paper(db, _user(db).id, cash="1000")
    p.baseline_value = Decimal("1000")
    db.commit()
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 77.0}), \
            patch("app.foundation.eur_prices.eur_closes", return_value={}):
        reset_portfolio(db, p.id, reason="test")
    db.refresh(p)
    assert p.benchmark_base_price == Decimal("77") and p.benchmark_base_at is not None
    from app.foundation.models.entities import PaperPortfolioArchive

    payload = json.loads(db.query(PaperPortfolioArchive).one().payload_json)
    assert payload["final"]["total_value"] == 1000.0
    assert "benchmark_return_pct" in payload["final"]


def test_a_failed_reset_changes_nothing():
    from app.decision.paper_portfolio import reset_portfolio
    from app.foundation.models.entities import PaperPortfolioArchive

    db = _memory_db()
    p = _paper(db, _user(db).id, cash="1000")
    db.add(PaperHolding(portfolio_id=p.id, ticker="KEEP", name="Keep", quantity=Decimal("1"),
                        avg_buy_price=Decimal("10"), currency="EUR"))
    db.commit()
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 77.0}), \
            patch("app.decision.paper_portfolio.recompute_baseline_value", side_effect=RuntimeError("boom")):
        try:
            reset_portfolio(db, p.id, reason="test")
        except RuntimeError:
            pass
    assert db.query(PaperHolding).filter_by(portfolio_id=p.id).count() == 1
    assert db.query(PaperPortfolioArchive).count() == 0


# --- snapshot convention -----------------------------------------------------

def test_the_midnight_snapshot_overwrites_the_cycles_row_for_that_date():
    from app.decision.paper_portfolio import snapshot_paper_portfolio
    from app.foundation.models.entities import PaperSnapshot

    db = _memory_db()
    p = _paper(db, _user(db).id, cash="1000")
    today = datetime.now(UTC).date()
    with patch("app.decision.paper_portfolio.credit_dividends"), \
            patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 100.0}):
        snapshot_paper_portfolio(db, p.id)  # 10:00 cycle: today, provisional
        execute_trade(db, p.id, "X", "buy", 1, 100.0, fee=1.0)
        out = snapshot_paper_portfolio(db, p.id, as_of=today)  # next 00:00 job for that date
        prev = snapshot_paper_portfolio(db, p.id, as_of=today - timedelta(days=1))
    rows = {r.date: r for r in db.query(PaperSnapshot).all()}
    assert out["date"] == today.isoformat() and prev["date"] == (today - timedelta(days=1)).isoformat()
    assert len(rows) == 2 and db.query(PaperSnapshot).filter_by(date=today).count() == 1


def _weekday_ago(n):
    day = date.today()
    while n > 0:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def test_a_four_trading_day_old_cached_quote_is_valued_but_flagged_and_never_traded(monkeypatch):
    from app.decision.llm_portfolio import review as review_mod
    from app.decision.paper_portfolio import MAX_QUOTE_AGE_TRADING_DAYS, get_holdings, paper_quote_eur

    assert MAX_QUOTE_AGE_TRADING_DAYS == 3
    db = _memory_db()
    user = _user(db)
    p = PaperPortfolio(user_id=user.id, name="A", initial_cash=Decimal("5000"), managed_by="llm",
                       mandate="A", mandate_config_json="{}")
    db.add(p)
    db.add(ListingCurrency(symbol="OLD4", currency="EUR", source="test"))
    db.commit()
    db.add(PaperHolding(portfolio_id=p.id, ticker="OLD4", name="Old", quantity=Decimal("2"),
                        avg_buy_price=Decimal("80"), currency="EUR"))
    db.commit()
    _price_row(db, "OLD4", _weekday_ago(4))
    # The provider did NOT fail: the quote is a fresh-looking cache hit, but 4 trading days old.
    with patch(QUOTE, return_value={"price": 100.0, "stale": False, "currency": "EUR"}):
        q = paper_quote_eur(db, "OLD4")
        row = get_holdings(db, p.id)[0]
    assert q["stale"] is True and row["stale"] is True and row["priced"] is False
    assert row["market_value"] == 200.0

    # A 3-trading-day-old bar is still tradable.
    _price_row(db, "OLD3", _weekday_ago(3))
    db.add(ListingCurrency(symbol="OLD3", currency="EUR", source="test"))
    db.commit()
    with patch(QUOTE, return_value={"price": 100.0}):
        assert paper_quote_eur(db, "OLD3")["stale"] is False

    # Not traded by the cycle ...
    with patch(QUOTE, return_value={"price": 100.0}):
        assert _resolve_execution_prices(db, _proposal(100.0), {"OLD4"})["OLD4"] is None
    # ... nor by a mandate sell.
    sell = json.dumps({"decision": {"action": "sell", "ticker": "OLD4", "quantity": 1, "thesis": "t",
                                    "confidence": 0.5, "key_risks": [], "alternatives_considered": [],
                                    "expectation": {}}})
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: sell)
    with patch(QUOTE, return_value={"price": 100.0}):
        res = review_mod.run_mandate_review(db, user.id, "A")
    assert res["trade_created"] is False and "no current EUR price" in res["gate_result"]["reason"]
