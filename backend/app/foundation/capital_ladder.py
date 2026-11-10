"""Capital ladder: parameters and the read side (ADR 0025, plan 2026-11-10 WS4).

Foundation-tier so the monthly plan (a module many decision-loop packages
import) can read the satellite weight without importing the graduation
package, which would chain those packages together. The nightly evidence and
the stage machine live in ``app.decision.graduation.ladder``.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import LadderCandidate, LadderEvidenceDaily
from app.foundation.models.entities._core import now_utc

# --- Pre-registered parameters (ADR 0025; fixed before any data was seen) ---
SLOT_LLM = "llm"
SLOT_RULES = "rules"
SLOT_PI = {SLOT_LLM: 0.5, SLOT_RULES: 0.5}
LAMBDAS = (0.025, 0.05, 0.10, 0.20, 0.35)
#: Clip of x / sigma (plan 2026-11-10, WP4, ADR 0025 amendment). A clip at 1 sigma
#: makes the bets test the *median* of the excess return, so skew alone moved the
#: e-processes (skew -0.85 at zero alpha reached Earned in 59 % of paths). At 3
#: sigma the bets are divided by 3 to stay inside [-1, 1], so ``1 + lambda * y``
#: stays positive and the LAMBDAS need no change.
CLIP_SIGMAS = 3.0
EWMA_HALFLIFE_DAYS = 126
#: Days of x before sigma is trusted; until then the e-processes do not bet.
SIGMA_BURN_IN = 20
TUITION_MIN_DAYS = 126
HARM_THRESHOLD = 10.0
EARNED_ALPHA = 0.2
PROVEN_ALPHA = 0.05
DRAWDOWN_BAND_K = 2.8
NULL_BAND_Z = 1.645
BRIER_VETO = 0.25
AUTHORISATION_MONTHS = 24
EARNED_WEIGHT_PCT = 10.0
TUITION_CAP_PCT = 5.0
PROVEN_WEIGHT_MAX_PCT = 20.0
TRADING_DAYS_PER_YEAR = 252
PRIOR_ALPHA_MEAN = -0.01
PRIOR_ALPHA_SD = 0.03
CS_ALPHA = 0.05

DEFAULTS = {
    "ladder_tuition_budget_pct": 1.0,
    "ladder_bad_alpha_pct": -3.0,
    "ladder_proven_weight_pct": 15.0,
    "ladder_owner_veto": False,
}

STAGE_NAMES = {0: "Paper", 1: "Tuition", 2: "Earned", 3: "Proven"}


def epoch_pi(slot: str, epoch: int) -> float:
    return SLOT_PI.get(slot, 0.5) / float(epoch) ** 2


def thresholds(slot: str, epoch: int) -> tuple[float, float]:
    """(Earned, Proven) e-value thresholds of an epoch: 10 e^2 and 40 e^2 at pi = 1/2."""
    pi = epoch_pi(slot, epoch)
    return 1.0 / (EARNED_ALPHA * pi), 1.0 / (PROVEN_ALPHA * pi)


def tuition_weight_pct(te_annual: float | None, budget_pct: float = 1.0, bad_alpha_pct: float = -3.0) -> float:
    """w1 = min(5 %, B / (|alpha_bad| + 1.645 TE)) in percent of the book."""
    if te_annual is None or not math.isfinite(te_annual) or te_annual <= 0:
        return 0.0
    denom = abs(bad_alpha_pct) / 100.0 + NULL_BAND_Z * te_annual
    return float(min(TUITION_CAP_PCT, 100.0 * (budget_pct / 100.0) / denom))


def aware(value: datetime | None) -> datetime:
    if value is None:
        return now_utc()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def ladder_settings(db: Session) -> dict[str, Any]:
    from app.foundation.settings import get_public_settings

    s = get_public_settings(db)
    return {k: s.get(k, v) for k, v in DEFAULTS.items()}


def stage_on(db: Session, candidate: LadderCandidate, as_of: date | None) -> tuple[int, float]:
    query = db.query(LadderEvidenceDaily).filter(LadderEvidenceDaily.candidate_id == candidate.id)
    if as_of is not None:
        query = query.filter(LadderEvidenceDaily.day < as_of)
    row = query.order_by(LadderEvidenceDaily.day.desc()).first()
    if row is None:
        return 0, 0.0
    return int(row.stage or 0), float(row.e_skill or 0.0)


def funding_candidate(db: Session, user_id: str, as_of: date | None) -> tuple[LadderCandidate, int, float] | None:
    """The one candidate that funds the satellite: highest stage, then higher E."""
    best: tuple[LadderCandidate, int, float] | None = None
    candidates = (
        db.query(LadderCandidate)
        .filter(LadderCandidate.user_id == user_id)
        .all()
    )
    for c in candidates:
        if as_of is None and c.retired_at is not None:
            continue
        if as_of is not None:
            born = aware(c.born_at).date()
            retired = aware(c.retired_at).date() if c.retired_at else None
            if born >= as_of or (retired is not None and retired < as_of):
                continue
        stage, e = stage_on(db, c, as_of)
        if stage <= 0:
            continue
        if best is None or (stage, e) > (best[1], best[2]):
            best = (c, stage, e)
    return best


def weight_pct(db: Session, candidate: LadderCandidate, stage: int, as_of: date | None) -> float:
    cfg = ladder_settings(db)
    if stage <= 0:
        return 0.0
    if stage == 1:
        query = db.query(LadderEvidenceDaily.te).filter(LadderEvidenceDaily.candidate_id == candidate.id)
        if as_of is not None:
            query = query.filter(LadderEvidenceDaily.day < as_of)
        row = query.order_by(LadderEvidenceDaily.day.desc()).first()
        return tuition_weight_pct(
            row[0] if row else None,
            float(cfg["ladder_tuition_budget_pct"]), float(cfg["ladder_bad_alpha_pct"]),
        )
    if stage == 2:
        return EARNED_WEIGHT_PCT
    return float(min(PROVEN_WEIGHT_MAX_PCT, max(0.0, float(cfg["ladder_proven_weight_pct"]))))


def month_start(day: date) -> date:
    return day.replace(day=1)


def ladder_weight(db: Session, user_id: str, *, today: date | None = None) -> float:
    """The satellite share (fraction of the book) the ladder allows this month.

    Read from the stage on the last evidence day before the current month, so a
    stage change takes effect at the next monthly plan, never mid-month.
    """
    funding = ladder_funding(db, user_id, today=today)
    return funding["weight_pct"] / 100.0 if funding else 0.0


def ladder_funding(db: Session, user_id: str, *, today: date | None = None) -> dict[str, Any] | None:
    as_of = month_start(today or now_utc().date())
    best = funding_candidate(db, user_id, as_of=as_of)
    if best is None:
        return None
    candidate, stage, e = best
    return {
        "candidate_id": candidate.id,
        "slot": candidate.slot,
        "kind": candidate.kind,
        "portfolio_id": candidate.portfolio_id,
        "epoch": candidate.epoch,
        "stage": stage,
        "stage_name": STAGE_NAMES[stage],
        "e_skill": e,
        "weight_pct": weight_pct(db, candidate, stage, as_of),
        "as_of": as_of.isoformat(),
    }
