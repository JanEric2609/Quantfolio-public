"""The calendar-time daily ledger the pre-registered tests bet on (ADR 0018 §3, §5).

One frozen row per series and trading day in ``trust_daily_active_returns``:

* ``ideas`` — Discover picks (``DiscoveryPrediction`` rows without a paper
  sleeve) and ``advisor`` — the advisor's directional calls (rows with a
  sleeve, ``buy``/``sell``; logged holds are not bets). The observation is the
  equal-weight mean daily active return of every open call:
  ``r_s = mean_i sign_i * (r_i,s - r_bench,s)``.
* ``ranking`` — the shadow ledger. Each run with at least
  :data:`MIN_RANKING_STOCKS` evaluable stocks is a cohort, weighted by the
  centred rank of its composite (long leg +1, short leg -1). The observation is
  the mean over open cohorts of ``sum_i w_i * r_i,s``.
* ``selection`` — each live pick against the same run's evaluable non-picks in
  its sector (all non-picks when the sector has fewer than
  :data:`SELECTION_MIN_CONTROLS`; counted as ``n_fallback``):
  ``r_s = mean_picks (r_pick,s - mean_controls r_c,s)``. The benchmark cancels
  (ADR 0018 amendment of 2026-10-14, §2).
* ``mandate_a`` / ``mandate_b`` — each mandate sleeve's daily NAV return (EUR,
  after fees, from the nightly ``paper_snapshots``) minus the passive core's,
  from the sleeve's inception (same amendment, §5).

A call issued on UTC date *d* enters at the close of *d* and is open for the
next *H* trading days of the passive core's calendar. Membership on a day
depends only on calls issued before it. Closes are in EUR and carried forward,
so a pick that stops trading becomes cash (active return ``-r_bench``) and is
never dropped.

Days are written in order, once. A day waits while a member has no close on or
after it, for at most :data:`GRACE_TRADING_DAYS`; after that it is frozen with
the carried price and the member counted in ``n_stale``. A frozen day is never
rewritten (the table rejects UPDATEs on Postgres).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.decision.verification.benchmark import passive_core_symbol
from app.decision.verification.ledger_prices import EurSeries, Loader, daily_return, window_days
from app.foundation.models.entities import (
    DiscoverCandidateSnapshot,
    DiscoveryPrediction,
    TrustDailyActiveReturn,
)

logger = logging.getLogger(__name__)

SERIES_IDEAS = "ideas"
SERIES_ADVISOR = "advisor"
SERIES_RANKING = "ranking"
SERIES_SELECTION = "selection"
SERIES_MANDATE_A = "mandate_a"
SERIES_MANDATE_B = "mandate_b"
SERIES = (SERIES_IDEAS, SERIES_ADVISOR, SERIES_RANKING, SERIES_SELECTION, SERIES_MANDATE_A, SERIES_MANDATE_B)
#: Series key -> ``PaperPortfolio.mandate`` of the sleeve it judges.
MANDATE_SERIES = {SERIES_MANDATE_A: "A", SERIES_MANDATE_B: "B"}
#: A pick's sector needs this many non-picks to be its control; else all non-picks.
SELECTION_MIN_CONTROLS = 3
SELECTION_HORIZON_DAYS = 21

#: A day waits this many trading days for a member's close, then freezes.
GRACE_TRADING_DAYS = 5
#: Runs with fewer evaluable stocks are not ranking cohorts (ADR 0018 §5).
MIN_RANKING_STOCKS = 30
RANKING_HORIZON_DAYS = 21
DEFAULT_HORIZON_DAYS = 21
_PAD_DAYS = 15
_MAX_STALE_LISTED = 10


@dataclass(frozen=True)
class Position:
    """One weighted line of a call or cohort: ``weight * r_symbol`` (+ ``-weight * r_bench``)."""

    symbol: str
    weight: float


@dataclass(frozen=True)
class Member:
    """A call (ideas, advisor) or a cohort (ranking), open for *horizon* trading days after *issue_day*."""

    key: str
    issue_day: date
    horizon: int
    positions: tuple[Position, ...]
    #: Active against the benchmark (calls) or self-financing (ranking cohorts, selection).
    vs_benchmark: bool
    #: A selection pick whose sector had too few controls (all non-picks used).
    fallback: bool = False


def _utc_date(value: datetime) -> date:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).date()


def prediction_members(db: Session, user_id: str, series: str) -> list[Member]:
    """Open-or-past calls of one prediction type as members (each call one position)."""
    advisor = series == SERIES_ADVISOR
    query = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.user_id == user_id)
    query = query.filter(
        DiscoveryPrediction.portfolio_id.isnot(None) if advisor else DiscoveryPrediction.portfolio_id.is_(None)
    )
    members = []
    for row in query.all():
        direction = (row.direction or "buy").lower()
        if direction in ("neutral", "hold"):
            continue
        sign = -1.0 if direction in ("sell", "bear") else 1.0
        members.append(Member(
            key=row.id,
            issue_day=_utc_date(row.predicted_at),
            horizon=int(row.horizon_days or DEFAULT_HORIZON_DAYS),
            positions=(Position(row.symbol.upper(), sign),),
            vs_benchmark=True,
        ))
    return members


def rank_weights(composites: dict[str, float]) -> dict[str, float]:
    """Centred-rank weights: highest composite longest, long leg +1, short leg -1, ties averaged."""
    if len(composites) < 2:
        return {}
    symbols = list(composites)
    ranks = pd.Series([composites[s] for s in symbols]).rank(method="average")
    centre = (len(symbols) + 1) / 2.0
    centred = {s: float(r) - centre for s, r in zip(symbols, ranks)}
    long_total = sum(c for c in centred.values() if c > 0)
    if long_total <= 0:
        return {}
    return {s: c / long_total for s, c in centred.items() if c != 0}


def ranking_members(db: Session, user_id: str) -> list[Member]:
    """One member per live (not back-filled) run with enough evaluable stocks."""
    rows = (
        db.query(DiscoverCandidateSnapshot)
        .filter(
            DiscoverCandidateSnapshot.user_id == user_id,
            DiscoverCandidateSnapshot.backfilled.is_(False),
            DiscoverCandidateSnapshot.evaluable.is_(True),
            DiscoverCandidateSnapshot.instrument_group == "stock",
            DiscoverCandidateSnapshot.composite.isnot(None),
        )
        .all()
    )
    by_run: dict[str, list[DiscoverCandidateSnapshot]] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append(row)
    members = []
    for run_id, group in by_run.items():
        if len(group) < MIN_RANKING_STOCKS:
            continue
        weights = rank_weights({r.symbol.upper(): float(r.composite) for r in group if r.composite is not None})
        if not weights:
            continue
        members.append(Member(
            key=run_id,
            issue_day=min(r.issue_date for r in group),
            horizon=RANKING_HORIZON_DAYS,
            positions=tuple(Position(s, w) for s, w in sorted(weights.items())),
            vs_benchmark=False,
        ))
    return members


def selection_members(db: Session, user_id: str) -> list[Member]:
    """One member per live pick: long the pick, short the equal-weighted controls.

    The frozen rule of ADR 0018's amendment of 2026-10-14 (§2): a pick's
    controls are the same run's evaluable, not-picked stocks in its sector
    (shortlisted and sector-capped ones included); with fewer than
    :data:`SELECTION_MIN_CONTROLS`, all of the run's evaluable non-picks.
    """
    rows = (
        db.query(DiscoverCandidateSnapshot)
        .filter(
            DiscoverCandidateSnapshot.user_id == user_id,
            DiscoverCandidateSnapshot.backfilled.is_(False),
            DiscoverCandidateSnapshot.evaluable.is_(True),
            DiscoverCandidateSnapshot.instrument_group == "stock",
        )
        .all()
    )
    by_run: dict[str, list[DiscoverCandidateSnapshot]] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append(row)
    members = []
    for run_id, group in by_run.items():
        others = [r for r in group if not r.picked]
        for pick in (r for r in group if r.picked):
            sector = (pick.sector or "").strip().lower()
            controls = [c for c in others if sector and (c.sector or "").strip().lower() == sector]
            fallback = len(controls) < SELECTION_MIN_CONTROLS
            if fallback:
                controls = others
            if not controls:
                continue
            w = 1.0 / len(controls)
            members.append(Member(
                key=f"{run_id}:{pick.symbol.upper()}",
                issue_day=pick.issue_date,
                horizon=SELECTION_HORIZON_DAYS,
                positions=(Position(pick.symbol.upper(), 1.0),
                           *(Position(c.symbol.upper(), -w) for c in controls)),
                vs_benchmark=False,
                fallback=fallback,
            ))
    return members


def _frozen_days(db: Session, user_id: str, series: str) -> set[date]:
    rows = (
        db.query(TrustDailyActiveReturn.day)
        .filter(TrustDailyActiveReturn.user_id == user_id, TrustDailyActiveReturn.series == series)
        .all()
    )
    return {d for (d,) in rows}


def _member_value(m: Member, prices: EurSeries, benchmark: str, prev: date, day: date) -> tuple[float, list[str]]:
    bench_r, _ = daily_return(prices, benchmark, prev, day)
    total, stale = 0.0, []
    for p in m.positions:
        r, fresh = daily_return(prices, p.symbol, prev, day)
        if not fresh:
            stale.append(p.symbol)
        total += p.weight * ((r - bench_r) if m.vs_benchmark else r)
    return total, stale


def freeze_series(
    db: Session,
    user_id: str,
    series: str,
    *,
    today: date | None = None,
    refresh: bool = False,
    loader: Loader | None = None,
    members: list[Member] | None = None,
) -> int:
    """Write every day of *series* that is ready and not yet frozen; returns rows written.

    The caller owns the commit.
    """
    today = today or datetime.now(UTC).date()
    if series in MANDATE_SERIES:
        return freeze_mandate_series(db, user_id, series, today=today, refresh=refresh, loader=loader)
    if members is None:
        if series == SERIES_RANKING:
            members = ranking_members(db, user_id)
        elif series == SERIES_SELECTION:
            members = selection_members(db, user_id)
        else:
            members = prediction_members(db, user_id, series)
    if not members:
        return 0
    benchmark = passive_core_symbol(db)
    frozen = _frozen_days(db, user_id, series)
    first_issue = min(m.issue_day for m in members)

    bench_prices = EurSeries(db, days=(today - first_issue).days + _PAD_DAYS, refresh=refresh, loader=loader)
    calendar = [d for d in bench_prices.calendar(benchmark) if d <= today]
    pending_days = [d for d in calendar if d > first_issue and d not in frozen]
    if not pending_days:
        return 0
    first_pending = pending_days[0]

    # Only members whose window reaches an unfrozen day need prices (a window
    # shorter than its horizon is still running past the calendar's end).
    windows = {m.key: window_days(calendar, m.issue_day, m.horizon) for m in members}
    live = [m for m in members if len(windows[m.key]) < m.horizon or windows[m.key][-1] >= first_pending]
    earliest = min((m.issue_day for m in live), default=first_pending)
    prices = EurSeries(db, days=(today - earliest).days + _PAD_DAYS, refresh=refresh, loader=loader)
    prices.seed(benchmark, bench_prices.series(benchmark))

    open_on: dict[date, list[Member]] = {}
    for m in live:
        for d in windows[m.key]:
            open_on.setdefault(d, []).append(m)

    index = {d: i for i, d in enumerate(calendar)}
    written = 0
    for day in pending_days:
        i = index[day]
        if i == 0:
            continue
        prev = calendar[i - 1]
        behind = len(calendar) - 1 - i
        members_today = open_on.get(day, [])
        symbols = {p.symbol for m in members_today for p in m.positions}
        lagging = [s for s in symbols if (prices.last_date(s) or date.min) < day]
        if lagging and behind < GRACE_TRADING_DAYS:
            break  # wait for the closes; frozen days stay a contiguous prefix
        detail: dict[str, Any] = {"prev_day": prev.isoformat()}
        value = None
        stale: set[str] = set()
        if members_today:
            values = []
            for m in members_today:
                v, s = _member_value(m, prices, benchmark, prev, day)
                values.append(v)
                stale.update(s)
            value = sum(values) / len(values)
            detail["n_positions"] = sum(len(m.positions) for m in members_today)
            if series == SERIES_SELECTION:
                detail["n_fallback"] = sum(1 for m in members_today if m.fallback)
            if stale:
                detail["stale"] = sorted(stale)[:_MAX_STALE_LISTED]
        db.add(TrustDailyActiveReturn(
            user_id=user_id,
            series=series,
            day=day,
            n_open=len(members_today),
            n_stale=len(stale),
            value=value,
            benchmark=benchmark,
            detail_json=detail,
        ))
        written += 1
    if written:
        db.flush()
    return written


def _mandate_sleeve(db: Session, user_id: str, series: str) -> Any:
    from app.foundation.models.entities import PaperPortfolio

    return (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == MANDATE_SERIES[series])
        .order_by(PaperPortfolio.created_at)
        .first()
    )


def mandate_starts(db: Session, user_id: str) -> dict[str, date]:
    """Each mandate series' current run start (the sleeve's last reset).

    The ledger keeps a reset sleeve's earlier days under the same key; the
    tests only read the days after this.
    """
    out: dict[str, date] = {}
    for series in MANDATE_SERIES:
        sleeve = _mandate_sleeve(db, user_id, series)
        start_at = (sleeve.inception_at or sleeve.created_at) if sleeve is not None else None
        if start_at is not None:
            out[series] = _utc_date(start_at)
    return out


def freeze_mandate_series(
    db: Session,
    user_id: str,
    series: str,
    *,
    today: date | None = None,
    refresh: bool = False,
    loader: Loader | None = None,
) -> int:
    """Freeze a mandate sleeve's daily active return against the passive core.

    The NAV is the nightly ``paper_snapshots`` row (valued at that day's
    closes, after fees and dividends). A day waits for both snapshots for at
    most :data:`GRACE_TRADING_DAYS`, then freezes without an observation. Days
    before the sleeve's inception (its last reset) are never written.
    """
    from app.foundation.models.entities import PaperSnapshot

    today = today or datetime.now(UTC).date()
    sleeve = _mandate_sleeve(db, user_id, series)
    if sleeve is None:
        return 0
    start_at = sleeve.inception_at or sleeve.created_at
    if start_at is None:
        return 0
    start = _utc_date(start_at)
    navs = {
        s.date: float(s.total_value)
        for s in db.query(PaperSnapshot).filter(PaperSnapshot.portfolio_id == sleeve.id).all()
        if s.total_value is not None and float(s.total_value) > 0
    }
    benchmark = passive_core_symbol(db)
    prices = EurSeries(db, days=(today - start).days + _PAD_DAYS, refresh=refresh, loader=loader)
    calendar = [d for d in prices.calendar(benchmark) if d <= today]
    frozen = _frozen_days(db, user_id, series)
    written = 0
    for i, day in enumerate(calendar):
        if i == 0 or day <= start or day in frozen:
            continue
        prev = max(calendar[i - 1], start)
        behind = len(calendar) - 1 - i
        a, b = navs.get(prev), navs.get(day)
        detail: dict[str, Any] = {"prev_day": prev.isoformat(), "portfolio_id": sleeve.id}
        value = None
        if a is None or b is None:
            if behind < GRACE_TRADING_DAYS:
                break  # the nightly snapshot may still come; frozen days stay a contiguous prefix
            detail["missing"] = "snapshot"
        else:
            bench_r, _ = daily_return(prices, benchmark, prev, day)
            value = b / a - 1.0 - bench_r
            detail["nav"] = round(b, 2)
        db.add(TrustDailyActiveReturn(
            user_id=user_id,
            series=series,
            day=day,
            n_open=1 if value is not None else 0,
            n_stale=0,
            value=value,
            benchmark=benchmark,
            detail_json=detail,
        ))
        written += 1
    if written:
        db.flush()
    return written


def freeze_user_series(
    db: Session,
    user_id: str,
    series_keys: tuple[str, ...],
    *,
    today: date | None = None,
    refresh: bool = False,
    loader: Loader | None = None,
) -> dict[str, int]:
    """Freeze several series for one user, committing after each; a failure skips that series."""
    out: dict[str, int] = {}
    for key in series_keys:
        try:
            out[key] = freeze_series(db, user_id, key, today=today, refresh=refresh, loader=loader)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("daily_ledger: %s series failed for user %s", key, user_id)
            out[key] = -1
    return out


def ledger_users(db: Session) -> list[str]:
    """Users with any prediction or snapshot, i.e. anything to freeze."""
    ids = {u for (u,) in db.query(DiscoveryPrediction.user_id).distinct().all()}
    ids |= {u for (u,) in db.query(DiscoverCandidateSnapshot.user_id).distinct().all()}
    return sorted(i for i in ids if i)


def mandate_users(db: Session) -> list[str]:
    """Users with a mandate sleeve, whether or not they have discovery records."""
    from app.foundation.models.entities import PaperPortfolio

    ids = {u for (u,) in db.query(PaperPortfolio.user_id).filter(PaperPortfolio.mandate.in_(tuple(MANDATE_SERIES.values()))).distinct().all()}
    return sorted(i for i in ids if i)


__all__ = [
    "GRACE_TRADING_DAYS",
    "MANDATE_SERIES",
    "MIN_RANKING_STOCKS",
    "SERIES",
    "SERIES_MANDATE_A",
    "SERIES_MANDATE_B",
    "SERIES_SELECTION",
    "freeze_mandate_series",
    "mandate_starts",
    "mandate_users",
    "freeze_series",
    "freeze_user_series",
    "ledger_users",
    "rank_weights",
    "selection_members",
]
