"""Nightly precompute and on-demand reuse of the user-independent Discover stages (plan 2026-11-10, WP9).

A morning run spent 1,291 s, most of it recomputing per-symbol stages that depend
only on stored bars: momentum, backtest against the benchmark, the verification
gate and the quant signals. Their output is cached per (symbol, stage, bar date),
where the bar date is the symbol's latest stored bar. Portfolio fit, ranking and
the LLM stay on demand. Stages that read the news or fundamentals tables are not
cached: those change without a new bar.

**Same output with and without the cache, by construction:** every stage result
passes through ``canonical()`` (a JSON round trip) whether it was computed or
loaded, so a run cannot tell the two apart.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Stages that depend on a symbol's stored bars (and the benchmark's) only.
#: Bump whenever a cached stage function changes: it is part of every key, so rows
#: written by the old code are no longer read (and are pruned after a week).
STAGE_CACHE_VERSION = 1

CACHEABLE_STAGES = frozenset({
    "momentum_quality", "backtest_vs_benchmark", "verification_gate", "quant_signals",
    # Factor exposure: bars of the symbol and the miner universe, plus the factor
    # library; its variant carries the library's signature and the day.
    "alpha_miner",
})


def _default(value: Any) -> Any:
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def canonical(value: Any) -> Any:
    """The JSON form of a stage result: what a cached run reads is also what a fresh run uses."""
    return json.loads(json.dumps(value, default=_default))


_BAR_DATE_MEMO: dict[str, tuple[float, str | None]] = {}
_BAR_DATE_TTL_S = 45.0


def latest_bar_date(db: Session, symbol: str) -> str | None:
    """The date of the symbol's latest stored daily bar, or None.

    Remembered for 45 s per symbol: the cached stages of one candidate run back to
    back, and each used to query the bar table on its own (about 45 ms per stage,
    ~70 s per stage over a 1,620-name run, even with every entry warm).
    """
    import time

    now = time.monotonic()
    hit = _BAR_DATE_MEMO.get(symbol)
    if hit is not None and now - hit[0] < _BAR_DATE_TTL_S:
        return hit[1]
    row = db.execute(text("SELECT max(ts) FROM bar_prices WHERE symbol = :s"), {"s": symbol}).scalar()
    value = None if row is None else str(row)[:10]
    if len(_BAR_DATE_MEMO) > 5000:
        _BAR_DATE_MEMO.clear()
    _BAR_DATE_MEMO[symbol] = (now, value)
    return value


def cached_stage(
    db: Session, symbol: str, stage: str, fn: Callable[..., Any], *args: Any, variant: str = "",
    store_if: Callable[[Any], bool] | None = None, **kwargs: Any,
) -> Any:
    """``fn(*args, **kwargs)`` through the cache when *stage* is cacheable and the symbol has a bar date.

    *variant* distinguishes arguments the symbol fixes (``is_etf``, ``asset_class``).
    Only a computed result is stored; a failing stage raises as before. *store_if*,
    when given, must accept the result for it to be stored (an error result is not).
    """
    from app.foundation.models.entities import DiscoverStageCache

    if stage not in CACHEABLE_STAGES:
        return fn(*args, **kwargs)
    bar_date = latest_bar_date(db, symbol)
    if bar_date is None:
        return fn(*args, **kwargs)
    key = f"v{STAGE_CACHE_VERSION}:{stage}:{variant}" if variant else f"v{STAGE_CACHE_VERSION}:{stage}"
    row = db.get(DiscoverStageCache, (symbol, key, bar_date))
    if row is not None:
        try:
            cached = json.loads(row.payload_json)
            return cached[0], cached[1]
        except (ValueError, IndexError, TypeError):
            logger.debug("stage cache entry for %s/%s unreadable; recomputing", symbol, key)
    result = fn(*args, **kwargs)
    payload = canonical([result[0], result[1]])
    if store_if is not None and not store_if(result):
        return payload[0], payload[1]
    try:
        db.merge(DiscoverStageCache(symbol=symbol, stage=key, bar_date=bar_date,
                                    payload_json=json.dumps(payload), computed_at=datetime.now(UTC)))
        db.commit()
    except Exception:  # noqa: BLE001 - a cache write never fails a run
        db.rollback()
        logger.debug("stage cache write failed for %s/%s", symbol, key, exc_info=True)
    return payload[0], payload[1]


def prune(db: Session, keep_days: int = 7) -> int:
    """Delete entries older than *keep_days* (a bar date is superseded by the next night's)."""
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).date().isoformat()
    from app.foundation.models.entities import DiscoverStageCache

    deleted = db.query(DiscoverStageCache).filter(DiscoverStageCache.bar_date < cutoff).delete(synchronize_session=False)
    db.commit()
    return int(deleted)


def warm_symbol(
    db: Session, symbol: str, stage_functions: dict[str, Callable[..., Any]],
    *, is_etf: bool = False, asset_class: str = "equity",
) -> int:
    """Compute and store the cacheable stages of one universe stock; returns the stages stored.

    The same calls and arguments as a stock in the pipeline loop (an ETF's ``is_etf`` and
    ``asset_class`` as ``pipeline.candidate_kind`` derives them; a stock's are False and "equity"), so a morning run finds exactly these keys.
    ``stage_functions`` maps a stage name to the pipeline's function; the caller
    passes them in so this module never imports the pipeline.
    """
    stored = 0
    stages: tuple[tuple[str, dict[str, Any]], ...] = (
        ("momentum_quality", {"is_etf": is_etf}),
        ("backtest_vs_benchmark", {"is_etf": is_etf, "asset_class": asset_class}),
        ("verification_gate", {"is_etf": is_etf}),
        ("quant_signals", {"is_etf": is_etf, "asset_class": asset_class}),
    )
    for stage, kwargs in stages:
        fn = stage_functions[stage]
        try:
            cached_stage(db, symbol, stage, fn, db, symbol,
                         variant=stage_variant(is_etf=is_etf, asset_class=kwargs.get("asset_class")), **kwargs)
            stored += 1
        except Exception:  # noqa: BLE001 - one stage failing never stops the others
            db.rollback()
            logger.debug("warm_symbol: %s/%s failed", symbol, stage, exc_info=True)
    cached_alpha = stage_functions.get("alpha_miner_cached")
    if cached_alpha is not None:
        try:
            cached_alpha(db, symbol)
            stored += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.debug("warm_symbol: %s/alpha_miner failed", symbol, exc_info=True)
    return stored


def stage_variant(*, is_etf: bool = False, asset_class: str | None = None) -> str:
    return f"etf={int(is_etf)},class={asset_class or '-'}"
