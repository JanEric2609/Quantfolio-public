"""Data management API for TimescaleDB backbone."""

from datetime import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.data_backbone.bars import BarStore
from app.foundation.settings import get_public_settings
from app.foundation.data_backbone.factors_store import FactorStore
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.data_backbone.regime_store import RegimeStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


class BackfillRequest(BaseModel):
    """Request to backfill historical data."""

    symbol: str
    start_date: str | None = None
    end_date: str | None = None
    days: int | None = Field(default=None, ge=1, le=3650)


class HoldingsBackfillResponse(BaseModel):
    """Result of backfilling all holdings for a user."""

    total: int
    succeeded: int
    failed: int
    severity: str
    title: str
    message: str
    results: list[dict[str, Any]]


class BackfillJobResponse(BaseModel):
    """Enqueued async backfill job handle."""

    job_id: str
    status: str


class BackfillJobStatusResponse(BaseModel):
    """Poll payload for an async backfill job."""

    job_id: str
    status: str
    days: int
    total: int | None
    succeeded: int | None
    failed: int | None
    error_message: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class DataCoverageResponse(BaseModel):
    """Data coverage info for a symbol."""

    symbol: str
    first_ts: datetime | None
    last_ts: datetime | None
    bar_count: int | None
    last_provider: str | None
    gaps_count: int


class ProviderHealthEntry(BaseModel):
    """Provider health status."""

    provider: str
    capability: str
    ok: bool
    latency_ms: float | None
    timestamp: datetime
    message: str | None


@router.post("/backfill")
def backfill_data(
    request: BackfillRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Backfill historical data for a symbol.

    Args:
        request: Backfill request with symbol and date range.

    Returns:
        Dict with ingestion results.
    """
    ingester = DataIngester(db)
    result = ingester.ingest_bar_prices(
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        days=request.days,
    )
    return result


@router.post("/backfill/holdings")
def backfill_holdings(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    days: int = Query(1825, ge=1, le=3650, description="Days of history to backfill (default 5y, max 10y)"),
) -> HoldingsBackfillResponse:
    """Backfill price data for all of the user's holdings + DKB positions.

    Iterates over all tickers (Holdings + DkbPositions) and triggers a
    price-data backfill for each one. New symbols get full *days* history;
    existing symbols get incremental from last bar. Returns per-symbol results.
    """
    from app.foundation.price_backfill import backfill_user_prices

    result = backfill_user_prices(db, user.id, days=days)
    total = result["total"]
    succeeded = result["succeeded"]
    failed = result["failed"]
    severity = "info" if failed == 0 else "warning"
    title = f"Price backfill: {succeeded}/{total} succeeded"
    if failed:
        title += f" ({failed} failed)"
    message = f"Backfilled price data for {total} symbols. {succeeded} succeeded, {failed} failed."

    return HoldingsBackfillResponse(
        total=total,
        succeeded=succeeded,
        failed=failed,
        severity=severity,
        title=title,
        message=message,
        results=result["results"],
    )


@router.post("/backfill/holdings/async")
def backfill_holdings_async(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    days: int = Query(1825, ge=1, le=3650, description="Days of history to backfill (default 5y, max 10y)"),
) -> BackfillJobResponse:
    """Enqueue a background backfill of all the user's holdings/DKB positions.

    Returns a job id immediately; poll ``GET /api/data/backfill/{job_id}`` for
    progress instead of blocking the request on a multi-year provider fetch.
    """
    from app.foundation.backfill_jobs import enqueue_backfill

    job_id = enqueue_backfill(db, user.id, days=days)
    return BackfillJobResponse(job_id=job_id, status="queued")


@router.get("/backfill/{job_id}")
def get_backfill_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BackfillJobStatusResponse:
    """Poll the status of an async backfill job (scoped to the current user)."""
    from app.foundation.backfill_jobs import get_backfill_status

    status = get_backfill_status(db, job_id, user.id)
    if status is None:
        raise HTTPException(status_code=404, detail="Backfill job not found")
    return BackfillJobStatusResponse(**status)


@router.get("/coverage")
def get_coverage(
    symbol: str = Query(...),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> DataCoverageResponse | dict[str, str]:
    """Get data coverage for a symbol.

    Args:
        symbol: Ticker symbol.

    Returns:
        Coverage info or error.
    """
    store = BarStore(db)
    coverage = store.get_coverage(symbol)

    if coverage is None:
        return {"error": f"No data found for {symbol}"}

    return DataCoverageResponse(
        symbol=coverage["symbol"],
        first_ts=coverage["first_ts"],
        last_ts=coverage["last_ts"],
        bar_count=coverage.get("bar_count"),
        last_provider=coverage["last_provider"],
        gaps_count=coverage.get("gaps_count", 0),
    )


class ResearchExtractResponse(BaseModel):
    """Freshness of one research extract in the Parquet panel."""

    key: str
    label: str
    refresh: str
    stale_after_days: int
    used_for: str
    newest: str | None = None
    rows: int = 0
    age_days: int | None = None
    stale: bool
    error: str | None = None


@router.get("/research-extracts")
def get_research_extracts(
    _user: User = Depends(current_user),
) -> list[ResearchExtractResponse]:
    """How current each research extract is (insider, IBES, JKP, WRDS links).

    The WRDS ones are exported by hand; a stale one shows here instead of
    silently emptying a Discover signal.
    """
    from app.foundation.data_engineering.extract_freshness import research_extract_freshness

    return [ResearchExtractResponse(**entry) for entry in research_extract_freshness()]


@router.get("/providers/health")
def get_provider_health(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """Get provider health history (latest events).

    Returns:
        List of recent health entries.
    """
    try:
        from sqlalchemy import text

        sql = text("""
            SELECT provider, capability, ok, latency_ms, ts, message
            FROM provider_health_history
            ORDER BY ts DESC
            LIMIT 100
        """)

        result = db.execute(sql)
        rows = result.fetchall()

        return [
            {
                "provider": row[0],
                "capability": row[1],
                "ok": row[2],
                "latency_ms": row[3],
                "timestamp": row[4],
                "message": row[5],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error fetching provider health: {e}")
        raise HTTPException(status_code=500, detail="Error fetching provider health")


@router.post("/macro/refresh")
def refresh_macro_indicators(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Refresh macro indicators from FRED, ECB, Bundesbank.

    Returns:
        Dict with refresh results per provider.
    """
    ingester = DataIngester(db)
    result = ingester.ingest_macro_indicators()
    return result


@router.get("/symbols")
def list_symbols_with_data(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[str]:
    """List all symbols with bar data.

    Returns:
        List of symbols.
    """
    store = BarStore(db)
    return store.list_symbols_with_data()


@router.get("/factors")
def list_factors(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[str]:
    """List all factors with loadings data.

    Returns:
        List of factors.
    """
    store = FactorStore(db)
    return store.get_all_factors()


@router.get("/regime/labels")
def list_regime_labels(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[str]:
    """List all regime labels.

    Returns:
        List of labels.
    """
    store = RegimeStore(db)
    return store.get_labels()


class RegimeCurrentResponse(BaseModel):
    """Current market regime snapshot for the Dashboard badge."""

    ts: datetime | None
    label: str | None
    score: float | None
    crisis: bool
    probs: dict[str, float]
    source: str | None
    stale: bool = False
    age_hours: float | None = None


class RegimeHistoryEntry(BaseModel):
    """A single historical regime snapshot."""

    ts: datetime
    label: str
    score: float
    source: str


@router.get("/regime/current")
def get_regime_current(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> RegimeCurrentResponse:
    """Return the latest regime snapshot (label, crisis flag, probs, timestamp).

    Flags the snapshot stale when its age exceeds regime_stale_ttl_hours so a
    dead pipeline cannot masquerade as live analysis. Returns an empty payload
    (label=None, crisis=False) when no snapshot exists.
    """
    store = RegimeStore(db)
    # The jump model is the one regime model; HMM fallback rows are not shown.
    snap = store.get_latest_snapshot(source="jump")
    if snap is None:
        return RegimeCurrentResponse(
            ts=None, label=None, score=None, crisis=False, probs={}, source=None,
            stale=False, age_hours=None,
        )

    ttl_raw = get_public_settings(db).get("regime_stale_ttl_hours")
    ttl_hours = float(ttl_raw) if ttl_raw is not None else 72.0
    age_hours = snap.get("age_hours")
    payload = snap.get("payload") or {}
    probs = payload.get("probs") or {}
    return RegimeCurrentResponse(
        ts=snap.get("ts"),
        label=snap.get("label"),
        score=snap.get("score"),
        crisis=bool(payload.get("crisis", False)),
        probs={str(k): float(v) for k, v in probs.items()},
        source=snap.get("source"),
        stale=age_hours is not None and age_hours > ttl_hours,
        age_hours=age_hours,
    )


@router.get("/regime/history")
def get_regime_history(
    start: str | None = Query(None, description="ISO start timestamp (inclusive)"),
    end: str | None = Query(None, description="ISO end timestamp (inclusive)"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[RegimeHistoryEntry]:
    """Return regime snapshot history (ascending by ts), optionally date-bounded."""
    store = RegimeStore(db)

    try:
        start_dt = datetime.fromisoformat(start) if start else None
        end_dt = datetime.fromisoformat(end) if end else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {exc}") from exc

    df = store.get_history(start=start_dt, end=end_dt)
    if df is None or len(df) == 0:
        return []

    return [
        RegimeHistoryEntry(
            ts=row["ts"],
            label=str(row["label"]),
            score=float(row["score"]),
            source=str(row["source"]),
        )
        for row in df.to_dict("records")
    ]
