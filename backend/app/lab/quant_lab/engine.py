"""Custom backtest engine (ADR 0015 ruling #25: "custom vectorised engine;
vectorbt as test oracle"). Supersedes ADR 0003 decision 7's "two engines,
unification deferred until a third engine proposal appears" -- this is that
proposal; ``backtest_vbt`` is demoted to the parity-check oracle, not deleted.

Periodic target-weight rebalancing over a symbol panel, applying
``quant_lab.costs``'s commission+spread+tax-drag cost model on every trade --
which ``backtest_vbt/engine.py`` does not (commission+slippage only, no
spread, no tax). Reuses ``backtest_vbt.metrics.compute_metrics`` for the
post-run Sharpe/Sortino/drawdown stats rather than reimplementing them
(ADR 0003 decision 4).

v1 scope: FIFO cost-basis tracking (via ``tax_calc.fifo_consume``) feeds
realized gains into ``costs.estimate_tax_drag`` on every sell, with a single
running Sparer-Pauschbetrag allowance for the whole backtest (a simplification
vs. the real tax cockpit's calendar-year resets -- see ``costs.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from typing import Literal

import numpy as np
import pandas as pd

from app.lab.backtest_vbt.metrics import BacktestMetrics, compute_metrics
from app.lab.quant_lab.costs import CostModel, apply_trade_costs, estimate_tax_drag
from app.foundation.tax_calc import fifo_consume


@dataclass
class LabBacktestSpec:
    """Specification for a quant_lab backtest run."""

    symbols: list[str]
    initial_cash: float = 100000.0
    cost_model: CostModel = field(default_factory=CostModel)
    teilfreistellung_pct: dict[str, Decimal] = field(default_factory=dict)
    allowance_total_eur: Decimal = Decimal("1000")
    # "target": every rebalance row re-targets all symbols (NaN = 0).
    # "trades": a rebalance row trades only what it names -- 0 sells the whole
    # position, NaN holds it untouched, and positive weights split the cash
    # (after the sells) between the buys. How a retail book trades in lumps.
    rebalance: Literal["target", "trades"] = "target"
    # Carry realized losses forward against later gains (the German
    # Verlustverrechnungstopf). Right for a book of one asset class only: stock
    # losses offset only stock gains.
    offset_losses: bool = False
    # Reset the Sparer-Pauschbetrag each calendar year, as the real allowance
    # does; off keeps the original single running allowance for the batch.
    annual_allowance: bool = False


@dataclass
class LabBacktestResult:
    """Result of a quant_lab backtest run."""

    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    trades: int
    final_equity: float
    equity_curve: np.ndarray
    timestamps: np.ndarray
    total_costs_eur: float
    total_tax_drag_eur: float
    metrics: BacktestMetrics | None = None
    error: str | None = None


def run_lab_backtest(
    spec: LabBacktestSpec,
    prices_df: pd.DataFrame,
    weights_df: pd.DataFrame,
) -> LabBacktestResult:
    """Simulate periodic target-weight rebalancing with explicit costs + tax drag.

    Args:
        spec: Backtest specification.
        prices_df: (date x symbol) close prices, one column per ``spec.symbols``.
        weights_df: (date x symbol) target weights. A row counts as a
            rebalance event only where it has at least one non-NaN value;
            between rebalance events, share counts are held constant
            (buy-and-hold drift), matching how a real periodic-rebalance
            strategy behaves rather than re-targeting every single day.
            With ``spec.rebalance == "trades"`` a row trades only the
            symbols it names (see ``LabBacktestSpec``).

    Returns:
        LabBacktestResult. Empty/misaligned input returns a zero-trade result
        with ``error`` set, matching ``backtest_vbt.engine``'s empty-input
        convention.
    """
    if prices_df.empty:
        return LabBacktestResult(
            total_return=0.0, annual_return=0.0, sharpe_ratio=0.0, max_drawdown=0.0,
            win_rate=0.0, trades=0, final_equity=spec.initial_cash,
            equity_curve=np.array([spec.initial_cash]), timestamps=np.array([]),
            total_costs_eur=0.0, total_tax_drag_eur=0.0, error="No price data provided",
        )

    dates = prices_df.index
    symbols = spec.symbols
    cash = float(spec.initial_cash)
    shares = pd.Series(0.0, index=symbols)
    lots: dict[str, list[dict]] = {s: [] for s in symbols}
    allowance_used = Decimal("0")
    loss_pot = Decimal("0")

    equity_curve = np.zeros(len(dates))
    total_costs = 0.0
    total_tax_drag = 0.0
    trade_count = 0

    rebalance_dates = set(weights_df.dropna(how="all").index) if weights_df is not None else set()

    def trade(sym: str, qty: float, price: float, i: int, acquired_on: date_type) -> None:
        nonlocal cash, total_costs, total_tax_drag, trade_count, allowance_used, loss_pot
        trade_count += 1
        notional = qty * price
        cost = apply_trade_costs(notional, spec.cost_model)
        total_costs += cost
        cash -= notional
        cash -= cost

        if qty > 0:
            lots[sym].append({
                "lot_id": f"{sym}-{i}",
                "acquired_at": acquired_on,
                "quantity_remaining": Decimal(str(qty)),
                "cost_basis_eur": Decimal(str(notional + cost)),
            })
        elif spec.cost_model.apply_tax_drag and lots[sym]:
            sell_qty = Decimal(str(-qty))
            available = sum((Decimal(str(lot["quantity_remaining"])) for lot in lots[sym]), Decimal("0"))
            sell_qty = min(sell_qty, available)
            if sell_qty > 0:
                sale = fifo_consume(lots[sym], sell_qty, Decimal(str(price)), Decimal(str(cost)))
                gain = sale.realised_gain_eur
                if spec.offset_losses:
                    if gain < 0:
                        loss_pot += -gain
                        gain = Decimal("0")
                    else:
                        used = min(loss_pot, gain)
                        loss_pot -= used
                        gain -= used
                tf_pct = spec.teilfreistellung_pct.get(sym, Decimal("0"))
                drag = estimate_tax_drag(
                    gain,
                    tf_pct,
                    allowance_total_eur=spec.allowance_total_eur,
                    allowance_used_so_far_eur=allowance_used,
                )
                allowance_used += drag.allowance_used_eur
                total_tax_drag += float(drag.total_tax_eur)
                cash -= float(drag.total_tax_eur)

    allowance_year = None
    for i, dt in enumerate(dates):
        prices_today = prices_df.loc[dt, symbols].astype(float)
        if spec.annual_allowance and hasattr(dt, "year") and dt.year != allowance_year:
            allowance_year = dt.year
            allowance_used = Decimal("0")

        if dt in rebalance_dates:
            acquired_on = dt.date() if hasattr(dt, "date") else date_type.today()
            if spec.rebalance == "trades":
                row = weights_df.loc[dt, symbols].astype(float)
                for sym in row.index[(row == 0) & (shares > 0)]:
                    price = float(prices_today[sym])
                    if price > 0:
                        trade(sym, -float(shares[sym]), price, i, acquired_on)
                        shares[sym] = 0.0
                buys = row[(row > 0) & (prices_today > 0)]
                budget = max(cash, 0.0)
                bps = spec.cost_model.commission_bps + spec.cost_model.spread_bps
                for sym, w in buys.items():
                    notional = budget * float(w) / float(buys.sum()) / (1.0 + bps / 10_000.0)
                    qty = notional / float(prices_today[sym])
                    if qty > 1e-9:
                        trade(str(sym), qty, float(prices_today[sym]), i, acquired_on)
                        shares[sym] += qty
            else:
                portfolio_value = cash + float((shares * prices_today).sum())
                target_w = weights_df.loc[dt, symbols].fillna(0.0).astype(float)
                target_value = target_w * portfolio_value
                safe_prices = prices_today.replace(0.0, np.nan)
                target_shares = (target_value / safe_prices).fillna(0.0)
                trade_shares = target_shares - shares

                for sym in symbols:
                    qty = float(trade_shares[sym])
                    price = float(prices_today[sym])
                    if abs(qty) < 1e-9 or price <= 0:
                        continue
                    trade(sym, qty, price, i, acquired_on)

                shares = target_shares

        equity_curve[i] = cash + float((shares * prices_today).sum())

    timestamps_arr = np.asarray(dates)
    metrics = compute_metrics(equity_curve, timestamps_arr)
    final_equity = float(equity_curve[-1]) if len(equity_curve) else spec.initial_cash

    return LabBacktestResult(
        total_return=metrics.total_return,
        annual_return=metrics.annual_return,
        sharpe_ratio=metrics.sharpe_ratio,
        max_drawdown=metrics.max_drawdown,
        win_rate=metrics.win_rate,
        trades=trade_count,
        final_equity=final_equity,
        equity_curve=equity_curve,
        timestamps=timestamps_arr,
        total_costs_eur=total_costs,
        total_tax_drag_eur=total_tax_drag,
        metrics=metrics,
    )
