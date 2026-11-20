"""FX shocks follow the currency a holding is priced in, not the one it was
booked in: DKB books a Nasdaq share bought at Tradegate in EUR, which used
to exempt it from every FX scenario (ADR 0007 amendment 3)."""
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.decision.verification.stress import PREDEFINED_SCENARIOS, _apply_scenario_to_holding
from app.foundation.models.entities import Holding


def _holding(ticker: str) -> Holding:
    return Holding(
        id=uuid4().hex, portfolio_id="p", ticker=ticker, name=ticker, asset_type="stock",
        quantity=Decimal("1"), avg_buy_price=Decimal("100"), currency="EUR",
    )


def _scenario(name: str):
    return next(s for s in PREDEFINED_SCENARIOS if s.name == name)


def test_a_dollar_share_booked_in_euros_takes_the_fx_shock():
    db = _memory_db()
    slump = _scenario("Dollar Slump")
    assert _apply_scenario_to_holding(_holding("AAPL"), slump, "EUR", db) == pytest.approx(-0.15)
    assert _apply_scenario_to_holding(_holding("SAP.DE"), slump, "EUR", db) == pytest.approx(0.0)


def test_a_falling_euro_cushions_foreign_holdings():
    """"Currency Crisis" is the euro falling: dollar holdings gain in euros
    (its description used to claim the opposite)."""
    db = _memory_db()
    crisis = _scenario("Currency Crisis")
    aapl = _apply_scenario_to_holding(_holding("AAPL"), crisis, "EUR", db)
    sap = _apply_scenario_to_holding(_holding("SAP.DE"), crisis, "EUR", db)
    assert aapl == pytest.approx(-0.08 + 0.15)
    assert sap == pytest.approx(-0.08)
    assert "gain" in crisis.description
