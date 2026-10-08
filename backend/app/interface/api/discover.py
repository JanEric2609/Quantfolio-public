"""API Router for Discover pipeline (Phase 3).

Endpoints:
- POST /api/discover/runs          — start a new discover run
- GET  /api/discover/runs          — list run summaries
- GET  /api/discover/runs/{id}     — full run with candidates
- GET  /api/discover/dossiers/{id} — dossier by id
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverCandidateSnapshot,
    DiscoverRun,
    DiscoveryConfig,
    DiscoveryConfigReview,
    DiscoverySkillSnapshot,
    Recommendation,
    RecommendationDossier,
    User,
)
from app.foundation.auth import current_user, require_admin
from app.decision.discover.config import (
    activate_config,
    create_config,
    get_config,
    is_config_stale,
    list_configs,
    sync_config_to_default,
)
from app.decision.discover.config_perturb import (
    generate_prompt_perturbations,
    generate_weight_perturbations,
)
from app.decision.discover import skill_summary
from app.decision.discover.orchestrator import reap_run_if_stale, submit_discover_job
from app.decision.discover.state import DiscoveryState


def _safe_json_loads(value: str | None) -> dict[str, Any]:
    """Parse JSON string to dict, returning {} on null, parse failure, or non-dict."""
    if not value:
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discover", tags=["discover"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    focus: str | None = None
    # The UI sends a free-form `description` textarea; treat it as the focus
    # hint so the universe builder can specialise the candidate pool.
    description: str | None = None


class StartRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    created_at: str | None
    completed_at: str | None
    stage_summary: str


class CandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    symbol: str
    isin: str | None
    name: str | None
    source: str
    status: str
    reject_stage: str | None
    reject_reason: str | None
    scores: dict[str, Any]
    tradeable: dict[str, Any]
    dossier_id: str | None
    recommendation_id: str | None
    # Dossier-derived fields (null unless dossier exists)
    conviction: float | None = None
    horizon_months: int | None = None
    expected_return: float | None = None
    # Which backward-looking signal `expected_return` was anchored on. The UI
    # gates its "historical figure, not a forecast" disclaimer on this — the
    # mitigation added after the BBVA.MC/XEON.DE incidents — so leaving it off
    # the wire silently disabled that disclaimer everywhere it was meant to show.
    expected_return_anchor_kind: str | None = None
    # Track B2: the candidate's cohort track-record gate status
    # ("insufficient_data" | "proven" | "unproven") — null when the
    # candidate has no recommendation yet, or predates this gate.
    track_record_status: str | None = None
    # True only when track_record_status == "unproven" — the candidate was
    # auto-withheld (approval_state left at "draft") rather than
    # auto-approved. Withheld candidates stay visible here with this badge;
    # they are never silently hidden.
    withheld: bool = False
    # Rank by composite among the run's evaluable stocks, frozen in the shadow
    # ledger (ADR 0018 §6: the score is shown as a tier, not a probability).
    # Null for ETFs, rejected names, and runs before the ledger existed.
    pool_rank: int | None = None
    pool_size: int | None = None


class RunDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage_json: dict[str, Any]
    params_json: dict[str, Any]
    error_message: str | None
    created_at: str | None
    completed_at: str | None
    candidates: list[CandidateResponse]
    shortlisted: list[CandidateResponse]
    rejected: list[CandidateResponse]


class DossierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conviction: float
    direction: str = "neutral"
    horizon_months: int = 6
    expected_return: float | None = None
    # Which backward-looking signal `expected_return` was anchored on. The UI
    # gates its "historical figure, not a forecast" disclaimer on this — the
    # mitigation added after the BBVA.MC/XEON.DE incidents — so leaving it off
    # the wire silently disabled that disclaimer everywhere it was meant to show.
    expected_return_anchor_kind: str | None = None
    # PRIIPs-style P10/P50/P90 total-return band around the anchor
    # ({p10,p50,p90,method,n_paths,estimate,not_tax_advice,concerns}); omitted
    # when no anchor/db history supports it (M5 Option 0).
    expected_return_band: dict[str, Any] | None = None
    # How the point estimate was built (ADR 0017): cash rate, adjusted beta,
    # equity premium, market-implied prior, anchor, credibility weight, tilt.
    # Null for dossiers written before it existed or with no anchor.
    expected_return_components: dict[str, Any] | None = None
    # Every composite input as {signal, score, weight, contribution}, largest
    # contribution first; null for dossiers written before ADR 0017.
    composite_breakdown: list[dict[str, Any]] | None = None
    thesis: str = ""
    key_risks: list[str] = []
    signal_breakdown: dict[str, Any] = {}
    # Non-directional macro/volatility regime context surfaced for instruments
    # (e.g. passive ETFs) whose directional ML signal is unavailable by design.
    regime_context: dict[str, Any] | None = None
    estimate: bool = True
    not_financial_advice: bool = True
    generated_by: str = "llm"
    fallback_reason: str | None = None
    created_at: str | None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/runs", response_model=StartRunResponse, status_code=status.HTTP_201_CREATED)
@safe_endpoint
def start_run(
    payload: StartRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StartRunResponse:
    """Start a new Discover pipeline run."""
    focus = payload.focus or payload.description
    run_id = submit_discover_job(db, user.id, focus=focus)
    return StartRunResponse(run_id=run_id)


@router.get("/runs", response_model=list[RunSummary])
@safe_endpoint
def list_runs(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[RunSummary]:
    """List Discover run summaries for the current user."""
    runs = (
        db.query(DiscoverRun)
        .filter(DiscoverRun.user_id == user.id)
        .order_by(DiscoverRun.created_at.desc())
        .all()
    )
    for r in runs:
        reap_run_if_stale(db, r)

    def _stage_summary(run: DiscoverRun) -> str:
        try:
            stages = _safe_json_loads(run.stage_json)
            return str(stages.get("discover", {}).get("message", "") or "")
        except (ValueError, AttributeError):
            return ""

    return [
        RunSummary(
            id=r.id,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            stage_summary=_stage_summary(r),
        )
        for r in runs
    ]


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
@safe_endpoint
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RunDetailResponse:
    """Get full Discover run with candidates."""
    run = db.get(DiscoverRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    reap_run_if_stale(db, run)

    candidates = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.run_id == run_id)
        .order_by(DiscoverCandidate.symbol.asc())
        .limit(1000)
        .all()
    )

    dossier_ids = [c.dossier_id for c in candidates if c.dossier_id]
    if dossier_ids:
        dossiers = (
            db.query(RecommendationDossier)
            .filter(RecommendationDossier.id.in_(dossier_ids))
            .all()
        )
        dossiers_by_id = {d.id: d for d in dossiers}
    else:
        dossiers_by_id = {}

    recommendation_ids = [c.recommendation_id for c in candidates if c.recommendation_id]
    if recommendation_ids:
        recommendations = (
            db.query(Recommendation)
            .filter(Recommendation.id.in_(recommendation_ids))
            .all()
        )
        recommendations_by_id = {r.id: r for r in recommendations}
    else:
        recommendations_by_id = {}

    pool = {
        symbol: (rank, size)
        for symbol, rank, size in db.query(
            DiscoverCandidateSnapshot.symbol, DiscoverCandidateSnapshot.stock_rank,
            DiscoverCandidateSnapshot.n_evaluable_stocks,
        ).filter(DiscoverCandidateSnapshot.run_id == run_id, DiscoverCandidateSnapshot.user_id == user.id)
    }

    def _to_candidate(
        c: DiscoverCandidate,
        dossiers_by_id: dict[str, RecommendationDossier],
        recommendations_by_id: dict[str, Recommendation],
    ) -> CandidateResponse:
        base = CandidateResponse(
            id=c.id,
            symbol=c.symbol,
            isin=c.isin,
            name=c.name,
            source=c.source,
            status=c.status,
            reject_stage=c.reject_stage,
            reject_reason=c.reject_reason,
            scores=_safe_json_loads(c.scores_json),
            tradeable=_safe_json_loads(c.tradeable_json),
            dossier_id=c.dossier_id,
            recommendation_id=c.recommendation_id,
        )
        rank, size = pool.get(c.symbol, (None, None))
        if rank is not None:
            base.pool_rank, base.pool_size = rank, size
        # Populate dossier-derived fields if dossier exists
        if c.dossier_id and c.dossier_id in dossiers_by_id:
            dossier = dossiers_by_id[c.dossier_id]
            d_json = _safe_json_loads(dossier.dossier_json)
            base.conviction = dossier.conviction
            base.horizon_months = d_json.get("horizon_months")
            base.expected_return = d_json.get("expected_return")
            base.expected_return_anchor_kind = d_json.get("expected_return_anchor_kind")
        # Track B2: surface the cohort track-record gate result, if any.
        if c.recommendation_id and c.recommendation_id in recommendations_by_id:
            rec = recommendations_by_id[c.recommendation_id]
            payload = _safe_json_loads(rec.payload_json)
            gate = payload.get("track_record_gate") or {}
            gate_status = gate.get("status")
            base.track_record_status = gate_status
            base.withheld = gate_status == "unproven"
        return base

    all_cands = [_to_candidate(c, dossiers_by_id, recommendations_by_id) for c in candidates]
    shortlisted = [c for c in all_cands if c.status == "shortlisted"]
    rejected = [c for c in all_cands if c.status == "rejected"]

    return RunDetailResponse(
        id=run.id,
        status=run.status,
        stage_json=_safe_json_loads(run.stage_json),
        params_json=_safe_json_loads(run.params_json),
        error_message=run.error_message,
        created_at=run.created_at.isoformat() if run.created_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
        candidates=all_cands,
        shortlisted=shortlisted,
        rejected=rejected,
    )


# Below this many seconds without a progress write we consider a running pipeline
# to be stalled (the warm-up + per-candidate loop write progress every symbol).
_STALL_THRESHOLD_S = 90.0


def _debug_hint(
    status_val: str,
    stage: dict[str, Any],
    since_progress: float | None,
    error_message: str | None,
) -> str:
    """Human-readable read of the run's health for the debug endpoint."""
    if status_val in ("completed", "failed", "cancelled"):
        base = f"Run finished with status '{status_val}'."
        return f"{base} Error: {error_message}" if error_message else base
    current_stage = stage.get("current_stage") or stage.get("state") or "unknown"
    candidate = stage.get("current_candidate")
    if since_progress is not None and since_progress > _STALL_THRESHOLD_S:
        who = f" on '{candidate}'" if candidate else ""
        if current_stage == "warming_price_cache":
            where = "price-cache warm-up"
        elif current_stage == DiscoveryState.WRITING_DOSSIER.value:
            where = "dossier generation"
        elif current_stage == "calibration":
            where = "prediction calibration"
        else:
            where = f"stage '{current_stage}'"
        return (
            f"No progress for {since_progress:.0f}s during {where}{who}. "
            "Cause not yet diagnosed from this signal alone — check server logs for the run_id."
        )
    return "Run appears active (recent progress)."


@router.get("/runs/{run_id}/debug")
@safe_endpoint
def debug_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Read-only diagnostic snapshot for a Discover run.

    Surfaces status, parsed stage, candidate breakdown, a staleness signal
    (seconds since the last progress write), the per-process scheduler view, and
    a human hint — so a stuck run can be diagnosed from the browser without
    shelling into the host. Owner-only.
    """
    run = db.get(DiscoverRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    reap_run_if_stale(db, run)

    now = datetime.now(UTC)
    created = run.created_at
    # SQLite (and some drivers) return naive datetimes; treat them as UTC so the
    # subtraction below never mixes naive/aware values.
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_seconds = (now - created).total_seconds() if created else None

    stages = _safe_json_loads(run.stage_json)
    raw_stage = stages.get("discover", {})
    stage = raw_stage if isinstance(raw_stage, dict) else {}
    elapsed = stage.get("elapsed_seconds")

    # Staleness is derived from `updated_at` (bumped by the ORM on every
    # stage_json write) rather than stage_json's own `elapsed_seconds` field.
    # Several stage-summary writes along the pipeline omit elapsed_seconds
    # (it isn't meaningful outside the per-candidate loops), which would
    # otherwise reset this to null and make an actually-stuck run misreport
    # as "active". updated_at is set on literally every write, and — unlike
    # elapsed_seconds, computed from the writing process's private
    # ``started_at`` — it's a DB timestamp, so it stays meaningful even if
    # the process that was running the pipeline died and got restarted.
    updated = run.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    since_progress = (now - updated).total_seconds() if updated is not None else None

    # Candidate breakdown (statuses are persisted after the pipeline finishes, so
    # mid-run these are mostly "pending" — still useful post-hoc).
    by_status: dict[str, int] = {}
    by_reject: dict[str, int] = {}
    rows = (
        db.query(DiscoverCandidate.status, DiscoverCandidate.reject_stage)
        .filter(DiscoverCandidate.run_id == run_id)
        .all()
    )
    for status_val, reject in rows:
        by_status[status_val] = by_status.get(status_val, 0) + 1
        if reject:
            by_reject[reject] = by_reject.get(reject, 0) + 1

    # Scheduler view is per-process and best-effort: never start a scheduler just
    # to peek, and note that a multi-worker server may run the job elsewhere.
    from app.foundation import jobs as jobs_mod

    if jobs_mod.scheduler_started():
        scheduler_job = jobs_mod.get_job_status(f"discover_{run_id}")
    else:
        scheduler_job = {"status": "no_scheduler_in_this_process"}

    return {
        "run_id": run.id,
        "status": run.status,
        "error_message": run.error_message,
        "created_at": created.isoformat() if created else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "updated_at": updated.isoformat() if updated is not None else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "last_progress_elapsed_seconds": elapsed,
        "seconds_since_last_progress": round(since_progress, 1) if since_progress is not None else None,
        "stage": stage,
        "candidates": {"total": len(rows), "by_status": by_status, "by_reject_stage": by_reject},
        "scheduler_job": scheduler_job,
        "hint": _debug_hint(run.status, stage, since_progress, run.error_message),
    }


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_200_OK)
@safe_endpoint
def cancel_run(
    run_id: str,
    mode: str = Query("graceful"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Cancel a Discover run.

    Modes:
    - ``graceful`` (default): sets ``status = "cancellation_requested"``.
      The running job picks this up on its next CancelToken check.
    - ``immediate``: removes the APScheduler job AND sets
      ``status = "cancelled"`` immediately.

    Auth: only the run owner may cancel. Idempotent: returns 409 if run
    is not in a cancellable state (``queued`` / ``running``).
    """
    if mode not in ("graceful", "immediate"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode must be 'graceful' or 'immediate'",
        )

    run = db.get(DiscoverRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )

    if run.status in ("completed", "cancelled", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run already {run.status}",
        )

    if mode == "immediate":
        # Kill the APScheduler job
        from app.foundation.jobs import cancel_job

        found = cancel_job(f"discover_{run_id}")
        if not found:
            logger.warning("cancel_job: job discover_%s not found (already completed or not running)", run_id)
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
    else:
        run.status = "cancellation_requested"

    db.commit()
    logger.info("Cancel request for run %s mode=%s (status=%s)", run_id, mode, run.status)
    return {"status": run.status, "mode": mode}


class SkillSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    snapshot_at: str | None
    metric_type: str
    total_predictions: int
    total_resolved: int
    hit_rate: float | None
    brier_score_avg: float | None
    rank_ic: float | None
    icir: float | None
    ece: float | None
    # Named "mincer_*" historically but actually the intercept/coefficient
    # of a logistic (Platt-scaling) fit of hit-probability on conviction,
    # not a Mincer-Zarnowitz regression (F14) — kept as a documented naming
    # quirk rather than a live-schema column rename.
    mincer_a0: float | None
    mincer_a1: float | None


@router.get("/skill-trend", response_model=list[SkillSnapshotResponse])
@safe_endpoint
def get_skill_trend(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[SkillSnapshotResponse]:
    """Return periodic prediction-skill metrics for the Discover Track Record tab."""
    rows = (
        db.query(DiscoverySkillSnapshot)
        .filter(DiscoverySkillSnapshot.user_id == user.id)
        .order_by(DiscoverySkillSnapshot.snapshot_at.asc())
        .all()
    )
    return [
        SkillSnapshotResponse(
            id=r.id,
            snapshot_at=r.snapshot_at.isoformat() if r.snapshot_at else None,
            metric_type=r.metric_type,
            total_predictions=r.total_predictions,
            total_resolved=r.total_resolved,
            hit_rate=r.hit_rate,
            brier_score_avg=r.brier_score_avg,
            rank_ic=r.rank_ic,
            icir=r.icir,
            ece=r.ece,
            mincer_a0=r.mincer_a0,
            mincer_a1=r.mincer_a1,
        )
        for r in rows
    ]


class SkillSummaryResponse(BaseModel):
    """Skill of Discover's resolved predictions, judged against the passive core.

    ``ic_t_stat`` is Newey-West over overlapping horizons; the hit-rate
    interval is clustered by date and None until the record spans
    ``min_independent_windows`` non-overlapping horizons.
    """

    benchmark: str
    resolved: int
    resolved_vs_benchmark: int
    pending: int
    next_resolve_at: str | None
    hit_rate: float | None
    mean_excess_return: float | None
    mean_rank_ic: float | None
    icir: float | None
    ic_t_stat: float | None
    ic_dates: int
    min_ic_names: int
    ic_nw_lags: int = 0
    hit_rate_ci_half_width: float | None = None
    independent_windows: int = 0
    min_independent_windows: int = 12
    horizon_calendar_days: int = 29


@router.get("/skill-summary", response_model=SkillSummaryResponse)
@safe_endpoint
def get_skill_summary(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SkillSummaryResponse:
    """Per-date rank IC and hit rate vs the benchmark over Discover's resolved predictions."""
    return SkillSummaryResponse(**skill_summary(db, user.id))


@router.get("/dossiers/{dossier_id}", response_model=DossierResponse)
@safe_endpoint
def get_dossier(
    dossier_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DossierResponse:
    """Get a dossier by id."""
    dossier = db.get(RecommendationDossier, dossier_id)
    if dossier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dossier not found")

    # Security: ensure the dossier belongs to a recommendation owned by this user
    # (dossiers themselves have no user_id, so we check via candidate linkage)
    candidate = (
        db.query(DiscoverCandidate)
        .join(DiscoverRun, DiscoverCandidate.run_id == DiscoverRun.id)
        .filter(
            DiscoverCandidate.dossier_id == dossier_id,
            DiscoverRun.user_id == user.id,
        )
        .first()
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dossier not found")

    data = _safe_json_loads(dossier.dossier_json)
    return DossierResponse(
        id=dossier.id,
        conviction=dossier.conviction,
        direction=data.get("direction", "neutral"),
        horizon_months=data.get("horizon_months", 6),
        expected_return=data.get("expected_return"),
        expected_return_anchor_kind=data.get("expected_return_anchor_kind"),
        expected_return_band=data.get("expected_return_band"),
        expected_return_components=data.get("expected_return_components"),
        composite_breakdown=data.get("composite_breakdown"),
        thesis=data.get("thesis", ""),
        key_risks=data.get("key_risks", []),
        signal_breakdown=data.get("signal_breakdown", {}),
        regime_context=data.get("regime_context"),
        estimate=data.get("estimate", True),
        not_financial_advice=data.get("not_financial_advice", True),
        generated_by=data.get("generated_by", "llm"),
        fallback_reason=data.get("fallback_reason"),
        created_at=str(dossier.ts) if dossier.ts else None,
    )


# ---------------------------------------------------------------------------
# Config registry CRUD (P3b)
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    config_type: str
    version_label: str
    description: str | None = None
    config_json: dict
    status: str
    source: str
    parent_config_id: str | None = None
    created_at: str | None = None
    champion_at: str | None = None
    metrics_json: dict | None = None
    # True when app.decision.discover.config's code-level default for this
    # config_type has changed since this row was seeded/last approved
    # against it — surfaced as a "stale, review needed" badge, never
    # auto-overwritten (Phase 5).
    is_stale: bool = False


def _config_to_response(cfg: DiscoveryConfig) -> ConfigResponse:
    return ConfigResponse(
        id=cfg.id,
        config_type=cfg.config_type,
        version_label=cfg.version_label,
        description=cfg.description,
        config_json=cfg.config_json,
        status=cfg.status,
        source=cfg.source,
        parent_config_id=cfg.parent_config_id,
        created_at=cfg.created_at.isoformat() if cfg.created_at else None,
        champion_at=cfg.champion_at.isoformat() if cfg.champion_at else None,
        metrics_json=cfg.metrics_json,
        is_stale=is_config_stale(cfg),
    )


class CreateConfigRequest(BaseModel):
    config_type: str
    config_json: dict
    version_label: str | None = None
    description: str | None = None


class ActivateConfigRequest(BaseModel):
    pass


@router.get("/configs", response_model=list[ConfigResponse])
@safe_endpoint
def list_config_endpoint(
    config_type: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ConfigResponse]:
    """List discovery configs, optionally filtered by type and/or status."""
    cfgs = list_configs(db, config_type=config_type, status=status)
    return [_config_to_response(c) for c in cfgs]


# ---------------------------------------------------------------------------
# Config review endpoints (P3d) — registered BEFORE /configs/{config_id} so
# literal path segments /configs/reviews… take priority over the parametric
# route in Starlette's first-match registration order.
# ---------------------------------------------------------------------------


class ConfigReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reviewed_at: str | None = None
    config_type: str
    champion_id: str
    promoted_id: str | None = None
    challenger_results: list[dict]
    rejected_ids: list[str]


def _review_to_response(review: DiscoveryConfigReview) -> ConfigReviewResponse:
    return ConfigReviewResponse(
        id=review.id,
        reviewed_at=review.reviewed_at.isoformat() if review.reviewed_at else None,
        config_type=review.config_type,
        champion_id=review.champion_id,
        promoted_id=review.promoted_id,
        challenger_results=review.challenger_results or [],
        rejected_ids=review.rejected_ids or [],
    )


@router.get("/configs/reviews", response_model=list[ConfigReviewResponse])
@safe_endpoint
def list_config_reviews(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ConfigReviewResponse]:
    """List DiscoveryConfigReview records, newest first."""
    rows = (
        db.query(DiscoveryConfigReview)
        .order_by(DiscoveryConfigReview.reviewed_at.desc())
        .all()
    )
    return [_review_to_response(r) for r in rows]


@router.get("/configs/reviews/{review_id}", response_model=ConfigReviewResponse)
@safe_endpoint
def get_config_review(
    review_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConfigReviewResponse:
    """Return a single review record by id."""
    review = db.get(DiscoveryConfigReview, review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    return _review_to_response(review)


@router.get("/configs/{config_id}", response_model=ConfigResponse)
@safe_endpoint
def get_config_endpoint(
    config_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConfigResponse:
    """Return a single config by id."""
    cfg = get_config(db, config_id)
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config not found")
    return _config_to_response(cfg)


@router.post("/configs", response_model=ConfigResponse, status_code=status.HTTP_201_CREATED)
@safe_endpoint
def create_config_endpoint(
    payload: CreateConfigRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConfigResponse:
    """Create a new manual challenger config."""
    cfg = create_config(
        db,
        config_type=payload.config_type,
        config_json=payload.config_json,
        version_label=payload.version_label,
        description=payload.description,
        source="manual",
    )
    return _config_to_response(cfg)


@router.post("/configs/{config_id}/activate", response_model=ConfigResponse)
@safe_endpoint
def activate_config_endpoint(
    config_id: str,
    payload: ActivateConfigRequest | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ConfigResponse:
    """Promote a challenger config to active.

    ``signal_weights`` configs are excluded: they're under automated weekly
    statistical review (Bonferroni + DSR, Sundays 04:00 UTC — see
    ``services/discover/config_review.py``), which promotes challengers via
    ``activate_config`` directly rather than through this endpoint. Manual
    promotion here would fight that process, so it's rejected outright. This
    mirrors the frontend gating in ConfigsTab.tsx.
    """
    existing = get_config(db, config_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config not found")
    if existing.config_type == "signal_weights":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "signal_weights configs are promoted automatically by the weekly "
                "statistical review (Sundays 04:00 UTC). Manual promotion is disabled "
                "to avoid conflicting with that process."
            ),
        )
    try:
        cfg = activate_config(db, config_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _config_to_response(cfg)


@router.post("/configs/{config_id}/sync-to-default", response_model=ConfigResponse)
@safe_endpoint
def sync_config_to_default_endpoint(
    config_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ConfigResponse:
    """Overwrite a stale config's payload with the current code-level default.

    Fixes a config row whose ``code_default_hash`` no longer matches the
    live ``CODE_DEFAULTS`` entry (the "Stale" badge in ConfigsTab) — the
    durable alternative to a one-shot manual DB edit.
    """
    existing = get_config(db, config_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config not found")
    try:
        cfg = sync_config_to_default(db, config_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _config_to_response(cfg)


@router.post("/configs/perturb")
@safe_endpoint
def perturb_configs_endpoint(
    config_type: str = "signal_weights",
    n_variants: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict:
    """Auto-perturb a config to generate challenger variants.

    For ``signal_weights``: adds Gaussian noise to weights (renormalised).
    For ``prompt_template``: generates 4 style/focus prompt variations.
    """
    if config_type == "signal_weights":
        created = generate_weight_perturbations(db, n_variants=n_variants)
    elif config_type == "prompt_template":
        created = generate_prompt_perturbations(db)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown config_type: {config_type}")
    return {"created": [_config_to_response(c) for c in created]}


@router.post("/configs/review")
@safe_endpoint
def trigger_config_review(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict:
    """Manually trigger the config review job in a background thread."""
    from app.decision.discover.config_review import discovery_review_inner
    from app.foundation.jobs import submit_job

    def _run_review() -> None:
        from app.foundation.core.db import SessionLocal

        inner_db = SessionLocal()
        try:
            discovery_review_inner(inner_db)
        except Exception:
            logger.exception("Manual discovery config review failed")
            inner_db.rollback()
        finally:
            inner_db.close()

    job_id = submit_job(_run_review)
    return {"status": "queued", "job_id": job_id}
