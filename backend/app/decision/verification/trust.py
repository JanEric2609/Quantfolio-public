"""Can I trust it? — the verdict on the system's own predictions.

Reads the immutable prediction ledgers and answers one question per prediction
type, honestly at small N (small-sample statistics in
``foundation.forecast_verification``):

* **ideas** — Discover picks (``DiscoveryPrediction`` rows without a paper
  sleeve). A *hit* is beating the passive core ETF over the call's horizon
  (``excess_return > 0``, fixed when the resolution job ran).
* **advisor** — the advisor loop's daily calls (same ledger, rows with a paper
  sleeve; logged holds are not directional bets and are left out of hit
  statistics).
* **mandates** — weekly mandate decisions. The verdict *hit* means the stated
  expectation held; there is no naive benchmark, so evidence is against a coin
  flip and the row says so.
* **regime** — the daily regime label history. Regime labels are latent
  volatility states with no realised-state definition to grade them against, so
  nothing is scored yet; the history accumulates and the row says so.

Everything shown is frozen at issue time: the ledger rows are never rewritten
after resolution, so the numbers cannot drift to look better.

The unit of evidence is the **rebalance date**, not the pick. One Discover run
issues ~17 picks on the same day against the same market move; counting them as
17 independent calls overstates the evidence about 17-fold. So the picks of one
date form an equal-weight basket, and the date is a hit when that basket beat
the ETF. Hit rates get an exact Clopper-Pearson interval over dates; the mean
excess gets a Newey-West (HAC) interval, because the holding windows of
neighbouring dates overlap. Picks that stopped trading before their horizon
stay in, scored at their last close (or as a miss when there was no price at
all): dropping them is survivorship bias.

Several types tested in two directions are several looks, so the state comes
from e-BH across all of them (false-discovery rate 5 %), on the e-values as
they stand now. A single e-process crossing 20 is not enough on its own.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.decision.verification.benchmark import passive_core_symbol
from app.foundation import forecast_verification as fv
from app.foundation.models.entities import (
    DiscoveryPrediction,
    LlmPortfolioDecision,
    PaperPortfolio,
    RegimeLabelHistory,
)

logger = logging.getLogger(__name__)

#: Hit rate the page asks "can we tell this from a coin flip?" about. A
#: realistic edge for picks against a world ETF is a few points, so 55 %.
TARGET_HIT_RATE = 0.55
#: Null hypothesis of every e-process: a coin flip.
NULL_HIT_RATE = 0.5
#: Nominal coverage of the stated ranges: Discover's P10-P90 band and the
#: advisor's Monte Carlo P5-P95 band.
IDEAS_RANGE_LEVEL = 0.8
ADVISOR_RANGE_LEVEL = 0.9
#: Level of every interval shown.
CI_LEVEL = 0.9
#: Rebalance dates before the page may say anything other than "too early".
MIN_CALLS_FOR_STATE = 20
#: False-discovery rate of the e-BH procedure across every type and direction.
FDR_LEVEL = 0.05

TYPE_KEYS = ("ideas", "advisor", "mandates", "regime")
TYPE_LABELS = {
    "ideas": "Ideas (Discover)",
    "advisor": "Advisor",
    "mandates": "Mandates",
    "regime": "Regime",
}

METHOD_NOTE = (
    "The unit is the rebalance date: the picks issued on one day form an equal-weight basket, and "
    "the date is a hit when the basket beat your passive core ETF over the horizon (mandates: when "
    "the stated expectation held). Picks that stopped trading count, at their last close. Every "
    "number is frozen when the call was resolved. Intervals are 90 %: exact (Clopper-Pearson) for "
    "hit rates, Newey-West for the mean excess because holding windows overlap, block bootstrap for "
    "Brier skill. Evidence is an anytime-valid e-process against a 50 % coin flip per type and "
    "direction, and the e-BH procedure keeps the false-discovery rate at 5 % across all of them. "
    f"Telling a {round(TARGET_HIT_RATE * 100)} % hit rate from a coin flip takes about "
    f"{fv.calls_needed(TARGET_HIT_RATE)} independent dates."
)


# ---------------------------------------------------------------------------
# Normalised calls
# ---------------------------------------------------------------------------


@dataclass
class Call:
    """One resolved, frozen call in the shape the Calls table shows."""

    type: str
    id: str
    issued_at: datetime
    resolved_at: datetime
    subject: str
    call: str
    hit: bool
    stated_p: float | None = None
    stated_range: tuple[float, float] | None = None
    outcome: float | None = None
    benchmark_outcome: float | None = None
    excess: float | None = None
    verdict: str | None = None
    #: Holding period in trading days (sets the HAC lag).
    horizon_days: int | None = None
    delisted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "issued_at": self.issued_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat(),
            "subject": self.subject,
            "call": self.call,
            "stated_p": self.stated_p,
            "stated_range": list(self.stated_range) if self.stated_range else None,
            "outcome": self.outcome,
            "benchmark_outcome": self.benchmark_outcome,
            "excess": self.excess,
            "hit": self.hit,
            "verdict": self.verdict,
            "delisted": self.delisted,
        }


@dataclass
class _TypeData:
    """Everything one prediction type contributes, before statistics."""

    key: str
    calls: list[Call] = field(default_factory=list)
    # (low, high, stock-return) for every resolved row that stated a range.
    ranges: list[tuple[float, float, float]] = field(default_factory=list)
    range_level: float | None = None
    next_resolution_at: datetime | None = None
    benchmark_label: str = ""
    benchmarked: bool = True
    note: str | None = None
    n_delisted: int = 0


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _exit_date(row: DiscoveryPrediction) -> datetime:
    """When the call was resolved: the exit close the resolution job measured."""
    outcome = (row.score_json or {}).get("outcome") if isinstance(row.score_json, dict) else None
    exit_date = outcome.get("exit_date") if isinstance(outcome, dict) else None
    if isinstance(exit_date, str):
        try:
            return datetime.fromisoformat(exit_date[:10]).replace(tzinfo=UTC)
        except ValueError:
            pass
    return _utc(row.resolve_at) or _utc(row.predicted_at) or datetime.now(UTC)


def _earliest_future(values: list[datetime | None], now: datetime) -> datetime | None:
    future = [v for v in (_utc(x) for x in values) if v is not None and v > now]
    return min(future) if future else None


# ---------------------------------------------------------------------------
# Ledger readers
# ---------------------------------------------------------------------------


def _prediction_type(db: Session, user_id: str, key: str, now: datetime, benchmark: str) -> _TypeData:
    advisor = key == "advisor"
    query = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.user_id == user_id)
    query = query.filter(
        DiscoveryPrediction.portfolio_id.isnot(None) if advisor else DiscoveryPrediction.portfolio_id.is_(None)
    )
    rows = query.all()

    data = _TypeData(key=key, range_level=ADVISOR_RANGE_LEVEL if advisor else IDEAS_RANGE_LEVEL)
    pending: list[datetime | None] = []
    measured_against: set[str] = set()
    for row in rows:
        if row.outcome_status == "pending":
            pending.append(row.resolve_at)
            continue
        direction = (row.direction or "buy").lower()
        delisted = row.outcome_status == "delisted"
        if delisted and row.realised_return is None:
            # No price at all: a pick that could not be scored is a miss, not a gap.
            if direction != "neutral":
                data.n_delisted += 1
                data.calls.append(Call(
                    type=key, id=row.id, issued_at=_utc(row.predicted_at) or now, resolved_at=_exit_date(row),
                    subject=row.symbol, call=f"{direction.upper()} · {row.horizon_days} days · no price",
                    hit=False, horizon_days=row.horizon_days, delisted=True,
                ))
            continue
        if row.outcome_status not in ("resolved", "delisted") or row.realised_return is None:
            continue
        outcome = (row.score_json or {}).get("outcome") if isinstance(row.score_json, dict) else None
        if isinstance(outcome, dict) and outcome.get("benchmark"):
            measured_against.add(str(outcome["benchmark"]))
        stock_return = -float(row.realised_return) if direction in ("sell", "bear") else float(row.realised_return)
        if row.expected_return_low is not None and row.expected_return_high is not None:
            data.ranges.append((float(row.expected_return_low), float(row.expected_return_high), stock_return))
        if row.excess_return is None or direction == "neutral":
            continue  # no benchmark recorded, or a logged hold: not a directional bet against the ETF
        data.calls.append(
            Call(
                type=key,
                id=row.id,
                issued_at=_utc(row.predicted_at) or now,
                resolved_at=_exit_date(row),
                subject=row.symbol,
                call=f"{direction.upper()} · {row.horizon_days} days" + (" · stopped trading" if delisted else ""),
                hit=float(row.excess_return) > 0,
                stated_p=float(row.conviction_calibrated) if row.conviction_calibrated is not None else None,
                stated_range=(
                    (float(row.expected_return_low), float(row.expected_return_high))
                    if row.expected_return_low is not None and row.expected_return_high is not None
                    else None
                ),
                outcome=float(row.realised_return),
                benchmark_outcome=float(row.benchmark_return) if row.benchmark_return is not None else None,
                excess=float(row.excess_return),
                horizon_days=row.horizon_days,
                delisted=delisted,
            )
        )
        data.n_delisted += int(delisted)
    # Label with what the outcomes were actually measured against, not today's setting.
    symbols = ", ".join(sorted(measured_against)) or benchmark
    data.benchmark_label = f"your passive core ETF ({symbols})"
    data.next_resolution_at = _earliest_future(pending, now)
    return data


def _expectation_text(expectation: dict[str, Any], weeks: int | None) -> str:
    metric = str(expectation.get("metric", "")).replace("_pct", "").replace("_", " ") or "metric"
    direction = str(expectation.get("direction", "")).lower() or "move"
    magnitude = expectation.get("magnitude")
    size = f" by {abs(float(magnitude)):g} %" if isinstance(magnitude, (int, float)) else ""
    horizon = f" in {weeks} weeks" if weeks else ""
    return f"{metric}: {direction}{size}{horizon}"


def _mandate_type(db: Session, user_id: str, now: datetime) -> _TypeData:
    rows = (
        db.query(LlmPortfolioDecision)
        .join(PaperPortfolio, PaperPortfolio.id == LlmPortfolioDecision.portfolio_id)
        .filter(PaperPortfolio.user_id == user_id)
        .all()
    )
    data = _TypeData(
        key="mandates",
        benchmarked=False,
        benchmark_label="a coin flip (a stated expectation has no naive benchmark)",
        note="A hit means the stated expectation held; partial and missed both count as not hit, "
        "unresolvable decisions are left out.",
    )
    pending: list[datetime | None] = []
    for row in rows:
        review = _utc(row.review_date)
        if row.verdict is None:
            if row.status == "completed" and row.horizon_weeks and review is not None:
                pending.append(review + timedelta(weeks=int(row.horizon_weeks)))
            continue
        if row.verdict not in ("hit", "partial", "miss"):
            continue  # unresolvable: not a miss, so not in the denominator
        try:
            payload = json.loads(row.decision_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        try:
            reflection = json.loads(row.reflection_json or "{}") if row.reflection_json else {}
        except (TypeError, ValueError):
            reflection = {}
        expectation = payload.get("expectation") if isinstance(payload, dict) else None
        expectation = expectation if isinstance(expectation, dict) else {}
        actual = (reflection.get("actual_outcome") or {}).get("actual_pct") if isinstance(reflection, dict) else None
        confidence = expectation.get("confidence")
        stated = float(confidence) if isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0 else None
        subject = str(payload.get("ticker") or (row.mandate and f"Mandate {row.mandate}") or "Mandate")
        data.calls.append(
            Call(
                type="mandates",
                id=row.id,
                issued_at=review or now,
                resolved_at=_utc(row.scored_at) or review or now,
                subject=subject,
                call=_expectation_text(expectation, row.horizon_weeks),
                hit=row.verdict == "hit",
                stated_p=stated,
                outcome=float(actual) / 100.0 if isinstance(actual, (int, float)) else None,
                verdict=row.verdict,
                horizon_days=int(row.horizon_weeks) * 5 if row.horizon_weeks else None,
            )
        )
    data.next_resolution_at = _earliest_future(pending, now)
    return data


def _regime_type(db: Session) -> _TypeData:
    days = db.query(RegimeLabelHistory).order_by(RegimeLabelHistory.as_of).all()
    data = _TypeData(key="regime", benchmarked=False, benchmark_label="not scored yet")
    if days:
        data.note = (
            f"{len(days)} daily regime calls recorded since {days[0].as_of.isoformat()}. None are scored: "
            "regime labels are latent volatility states, and there is no agreed realised-state "
            "definition to grade them against yet, so the history only accumulates."
        )
    else:
        data.note = (
            "No daily regime calls recorded yet (the daily regime job writes one per day). "
            "Nothing is scored: regime labels have no realised-state definition to grade them against."
        )
    return data


# ---------------------------------------------------------------------------
# Statistics per type
# ---------------------------------------------------------------------------


def _calibration_caption(calls: list[Call]) -> str | None:
    """'When we said 70 %, it happened 6 of 10 times.' from the fullest 10 % bucket."""
    buckets: dict[int, list[bool]] = {}
    for c in calls:
        if c.stated_p is not None:
            buckets.setdefault(int(round(c.stated_p * 10)) * 10, []).append(c.hit)
    usable = {pct: hits for pct, hits in buckets.items() if len(hits) >= 5}
    if not usable:
        return None
    pct = max(usable, key=lambda p: (len(usable[p]), p))
    hits = usable[pct]
    return f"When we said {pct} %, it happened {sum(hits)} of {len(hits)} times."


def _interval(pair: tuple[float, float] | None) -> list[float] | None:
    return [pair[0], pair[1]] if pair else None


@dataclass
class _Unit:
    """One rebalance date: the equal-weight basket of that day's calls."""

    day: date
    calls: int
    hits: int
    excess: float | None  # basket mean excess; None when no call that day has one

    @property
    def hit(self) -> bool:
        if self.excess is not None:
            return self.excess > 0
        return self.hits * 2 > self.calls  # mandates: no excess, the majority of the day's calls


def _units(calls: list[Call]) -> list[_Unit]:
    by_day: dict[date, list[Call]] = {}
    for c in calls:
        by_day.setdefault(c.issued_at.date(), []).append(c)
    out = []
    for day in sorted(by_day):
        group = by_day[day]
        excesses = [c.excess for c in group if c.excess is not None]
        # A pick with no price at all counts as a miss; in the basket it is the day's worst pick.
        if excesses and any(c.delisted and c.excess is None for c in group):
            excesses += [min(excesses)] * sum(1 for c in group if c.delisted and c.excess is None)
        out.append(_Unit(
            day=day, calls=len(group), hits=sum(c.hit for c in group),
            excess=sum(excesses) / len(excesses) if excesses else None,
        ))
    return out


def _hac_lag(units: list[_Unit], horizon_days: int | None) -> int:
    """How many later dates fall inside one date's holding window (calendar ≈ trading × 7/5)."""
    if not units or not horizon_days:
        return 0
    window = timedelta(days=math.ceil(horizon_days * 7 / 5))
    days = [u.day for u in units]
    return max(sum(1 for d in days[i + 1:] if d - day < window) for i, day in enumerate(days))


def _type_stats(data: _TypeData) -> dict[str, Any]:
    calls = sorted(data.calls, key=lambda c: (c.resolved_at, c.id))
    units = _units(calls)
    n = len(units)
    hits = sum(1 for u in units if u.hit)
    flags = [1.0 if u.hit else 0.0 for u in units]

    skill_proc = fv.e_process_bernoulli(flags, NULL_HIT_RATE)
    harm_proc = fv.e_process_harm(flags, NULL_HIT_RATE)

    excesses = [u.excess for u in units if u.excess is not None]
    mean_excess = sum(excesses) / len(excesses) if excesses else None
    horizons = [c.horizon_days for c in calls if c.horizon_days]
    lag = _hac_lag(units, max(horizons) if horizons else None)

    stated = [(c.stated_p, 1.0 if c.hit else 0.0) for c in calls if c.stated_p is not None]
    brier = bss = None
    bss_ci: tuple[float, float] | None = None
    z = None
    reliability: list[dict[str, float]] = []
    if stated:
        p_vals = [s[0] for s in stated]
        y_vals = [s[1] for s in stated]
        skill = fv.brier_skill(p_vals, y_vals, level=CI_LEVEL)
        brier, bss = skill.brier, skill.skill
        bss_ci = (skill.ci_low, skill.ci_high) if skill.ci_low is not None and skill.ci_high is not None else None
        z = fv.spiegelhalter_z(p_vals, y_vals)
        if len(stated) >= fv.MIN_CALLS_FOR_RELIABILITY:
            reliability = fv.reliability_bins(p_vals, y_vals, bins=5)

    coverage = None
    if data.ranges and data.range_level is not None:
        cov = fv.coverage([low <= r <= high for low, high, r in data.ranges], data.range_level, CI_LEVEL)
        coverage = {
            "k": cov.k,
            "n": cov.n,
            "rate": cov.rate,
            "ci_low": cov.ci_low,
            "ci_high": cov.ci_high,
            "nominal": cov.nominal,
        }

    call_hits = sum(1 for c in calls if c.hit)
    return {
        "type": data.key,
        "label": TYPE_LABELS[data.key],
        # n, hits and the hit rate count rebalance dates, the independent unit.
        "n": n,
        "n_needed": fv.calls_needed(TARGET_HIT_RATE) if data.key != "regime" else None,
        "n_issue_days": n,
        "n_calls": len(calls),
        "n_delisted": data.n_delisted,
        "call_hits": call_hits,
        "call_hit_rate": call_hits / len(calls) if calls else None,
        "hits": hits,
        "hit_rate": hits / n if n else None,
        "hit_ci": _interval(fv.hit_rate_ci(hits, n, CI_LEVEL)),
        "mean_excess": mean_excess,
        "mean_excess_ci": _interval(fv.mean_ci_hac(excesses, lag, CI_LEVEL)) if excesses else None,
        "hac_lag": lag,
        # Current values (an e-value at any stopping time); the peak is shown for context only.
        "e_skill": skill_proc.final,
        "e_harm": harm_proc.final,
        "e_skill_peak": skill_proc.max_value,
        "e_harm_peak": harm_proc.max_value,
        "e_skill_crossed_strong": skill_proc.crossed(fv.STRONG_EVIDENCE_THRESHOLD),
        "state": "too_early",  # set across all types by _apply_e_bh
        "benchmarked": data.benchmarked,
        "benchmark_label": data.benchmark_label,
        "brier": brier,
        "bss": bss,
        "bss_ci": _interval(bss_ci),
        "n_stated_p": len(stated),
        "spiegelhalter_z": z,
        "reliability": reliability,
        "range_coverage": coverage,
        "calibration_caption": _calibration_caption(calls),
        "next_resolution_at": data.next_resolution_at.isoformat() if data.next_resolution_at else None,
        "note": data.note,
    }


def _apply_e_bh(stats: list[dict[str, Any]]) -> int:
    """Set each type's state from e-BH over every (type, direction) with data; returns the tests run."""
    tested = [(s, side) for s in stats if s["n"] > 0 and s["n_needed"] is not None for side in ("skill", "harm")]
    rejected = fv.e_bh([s[f"e_{side}"] for s, side in tested], FDR_LEVEL)
    found = {(id(s), side) for (s, side), r in zip(tested, rejected, strict=True) if r}
    for s in stats:
        s["n_tests"] = len(tested)
        if (id(s), "harm") in found:
            s["state"] = "harm"
        elif (id(s), "skill") in found:
            s["state"] = "skill"
        elif s["n"] < MIN_CALLS_FOR_STATE:
            s["state"] = "too_early"
        else:
            s["state"] = "no_evidence"
    return len(tested)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _collect(db: Session, user_id: str, now: datetime) -> tuple[dict[str, _TypeData], str]:
    benchmark = passive_core_symbol(db)
    return (
        {
            "ideas": _prediction_type(db, user_id, "ideas", now, benchmark),
            "advisor": _prediction_type(db, user_id, "advisor", now, benchmark),
            "mandates": _mandate_type(db, user_id, now),
            "regime": _regime_type(db),
        },
        benchmark,
    )


def _overall_state(stats: list[dict[str, Any]]) -> str:
    """Harm beats skill beats everything else; benchmarked types only."""
    states = [s["state"] for s in stats if s["benchmarked"]]
    if "harm" in states:
        return "harm"
    if "skill" in states:
        return "skill"
    if all(s == "too_early" for s in states):
        return "too_early"
    return "no_evidence"


def _about(n: int) -> int:
    """'~600', not '~617': the count needed is a rough planning number."""
    return int(round(n, -2)) if n >= 100 else n


def _headline(state: str, n_units: int, needed: int, first_due: datetime | None) -> str:
    target = round(TARGET_HIT_RATE * 100)
    if state == "skill":
        return f"Evidence of skill vs your ETF over {n_units} rebalance dates (after correcting for every look)."
    if state == "harm":
        return f"Evidence of harm: the calls have done worse than simply holding your ETF ({n_units} rebalance dates)."
    if state == "no_evidence":
        return (
            f"No evidence yet that the calls beat your ETF: {n_units} of ~{_about(needed)} independent "
            f"rebalance dates needed to tell a {target} % hit rate from a coin flip."
        )
    if n_units == 0:
        due = f" First resolutions due {first_due.date().isoformat()}." if first_due else ""
        return f"Too early to tell: nothing has resolved yet.{due}"
    return (
        f"Too early to tell: {n_units} of ~{_about(needed)} independent rebalance dates needed to tell "
        f"a {target} % hit rate from a coin flip."
    )


def build_verdict(db: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The Verdict tab: headline, three numbers and one evidence row per prediction type."""
    now = now or datetime.now(UTC)
    by_type, benchmark = _collect(db, user_id, now)
    stats = [_type_stats(by_type[key]) for key in TYPE_KEYS]
    n_tests = _apply_e_bh(stats)

    # Skill: one basket per rebalance date across the benchmark-relative types
    # (ideas + advisor; the advisor often re-picks Discover names, so per-pick
    # pooling would count the same bet twice).
    pooled = [c for key in ("ideas", "advisor") for c in by_type[key].calls]
    units = _units(pooled)
    excess = [u.excess for u in units if u.excess is not None]
    labels = sorted({by_type[k].benchmark_label for k in ("ideas", "advisor") if by_type[k].calls})
    skill = None
    if excess:
        horizons = [c.horizon_days for c in pooled if c.horizon_days]
        ci = fv.mean_ci_hac(excess, _hac_lag(units, max(horizons) if horizons else None), CI_LEVEL)
        skill = {
            "metric": "mean_excess_return_per_date",
            "value": sum(excess) / len(excess),
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
            "n": len(excess),
            "n_calls": len(pooled),
            "benchmark_label": labels[0] if len(labels) == 1 else f"your passive core ETF ({benchmark})",
        }

    needed = fv.calls_needed(TARGET_HIT_RATE)
    state = _overall_state(stats)
    n_units = len(units)
    first_due = _earliest_future([by_type[k].next_resolution_at for k in ("ideas", "advisor")], now)
    return {
        "headline": _headline(state, n_units, needed, first_due),
        "resolved_calls": sum(s["n_calls"] for s in stats),
        "resolved_units": n_units,
        "n_needed": needed,
        "target_hit_rate": TARGET_HIT_RATE,
        "n_tests": n_tests,
        "fdr_level": FDR_LEVEL,
        "skill": skill,
        "state": state,
        "types": stats,
        "frozen_at_issue": True,
        "method_note": METHOD_NOTE,
    }


def list_calls(
    db: Session,
    user_id: str,
    *,
    type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The Calls tab: resolved calls newest first, plus the five worst misses."""
    now = now or datetime.now(UTC)
    by_type, _benchmark = _collect(db, user_id, now)
    keys = [type] if type in TYPE_KEYS else list(TYPE_KEYS)
    calls = [c for key in keys for c in by_type[key].calls]
    calls.sort(key=lambda c: (c.resolved_at, c.id), reverse=True)
    misses = sorted((c for c in calls if c.excess is not None), key=lambda c: c.excess or 0.0)[:5]
    return {
        "type": type if type in TYPE_KEYS else None,
        "total": len(calls),
        "limit": limit,
        "offset": offset,
        "items": [c.as_dict() for c in calls[offset : offset + limit]],
        "worst_misses": [c.as_dict() for c in misses if (c.excess or 0.0) < 0],
        "frozen_at_issue": True,
    }
