"""AlphaCrafter API endpoints (Phase 4).

Routes:
- POST /api/alphacrafter/jobs          → start async pipeline, returns job_id
- GET  /api/alphacrafter/jobs/{id}/status → poll job status / progress
- GET  /api/alphacrafter/jobs/{id}/memory → SharedMemoryH snapshot
- GET  /api/alphacrafter/jobs/{id}/timeline → agent_history from SharedMemoryH
- POST /api/alphacrafter/recommendations/generate  → synchronous fallback
- GET  /api/alphacrafter/dossiers
- GET  /api/alphacrafter/dossiers/{id}
- GET  /api/alphacrafter/factors/library
- POST /api/alphacrafter/factors/{id}/retire
- POST /api/alphacrafter/diagnostics   → run pipeline health diagnostics
- POST /api/alphacrafter/miner/run      → legacy (synchronous)
- POST /api/alphacrafter/screener/run   → legacy
- POST /api/alphacrafter/trader/backtest → legacy
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import (
    AlphacrafterJobRun,
    FactorsLibrary,
    RecommendationDossier,
    User,
)
from app.lab import alphacrafter
from app.foundation.auth import current_user
from app.foundation.data_backbone.bars import BarStore
from app.foundation.data_backbone.regime_store import RegimeStore
from app.foundation.jobs import submit_job
from app.foundation.providers.registry import build_provider_registry
from app.lab.alphacrafter.universe import MIN_MINER_UNIVERSE, MINER_UNIVERSE
from app.foundation.settings import get_public_settings
from app.interface.api.safe_endpoint import safe_endpoint

router = APIRouter(
    prefix="/api/alphacrafter",
    tags=["alphacrafter"],
)


def _user_scope(user_id_column, user: User):
    """Ownership filter for user-scoped rows.

    Legacy rows created before user scoping have NULL user_id; those stay
    visible to admins only.
    """
    if user.role == "admin":
        return or_(user_id_column == user.id, user_id_column.is_(None))
    return user_id_column == user.id


def _get_scoped_job(db: Session, job_id: str, user: User) -> AlphacrafterJobRun | None:
    return (
        db.query(AlphacrafterJobRun)
        .filter(AlphacrafterJobRun.id == job_id, _user_scope(AlphacrafterJobRun.user_id, user))
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class MinerRunRequest(BaseModel):
    """Request to run the Miner agent."""
    universe: list[str]
    lookback_years: float = 5.0


class MinerRunResponse(BaseModel):
    """Response from Miner run."""
    factors_count: int
    valid_factors: int
    timestamp: datetime


class ScreenerRunRequest(BaseModel):
    """Request to run the Screener agent."""
    candidate_factor_ids: list[UUID]
    regime_label: str | None = None


class ScreenerRunResponse(BaseModel):
    """Response from Screener run."""
    screener_run_id: UUID
    selected_factor_ids: list[UUID]
    regime_label: str | None
    timestamp: datetime


class TraderBacktestRequest(BaseModel):
    """Request to run Trader backtest."""
    screener_run_id: UUID
    universe: list[str]
    start_date: datetime
    end_date: datetime
    num_configs: int = 10


class TraderBacktestResponse(BaseModel):
    """Response from Trader backtest."""
    results_count: int
    top_sharpe: float
    top_max_dd: float
    timestamp: datetime


class RecommendationsGenerateRequest(BaseModel):
    """Request to generate full recommendations."""
    universe: list[str] | None = None


class DossierResponse(BaseModel):
    """Dossier response."""
    id: UUID
    ts: datetime
    conviction: float
    dossier_json: str
    model_config = ConfigDict(from_attributes=True)


class DiagnosticStep(BaseModel):
    """A single diagnostic check result."""
    key: str
    label: str
    status: str  # "passed" | "warning" | "failed"
    message: str


class DiagnosticsResponse(BaseModel):
    """AlphaCrafter pipeline diagnostics response."""
    status: str  # "passed" | "warning" | "failed"
    summary: str
    steps: list[DiagnosticStep]
    created_at: datetime


# ---------------------------------------------------------------------------
# Async job endpoints (NEW — Phase 4 v2)
# ---------------------------------------------------------------------------

class JobStartResponse(BaseModel):
    """Response from starting an async pipeline job."""
    job_id: str
    status: str
    message: str


class StageProgress(BaseModel):
    """Progress for one pipeline stage."""
    stage: str
    status: str  # ok | error | skipped
    duration_s: float | None = None
    error: str | None = None
    symbols: list[str] | None = None
    count: int | None = None
    reason: str | None = None


class JobStatusResponse(BaseModel):
    """Job status response for frontend polling."""
    job_id: str
    status: str  # running | completed | failed
    progress: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    stages: list[StageProgress] = []


@router.post("/jobs", status_code=201)
def start_pipeline_job(
    request: RecommendationsGenerateRequest = RecommendationsGenerateRequest(),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> JobStartResponse:
    """Start an *async* AlphaCrafter pipeline.

    Returns immediately with a ``job_id``.  The frontend polls
    ``GET /api/alphacrafter/jobs/{job_id}`` for progress.
    The pipeline runs in an APScheduler background thread.
    """
    job_run_id = str(uuid4())

    # Create the job run row (not yet committed — see below).
    job_run = AlphacrafterJobRun(
        id=job_run_id,
        user_id=_user.id,
        status="running",
        progress_json=json.dumps({"stages": {}}),
    )
    db.add(job_run)

    # Submit to APScheduler *before* committing so a submit failure does not
    # leave a dangling "running" row.  The background job runner gets its own
    # DB session and will find the row once committed.
    try:
        submit_job(
            alphacrafter.orchestrator.run_async_pipeline_job,
            args=(job_run_id, request.universe, _user.id),
            job_id=f"alphacrafter_pipeline_{job_run_id}",
        )
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Failed to schedule pipeline job")

    db.commit()

    return JobStartResponse(
        job_id=job_run_id,
        status="running",
        message="AlphaCrafter pipeline started. Poll GET /api/alphacrafter/jobs/{job_id} for progress.",
    )


@router.post("/tuning/run", status_code=201)
def start_tuning_job(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> JobStartResponse:
    """Start the quarterly walk-forward tuning job manually.

    Same DSR-guarded grid search as the quarterly cron; returns a job_id to
    poll via ``GET /api/alphacrafter/jobs/{job_id}/status``.
    """
    from app.lab.alphacrafter import tuning

    job_run_id = str(uuid4())

    job_run = AlphacrafterJobRun(
        id=job_run_id,
        user_id=_user.id,
        status="running",
        progress_json=json.dumps({"stages": {"tuning": {"stage": "tuning", "status": "running"}}}),
    )
    db.add(job_run)

    try:
        submit_job(
            tuning.run_tuning_job_sync,
            args=(job_run_id,),
            job_id=f"alphacrafter_tuning_{job_run_id}",
        )
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Failed to schedule tuning job")

    db.commit()

    return JobStartResponse(
        job_id=job_run_id,
        status="running",
        message="AlphaCrafter tuning started. Poll GET /api/alphacrafter/jobs/{job_id}/status for progress.",
    )


@router.get("/jobs/{job_id}/status")
def get_job_status(
    job_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> JobStatusResponse:
    """Poll job status and progress."""
    job_run = _get_scoped_job(db, job_id, _user)
    if not job_run:
        raise HTTPException(status_code=404, detail="Job not found")

    progress: dict[str, Any] = {}
    try:
        progress = json.loads(job_run.progress_json) if job_run.progress_json else {}
    except (json.JSONDecodeError, TypeError):
        progress = {}

    result: dict[str, Any] | None = None
    try:
        result = json.loads(job_run.result_json) if job_run.result_json else None
    except (json.JSONDecodeError, TypeError):
        result = None

    # Flatten stages from progress dict.
    raw_stages = progress.get("stages", {})
    stages: list[StageProgress] = []
    for stage_name, data in raw_stages.items():
        if isinstance(data, dict):
            stages.append(StageProgress(
                stage=data.get("stage", stage_name),
                status=data.get("status", "unknown"),
                duration_s=data.get("duration_s"),
                error=data.get("error"),
                symbols=data.get("symbols"),
                count=data.get("count"),
                reason=data.get("reason"),
            ))

    return JobStatusResponse(
        job_id=job_run.id,
        status=job_run.status,
        progress=progress,
        result=result,
        error_message=job_run.error_message,
        created_at=job_run.created_at,
        completed_at=job_run.completed_at,
        stages=stages,
    )


@router.get("/jobs/{job_id}/memory")
def get_job_memory(
    job_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> dict[str, Any]:
    """Return the full SharedMemoryH snapshot for a completed job.

    Returns 404 if the job is not found or has no shared_memory.
    """
    job_run = _get_scoped_job(db, job_id, _user)
    if not job_run:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job_run.shared_memory:
        raise HTTPException(status_code=404, detail="Shared memory not available for this job")
    return job_run.shared_memory


@router.get("/jobs/{job_id}/timeline")
def get_job_timeline(
    job_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> list[dict[str, Any]]:
    """Return the agent_history list from SharedMemoryH for a completed job.

    Returns an empty list if the job is not found or has no history.
    """
    job_run = _get_scoped_job(db, job_id, _user)
    if not job_run or not job_run.shared_memory:
        return []
    return job_run.shared_memory.get("agent_history", [])


# ---------------------------------------------------------------------------
# Synchronous pipeline (legacy — kept for backward compat / cron usage)
# ---------------------------------------------------------------------------

@router.post("/recommendations/generate")
@safe_endpoint
async def generate_recommendations_endpoint(
    request: RecommendationsGenerateRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> dict[str, Any]:
    """Run the complete AlphaCrafter pipeline *synchronously*.

    Prefer ``POST /api/alphacrafter/jobs`` for the async version.
    """
    result = await alphacrafter.run_daily_alphacrafter(db, request.universe, user_id=_user.id)
    return {
        "factors_generated": result.get("factors_generated", 0),
        "factors_selected": result.get("factors_selected", 0),
        "dossiers_created": result.get("dossiers_created", 0),
        "factors_retired": result.get("factors_retired", 0),
        "dossier_symbols": result.get("dossier_symbols", []),
        "stages": result.get("stages", {}),
        "timestamp": datetime.now(UTC),
    }


# ---------------------------------------------------------------------------
# Dossier endpoints
# ---------------------------------------------------------------------------

@router.get("/dossiers")
def list_dossiers(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    symbol: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> list[DossierResponse]:
    """List recommendation dossiers, optionally filtered by symbol."""
    stmt = select(RecommendationDossier).where(
        _user_scope(RecommendationDossier.user_id, _user)
    )
    if symbol:
        stmt = stmt.where(
            func.json_extract(RecommendationDossier.dossier_json, "$.symbol") == symbol
        )
    stmt = stmt.order_by(desc(RecommendationDossier.ts)).limit(limit).offset(offset)
    dossiers = db.execute(stmt).scalars().all()
    return [DossierResponse.model_validate(d) for d in dossiers]


@router.get("/dossiers/{dossier_id}")
def get_dossier(
    dossier_id: UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> DossierResponse:
    """Get a specific dossier by ID."""
    dossier = db.execute(
        select(RecommendationDossier).where(
            RecommendationDossier.id == str(dossier_id),
            _user_scope(RecommendationDossier.user_id, _user),
        )
    ).scalar()

    if not dossier:
        raise HTTPException(status_code=404, detail="Dossier not found")

    return DossierResponse.model_validate(dossier)


# ---------------------------------------------------------------------------
# Factor library
# ---------------------------------------------------------------------------

@router.get("/factors/library")
def list_factors(
    retired: bool = Query(False),
    sort_by: str = Query("ic", pattern="^(ic|icir|name|created_at|abs_ic)$"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> list[dict[str, Any]]:
    """List factors in the library, sorted by |ic| descending by default."""
    if retired:
        stmt = select(FactorsLibrary).where(FactorsLibrary.retired_at.isnot(None))
    else:
        stmt = select(FactorsLibrary).where(FactorsLibrary.retired_at.is_(None))

    factors = db.execute(stmt.order_by(desc(FactorsLibrary.created_at))).scalars().all()

    result = [_factor_to_dict(f) for f in factors]

    # Sort by the requested field (descending for numeric, ascending for name).
    if sort_by in ("ic", "icir", "abs_ic"):
        field = "ic" if sort_by == "abs_ic" else sort_by
        result.sort(key=lambda x: abs(x.get(field) or 0.0), reverse=True)
    elif sort_by == "name":
        result.sort(key=lambda x: (x.get("name") or "").lower())

    return result


def _factor_to_dict(f: FactorsLibrary) -> dict[str, Any]:
    """Serialise a factor row with its validation metrics for the frontend."""
    try:
        metrics = json.loads(f.ic_summary_json) if f.ic_summary_json else {}
    except (json.JSONDecodeError, TypeError):
        metrics = {}
    try:
        formula_meta = json.loads(f.formula_json) if f.formula_json else {}
    except (json.JSONDecodeError, TypeError):
        formula_meta = {}
    return {
        "id": f.id,
        "name": f.name,
        "source": f.source,
        "created_at": f.created_at,
        "retired_at": f.retired_at,
        "formula": formula_meta.get("formula"),
        "dsl": formula_meta.get("dsl"),
        "ic": metrics.get("ic"),
        "icir": metrics.get("icir"),
        "turnover": metrics.get("turnover"),
        "decay_halflife_days": metrics.get("decay_halflife_days"),
        "n_obs": metrics.get("n_obs"),
    }


@router.post("/factors/{factor_id}/retire")
def retire_factor(
    factor_id: UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> dict[str, Any]:
    """Manually retire a factor."""
    factor = db.execute(
        select(FactorsLibrary).where(FactorsLibrary.id == str(factor_id))
    ).scalar()

    if not factor:
        raise HTTPException(status_code=404, detail="Factor not found")

    factor.retired_at = datetime.now(UTC)
    db.commit()

    return {"id": str(factor.id), "retired_at": factor.retired_at}


# ---------------------------------------------------------------------------
# Diagnostics endpoint
# ---------------------------------------------------------------------------

@router.post("/diagnostics")
def run_diagnostics(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> DiagnosticsResponse:
    """Run AlphaCrafter pipeline health diagnostics.

    Checks settings, price data availability, provider configuration,
    factor library state, regime data, recent pipeline runs, and dossiers.
    Returns step-by-step results following the DKB diagnostics pattern.
    """
    steps: list[DiagnosticStep] = []

    def step(key: str, label: str, status: str, message: str) -> None:
        steps.append(DiagnosticStep(key=key, label=label, status=status, message=message))

    # 1. Check index basket configuration
    settings = get_public_settings(db)
    basket_str = settings.get("alphacrafter_index_basket", "")
    symbols = [s.strip() for s in basket_str.split(",") if s.strip()] if isinstance(basket_str, str) else []
    if len(symbols) >= MIN_MINER_UNIVERSE:
        step("basket", "Index basket", "passed",
             f"{len(symbols)} symbol(s) configured: {', '.join(symbols[:8])}" +
             (f" (+{len(symbols)-8} more)" if len(symbols) > 8 else ""))
    elif symbols:
        step("basket", "Index basket", "warning",
             f"The configured basket has {len(symbols)} symbol(s), too few for a cross-sectional IC "
             f"(needs {MIN_MINER_UNIVERSE}); the miner uses the built-in {len(MINER_UNIVERSE)}-name "
             "Euro Stoxx 50 + S&P 100 universe instead. Clear the setting or list more names.")
    else:
        step("basket", "Index basket", "passed",
             f"Built-in universe: Euro Stoxx 50 + S&P 100 ({len(MINER_UNIVERSE)} names).")

    # 3. Check IC/ICIR thresholds
    min_ic = settings.get("alphacrafter_min_ic", 0.02)
    min_icir = settings.get("alphacrafter_min_icir", 0.3)
    ic_horizon = settings.get("alphacrafter_ic_horizon_days", 5)
    step("thresholds", "Factor thresholds", "passed",
         f"|IC| ≥ {min_ic}, |ICIR| ≥ {min_icir}, horizon = {ic_horizon}d")

    # 4. Check LLM proposer
    llm_propose = bool(settings.get("alphacrafter_propose_llm", False))
    if llm_propose:
        step("llm_proposer", "LLM factor proposer", "passed", "Enabled. Extra factors will be proposed by LLM.")
    else:
        step("llm_proposer", "LLM factor proposer", "warning",
             "Disabled. Only seed factors from quant_factors.py are used. "
             "Enable in Settings → Experimental → AlphaCrafter to expand the factor zoo.")

    # 5. Check price data availability
    bar_store = BarStore(db)
    try:
        symbols_with_data = bar_store.list_symbols_with_data()
    except Exception:
        symbols_with_data = []

    if symbols_with_data:
        basket_set = set(s.strip().upper() for s in (basket_str.split(",") if isinstance(basket_str, str) else []) if s.strip())
        default_set = {"EUNL.DE", "VWRL.L", "EIMI.L", "SXR8.DE"}
        target = basket_set or default_set
        covered = [s for s in symbols_with_data if s.upper() in target]
        missing = [s for s in target if s.upper() not in {d.upper() for d in symbols_with_data}]

        if not missing:
            step("price_data", "Price data", "passed",
                 f"{len(symbols_with_data)} symbol(s) with data in bar_prices. "
                 f"All {len(covered)} basket symbols covered.")
        else:
            step("price_data", "Price data", "warning",
                 f"{len(symbols_with_data)} symbol(s) with data, but missing from basket: "
                 f"{', '.join(missing)}. "
                 f"The Miner will attempt auto-download via DataIngester.")
    else:
        step("price_data", "Price data", "failed",
             "bar_prices table is empty. No price data available. "
             "The Miner will attempt auto-download on next run if a data provider is configured. "
             "Otherwise, manually load data via Market Data or DKB sync.")

    # 6. Check data providers
    try:
        registry = build_provider_registry(db)
        provider_statuses = registry.status()
        enabled = [p for p in provider_statuses if p.get("enabled")]
        available = [p for p in provider_statuses if p.get("available")]
        if available:
            names = [p.get("provider", "?") for p in available]
            step("providers", "Data providers", "passed",
                 f"{len(available)} available ({', '.join(names)}). "
                 f"{len(enabled)} enabled.")
        elif enabled:
            names = [p.get("provider", "?") for p in enabled]
            step("providers", "Data providers", "warning",
                 f"No API keys configured, but {len(enabled)} free provider(s) available ({', '.join(names)}). "
                 f"yfinance is always available as baseline.")
        else:
            step("providers", "Data providers", "warning",
                 "No providers appear to be configured. yfinance should still work as baseline.")
    except Exception as e:
        step("providers", "Data providers", "warning",
             f"Could not inspect provider registry: {e}")

    # 7. Check regime data
    regime_store = RegimeStore(db)
    try:
        snapshot = regime_store.get_latest_snapshot()
        if snapshot:
            label = snapshot.get("label", "unknown")
            ts = snapshot.get("ts")
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown"
            step("regime", "Regime data", "passed",
                 f"Latest snapshot: label='{label}' at {ts_str}. "
                 f"Regime-gating is active.")
        else:
            step("regime", "Regime data", "warning",
                 "No regime snapshots found. Screener will use neutral affinities "
                 "(all factors treated equally). Regime-gating will activate once "
                 "the HMM classifier runs (daily at 07:15 UTC).")
    except Exception as e:
        step("regime", "Regime data", "warning",
             f"Could not read regime snapshots: {e}")

    # 8. Check factor library
    try:
        active_count = db.execute(
            select(func.count()).select_from(FactorsLibrary).where(FactorsLibrary.retired_at.is_(None))
        ).scalar() or 0
        retired_count = db.execute(
            select(func.count()).select_from(FactorsLibrary).where(FactorsLibrary.retired_at.isnot(None))
        ).scalar() or 0
        if active_count > 0:
            # Get best IC
            best = db.execute(
                select(FactorsLibrary.ic_summary_json)
                .where(FactorsLibrary.retired_at.is_(None))
                .order_by(desc(FactorsLibrary.created_at))
                .limit(10)
            ).scalars().all()
            best_ic = 0.0
            for ic_json in best:
                try:
                    m = json.loads(ic_json) if ic_json else {}
                    ic_val = abs(m.get("ic") or 0)
                    if ic_val > best_ic:
                        best_ic = ic_val
                except (json.JSONDecodeError, TypeError):
                    pass
            step("factors", "Factor library", "passed",
                 f"{active_count} active factor(s), {retired_count} retired. "
                 f"Best |IC| in recent: {best_ic:.4f}")
        else:
            step("factors", "Factor library", "warning",
                 f"No active factors. {retired_count} retired. "
                 f"The Miner will populate this on the next pipeline run.")
    except Exception as e:
        step("factors", "Factor library", "warning",
             f"Could not inspect factor library: {e}")

    # 9. Check recent job runs
    try:
        last_job = db.execute(
            select(AlphacrafterJobRun)
            .where(_user_scope(AlphacrafterJobRun.user_id, _user))
            .order_by(desc(AlphacrafterJobRun.created_at))
            .limit(1)
        ).scalar()
        if last_job:
            status_str = last_job.status
            created = last_job.created_at.strftime("%Y-%m-%d %H:%M") if last_job.created_at else "unknown"
            completed = last_job.completed_at.strftime("%Y-%m-%d %H:%M") if last_job.completed_at else "still running"
            result_msg = ""
            if last_job.result_json:
                try:
                    r = json.loads(last_job.result_json)
                    result_msg = f" — {r.get('message', '')}"
                except (json.JSONDecodeError, TypeError):
                    pass
            error_msg = f" — Error: {last_job.error_message}" if last_job.error_message else ""
            step("last_job", "Last pipeline run", "passed" if status_str == "completed" else "warning",
                 f"Status: {status_str}. Started: {created}, finished: {completed}.{result_msg}{error_msg}")
        else:
            step("last_job", "Last pipeline run", "warning",
                 "No pipeline runs found. Click 'Generate Recommendations' to run the first pipeline.")
    except Exception as e:
        step("last_job", "Last pipeline run", "warning",
             f"Could not inspect job history: {e}")

    # 10. Check dossiers
    try:
        dossier_count = db.execute(
            select(func.count())
            .select_from(RecommendationDossier)
            .where(_user_scope(RecommendationDossier.user_id, _user))
        ).scalar() or 0
        if dossier_count > 0:
            latest = db.execute(
                select(RecommendationDossier.ts, RecommendationDossier.conviction)
                .where(_user_scope(RecommendationDossier.user_id, _user))
                .order_by(desc(RecommendationDossier.ts))
                .limit(1)
            ).first()
            ts_str = latest[0].strftime("%Y-%m-%d %H:%M") if latest and latest[0] else "unknown"
            conv_val = latest[1] if latest and latest[1] is not None else 0.0
            step("dossiers", "Recommendation dossiers", "passed",
                 f"{dossier_count} dossier(s) total. Latest: {ts_str} (conviction: {conv_val:.0%}).")
        else:
            step("dossiers", "Recommendation dossiers", "warning",
                 "No dossiers yet. Run the pipeline to generate recommendations.")
    except Exception as e:
        step("dossiers", "Recommendation dossiers", "warning",
             f"Could not inspect dossiers: {e}")

    # Compute overall status
    statuses = [s.status for s in steps]
    if "failed" in statuses:
        overall = "failed"
    elif "warning" in statuses:
        overall = "warning"
    else:
        overall = "passed"

    # Build summary
    failed = [s for s in steps if s.status == "failed"]
    warnings = [s for s in steps if s.status == "warning"]
    if failed:
        summary = f"{len(failed)} blocking issue(s), {len(warnings)} warning(s). Pipeline may not produce output."
    elif warnings:
        summary = f"{len(warnings)} warning(s). Pipeline should work but with reduced functionality."
    else:
        summary = "All checks passed. Pipeline should produce dossiers."

    return DiagnosticsResponse(
        status=overall,
        summary=summary,
        steps=steps,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Legacy standalone stage endpoints (kept for debugging)
# ---------------------------------------------------------------------------

@router.post("/miner/run")
@safe_endpoint
async def run_miner_endpoint(
    request: MinerRunRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> MinerRunResponse:
    """Run the Miner agent to generate factor candidates."""
    candidates = await alphacrafter.run_miner(
        db, request.universe, request.lookback_years
    )
    valid = sum(1 for c in candidates if c.valid)

    return MinerRunResponse(
        factors_count=len(candidates),
        valid_factors=valid,
        timestamp=datetime.now(UTC),
    )


@router.post("/screener/run")
@safe_endpoint
async def run_screener_endpoint(
    request: ScreenerRunRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> ScreenerRunResponse:
    """Run the Screener agent to select regime-appropriate factors."""
    result = await alphacrafter.run_screener(
        db, [str(x) for x in request.candidate_factor_ids], request.regime_label
    )

    return ScreenerRunResponse(
        screener_run_id=UUID(result.screener_run_id),
        selected_factor_ids=[UUID(x) for x in result.selected_factor_ids],
        regime_label=result.regime_label,
        timestamp=result.timestamp,
    )


@router.post("/trader/backtest")
@safe_endpoint
async def run_trader_endpoint(
    request: TraderBacktestRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),

) -> TraderBacktestResponse:
    """Run the Trader agent backtest sweep."""
    results = await alphacrafter.run_trader(
        db,
        request.screener_run_id,
        request.universe,
        request.start_date,
        request.end_date,
        request.num_configs,
    )

    top_sharpe = max([r.sharpe_ratio for r in results]) if results else 0.0
    top_max_dd = max([abs(r.max_drawdown) for r in results]) if results else 0.0

    return TraderBacktestResponse(
        results_count=len(results),
        top_sharpe=top_sharpe,
        top_max_dd=top_max_dd,
        timestamp=datetime.now(UTC),
    )
