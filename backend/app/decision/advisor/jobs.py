"""Scheduler registrations for the advisor loop (Phase E relocation).

Registrars moved byte-identical out of ``services/jobs.py`` so the generic
job infrastructure no longer imports owning contexts. The composition root
(``app.worker.build_scheduler``) calls these by name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_advisor_cycle_job(scheduler: Any | None = None) -> str:
    """Register the daily autonomous advisor paper-trade cycle (advisor loop D2).

    Cadence: once per trading day — Mon-Fri at 10:00 UTC. Market holidays are
    skipped via a configurable holiday list (public setting
    ``advisor_market_holidays`` or env ``ADVISOR_MARKET_HOLIDAYS``, both
    comma-separated ISO dates); no trade happens on closed days.
    """

    def advisor_cycle_inner() -> None:
        import os

        from datetime import UTC, datetime

        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import User
        from app.decision.advisor.cycle import run_advisor_cycle
        from app.foundation.settings import get_public_settings

        today = datetime.now(UTC).date()
        if today.weekday() >= 5:  # belt-and-braces; cron is already mon-fri
            return

        db = SessionLocal()
        try:
            raw_holidays = str(
                get_public_settings(db).get("advisor_market_holidays")
                or os.getenv("ADVISOR_MARKET_HOLIDAYS")
                or ""
            )
            holidays = {h.strip() for h in raw_holidays.split(",") if h.strip()}
            if today.isoformat() in holidays:
                logger.info("advisor_cycle: %s is a market holiday — skipping", today)
                return
            for user in db.query(User).all():
                try:
                    result = run_advisor_cycle(db, user.id)
                    logger.info(
                        "advisor_cycle user=%s status=%s trades=%d",
                        user.id,
                        result.get("status"),
                        len(result.get("trades") or []),
                    )
                except Exception:
                    logger.error("advisor_cycle failed for user %s", user.id, exc_info=True)
                    db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "advisor_cycle", advisor_cycle_inner,
        scheduler=scheduler, day_of_week="mon-fri", hour=10, minute=0,
    )

def register_evolution_job(scheduler: Any | None = None) -> str:
    """Register the daily champion-vs-challenger evolution round (PR2 C4).

    Runs Mon-Fri at 10:30 UTC, after the advisor cycle (whose champion run is
    idempotent — the evolution round re-invoking it is a no-op). Each round
    scores both sleeves on the 4-axis card, records the round, and
    promotes/retires the challenger under the small-sample guard.
    """

    def evolution_inner() -> None:
        import os

        from datetime import UTC, datetime

        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import User
        from app.decision.advisor.evolution import run_evolution_round
        from app.decision.graduation.evaluator import evaluate_graduation
        from app.decision.graduation.state import apply_graduation_state
        from app.foundation.settings import get_public_settings

        today = datetime.now(UTC).date()
        if today.weekday() >= 5:
            return

        db = SessionLocal()
        try:
            raw_holidays = str(
                get_public_settings(db).get("advisor_market_holidays")
                or os.getenv("ADVISOR_MARKET_HOLIDAYS")
                or ""
            )
            holidays = {h.strip() for h in raw_holidays.split(",") if h.strip()}
            if today.isoformat() in holidays:
                logger.info("evolution_round: %s is a market holiday — skipping", today)
                return
            for user in db.query(User).all():
                try:
                    summary = run_evolution_round(db, user.id)
                    logger.info(
                        "evolution_round user=%s verdict=%s champion=%s challenger=%s",
                        user.id,
                        summary.get("verdict"),
                        summary.get("champion_score"),
                        summary.get("challenger_score"),
                    )
                    # Graduation state stays authoritative; inbox-card
                    # emission (emit_rec_cards) died with the review inbox.
                    verdict = evaluate_graduation(db, user.id)
                    apply_graduation_state(db, user.id, verdict)
                except Exception:
                    logger.error("evolution_round failed for user %s", user.id, exc_info=True)
                    db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "evolution_round", evolution_inner,
        scheduler=scheduler, day_of_week="mon-fri", hour=10, minute=30,
    )

def register_advisor_rl_training_job(scheduler: Any | None = None) -> str:
    """Register the quarterly RL policy training + validation job (Track D2).

    Expensive (SB3 training + a full FinRL rollout) — runs quarterly, not
    daily, the same cadence class as AlphaCrafter's quarterly tuning job
    (``alphacrafter_tuning``) but offset by a month and time so the two
    expensive quarterly jobs never collide. Skipped entirely when FinRL is
    disabled: there is then nothing to train, and ``advisor/rl_signal.py``'s
    per-cycle read already fails open on an empty/absent
    ``QuantRlPolicy`` row, so a paper-trading cycle is unaffected either way.

    Per-user cooldown (~80 days, just under a quarter) skips a user already
    retrained recently, mirroring the discover-ml-training job's cooldown
    check (Track D1b).
    """
    _retrain_cooldown_days = 80

    def advisor_rl_training_inner() -> None:
        from app.foundation.core.config import get_settings

        settings = get_settings()
        if not settings.finrl_enabled:
            logger.info("advisor_rl_training: FinRL disabled — skipping")
            return

        from datetime import UTC, datetime, timedelta

        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import QuantRlPolicy, User
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = SessionLocal()
        try:
            cutoff = datetime.now(UTC) - timedelta(days=_retrain_cooldown_days)
            trained = 0
            for user in db.query(User).all():
                try:
                    recent = (
                        db.query(QuantRlPolicy)
                        .filter(
                            QuantRlPolicy.user_id == user.id,
                            QuantRlPolicy.env_id == "portfolio_allocation",
                            QuantRlPolicy.created_at >= cutoff,
                        )
                        .first()
                    )
                    if recent is not None:
                        logger.debug(
                            "advisor_rl_training: user %s already trained within cooldown, skipping",
                            user.id,
                        )
                        continue
                    row = train_and_validate_policy(db, user.id)
                    trained += 1
                    logger.info(
                        "advisor_rl_training: user %s -> policy %s status=%s",
                        user.id, row.id, row.status,
                    )
                except Exception:
                    logger.error("advisor_rl_training failed for user %s", user.id, exc_info=True)
                    db.rollback()
            logger.info("advisor_rl_training: trained %d polic(y/ies)", trained)
        finally:
            db.close()

    return register_cron_job(
        "advisor_rl_training", advisor_rl_training_inner,
        scheduler=scheduler, month="2,5,8,11", day=2, hour=6, minute=0,
    )
