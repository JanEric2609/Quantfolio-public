"""Tests for the lab's cost model (app.lab.quant_lab.costs)."""
from decimal import Decimal

from app.lab.quant_lab.costs import CostModel, apply_trade_costs, estimate_tax_drag


def test_apply_trade_costs_sums_commission_and_spread():
    cost_model = CostModel(commission_bps=10.0, spread_bps=5.0)

    cost = apply_trade_costs(10_000.0, cost_model)

    assert cost == 10_000.0 * (15.0 / 10_000.0)


def test_apply_trade_costs_uses_absolute_notional():
    cost_model = CostModel(commission_bps=10.0, spread_bps=5.0)

    buy_cost = apply_trade_costs(5_000.0, cost_model)
    sell_cost = apply_trade_costs(-5_000.0, cost_model)

    assert buy_cost == sell_cost


def test_estimate_tax_drag_zero_on_loss():
    result = estimate_tax_drag(Decimal("-100"), Decimal("0.30"))

    assert result.total_tax_eur == Decimal("0")


def test_estimate_tax_drag_applies_teilfreistellung_then_kest():
    # 2000 EUR gain, 30% Teilfreistellung -> 1400 taxable, minus 1000 allowance -> 400 taxable.
    result = estimate_tax_drag(
        Decimal("2000"),
        Decimal("0.30"),
        allowance_total_eur=Decimal("1000"),
        allowance_used_so_far_eur=Decimal("0"),
    )

    assert result.taxable_after_teilfreistellung_eur == Decimal("1400.00")
    assert result.allowance_used_eur == Decimal("1000")
    assert result.taxable_after_allowance_eur == Decimal("400.00")
    assert result.kest_eur == Decimal("100.00")  # 25% of 400
    assert result.soli_eur == Decimal("5.50")  # 5.5% of 100
    assert result.kirchensteuer_eur == Decimal("0")
    assert result.total_tax_eur == Decimal("105.50")


def test_estimate_tax_drag_respects_already_used_allowance():
    result = estimate_tax_drag(
        Decimal("500"),
        Decimal("0"),
        allowance_total_eur=Decimal("1000"),
        allowance_used_so_far_eur=Decimal("1000"),
    )

    # Allowance already exhausted -> full 500 is taxable.
    assert result.allowance_used_eur == Decimal("0")
    assert result.taxable_after_allowance_eur == Decimal("500")
    assert result.kest_eur == Decimal("125.00")


def test_estimate_tax_drag_applies_church_tax_when_given():
    result = estimate_tax_drag(
        Decimal("2000"),
        Decimal("0"),
        allowance_total_eur=Decimal("0"),
        church_rate=Decimal("0.09"),
    )

    assert result.kirchensteuer_eur == (result.kest_eur * Decimal("0.09")).quantize(Decimal("0.01"))
    assert result.total_tax_eur == result.kest_eur + result.soli_eur + result.kirchensteuer_eur
