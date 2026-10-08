"""Trust API — "Can I trust it?" (frozen-call verdicts).

GET /api/trust/verdict — headline, three numbers and one evidence row per prediction type
GET /api/trust/calls   — resolved calls, newest first, with the five worst misses
GET /api/trust/ranking — descriptive shadow-ledger metrics of Discover's ranking (ADR 0018 §5)
GET /api/trust/history — the historical panel: factor tilt, ranking model, satellite (simulated, not live)

Both read the immutable prediction ledgers for the signed-in user only. Every
value is frozen at issue/resolution time; see ``decision/verification/trust.py``
for the definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.decision.verification import build_history, build_ranking_metrics
from app.decision.verification.trust import build_verdict, list_calls
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/trust", tags=["trust"])

EvidenceState = Literal["too_early", "skill", "harm", "no_evidence"]
TypeKey = Literal["ideas", "advisor", "mandates", "regime"]


class SkillOut(BaseModel):
    metric: str
    value: float
    ci_low: float | None = None
    ci_high: float | None = None
    n: int = Field(description="Rebalance dates (the independent unit).")
    n_calls: int = 0
    benchmark_label: str


class RangeCoverageOut(BaseModel):
    k: int
    n: int
    rate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    nominal: float


class AciDateOut(BaseModel):
    date: str
    inside: int
    n: int


class AciOut(BaseModel):
    """Online conformal correction of the stated ranges (ADR 0018 §6)."""

    active: bool
    matured_weeks: int
    weeks_needed: int
    alpha_target: float
    alpha_now: float
    gamma: float
    widen_now: float | None = None
    per_date: list[AciDateOut] = []
    corrected: RangeCoverageOut | None = None
    raw_same_dates: RangeCoverageOut | None = None


class TypeRangeCoverageOut(RangeCoverageOut):
    aci: AciOut | None = None


class ReliabilityBinOut(BaseModel):
    p_mean: float
    hit_rate: float
    n: int


class CorpOut(BaseModel):
    mcb: float = Field(description="Miscalibration (CORP): Brier lost to unreliable probabilities.")
    dsc: float = Field(description="Discrimination: Brier gained over the base rate.")
    unc: float = Field(description="Uncertainty: Brier of the base rate.")


class TypeVerdictOut(BaseModel):
    type: TypeKey
    label: str
    n: int = Field(description="Rebalance dates: the picks of one date form one equal-weight basket.")
    n_needed: int | None = Field(
        default=None, description="Simulated median issue days until this verdict would say skill at the target hit rate."
    )
    n_needed_is_lower_bound: bool = False
    h_eff: int | None = Field(default=None, description="Chains the observed issue cadence needs per horizon window (used for dates needed).")
    cadence_days: float | None = Field(default=None, description="Median trading days between rebalance dates (1 below 5 dates).")
    years_needed: float | None = Field(default=None, description="n_needed dates at this cadence, in years (252 trading days).")
    horizon_days: int = Field(default=21, description="Holding period (trading days); also the lag-h of the e-process.")
    lag_h: int = 21
    n_unscored: int = Field(default=0, description="Resolved calls that could not be scored (no benchmark price).")
    n_holds: int = Field(default=0, description="Logged holds, left out of hit statistics by design.")
    n_issue_days: int = 0
    n_calls: int = 0
    n_delisted: int = 0
    call_hits: int = 0
    call_hit_rate: float | None = None
    hits: int
    hit_rate: float | None = None
    hit_ci: list[float] | None = None
    mean_excess: float | None = None
    mean_excess_ci: list[float] | None = None
    hac_lag: int = 0
    e_skill: float = Field(description="Current e-value (valid at any stopping time); the state uses it via e-BH.")
    e_harm: float
    e_skill_peak: float = 1.0
    e_harm_peak: float = 1.0
    n_tests: int = 0
    e_skill_lower: float | None = Field(
        default=None, description="Legacy e-value if every call still in flight resolved at its worst; only this may cross."
    )
    e_harm_lower: float | None = None
    family: str | None = Field(default=None, description="Calendar-time family whose test sets the state (F1 ideas, F3 advisor).")
    e_skill_crossed_strong: bool = False
    state: EvidenceState
    benchmarked: bool = True
    benchmark_label: str = ""
    brier: float | None = None
    bss: float | None = None
    bss_ci: list[float] | None = None
    n_stated_p: int = 0
    n_eff_stated: int = Field(default=0, description="Issue dates behind the stated probabilities: the effective labels.")
    spiegelhalter_z: float | None = None
    reliability: list[ReliabilityBinOut] = Field(default_factory=list)
    corp: CorpOut | None = None
    range_coverage: TypeRangeCoverageOut | None = None
    calibration_caption: str | None = None
    next_resolution_at: str | None = None
    note: str | None = None


class EPathPointOut(BaseModel):
    day: str
    e_skill: float
    e_harm: float


class PosteriorSensitivityOut(BaseModel):
    tau_21d: float
    p_positive: float
    mean_21d: float


class PosteriorOut(BaseModel):
    tau_21d: float = Field(description="Prior standard deviation of the edge, per 21 trading days.")
    n: int
    sample_mean_21d: float
    sample_se_21d: float
    shrinkage: float = Field(description="Weight on the data: tau^2 / (tau^2 + se^2).")
    mean_21d: float
    ci_low_21d: float
    ci_high_21d: float
    p_positive: float = Field(description="Posterior chance the edge is positive. Never a verdict.")
    sensitivity: list[PosteriorSensitivityOut] = Field(default_factory=list)


class WithinOut(BaseModel):
    years: int
    p: float


class ForecastGridOut(BaseModel):
    assumption: float
    q50_days: int | None = None
    q50_years: float | None = None


class TimeToKnowOut(BaseModel):
    assumption_unit: Literal["excess_per_21d", "rank_ic"]
    assumption: float
    ic_sd: float | None = None
    basket_sd_21d: float | None = None
    sd_daily: float
    sd_measured: bool = False
    q10_days: int | None = None
    q50_days: int | None = None
    q90_days: int | None = None
    q10_years: float | None = None
    q50_years: float | None = None
    q90_years: float | None = None
    max_days: int
    p_within: list[WithinOut] = Field(default_factory=list)
    grid: list[ForecastGridOut] = Field(default_factory=list)


class PreRegistrationOut(BaseModel):
    n_days: int = 0
    mean_21d: float | None = None


class FamilyTestOut(BaseModel):
    family: Literal["F1", "F2", "F3"]
    series: Literal["ideas", "ranking", "advisor"]
    role: Literal["primary", "secondary"]
    question: str
    start: str
    n_days: int = Field(description="Trading days with something open since the start; each is one observation.")
    n_empty_days: int = 0
    n_stale_days: int = 0
    n_clipped: int = 0
    first_day: str | None = None
    last_day: str | None = None
    mean_open: float | None = None
    mean_21d: float | None = None
    mean_ci_21d: list[float] | None = None
    sd_daily: float | None = None
    e_skill: float
    e_harm: float
    e_skill_peak: float = 1.0
    e_harm_peak: float = 1.0
    threshold: float
    n_tests: int
    state: EvidenceState
    rho1: float | None = None
    rho1_flag: bool = False
    pre_registration: PreRegistrationOut
    path: list[EPathPointOut] = Field(default_factory=list)
    posterior: PosteriorOut | None = None
    time_to_know: TimeToKnowOut


class PairedPointOut(BaseModel):
    day: str
    cumulative: float


class PairedComparisonOut(BaseModel):
    """Advisor minus Discover picks on common days; descriptive, never a verdict."""

    question: str
    start: str
    n_days: int
    n_days_pre_registration: int = 0
    n_days_advisor_only: int = 0
    n_days_ideas_only: int = 0
    mean_21d: float | None = None
    mean_ci_21d: list[float] | None = None
    share_advisor_ahead: float | None = None
    enough_days: bool = False
    path: list[PairedPointOut] = []


class FactorStudyOut(BaseModel):
    """ADR 0018 §8: recorded once per spec version; "waiting" until it can be."""

    status: Literal["waiting", "decided"]
    spec_version: int
    n_backfilled_picks: int | None = None
    spec: dict[str, Any] | None = None
    decision: Literal["build", "drop", "neither"] | None = None
    oos_r2: float | None = None
    n_days: int | None = None
    n_oos_days: int | None = None
    betas: dict[str, float] | None = None
    computed_at: str | None = None


class FactorNeutralRowOut(BaseModel):
    n_days: int
    first_day: str | None = None
    last_day: str | None = None
    mean_21d: float | None = None
    mean_ci_21d: list[float] | None = None


class FactorGateOut(BaseModel):
    build: float
    drop: float
    min_oos_days: int
    min_fit_days: int


class FactorNeutralOut(BaseModel):
    study: FactorStudyOut
    row: FactorNeutralRowOut | None = None
    gate: FactorGateOut


class DailyTestsOut(BaseModel):
    start: str = Field(description="First day an observation may enter an e-value (ADR 0018).")
    clip: float
    sigma_ref: float
    prior_var: float
    bet_cap: float
    fdr_level: float
    threshold: float
    min_days_for_state: int
    horizon_days: int
    families: list[FamilyTestOut]
    paired: PairedComparisonOut | None = None
    factor_neutral: FactorNeutralOut | None = None


class VerdictOut(BaseModel):
    headline: str
    resolved_calls: int
    resolved_units: int = 0
    n_needed: int | None = None
    n_needed_is_lower_bound: bool = False
    h_eff: int | None = None
    cadence_days: float | None = None
    years_needed: float | None = None
    ebh_threshold: float | None = Field(default=None, description="e-value one rejection needs under e-BH: K / alpha.")
    horizon_days: int | None = None
    min_calls_for_state: int | None = Field(default=None, description="Rebalance dates before a state other than too early.")
    n_unscored: int = 0
    assumed_edge: float | None = Field(default=None, description="Assumed true mean excess return per 21-day date, for the time-to-know number.")
    assumed_sd: float | None = Field(default=None, description="Assumed standard deviation of one date's basket excess return.")
    excess_clip: float | None = Field(default=None, description="Excess returns are clipped to +-this before betting.")
    min_calls_for_reliability: int | None = None
    min_n_eff_for_probability: int | None = Field(default=None, description="Issue dates before any probability is stated.")
    min_units_for_interval: int | None = None
    min_calls_for_interval: int | None = None
    target_hit_rate: float | None = None
    n_tests: int = 0
    fdr_level: float | None = None
    skill: SkillOut | None = None
    state: EvidenceState
    types: list[TypeVerdictOut]
    frozen_at_issue: bool = True
    method_note: str
    legacy_method_note: str | None = None
    first_resolution_due: str | None = None
    daily_tests: DailyTestsOut | None = None


class CallOut(BaseModel):
    type: TypeKey
    id: str
    issued_at: str
    resolved_at: str
    subject: str
    call: str
    stated_p: float | None = None
    stated_range: list[float] | None = None
    outcome: float | None = None
    benchmark_outcome: float | None = None
    excess: float | None = None
    hit: bool
    verdict: str | None = None
    delisted: bool = False


class CallsOut(BaseModel):
    horizon_days: int | None = Field(default=None, description="Longest holding period (trading days) of the benchmarked calls.")
    type: TypeKey | None = None
    total: int
    limit: int
    offset: int
    items: list[CallOut] = Field(default_factory=list)
    worst_misses: list[CallOut] = Field(default_factory=list)
    frozen_at_issue: bool = True


class IcSummaryOut(BaseModel):
    n_runs: int
    mean: float | None = None
    t_nw: float | None = Field(default=None, description="Newey-West t of the mean IC (lag 5 runs).")
    icir: float | None = None
    share_positive: float | None = None


class IcDecayOut(BaseModel):
    horizon_days: int
    n_runs: int
    mean_ic: float | None = None


class QuintileOut(BaseModel):
    quintile: int = Field(description="1 = the highest composite scores.")
    n_runs: int
    mean_excess: float | None = None


class PairedGapOut(BaseModel):
    n_runs: int
    mean_gap: float | None = None
    ci: list[float] | None = Field(default=None, description="90 % run-block bootstrap interval.")


class GateCheckOut(PairedGapOut):
    reject_stage: str
    n_names: int


class TierRateOut(BaseModel):
    n_runs: int
    hit_rate: float | None = None
    range: list[float] | None = Field(default=None, description="90 % Beta(1, 1) range on the runs (effective labels).")


class TierOut(TierRateOut):
    tier: Literal["top", "middle", "bottom"]


class TiersOut(BaseModel):
    n_eff: int = Field(description="Issue dates behind the rates: the effective number of labels.")
    base: TierRateOut
    tiers: list[TierOut]


class RankingSectionOut(BaseModel):
    n_runs: int
    n_cohort_runs: int
    n_snapshots: int
    n_outcomes: int
    outcome_status: dict[str, int] = Field(default_factory=dict)
    first_issue: str | None = None
    last_issue: str | None = None
    ic: IcSummaryOut
    ic_decay: list[IcDecayOut]
    sector_neutral_ic: IcSummaryOut
    quintiles: list[QuintileOut]
    picked_vs_rest: PairedGapOut
    gate_check: list[GateCheckOut]
    tiers: TiersOut | None = None
    etf_ic: IcSummaryOut


class RankingMetricsOut(BaseModel):
    horizon_days: int
    min_stocks: int
    ic_hac_lag: int
    live: RankingSectionOut
    exploratory: RankingSectionOut | None = Field(
        default=None, description="Back-filled snapshots: exploratory, never in a verdict."
    )
    verdict_note: str


class FanPointOut(BaseModel):
    p: int
    cumulative_excess: float


class FanOut(BaseModel):
    years: int
    percentiles: list[FanPointOut]
    share_beat: float = Field(description="Share of simulated ten-year periods that beat the ETF after costs and tax.")


class DecayOut(BaseModel):
    full_period: float | None = None
    last_10_years: float | None = None
    after_haircut: float | None = None


class TiltCheckOut(BaseModel):
    name: str
    label: str
    value: float | None = None
    passed: bool


class TiltCardOut(BaseModel):
    strategy: str | None = None
    label: str | None = None
    region: str | None = None
    gates_tilt: bool = False
    citation: str | None = None
    passed: bool = False
    long_short_t: float | None = None
    t_bar: float = 2.0
    months: int | None = None
    start: str | None = None
    end: str | None = None
    stale: bool = False
    decay: DecayOut
    tracking_error_annual: float | None = None
    worst_5y_excess: float | None = None
    checks: list[TiltCheckOut] = Field(default_factory=list)
    fan: FanOut | None = None
    computed_at: str | None = None


class RankingModelRowOut(BaseModel):
    model: str | None = None
    months: int | None = None
    first_month: str | None = None
    last_month: str | None = None
    ic_mean: float | None = None
    ic_t: float | None = None
    long_short_annual: float | None = None
    long_only_annual: float | None = None
    recent_months: int | None = None
    recent_ic_mean: float | None = None
    recent_long_short_annual: float | None = None
    dsr: float | None = None


class RankingModelOut(BaseModel):
    available: bool
    status: str
    models: list[RankingModelRowOut] = Field(default_factory=list)
    pbo: float | None = None
    data_to: str | None = None
    stale: bool = False
    computed_at: str | None = None


class SatelliteBestOut(BaseModel):
    name: str | None = None
    sharpe_annual: float | None = None
    mean_annual: float | None = None
    dsr: float | None = None
    months: int | None = None


class SatelliteOut(BaseModel):
    available: bool
    unlocked: bool
    reason: str | None = None
    dsr_bar: float
    best: SatelliteBestOut | None = None
    n_candidates: int = 0
    pbo: float | None = None
    computed_at: str | None = None


class TiltReviewOut(BaseModel):
    status: Literal["not_live", "too_early", "inside", "under_review"]
    note: str


class HaircutOut(BaseModel):
    publication_decay: float
    implementation_cost: float
    after_tax_factor: float


class HistoryOut(BaseModel):
    title: str
    tilt_cards: list[TiltCardOut]
    ranking_model: RankingModelOut
    satellite: SatelliteOut
    tilt_review: TiltReviewOut
    n_trials: int | None = None
    haircut: HaircutOut
    not_covered: str
    disclosures: list[str]


@router.get("/verdict", response_model=VerdictOut)
def get_verdict(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Headline sentence, resolved-call count, skill vs the passive core and the
    evidence state per prediction type (too early / skill / no evidence / harm)."""
    return build_verdict(db, user.id)


@router.get("/calls", response_model=CallsOut)
def get_calls(
    type: TypeKey | None = Query(None, description="Only this prediction type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Resolved calls, newest first, with the five worst misses against the ETF."""
    return list_calls(db, user.id, type=type, limit=limit, offset=offset)


@router.get("/ranking", response_model=RankingMetricsOut)
def get_ranking(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """How well Discover's score sorted every scored stock (descriptive; the verdict is F2)."""
    return build_ranking_metrics(db, user.id)


@router.get("/history", response_model=HistoryOut)
def get_history(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What decades of past data say (simulated, not live); it tests none of the live calls."""
    return build_history(db)
