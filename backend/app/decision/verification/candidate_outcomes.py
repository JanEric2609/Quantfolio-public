"""Forward returns of every shadow-ledger snapshot (ADR 0018 §5).

For each ``discover_candidate_snapshot`` row and horizon *H* in
:data:`HORIZONS` trading days of the passive core's calendar, one
``candidate_outcome`` row:

* entry: the last EUR close on or before the issue date (at most
  :data:`MAX_ENTRY_GAP_DAYS` old), the same entry as the daily ledger;
* exit: the last EUR close on or before the *H*-th trading day after issue;
* the passive core's return over the same two dates, and the excess.

An outcome is written once the horizon day has a benchmark close and the
stock has a close on or after it. A stock without one
:data:`GRACE_DAYS` calendar days past the horizon is written as ``delisted``
at its last close (never dropped: that is survivorship bias); one without an
entry close then is ``no_price``. Rows are append-only.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.decision.verification.benchmark import passive_core_symbol
from app.decision.verification.ledger_prices import EurSeries, Loader, window_days
from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot

logger = logging.getLogger(__name__)

HORIZONS = (5, 10, 21, 63)
GRACE_DAYS = 30
MAX_ENTRY_GAP_DAYS = 7
#: Snapshots older than this cannot still be waiting (63 trading days + grace).
_LOOKBACK_DAYS = 200
_PAD_DAYS = 15


def _price_on_or_before(prices: EurSeries, symbol: str, day: date, max_gap: int | None) -> tuple[float, date] | None:
    hit = prices.price_on(symbol, day)
    if hit is None:
        return None
    if max_gap is not None and (day - hit[1]).days > max_gap:
        return None
    return hit


def resolve_candidate_outcomes(
    db: Session,
    *,
    user_id: str | None = None,
    today: date | None = None,
    refresh: bool = False,
    loader: Loader | None = None,
    lookback_days: int = _LOOKBACK_DAYS,
) -> int:
    """Write every due outcome; returns rows written. The caller owns the commit.

    *lookback_days* bounds which snapshots are looked at; the weekly job keeps
    the default, a one-off backfill passes a longer one.
    """
    today = today or datetime.now(UTC).date()
    query = db.query(DiscoverCandidateSnapshot).filter(
        DiscoverCandidateSnapshot.issue_date >= today - timedelta(days=lookback_days)
    )
    if user_id is not None:
        query = query.filter(DiscoverCandidateSnapshot.user_id == user_id)
    snapshots = query.all()
    if not snapshots:
        return 0
    done: set[tuple[str, int]] = set()
    ids = [s.id for s in snapshots]
    for chunk in range(0, len(ids), 500):
        rows = (
            db.query(CandidateOutcome.snapshot_id, CandidateOutcome.horizon_days)
            .filter(CandidateOutcome.snapshot_id.in_(ids[chunk : chunk + 500]))
            .all()
        )
        done.update((sid, int(h)) for sid, h in rows)

    benchmark = passive_core_symbol(db)
    earliest = min(s.issue_date for s in snapshots)
    days = (today - earliest).days + MAX_ENTRY_GAP_DAYS + _PAD_DAYS
    prices = EurSeries(db, days=days, refresh=refresh, loader=loader)
    calendar = [d for d in prices.calendar(benchmark) if d <= today]
    if not calendar:
        return 0

    due: list[tuple[DiscoverCandidateSnapshot, int, date]] = []
    for snap in snapshots:
        window = window_days(calendar, snap.issue_date, max(HORIZONS))
        for h in HORIZONS:
            if (snap.id, h) in done or len(window) < h:
                continue
            due.append((snap, h, window[h - 1]))

    written = 0
    for snap, h, target in due:
        symbol = snap.symbol.upper()
        past_grace = (today - target).days > GRACE_DAYS
        last = prices.last_date(symbol)
        entry = _price_on_or_before(prices, symbol, snap.issue_date, MAX_ENTRY_GAP_DAYS)
        status = "resolved"
        if entry is None:
            if not past_grace:
                continue
            exit_ = None
            status = "no_price"
        elif last is not None and last >= target:
            exit_ = _price_on_or_before(prices, symbol, target, None)
        elif past_grace:
            exit_ = _price_on_or_before(prices, symbol, target, None)
            status = "delisted"
        else:
            continue
        if exit_ is not None and entry is not None and exit_[1] <= entry[1] and status == "delisted":
            exit_ = None
            status = "no_price"

        bench_entry = _price_on_or_before(prices, benchmark, snap.issue_date, MAX_ENTRY_GAP_DAYS)
        bench_exit = _price_on_or_before(prices, benchmark, target, MAX_ENTRY_GAP_DAYS)
        bench_ret = bench_exit[0] / bench_entry[0] - 1.0 if bench_entry and bench_exit else None
        ret = exit_[0] / entry[0] - 1.0 if entry is not None and exit_ is not None else None
        db.add(CandidateOutcome(
            snapshot_id=snap.id,
            user_id=snap.user_id,
            run_id=snap.run_id,
            symbol=snap.symbol,
            horizon_days=h,
            target_date=target,
            entry_date=entry[1] if entry else None,
            exit_date=exit_[1] if exit_ else None,
            ret_eur=ret,
            bench_ret_eur=bench_ret,
            excess_eur=ret - bench_ret if ret is not None and bench_ret is not None else None,
            status=status,
            benchmark=benchmark,
        ))
        written += 1
    if written:
        db.flush()
    logger.info("candidate_outcomes: wrote %d of %d due outcomes", written, len(due))
    return written


__all__ = ["GRACE_DAYS", "HORIZONS", "resolve_candidate_outcomes"]
