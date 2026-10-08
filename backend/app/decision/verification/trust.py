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

**The verdict comes from the pre-registered calendar-time tests (ADR 0018,
``daily_tests.py``)**: ideas (F1, primary, sets the headline), ranking (F2,
primary, its own row) and advisor (F3, secondary), each its own e-BH family of
skill and harm (K = 2, so one rejection needs e >= 40). The per-date basket
statistics below stay on the page as descriptive numbers and a secondary,
legacy test:

* **Overlap.** A call's outcome covers ``horizon`` issue days, so the hit flags
  of consecutive dates overlap. The e-process is therefore the lag-h average of
  Henzi & Ziegel (arXiv:2103.08402), ``h`` = the longest horizon in issue days
  (21 trading days by default).
* **Excess return, not hit/miss.** The skill and harm tests bet on each date's
  basket excess return (clipped to +-``EXCESS_CLIP``), which uses the size of the
  win as well as its sign; the hit rate stays a descriptive number.
* **Legacy e-values are bounded below.** Stopping the lag-h average at a
  crossing is only safe when the calls still in flight are counted at their
  worst (Henzi & Ziegel, App. A.2), so the row also reports ``e_skill_lower``
  / ``e_harm_lower``: the value if every pending call resolved at the clip
  against it. It sets no state.
* **Mandates** have no naive benchmark, so the "hit" is the system's own
  grading; they show their numbers but never get a skill or harm verdict.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.decision.verification import daily_tests, factor_study
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
#: Null hypothesis of the hit-rate e-process used for the types that have no
#: excess return (mandates): a coin flip.
NULL_HIT_RATE = 0.5
#: The skill / harm tests bet on each date's basket excess return. It is clipped
#: to +-20 % before betting: over 21 trading days a +-20 % excess against a
#: world ETF is already an extreme day for an equal-weight basket, so clipping
#: rarely bites, but it bounds the bet. Validity then holds for the *clipped*
#: mean: H0 is "the mean clipped excess return is <= 0" (skill) or ">= 0" (harm).
EXCESS_CLIP = 0.20
#: Assumed true edge for the "time to know" number: +1 % excess per 21-day window
#: (about 12 % a year before noise) ...
ASSUMED_EDGE = 0.01
#: ... with 5 % noise (standard deviation of one date's basket excess return).
#: Not measured: there is no live data to estimate it from. It also sets the
#: regulariser of the first bets.
ASSUMED_SD = 0.05
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
#: Types measured against the ETF (each its own family, ADR 0018 §2).
TESTED_KEYS = ("ideas", "advisor")
#: Hypotheses per family: skill and harm.
N_TESTS = daily_tests.FAMILY_K
#: e-value a single rejection needs under e-BH: K / alpha.
EBH_THRESHOLD = N_TESTS / FDR_LEVEL
#: Which calendar-time family sets each type's state.
_FAMILY_OF = {"ideas": "F1", "advisor": "F3"}
#: Brier-skill bootstrap blocks: issue dates within this many weeks resample together.
CALIBRATION_BLOCK_WEEKS = 5
#: Effective (date-level) labels before any probability is stated (ADR 0018 §6).
MIN_N_EFF_FOR_PROBABILITY = 100
#: Holding period (trading days) assumed when a call carries none.
DEFAULT_HORIZON_DAYS = 21
TRADING_DAYS_PER_YEAR = 252

TYPE_KEYS = ("ideas", "advisor", "mandates", "regime")
TYPE_LABELS = {
    "ideas": "Ideas (Discover)",
    "advisor": "Advisor",
    "mandates": "Mandates",
    "regime": "Regime",
}

def _method_note() -> str:
    start = daily_tests.PRIMARY_TEST_START.isoformat()
    clip = round(daily_tests.CLIP * 100)
    return (
        "The verdict comes from tests fixed in advance (ADR 0018) before the results they judge. Each "
        "trading day, the open calls of a type form one portfolio held as issued, and its return against "
        "your passive core ETF that day is one observation (a calendar-time portfolio, so overlapping "
        "holding windows are not a problem). Each day is recorded once and never rewritten. An "
        f"anytime-valid e-process bets on these daily returns (clipped to +-{clip} %) from {start} on; "
        "skill and harm are tested together with e-BH at a 5 % false-discovery rate, so one verdict needs "
        f"an e-value of {round(daily_tests.THRESHOLD)}. The picks against the ETF set the headline. "
        "Discover's ranking of every scored stock is its own test, and the advisor's is secondary. The "
        "chance the edge is positive is a Bayesian summary with a sceptical prior, shown next to the "
        "test and never used as a verdict. Below, the per-date basket numbers are descriptive."
    )


def _legacy_method_note() -> str:
    needed = _needed_text(*_issue_days_needed(DEFAULT_HORIZON_DAYS))  # daily-issuing reference
    edge, sd = round(ASSUMED_EDGE * 100, 1), round(ASSUMED_SD * 100, 1)
    return (
        "The unit is the rebalance date: the picks issued on one day form an equal-weight basket, and "
        "the date is a hit when the basket beat your passive core ETF over the horizon (mandates: when "
        "the stated expectation held). Picks that stopped trading count, at their last close. Every "
        "number is frozen when the call was resolved. Intervals are 90 %: exact (Clopper-Pearson) for "
        "hit rates, Newey-West for the mean excess because holding windows overlap, block bootstrap for "
        "Brier skill. Evidence is an anytime-valid e-process that bets on each date's average excess "
        f"return (clipped to +-{round(EXCESS_CLIP * 100)} %) against zero, per type and direction; "
        "because holding windows overlap it is averaged over interleaved dates (Henzi & "
        "Ziegel), with the calls still in flight counted at their worst before it may cross. Mandates have "
        "no benchmark and get no verdict. "
        f"Assuming an edge of +{edge} % per 21 days with {sd} % noise, seeing it this way takes {needed} "
        "rebalance dates if one is issued every trading day, fewer for a weekly cadence (each page row "
        "states its own)."
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
    # Every resolved row that stated a range, with its issue and exit dates.
    ranges: list[fv.RangeOutcome] = field(default_factory=list)
    range_level: float | None = None
    next_resolution_at: datetime | None = None
    benchmark_label: str = ""
    benchmarked: bool = True
    note: str | None = None
    n_delisted: int = 0
    #: Resolved calls that could not be scored (no return or no benchmark price).
    n_unscored: int = 0
    #: Logged holds: resolved but not a directional bet, left out by design.
    n_holds: int = 0
    #: Issue dates of directional calls still in flight (for the legacy lower bound).
    pending_days: list[date] = field(default_factory=list)


def _coverage_dict(cov: fv.Coverage) -> dict[str, Any]:
    return {
        "k": cov.k,
        "n": cov.n,
        "rate": cov.rate,
        "ci_low": cov.ci_low,
        "ci_high": cov.ci_high,
        "nominal": cov.nominal,
    }


def _aci_dict(aci: fv.AciCoverage) -> dict[str, Any]:
    """The online conformal correction of the stated ranges (ADR 0018 §6)."""
    return {
        "active": aci.active,
        "matured_weeks": aci.matured_weeks,
        "weeks_needed": aci.weeks_needed,
        "alpha_target": aci.alpha_target,
        "alpha_now": aci.alpha_now,
        "gamma": aci.gamma,
        "widen_now": aci.widen_now,
        "per_date": [{"date": d.isoformat(), "inside": k, "n": n} for d, k, n in aci.per_date],
        "corrected": _coverage_dict(aci.corrected) if aci.corrected else None,
        "raw_same_dates": _coverage_dict(aci.raw_same_dates) if aci.raw_same_dates else None,
    }


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


def _issue_days_needed(h: int) -> tuple[int, bool]:
    """(issue dates, is_lower_bound) for the assumed edge with *h* chains; simulated, cached."""
    n = fv.issue_days_needed_mean(ASSUMED_EDGE, ASSUMED_SD, EXCESS_CLIP, N_TESTS, FDR_LEVEL, h)
    return (fv.SIM_MAX_DAYS, True) if n is None else (n, False)


def _cadence(units_days: list[date], horizon: int) -> dict[str, Any]:
    """Dates needed at the *observed* issue cadence (daily assumed below 5 dates), and in years."""
    h_eff, gap = fv.effective_lag(units_days, horizon)
    needed, lower = _issue_days_needed(h_eff)
    return {
        "n_needed": needed,
        "n_needed_is_lower_bound": lower,
        "h_eff": h_eff,
        "cadence_days": gap,
        "years_needed": needed * gap / TRADING_DAYS_PER_YEAR,
    }


def _about(n: int) -> int:
    """'~9,800', not '~9,783': the count needed is a rough planning number."""
    return int(round(n, -2)) if n >= 100 else n


def _needed_text(n: int, lower_bound: bool) -> str:
    return f"{'more than' if lower_bound else 'about'} {_about(n):,}"


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
            if (row.direction or "buy").lower() != "neutral" and row.predicted_at is not None:
                data.pending_days.append((_utc(row.predicted_at) or now).date())
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
        if row.outcome_status not in ("resolved", "delisted"):
            continue
        if row.realised_return is None:
            if direction == "neutral":
                data.n_holds += 1
            else:
                data.n_unscored += 1
            continue
        outcome = (row.score_json or {}).get("outcome") if isinstance(row.score_json, dict) else None
        if isinstance(outcome, dict) and outcome.get("benchmark"):
            measured_against.add(str(outcome["benchmark"]))
        stock_return = -float(row.realised_return) if direction in ("sell", "bear") else float(row.realised_return)
        if row.expected_return_low is not None and row.expected_return_high is not None:
            data.ranges.append(fv.RangeOutcome(
                issued=(_utc(row.predicted_at) or now).date(),
                matured=_exit_date(row).date(),
                low=float(row.expected_return_low),
                high=float(row.expected_return_high),
                outcome=stock_return,
            ))
        if direction == "neutral":
            data.n_holds += 1  # a logged hold: not a directional bet against the ETF
            continue
        if row.excess_return is None:
            data.n_unscored += 1  # no benchmark price recorded: counted and shown, never silently dropped
            continue
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

    horizons = [c.horizon_days for c in calls if c.horizon_days]
    horizon = max(horizons) if horizons else DEFAULT_HORIZON_DAYS
    unit_days = [u.day for u in units]
    # Overlapping holding windows: lag-h e-process (Henzi & Ziegel, arXiv:2103.08402).
    if data.key in TESTED_KEYS:
        # Bet on the date's basket excess return. A date with no price at all for any pick
        # carries no information about the size of the move: it enters as 0.
        obs = [u.excess if u.excess is not None else 0.0 for u in units]
        # Chains of non-overlapping windows for this issue schedule (treated as exogenous).
        chains = fv.chain_partition(unit_days, horizon)
        skill_proc = fv.e_process_mean(obs, horizon, EXCESS_CLIP, ASSUMED_SD, chains=chains)
        harm_proc = fv.e_process_mean(obs, horizon, EXCESS_CLIP, ASSUMED_SD, harm=True, chains=chains)
        n_chains = (max(chains) + 1) if chains else 0
        e_skill_lower, e_harm_lower = _lower_bounds(units, obs, data.pending_days, horizon) or (
            skill_proc.final, harm_proc.final,
        )
    else:
        n_chains = horizon  # mandates / regime: no excess return, only the descriptive hit-rate evidence
        skill_proc = fv.e_process_lag_h(flags, NULL_HIT_RATE, horizon)
        harm_proc = fv.e_process_lag_h_harm(flags, NULL_HIT_RATE, horizon)
        e_skill_lower, e_harm_lower = skill_proc.final, harm_proc.final

    excesses = [u.excess for u in units if u.excess is not None]
    mean_excess = sum(excesses) / len(excesses) if excesses else None
    lag = _hac_lag(units, max(horizons) if horizons else None)

    # Calibration metrics count issue dates, not calls (ADR 0018 §6): the
    # bootstrap resamples five-week blocks of dates, Z sums residuals per date,
    # and the reliability curve is CORP (PAV) with its Brier decomposition.
    stated = [(c.stated_p, 1.0 if c.hit else 0.0, c.issued_at.date()) for c in calls if c.stated_p is not None]
    brier = bss = None
    bss_ci: tuple[float, float] | None = None
    z = None
    reliability: list[dict[str, float]] = []
    corp: dict[str, float] | None = None
    n_eff_stated = len({d for _, _, d in stated})
    if stated:
        p_vals = [s[0] for s in stated]
        y_vals = [s[1] for s in stated]
        first = min(d for _, _, d in stated)
        blocks = [(d - first).days // (7 * CALIBRATION_BLOCK_WEEKS) for _, _, d in stated]
        skill = fv.brier_skill(p_vals, y_vals, level=CI_LEVEL, blocks=blocks)
        brier, bss = skill.brier, skill.skill
        bss_ci = (skill.ci_low, skill.ci_high) if skill.ci_low is not None and skill.ci_high is not None else None
        z = fv.spiegelhalter_z_clustered(p_vals, y_vals, [d for _, _, d in stated])
        if len(stated) >= fv.MIN_CALLS_FOR_RELIABILITY:
            fit = fv.corp_reliability(p_vals, y_vals)
            if fit is not None:
                reliability = fit.points
                corp = {"mcb": fit.mcb, "dsc": fit.dsc, "unc": fit.unc}

    coverage = None
    if data.ranges and data.range_level is not None:
        cov = fv.coverage([r.low <= r.outcome <= r.high for r in data.ranges], data.range_level, CI_LEVEL)
        coverage = {**_coverage_dict(cov), "aci": _aci_dict(fv.aci_coverage(data.ranges, data.range_level, CI_LEVEL))}

    call_hits = sum(1 for c in calls if c.hit)
    tested = data.key in TESTED_KEYS
    cad = _cadence(unit_days, horizon) if tested else None
    needed = cad["n_needed"] if cad else None
    lower_bound = cad["n_needed_is_lower_bound"] if cad else False
    return {
        "type": data.key,
        "label": TYPE_LABELS[data.key],
        # n, hits and the hit rate count rebalance dates, the independent unit.
        "n": n,
        "n_needed": needed,
        "n_needed_is_lower_bound": lower_bound,
        "horizon_days": horizon,
        "lag_h": n_chains,
        "h_eff": cad["h_eff"] if cad else None,
        "cadence_days": cad["cadence_days"] if cad else None,
        "years_needed": cad["years_needed"] if cad else None,
        "n_unscored": data.n_unscored,
        "n_holds": data.n_holds,
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
        "e_skill_lower": e_skill_lower,
        "e_harm_lower": e_harm_lower,
        "e_skill_crossed_strong": skill_proc.crossed(fv.STRONG_EVIDENCE_THRESHOLD),
        "state": "too_early",  # set from the calendar-time families by _apply_states
        "benchmarked": data.benchmarked,
        "benchmark_label": data.benchmark_label,
        "brier": brier,
        "bss": bss,
        "bss_ci": _interval(bss_ci),
        "n_stated_p": len(stated),
        "n_eff_stated": n_eff_stated,
        "spiegelhalter_z": z,
        "reliability": reliability,
        "corp": corp,
        "range_coverage": coverage,
        "calibration_caption": _calibration_caption(calls),
        "next_resolution_at": data.next_resolution_at.isoformat() if data.next_resolution_at else None,
        "note": data.note,
    }


def _lower_bounds(
    units: list[_Unit], obs: list[float], pending_days: list[date], horizon: int,
) -> tuple[float, float] | None:
    """Legacy e-values if every in-flight date resolved at its worst (Henzi & Ziegel, App. A.2).

    A pending date is appended to its chain at the clip against the
    hypothesis (-clip for skill, +clip for harm). Only these bounds may be
    read against a threshold while calls are in flight. ``None`` when nothing
    is in flight (the bound is then the current value).
    """
    resolved_days = {u.day for u in units}
    extra = sorted({d for d in pending_days if d not in resolved_days})
    if not extra:
        return None
    merged = sorted([(u.day, x, False) for u, x in zip(units, obs, strict=True)] + [(d, 0.0, True) for d in extra])
    chains = fv.chain_partition([m[0] for m in merged], horizon)
    worst_skill = [-EXCESS_CLIP if pend else x for _, x, pend in merged]
    worst_harm = [EXCESS_CLIP if pend else x for _, x, pend in merged]
    skill = fv.e_process_mean(worst_skill, horizon, EXCESS_CLIP, ASSUMED_SD, chains=chains)
    harm = fv.e_process_mean(worst_harm, horizon, EXCESS_CLIP, ASSUMED_SD, harm=True, chains=chains)
    return skill.final, harm.final


def _apply_states(stats: list[dict[str, Any]], tests: dict[str, Any]) -> int:
    """Each tested type takes the state of its calendar-time family (ADR 0018 §2).

    Mandates and regime never get a skill or harm verdict. The legacy per-date
    e-values sit beside the state and set none of it.
    """
    families = {f["family"]: f for f in tests["families"]}
    for s in stats:
        s["n_tests"] = N_TESTS
        family = families.get(_FAMILY_OF.get(s["type"], ""))
        if family is not None:
            s["state"] = family["state"]
            s["family"] = family["family"]
        else:
            s["state"] = "too_early" if s["n"] < MIN_CALLS_FOR_STATE else "no_evidence"
    return N_TESTS


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


def _primary(tests: dict[str, Any]) -> dict[str, Any]:
    """F1, the family that sets the headline (ADR 0018 §2)."""
    return next(f for f in tests["families"] if f["family"] == "F1")


def _days(n: int) -> str:
    return f"{n:,} trading day{'' if n == 1 else 's'}"


def _years_text(years: float) -> str:
    return f"{years:.1f}" if years < 10 else f"{years:.0f}"


def _time_to_know_phrase(f1: dict[str, Any]) -> str:
    """'most likely after about 19 years (1 in 10 paths: under 6 years; 9 in 10: under 42)'."""
    t = f1["time_to_know"]
    edge = round(t["assumption"] * 100, 1)
    assumption = f"an edge of +{edge:g} % per 21 days"
    if t["q50_years"] is None:
        cap = _years_text(t["max_days"] / TRADING_DAYS_PER_YEAR)
        return f"{assumption} would most likely take more than {cap} years to show"
    spread = ""
    if t["q10_years"] is not None:
        hi = f"{_years_text(t['q90_years'])}" if t["q90_years"] is not None else "more than " + _years_text(
            t["max_days"] / TRADING_DAYS_PER_YEAR
        )
        spread = f" (10-90 % range: {_years_text(t['q10_years'])} to {hi} years)"
    return f"{assumption} would most likely show after about {_years_text(t['q50_years'])} years{spread}"


def _headline(f1: dict[str, Any], today: date) -> str:
    n = f1["n_days"]
    e = f1["e_skill"] if f1["state"] == "skill" else f1["e_harm"]
    if f1["state"] == "skill":
        return (
            f"Evidence that Discover's picks beat your ETF: e = {e:,.0f} over {_days(n)} "
            "(pre-registered test, 5 % false-discovery rate)."
        )
    if f1["state"] == "harm":
        return (
            f"Evidence of harm: Discover's picks have done worse than simply holding your ETF "
            f"(e = {e:,.0f} over {_days(n)})."
        )
    if n == 0:
        start = date.fromisoformat(f1["start"])
        when = f"starts on {start.isoformat()}" if today < start else f"started on {start.isoformat()}"
        return f"Too early to tell: the pre-registered test {when}; no trading day with open picks recorded yet."
    if f1["state"] == "no_evidence":
        return f"No evidence yet that the picks beat your ETF: {_days(n)} so far; {_time_to_know_phrase(f1)}."
    return f"Too early to tell: {_days(n)} so far; {_time_to_know_phrase(f1)}."


def build_verdict(db: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The Verdict tab: headline, three numbers and one evidence row per prediction type."""
    now = now or datetime.now(UTC)
    by_type, benchmark = _collect(db, user_id, now)
    stats = [_type_stats(by_type[key]) for key in TYPE_KEYS]
    tests = daily_tests.build_daily_tests(db, user_id)
    tests["factor_neutral"] = factor_study.factor_neutral_summary(db, user_id)
    n_tests = _apply_states(stats, tests)
    f1 = _primary(tests)

    # Descriptive: one basket per rebalance date of the picks (F1's calls; the
    # advisor is its own family and never pooled with them).
    pooled = list(by_type["ideas"].calls)
    units = _units(pooled)
    excess = [u.excess for u in units if u.excess is not None]
    labels = sorted({by_type["ideas"].benchmark_label} if pooled else set())
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

    horizons_all = [c.horizon_days for k in TESTED_KEYS for c in by_type[k].calls if c.horizon_days]
    horizon = max(horizons_all) if horizons_all else DEFAULT_HORIZON_DAYS
    cad = _cadence([u.day for u in units], horizon)  # legacy: the picks' issue schedule
    needed, lower_bound = cad["n_needed"], cad["n_needed_is_lower_bound"]
    state = f1["state"]
    n_units = len(units)
    first_due = _earliest_future([by_type[k].next_resolution_at for k in ("ideas", "advisor")], now)
    return {
        "headline": _headline(f1, now.date()),
        "first_resolution_due": first_due.isoformat() if first_due else None,
        "daily_tests": tests,
        "resolved_calls": sum(s["n_calls"] for s in stats),
        "resolved_units": n_units,
        "n_needed": needed,
        "n_needed_is_lower_bound": lower_bound,
        "h_eff": cad["h_eff"],
        "cadence_days": cad["cadence_days"],
        "years_needed": cad["years_needed"],
        "target_hit_rate": TARGET_HIT_RATE,
        "n_tests": n_tests,
        "fdr_level": FDR_LEVEL,
        "ebh_threshold": EBH_THRESHOLD,
        "assumed_edge": ASSUMED_EDGE,
        "assumed_sd": ASSUMED_SD,
        "excess_clip": EXCESS_CLIP,
        "horizon_days": horizon,
        "min_calls_for_state": MIN_CALLS_FOR_STATE,
        "n_unscored": sum(s["n_unscored"] for s in stats),
        "min_calls_for_reliability": fv.MIN_CALLS_FOR_RELIABILITY,
        "min_n_eff_for_probability": MIN_N_EFF_FOR_PROBABILITY,
        "min_units_for_interval": fv.MIN_UNITS_FOR_HAC,
        "min_calls_for_interval": fv.MIN_CALLS_FOR_CI,
        "skill": skill,
        "state": state,
        "types": stats,
        "frozen_at_issue": True,
        "method_note": _method_note(),
        "legacy_method_note": _legacy_method_note(),
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
    horizons = [c.horizon_days for c in calls if c.horizon_days and c.type in TESTED_KEYS]
    return {
        "horizon_days": max(horizons) if horizons else DEFAULT_HORIZON_DAYS,
        "type": type if type in TYPE_KEYS else None,
        "total": len(calls),
        "limit": limit,
        "offset": offset,
        "items": [c.as_dict() for c in calls[offset : offset + limit]],
        "worst_misses": [c.as_dict() for c in misses if (c.excess or 0.0) < 0],
        "frozen_at_issue": True,
    }
