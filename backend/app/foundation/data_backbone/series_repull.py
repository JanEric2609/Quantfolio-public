"""Weekly full re-pull of stored bar series (plan 2026-10-12, D1).

Adjusted providers restate history after every dividend and split, so an
incrementally ingested series drifts off its provider's current scale: the
day after an ex-date the stored history still carries the old factor and the
return across that day is price-only. A full re-pull of each series puts it
back on one scale. Discover already re-pulls five years for its candidates on
each run; this job covers everything else (held symbols, benchmarks, the
regime index), oldest re-pull first.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: A series re-pulled within this many days is skipped.
REPULL_MAX_AGE_DAYS = 6
#: Cap per run, above the stored universe (851 series on 2026-10-12) so every
#: series is re-pulled each week. Most series are pinned to yfinance (~2,000
#: calls an hour); a Tiingo-pinned series that hits Tiingo's 50 an hour fails
#: and keeps its pin until the next run.
REPULL_MAX_SYMBOLS = 1500


def due_symbols(db: Session, *, now: datetime | None = None, limit: int = REPULL_MAX_SYMBOLS) -> list[str]:
    """Stored symbols whose last full re-pull is older than the max age, oldest first."""
    from app.foundation.models.entities import BarSeriesMeta

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=REPULL_MAX_AGE_DAYS)
    stored = [r[0] for r in db.execute(text("SELECT DISTINCT symbol FROM bar_prices"))]
    pins = {p.symbol: p for p in db.query(BarSeriesMeta).all()}

    def _last(symbol: str) -> datetime:
        pin = pins.get(symbol)
        ts = pin.last_full_repull_at if pin is not None else None
        if ts is None:
            return datetime.min.replace(tzinfo=UTC)
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    due = [s for s in stored if _last(s) < cutoff]
    due.sort(key=_last)
    return due[:limit]


def repull_due_series(db: Session, *, limit: int = REPULL_MAX_SYMBOLS) -> dict[str, Any]:
    """Re-pull each due series from its pinned provider; never raises.

    A pinned provider that is no longer configured is replaced through the
    adjusted chain (a full re-pull, so no splice). A pinned provider that
    merely failed today keeps its pin and is retried next week.
    """
    from app.foundation.data_backbone.ingest import DataIngester
    from app.foundation.models.entities import BarSeriesMeta

    outcome: dict[str, Any] = {"repulled": 0, "failed": [], "split_unadjusted": []}
    try:
        ingester = DataIngester(db)
        configured = {p.name for p in ingester.registry.providers if getattr(p, "enabled", True)}
        for symbol in due_symbols(db, limit=limit):
            pin = db.get(BarSeriesMeta, symbol)
            provider = pin.provider if pin is not None and pin.provider in configured else None
            result = ingester.repull_series(symbol, provider=provider)
            if result.get("success"):
                outcome["repulled"] += 1
                if result.get("split_unadjusted"):
                    outcome["split_unadjusted"].append(symbol)
            else:
                outcome["failed"].append(symbol)
    except Exception as exc:  # noqa: BLE001 - a scheduled job logs and continues
        logger.error("bar re-pull failed: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        outcome["error"] = str(exc)[:200]
    logger.info(
        "bar re-pull: %d re-pulled, %d failed, split still unadjusted: %s",
        outcome["repulled"], len(outcome["failed"]), outcome["split_unadjusted"],
    )
    return outcome
