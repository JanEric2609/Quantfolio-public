"""AlphaCrafter orchestrator: Daily pipeline execution (Phase 4).

Runs Miner → Screener → Trader → Dossier Builder in sequence, handing off by
**real `FactorsLibrary` ids** (the Miner persists survivors and stamps each
candidate's `id`). Registered as an APScheduler job in `services/jobs.py`.

Supports two modes:
  - **Sync** (via ``run_daily_alphacrafter``) — used by the daily cron job.
  - **Async job** (via ``run_async_pipeline_job``) — launched by ``submit_job``
    with progress written to an ``AlphacrafterJobRun`` row for frontend polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from app.lab.alphacrafter.universe import MINER_UNIVERSE, resolve_miner_universe
from app.foundation.models.entities import AlphacrafterJobRun, Holding, Portfolio
from app.lab.alphacrafter.dossier import DossierBuilder, build_dossier
from app.lab.alphacrafter.ic_decay import auto_retire_factors
from app.lab.alphacrafter.miner import MinerAgent, run_miner
from app.lab.alphacrafter.screener import ScreenerAgent, run_screener
from app.lab.alphacrafter.shared_memory import SharedMemoryH
from app.lab.alphacrafter.trader import TraderAgent, run_trader
from app.foundation.data_backbone.regime_store import RegimeStore
from app.foundation.settings import get_public_settings


def _run_in_thread_with_session(bind, agent_method, h, *args):
    """Create a per-thread SQLAlchemy session bound to *bind* and call agent_method(h, db).

    This is necessary because asyncio.to_thread runs in a separate thread,
    and SQLAlchemy Session objects are NOT thread-safe. Each thread must
    have its own session.

    The session is bound to the *same engine* as the caller's session (passed
    as ``bind``) rather than the global ``SessionLocal`` engine, so the pipeline
    operates on whatever database the caller is using — including the in-memory
    engine used by tests.

    Returns the agent's result after closing the session.
    """
    db = sessionmaker(bind=bind, autoflush=False, autocommit=False)()
    try:
        return agent_method(h, db, *args)
    finally:
        db.close()


def _run_async_in_thread_with_session(bind, agent_method, h, *args):
    """Like _run_in_thread_with_session, but for the Trader/Dossier agents whose
    methods are `async def` yet contain no real awaits (pure vectorbt/pandas
    compute). Running them inline on the event loop froze every other request for
    the length of a backtest sweep; offload to a worker thread and drive the
    coroutine to completion there with a fresh event loop.
    """
    import asyncio as _asyncio

    db = sessionmaker(bind=bind, autoflush=False, autocommit=False)()
    try:
        return _asyncio.run(agent_method(h, db, *args))
    finally:
        db.close()


def _deep_set(d: MutableMapping, key_path: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation key path.

    ``_deep_set({}, "stages.miner", {...})`` sets ``d["stages"]["miner"] = ...``.
    """
    parts = key_path.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value

logger = logging.getLogger(__name__)

# The miner's cross-section (Euro Stoxx 50 + S&P 100); a stored basket of at
# least MIN_MINER_UNIVERSE names wins. See alphacrafter/universe.py for why
# the old four-ETF default could never validate a factor.
DEFAULT_UNIVERSE = MINER_UNIVERSE

# Hard cap on Stage-3 trader sweep configurations (audit-fixes-2026-08 todo 19):
# the settings-driven grid can grow arbitrarily, so scheduled runs truncate to
# this bound with a warning instead of silently running an unbounded sweep.
MAX_SWEEP_CONFIGS = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _update_job_progress(
    db: Session,
    job_run_id: str | None,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Write progress / status to the ``AlphacrafterJobRun`` row (if set)."""
    if not job_run_id:
        return
    row = db.get(AlphacrafterJobRun, job_run_id)
    if not row:
        return
    if status is not None:
        row.status = status
    if progress is not None:
        existing: dict = json.loads(row.progress_json) if row.progress_json else {}
        for key, value in progress.items():
            _deep_set(existing, key, value)
        row.progress_json = json.dumps(existing)
    if result is not None:
        row.result_json = json.dumps(result)
    if error is not None:
        row.error_message = error
        row.status = "failed"
        row.completed_at = datetime.now(UTC)
    if status == "completed":
        row.completed_at = datetime.now(UTC)
    db.commit()


def _get_portfolio_holdings(db: Session, user_id: str | None = None) -> list[str]:
    """Collect distinct tickers from portfolio holdings.

    If *user_id* is provided, only return holdings belonging to that user
    (via the Portfolio → User FK chain).  When absent (e.g. cron context),
    returns holdings across all users.
    """
    stmt = select(Holding.ticker).where(Holding.ticker.isnot(None)).where(Holding.ticker != "")
    if user_id is not None:
        stmt = stmt.join(Portfolio).where(Portfolio.user_id == user_id)
    rows = db.execute(stmt.distinct()).scalars().all()
    return [ticker for ticker in rows if ticker is not None]


def reap_stale_job_runs(db: Session, max_age_hours: int = 2) -> int:
    """Reap AlphaCrafter job runs that have been stuck in *running* status.

    Queries ``AlphacrafterJobRun`` rows with ``status == "running"`` and
    ``created_at`` older than *max_age_hours*, sets their status to
    ``"interrupted"`` and records an error message.  Returns the number of
    rows reaped.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    stmt = select(AlphacrafterJobRun).where(
        AlphacrafterJobRun.status == "running",
        AlphacrafterJobRun.created_at < cutoff,
    )
    rows = db.execute(stmt).scalars().all()
    count = 0
    for row in rows:
        row.status = "interrupted"
        row.error_message = "Stale job reaped"
        row.completed_at = datetime.now(UTC)
        count += 1
    if count:
        db.commit()
        logger.warning("Reaped %d stale AlphaCrafter job run(s) older than %dh", count, max_age_hours)
    return count


async def _safe_stage(
    db: Session,
    job_run_id: str | None,
    stage_name: str,
    fn,
    progress_key: str,
    *,
    timeout_s: float = 300.0,
) -> tuple[Any, dict[str, Any]]:
    """Run a pipeline stage with structured error handling, timeout, and progress tracking.

    Returns ``(result, stage_summary)`` where ``stage_summary`` is serialisable
    and includes ``{stage, status, duration_s, [error]}``.

    Args:
        timeout_s: Maximum seconds to allow the stage to run.  Defaults to 300
            (5 minutes).  Set to ``0`` or ``None`` to disable the timeout.
    """
    t0 = time.monotonic()
    summary: dict[str, Any] = {"stage": stage_name, "status": "running"}
    # Write "running" marker *before* execution so the polling frontend sees in-flight work.
    _update_job_progress(db, job_run_id, progress={progress_key: summary})
    try:
        if timeout_s:
            result = await asyncio.wait_for(fn, timeout=timeout_s)
        else:
            result = await fn
        elapsed = time.monotonic() - t0
        summary["status"] = "ok"
        summary["duration_s"] = round(elapsed, 2)
        _update_job_progress(
            db,
            job_run_id,
            progress={progress_key: summary},
        )
        return result, summary
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        logger.warning(
            "AlphaCrafter stage '%s' timed out after %.0fs", stage_name, elapsed,
        )
        summary["status"] = "error"
        summary["error"] = f"Stage '{stage_name}' timed out after {timeout_s:.0f}s"
        summary["duration_s"] = round(elapsed, 2)
        _update_job_progress(
            db,
            job_run_id,
            progress={progress_key: summary},
            error=f"Stage '{stage_name}' timed out after {timeout_s:.0f}s",
        )
        return None, summary
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.exception("AlphaCrafter stage '%s' failed: %s", stage_name, e)
        summary["status"] = "error"
        summary["error"] = str(e)
        summary["duration_s"] = round(elapsed, 2)
        _update_job_progress(
            db,
            job_run_id,
            progress={progress_key: summary},
            error=f"Stage '{stage_name}' failed: {e}",
        )
        return None, summary


# ---------------------------------------------------------------------------
# SharedMemoryH-based pipeline (Task 7)
# ---------------------------------------------------------------------------

async def run_pipeline(
    db: Session,
    universe: list[str] | None = None,
    *,
    job_run_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Run the AlphaCrafter pipeline using SharedMemoryH with checkpointing.

    Creates a SharedMemoryH, runs agents sequentially, and persists H after
    each agent for checkpoint/restore capability.

    Stages:
        0. Data Ingestion — AlphaCrafterDataIngestion.ensure_data()
        1. Create Shared Memory — SharedMemoryH.create_initial()
        2. Miner.run(H)
        3. Screener.run(H)
        4. Trader.run(H)
        5. DossierBuilder.build(H)
        6. Persist H — H.persist(db, job_run_id)

    Args:
        db: Database session.
        universe: Symbols to mine/trade. Defaults to settings basket or SPY mega-cap.
        job_run_id: Optional AlphacrafterJobRun.id for checkpointing.
        user_id: Optional user ID to scope portfolio holdings.

    Returns:
        Dict with factors_generated, factors_selected, dossiers_created, shared_memory.
    """
    from app.lab.alphacrafter.data_ingestion import AlphaCrafterDataIngestion  # noqa: PLC0415

    universe = resolve_miner_universe(get_public_settings(db), universe)

    snapshot = RegimeStore(db).get_latest_snapshot()
    regime_label = snapshot.get("label") if snapshot else None

    # Stage 0: Data Ingestion
    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=365 * 5)
    ingestion = AlphaCrafterDataIngestion()
    data_availability = await ingestion.ensure_data(db, universe, start_date, end_date)

    # Stage 1: Create Shared Memory H
    h = SharedMemoryH.create_initial(
        universe=universe,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        regime_label=regime_label,
    )
    h.market_state.data_availability = {
        "price_symbols": data_availability.price_symbols,
        "fundamental_symbols": data_availability.fundamental_symbols,
        "news_symbols": data_availability.news_symbols,
    }
    h.append_history("orchestrator", "pipeline_start", {"universe": universe})

    # Stage 2: Miner — Agent reads universe/date_range from H, writes factor_states/miner_outputs.
    # alphacrafter_propose_llm is plumbed through so the LLM proposal branch
    # (miner._llm_extra_factors) is reachable on this async path too — it was
    # previously only wired in run_daily_alphacrafter (audit todo 19b).
    propose_llm = bool(get_public_settings(db).get("alphacrafter_propose_llm", False))
    miner_agent = MinerAgent()
    _, miner_summary = await _safe_stage(
        db, job_run_id, "miner",
        asyncio.to_thread(_run_in_thread_with_session, db.get_bind(), miner_agent.run, h, propose_llm),
        "stages.miner",
    )
    h.append_history("miner", miner_summary.get("status", "completed"), {"valid_count": len(h.factor_states)})
    h.persist(db, job_run_id)

    # Stage 3: Screener — Agent reads regime/factor_states from H, writes screener_outputs.
    if h.factor_states:
        screener_agent = ScreenerAgent()
        _, screener_summary = await _safe_stage(
            db, job_run_id, "screener",
            asyncio.to_thread(_run_in_thread_with_session, db.get_bind(), screener_agent.run, h),
            "stages.screener",
        )
    else:
        screener_summary = {"stage": "screener", "status": "skipped"}
    h.append_history("screener", screener_summary.get("status", "completed"), {"selected_count": len(h.screener_outputs.get("selected_factor_ids", []))})
    h.persist(db, job_run_id)

    # Stage 4: Trader — Agent reads selected_factor_ids from H, writes trader_outputs.
    selected_ids = h.screener_outputs.get("selected_factor_ids", [])
    if selected_ids:
        trader_agent = TraderAgent()
        _, trader_summary = await _safe_stage(
            db, job_run_id, "trader",
            asyncio.to_thread(_run_async_in_thread_with_session, db.get_bind(), trader_agent.run, h),
            "stages.trader",
        )
    else:
        trader_summary = {"stage": "trader", "status": "skipped"}
    h.append_history("trader", trader_summary.get("status", "completed"), {"n_configs_run": h.trader_outputs.get("n_configs_run", 0)})
    h.persist(db, job_run_id)

    # Stage 5: Dossier — Agent reads all from H, builds recommendations, writes evaluation.
    dossier_agent = DossierBuilder()
    _, dossier_summary = await _safe_stage(
        db, job_run_id, "dossier",
        asyncio.to_thread(_run_async_in_thread_with_session, db.get_bind(), dossier_agent.build, h, user_id),
        "stages.dossier",
    )
    h.append_history("dossier", dossier_summary.get("status", "completed"), {"dossiers_created": h.evaluation.get("dossiers_created", 0)})
    h.persist(db, job_run_id)

    if job_run_id:
        current = db.get(AlphacrafterJobRun, job_run_id)
        if current and current.status != "failed":
            _update_job_progress(db, job_run_id, status="completed")

    return {
        "factors_generated": len(h.factor_states),
        "factors_selected": len(selected_ids),
        "dossiers_created": h.evaluation.get("dossiers_created", 0),
        "shared_memory": h.to_dict(),
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_daily_alphacrafter(
    db: Session,
    universe: list[str] | None = None,
    *,
    job_run_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Run the complete AlphaCrafter pipeline: Miner → Screener → Trader → Dossier.

    Stages:
    1. **Retire decayed factors** (rolling IC below threshold for τ periods).
    2. **Miner**: compute real IC/ICIR over the index basket, persist survivors.
    3. **Screener**: regime-gate + diversify the persisted factors.
    4. **Trader**: backtest a config sweep on the selected factors.
    5. **Dossier**: package the top result with conviction from Sharpe + IC + regime.
       Generates one dossier per user portfolio holding (in addition to the basket
       anchor), making recommendations portfolio-aware.

    Args:
        db: Database session.
        universe: Symbols to mine/trade. Defaults to a small SPY + mega-cap basket.
        job_run_id: Optional ``AlphacrafterJobRun.id`` for progress tracking.
        user_id: Optional user ID to scope portfolio holdings to a single user.

    Returns:
        Counts and stage summaries:
        ``{factors_generated, factors_selected, dossiers_created, factors_retired,
          dossiers, stages: {...}}``
    """
    t0 = time.monotonic()

    settings = get_public_settings(db)

    universe = resolve_miner_universe(settings, universe)

    # Current regime context (Plan 1) — gates the Screener and informs the Miner.
    snapshot = RegimeStore(db).get_latest_snapshot()
    regime_label = snapshot.get("label") if snapshot else None
    propose_llm = bool(settings.get("alphacrafter_propose_llm", False))

    stages: dict[str, dict[str, Any]] = {}

    if job_run_id is None:
        import uuid
        job_run = AlphacrafterJobRun(
            id=str(uuid.uuid4()),
            status="running",
            progress_json=json.dumps({"stage": "init"}),
        )
        db.add(job_run)
        db.commit()
        job_run_id = job_run.id

    # Stage 0: retire decayed factors.
    retired_count = 0
    try:
        retired_factors = await auto_retire_factors(db)
        retired_count = len(retired_factors)
    except Exception as e:
        logger.warning("AlphaCrafter factor retirement failed (non-fatal): %s", e)

    # Stage 0b: bars for the universe. The miner's panel reads bar_prices
    # only, and nothing else ingests most of the 150-name cross-section.
    from app.lab.alphacrafter.data_ingestion import AlphaCrafterDataIngestion  # noqa: PLC0415

    now = datetime.now(UTC)
    try:
        availability = await AlphaCrafterDataIngestion().ensure_data(
            db, universe, now - timedelta(days=int(365.25 * 5)), now,
        )
        stages["data"] = {
            "stage": "data",
            "status": "ok",
            "symbols": len(universe),
            "with_bars": availability.price_symbols,
            "failed": availability.failed_symbols[:20],
        }
    except Exception as e:
        logger.warning("AlphaCrafter data stage failed (non-fatal): %s", e)
        stages["data"] = {"stage": "data", "status": "error", "reason": str(e)}
    _update_job_progress(db, job_run_id, progress={"stages.data": stages["data"]})

    # Stage 1: Miner — real metrics, persists survivors.
    candidates, stage_summary = await _safe_stage(
        db, job_run_id, "miner",
        run_miner(db, universe, lookback_years=5.0, propose_llm=propose_llm, regime_label=regime_label),
        "stages.miner",
    )
    stages["miner"] = stage_summary

    valid: list = []
    candidate_ids: list = []
    if candidates:
        valid = [c for c in candidates if c.valid and c.id]
        candidate_ids = [c.id for c in valid][:10]

    # Enrich miner summary with count and reason.
    miner_summary = stages.get("miner", {})
    miner_summary["count"] = len(valid)
    miner_summary["symbols"] = universe[:10]
    if len(valid) == 0:
        miner_summary["reason"] = (
            f"No factors passed IC/ICIR gates (min |IC|={settings.get('alphacrafter_min_ic', 0.02)}, "
            f"min |ICIR|={settings.get('alphacrafter_min_icir', 0.3)}) from {len(candidates or [])} candidates. "
            "Try lowering thresholds or expanding the basket."
        )
    stages["miner"] = miner_summary
    if job_run_id:
        _update_job_progress(db, job_run_id, progress={"stages.miner": miner_summary})

    # Stage 2: Screener — regime-gate + diversify.
    if candidate_ids:
        screener_result, stage_summary = await _safe_stage(
            db, job_run_id, "screener",
            run_screener(db, candidate_ids),
            "stages.screener",
        )
    else:
        screener_result = None
        stage_summary = {"stage": "screener", "status": "skipped", "reason": "no mined factors to screen"}
        _update_job_progress(db, job_run_id, progress={"stages.screener": stage_summary})
    stages["screener"] = stage_summary

    selected_ids: list = []
    if screener_result:
        selected_ids = screener_result.selected_factor_ids

    # Enrich screener summary with count and reason.
    screener_summary = stages.get("screener", {})
    screener_summary["count"] = len(selected_ids)
    if len(selected_ids) == 0 and len(candidate_ids) > 0:
        screener_summary["reason"] = (
            f"All {len(candidate_ids)} factor(s) were filtered out by regime gating "
            "or correlation diversification. Check regime snapshots in Control Center."
        )
    stages["screener"] = screener_summary
    if job_run_id:
        _update_job_progress(db, job_run_id, progress={"stages.screener": screener_summary})

    # Stage 3: Trader — settings-driven config sweep on the selected factors
    # (todo 19a): the grid comes from the alphacrafter_* settings via
    # run_trader's parsed values, capped at MAX_SWEEP_CONFIGS with a
    # warn-truncate log. Every swept config is persisted to TraderBacktest.
    trader_results = None
    if selected_ids and screener_result:
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=365)
        trader_results, stage_summary = await _safe_stage(
            db, job_run_id, "trader",
            run_trader(
                db,
                screener_result.screener_run_id,
                universe=universe,
                start_date=start_date,
                end_date=end_date,
                num_configs=3,
                max_sweep_configs=MAX_SWEEP_CONFIGS,
            ),
            "stages.trader",
        )
        stages["trader"] = stage_summary
        # Enrich trader summary with count.
        if trader_results:
            stage_summary["count"] = len(trader_results)
            _update_job_progress(db, job_run_id, progress={"stages.trader": stage_summary})
    else:
        stages["trader"] = {"stage": "trader", "status": "skipped", "reason": "no factors selected by screener"}

    # Stage 4: Dossier — build recommendations.
    dossier_symbols: list[str] = []
    dossier_count = 0

    # Shared support-factor context (computed once, shared across dossiers).
    support: list = []
    mean_ic = 0.0
    if trader_results:
        selected_set = set(selected_ids)
        selected_candidates = [c for c in valid if c.id in selected_set]
        support = selected_candidates or valid[:3]
        mean_ic = (sum(c.ic for c in support) / len(support) if support else 0.0)

    # Mark dossier stage as "running" for async progress tracking.
    _update_job_progress(
        db, job_run_id,
        progress={"stages.dossier": {"stage": "dossier", "status": "running"}},
    )

    if trader_results and screener_result:
        top = trader_results[0]
        trader_cfg = {
            "sharpe_ratio": top.sharpe_ratio,
            "max_drawdown": top.max_drawdown,
            "total_return": top.total_return,
            "rebalance_freq": top.config.rebalance_freq,
            "position_size": top.config.position_size,
        }

        # Collect target symbols: basket anchor + portfolio holdings.
        dossier_targets = list(dict.fromkeys([universe[0]] + _get_portfolio_holdings(db, user_id=user_id)))
        # Limit to symbols that are actually in the universe (we mined on it).
        dossier_targets = [s for s in dossier_targets if s in universe]

        for sym in dossier_targets:
            try:
                await build_dossier(
                    db,
                    symbol=sym,
                    miner_factors=[c.name for c in support] if support else [],
                    screener_regime=screener_result.regime_label,
                    trader_config=trader_cfg,
                    mean_ic=mean_ic,
                    regime_score=screener_result.regime_score,
                    crisis=screener_result.crisis,
                    user_id=user_id,
                )
                dossier_symbols.append(sym)
                dossier_count += 1
            except Exception:
                logger.warning("AlphaCrafter dossier failed for %s", sym)

        dossier_summary = {
            "stage": "dossier",
            "status": "ok",
            "symbols": dossier_symbols,
            "count": dossier_count,
        }
        stages["dossier"] = dossier_summary
        _update_job_progress(db, job_run_id, progress={"stages.dossier": dossier_summary})
    else:
        dossier_summary = {
            "stage": "dossier",
            "status": "skipped",
            "reason": "no trader results available",
        }
        stages["dossier"] = dossier_summary
        _update_job_progress(db, job_run_id, progress={"stages.dossier": dossier_summary})

    # Final completion — only set "completed" if the job hasn't already been
    # marked as failed (by _safe_stage / _update_job_progress error path).
    final_status: str | None = None
    if job_run_id:
        current = db.get(AlphacrafterJobRun, job_run_id)
        if current and current.status != "failed":
            final_status = "completed"

    # Build a user-facing message explaining the outcome.
    if dossier_count > 0:
        result_message = f"Generated {dossier_count} recommendation(s)."
    elif len(selected_ids) == 0 and len(valid) == 0:
        result_message = (
            "No factor candidates were found with sufficient statistical "
            "significance (IC/ICIR). Try expanding the universe basket or "
            "increasing the lookback period in Settings → AlphaCrafter."
        )
    elif len(selected_ids) == 0:
        result_message = (
            f"Found {len(valid)} factor candidate(s) but none passed the "
            "screener's regime-gating or diversification filters."
        )
    elif not trader_results:
        result_message = (
            "Factor backtests produced no positive-Sharpe configurations. "
            "The market regime may not be conducive to the mined factors."
        )
    else:
        result_message = "Pipeline completed but no recommendations were generated."

    total_duration_s = round(time.monotonic() - t0, 2)

    _update_job_progress(
        db, job_run_id,
        status=final_status,
        result={
            "factors_generated": len(candidates) if candidates else 0,
            "factors_selected": len(selected_ids),
            "dossiers_created": dossier_count,
            "factors_retired": retired_count,
            "dossier_symbols": dossier_symbols,
            "total_duration_s": total_duration_s,
            "message": result_message,
        },
    )

    return {
        "factors_generated": len(candidates) if candidates else 0,
        "factors_selected": len(selected_ids),
        "dossiers_created": dossier_count,
        "factors_retired": retired_count,
        "dossier_symbols": dossier_symbols,
        "total_duration_s": total_duration_s,
        "message": result_message,
        "stages": stages,
    }


# ---------------------------------------------------------------------------
# Async job wrapper (called via submit_job in a background thread)
# ---------------------------------------------------------------------------

def run_async_pipeline_job(
    job_run_id: str,
    universe: list[str] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper for ``submit_job``.

    Creates its own DB session and event loop.  The caller is expected to have
    already persisted an ``AlphacrafterJobRun`` row with *job_run_id*.

    Dispatches to ``run_pipeline`` (SharedMemoryH-based) rather than the
    legacy ``run_daily_alphacrafter``.
    """
    from app.foundation.core.db import get_db

    db = next(get_db())
    try:
        result = asyncio.run(run_pipeline(db, universe, job_run_id=job_run_id, user_id=user_id))
        return result
    except Exception:
        logger.exception("Pipeline failed for job %s", job_run_id)
        _update_job_progress(
            db, job_run_id,
            status="failed",
            error="Pipeline failed: internal error",
        )
        return {"error": "An internal error occurred"}
    finally:
        db.close()
