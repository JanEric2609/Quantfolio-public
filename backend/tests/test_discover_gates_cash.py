"""Tests for the LLM buy-gate cash-sufficiency selection by mandate."""
from __future__ import annotations

import json
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import PaperPortfolio, User
from app.decision.llm_portfolio.gates import check_buy_gates


def _llm_portfolio(db, user_id: str, mandate: str, cash: str) -> PaperPortfolio:
    p = PaperPortfolio(
        user_id=user_id,
        name=f"Mandate {mandate}",
        initial_cash=Decimal(cash),
        managed_by="llm",
        mandate=mandate,
        mandate_config_json=json.dumps({"style": "x", "max_single_position_pct": 0.2}),
    )
    db.add(p)
    db.commit()
    return p


def _cash_check(result: dict) -> dict:
    return next(c for c in result["checks"] if c["name"] == "CASH_SUFFICIENCY")


def test_cash_gate_selects_portfolio_by_mandate():
    db = _memory_db()
    user = User(username="bob", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)

    _llm_portfolio(db, user.id, "A", "50000")
    _llm_portfolio(db, user.id, "B", "10000")

    # EU ISIN + EU venue suffix keeps tradeability offline (no provider call).
    common = dict(
        db=db,
        user_id=user.id,
        mandate_config={"style": "x", "max_single_position_pct": 0.2},
        ticker="IWDA.AS",
        isin="IE00B4L5Y983",
        quantity=Decimal("1"),
        price=Decimal("100"),
    )

    res_a = check_buy_gates(**common, mandate="A")
    res_b = check_buy_gates(**common, mandate="B")

    assert Decimal(_cash_check(res_a)["detail"]["cash_available"]) == Decimal("50000")
    assert Decimal(_cash_check(res_b)["detail"]["cash_available"]) == Decimal("10000")
    # Mandate B can afford 1 share @100 (needs 101); A obviously can too.
    assert _cash_check(res_a)["passed"] is True
    assert _cash_check(res_b)["passed"] is True
