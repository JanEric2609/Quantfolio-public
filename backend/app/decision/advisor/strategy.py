"""Champion / challenger strategy lifecycle (PR2 C1/C2).

A strategy = (cycle config + prompt framing + active lessons + calibration
state). Exactly one champion per user; at most one active challenger. The
challenger is a bounded mutation of the champion — prompt framing variants
reuse ``discover/framings``' style framings, numeric knobs get
clamped jitter. Risk ceilings can only be mutated *tighter* than the default
envelope, never looser (safety constraint).
"""
from __future__ import annotations

import logging
import random
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import AdvisorStrategy, PaperPortfolio
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.risk_gate import DEFAULT_RISK_CEILINGS
from app.decision.discover.framings import STYLE_FRAMINGS

logger = logging.getLogger(__name__)

ROLE_CHAMPION = "champion"
ROLE_CHALLENGER = "challenger"
ROLE_RETIRED = "retired"

#: The one sleeve every challenger shared before ADR 0025 (archived by the
#: one-time split, :func:`split_shared_challenger_sleeve`).
CHALLENGER_MANDATE = "advisor-challenger"
SPLIT_SETTING_KEY = "advisor_sleeves_split_at"

_MAX_CANDIDATE_BOUNDS = (5, 15)


def get_champion(db: Session, user_id: str) -> AdvisorStrategy | None:
    return (
        db.query(AdvisorStrategy)
        .filter(AdvisorStrategy.user_id == user_id, AdvisorStrategy.role == ROLE_CHAMPION)
        .one_or_none()
    )


def get_active_challenger(db: Session, user_id: str) -> AdvisorStrategy | None:
    return (
        db.query(AdvisorStrategy)
        .filter(AdvisorStrategy.user_id == user_id, AdvisorStrategy.role == ROLE_CHALLENGER)
        .one_or_none()
    )


def ensure_champion(db: Session, user_id: str) -> AdvisorStrategy:
    """Return the user's champion, creating a default one if none exists.

    The default champion drives the existing advisor sleeve with the stock
    config (no framing, default ceilings) — it IS the PR1 behaviour, now
    named and versioned so challengers can mutate against it.
    """
    champion = get_champion(db, user_id)
    if champion is not None:
        return champion

    from app.decision.paper_portfolio import seed_paper_portfolio_from_real

    portfolio = seed_paper_portfolio_from_real(db, user_id)
    champion = AdvisorStrategy(
        user_id=user_id,
        role=ROLE_CHAMPION,
        portfolio_id=portfolio.id,
        config_json={},
        promoted_at=now_utc(),
    )
    db.add(champion)
    db.flush()

    from app.foundation import quant_metrics as qm

    qm.record_trial(
        db,
        context="advisor_champion",
        trial_key=champion.id,
        metadata={"user_id": user_id, "portfolio_id": portfolio.id},
    )
    db.commit()
    db.refresh(champion)
    logger.info("ensure_champion: created default champion %s for user %s", champion.id, user_id)
    return champion


def _mutate_config(base: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Produce a bounded mutation of the champion's config."""
    cfg = dict(base or {})

    # Prompt framing: pick a style different from the parent's.
    current = cfg.get("prompt_framing")
    choices = [f for f in STYLE_FRAMINGS.values() if f != current]
    cfg["prompt_framing"] = rng.choice(choices)

    # Candidate breadth jitter within bounds.
    lo, hi = _MAX_CANDIDATE_BOUNDS
    base_n = int(cfg.get("max_candidates", 10))
    cfg["max_candidates"] = max(lo, min(hi, base_n + rng.choice([-3, -2, 2, 3])))

    # Risk ceilings: jitter but only equal-or-tighter than the defaults.
    ceilings = dict(cfg.get("risk_ceilings") or DEFAULT_RISK_CEILINGS)
    mutated_ceilings: dict[str, float] = {}
    for key, default_val in DEFAULT_RISK_CEILINGS.items():
        current_val = float(ceilings.get(key, default_val))
        factor = 1.0 + rng.uniform(-0.2, 0.2)
        mutated_ceilings[key] = round(min(default_val, max(default_val * 0.5, current_val * factor)), 6)
    cfg["risk_ceilings"] = mutated_ceilings
    return cfg


def strategy_mandate(strategy_id: str) -> str:
    """The paper-portfolio mandate of one strategy's own sleeve (ADR 0025).

    16 characters, inside ``PaperPortfolio.mandate``'s 24. A sleeve is never
    reused by another strategy.
    """
    return f"adv-{strategy_id[:12]}"


def retire_strategy(db: Session, strategy: AdvisorStrategy, *, reason: str) -> None:
    """Retire *strategy* and archive its sleeve (kept in place, no longer driven)."""
    from app.decision.paper_portfolio import archive_paper_portfolio

    strategy.role = ROLE_RETIRED
    strategy.retired_at = now_utc()
    db.flush()
    if strategy.portfolio_id:
        archive_paper_portfolio(db, strategy.portfolio_id, reason=reason)
    db.commit()


def spawn_challenger(
    db: Session,
    user_id: str,
    *,
    seed: int | None = None,
) -> AdvisorStrategy:
    """Create a new challenger as a bounded mutation of the champion (C2).

    Any existing active challenger is retired first (one at a time) and its
    sleeve archived. The new challenger gets its own sleeve, a clone of the
    champion's current book (same positions, same cash, same moment), so the
    two differ only in the decisions made from here (ADR 0025).
    """
    from uuid import uuid4

    from app.decision.paper_portfolio import clone_paper_portfolio

    champion = ensure_champion(db, user_id)
    rng = random.Random(seed)

    existing = get_active_challenger(db, user_id)
    if existing is not None:
        retire_strategy(db, existing, reason="challenger replaced")

    strategy_id = str(uuid4())
    if not champion.portfolio_id:
        raise ValueError("champion has no sleeve to clone")
    portfolio = clone_paper_portfolio(
        db, champion.portfolio_id,
        mandate=strategy_mandate(strategy_id),
        name=f"Advisor Challenger {strategy_id[:8]}",
    )
    challenger = AdvisorStrategy(
        id=strategy_id,
        user_id=user_id,
        role=ROLE_CHALLENGER,
        portfolio_id=portfolio.id,
        config_json=_mutate_config(champion.config_json or {}, rng),
        parent_strategy_id=champion.id,
    )
    db.add(challenger)
    db.flush()

    # ADR 0025: every spawned strategy is one trial of the advisor lineage,
    # the family the promotion test's alpha is split over.
    from app.foundation import quant_metrics as qm

    qm.record_trial(
        db,
        context=qm.ADVISOR_CHALLENGER_FAMILY,
        trial_key=challenger.id,
        metadata={"user_id": user_id, "parent_strategy_id": champion.id},
        config_hash=qm.config_digest(challenger.config_json or {}),
    )
    db.commit()
    db.refresh(challenger)
    logger.info(
        "spawn_challenger: %s (parent champion %s) for user %s",
        challenger.id, champion.id, user_id,
    )
    return challenger


def promote_challenger(db: Session, challenger: AdvisorStrategy) -> AdvisorStrategy:
    """Flip roles: challenger → champion, old champion → retired (C3).

    The champion pointer moves to the challenger's own sleeve; the old
    champion's sleeve is archived and no longer driven (ADR 0025). Graduation
    and the divergence diff always follow the *current champion's* sleeve.
    """
    champion = get_champion(db, challenger.user_id)
    if champion is not None:
        retire_strategy(db, champion, reason="champion replaced by promotion")
    challenger.role = ROLE_CHAMPION
    challenger.promoted_at = now_utc()

    from app.foundation import quant_metrics as qm

    qm.record_trial(
        db,
        context="advisor_champion",
        trial_key=challenger.id,
        metadata={"user_id": challenger.user_id, "portfolio_id": challenger.portfolio_id},
    )
    db.commit()
    db.refresh(challenger)
    logger.info(
        "promote_challenger: %s is the new champion (retired %s)",
        challenger.id, champion.id if champion else None,
    )
    return challenger


def split_shared_challenger_sleeve(db: Session, user_id: str) -> bool:
    """One-time reset (ADR 0025): end the shared ``advisor-challenger`` sleeve.

    Marked by the app setting ``advisor_sleeves_split_at``. Retires the current
    challenger, archives the shared sleeve, and spawns a fresh challenger
    cloned from the champion's sleeve. The owner accepted that the advisor's
    paper comparison history restarts. Returns True when it ran now.
    """
    from app.foundation.settings import get_public_settings, upsert_public_settings

    if get_public_settings(db).get(SPLIT_SETTING_KEY):
        return False
    challenger = get_active_challenger(db, user_id)
    shared = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == CHALLENGER_MANDATE)
        .one_or_none()
    )
    if challenger is not None and (shared is None or challenger.portfolio_id == shared.id):
        retire_strategy(db, challenger, reason="sleeve split (ADR 0025)")
        challenger = None
    if shared is not None:
        from app.decision.paper_portfolio import archive_paper_portfolio

        archive_paper_portfolio(db, shared.id, reason="shared challenger sleeve ended (ADR 0025)")
    if get_champion(db, user_id) is not None and challenger is None:
        spawn_challenger(db, user_id)
    upsert_public_settings(db, {SPLIT_SETTING_KEY: now_utc().isoformat()})
    logger.info("split_shared_challenger_sleeve: done for user %s", user_id)
    return True
