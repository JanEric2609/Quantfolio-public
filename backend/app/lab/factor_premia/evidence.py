"""Evidence card for one pre-registered factor strategy (prior-informed test).

A documented premium is not asked to prove itself from scratch (t > 3 after
deflating for hundreds of trials). It is asked four pre-registered questions
(``strategies.py``):

1. Is there enough history? At least ``MIN_MONTHS`` monthly cross-sections.
2. Is the long-short premium there? Newey-West t >= ``MIN_T_STAT``.
3. Did it survive publication? Positive mean long-short return after the
   publication year, over at least ``MIN_POST_PUBLICATION_MONTHS``.
4. Is it worth having, long-only, after decay, costs and tax? The
   top-third-minus-market return is haircut for post-publication decay,
   then for the extra cost of a factor ETF, then for German tax, and must
   stay positive.

The card also says what the tilt would cost in tracking error: the worst
five-year stretch of the long-only excess, scaled to the tilt cap.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

from app.foundation.quant_metrics import newey_west_t_stat
from app.lab.factor_premia.strategies import (
    AFTER_TAX_FACTOR,
    IMPLEMENTATION_COST,
    LAG_WINDOW_MONTHS,
    MIN_MONTHS,
    MIN_POST_PUBLICATION_MONTHS,
    MIN_T_STAT,
    NEWEY_WEST_LAGS,
    PUBLICATION_DECAY,
    Strategy,
)


@dataclass
class EvidenceCard:
    strategy: str
    label: str
    region: str
    citation: str
    etf_hint: str
    months: int
    start: str | None
    end: str | None
    long_short_annual: float | None
    long_short_t: float | None
    post_publication_months: int
    post_publication_annual: float | None
    long_only_annual: float | None
    expected_annual: float | None
    net_expected_annual: float | None
    tracking_error_annual: float | None
    worst_5y_excess: float | None
    book_worst_5y_lag: float | None
    tilt_cap_pct: float
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    yearly_long_only: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _annual(values: pd.Series) -> float:
    return float(values.to_numpy(dtype=float).mean()) * 12.0


def _worst_window(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    growth = (1.0 + series).rolling(window).apply(np.prod, raw=True) - 1.0
    worst = growth.min()
    return None if pd.isna(worst) else float(worst)


def evaluate(
    strategy: Strategy,
    series: pd.DataFrame,
    *,
    region: str,
    tilt_cap_pct: float,
) -> EvidenceCard:
    """Build the evidence card for *strategy* from its monthly *series*.

    ``series`` holds the strategy's rows from ``factor_series``: ``month``
    (a monthly period), ``long_short`` and ``long_only``.
    """
    frame = cast(pd.DataFrame, series.sort_values("month").dropna(subset=["long_short", "long_only"]))
    months = len(frame)
    ls = cast(pd.Series, frame["long_short"]).astype(float)
    lo = cast(pd.Series, frame["long_only"]).astype(float)

    post = frame[frame["month"].dt.year > strategy.published]
    post_months = len(post)

    ls_annual = _annual(ls) if months else None
    ls_t = float(newey_west_t_stat(ls.to_numpy(), lags=NEWEY_WEST_LAGS)) if months > NEWEY_WEST_LAGS else None
    post_annual = _annual(cast(pd.Series, post["long_short"])) if post_months else None
    lo_annual = _annual(lo) if months else None
    expected = lo_annual * (1.0 - PUBLICATION_DECAY) if lo_annual is not None else None
    net = (expected - IMPLEMENTATION_COST) * AFTER_TAX_FACTOR if expected is not None else None
    te = float(lo.std(ddof=1) * math.sqrt(12.0)) if months > 1 else None
    worst = _worst_window(lo.reset_index(drop=True), LAG_WINDOW_MONTHS)
    book_lag = worst * tilt_cap_pct / 100.0 if worst is not None else None

    checks = [
        {
            "name": "history",
            "label": f"At least {MIN_MONTHS // 12} years of monthly data",
            "value": months,
            "passed": months >= MIN_MONTHS,
        },
        {
            "name": "premium",
            "label": f"Long-short premium, Newey-West t ≥ {MIN_T_STAT:g}",
            "value": ls_t,
            "passed": ls_t is not None and ls_t >= MIN_T_STAT,
        },
        {
            "name": "post_publication",
            "label": f"Still positive after {strategy.published} (≥ {MIN_POST_PUBLICATION_MONTHS // 12} years)",
            "value": post_annual,
            "passed": post_months >= MIN_POST_PUBLICATION_MONTHS
            and post_annual is not None
            and post_annual > 0,
        },
        {
            "name": "net_of_costs",
            "label": "Long-only excess after decay, costs and German tax > 0",
            "value": net,
            "passed": net is not None and net > 0,
        },
    ]

    yearly: dict[str, float] = {}
    if months:
        by_year = (1.0 + lo).groupby(frame["month"].dt.year.to_numpy()).prod() - 1.0
        yearly = {str(int(y)): float(v) for y, v in by_year.items()}

    return EvidenceCard(
        strategy=strategy.key,
        label=strategy.label,
        region=region,
        citation=strategy.citation,
        etf_hint=strategy.etf_hint,
        months=months,
        start=str(frame["month"].iloc[0]) if months else None,
        end=str(frame["month"].iloc[-1]) if months else None,
        long_short_annual=ls_annual,
        long_short_t=ls_t,
        post_publication_months=post_months,
        post_publication_annual=post_annual,
        long_only_annual=lo_annual,
        expected_annual=expected,
        net_expected_annual=net,
        tracking_error_annual=te,
        worst_5y_excess=worst,
        book_worst_5y_lag=book_lag,
        tilt_cap_pct=tilt_cap_pct,
        passed=all(c["passed"] for c in checks),
        checks=checks,
        yearly_long_only=yearly,
    )
