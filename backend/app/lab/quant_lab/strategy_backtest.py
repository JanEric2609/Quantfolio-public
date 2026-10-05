"""Timing backtests of one instrument in EUR, judged against luck.

The old Backtest tab ran a pandas loop on two years of local-currency closes,
traded on the same close that produced the signal, and showed the Sharpe of
whatever the user tried last as if it were evidence. This runs the same rules
through the lab engine and says how much of the result could be luck:

* **Prices in EUR** (``eur_prices``), so a USD listing carries its currency
  risk, as it would in the book.
* **One bar delay.** A signal from the close of day t is traded at the close
  of day t+1; the day that produced the signal never also fills the order.
* **Costs and tax.** Commission and half-spread on every trade, German
  capital-gains tax on every sale (Teilfreistellung by fund type, losses
  offset, Sparer-Pauschbetrag each calendar year) through
  ``quant_lab.engine``. A buy-and-hold book is never sold, so it pays no
  tax here; that deferral is a real advantage of holding, not a bias.
* **Three lines, one window.** The rule, holding the same instrument, and the
  configured benchmark (MSCI World in EUR) start on the same day, after the
  longest look-back of the rule's parameter grid.
* **Luck.** Every run is a trial in the global ledger. The excess over
  holding the same instrument (what timing adds) gets a Deflated Sharpe Ratio
  at the ledger's trial count and at its count of distinct searches (Bailey
  and Lopez de Prado 2014), and the rule's parameter grid gets a Probability
  of Backtest Overfitting (CSCV, Bailey, Borwein, Lopez de Prado and Zhu
  2016). Below DSR 0.95, or with PBO above one half, the verdict is
  "insufficient evidence", whatever the return.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation import quant_metrics as qm
from app.foundation.tax_calc.jurisdictions.de.teilfreistellung import teilfreistellung_pct_for_fund_class
from app.lab.quant_lab.costs import CostModel
from app.lab.quant_lab.engine import LabBacktestSpec, run_lab_backtest

TRIAL_CONTEXT = "backtest"
DSR_THRESHOLD = 0.95
PBO_THRESHOLD = 0.5
MIN_EVALUATION_YEARS = 3.0
PBO_PARTITIONS = 16
INITIAL_EUR = 10_000.0
TRADING_DAYS = 252


Signal = Callable[[pd.Series, dict[str, Any]], pd.Series]


def _hold_state(enter: pd.Series, leave: pd.Series) -> pd.Series:
    """1 from an entry until the next exit, else 0 (exits win on a tie)."""
    state = pd.Series(np.nan, index=enter.index)
    state[enter.fillna(False).astype(bool)] = 1.0
    state[leave.fillna(False).astype(bool)] = 0.0
    return state.ffill().fillna(0.0)


def _mean(close: pd.Series, window: int) -> pd.Series:
    return cast(pd.Series, close.rolling(window).mean())


def _buy_hold(close: pd.Series, _p: dict[str, Any]) -> pd.Series:
    return pd.Series(1.0, index=close.index)


def _trend(close: pd.Series, p: dict[str, Any]) -> pd.Series:
    sma = _mean(close, int(p["window"]))
    return cast(pd.Series, close > sma).astype(float).where(sma.notna(), 0.0)


def _sma_cross(close: pd.Series, p: dict[str, Any]) -> pd.Series:
    fast = _mean(close, int(p["fast"]))
    slow = _mean(close, int(p["slow"]))
    return cast(pd.Series, fast > slow).astype(float).where(slow.notna(), 0.0)


def _momentum(close: pd.Series, p: dict[str, Any]) -> pd.Series:
    past = close.shift(int(p["lookback"]))
    return cast(pd.Series, close > past).astype(float).where(past.notna(), 0.0)


def _rsi_mean_reversion(close: pd.Series, p: dict[str, Any]) -> pd.Series:
    period = int(p["period"])
    delta = close.diff()
    # Wilder's smoothing, alpha = 1/period.
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rsi = cast(pd.Series, 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan)))
    return _hold_state(cast(pd.Series, rsi < float(p["oversold"])), cast(pd.Series, rsi > float(p["overbought"])))


def _bollinger_breakout(close: pd.Series, p: dict[str, Any]) -> pd.Series:
    window = int(p["window"])
    mid = close.rolling(window).mean()
    upper = mid + float(p["width"]) * close.rolling(window).std()
    return _hold_state(close > upper, close < mid)


@dataclass(frozen=True)
class Strategy:
    label: str
    about: str
    signal: Signal
    defaults: dict[str, float] = field(default_factory=dict)
    grid: tuple[dict[str, float], ...] = ()

    def lookback(self) -> int:
        lengths = [int(v) for params in (self.defaults, *self.grid) for k, v in params.items()
                   if k in ("window", "slow", "lookback", "period")]
        return max(lengths, default=0)


STRATEGIES: dict[str, Strategy] = {
    "buy_hold": Strategy(
        "Buy and hold", "Always invested. The reference every timing rule has to beat.", _buy_hold,
    ),
    "trend": Strategy(
        "Above its moving average",
        "Invested while the close is above its N-day average, in cash otherwise (Faber 2007).",
        _trend, {"window": 200}, tuple({"window": w} for w in (50, 100, 150, 200, 250)),
    ),
    "sma_cross": Strategy(
        "Moving-average crossover",
        "Invested while the fast average is above the slow one.",
        _sma_cross, {"fast": 50, "slow": 200},
        tuple({"fast": f, "slow": s} for f, s in ((10, 50), (20, 100), (50, 150), (50, 200), (100, 200), (20, 200))),
    ),
    "momentum": Strategy(
        "Time-series momentum",
        "Invested while the price is above its level N days ago (Moskowitz, Ooi and Pedersen 2012).",
        _momentum, {"lookback": 252}, tuple({"lookback": n} for n in (63, 126, 189, 252)),
    ),
    "rsi_mean_reversion": Strategy(
        "RSI mean reversion",
        "Buys when Wilder's RSI falls below the oversold level, sells above the overbought level.",
        _rsi_mean_reversion, {"period": 14, "oversold": 30, "overbought": 70},
        tuple({"period": n, "oversold": o, "overbought": 70} for n in (7, 14, 21) for o in (25, 30)),
    ),
    "bollinger_breakout": Strategy(
        "Bollinger breakout",
        "Buys a close above the upper band, sells a close below the middle band.",
        _bollinger_breakout, {"window": 20, "width": 2.0},
        tuple({"window": w, "width": k} for w in (20, 50) for k in (1.5, 2.0, 2.5)),
    ),
}


def strategy_catalog() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": s.label, "about": s.about, "defaults": s.defaults, "grid_size": len(s.grid)}
        for key, s in STRATEGIES.items()
    ]


def _clean_params(strategy: Strategy, params: dict[str, Any] | None) -> dict[str, float]:
    out = dict(strategy.defaults)
    for key, value in (params or {}).items():
        if key not in strategy.defaults:
            raise ValueError(f"Unknown parameter {key!r}")
        number = float(value)
        if not math.isfinite(number) or number <= 0 or number > 1000:
            raise ValueError(f"Parameter {key} must be between 0 and 1000")
        out[key] = int(number) if float(strategy.defaults[key]).is_integer() else number
    if "fast" in out and out["fast"] >= out["slow"]:
        raise ValueError("The fast average must be shorter than the slow one")
    return out


def _unit(equity: np.ndarray) -> np.ndarray:
    return 100.0 * equity / equity[0]


def _stats(unit: pd.Series, risk_free: float) -> dict[str, float | None]:
    rets = unit.pct_change().dropna()
    years = len(rets) / TRADING_DAYS
    total = float(unit.iloc[-1] / unit.iloc[0] - 1.0)
    vol = float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else None
    excess = rets - risk_free / TRADING_DAYS
    sd = float(excess.std(ddof=1)) if len(excess) > 1 else 0.0
    peak = unit.cummax()
    return {
        "total_return": total,
        "cagr": (1.0 + total) ** (1.0 / years) - 1.0 if years >= 1 else None,
        "volatility": vol,
        "sharpe": float(excess.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else None,
        "max_drawdown": float((unit / peak - 1.0).min()),
    }


def _engine_run(
    prices: pd.Series, position: pd.Series, symbol: str, cost: CostModel, tf_pct: Decimal,
) -> tuple[np.ndarray, float, float, int]:
    """Equity, costs, taxes and trade count for a 0/1 position traded at each date's close."""
    changes = position.where(position.diff().fillna(position) != 0)
    spec = LabBacktestSpec(
        symbols=[symbol], initial_cash=INITIAL_EUR, cost_model=cost,
        teilfreistellung_pct={symbol: tf_pct}, offset_losses=True, annual_allowance=True,
    )
    result = run_lab_backtest(spec, prices.to_frame(symbol), changes.to_frame(symbol))
    return result.equity_curve, result.total_costs_eur, result.total_tax_drag_eur, result.trades


def _grid_active_returns(
    close: pd.Series, strategy: Strategy, start: pd.Timestamp, cost_rate: float,
) -> pd.DataFrame:
    """Daily excess over holding, after costs, of every grid variant (no tax), for PBO."""
    asset = close.pct_change()
    cols = {}
    for i, params in enumerate(strategy.grid):
        traded = strategy.signal(close, params).shift(1)  # traded at the next close
        held = traded.shift(1)  # exposed from the close after the trade
        strat = held * asset - cost_rate * traded.diff().abs()
        cols[i] = (strat - asset).loc[start:].iloc[1:]
    return pd.DataFrame(cols).dropna()


def _dsr(returns: pd.Series, n_trials: int) -> float | None:
    if len(returns) < 3 or float(returns.std(ddof=1)) == 0.0:
        return None
    return float(qm.deflated_sharpe_ratio(returns.tolist(), n_trials=max(1, n_trials)))


def run_strategy_backtest(
    db: Session,
    ticker: str,
    strategy_key: str,
    params: dict[str, Any] | None = None,
    *,
    years: int = 10,
    commission_bps: float = 10.0,
    spread_bps: float = 5.0,
    fund_class: str = "other",
    apply_tax: bool = True,
    record: bool = True,
) -> dict[str, Any]:
    """Backtest one timing rule on one instrument; see the module docstring."""
    from app.foundation.eur_prices import benchmark_ticker, eur_closes
    from app.foundation.settings import get_risk_free_rate

    if strategy_key not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy_key!r}")
    strategy = STRATEGIES[strategy_key]
    chosen = _clean_params(strategy, params)
    symbol = ticker.strip().upper()
    lookback = max(strategy.lookback(), int(max(chosen.values(), default=0)))
    base: dict[str, Any] = {
        "ticker": symbol, "strategy": strategy_key, "label": strategy.label, "params": chosen,
        "available": False, "currency": "EUR",
    }

    rate_cache: dict[str, dict[str, float] | None] = {}
    days = int(years * 365.25) + int(lookback * 1.5) + 30
    closes = eur_closes(db, symbol, days=days, rate_cache=rate_cache)
    if len(closes) < lookback + 2 * TRADING_DAYS // 2:
        return {**base, "reason": f"Only {len(closes)} days of EUR prices for {symbol}; "
                                  f"the rule needs {lookback} days to warm up and a year to judge."}
    close = pd.Series(closes, dtype=float).sort_index()
    close.index = pd.to_datetime(close.index)
    start = cast(pd.Timestamp, close.index[min(lookback + 1, len(close) - 1)])
    window = close.loc[start:]

    bench_symbol = benchmark_ticker(db).upper()
    bench_raw = eur_closes(db, bench_symbol, days=days, rate_cache=rate_cache) if bench_symbol != symbol else closes
    bench = pd.Series(bench_raw, dtype=float).sort_index()
    bench.index = pd.to_datetime(bench.index)
    bench = bench.reindex(window.index.union(bench.index)).ffill(limit=5).reindex(window.index)

    cost = CostModel(commission_bps=commission_bps, spread_bps=spread_bps, apply_tax_drag=apply_tax)
    tf_pct = teilfreistellung_pct_for_fund_class(fund_class)
    position = strategy.signal(close, chosen).shift(1).fillna(0.0).loc[start:]
    eq, costs, taxes, trades = _engine_run(window, position, symbol, cost, tf_pct)
    bh_eq, bh_costs, _bh_tax, _ = _engine_run(window, pd.Series(1.0, index=window.index), symbol, cost, tf_pct)

    strat_unit = pd.Series(_unit(eq), index=window.index)
    hold_unit = pd.Series(_unit(bh_eq), index=window.index)
    bench_ok = bench.notna()
    bench_unit = None
    if bench_ok.iloc[0] and bench_ok.mean() > 0.95:
        filled = bench.ffill()
        bench_unit = 100.0 * filled / filled.iloc[0]

    risk_free = float(get_risk_free_rate(db))
    strat_ret = strat_unit.pct_change().dropna()
    hold_ret = hold_unit.pct_change().dropna()
    active = strat_ret - hold_ret
    eval_years = len(strat_ret) / TRADING_DAYS

    vs_hold = qm.benchmark_regression(strat_ret.tolist(), hold_ret.tolist(), risk_free=risk_free)
    vs_bench: dict[str, Any] | None = None
    if bench_unit is not None:
        b_ret = bench_unit.pct_change().dropna()
        joined = pd.concat([strat_ret, b_ret], axis=1, join="inner").dropna()
        vs_bench = qm.benchmark_regression(joined.iloc[:, 0].tolist(), joined.iloc[:, 1].tolist(), risk_free=risk_free)

    if record:
        key = json.dumps({
            "t": symbol, "s": strategy_key, "p": chosen, "from": str(start.date()), "to": str(cast(pd.Timestamp, window.index[-1]).date()),
            "c": [commission_bps, spread_bps], "tax": apply_tax, "fc": fund_class,
        }, sort_keys=True)
        qm.record_trial(db, TRIAL_CONTEXT, key, {"family": f"{symbol}:{strategy_key}", "source": "quant_lab_backtest"})
        db.commit()
    n_trials = qm.resolve_n_trials(db)
    n_eff = qm.effective_n_trials(db)

    timing = strategy_key != "buy_hold"
    pbo = None
    grid_size = len(strategy.grid)
    if timing and grid_size >= 2:
        grid = _grid_active_returns(close, strategy, start, (commission_bps + spread_bps) / 10_000.0)
        if len(grid) >= 2 * PBO_PARTITIONS:
            pbo = float(qm.probability_of_backtest_overfitting(grid.values, n_partitions=PBO_PARTITIONS))
    dsr = _dsr(active, n_trials) if timing else None
    dsr_eff = _dsr(active, n_eff) if timing else None
    mtrl = qm.min_track_record_length(active.tolist()) if timing and len(active) > 2 else None

    if not timing:
        verdict, why = "reference", "Buy and hold is the reference; there is no timing to test."
    elif eval_years < MIN_EVALUATION_YEARS:
        verdict, why = "too_short", (
            f"{eval_years:.1f} years after the warm-up; below {MIN_EVALUATION_YEARS:.0f} years a timing rule's "
            "excess is mostly noise."
        )
    elif dsr is not None and dsr >= DSR_THRESHOLD and (pbo is None or pbo <= PBO_THRESHOLD):
        verdict, why = "evidence", (
            f"The excess over holding survives {n_trials} trials (DSR {dsr:.2f})"
            + (f" and the parameter choice is unlikely to be overfit (PBO {pbo:.0%})." if pbo is not None else ".")
        )
    else:
        reasons = []
        if dsr is None or dsr < DSR_THRESHOLD:
            reasons.append(
                f"the excess over holding is within what {n_trials} tries would produce by luck "
                f"(DSR {dsr:.2f} < {DSR_THRESHOLD})" if dsr is not None else "the excess over holding has no variance"
            )
        if pbo is not None and pbo > PBO_THRESHOLD:
            reasons.append(f"the best parameters in-sample usually rank below the median out-of-sample (PBO {pbo:.0%})")
        verdict, why = "insufficient_evidence", "Insufficient evidence: " + "; ".join(reasons) + "."

    pos_change = position.diff().fillna(position)
    trade_rows = [
        {"date": str(d.date()), "side": "buy" if v > 0 else "sell", "price_eur": round(float(window.loc[d]), 4)}
        for d, v in pos_change[pos_change != 0].items()
    ]
    peak = strat_unit.cummax()
    series = [
        {
            "date": str(d.date()),
            "strategy": round(float(strat_unit.loc[d]), 4),
            "buy_hold": round(float(hold_unit.loc[d]), 4),
            "benchmark": None if bench_unit is None or pd.isna(bench_unit.loc[d]) else round(float(bench_unit.loc[d]), 4),
            "drawdown": round(float(strat_unit.loc[d] / peak.loc[d] - 1.0), 6),
        }
        for d in window.index
    ]
    return {
        **base,
        "available": True,
        "start": str(window.index[0].date()),
        "end": str(window.index[-1].date()),
        "years": round(eval_years, 2),
        "warmup_days": lookback,
        "benchmark": bench_symbol if bench_unit is not None else None,
        "risk_free": risk_free,
        "series": series,
        "trades": trade_rows,
        "stats": {
            "strategy": {**_stats(strat_unit, risk_free), "exposure": float(position.mean()), "trades": trades,
                         "costs_eur": round(costs, 2), "taxes_eur": round(taxes, 2)},
            "buy_hold": {**_stats(hold_unit, risk_free), "exposure": 1.0, "trades": 1, "costs_eur": round(bh_costs, 2),
                         "taxes_eur": 0.0},
            "benchmark": _stats(bench_unit, risk_free) if bench_unit is not None else None,
        },
        "vs_buy_hold": vs_hold,
        "vs_benchmark": vs_bench,
        "evidence": {
            "verdict": verdict,
            "why": why,
            "n_trials": n_trials,
            "n_trials_effective": n_eff,
            "dsr": dsr,
            "dsr_effective": dsr_eff,
            "dsr_threshold": DSR_THRESHOLD,
            "pbo": pbo,
            "pbo_threshold": PBO_THRESHOLD,
            "pbo_grid_size": grid_size if timing else 0,
            "min_track_record_years": (
                None if mtrl is None or not math.isfinite(mtrl) else round(mtrl / TRADING_DAYS, 1)
            ),
        },
        "assumptions": {
            "signal_delay": "A signal from one close is traded at the next close.",
            "costs": f"{commission_bps:g} bp commission + {spread_bps:g} bp half-spread per trade.",
            "tax": (
                f"German capital-gains tax on each sale, {float(tf_pct) * 100:.0f} % Teilfreistellung, losses offset, "
                "EUR 1,000 allowance a year; no Vorabpauschale, church tax or unsold gains."
                if apply_tax else "No tax."
            ),
            "cash": "Uninvested money earns nothing.",
        },
        "estimate": True,
    }
