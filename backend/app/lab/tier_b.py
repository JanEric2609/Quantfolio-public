"""Tier B: long-history tests of ETF overlays, pre-registered in ADR 0020 (plan 2026-10-12, D13 / WP12).

Pure functions over monthly return series; the data are pulled by
``scripts/tier_b_long_history.py``. ADR 0020 fixed the strategies, periods,
costs and pass bars first; the one registered run (2026-10-14) passed none.

Strategies (each one trial in ``trial_ledger``, context ``tier_b``):

* **B1 trend overlay** — hold the equity index while its total-return level
  is above its 10-month moving average at month-end, else cash (T-bills /
  €STR); the 200-day variant is a second trial.
* **B2 minimum volatility**, **B3 size**, **B4 value**, **B5 momentum** —
  long-short or long-only factor returns against the market.

Pass bar (all must hold): Newey-West t >= 2.0 on the net excess return in
every out-of-sample region, the same sign in-sample, and for B1 a max
drawdown at least 25 % smaller than buy-and-hold with net CAGR no more than
1 pp/yr below it. Returns dated before a strategy's publication keep
``1 - PUBLICATION_DECAY`` of their size (ADR 0020 amendment 2026-10-14);
later months are real post-publication data and already carry any decay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, cast

import numpy as np
import pandas as pd

from app.foundation.quant_metrics import newey_west_t_stat
from app.lab.factor_premia.strategies import AFTER_TAX_FACTOR, IMPLEMENTATION_COST, PUBLICATION_DECAY

#: German tax on a realised gain at a B1 switch, after 30 % Teilfreistellung
#: (the same constant as ``lab.factor_premia.strategies.AFTER_TAX_FACTOR``).
GAIN_TAX_RATE = 0.26375 * (1.0 - 0.30)
#: One-way cost of a B1 switch (spread plus order cost), as a fraction.
SWITCH_COST = 0.0010
#: Year each strategy was published. Returns dated earlier keep
#: ``1 - PUBLICATION_DECAY`` (McLean & Pontiff: about 58 % of a premium is
#: gone after publication). Faber 2007; Blitz & van Vliet 2007; Banz 1981;
#: Fama & French 1992; Jegadeesh & Titman 1993.
PUBLICATION_YEAR: dict[str, int] = {"B1": 2007, "B2": 2007, "B3": 1981, "B4": 1992, "B5": 1993}
#: Newey-West lag for monthly data (about one year).
NW_LAGS = 12

IN_SAMPLE = ("1926-07", "1989-12")
OUT_OF_SAMPLE = ("1990-01", "2025-12")
OOS_REGIONS = ("us", "developed_ex_us", "europe")

REGISTERED_TRIALS: tuple[str, ...] = (
    "B1_trend_10m",
    "B1_trend_200d",
    "B2_min_vol",
    "B3_size",
    "B4_value",
    "B5_momentum",
)

T_PASS = 2.0
B1_MIN_DRAWDOWN_CUT = 0.25
B1_MAX_CAGR_SHORTFALL = 0.01


def trend_signal(level: pd.Series, window: int = 10) -> pd.Series:
    """1 when the month-end level is above its ``window``-month moving average, else 0.

    The signal at month t decides the position held during month t+1, so it
    is shifted by one before use (no look-ahead).
    """
    ma = cast(pd.Series, level.rolling(window, min_periods=window).mean())
    above = cast(pd.Series, (level > ma).astype(float))
    return above.where(ma.notna())


def trend_overlay(
    equity_ret: pd.Series,
    cash_ret: pd.Series,
    *,
    window: int = 10,
    switch_cost: float = SWITCH_COST,
    gain_tax_rate: float = GAIN_TAX_RATE,
) -> pd.DataFrame:
    """Monthly net returns of B1, with switches and the tax paid at each exit.

    Returns a frame with ``gross``, ``net`` and ``in_equity`` columns. Tax is
    charged on the realised gain of the equity leg when the overlay exits to
    cash (a loss creates no refund here: conservative). ``gain_tax_rate=0``
    is the NV-Bescheinigung line.
    """
    df = pd.DataFrame({"eq": equity_ret, "cash": cash_ret}).dropna()
    level = (1.0 + df["eq"]).cumprod()
    position = trend_signal(level, window).shift(1)
    df = df.assign(pos=position).dropna()
    gross, net, holding = [], [], []
    entry_value: float | None = None
    leg_value = 1.0
    prev = None
    for eq_r, cash_r, pos_raw in zip(df["eq"].astype(float), df["cash"].astype(float), df["pos"].astype(float)):
        pos = float(pos_raw)
        r_gross = pos * eq_r + (1.0 - pos) * cash_r
        r_net = r_gross
        if prev is not None and pos != prev:
            r_net -= switch_cost
        if pos == 1.0:
            if prev != 1.0:
                entry_value = leg_value
            leg_value *= 1.0 + eq_r
        elif prev == 1.0 and entry_value is not None:
            gain = leg_value / entry_value - 1.0
            if gain > 0:
                r_net -= gain_tax_rate * gain * (entry_value / leg_value)
            entry_value = None
        gross.append(r_gross)
        net.append(r_net)
        holding.append(pos)
        prev = pos
    return pd.DataFrame({"gross": gross, "net": net, "in_equity": holding}, index=df.index)


def max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.fillna(0.0)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min()) if len(wealth) else 0.0


def cagr(returns: pd.Series, periods_per_year: int = 12) -> float:
    r = returns.dropna()
    if r.empty:
        return 0.0
    growth = float((1.0 + r).prod())
    return growth ** (periods_per_year / len(r)) - 1.0 if growth > 0 else -1.0


def nw_t(excess: pd.Series) -> float:
    return float(newey_west_t_stat(np.asarray(excess.dropna(), dtype=float), NW_LAGS))


def publication_year(trial: str) -> int:
    return PUBLICATION_YEAR[trial.split("_")[0]]


def decay_before_publication(series: pd.Series, year: int) -> pd.Series:
    """Scale the months dated before ``year`` by ``1 - PUBLICATION_DECAY``.

    A plain rescaling of the whole series would leave the t-statistic
    unchanged; only discounting the pre-publication part moves it.
    """
    idx = pd.PeriodIndex(series.index, freq="M")
    factor = np.where(np.array([p.year for p in idx]) < year, 1.0 - PUBLICATION_DECAY, 1.0)
    return series * factor


def tilt_net(excess: pd.Series, *, taxed: bool = True) -> pd.Series:
    """Net excess of a factor tilt (B2-B5) over the core, per month.

    ``IMPLEMENTATION_COST`` (TER gap to the core plus in-fund trading, per
    year) comes off first; the tilt and the core are taxed alike, so tax then
    scales the excess by ``AFTER_TAX_FACTOR``. ``taxed=False`` is the
    NV-Bescheinigung line.
    """
    net = excess - IMPLEMENTATION_COST / 12.0
    return net * AFTER_TAX_FACTOR if taxed else net


def window(series: pd.Series, period: tuple[str, str]) -> pd.Series:
    idx = pd.PeriodIndex(series.index, freq="M") if not isinstance(series.index, pd.PeriodIndex) else series.index
    s = pd.Series(series.to_numpy(), index=idx)
    mask = (s.index >= pd.Period(period[0], "M")) & (s.index <= pd.Period(period[1], "M"))
    return cast(pd.Series, s[mask])


@dataclass
class Verdict:
    trial: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def evaluate(
    trial: str,
    in_sample_excess: pd.Series,
    oos_excess: dict[str, pd.Series],
    *,
    publication: int | None = None,
    b1: dict[str, float] | None = None,
) -> Verdict:
    """Apply ADR 0020's pass bar to one trial's net excess returns.

    ``oos_excess`` maps each out-of-sample region to its net excess series;
    at least two regions must be present and every one must clear t >= 2.0.
    ``publication`` is the year the strategy was published (months before it
    are decayed); ``None`` skips the decay.
    ``b1`` carries ``{"mdd", "mdd_bh", "cagr", "cagr_bh"}`` for the overlay.
    """
    reasons: list[str] = []
    t_is = nw_t(in_sample_excess)
    mean_is = float(in_sample_excess.mean()) if len(in_sample_excess) else 0.0
    stats: dict[str, Any] = {"t_in_sample": round(t_is, 3), "mean_in_sample": mean_is}
    if len(oos_excess) < 2:
        reasons.append("fewer than two out-of-sample regions")
    for region, series in oos_excess.items():
        scaled = series if publication is None else decay_before_publication(series, publication)
        t = nw_t(scaled)
        stats[f"t_{region}"] = round(t, 3)
        stats[f"mean_pa_{region}"] = round(float(series.mean()) * 12.0, 5)
        if t < T_PASS:
            reasons.append(f"{region}: t {t:.2f} < {T_PASS}")
        if math.copysign(1.0, float(series.mean() or 0.0)) != math.copysign(1.0, mean_is or 0.0):
            reasons.append(f"{region}: sign differs from in-sample")
    if b1 is not None:
        cut = 1.0 - (b1["mdd"] / b1["mdd_bh"]) if b1["mdd_bh"] else 0.0
        stats.update({"drawdown_cut": round(cut, 4), "cagr_shortfall": round(b1["cagr_bh"] - b1["cagr"], 4)})
        if cut < B1_MIN_DRAWDOWN_CUT:
            reasons.append(f"drawdown cut {cut:.0%} < {B1_MIN_DRAWDOWN_CUT:.0%}")
        if b1["cagr_bh"] - b1["cagr"] > B1_MAX_CAGR_SHORTFALL:
            reasons.append(f"net CAGR {b1['cagr_bh'] - b1['cagr']:.2%}/yr below buy-and-hold")
    return Verdict(trial, not reasons, reasons, stats)


def register_trials(db: Any) -> int:
    """Record every pre-registered trial in ``trial_ledger`` (context ``tier_b``); idempotent."""
    from app.foundation.quant_metrics import record_trial

    for trial in REGISTERED_TRIALS:
        # The two B1 variants are one family (same signal, same data); the rest are separate.
        record_trial(db, "tier_b", trial, {"family": trial.split("_")[0], "adr": "0020"})
    db.commit()
    return len(REGISTERED_TRIALS)
