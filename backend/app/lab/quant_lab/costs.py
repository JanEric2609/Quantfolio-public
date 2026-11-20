"""Explicit trading-cost and German tax-drag model for the lab backtester.

ADR 0015 ruling #27 ("Cost model: explicit costs + spread, and German tax
drag"). ``backtest_vbt/engine.py`` only models flat commission + slippage via
vectorbt's built-ins; this module adds a spread cost and composes the
existing ``tax_calc`` scalar building blocks into a per-realized-gain drag
estimate, for ``quant_lab.engine.run_lab_backtest`` to apply on every trade.

``estimate_tax_drag`` is a backtest-research simplification, not the tax
cockpit: it estimates drag per realized gain using a caller-tracked running
Sparer-Pauschbetrag allowance, without the cockpit's full annual FIFO-lot
pooling. Not exposed via any API, so the ``estimate``/``not_tax_advice``
response convention (see ``tax_cockpit.py``) doesn't apply here structurally
-- but the result is exactly as approximate as that phrasing implies.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.foundation.tax_calc import (
    KEST_RATE,
    apply_sparer_pauschbetrag,
    apply_teilfreistellung,
    kirchensteuer_amount,
    soli_amount,
)


@dataclass(frozen=True)
class CostModel:
    """Per-trade cost assumptions for the lab backtest engine."""

    commission_bps: float = 10.0  # 0.10%, matches backtest_vbt's default commission=0.001
    spread_bps: float = 5.0  # half-spread cost per trade; unmodeled anywhere else in the codebase
    apply_tax_drag: bool = True


def apply_trade_costs(notional_eur: float, cost_model: CostModel) -> float:
    """Commission + spread cost in EUR for one trade of the given notional."""
    bps = cost_model.commission_bps + cost_model.spread_bps
    return abs(notional_eur) * (bps / 10_000.0)


@dataclass(frozen=True)
class TaxDragResult:
    taxable_after_teilfreistellung_eur: Decimal
    taxable_after_allowance_eur: Decimal
    allowance_used_eur: Decimal
    kest_eur: Decimal
    soli_eur: Decimal
    kirchensteuer_eur: Decimal
    total_tax_eur: Decimal


def estimate_tax_drag(
    realized_gain_eur: Decimal,
    teilfreistellung_pct: Decimal = Decimal("0"),
    *,
    allowance_total_eur: Decimal = Decimal("1000"),
    allowance_used_so_far_eur: Decimal = Decimal("0"),
    church_rate: Decimal = Decimal("0"),
) -> TaxDragResult:
    """Estimate German capital-gains tax drag on one realized gain.

    Composes the existing ``tax_calc`` scalar building blocks: Teilfreistellung
    (Sec. 20 InvStG) -> Sparer-Pauschbetrag -> flat 25% KESt -> Soli -> optional
    church tax. Losses (``realized_gain_eur <= 0``) carry zero drag -- this
    module does not model loss-bucket offsetting (``tax_calc.classify_loss_bucket``),
    matching the "simplification" scope stated in the module docstring.
    """
    if realized_gain_eur <= 0:
        zero = Decimal("0")
        return TaxDragResult(zero, zero, zero, zero, zero, zero, zero)

    taxable_after_tf = apply_teilfreistellung(realized_gain_eur, teilfreistellung_pct)
    taxable_after_allowance, used = apply_sparer_pauschbetrag(
        taxable_after_tf, allowance_total_eur, allowance_used_so_far_eur
    )
    kest = (taxable_after_allowance * KEST_RATE).quantize(Decimal("0.01"))
    soli = soli_amount(kest)
    church = kirchensteuer_amount(kest, church_rate)
    total = kest + soli + church

    return TaxDragResult(
        taxable_after_teilfreistellung_eur=taxable_after_tf,
        taxable_after_allowance_eur=taxable_after_allowance,
        allowance_used_eur=used,
        kest_eur=kest,
        soli_eur=soli,
        kirchensteuer_eur=church,
        total_tax_eur=total,
    )
