"""Scheduler registrations for the trust evidence ledgers (ADR 0018).

* ``trust_daily_ledger`` — nightly at 02:40 UTC, after the 02:00 prediction
  resolution: freezes the ``ideas``, ``advisor``, ``mandate_a`` and
  ``mandate_b`` daily active-return series.
  It refreshes prices for the open picks only (about 15-60 symbols), which
  nothing else keeps current.
* ``trust_weekly_ledger`` — Saturdays at 03:40 UTC, when Friday's closes are
  final: resolves the shadow ledger's candidate outcomes and freezes the
  ``ranking`` and ``selection`` series. One price pass over the open cohorts' ~300 names a week,
  about the load of one Discover run, instead of every night.

Both are idempotent catch-ups: a missed night is filled in by the next run,
and nothing frozen is ever rewritten.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


#: ``PaperPortfolio.managed_by`` of a mandate whose futility test rejected.
RETIRED = "retired"


def retire_futile_mandates(db: Any, user_id: str) -> list[str]:
    """ADR 0018 amendment of 2026-10-14, §5: retire a mandate its futility test rejects.

    Its reviews and trades stop (the review loop only runs ``managed_by ==
    "llm"`` sleeves), its NAV is still marked, and the owner gets one message.
    A retirement is never reversed by later data. Returns the mandates retired now.
    """
    from app.decision.verification.daily_ledger import MANDATE_SERIES, SERIES
    from app.decision.verification.daily_ledger import mandate_starts
    from app.decision.verification.daily_tests import mandate_results
    from app.foundation.models.entities import PaperPortfolio, TrustDailyActiveReturn
    from app.foundation.notify import alert_user

    rows = (
        db.query(TrustDailyActiveReturn)
        .filter(TrustDailyActiveReturn.user_id == user_id,
                TrustDailyActiveReturn.series.in_(tuple(MANDATE_SERIES)))
        .all()
    )
    by_series: dict[str, list[Any]] = {s: [] for s in SERIES}
    for r in rows:
        by_series[r.series].append(r)
    retired: list[str] = []
    for result in mandate_results(by_series, mandate_starts(db, user_id)):
        if not result["retire"]:
            continue
        sleeve = (
            db.query(PaperPortfolio)
            .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == result["mandate"])
            .first()
        )
        if sleeve is None or sleeve.managed_by == RETIRED:
            continue
        sleeve.managed_by = RETIRED
        retired.append(result["mandate"])
        text = (
            f"Quantfolio: mandate {result['mandate']} is retired. Its pre-registered test says it adds less than "
            f"{result['delta_21d']:.1%} per 21 days over MSCI World after costs (e = {result['e_futility']:.0f}). "
            "It stops trading; its value is still tracked on the Trust page."
        )
        alert_user(db, user_id, source="trust", title=f"Mandate {result['mandate']} retired", telegram_text=text,
                   body=text, severity="info", href="/trust")
    return retired


def run_daily_ledger() -> dict[str, dict[str, int]]:
    from app.decision.verification.daily_ledger import (
        SERIES_ADVISOR,
        SERIES_IDEAS,
        SERIES_MANDATE_A,
        SERIES_MANDATE_B,
        freeze_user_series,
        ledger_users,
        mandate_users,
    )
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        # A mandate sleeve needs no discovery records, so its owner may not be a ledger user.
        out = {
            user_id: freeze_user_series(
                db, user_id, (SERIES_IDEAS, SERIES_ADVISOR, SERIES_MANDATE_A, SERIES_MANDATE_B), refresh=True,
            )
            for user_id in sorted(set(ledger_users(db)) | set(mandate_users(db)))
        }
        for user_id in out:
            try:
                retire_futile_mandates(db, user_id)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("trust_daily_ledger: mandate retire check failed for user %s", user_id)
        return out
    finally:
        db.close()


def run_weekly_ledger() -> dict[str, Any]:
    from app.decision.verification.candidate_outcomes import resolve_candidate_outcomes
    from app.decision.verification.daily_ledger import (
        SERIES_RANKING,
        SERIES_RANKING_EF,
        SERIES_SELECTION,
        SERIES_SELECTION_W,
        freeze_user_series,
        ledger_users,
    )
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        try:
            outcomes = resolve_candidate_outcomes(db, refresh=True)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("trust_weekly_ledger: candidate outcomes failed")
            outcomes = -1
        # The selection series reads the same ~300 closes as the ranking one,
        # so it shares this weekly price pass (its days are frozen from the
        # snapshots, which were fixed at issue; a later freeze changes nothing).
        # The lower-noise designs of the 2026-11-08 amendment read the same
        # closes plus the year before each issue (already stored: the run's
        # history ingest keeps five years).
        series = (SERIES_RANKING, SERIES_SELECTION, SERIES_SELECTION_W, SERIES_RANKING_EF)
        ranking = {
            user_id: freeze_user_series(db, user_id, series, refresh=True)
            for user_id in ledger_users(db)
        }
        # The Discover BUY gate reads this stored copy (it may not import this package).
        from app.decision.verification.daily_tests import store_buy_gate_evidence

        try:
            buy_gate = store_buy_gate_evidence(db, ledger_users(db))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("trust_weekly_ledger: storing the BUY gate evidence failed")
            buy_gate = {}
        # The factor study (ADR 0018 §8) was withdrawn on 2026-10-14: the
        # selection family against matched non-picks does its job.
        return {"candidate_outcomes": outcomes, "ranking": ranking, "buy_gate": buy_gate}
    finally:
        db.close()


def register_trust_daily_ledger_job(scheduler: Any | None = None) -> str:
    """Nightly freeze of the ideas and advisor daily active-return series."""

    def _inner() -> None:
        result = run_daily_ledger()
        logger.info("trust_daily_ledger: %s", result)

    return register_cron_job("trust_daily_ledger", _inner, scheduler=scheduler, hour=2, minute=40)


def register_trust_weekly_ledger_job(scheduler: Any | None = None) -> str:
    """Saturday shadow-ledger outcomes and ranking-series freeze."""

    def _inner() -> None:
        result = run_weekly_ledger()
        logger.info("trust_weekly_ledger: %s", result)

    return register_cron_job(
        "trust_weekly_ledger", _inner, scheduler=scheduler, day_of_week="sat", hour=3, minute=40,
    )
