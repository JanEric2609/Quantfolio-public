"""The capital ladder (ADR 0025, plan 2026-11-10 WS4).

The question is no longer "has the candidate proven skill?" (no test can show
a 2-4 %/yr edge at 8-12 % tracking error within two years) but "how much real
money may this candidate's picks hold, given the evidence?". The ladder caps
the cost of an unproven idea; it is not a skill detector, and in simulation it
promotes almost nothing within five years unless the edge is large.

Candidates (two slots, pi = 1/2 each): ``llm``, the advisor champion lineage,
and ``rules``, Mandate B. A candidate is a frozen configuration hashed with
SHA-256; any change (a champion promotion, a paper reset, a new model) starts
a new epoch in the same slot. Epoch e gets testing wealth pi_e = 1/2 / e^2 and
the thresholds 1/(0.2 pi_e) = 10 e^2 (Earned) and 1/(0.05 pi_e) = 40 e^2
(Proven). By Ville's inequality, summed over every epoch of both slots
(sum 1/e^2 = 1.645), a no-skill candidate ever reaches Earned with probability
at most 0.33 and Proven at most 0.08; epoch 1 alone 0.2 and 0.05.

Measurement, nightly: x_t = r_sleeve - r_EUNL in EUR on identical dates, from
the paper snapshots (no external cash flows in a paper sleeve, so NAV returns
are time-weighted) after the fees and spreads the paper engine charges. The
tax drag is left out (ADR 0025). sigma_t is an EWMA of x^2 with a 126-day
half-life, lagged one day. Only returns after the epoch's birth count.

Evidence: the skill e-process E_t = mean over lambda of prod(1 + lambda Y_s),
Y = clip(x / sigma, -1, 1); the harm e-process H_t the same with -x; the
relative drawdown of cumulative x against 2.8 TE sqrt(years). A confidence
sequence and a Bayesian panel are display only.

Stage changes take effect at the next monthly plan: :func:`ladder_weight`
reads the stage on the last evidence day before the current month.

Advisory only. The ladder sets the satellite target of the monthly plan;
nothing here places orders.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Sequence

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorGraduationState,
    AdvisorStrategy,
    AdvisorGraduationTransition,
    DiscoveryPrediction,
    LadderCandidate,
    LadderEvidenceDaily,
    PaperPortfolio,
    PaperSnapshot,
)
from app.foundation.models.entities._core import now_utc
from app.foundation.capital_ladder import (  # noqa: F401 - re-exported for the facade
    AUTHORISATION_MONTHS,
    BRIER_VETO,
    CS_ALPHA,
    DEFAULTS,
    DRAWDOWN_BAND_K,
    EARNED_ALPHA,
    EARNED_WEIGHT_PCT,
    EWMA_HALFLIFE_DAYS,
    HARM_THRESHOLD,
    LAMBDAS,
    NULL_BAND_Z,
    PRIOR_ALPHA_MEAN,
    PRIOR_ALPHA_SD,
    PROVEN_ALPHA,
    PROVEN_WEIGHT_MAX_PCT,
    SIGMA_BURN_IN,
    SLOT_LLM,
    SLOT_PI,
    SLOT_RULES,
    STAGE_NAMES,
    TRADING_DAYS_PER_YEAR,
    TUITION_CAP_PCT,
    TUITION_MIN_DAYS,
    aware,
    epoch_pi,
    funding_candidate,
    ladder_funding,
    ladder_settings,
    ladder_weight,
    month_start,
    stage_on,
    thresholds,
    tuition_weight_pct,
    weight_pct,
)

logger = logging.getLogger(__name__)

def candidate_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Evidence -----------------------------------------------------------------


def lagged_sigma(x: Sequence[float] | np.ndarray, halflife: int = EWMA_HALFLIFE_DAYS, burn_in: int = SIGMA_BURN_IN) -> np.ndarray:
    """sigma_t from x_1..x_{t-1}: EWMA of x^2 (zero-mean, the null), NaN in burn-in."""
    arr = np.asarray(x, dtype=float)
    out = np.full(len(arr), np.nan)
    decay = 0.5 ** (1.0 / halflife)
    var = None
    for t in range(len(arr)):
        if t >= burn_in and var is not None and var > 0:
            out[t] = math.sqrt(var)
        sq = arr[t] ** 2
        if t + 1 == burn_in:
            var = float(np.mean(arr[:burn_in] ** 2))
        elif var is not None:
            var = decay * var + (1.0 - decay) * sq
    return out


def e_processes(
    x: Sequence[float] | np.ndarray, sigma: Sequence[float] | np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(E_t, H_t): the skill and harm betting e-processes, one value per day."""
    arr = np.asarray(x, dtype=float)
    sig = np.asarray(sigma, dtype=float)
    y = np.where(np.isfinite(sig) & (sig > 0), np.clip(arr / np.where(sig > 0, sig, 1.0), -1.0, 1.0), 0.0)
    lam = np.asarray(LAMBDAS)[:, None]
    skill = np.cumprod(1.0 + lam * y[None, :], axis=1).mean(axis=0)
    harm = np.cumprod(1.0 - lam * y[None, :], axis=1).mean(axis=0)
    return skill, harm


def relative_drawdown(x: Sequence[float] | np.ndarray) -> np.ndarray:
    cum = np.cumsum(np.asarray(x, dtype=float))
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return peak - cum


def tracking_error(x: Sequence[float] | np.ndarray) -> float | None:
    arr = np.asarray(x, dtype=float)
    if len(arr) < 2:
        return None
    return float(arr.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def drawdown_band(te_annual: float | None, n_days: int) -> float | None:
    if te_annual is None or n_days <= 0:
        return None
    return DRAWDOWN_BAND_K * te_annual * math.sqrt(n_days / TRADING_DAYS_PER_YEAR)


def confidence_sequence(
    x: Sequence[float] | np.ndarray, sigma: Sequence[float] | np.ndarray
) -> dict[str, float] | None:
    """Display only: a normal-mixture confidence sequence for the annual alpha."""
    arr = np.asarray(x, dtype=float)
    sig = np.asarray(sigma, dtype=float)
    ok = np.isfinite(sig) & (sig > 0)
    t = int(ok.sum())
    if t < SIGMA_BURN_IN:
        return None
    s, v = float(arr[ok].sum()), float((sig[ok] ** 2).sum())
    rho = float(np.mean(sig[ok] ** 2)) * TUITION_MIN_DAYS
    radius = math.sqrt((v + rho) * (2 * math.log(1 / CS_ALPHA) + math.log((v + rho) / rho)))
    return {
        "alpha_low": (s - radius) / t * TRADING_DAYS_PER_YEAR,
        "alpha_high": (s + radius) / t * TRADING_DAYS_PER_YEAR,
        "level": 1 - CS_ALPHA,
    }


def bayesian_panel(x: Sequence[float] | np.ndarray) -> dict[str, Any] | None:
    """Display only: N(-1 %, 3 %) prior on annual alpha, posterior and 1/4-Kelly weight."""
    arr = np.asarray(x, dtype=float)
    te = tracking_error(arr)
    if te is None or te <= 0:
        return None
    years = len(arr) / TRADING_DAYS_PER_YEAR
    obs = float(arr.mean() * TRADING_DAYS_PER_YEAR)
    se2 = te**2 / years
    prior_prec, data_prec = 1 / PRIOR_ALPHA_SD**2, 1 / se2
    post_var = 1 / (prior_prec + data_prec)
    post_mean = post_var * (PRIOR_ALPHA_MEAN * prior_prec + obs * data_prec)
    kelly = max(0.0, post_mean) / te**2 / 4.0
    return {
        "prior_mean": PRIOR_ALPHA_MEAN,
        "prior_sd": PRIOR_ALPHA_SD,
        "posterior_mean": post_mean,
        "posterior_sd": math.sqrt(post_var),
        "quarter_kelly_weight": min(kelly, PROVEN_WEIGHT_MAX_PCT / 100.0),
        "note": (
            "The prior expects a small negative alpha; with a few years of daily data at this "
            "tracking error the posterior barely moves, so the weight stays near 0 for years."
        ),
    }


# --- Stage machine ------------------------------------------------------------


@dataclass
class StageInputs:
    days: int
    e_skill: float
    e_harm: float
    cum_x: float
    rel_dd: float
    dd_band: float | None
    null_band: float | None
    brier: float | None
    vetoed: bool
    earned_at: float
    proven_at: float


def stage_for(prev: int, s: StageInputs) -> tuple[int, str, bool]:
    """(stage, reason, new_epoch). Demotion to stage 0 starts a new epoch."""
    if prev >= 1:
        if s.vetoed:
            return 0, "owner veto", True
        if s.e_harm >= HARM_THRESHOLD:
            return 0, f"harm e-value {s.e_harm:.1f} >= {HARM_THRESHOLD:g}", True
        if s.dd_band is not None and s.rel_dd > s.dd_band:
            return 0, f"relative drawdown {s.rel_dd:.1%} beyond the {s.dd_band:.1%} band", True
        if prev >= 2 and s.e_skill < 1.0:
            return 1, f"skill e-value {s.e_skill:.2f} fell below 1", False
    eligible = (
        s.days >= TUITION_MIN_DAYS
        and s.e_harm < HARM_THRESHOLD
        and (s.null_band is None or s.cum_x >= -s.null_band)
        and (s.brier is None or s.brier <= BRIER_VETO)
        and not s.vetoed
    )
    if prev == 0:
        if not eligible:
            return 0, _why_not(s), False
        prev = 1
    if s.e_skill >= s.proven_at:
        return 3, f"skill e-value {s.e_skill:.1f} >= {s.proven_at:g}", False
    if s.e_skill >= s.earned_at or prev >= 2:
        return max(prev, 2), f"skill e-value {s.e_skill:.1f} >= {s.earned_at:g}", False
    return 1, "tuition: paired returns long enough, no harm signal", False


def _why_not(s: StageInputs) -> str:
    if s.vetoed:
        return "owner veto"
    if s.days < TUITION_MIN_DAYS:
        return f"{s.days} of {TUITION_MIN_DAYS} trading days of paired returns"
    if s.e_harm >= HARM_THRESHOLD:
        return f"harm e-value {s.e_harm:.1f} >= {HARM_THRESHOLD:g}"
    if s.null_band is not None and s.cum_x < -s.null_band:
        return "cumulative excess below the 95 % null band"
    if s.brier is not None and s.brier > BRIER_VETO:
        return f"calibration worse than a coin flip (Brier {s.brier:.3f})"
    return "not eligible"


def next_step(stage: int, s: StageInputs) -> str:
    """Plain sentence on what moves the candidate next."""
    if stage == 0:
        if s.days < TUITION_MIN_DAYS:
            return f"Tuition needs {TUITION_MIN_DAYS - s.days} more trading days of paired returns, with no harm signal."
        return _why_not(s)
    if stage == 1:
        return f"Earned needs the skill e-value at {s.earned_at:g} (now {s.e_skill:.2f})."
    if stage == 2:
        return f"Proven needs the skill e-value at {s.proven_at:g} (now {s.e_skill:.2f})."
    return "Top stage. It stays while the skill e-value is above 1 and no harm or drawdown signal fires."


# --- Candidates -----------------------------------------------------------------


def llm_candidate_config(db: Session, champion: AdvisorStrategy) -> dict[str, Any]:
    """The frozen configuration of the LLM slot, hashed into ``config_hash``.

    Model and prompt come from the cohort spec the cycle stamps on each of its
    predictions (ADR 0018 §10): the prompt-template hash and the served model
    file. Universe, sizing, cost model and cadence are the cycle's own.
    """
    latest = (
        db.query(DiscoveryPrediction.provenance_json)
        .filter(DiscoveryPrediction.portfolio_id == champion.portfolio_id,
                DiscoveryPrediction.provenance_json.isnot(None))
        .order_by(DiscoveryPrediction.predicted_at.desc())
        .first()
    )
    spec = ((latest[0] or {}).get("cohort_spec") or {}) if latest else {}
    llm = spec.get("llm") or {}
    return {
        "strategy_id": champion.id,
        "strategy_config": champion.config_json or {},
        "model": llm.get("model_path"),
        "prompt_template_hash": spec.get("prompt_template_hash"),
        "universe": "discover_shortlist",
        "sizing": "advisor_cycle_defaults",
        "cost_model": "paper_engine_fees_and_spreads",
        "cadence": "daily",
    }


def _champion(db: Session, user_id: str) -> AdvisorStrategy | None:
    return (
        db.query(AdvisorStrategy)
        .filter(AdvisorStrategy.user_id == user_id, AdvisorStrategy.role == "champion")
        .one_or_none()
    )


def _llm_spec(db: Session, user_id: str) -> dict[str, Any] | None:
    champion = _champion(db, user_id)
    if champion is None or not champion.portfolio_id:
        return None
    portfolio = db.get(PaperPortfolio, champion.portfolio_id)
    if portfolio is None:
        return None
    born = max(
        aware(portfolio.inception_at or portfolio.created_at),
        aware(champion.promoted_at or champion.created_at),
    )
    return {
        "kind": "advisor", "ref_id": champion.id, "portfolio_id": portfolio.id,
        "config": llm_candidate_config(db, champion), "born": born,
    }


def _rules_spec(db: Session, user_id: str) -> dict[str, Any] | None:
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == "B")
        .one_or_none()
    )
    if portfolio is None:
        return None
    return {
        "kind": "mandate", "ref_id": portfolio.id, "portfolio_id": portfolio.id,
        "config": {"mandate": "B", "rules": portfolio.mandate_config_json, "cadence": "weekly"},
        "born": aware(portfolio.inception_at or portfolio.created_at),
    }


SPECS: dict[str, Callable[[Session, str], dict[str, Any] | None]] = {
    SLOT_LLM: _llm_spec,
    SLOT_RULES: _rules_spec,
}


def active_candidate(db: Session, user_id: str, slot: str) -> LadderCandidate | None:
    return (
        db.query(LadderCandidate)
        .filter(LadderCandidate.user_id == user_id, LadderCandidate.slot == slot,
                LadderCandidate.retired_at.is_(None))
        .order_by(LadderCandidate.epoch.desc())
        .first()
    )


def _new_epoch(db: Session, user_id: str, slot: str, spec: dict[str, Any], born: datetime) -> LadderCandidate:
    last = (
        db.query(LadderCandidate.epoch)
        .filter(LadderCandidate.user_id == user_id, LadderCandidate.slot == slot)
        .order_by(LadderCandidate.epoch.desc())
        .first()
    )
    epoch = (last[0] + 1) if last else 1
    row = LadderCandidate(
        user_id=user_id, slot=slot, kind=spec["kind"], ref_id=spec["ref_id"],
        portfolio_id=spec["portfolio_id"], config_hash=candidate_hash(spec["config"]),
        epoch=epoch, pi=epoch_pi(slot, epoch), born_at=born, stage=0, stage_since=born,
    )
    db.add(row)
    db.flush()
    return row


def register_candidate(db: Session, user_id: str, slot: str, *, now: datetime | None = None) -> LadderCandidate | None:
    """The slot's active candidate, starting a new epoch when its configuration changed."""
    spec = SPECS[slot](db, user_id)
    if spec is None:
        return None
    ref = now or now_utc()
    current = active_candidate(db, user_id, slot)
    digest = candidate_hash(spec["config"])
    if current is not None:
        changed = (
            current.config_hash != digest
            or current.ref_id != spec["ref_id"]
            or current.portfolio_id != spec["portfolio_id"]
            or spec["born"] > aware(current.born_at)  # a paper reset is a new epoch
        )
        expired = aware(current.born_at) + timedelta(days=round(AUTHORISATION_MONTHS * 30.44)) <= ref
        if not changed and not expired:
            return current
        reason = "configuration changed" if changed else f"authorisation expired after {AUTHORISATION_MONTHS} months"
        _retire(db, current, ref, reason)
        return _new_epoch(db, user_id, slot, spec, spec["born"] if changed else ref)
    return _new_epoch(db, user_id, slot, spec, spec["born"])


def _retire(db: Session, candidate: LadderCandidate, when: datetime, reason: str) -> None:
    candidate.retired_at = when
    if candidate.stage:
        _transition(db, candidate, candidate.stage, 0, f"new epoch: {reason}")
    candidate.stage = 0


# --- Measurement --------------------------------------------------------------


def paired_excess(
    db: Session,
    candidate: LadderCandidate,
    start: date,
    end: date,
    *,
    bench_closes: dict[str, float] | None = None,
) -> tuple[list[date], list[float]]:
    """(days, x) with x = sleeve return - benchmark EUR return on identical dates."""
    nav = {
        d: float(v)
        for d, v in db.query(PaperSnapshot.date, PaperSnapshot.total_value)
        .filter(PaperSnapshot.portfolio_id == candidate.portfolio_id,
                PaperSnapshot.date >= start, PaperSnapshot.date <= end)
        .all()
        if v is not None and float(v) > 0
    }
    if bench_closes is None:
        from app.foundation.eur_prices import benchmark_ticker, eur_closes

        bench_closes = eur_closes(db, benchmark_ticker(db), days=(end - start).days + 10)
    bench = {date.fromisoformat(k): float(v) for k, v in bench_closes.items() if v and float(v) > 0}
    days = sorted(d for d in nav if d in bench and start <= d <= end)
    out_days, xs = [], []
    for prev, cur in zip(days, days[1:]):
        xs.append((nav[cur] / nav[prev] - 1.0) - (bench[cur] / bench[prev] - 1.0))
        out_days.append(cur)
    return out_days, xs


def _brier(db: Session, candidate: LadderCandidate) -> float | None:
    if candidate.kind != "advisor":
        return None
    rows = (
        db.query(DiscoveryPrediction.conviction, DiscoveryPrediction.excess_return)
        .filter(DiscoveryPrediction.portfolio_id == candidate.portfolio_id,
                DiscoveryPrediction.outcome_status == "resolved",
                DiscoveryPrediction.direction.in_(("buy", "sell")),
                DiscoveryPrediction.predicted_at >= candidate.born_at,
                DiscoveryPrediction.conviction.isnot(None),
                DiscoveryPrediction.excess_return.isnot(None))
        .all()
    )
    if not rows:
        return None
    return float(np.mean([(float(p) - (1.0 if float(e) > 0 else 0.0)) ** 2 for p, e in rows]))


@dataclass
class CandidateEvidence:
    days: list[date]
    x: list[float]
    sigma: list[float]
    e_skill: list[float]
    e_harm: list[float]
    rel_dd: list[float]
    te: float | None
    brier: float | None
    extras: dict[str, Any] = field(default_factory=dict)


def evidence_for(db: Session, candidate: LadderCandidate, end: date, *, bench_closes: dict[str, float] | None = None) -> CandidateEvidence:
    start = aware(candidate.born_at).date()
    days, x = paired_excess(db, candidate, start, end, bench_closes=bench_closes)
    sigma = lagged_sigma(x)
    skill, harm = e_processes(x, sigma) if x else (np.array([]), np.array([]))
    return CandidateEvidence(
        days=days, x=x, sigma=sigma.tolist(), e_skill=skill.tolist(), e_harm=harm.tolist(),
        rel_dd=relative_drawdown(x).tolist() if x else [], te=tracking_error(x),
        brier=_brier(db, candidate),
        extras={"confidence_sequence": confidence_sequence(x, sigma), "bayes": bayesian_panel(x)},
    )


def _inputs(ev: CandidateEvidence, i: int, candidate: LadderCandidate, vetoed: bool) -> StageInputs:
    n = i + 1
    te = tracking_error(ev.x[:n])
    earned, proven = thresholds(candidate.slot, candidate.epoch)
    null_band = NULL_BAND_Z * (te / math.sqrt(TRADING_DAYS_PER_YEAR)) * math.sqrt(n) if te else None
    return StageInputs(
        days=n, e_skill=ev.e_skill[i], e_harm=ev.e_harm[i], cum_x=float(np.sum(ev.x[:n])),
        rel_dd=ev.rel_dd[i], dd_band=drawdown_band(te, n), null_band=null_band,
        brier=ev.brier, vetoed=vetoed, earned_at=earned, proven_at=proven,
    )


def _transition(db: Session, candidate: LadderCandidate, from_stage: int, to_stage: int, reason: str) -> None:
    db.add(AdvisorGraduationTransition(
        user_id=candidate.user_id,
        strategy_id=candidate.ref_id if candidate.kind == "advisor" else None,
        from_graduated=from_stage >= 2,
        to_graduated=to_stage >= 2,
        reason=f"ladder {candidate.slot} epoch {candidate.epoch}: {STAGE_NAMES[from_stage]} -> "
               f"{STAGE_NAMES[to_stage]} ({reason})",
        criteria_json={"ladder": True, "slot": candidate.slot, "epoch": candidate.epoch,
                       "from_stage": from_stage, "to_stage": to_stage, "candidate_id": candidate.id},
    ))


def _update_candidate(
    db: Session, candidate: LadderCandidate, end: date, vetoed: bool,
    *, bench_closes: dict[str, float] | None = None,
) -> LadderCandidate | None:
    """Step one candidate through its new evidence days. Returns a successor on demotion."""
    ev = evidence_for(db, candidate, end, bench_closes=bench_closes)
    stored = {
        r.day: r
        for r in db.query(LadderEvidenceDaily).filter(LadderEvidenceDaily.candidate_id == candidate.id).all()
    }
    stage = 0
    last_day = max(stored) if stored else None
    if last_day is not None:
        stage = stored[last_day].stage
    for i, day in enumerate(ev.days):
        row = stored.get(day)
        if row is None or (last_day is not None and day > last_day):
            inputs = _inputs(ev, i, candidate, vetoed and day == ev.days[-1])
            new_stage, reason, new_epoch = stage_for(stage, inputs)
            if new_stage != stage:
                _transition(db, candidate, stage, new_stage, reason)
                candidate.stage_since = datetime.combine(day, datetime.min.time(), UTC)
            stage = new_stage
            if row is None:
                row = LadderEvidenceDaily(candidate_id=candidate.id, day=day)
                db.add(row)
                stored[day] = row
            row.stage = stage
            if new_epoch:
                _fill(row, ev, i)
                candidate.stage = 0
                retired_at = datetime.combine(day, datetime.min.time(), UTC) + timedelta(days=1)
                candidate.retired_at = retired_at
                spec = SPECS[candidate.slot](db, candidate.user_id)
                db.flush()
                if spec is None:
                    return None
                return _new_epoch(db, candidate.user_id, candidate.slot, spec, retired_at)
        _fill(row, ev, i)
    candidate.stage = stage
    return None


def _fill(row: LadderEvidenceDaily, ev: CandidateEvidence, i: int) -> None:
    sig = ev.sigma[i]
    row.x = ev.x[i]
    row.sigma = None if sig is None or not math.isfinite(sig) else sig
    row.e_skill = ev.e_skill[i]
    row.e_harm = ev.e_harm[i]
    row.rel_drawdown = ev.rel_dd[i]
    row.te = tracking_error(ev.x[: i + 1])


def run_ladder(
    db: Session,
    user_id: str,
    now: datetime | None = None,
    *,
    bench_closes: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Nightly: register both slots, step their evidence, persist. Returns ladder_state."""
    ref = now or now_utc()
    end = ref.date() - timedelta(days=1)
    vetoed = bool(ladder_settings(db)["ladder_owner_veto"])
    bench = bench_closes
    if bench is None:
        try:
            from app.foundation.eur_prices import benchmark_ticker, eur_closes

            bench = eur_closes(db, benchmark_ticker(db), days=(365 * AUTHORISATION_MONTHS) // 12 + 30)
        except Exception:  # noqa: BLE001 - no benchmark: nothing to measure tonight
            logger.warning("ladder: benchmark closes unavailable", exc_info=True)
            bench = {}
    for slot in SPECS:
        candidate = register_candidate(db, user_id, slot, now=ref)
        guard = 0
        while candidate is not None and guard < 5:
            candidate = _update_candidate(db, candidate, end, vetoed, bench_closes=bench)
            guard += 1
    _sync_state(db, user_id)
    db.commit()
    return ladder_state(db, user_id, now=ref)


def _sync_state(db: Session, user_id: str) -> None:
    """Keep ``AdvisorGraduationState`` as the one switch: graduated = a stage >= 2 now."""
    best = funding_candidate(db, user_id, as_of=None)
    state = db.query(AdvisorGraduationState).filter(AdvisorGraduationState.user_id == user_id).one_or_none()
    if state is None:
        state = AdvisorGraduationState(user_id=user_id, graduated=False)
        db.add(state)
    graduated = best is not None and best[1] >= 2
    if bool(state.graduated) != graduated:
        state.graduated = graduated
        state.since = now_utc() if graduated else None
        state.last_transition_at = now_utc()
        state.last_reason = "capital ladder: " + (f"{best[0].slot} at stage {best[1]}" if best else "no candidate")
    state.details_json = {"source": "capital_ladder", "funding": best[0].id if best else None}


def ladder_state(db: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Per candidate: stage, weight, E, H, TE, drawdown vs band, days, next step, history."""
    ref = now or now_utc()
    out: list[dict[str, Any]] = []
    for slot in SPECS:
        candidate = active_candidate(db, user_id, slot)
        if candidate is None:
            continue
        rows = (
            db.query(LadderEvidenceDaily)
            .filter(LadderEvidenceDaily.candidate_id == candidate.id)
            .order_by(LadderEvidenceDaily.day.asc())
            .all()
        )
        earned, proven = thresholds(slot, candidate.epoch)
        last = rows[-1] if rows else None
        n = len(rows)
        te = last.te if last else None
        band = drawdown_band(te, n)
        x = [r.x for r in rows if r.x is not None]
        cum = float(np.sum(x)) if x else 0.0
        inputs = StageInputs(
            days=n,
            e_skill=float(last.e_skill) if last and last.e_skill is not None else 1.0,
            e_harm=float(last.e_harm) if last and last.e_harm is not None else 1.0,
            cum_x=cum,
            rel_dd=float(last.rel_drawdown) if last and last.rel_drawdown is not None else 0.0,
            dd_band=band,
            null_band=(NULL_BAND_Z * te / math.sqrt(TRADING_DAYS_PER_YEAR) * math.sqrt(n)) if te else None,
            brier=_brier(db, candidate), vetoed=bool(ladder_settings(db)["ladder_owner_veto"]),
            earned_at=earned, proven_at=proven,
        )
        stage = int(candidate.stage or 0)
        sigma = [r.sigma if r.sigma is not None else float("nan") for r in rows]
        out.append({
            "candidate_id": candidate.id,
            "slot": slot,
            "kind": candidate.kind,
            "ref_id": candidate.ref_id,
            "portfolio_id": candidate.portfolio_id,
            "config_hash": candidate.config_hash,
            "epoch": candidate.epoch,
            "pi": candidate.pi,
            "born_at": aware(candidate.born_at).isoformat(),
            "stage": stage,
            "stage_name": STAGE_NAMES[stage],
            "stage_since": aware(candidate.stage_since).isoformat() if candidate.stage_since else None,
            "weight_pct": weight_pct(db, candidate, stage, None),
            "e_skill": inputs.e_skill,
            "e_harm": inputs.e_harm,
            "thresholds": {"earned": earned, "proven": proven, "harm": HARM_THRESHOLD},
            "tracking_error": te,
            "relative_drawdown": inputs.rel_dd,
            "drawdown_band": band,
            "days_of_evidence": n,
            "tuition_min_days": TUITION_MIN_DAYS,
            "brier": inputs.brier,
            "vetoed": inputs.vetoed,
            "next_stage": STAGE_NAMES[min(stage + 1, 3)] if stage < 3 else None,
            "next_step": next_step(stage, inputs),
            "expires_at": (aware(candidate.born_at) + timedelta(days=round(AUTHORISATION_MONTHS * 30.44))).isoformat(),
            "confidence_sequence": confidence_sequence(x, sigma),
            "bayes": bayesian_panel(x),
            "history": [
                {"day": r.day.isoformat(), "e_skill": r.e_skill, "e_harm": r.e_harm,
                 "rel_drawdown": r.rel_drawdown, "stage": r.stage}
                for r in rows
            ],
        })
    funding = ladder_funding(db, user_id, today=ref.date())
    return {
        "candidates": out,
        "funding": funding,
        "satellite_weight_pct": funding["weight_pct"] if funding else 0.0,
        "epoch_guarantee": {
            "earned_ever_max": round(EARNED_ALPHA * sum(1 / e**2 for e in range(1, 10_000)), 3),
            "proven_ever_max": round(PROVEN_ALPHA * sum(1 / e**2 for e in range(1, 10_000)), 3),
            "earned_epoch1": EARNED_ALPHA,
            "proven_epoch1": PROVEN_ALPHA,
        },
        "note": (
            "The ladder caps what an unproven idea may cost; it is not a skill detector. At realistic "
            "edges it moves nothing past Tuition for years. Stage changes apply at the next monthly plan."
        ),
        "estimate": True,
        "not_tax_advice": True,
        "not_financial_advice": True,
    }

