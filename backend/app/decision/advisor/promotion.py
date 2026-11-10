"""Paired promotion test for champion vs challenger (ADR 0025, plan 2026-11-10 WS3).

Pre-registered before any data was seen; the constants below are the ADR's.

Primary endpoint: on each matched (date, ticker) shadow forecast,
``d = Brier(champion) - Brier(challenger)``; averaged across tickers within a
date to ``d_t`` (clustered on date). One-sided Diebold-Mariano on ``d_t`` with
a Newey-West variance at lag ``h - 1 = 20`` and the Harvey-Leybourne-Newbold
correction (t with T-1 df). Positive ``d`` means the challenger forecasts better.

Planned looks: the first on the day the minimums are met, then every 21
trading days, at most 6. Each look tests at the Pocock nominal level for 6
looks at an overall one-sided alpha of 0.05 / 4 (Bonferroni over about four
challengers a year). Not better at the 6th look: the challenger is retired.

Guardrails (promotion is blocked when either fails): the lower 90 % bound of
the paired daily return difference (moving-block bootstrap, block 21) must be
above -3 %/yr, and the challenger's max drawdown may exceed the champion's by
at most 5 pp. The composite 4-axis score stays on screen as a display only.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Sequence

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorShadowForecast,
    AdvisorStrategy,
    PaperSnapshot,
)
from app.foundation.quant_metrics import diebold_mariano_hln

HORIZON_DAYS = 21
NW_LAG = HORIZON_DAYS - 1
OVERALL_ALPHA = 0.05 / 4
MAX_LOOKS = 6
LOOK_SPACING_TRADING_DAYS = 21
#: Pocock nominal one-sided level per look for 6 equally spaced looks at an
#: overall one-sided 0.0125: boundary c = 2.713 (2,000,000-path simulation,
#: seed 20261110; ``tests/test_advisor_promotion.py`` re-derives it).
POCOCK_BOUNDARY = 2.713
POCOCK_NOMINAL_ALPHA = 0.00333

MIN_TRADING_DAYS = 60
MIN_HORIZONS = 3
MIN_PAIRS = 200

GUARD_RETURN_FLOOR_ANNUAL = -0.03
GUARD_BOOTSTRAP_LEVEL = 0.90
GUARD_BLOCK = 21
GUARD_BOOTSTRAP_DRAWS = 2000
GUARD_MDD_SLACK = 0.05
TRADING_DAYS_PER_YEAR = 252


def trading_days_between(start: date, end: date) -> int:
    """Mon-Fri days in ``[start, end)``."""
    if end <= start:
        return 0
    return int(np.busday_count(start, end))


def matched_daily_differences(
    rows: Sequence[AdvisorShadowForecast], champion_id: str, challenger_id: str
) -> tuple[list[date], list[float], int]:
    """(dates, d_t, n_pairs) over resolved (date, ticker) pairs both forecast."""
    by_key: dict[tuple[date, str], dict[str, float]] = {}
    for row in rows:
        if row.brier is None or row.resolved_at is None:
            continue
        by_key.setdefault((row.forecast_date, row.ticker), {})[row.strategy_id] = float(row.brier)
    per_date: dict[date, list[float]] = {}
    pairs = 0
    for (day, _ticker), briers in by_key.items():
        if champion_id in briers and challenger_id in briers:
            per_date.setdefault(day, []).append(briers[champion_id] - briers[challenger_id])
            pairs += 1
    days = sorted(per_date)
    return days, [float(np.mean(per_date[d])) for d in days], pairs


def non_overlapping_horizons(days: Sequence[date], horizon: int = HORIZON_DAYS) -> int:
    """Resolved forecast dates at least one horizon apart, counted greedily."""
    count = 0
    last: date | None = None
    for day in sorted(days):
        if last is None or trading_days_between(last, day) >= horizon:
            count += 1
            last = day
    return count


def moving_block_bootstrap_lower(
    diff: Sequence[float],
    *,
    block: int = GUARD_BLOCK,
    level: float = GUARD_BOOTSTRAP_LEVEL,
    draws: int = GUARD_BOOTSTRAP_DRAWS,
    seed: int = 0,
) -> float | None:
    """One-sided lower bound of the annualised mean daily difference."""
    x = np.asarray([v for v in diff if np.isfinite(v)], dtype=float)
    n = len(x)
    if n < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(draws, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(draws, -1)[:, :n]
    means = x[idx].mean(axis=1) * TRADING_DAYS_PER_YEAR
    return float(np.quantile(means, 1.0 - level))


def max_drawdown(values: Sequence[float]) -> float | None:
    v = np.asarray([x for x in values if x is not None and x > 0], dtype=float)
    if len(v) < 2:
        return None
    peak = np.maximum.accumulate(v)
    return float(np.max(1.0 - v / peak))


def _nav_by_date(db: Session, portfolio_id: str, since: date) -> dict[date, float]:
    rows = (
        db.query(PaperSnapshot.date, PaperSnapshot.total_value)
        .filter(PaperSnapshot.portfolio_id == portfolio_id, PaperSnapshot.date >= since)
        .all()
    )
    return {d: float(v) for d, v in rows if v is not None and float(v) > 0}


def paired_sleeve_returns(
    db: Session, champion_pid: str, challenger_pid: str, since: date
) -> tuple[list[float], list[float]]:
    """Daily NAV returns of both sleeves on the same dates (weekdays only)."""
    a = _nav_by_date(db, champion_pid, since)
    b = _nav_by_date(db, challenger_pid, since)
    days = sorted(d for d in set(a) & set(b) if d.weekday() < 5)
    ra, rb = [], []
    for prev, cur in zip(days, days[1:]):
        ra.append(a[cur] / a[prev] - 1.0)
        rb.append(b[cur] / b[prev] - 1.0)
    return ra, rb


@dataclass
class PairedTestResult:
    verdict: str  # observing | promote | retire_challenger
    reason: str
    n_pairs: int = 0
    n_dates: int = 0
    horizons_resolved: int = 0
    trading_days_since_spawn: int = 0
    minimums: dict[str, bool] = field(default_factory=dict)
    mean_d: float | None = None
    dm_stat: float | None = None
    p_value: float | None = None
    look: int | None = None
    looks_done: int = 0
    next_look_on: str | None = None
    nominal_alpha: float = POCOCK_NOMINAL_ALPHA
    guardrails: dict[str, Any] = field(default_factory=dict)
    mcnemar: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def mcnemar_exact(rows: Sequence[AdvisorShadowForecast], champion_id: str, challenger_id: str) -> dict[str, Any] | None:
    """Display only: exact McNemar on matched pairs where the calls disagree in direction."""
    from scipy.stats import binomtest

    by_key: dict[tuple[date, str], dict[str, tuple[float, bool]]] = {}
    for row in rows:
        if row.beat is None or row.p_beat is None:
            continue
        by_key.setdefault((row.forecast_date, row.ticker), {})[row.strategy_id] = (
            float(row.p_beat), bool(row.beat)
        )
    champ_only = chall_only = 0
    for calls in by_key.values():
        if champion_id not in calls or challenger_id not in calls:
            continue
        (pc, beat), (px, _) = calls[champion_id], calls[challenger_id]
        if (pc >= 0.5) == (px >= 0.5):
            continue
        if (pc >= 0.5) == beat:
            champ_only += 1
        else:
            chall_only += 1
    n = champ_only + chall_only
    if n == 0:
        return None
    p = float(binomtest(chall_only, n, 0.5, alternative="greater").pvalue)
    return {"challenger_right": chall_only, "champion_right": champ_only, "p_value": p}


def evaluate_paired_promotion(
    db: Session,
    champion: AdvisorStrategy,
    challenger: AdvisorStrategy,
    *,
    today: date,
    state: dict[str, Any],
) -> PairedTestResult:
    """Apply the pre-registered rule. *state* (persisted by the caller) holds
    ``minimums_met_on`` and ``looks`` (list of ISO dates already used)."""
    born = _as_date(challenger.created_at) or today
    rows = (
        db.query(AdvisorShadowForecast)
        .filter(
            AdvisorShadowForecast.strategy_id.in_([champion.id, challenger.id]),
            AdvisorShadowForecast.forecast_date >= born,
        )
        .all()
    )
    days, d_t, n_pairs = matched_daily_differences(rows, champion.id, challenger.id)
    horizons = non_overlapping_horizons(days)
    since_spawn = trading_days_between(born, today)
    minimums = {
        "trading_days": since_spawn >= MIN_TRADING_DAYS,
        "horizons": horizons >= MIN_HORIZONS,
        "pairs": n_pairs >= MIN_PAIRS,
    }
    result = PairedTestResult(
        verdict="observing", reason="", n_pairs=n_pairs, n_dates=len(days),
        horizons_resolved=horizons, trading_days_since_spawn=since_spawn, minimums=minimums,
        looks_done=len(state.get("looks") or []),
    )
    if d_t:
        result.mean_d = float(np.mean(d_t))
    result.mcnemar = mcnemar_exact(rows, champion.id, challenger.id)

    # Guardrails (always computed for display).
    ra, rb = (
        paired_sleeve_returns(db, champion.portfolio_id, challenger.portfolio_id, born)
        if champion.portfolio_id and challenger.portfolio_id
        else ([], [])
    )
    lower = moving_block_bootstrap_lower([b - a for a, b in zip(ra, rb)])
    mdd_champ = max_drawdown(np.cumprod(1 + np.asarray(ra)).tolist()) if ra else None
    mdd_chall = max_drawdown(np.cumprod(1 + np.asarray(rb)).tolist()) if rb else None
    return_ok = lower is not None and lower > GUARD_RETURN_FLOOR_ANNUAL
    mdd_ok = (
        mdd_champ is not None and mdd_chall is not None
        and mdd_chall - mdd_champ <= GUARD_MDD_SLACK
    )
    result.guardrails = {
        "return_diff_lower_90_annual": lower,
        "return_floor_annual": GUARD_RETURN_FLOOR_ANNUAL,
        "return_ok": return_ok,
        "mdd_champion": mdd_champ,
        "mdd_challenger": mdd_chall,
        "mdd_slack": GUARD_MDD_SLACK,
        "mdd_ok": mdd_ok,
    }

    if not all(minimums.values()):
        result.reason = "minimums not met: " + ", ".join(k for k, ok in minimums.items() if not ok)
        return result

    met_on = date.fromisoformat(state["minimums_met_on"]) if state.get("minimums_met_on") else today
    state["minimums_met_on"] = met_on.isoformat()
    looks: list[str] = list(state.get("looks") or [])
    due_index = trading_days_between(met_on, today) // LOOK_SPACING_TRADING_DAYS
    next_look = len(looks)
    if next_look >= MAX_LOOKS:
        result.verdict, result.reason = "retire_challenger", "all planned looks used"
        return result
    if due_index < next_look:
        result.next_look_on = _add_trading_days(met_on, next_look * LOOK_SPACING_TRADING_DAYS).isoformat()
        result.reason = "between planned looks"
        return result

    look = next_look + 1
    looks.append(today.isoformat())
    state["looks"] = looks
    result.look, result.looks_done = look, len(looks)
    dm = diebold_mariano_hln(d_t, h=HORIZON_DAYS, lag=NW_LAG)
    result.dm_stat, result.p_value = dm["stat"], dm["p_value"]
    better = dm["p_value"] is not None and dm["p_value"] < POCOCK_NOMINAL_ALPHA
    if better and return_ok and mdd_ok:
        result.verdict, result.reason = "promote", f"look {look}: p {dm['p_value']:.4f} < {POCOCK_NOMINAL_ALPHA}"
    elif look >= MAX_LOOKS:
        result.verdict = "retire_challenger"
        result.reason = f"not better at look {look} of {MAX_LOOKS}" + (
            "" if not better else " (a guardrail failed)"
        )
    else:
        result.reason = f"look {look}: " + (
            "better, but a guardrail failed" if better else "not better yet"
        )
        result.next_look_on = _add_trading_days(today, LOOK_SPACING_TRADING_DAYS).isoformat()
    return result


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _add_trading_days(start: date, n: int) -> date:
    return np.busday_offset(start, n, roll="forward").astype(date)
