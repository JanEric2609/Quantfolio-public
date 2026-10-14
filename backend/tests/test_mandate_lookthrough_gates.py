"""Mandates: look-through concentration, sell gates, cash deployment (plan 2026-10-12, D6 / WP3)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from conftest import _memory_db

from app.decision.llm_portfolio import review as review_mod
from app.decision.llm_portfolio.assessment import compute_assessment
from app.decision.llm_portfolio.gates import check_sell_gates
from app.decision.paper_portfolio import execute_trade
from app.foundation.models.entities import ListingCurrency, PaperHolding, PaperPortfolio, PaperTrade, User

EUNL = {"isin": "IE00B4L5Y983", "ticker": "EUNL.DE", "name": "iShares Core MSCI World UCITS ETF USD (Acc)",
        "asset_type": "etf"}


def _assess(positions, cash, *, cap=0.20, etf_holdings=None):
    return compute_assessment(
        positions=positions, cash_balance=cash, max_drawdown=None, total_return_pct=None,
        benchmark_excess=None, max_single_position_pct=cap, min_etf_pct=0.30, etf_holdings=etf_holdings,
    )


def _check(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


def test_index_fund_at_94_percent_is_no_breach():
    result = _assess([{**EUNL, "quantity": 94, "price": 100.0}], 600.0)
    conc = _check(result, "concentration")
    assert conc["status"] == "ok"
    assert conc["basis"] == "look_through"
    assert conc["exempt_diversified"] == ["EUNL.DE"]
    assert "Concentration breach" not in result["summary_line"]


def test_narrow_fund_is_still_one_position():
    semis = {"isin": "IE00BMC38736", "ticker": "SMH", "name": "VanEck Semiconductor UCITS ETF", "asset_type": "etf"}
    conc = _check(_assess([{**semis, "quantity": 50, "price": 100.0}], 5_000.0), "concentration")
    assert conc["status"] == "breach"
    assert conc["breaches"][0]["ticker"] == "SMH"


def test_issuer_exposure_adds_direct_and_inside_the_fund():
    positions = [
        {**EUNL, "quantity": 94, "price": 100.0},
        {"isin": "US67066G1040", "ticker": "NVDA", "name": "NVIDIA", "asset_type": "stock",
         "quantity": 3, "price": 100.0},
    ]
    result = _assess(positions, 300.0, cap=0.05,
                     etf_holdings={"EUNL.DE": [{"ticker": "NVDA", "name": "NVIDIA", "weight": 0.05}]})
    conc = _check(result, "concentration")
    nvda = next(r for r in conc["look_through"] if r["ticker"] == "NVDA")
    assert nvda["direct_pct"] == pytest.approx(3.0)
    assert nvda["via_etf_pct"] == pytest.approx(0.94 * 5.0)
    assert nvda["weight_pct"] == pytest.approx(7.7, abs=0.01)
    assert conc["status"] == "breach"
    assert "NVDA 7.7% > 5.0% cap" in result["summary_line"]


def test_cash_above_ten_percent_asks_to_deploy_the_core():
    dc = _check(_assess([{**EUNL, "quantity": 6, "price": 100.0}], 9_400.0), "deploy_cash")
    assert dc["status"] == "action"
    assert dc["core_etf"] == "EUNL.DE"
    assert "EUNL.DE" in dc["instruction"]


def _book(etf_value: float, stock_value: float) -> dict[str, tuple[float, str | None]]:
    return {"EUNL.DE": (etf_value, "etf"), "DELL": (stock_value, "stock")}


def test_sell_that_leaves_etf_weight_at_5_percent_is_rejected():
    gate = check_sell_gates(
        ticker="EUNL.DE", quantity=Decimal("1"), price=Decimal("100"), book=_book(600.0, 9_400.0),
        cash=0.0, mandate_config={"min_etf_pct": 0.30}, cumulative_totals={},
    )
    assert gate["passed"] is False
    assert "ETF_FLOOR" in gate["reason"]


def test_sell_is_clipped_to_a_third_of_the_position():
    gate = check_sell_gates(
        ticker="DELL", quantity=Decimal("90"), price=Decimal("100"), book=_book(5_000.0, 9_000.0),
        cash=0.0, mandate_config={"min_etf_pct": 0.30}, cumulative_totals={},
    )
    assert gate["passed"] is True
    assert gate["quantity"] == Decimal("30")
    assert "clipped" in gate["reason"]


def test_sell_respects_the_cumulative_cap():
    # 2,000 of DELL already sold in the window: a third of the 11,000 start is 3,667.
    gate = check_sell_gates(
        ticker="DELL", quantity=Decimal("30"), price=Decimal("100"), book=_book(5_000.0, 9_000.0),
        cash=0.0, mandate_config={}, cumulative_totals={"DELL": (2_000.0, 0.0)},
    )
    assert gate["passed"] is True
    assert float(gate["quantity"]) * 100 == pytest.approx(11_000 / 3 - 2_000, abs=0.01)


def test_mandate_b_sell_of_250_of_265_eunl_is_clipped():
    # The 2026-10-09 trade: 265 EUNL at ~131.6, "94 % > 20 % cap".
    gate = check_sell_gates(
        ticker="EUNL.DE", quantity=Decimal("250"), price=Decimal("131.64"),
        book={"EUNL.DE": (265 * 131.64, "etf")}, cash=2_200.0, mandate_config={"min_etf_pct": 0.30},
        cumulative_totals={},
    )
    assert gate["passed"] is True
    assert gate["quantity"] <= Decimal("88.34")


def test_hold_with_94_percent_cash_buys_the_core_up_to_the_floor(monkeypatch):
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    p = PaperPortfolio(user_id=user.id, name="B", initial_cash=Decimal("10000"), managed_by="llm",
                       mandate="B", mandate_config_json=json.dumps({"min_etf_pct": 0.30}))
    db.add(p)
    db.add(ListingCurrency(symbol="EUNL.DE", currency="EUR", source="test"))
    db.commit()
    execute_trade(db, p.id, "EUNL.DE", "buy", 6, 100.0)
    db.query(PaperTrade).update({PaperTrade.date: datetime.now(UTC) - timedelta(days=40)})
    db.query(PaperHolding).update({PaperHolding.asset_type: "etf", PaperHolding.name: EUNL["name"]})
    db.commit()

    hold = json.dumps({"decision": {"action": "hold", "ticker": "", "quantity": 0, "thesis": "wait",
                                    "confidence": 0.5, "key_risks": [], "alternatives_considered": [],
                                    "expectation": {}}})
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: hold)
    passing = {"passed": True, "checks": [], "reason": "All gates passed"}
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 100.0}), \
            patch("app.decision.llm_portfolio.gates.check_buy_gates", return_value=passing):
        res = review_mod.run_mandate_review(db, user.id, "B")

    assert res["trade_created"] is True
    assert res["decision"]["action"] == "hold"
    assert res["gate_result"]["deploy_cash"]["ticker"] == "EUNL.DE"
    held = db.query(PaperHolding).filter(PaperHolding.ticker == "EUNL.DE").one()
    # ETF weight goes from 6 % to the 30 % floor of a 10,000 book.
    assert float(held.quantity) * 100 == pytest.approx(3_000, abs=1.0)
