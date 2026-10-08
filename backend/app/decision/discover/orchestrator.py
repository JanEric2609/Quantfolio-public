"""Discover pipeline orchestrator (Phase 3).

Runs the full discovery pipeline as a background job:
1. Build universe
2. Run pipeline stages (history_ingest → momentum_quality → backtest_vs_benchmark → verification_gate → portfolio_fit)
3. Assess tradeability for shortlisted candidates
4. Generate dossiers for shortlisted candidates
5. Create Recommendation + RecommendationDossier records
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.core.db import SessionLocal
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
)
from app.decision.discover.cancel_token import CancelToken as CancellationToken, CancellationError
from app.decision.discover.dossier_writer import (
    expected_return_over_trading_days,
    ledger_return_range,
    write_dossier,
)
from app.decision.discover.calibrator import calibrate_prediction
from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS
from app.decision.discover.predictor import store_prediction
from app.decision.discover.shadow_ledger import capture_snapshot, discover_stamp
from app.decision.discover.pipeline import (
    LLM_MAX_CANDIDATES,
    attach_earnings_calendar,
    candidate_sector,
    run_candidate_pipeline,
    sector_cap_for,
)
from app.decision.discover.state import DiscoveryState, make_stage_json
from app.decision.discover.tradeability import assess as assess_tradeability
from app.decision.discover.tradeability import unknown as unknown_tradeability
from app.decision.discover.config import get_or_seed_active_config
from app.decision.discover.universe import build_universe
from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.providers import build_provider_registry
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

# After this many consecutive LLM call failures during dossier generation,
# skip the LLM for the remaining candidates (deterministic template only)
# instead of paying the full HTTP timeout for every one of them.
LLM_FAILURE_BREAKER = 2


def run_discover(run_id: str) -> None:
    """Main orchestrator function called as a background job.

    Args:
        run_id: UUID of the DiscoverRun to process.
    """
    db = SessionLocal()
    try:
        _run_discover_inner(db, run_id)
    except Exception as exc:
        logger.exception("run_discover failed for run_id=%s: %s", run_id, exc)
        # The main session may be in an aborted (poisoned) transaction, so
        # rolling it back and re-reading on it is not reliable. Mark the run
        # failed on a fresh connection so it always reaches a terminal state.
        _mark_run_failed_fresh(run_id, f"Pipeline error: {exc}")
    finally:
        db.close()


def _mark_run_failed_fresh(run_id: str, error_message: str) -> None:
    """Record a failed DiscoverRun on a fresh session.

    Used from the top-level error path where the caller's session may be in an
    aborted transaction (InFailedSqlTransaction). A new connection guarantees
    the run reaches a terminal state and the real error is recorded.
    """
    from app.foundation.core.db import SessionLocal

    fresh = SessionLocal()
    try:
        run = fresh.get(DiscoverRun, run_id)
        if run is not None:
            _finalise_run(fresh, run, "failed", error_message=error_message)
    except Exception:
        logger.exception("Failed to mark run %s as failed", run_id)
    finally:
        fresh.close()


def _last_write(run: DiscoverRun) -> datetime | None:
    """``run.updated_at`` normalised to tz-aware, falling back to created_at.

    ``updated_at`` is bumped by the ORM on every ``_update_stage_json``
    commit; it's a DB timestamp, so — unlike stage_json's own
    ``elapsed_seconds`` (measured against the writing process's private
    ``time.time()`` epoch) — it stays meaningful even if the process that
    was running the pipeline died and was replaced by a different one. Rows
    written before the column existed have it NULL; created_at is the best
    available fallback for those.
    """
    ts = run.updated_at or run.created_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def reap_stale_discover_runs(
    db: Session,
    max_age_minutes: float = 35.0,
    stale_progress_minutes: float = 10.0,
    dossier_complete_grace_minutes: float = 3.0,
) -> int:
    """Reconcile DiscoverRun rows left inconsistent by an interrupted worker
    process (e.g. a deploy restart killing the process mid-pipeline).

    Three situations, all caused by the process dying rather than by the
    pipeline's own state machine reaching a terminal state:

      1. status "running"/"cancellation_requested" with no stage_json write
         in over ``stale_progress_minutes`` — no writer is left alive to
         ever finish these, so mark them "failed" instead of leaving a
         permanent zombie row. ``max_age_minutes`` (measured from
         created_at) is kept as a coarser backstop for rows written before
         the ``updated_at`` column existed (where ``_last_write`` falls back
         to created_at, so the two checks coincide).
      2. status "running" with the dossier-writing stage already at 100%
         but no further write in over ``dossier_complete_grace_minutes`` —
         the only work left at that point (prediction-ledger write,
         calibration, ``_finalise_run``) is fast, best-effort, in-process DB
         work with nothing left to wait on, so a stall there this long means
         the process died before reaching ``_finalise_run``, not that it's
         still working. Promote to "completed" — the dossiers exist and are
         the data that matters. A short grace window (much shorter than the
         general staleness check) recovers this common case quickly instead
         of waiting for a full ``max_age_minutes``.
      3. status "cancelled" even though stage_json shows the dossier stage
         already reached 100% — ``raise_if_cancelled()`` is checked before
         every dossier write, so a genuine user-initiated cancellation would
         essentially never land exactly at full completion. In practice this
         happens when the process is killed after the pipeline finished all
         real work (dossiers written) but before ``_finalise_run(...,
         "completed")`` committed. Promote these to "completed" instead of
         permanently mislabelling finished data as cancelled.

    Called on process startup (mirrors ``reap_stale_job_runs`` for
    AlphaCrafter, in ``app.main.lifespan`` and ``app.worker.build_scheduler``)
    and periodically from the worker scheduler, so a run orphaned mid-flight
    doesn't sit stuck until the next process restart.
    """
    now = datetime.now(UTC)
    reconciled = 0

    active = db.query(DiscoverRun).filter(
        DiscoverRun.status.in_(["running", "cancellation_requested"]),
    ).all()
    for run in active:
        if _reconcile_run(
            run, now,
            max_age_minutes=max_age_minutes,
            stale_progress_minutes=stale_progress_minutes,
            dossier_complete_grace_minutes=dossier_complete_grace_minutes,
        ):
            reconciled += 1

    mislabelled = db.query(DiscoverRun).filter(DiscoverRun.status == "cancelled").all()
    for run in mislabelled:
        if _dossier_stage_complete(run):
            run.status = "completed"
            run.completed_at = run.completed_at or now
            reconciled += 1

    if reconciled:
        db.commit()
        logger.warning("Reconciled %d stale/mislabelled discover run(s)", reconciled)
    return reconciled


def _reconcile_run(
    run: DiscoverRun,
    now: datetime,
    *,
    max_age_minutes: float,
    stale_progress_minutes: float,
    dossier_complete_grace_minutes: float,
) -> bool:
    """Apply cases 1-2 of ``reap_stale_discover_runs`` to a single run.

    Mutates *run* in place and returns whether it changed. Caller owns the
    commit — shared by the bulk sweep and the on-read self-heal in
    ``reap_run_if_stale``.
    """
    last_write = _last_write(run)
    if last_write is None:
        return False
    dossier_grace_cutoff = now - timedelta(minutes=dossier_complete_grace_minutes)
    if run.status == "running" and _dossier_stage_complete(run):
        if last_write < dossier_grace_cutoff:
            run.status = "completed"
            run.completed_at = now
            return True
        return False
    created = run.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_cutoff = now - timedelta(minutes=max_age_minutes)
    progress_cutoff = now - timedelta(minutes=stale_progress_minutes)
    if last_write < progress_cutoff or (created is not None and created < age_cutoff):
        run.status = "failed"
        run.error_message = "Interrupted by service restart"
        run.completed_at = now
        return True
    return False


def reap_run_if_stale(
    db: Session,
    run: DiscoverRun,
    *,
    dossier_complete_grace_minutes: float = 3.0,
    stale_progress_minutes: float = 10.0,
    max_age_minutes: float = 35.0,
) -> bool:
    """Opportunistically reconcile *run* on read, not just from the periodic
    sweep.

    The periodic reap (``reap_stale_discover_runs``, run from the worker
    process on a 5-minute interval) only catches an orphaned run once its
    interval next fires, which can leave a run stuck as "running" for up to
    5 minutes after it's actually eligible for reconciliation. Since
    ``GET /runs/{id}`` and the debug endpoint are polled by the frontend
    while a run is active anyway, checking staleness there too closes that
    gap immediately for whoever is actually looking at the run, independent
    of the worker process's schedule or even whether it's running at all.
    """
    if run.status not in ("running", "cancellation_requested"):
        return False
    now = datetime.now(UTC)
    changed = _reconcile_run(
        run, now,
        max_age_minutes=max_age_minutes,
        stale_progress_minutes=stale_progress_minutes,
        dossier_complete_grace_minutes=dossier_complete_grace_minutes,
    )
    if changed:
        db.commit()
        db.refresh(run)
        logger.warning("Reconciled stale discover run %s on read", run.id)
    return changed


def _dossier_stage_complete(run: DiscoverRun) -> bool:
    """True if stage_json shows the dossier-writing stage reached 100%."""
    if not run.stage_json:
        return False
    try:
        stage = json.loads(run.stage_json).get("discover", {})
    except (TypeError, ValueError):
        return False
    if stage.get("state") != DiscoveryState.WRITING_DOSSIER.value:
        return False
    total = stage.get("total_candidates")
    processed = stage.get("processed_candidates")
    return bool(total) and processed is not None and processed >= total


def _run_discover_inner(db: Session, run_id: str) -> None:
    run = db.get(DiscoverRun, run_id)
    if run is None:
        raise ValueError(f"DiscoverRun {run_id} not found")

    # Don't resurrect a run in terminal or cancellation-requested state
    if run.status in ("completed", "failed", "cancelled", "cancellation_requested"):
        logger.info("run %s already in state %s — skipping", run_id, run.status)
        return

    run.status = "running"
    db.commit()

    raw_params = json.loads(run.params_json) if run.params_json else {}
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    focus = params.get("focus")
    raw_profile = params.get("profile", {})
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    user_id = run.user_id

    cancel_token = CancellationToken(db, run_id)
    registry = build_provider_registry(db)
    started_at = time.time()

    # ------------------------------------------------------------------
    # 1. Build universe
    # ------------------------------------------------------------------
    _update_stage_json(db, run, make_stage_json(
        DiscoveryState.BUILDING_UNIVERSE,
        message="Building candidate universe...",
    ))

    universe = build_universe(db, user_id, focus=focus, registry=registry)
    candidates: list[DiscoverCandidate] = []
    # Retain ETF metadata (tf_class, domicile, distribution) for ETF-sourced
    # candidates so the dossier pipeline can include them in tradeable_json.
    etf_meta_by_symbol: dict[str, dict[str, str]] = {}
    for item in universe:
        cand = DiscoverCandidate(
            id=str(uuid.uuid4()),
            run_id=run_id,
            symbol=item["symbol"],
            isin=item.get("isin"),
            name=item.get("name"),
            source=item.get("source", "unknown"),
            status="pending",
            scores_json="{}",
            tradeable_json="{}",
        )
        db.add(cand)
        candidates.append(cand)
        if item.get("source") == "screen_etf":
            etf_meta_by_symbol[item["symbol"]] = {
                "tf_class": item.get("tf_class", "aktien"),
                "domicile": item.get("domicile_country", ""),
                "distribution_policy": item.get("distribution_policy", ""),
            }
    db.commit()
    for c in candidates:
        db.refresh(c)

    _update_stage_json(db, run, make_stage_json(
        DiscoveryState.BUILDING_UNIVERSE,
        total_candidates=len(candidates),
        message=f"Universe built: {len(candidates)} candidates",
    ))

    # ------------------------------------------------------------------
    # 2. Run pipeline stages with per-candidate progress tracking
    # ------------------------------------------------------------------
    pipeline_results: list[dict[str, Any]] = []

    try:
        try:
            _update_stage_json(db, run, make_stage_json(
                DiscoveryState.PIPELINE_CANDIDATES,
                total_candidates=len(candidates),
                processed_candidates=0,
                message="Warming price history cache (this can take a few minutes)...",
            ))

            def _preingest_progress(done: int, total: int, symbol: str) -> None:
                # Surface a live counter during the opaque warm-up phase so the
                # UI does not appear frozen at 0/N. We keep processed_candidates
                # at 0 (no candidate has been scored yet) and communicate warm-up
                # progress through the message + current_candidate fields.
                _update_stage_json(db, run, make_stage_json(
                    DiscoveryState.PIPELINE_CANDIDATES,
                    total_candidates=len(candidates),
                    processed_candidates=0,
                    current_candidate=symbol,
                    current_stage="warming_price_cache",
                    elapsed_seconds=time.time() - started_at,
                    warmup_done=done,
                    warmup_total=total,
                    message=f"Warming price history: {done}/{total} symbols",
                ))

            pipeline_gen = run_candidate_pipeline(
                db, user_id, universe, profile,
                cancel_token=cancel_token,
                results_out=pipeline_results,
                preingest_progress=_preingest_progress,
                run_id=run_id,
            )
            # Use while+next to capture generator return value (the shortlist).
            # A plain for-loop discards StopIteration.value.
            top_shortlisted: list[dict[str, Any]] = []
            try:
                benchmark_ingest: dict[str, Any] | None = None
                while True:
                    progress = next(pipeline_gen)
                    if progress.get("benchmark_ingest") is not None:
                        benchmark_ingest = progress["benchmark_ingest"]
                    _update_stage_json(db, run, make_stage_json(
                        DiscoveryState.PIPELINE_CANDIDATES,
                        total_candidates=progress["total"],
                        processed_candidates=progress["index"],
                        current_candidate=progress["symbol"],
                        current_stage=progress["stage"],
                        elapsed_seconds=time.time() - started_at,
                        message=f"Processing {progress['symbol']}...",
                        benchmark_ingest=benchmark_ingest,
                    ))
            except StopIteration as e:
                top_shortlisted = e.value or []
        except CancellationError:
            logger.info("Pipeline cancelled for run %s", run_id)
            _finalise_run(db, run, "cancelled", started_at=started_at)
            return
        except Exception as exc:
            db.rollback()
            logger.exception("Pipeline failed for run %s", run_id)
            _update_stage_json(db, run, make_stage_json(
                DiscoveryState.FAILED,
                message=f"Pipeline failed: {exc}",
            ))
            raise

        top_symbols = {r["symbol"] for r in top_shortlisted}

        # Map pipeline results back to DB rows using the generator's shortlist.
        result_by_symbol = {r["symbol"]: r for r in pipeline_results}
        for cand in candidates:
            result = result_by_symbol.get(cand.symbol)
            if result:
                raw_scores = result.get("scores")
                scores = raw_scores if isinstance(raw_scores, dict) else {}
                scores["composite"] = result.get("composite_score", 0.0)
                scores["concerns"] = result.get("concerns", [])
                cand.scores_json = json.dumps(scores)
                cand.reject_stage = result.get("reject_stage")
                cand.reject_reason = result.get("reject_reason")
                if cand.symbol in top_symbols:
                    cand.status = "shortlisted"
                else:
                    cand.status = "rejected"
                    if result.get("reject_stage") is None and result.get("sector_capped"):
                        cand.reject_stage = "sector_cap"
                        cand.reject_reason = (
                            f"Sector cap: {sector_cap_for(LLM_MAX_CANDIDATES)} higher-ranked "
                            f"{candidate_sector(result)} names already shortlisted"
                        )
                    elif result.get("reject_stage") is None:
                        cand.reject_stage = "shortlist_limit"
                        cand.reject_reason = (
                            f"Below composite shortlist cutoff (top {LLM_MAX_CANDIDATES})"
                        )
            else:
                cand.status = "rejected"
                cand.reject_stage = "pipeline"
                cand.reject_reason = "Missing from pipeline results"
        db.commit()

        passed = len([c for c in candidates if c.status != "rejected"])
        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.PIPELINE_CANDIDATES,
            total_candidates=len(candidates),
            processed_candidates=passed,
            message=f"Pipeline: {passed}/{len(candidates)} passed",
            benchmark_ingest=benchmark_ingest,
        ))

        # ------------------------------------------------------------------
        # 3. Tradeability assessment for shortlisted candidates
        # ------------------------------------------------------------------
        cancel_token.raise_if_cancelled()
        shortlisted = [c for c in candidates if c.status == "shortlisted"]

        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.ASSESSING_TRADEABILITY,
            total_candidates=len(shortlisted),
            message="Assessing tradeability...",
        ))

        for cand in shortlisted:
            cancel_token.raise_if_cancelled()
            try:
                tradeable = assess_tradeability(
                    db, cand.symbol, cand.isin, cand.name or cand.symbol,
                    instrument_type=classify_instrument(cand.symbol, cand.source, cand.name or cand.symbol),
                )
                # Enrich with ETF metadata so the dossier has tax-relevant fields.
                meta = etf_meta_by_symbol.get(cand.symbol, {})
                if meta:
                    tradeable.setdefault("teilfreistellung_class", meta.get("tf_class", "aktien"))
                    tradeable.setdefault("domicile", meta.get("domicile", ""))
                    tradeable.setdefault("distribution_policy", meta.get("distribution_policy", ""))
                cand.tradeable_json = json.dumps(tradeable)
            except Exception as exc:
                db.rollback()
                logger.warning("Tradeability assessment failed for %s: %s", cand.symbol, exc)
                fallback = unknown_tradeability(cand.isin, str(exc))
                meta = etf_meta_by_symbol.get(cand.symbol, {})
                if meta:
                    fallback["teilfreistellung_class"] = meta.get("tf_class", "aktien")
                    fallback["domicile"] = meta.get("domicile", "")
                    fallback["distribution_policy"] = meta.get("distribution_policy", "")
                cand.tradeable_json = json.dumps(fallback)
        db.commit()
        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.ASSESSING_TRADEABILITY,
            total_candidates=len(shortlisted),
            processed_candidates=len(shortlisted),
            message=f"Tradeability assessed for {len(shortlisted)} candidates",
        ))

        # ------------------------------------------------------------------
        # 3b. Tradeability gate: skip dossier generation for candidates
        #     assessed as not tradeable, then backfill from the next-ranked
        #     evaluable candidates so the gate doesn't silently starve the
        #     shortlist (a candidate rejected here consumed a composite-rank
        #     slot for nothing; rank 16+ never got a chance).
        # ------------------------------------------------------------------
        tradeable_shortlisted: list[DiscoverCandidate] = []
        for cand in shortlisted:
            raw_tradeable = json.loads(cand.tradeable_json) if cand.tradeable_json else {}
            tradeable = raw_tradeable if isinstance(raw_tradeable, dict) else {}
            if tradeable.get("likely_tradeable") is not False:
                tradeable_shortlisted.append(cand)
            else:
                cand.status = "rejected"
                cand.reject_stage = "tradeability_gate"
                cand.reject_reason = "Not likely tradeable at DKB or Scalable"

        if len(tradeable_shortlisted) < LLM_MAX_CANDIDATES:
            cand_by_symbol = {c.symbol: c for c in candidates}
            shortlisted_symbols = {c.symbol for c in shortlisted}
            backfill_pool = sorted(
                (
                    r for r in pipeline_results
                    if r.get("reject_stage") is None and r["symbol"] not in shortlisted_symbols
                ),
                key=lambda r: r.get("composite_score", 0.0),
                reverse=True,
            )
            # The backfill honours the same per-sector cap as the pipeline's
            # own shortlist, or a rejected slot would reopen the sector bet.
            sector_cap = sector_cap_for(LLM_MAX_CANDIDATES)
            sector_counts: dict[str, int] = {}
            for kept in tradeable_shortlisted:
                kept_sector = candidate_sector(result_by_symbol.get(kept.symbol) or {})
                if kept_sector is not None:
                    sector_counts[kept_sector] = sector_counts.get(kept_sector, 0) + 1
            for result in backfill_pool:
                if len(tradeable_shortlisted) >= LLM_MAX_CANDIDATES:
                    break
                cand = cand_by_symbol.get(result["symbol"])
                if cand is None:
                    continue
                sector = candidate_sector(result)
                if sector is not None and sector_counts.get(sector, 0) >= sector_cap:
                    continue
                try:
                    tradeable_res = assess_tradeability(
                        db, cand.symbol, cand.isin, cand.name or cand.symbol,
                        instrument_type=classify_instrument(cand.symbol, cand.source, cand.name or cand.symbol),
                    )
                    tradeable = tradeable_res if isinstance(tradeable_res, dict) else {}
                    meta = etf_meta_by_symbol.get(cand.symbol, {})
                    if meta and isinstance(meta, dict):
                        tradeable.setdefault("teilfreistellung_class", meta.get("tf_class", "aktien"))
                        tradeable.setdefault("domicile", meta.get("domicile", ""))
                        tradeable.setdefault("distribution_policy", meta.get("distribution_policy", ""))
                    cand.tradeable_json = json.dumps(tradeable)
                except Exception as exc:
                    db.rollback()
                    logger.warning("Backfill tradeability assessment failed for %s: %s", cand.symbol, exc)
                    tradeable = unknown_tradeability(cand.isin, str(exc))
                    cand.tradeable_json = json.dumps(tradeable)
                if tradeable.get("likely_tradeable") is not False:
                    cand.status = "shortlisted"
                    cand.reject_stage = None
                    cand.reject_reason = None
                    tradeable_shortlisted.append(cand)
                    if sector is not None:
                        sector_counts[sector] = sector_counts.get(sector, 0) + 1
                else:
                    cand.status = "rejected"
                    cand.reject_stage = "tradeability_gate"
                    cand.reject_reason = "Not likely tradeable at DKB or Scalable"

        shortlisted = tradeable_shortlisted
        db.commit()

        # Resolve active configs for the pipeline run so the dossier writer and
        # prediction ledger use the same version throughout.
        signal_cfg = get_or_seed_active_config(db, "signal_weights")
        prompt_cfg = get_or_seed_active_config(db, "prompt_template")
        prompt_json = prompt_cfg.config_json if isinstance(prompt_cfg.config_json, dict) else {}

        # ------------------------------------------------------------------
        # 3c. Shadow ledger (ADR 0018 §5): freeze every scored candidate as
        #     issued, before dossiers rewrite any working row. Best-effort:
        #     evidence capture must never fail the run.
        # ------------------------------------------------------------------
        stamp = _run_stamp(db, run_id, signal_cfg, prompt_json)
        try:
            capture_snapshot(
                db,
                run_id=run_id,
                user_id=user_id,
                issued_at=datetime.now(UTC),
                pipeline_results=pipeline_results,
                shortlisted_symbols=top_symbols,
                picked_symbols={c.symbol for c in shortlisted},
                stamp=stamp,
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Shadow-ledger snapshot failed for run %s", run_id)

        # ------------------------------------------------------------------
        # 4. Dossier generation for shortlisted candidates
        # ------------------------------------------------------------------
        cancel_token.raise_if_cancelled()

        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.WRITING_DOSSIER,
            total_candidates=len(shortlisted),
            message="Generating dossiers...",
        ))

        dossier_count, ledger_fields_by_symbol = _generate_dossiers(
            db, run, shortlisted, profile,
            prompt_config=prompt_json,
            cancel_token=cancel_token,
            started_at=started_at,
            config_id=signal_cfg.id,
        )

        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.WRITING_DOSSIER,
            total_candidates=len(shortlisted),
            processed_candidates=dossier_count,
            elapsed_seconds=time.time() - started_at,
            message=f"Dossiers: {dossier_count}/{len(shortlisted)} generated",
        ))

        # ------------------------------------------------------------------
        # 4b. Prediction ledger (P0 — #111) + calibration (P2)
        # ------------------------------------------------------------------
        # Create one DiscoveryPrediction per shortlist candidate, then calibrate
        # conviction. Best-effort: failure must never fail the discovery run.
        try:
            created_predictions = []
            for c in shortlisted:
                result = result_by_symbol.get(c.symbol)
                if result is None:
                    continue
                raw_scores = result.get("scores")
                scores = raw_scores if isinstance(raw_scores, dict) else {}
                signal_breakdown = {
                    "composite_score": scores.get("composite", 0.0),
                    # Pre-penalty composite: the recency penalty's
                    # "improved since last time" exemption compares against it.
                    "composite_raw": scores.get("composite_raw"),
                    "composite_inputs": scores.get("composite_inputs"),
                    "momentum_quality": scores.get("momentum_quality"),
                    "backtest_vs_benchmark": scores.get("backtest_vs_benchmark"),
                    "verification_gate": scores.get("verification_gate"),
                    "quant_signals": scores.get("quant_signals"),
                    "portfolio_fit": scores.get("portfolio_fit"),
                    "alpha_miner": scores.get("alpha_miner"),
                    "alpha_screener": scores.get("alpha_screener"),
                    "sentiment_fundamentals": scores.get("sentiment_fundamentals"),
                    "history_ingest": scores.get("history_ingest"),
                    "concerns": result.get("concerns", []),
                }
                composite = result.get("composite_score", 0.0)
                ledger_fields = dict(ledger_fields_by_symbol.get(c.symbol, {}))
                generated_by = ledger_fields.pop("dossier_generated_by", None)
                pred = store_prediction(
                    db,
                    symbol=c.symbol,
                    composite_score=composite,
                    signal_breakdown=signal_breakdown,
                    direction_hint="buy",
                    user_id=user_id,
                    run_id=run_id,
                    isin=c.isin,
                    config_id=signal_cfg.id,
                    horizon_days=DEFAULT_HORIZON_DAYS,
                    provenance_json=_prediction_stamp(stamp, generated_by),
                    **ledger_fields,
                )
                created_predictions.append(pred)

            # Durable checkpoint: predictions are the valuable artifact and
            # must survive a downstream calibration failure. Without this
            # commit, a rollback in the calibration loop below would unwind
            # every prediction created above, not just the failed one.
            db.commit()

            # Calibrate each prediction's conviction (best-effort). Commit
            # after each success so a later failure's rollback discards only
            # that failed calibration attempt, not earlier ones in this loop.
            for pred in created_predictions:
                try:
                    calibrate_prediction(db, pred.id, user_id=user_id)
                    db.commit()
                except Exception:
                    logger.exception("Calibration failed for prediction %s", pred.id)

            logger.info("orchestrator: stored %d predictions for run %s", len(created_predictions), run_id)
        except Exception:
            logger.exception("Prediction ledger write failed for run %s", run_id)

        # ------------------------------------------------------------------
        # 5. Finalise
        # ------------------------------------------------------------------
        # An immediate-mode cancel (api/discover.py cancel_run) sets status
        # directly and isn't gated by the cooperative checks above, so it can
        # land in the gap between the last raise_if_cancelled() call and here.
        # Re-check right before declaring success so that race can't get
        # silently overwritten back to "completed".
        cancel_token.raise_if_cancelled()
        _finalise_run(db, run, "completed", started_at=started_at)
        logger.info("Discover run %s completed: %d candidates, %d dossiers", run_id, len(candidates), dossier_count)
    except CancellationError:
        logger.info("Discover run %s cancelled (late stage)", run_id)
        _finalise_run(db, run, "cancelled", started_at=started_at)
        return


def _run_stamp(db: Session, run_id: str, signal_cfg: Any, prompt_json: dict[str, Any]) -> dict[str, Any]:
    """The run's provenance stamp (ADR 0018 §10); a failure degrades it, never the run."""
    config_json = signal_cfg.config_json if isinstance(signal_cfg.config_json, dict) else {}
    try:
        return discover_stamp(
            db,
            run_id=run_id,
            signal_config=config_json,
            signal_config_id=signal_cfg.id,
            prompt_config=prompt_json,
            horizon_days=DEFAULT_HORIZON_DAYS,
        )
    except Exception as exc:
        logger.exception("Provenance stamp failed for run %s", run_id)
        return {
            "provenance_schema_version": 0,
            "source": "discover",
            "run_id": run_id,
            "degradation_flags": ["stamp_failed"],
            "error": str(exc)[:200],
        }


def _prediction_stamp(stamp: dict[str, Any], dossier_generated_by: str | None) -> dict[str, Any]:
    """The run stamp plus how this pick's dossier was written (LLM or template)."""
    out = dict(stamp)
    out["dossier_generated_by"] = dossier_generated_by
    if dossier_generated_by not in (None, "llm"):
        out["degradation_flags"] = sorted({*stamp.get("degradation_flags", []), "dossier_not_llm"})
    return out


def _generate_dossiers(
    db: Session,
    run: DiscoverRun,
    shortlisted: list[DiscoverCandidate],
    profile: dict[str, Any],
    *,
    prompt_config: dict | None,
    cancel_token: CancellationToken,
    started_at: float,
    config_id: str | None = None,
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Generate one dossier per shortlisted candidate with live progress.

    Emits a per-candidate stage_json update (so the UI and the staleness
    detector in the debug endpoint always see forward progress) and opens a
    circuit breaker after ``LLM_FAILURE_BREAKER`` consecutive LLM call
    failures so a dead LLM endpoint costs at most two timeouts, not one per
    candidate.

    Returns the number of dossiers successfully written, and a symbol ->
    ``{"expected_return", "thesis"}`` map of prediction-ledger fields (the
    estimate restated over the ledger horizon) so the caller can thread them
    into the ledger without recomputing them.
    """
    dossier_count = 0
    consecutive_llm_failures = 0
    llm_circuit_open = False
    ledger_fields_by_symbol: dict[str, dict[str, Any]] = {}
    track_record_cache: dict[str, dict[str, Any]] = {}
    for idx, cand in enumerate(shortlisted):
        cancel_token.raise_if_cancelled()
        _update_stage_json(db, run, make_stage_json(
            DiscoveryState.WRITING_DOSSIER,
            total_candidates=len(shortlisted),
            processed_candidates=idx,
            current_candidate=cand.symbol,
            elapsed_seconds=time.time() - started_at,
            message=(
                f"Generating dossier {idx + 1}/{len(shortlisted)}: {cand.symbol}"
                + (" (LLM unavailable — using quant template)" if llm_circuit_open else "")
            ),
        ))
        try:
            if classify_instrument(cand.symbol, cand.source, cand.name or cand.symbol) == "equity":
                _attach_earnings(db, cand)
            candidate_dict = _candidate_to_dict(cand)
            raw_dossier = write_dossier(
                db, candidate_dict, profile,
                prompt_config=prompt_config,
                skip_llm=llm_circuit_open,
                config_id=config_id,
                track_record_cache=track_record_cache,
            )
            dossier: dict[str, Any] = raw_dossier if isinstance(raw_dossier, dict) else {}
            reason = dossier.get("fallback_reason") or ""
            if dossier.get("generated_by") == "llm":
                consecutive_llm_failures = 0
            elif reason.startswith("llm_error"):
                consecutive_llm_failures += 1
                if consecutive_llm_failures >= LLM_FAILURE_BREAKER and not llm_circuit_open:
                    llm_circuit_open = True
                    logger.warning(
                        "run %s: %d consecutive LLM failures — using deterministic dossiers for remaining candidates",
                        run.id, consecutive_llm_failures,
                    )
            # write_dossier nests the dossier under "dossier"; reading
            # expected_return off the top level stored NULL for every
            # Discover prediction since #210. The ledger wants the estimate
            # over its own horizon, as a fraction like realised_return.
            nested = dossier.get("dossier")
            written: dict[str, Any] = nested if isinstance(nested, dict) else {}
            thesis = written.get("thesis")
            ledger_expected = expected_return_over_trading_days(
                dossier.get("annual_estimate"), DEFAULT_HORIZON_DAYS,
            )
            range_low, range_high = ledger_return_range(
                db, cand.symbol, ledger_expected, DEFAULT_HORIZON_DAYS,
            )
            ledger_fields_by_symbol[cand.symbol] = {
                "expected_return": ledger_expected,
                "expected_return_low": range_low,
                "expected_return_high": range_high,
                "thesis": thesis if isinstance(thesis, str) and thesis.strip() else None,
                # Provenance only (popped before store_prediction).
                "dossier_generated_by": dossier.get("generated_by") or "unknown",
            }
            if dossier.get("dossier_id"):
                cand.dossier_id = dossier["dossier_id"]
                cand.recommendation_id = dossier.get("recommendation_id")
                db.commit()
                dossier_count += 1
        except CancellationError:
            raise
        except Exception as exc:
            db.rollback()
            logger.exception("Dossier generation failed for %s in run %s", cand.symbol, run.id)
            _update_stage_json(db, run, make_stage_json(
                DiscoveryState.WRITING_DOSSIER,
                total_candidates=len(shortlisted),
                processed_candidates=idx,
                message=f"Dossier failed for {cand.symbol}: {exc}",
            ))
    return dossier_count, ledger_fields_by_symbol


def submit_discover_job(db: Session, user_id: str, focus: str | None = None) -> str:
    """Create a DiscoverRun and submit it as a background job.

    Args:
        db: Database session (for creating the run record)
        user_id: User who initiated the run
        focus: Optional focus area

    Returns:
        The DiscoverRun id (run_id).
    """
    profile = get_public_settings(db)
    # Snapshot only non-sensitive settings
    safe_profile = {
        "user_id": user_id,
        "currency": profile.get("currency", "EUR"),
        "risk_profile": profile.get("risk_profile", "moderate"),
        "tax_residency_country": profile.get("tax_residency_country", "DE"),
        "church_tax": profile.get("church_tax", "none"),
        "freistellungsauftrag_amount": profile.get("freistellungsauftrag_amount", 1000),
    }

    run = DiscoverRun(
        id=str(uuid.uuid4()),
        user_id=user_id,
        status="queued",
        stage_json=json.dumps({}),
        params_json=json.dumps({"focus": focus, "profile": safe_profile}),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # One-directional edge into the generic job infra (Phase E): kept lazy
    # per house style; jobs no longer imports discover back.
    from app.foundation.jobs import submit_job

    submit_job(run_discover, args=(run.id,), job_id=f"discover_{run.id}")
    logger.info("Submitted discover job %s for user %s focus=%s", run.id, user_id, focus)
    return run.id


def _attach_earnings(db: Session, cand: DiscoverCandidate) -> None:
    """Store the next earnings report in a shortlisted stock's scores."""
    try:
        scores = json.loads(cand.scores_json) if cand.scores_json else {}
    except ValueError:
        return
    if not isinstance(scores, dict):
        return
    cand.scores_json = json.dumps(attach_earnings_calendar(db, cand.symbol, scores))
    db.commit()


def _candidate_to_dict(cand: DiscoverCandidate) -> dict[str, Any]:
    return {
        "id": cand.id,
        "run_id": cand.run_id,
        "symbol": cand.symbol,
        "isin": cand.isin,
        "name": cand.name,
        "source": cand.source,
        "status": cand.status,
        "reject_stage": cand.reject_stage,
        "reject_reason": cand.reject_reason,
        "scores_json": cand.scores_json,
        "tradeable_json": cand.tradeable_json,
        "dossier_id": cand.dossier_id,
        "recommendation_id": cand.recommendation_id,
    }


def _update_stage_json(db: Session, run: DiscoverRun, data: dict[str, Any]) -> None:
    stages: dict[str, Any] = json.loads(run.stage_json) if run.stage_json else {}
    stages["discover"] = data
    run.stage_json = json.dumps(stages)
    db.commit()


def _finalise_run(
    db: Session,
    run: DiscoverRun,
    status: str,
    *,
    started_at: float | None = None,
    error_message: str | None = None,
) -> None:
    """Set run status, completed_at, optional error."""
    run.status = status
    run.completed_at = datetime.now(UTC)
    if error_message:
        run.error_message = error_message
    db.commit()
    if started_at:
        elapsed = time.time() - started_at
        _update_stage_json(db, run, make_stage_json(
            DiscoveryState(status),
            message=f"Run {status} in {elapsed:.0f}s",
        ))
