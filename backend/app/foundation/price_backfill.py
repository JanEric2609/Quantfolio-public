"""Price history backfill service for holdings and DKB positions.

Triggers:
  1. End of DKB sync (persist_snapshot) — non-blocking background job
  2. Worker refresh_prices cycle — extends existing 30d logic with 5y-if-empty
  3. POST /api/data/backfill/holdings — extended to use this service

Strategy per symbol:
  - No bars in bar_prices → full 5-year backfill via DataIngester.ingest_bar_prices
  - Existing bars → incremental from last bar date (30d minimum)
  - Missing ticker → resolve via portfolio_bridge.resolve_isin_to_ticker first
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker

logger = logging.getLogger(__name__)

BACKFILL_DAYS_DEFAULT = 1825  # ~5 years
INCREMENTAL_DAYS = 30

# Symbols backfilled concurrently within one user's pass. The 15-minute
# refresh used to walk every symbol one after another, so a few hundred
# symbols (each a provider round trip) could outlast its own interval.
# Override with PRICE_BACKFILL_WORKERS; 1 restores the sequential walk.
BACKFILL_MAX_WORKERS = 4


def _configured_workers() -> int:
    raw = os.getenv("PRICE_BACKFILL_WORKERS")
    try:
        return max(1, int(raw)) if raw else BACKFILL_MAX_WORKERS
    except ValueError:
        return BACKFILL_MAX_WORKERS


def _worker_count(db: Session, symbols: int, max_workers: int | None) -> int:
    """How many symbols to backfill at once.

    ``None`` means the configured default, except on SQLite: one writer at a
    time there, so extra threads only add "database is locked" contention (and
    the in-memory test database is a single shared connection). An explicit
    value is always honoured.
    """
    if max_workers is None:
        bind = db.get_bind()
        if bind.dialect.name == "sqlite":
            return 1
        max_workers = _configured_workers()
    return max(1, min(max_workers, symbols))


def backfill_user_prices(
    db: Session,
    user_id: str,
    days: int = BACKFILL_DAYS_DEFAULT,
    *,
    max_workers: int | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> dict[str, Any]:
    """Backfill price data for all of a user's holdings and DKB positions.

    Args:
        db: Database session.
        user_id: The user's UUID.
        days: How many days of history to fetch for symbols with no data.
        max_workers: Symbols processed concurrently (default
            :data:`BACKFILL_MAX_WORKERS`; 1 on SQLite). Each worker thread uses
            its own session, never ``db``.
        session_factory: Builds a worker's session; defaults to a session on
            ``db``'s engine.

    Returns:
        Per-symbol results dict.
    """
    # 1. Resolve ISINs → tickers first
    resolve_isin_to_ticker(db, user_id)

    # 2. Collect all tickers
    tickers: set[str] = set()

    # From Holding table
    from app.foundation.models.entities import Holding, Portfolio
    portfolio_ids = [
        p.id for p in db.query(Portfolio.id).filter(Portfolio.user_id == user_id).all()
    ]
    if portfolio_ids:
        holding_tickers = [
            r[0] for r in db.query(Holding.ticker)
            .filter(Holding.portfolio_id.in_(portfolio_ids), Holding.ticker.isnot(None), Holding.ticker != "")
            .distinct().all()
        ]
        tickers.update(holding_tickers)

    # From every synced broker position (DKB, Scalable)
    from app.foundation.live_positions import live_tickers
    tickers.update(live_tickers(db, user_id))

    # The passive core is the benchmark every health/excess-return view
    # compares against, so keep it fresh even when no holding carries that
    # exact listing (it went stale at 2026-08-27 when holdings moved venue).
    from app.foundation.settings import get_public_settings
    core = str(get_public_settings(db).get("passive_core_ticker") or "").strip().upper()
    if core and tickers:
        tickers.add(core)

    if not tickers:
        logger.info("backfill_user_prices: no tickers found for user %s", user_id)
        return {"total": 0, "succeeded": 0, "failed": 0, "results": []}

    # 3. Backfill per ticker
    ordered = sorted(tickers)
    workers = _worker_count(db, len(ordered), max_workers)
    if workers <= 1:
        ingester = DataIngester(db)
        results = [_backfill_symbol_guarded(db, ingester, ticker, days) for ticker in ordered]
    else:
        factory = session_factory or sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
        # Provider rate limits stay global: every worker goes through the same
        # process-wide limiters in ProviderRegistry.first_success (a throttled
        # provider is skipped for the next one in the chain, never overrun).
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="price-backfill") as pool:
            futures = [pool.submit(_backfill_symbol_own_session, factory, ticker, days) for ticker in ordered]
            results = [future.result() for future in futures]  # sorted-ticker order, as sequentially
    succeeded = sum(1 for result in results if result.get("success"))
    failed = len(results) - succeeded

    return {
        "total": len(tickers),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def _backfill_symbol_guarded(
    db: Session, ingester: DataIngester, symbol: str, days: int,
) -> dict[str, Any]:
    """:func:`_backfill_one_symbol`, with one symbol's failure contained."""
    try:
        return _backfill_one_symbol(db, ingester, symbol, days)
    except Exception as exc:
        logger.warning("backfill_user_prices: error for %s: %s", symbol, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return {"symbol": symbol, "success": False, "message": str(exc)[:200]}


def _backfill_symbol_own_session(
    session_factory: Callable[[], Session], symbol: str, days: int,
) -> dict[str, Any]:
    """Backfill one symbol on a session private to the calling worker thread."""
    session = session_factory()
    try:
        return _backfill_symbol_guarded(session, DataIngester(session), symbol, days)
    finally:
        session.close()


def _backfill_one_symbol(
    db: Session,
    ingester: DataIngester,
    symbol: str,
    full_days: int,
) -> dict[str, Any]:
    """Backfill a single symbol — full fetch if empty, incremental otherwise.

    Returns:
        Dict with symbol/success/rows_inserted/provider/date_range.
    """
    last_bar = _last_bar_date(db, symbol)

    if last_bar is None:
        # Full backfill
        result = ingester.ingest_bar_prices(symbol=symbol, days=full_days)
        result["mode"] = "full"
        return result

    # Incremental: fetch from last bar minus 1 day overlap
    start = (last_bar - timedelta(days=1)).strftime("%Y-%m-%d")
    result = ingester.ingest_bar_prices(symbol=symbol, start_date=start)
    result["mode"] = "incremental"
    return result


def _last_bar_date(db: Session, symbol: str) -> datetime | None:
    """Return the latest ts for *symbol* in bar_prices, or None."""
    try:
        row = db.execute(
            text("SELECT MAX(ts) FROM bar_prices WHERE symbol = :sym"),
            {"sym": symbol},
        ).scalar()
        if row is None:
            return None
        if isinstance(row, str):
            return datetime.fromisoformat(row)
        return row
    except Exception:
        return None


# ---------------------------------------------------------------------------
# refresh_prices extended logic (called from worker.py)
# ---------------------------------------------------------------------------


def refresh_all_user_prices() -> dict[str, Any]:
    """Extend the existing 30d price refresh with DKB positions + 5y-if-empty.

    Called from worker.py refresh_prices to replace the current logic.
    Handles all users, own SessionLocal.
    """
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import User

    db = SessionLocal()
    try:
        user_ids = [r[0] for r in db.query(User.id).all()]
        combined = {"total": 0, "succeeded": 0, "failed": 0, "user_results": {}}
        for uid in user_ids:
            try:
                result = backfill_user_prices(db, uid, days=BACKFILL_DAYS_DEFAULT)
                combined["total"] += result["total"]
                combined["succeeded"] += result["succeeded"]
                combined["failed"] += result["failed"]
                combined["user_results"][uid] = {
                    "total": result["total"],
                    "succeeded": result["succeeded"],
                    "failed": result["failed"],
                }
            except Exception as exc:
                logger.warning("refresh_all_user_prices: user %s failed: %s", uid, exc)
                combined["failed"] += 1
                try:
                    db.rollback()
                except Exception:
                    pass

        logger.info(
            "refresh_all_user_prices: %d/%d succeeded, %d failed across %d users",
            combined["succeeded"], combined["total"], combined["failed"], len(user_ids),
        )
        return combined
    finally:
        db.close()
