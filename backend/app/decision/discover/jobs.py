"""Scheduler registrations for the Discover pipeline (Phase E relocation).

Registrars moved byte-identical out of ``services/jobs.py`` so the generic
job infrastructure no longer imports owning contexts. The composition root
(``app.worker.build_scheduler``) calls these by name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_discovery_resolution_job(scheduler: Any | None = None) -> str:
    """Register the nightly Discovery prediction resolution job (P1 — #112).

    Finds DiscoveryPrediction rows past their resolve_at date, computes
    realised returns at current market prices, scores each prediction
    (directional hit, Brier, Rank IC, Mincer regression), and writes a
    ``DiscoverySkillSnapshot`` aggregate row so the UI can surface a
    "is the system improving?" trend chart.

    Runs at 02:00 UTC daily (after price refresh has settled).
    """
    from app.foundation.core.db import SessionLocal
    from app.decision.discover.resolution import resolve_due_predictions
    from app.decision.discover.scoring import score_batch
    from app.decision.discover.skill_snapshot import write_skill_snapshot

    def discovery_resolution_inner() -> None:
        db = SessionLocal()
        try:
            resolved = resolve_due_predictions(db)
            if not resolved:
                logger.debug("Discovery resolution: no pending predictions due")
                return
            db.commit()

            # Group resolved predictions by user so each user gets their
            # own scoring batch and skill snapshot — prevents cross-user
            # contamination of Rank IC, Mincer, and snapshot ownership.
            from collections import defaultdict

            by_user: dict[str, list[dict]] = defaultdict(list)
            for r in resolved:
                by_user[r["user_id"]].append(r)

            snapshot_ids: list[str] = []
            for user_id, batch in by_user.items():
                score_batch(db, batch)
                snapshot = write_skill_snapshot(db, batch)
                if snapshot:
                    snapshot_ids.append(snapshot.id)
                # Advisor loop E2: refresh the 4-axis scorecard once this
                # user's matured predictions are scored.
                try:
                    from app.decision.advisor.scorecard import compute_advisor_scorecard

                    compute_advisor_scorecard(db, user_id)
                except Exception:
                    logger.error(
                        "advisor scorecard failed for user %s", user_id, exc_info=True
                    )
                    db.rollback()

            logger.info(
                "Discovery resolution: %d predictions resolved across %d users, "
                "%d snapshots written",
                len(resolved), len(by_user), len(snapshot_ids),
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return register_cron_job(
        "discovery_resolution", discovery_resolution_inner,
        scheduler=scheduler, hour=2, minute=0,
    )

def register_discover_refresh_job(scheduler: Any | None = None) -> str:
    """Register the weekly Discover candidate refresh (feeds the advisor loop).

    ``run_advisor_cycle`` hard-requires a *completed* DiscoverRun with
    shortlisted candidates, and treats a shortlist older than 7 days as stale.
    Until this job existed the only way to produce one was clicking "Discover"
    in the UI, so the autonomous loop starved as soon as the last manual run
    aged out — it kept running daily and kept exiting with ``no_candidates``.

    Runs Mondays at 05:00 UTC: after the weekend, before Monday's 10:00 advisor
    cycle, and well clear of the Sunday 04:00 config review. Skipped for a user
    who already has a completed run newer than ``_min_age_days``, so a manual
    run the same week is not duplicated.
    """
    _min_age_days = 5

    def discover_refresh_inner() -> None:
        from datetime import UTC, datetime, timedelta

        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import DiscoverRun, User
        from app.decision.discover.orchestrator import submit_discover_job

        db = SessionLocal()
        try:
            cutoff = datetime.now(UTC) - timedelta(days=_min_age_days)
            submitted = 0
            for user in db.query(User).all():
                try:
                    latest = (
                        db.query(DiscoverRun)
                        .filter(DiscoverRun.user_id == user.id)
                        .order_by(DiscoverRun.created_at.desc())
                        .first()
                    )
                    if latest is not None:
                        created_at = latest.created_at
                        if created_at is not None and created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=UTC)
                        # Don't pile onto a run that is still in flight.
                        if latest.status in ("queued", "running"):
                            logger.info(
                                "discover_refresh: user %s already has run %s in status %s — skipping",
                                user.id, latest.id, latest.status,
                            )
                            continue
                        if created_at is not None and created_at > cutoff:
                            logger.info(
                                "discover_refresh: user %s has a run newer than %dd — skipping",
                                user.id, _min_age_days,
                            )
                            continue
                    run_id = submit_discover_job(db, user.id)
                    submitted += 1
                    logger.info("discover_refresh: submitted run %s for user %s", run_id, user.id)
                except Exception:
                    logger.error("discover_refresh failed for user %s", user.id, exc_info=True)
                    db.rollback()
            logger.info("discover_refresh: submitted %d run(s)", submitted)
        finally:
            db.close()

    return register_cron_job(
        "discover_refresh", discover_refresh_inner,
        scheduler=scheduler, day_of_week="mon", hour=5, minute=0,
    )

def register_discovery_review_job(scheduler: Any | None = None) -> str:
    """Register the weekly Discovery config review job (P3d).

    Scores challenger configs against the current champion, applies Bonferroni +
    Deflated-Sharpe-Ratio (DSR) multi-testing correction with a t-stat threshold
    of 3, and auto-promotes the winner. Runs Sundays at 04:00 UTC, after the
    nightly resolution job has produced fresh outcomes.
    """
    from app.foundation.core.db import SessionLocal
    from app.decision.discover.config_review import discovery_review_inner

    def _inner() -> None:
        db = SessionLocal()
        try:
            discovery_review_inner(db)
        except Exception:
            logger.exception("discovery_config_review job failed")
            db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "discovery_config_review",
        _inner,
        scheduler=scheduler,
        hour=4,
        minute=0,
        day_of_week="sun",
    )

def analyst_estimates_snapshot_inner(db: Any) -> dict[str, int]:
    """Snapshot the consensus EPS of every name the latest completed
    Discover run evaluated (``data_backbone.analyst_estimates``)."""
    from app.foundation.data_backbone.analyst_estimates import snapshot_many
    from app.foundation.models.entities import DiscoverCandidate, DiscoverRun

    run = (
        db.query(DiscoverRun)
        .filter(DiscoverRun.status == "completed")
        .order_by(DiscoverRun.created_at.desc())
        .first()
    )
    if run is None:
        return {"stored_or_recent": 0, "no_data": 0}
    symbols = [c.symbol for c in db.query(DiscoverCandidate).filter(DiscoverCandidate.run_id == run.id).all()]
    return snapshot_many(db, symbols)


def register_analyst_estimates_snapshot_job(scheduler: Any | None = None) -> str:
    """Register the nightly analyst-estimate snapshot (04:45 UTC).

    Stores Yahoo's current-year consensus EPS trend for the names the latest
    Discover run evaluated, so the estimate-revision signal has a live
    source when the WRDS IBES extract is stale, and a point-in-time history
    builds up (owner decision 2026-09-29).
    """
    from app.foundation.core.db import SessionLocal

    def _inner() -> None:
        db = SessionLocal()
        try:
            analyst_estimates_snapshot_inner(db)
        except Exception:
            logger.exception("analyst_estimates_snapshot job failed")
            db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "analyst_estimates_snapshot",
        _inner,
        scheduler=scheduler,
        hour=4,
        minute=45,
    )

def register_discover_ml_training_job(scheduler: Any | None = None) -> str:
    """Register the weekly pooled ML-model training job.

    Refits ``lab.quant_lab.pooled_ml``'s cross-sectional model on the miner
    universe (Euro Stoxx 50 + S&P 100) from stored bars, evaluates it under
    purged walk-forward CV and persists a ``QuantMlModel`` row; only a model
    that clears the rank-IC gate gets an artefact, and ``stage_ml_signal``
    reads only the newest row. One model serves every user, so the row is
    owned by the user with the most recent completed DiscoverRun.

    Until 2026-09-28 this trained one triple-barrier classifier per
    shortlisted ticker; all were rejected against the majority-class
    baseline (ADR 0015 ruling #24 had already named the pooled target).

    Runs Saturdays at 03:00 UTC -- after the week's discover_refresh/
    discovery_resolution/discovery_config_review jobs have settled, clear
    of the daily advisor cycle.
    """
    _retrain_cooldown_days = 6

    def discover_ml_training_inner() -> None:
        from datetime import UTC, datetime, timedelta

        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import DiscoverRun
        from app.lab.alphacrafter.panel import build_panel
        from app.lab.alphacrafter.universe import MINER_UNIVERSE
        from app.lab.quant_lab import pooled_ml as pooled

        db = SessionLocal()
        try:
            latest = pooled.latest_pooled_row(db)
            now = datetime.now(UTC)
            if latest is not None and latest.created_at is not None:
                created = latest.created_at if latest.created_at.tzinfo else latest.created_at.replace(tzinfo=UTC)
                if created > now - timedelta(days=_retrain_cooldown_days):
                    logger.info("discover_ml_training: pooled model trained %s, within cooldown", created)
                    return
            owner = (
                db.query(DiscoverRun.user_id)
                .filter(DiscoverRun.status == "completed")
                .order_by(DiscoverRun.completed_at.desc())
                .first()
            )
            if owner is None:
                logger.info("discover_ml_training: no completed DiscoverRun yet, skipping")
                return
            panel = build_panel(db, MINER_UNIVERSE, now - timedelta(days=365 * 5), now, include_fundamentals=False)
            close = panel.get("close") if panel else None
            if close is None or close.empty:
                logger.warning("discover_ml_training: no stored bars for the miner universe")
                return
            row = pooled.train_pooled_model(db, owner[0], close)
            logger.info(
                "discover_ml_training: pooled model %s status=%s (%s)",
                row.id, row.status, row.error_message or "passed",
            )
        except Exception:
            logger.error("discover_ml_training failed", exc_info=True)
            db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "discover_ml_training", discover_ml_training_inner,
        scheduler=scheduler, day_of_week="sat", hour=3, minute=0,
    )
