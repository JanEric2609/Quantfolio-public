"""Rolling protective puts against simply holding less equity (Israelov 2019).

Israelov, "Pathetic Protection: The Elusive Benefits of Protective Puts",
Journal of Alternative Investments (Winter 2019): an index protected with
rolled puts had a lower Sharpe ratio and deeper peak-to-trough falls than a
portfolio that held less of the same index and the rest in cash, matched on
expected return. This replays that comparison on any index with an implied
volatility series (S&P 500 with VIX; Euro Stoxx 50 with VSTOXX):

* every ``tenor_days`` trading days, buy one put per unit of the index, struck
  ``otm`` below the close, priced with Black-Scholes at the implied volatility
  of that day plus ``skew`` (out-of-the-money puts trade above the at-the-money
  volatility an index like VIX measures);
* the hedged unit's return over the period is ``(S_end + payoff) / (S_0 +
  premium) - 1``, plus ``cost_bps`` of the premium lost to the spread;
* the comparison holds a fixed share ``w`` of the index and ``1 - w`` in cash,
  with ``w`` chosen so its mean period return equals the hedged one.

The model price is a floor on what a warrant costs: an issuer's margin comes
on top (:mod:`app.lab.options_lab.warrant_margin`).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.foundation.derivatives import black_scholes


def _drawdown(curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1.0).min())


def _stats(returns: np.ndarray, periods_per_year: float, cash: np.ndarray) -> dict[str, float]:
    curve = np.cumprod(1.0 + returns)
    years = len(returns) / periods_per_year
    cagr = float(curve[-1] ** (1.0 / years) - 1.0) if years > 0 and curve[-1] > 0 else float("nan")
    excess = returns - cash
    vol = float(np.std(returns, ddof=1) * math.sqrt(periods_per_year)) if len(returns) > 1 else float("nan")
    sd_excess = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    sharpe = float(np.mean(excess) / sd_excess * math.sqrt(periods_per_year)) if sd_excess > 0 else float("nan")
    return {
        "annual_return": cagr,
        "annual_volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": _drawdown(np.concatenate([[1.0], curve])),
        "worst_period": float(returns.min()),
    }


def protective_put_backtest(
    prices: pd.Series,
    implied_vol: pd.Series,
    *,
    otm: float = 0.05,
    tenor_days: int = 21,
    rate: float = 0.0,
    skew: float = 0.0,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """Hedged index vs. index, vs. less index plus cash at the same mean return.

    ``prices`` and ``implied_vol`` (annual fraction, e.g. VIX / 100) are daily
    series; only common dates are used. ``rate`` is the annual cash rate.
    """
    frame = pd.concat([prices.rename("s"), implied_vol.rename("v")], axis=1).dropna()
    frame = frame[(frame["s"] > 0) & (frame["v"] > 0)]
    if len(frame) < tenor_days * 12 + 1:
        raise ValueError("need at least a year of common prices and implied volatilities")
    s = np.asarray(frame["s"], dtype=float)
    v = np.asarray(frame["v"], dtype=float)
    years = tenor_days / 252.0
    starts = range(0, len(frame) - tenor_days, tenor_days)
    index_r, hedged_r, premium_share = [], [], []
    for i in starts:
        s0, s1 = s[i], s[i + tenor_days]
        strike = s0 * (1.0 - otm)
        premium = black_scholes("put", s0, strike, years, rate, v[i] + skew) * (1.0 + cost_bps / 1e4)
        payoff = max(strike - s1, 0.0)
        index_r.append(s1 / s0 - 1.0)
        hedged_r.append((s1 + payoff) / (s0 + premium) - 1.0)
        premium_share.append(premium / s0)
    idx, hed = np.array(index_r), np.array(hedged_r)
    periods_per_year = 252.0 / tenor_days
    cash = np.full(len(idx), (1.0 + rate) ** years - 1.0)
    # The share of index that, with the rest in cash, earns the hedged mean.
    gap = float(np.mean(idx - cash))
    weight = float(np.clip((np.mean(hed) - cash[0]) / gap, 0.0, 1.5)) if abs(gap) > 1e-12 else 1.0
    divested = weight * idx + (1.0 - weight) * cash
    return {
        "periods": len(idx),
        "first_day": str(frame.index[0])[:10],
        "last_day": str(frame.index[-1])[:10],
        "otm": otm, "tenor_days": tenor_days, "skew": skew, "rate": rate, "cost_bps": cost_bps,
        "premium_per_year": float(np.mean(premium_share) * periods_per_year),
        "matched_equity_weight": weight,
        "index": _stats(idx, periods_per_year, cash),
        "protected": _stats(hed, periods_per_year, cash),
        "divested": _stats(divested, periods_per_year, cash),
        "estimate": True,
    }
