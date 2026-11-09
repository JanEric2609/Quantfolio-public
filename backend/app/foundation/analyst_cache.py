"""A short-lived cache of price-target consensus payloads (plan 2026-11-08, §6).

Discover asked Yahoo's ``.info`` for every candidate on every run. At 250
names that worked; at 1,500 it would run into Yahoo's throttling (reported
from about 950 tickers in a burst). Consensus targets move slowly, so a
payload younger than :data:`TTL` is reused, and the nightly universe prefetch
refreshes the cache at a gentle pace. Only successful payloads are stored: a
timeout or "no coverage" is asked again next time.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import AnalystConsensusCache

TTL = timedelta(days=3)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def cached(db: Session, symbol: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The stored payload for *symbol* when younger than :data:`TTL`, else None."""
    row = db.get(AnalystConsensusCache, symbol.upper())
    if row is None or not row.ok:
        return None
    if _aware(row.fetched_at) < (now or datetime.now(UTC)) - TTL:
        return None
    return dict(row.payload_json or {})


def store(db: Session, symbol: str, payload: dict[str, Any]) -> None:
    """Keep a successful payload inside a savepoint; the caller's transaction commits it.

    A failed write rolls back only the savepoint, never the caller's work.
    """
    if not payload.get("ok"):
        return
    try:
        with db.begin_nested():
            row = db.get(AnalystConsensusCache, symbol.upper())
            if row is None:
                row = AnalystConsensusCache(symbol=symbol.upper())
                db.add(row)
            # Dates and other non-JSON values become strings, as the API shows them.
            row.payload_json = json.loads(json.dumps(payload, default=str))
            row.ok = True
            row.fetched_at = datetime.now(UTC)
    except Exception:  # noqa: BLE001 - a cache write must never fail the caller
        pass


def get_or_fetch(db: Session, symbol: str, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """The cached payload, or *fetch*'s (stored when it succeeded)."""
    hit = cached(db, symbol)
    if hit is not None:
        return hit
    result = fetch(symbol)
    if isinstance(result, dict):
        store(db, symbol, result)
        return result
    return {"ok": False}


__all__ = ["TTL", "cached", "get_or_fetch", "store"]
