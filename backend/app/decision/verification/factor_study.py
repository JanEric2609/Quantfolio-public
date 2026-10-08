"""The pre-registered factor study and the gated factor-neutral row (ADR 0018 §8).

**The study** asks how much of the picks' daily active return is explained by
moves the system does not claim as skill: the market, the picks' sectors, the
dollar, and the MSCI World style factors. It runs on the back-filled Discover
baskets only (``discover_candidate_snapshot.backfilled``), never on live
calls, so it cannot look at what the live test judges. Everything it uses is
fixed in :func:`spec` before it first runs:

* **Basket.** Each back-filled run's picked stocks, equal-weight, entered at
  the issue-date close and held for 21 trading days of the passive core's
  calendar; the day's observation is the mean over every open pick of
  ``r_i - r_core`` (the same calendar-time construction as F1, ADR 0018 §3).
* **Factors** (daily, EUR):
  ``market`` = ``r_core``; ``sector`` = the mean over open picks of the
  equal-weight return of the evaluable stocks of the same run and sector
  that were *not* picked, minus ``r_core``; ``usd_eur`` = the daily change of EUR per USD;
  ``value``, ``momentum``, ``quality``, ``min_vol``, ``size`` = the Xetra
  listing of the iShares MSCI World factor ETF minus ``r_core``; ``europe`` =
  iShares STOXX Europe 600 (Xetra) minus ``r_core``, since the core is mostly
  US and Discover also picks European names.
* **Predictable fit.** Each day's exposures are an OLS fit on at most the
  :data:`FIT_WINDOW` days before it, and only once :data:`MIN_FIT_DAYS` exist.
  The out-of-sample R² compares the squared errors of that prediction with the
  errors of the prior mean (Campbell & Thompson 2008).
* **Run once.** The study is recorded the first time it has
  :data:`MIN_OOS_DAYS` out-of-sample days, and that record is binding for its
  spec version: ``build`` at R² >= 0.30, ``drop`` below 0.15, ``neither`` in
  between (not built; a new factor set needs a new spec version and a person
  to decide it). Re-running until the number clears 0.30 would be a look at
  the data.

**The row.** Only after a ``build`` decision, the weekly job freezes
``ideas_neutral``: for each frozen F1 day, ``value = r_s - x_s · beta_s``, with
``beta_s`` fitted on the days before *s* (the study's days, then the live
ones). It is secondary, descriptive and never in F1's e-BH.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.decision.verification.benchmark import passive_core_symbol
from app.decision.verification.daily_ledger import SERIES_IDEAS
from app.decision.verification.daily_tests import PRIMARY_TEST_START
from app.decision.verification.ledger_prices import EurSeries, Loader, daily_return, window_days
from app.foundation import eur_prices
from app.foundation import forecast_verification as fv
from app.foundation.models.entities import (
    DiscoverCandidateSnapshot,
    DiscoveryPrediction,
    TrustDailyActiveReturn,
    TrustFactorStudy,
)

logger = logging.getLogger(__name__)

SPEC_VERSION = 1
#: iShares MSCI World factor ETFs plus STOXX Europe 600, Xetra listings (EUR),
#: fixed before the study runs.
FACTOR_ETFS: dict[str, str] = {
    "value": "IS3S.DE",
    "momentum": "IS3R.DE",
    "quality": "IS3Q.DE",
    "min_vol": "IQQ0.DE",
    "size": "IUSN.DE",
    "europe": "EXSA.DE",
}
FACTORS: tuple[str, ...] = ("market", "sector", "usd_eur", *FACTOR_ETFS)
HORIZON_DAYS = 21
FIT_WINDOW = 252
MIN_FIT_DAYS = 126
MIN_OOS_DAYS = 126
BUILD_R2 = 0.30
DROP_R2 = 0.15
SERIES_IDEAS_NEUTRAL = "ideas_neutral"
_PAD_DAYS = 15

#: ``(db, currency, days) -> {YYYY-MM-DD: EUR per unit}``; tests inject one.
FxLoader = Callable[[Session, str, int], dict[str, float] | None]


def _load_fx(db: Session, currency: str, days: int) -> dict[str, float] | None:
    # Nothing else in the app keeps USD/EUR bars current over the study's span.
    return eur_prices.eur_per_unit_by_date(db, currency, days=days, allow_live=True)


def spec(core: str) -> dict[str, Any]:
    """Everything the study fixes in advance, stored with its result."""
    return {
        "version": SPEC_VERSION,
        "data": "back-filled Discover baskets only (discover_candidate_snapshot.backfilled)",
        "basket": "picked stocks per run, equal-weight, issue-date close entry, 21 trading days",
        "dependent": "daily mean active return r_i - r_core over open picks (EUR)",
        "core": core,
        "factors": {
            "market": "r_core",
            "sector": "evaluable, not-picked stocks of the same run and sector, equal-weight, minus r_core",
            "usd_eur": "daily change of EUR per USD",
            **{k: f"{v} minus r_core" for k, v in FACTOR_ETFS.items()},
        },
        "fit": {"method": "OLS with intercept", "window_days": FIT_WINDOW, "min_fit_days": MIN_FIT_DAYS},
        "oos_r2": "1 - SSE(model) / SSE(prior mean), Campbell & Thompson (2008)",
        "min_oos_days": MIN_OOS_DAYS,
        "gate": {"build": BUILD_R2, "drop": DROP_R2},
    }


@dataclass(frozen=True)
class Pick:
    """One open pick: its symbol, sign, window and same-run, same-sector peers."""

    key: str
    issue_day: date
    symbol: str
    sign: float
    peers: tuple[str, ...]
    horizon: int = HORIZON_DAYS


@dataclass
class PanelDay:
    day: date
    y: float | None
    x: dict[str, float] = field(default_factory=dict)


def _peers_by_run(rows: list[DiscoverCandidateSnapshot]) -> dict[tuple[str, str | None], list[str]]:
    out: dict[tuple[str, str | None], list[str]] = {}
    for r in rows:
        # Picked names are left out: with a few names per sector, a sector
        # average that included the basket would mostly be the basket itself.
        if r.evaluable and r.instrument_group == "stock" and not r.picked:
            out.setdefault((r.run_id, r.sector), []).append(r.symbol.upper())
    return out


def _snapshot_rows(db: Session, user_id: str, *, backfilled: bool) -> list[DiscoverCandidateSnapshot]:
    return (
        db.query(DiscoverCandidateSnapshot)
        .filter(
            DiscoverCandidateSnapshot.user_id == user_id,
            DiscoverCandidateSnapshot.backfilled.is_(backfilled),
        )
        .all()
    )


def study_picks(db: Session, user_id: str) -> list[Pick]:
    """The back-filled baskets: every picked stock of every back-filled run."""
    rows = _snapshot_rows(db, user_id, backfilled=True)
    peers = _peers_by_run(rows)
    picks = []
    for r in rows:
        if not (r.picked and r.instrument_group == "stock"):
            continue
        sym = r.symbol.upper()
        group = tuple(s for s in peers.get((r.run_id, r.sector), []) if s != sym) if r.sector else ()
        picks.append(Pick(key=f"{r.run_id}:{sym}", issue_day=r.issue_date, symbol=sym, sign=1.0, peers=group))
    return picks


def live_picks(db: Session, user_id: str) -> list[Pick]:
    """F1's calls, with peers from the live snapshot of the same issue day."""
    rows = _snapshot_rows(db, user_id, backfilled=False)
    peers = _peers_by_run(rows)
    # Keyed by run: two runs on one day can share a symbol but not a candidate set.
    sector_of = {(r.run_id, r.symbol.upper()): (r.run_id, r.sector) for r in rows}
    picks = []
    query = db.query(DiscoveryPrediction).filter(
        DiscoveryPrediction.user_id == user_id, DiscoveryPrediction.portfolio_id.is_(None)
    )
    for row in query.all():
        direction = (row.direction or "buy").lower()
        if direction in ("neutral", "hold"):
            continue
        issued = row.predicted_at if row.predicted_at.tzinfo else row.predicted_at.replace(tzinfo=UTC)
        day = issued.astimezone(UTC).date()
        sym = row.symbol.upper()
        run_sector = sector_of.get((row.run_id, sym))
        group: tuple[str, ...] = ()
        if run_sector and run_sector[1]:
            group = tuple(s for s in peers.get(run_sector, []) if s != sym)
        picks.append(Pick(
            key=row.id, issue_day=day, symbol=sym, sign=-1.0 if direction in ("sell", "bear") else 1.0,
            peers=group, horizon=int(row.horizon_days or HORIZON_DAYS),
        ))
    return picks


def _fx_levels(raw: dict[str, float] | None) -> dict[date, float]:
    out: dict[date, float] = {}
    for k, v in (raw or {}).items():
        try:
            d, rate = date.fromisoformat(str(k)[:10]), float(v)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            out[d] = rate
    return out


def _closes_on_both(prices: EurSeries, symbol: str, prev: date, day: date) -> bool:
    """A close dated exactly *prev* and one dated exactly *day*: a one-day return."""
    a, b = prices.price_on(symbol, prev), prices.price_on(symbol, day)
    return a is not None and b is not None and a[1] == prev and b[1] == day


def build_panel(
    db: Session,
    picks: list[Pick],
    *,
    today: date | None = None,
    loader: Loader | None = None,
    fx_loader: FxLoader | None = None,
    with_y: bool = True,
    refresh_factors: bool = False,
) -> list[PanelDay]:
    """One row per trading day with an open pick: the basket's active return and the factors.

    A day enters only if every factor ETF and the USD/EUR rate have a value
    dated that day and one dated the trading day before: a missing factor is
    never filled in as zero, nor stretched over two days. *refresh_factors* catches the factor ETFs and the core up from the
    provider first, since nothing else in the app keeps them current.
    """
    today = today or datetime.now(UTC).date()
    if not picks:
        return []
    core = passive_core_symbol(db)
    first = min(p.issue_day for p in picks)
    days = (today - first).days + _PAD_DAYS
    factor_prices = EurSeries(db, days=days, refresh=refresh_factors, loader=loader)
    prices = EurSeries(db, days=days, refresh=False, loader=loader)
    prices.seed(core, factor_prices.series(core))
    calendar = [d for d in factor_prices.calendar(core) if d <= today]
    if len(calendar) < 2:
        return []
    fx = _fx_levels((fx_loader or _load_fx)(db, "USD", days))
    open_on: dict[date, list[Pick]] = {}
    for p in picks:
        for d in window_days(calendar, p.issue_day, p.horizon):
            open_on.setdefault(d, []).append(p)
    index = {d: i for i, d in enumerate(calendar)}

    panel = []
    for day in sorted(open_on):
        i = index[day]
        if i == 0:
            continue
        prev = calendar[i - 1]
        r_core, _ = daily_return(prices, core, prev, day)
        # Every factor must move over exactly this day: a gap the day before
        # would make it a two-day move next to one-day stock returns.
        if prev not in fx or day not in fx:
            continue
        if not all(_closes_on_both(factor_prices, t, prev, day) for t in FACTOR_ETFS.values()):
            continue
        etf = {k: daily_return(factor_prices, t, prev, day) for k, t in FACTOR_ETFS.items()}
        members = open_on[day]
        actives, sectors = [], []
        for p in members:
            if with_y:
                r, _ = daily_return(prices, p.symbol, prev, day)
                actives.append(p.sign * (r - r_core))
            if p.peers:
                peer_r = float(np.mean([daily_return(prices, s, prev, day)[0] for s in p.peers]))
                sectors.append(p.sign * (peer_r - r_core))
            else:
                sectors.append(0.0)
        x = {
            "market": r_core,
            "sector": float(np.mean(sectors)),
            "usd_eur": fx[day] / fx[prev] - 1.0,
            **{k: r - r_core for k, (r, _) in etf.items()},
        }
        panel.append(PanelDay(day=day, y=float(np.mean(actives)) if with_y else None, x=x))
    return panel


def _design(rows: list[PanelDay]) -> np.ndarray:
    return np.array([[1.0, *(r.x[f] for f in FACTORS)] for r in rows], dtype=float)


def _fit(rows: list[PanelDay]) -> np.ndarray:
    """OLS coefficients (intercept first); lstsq copes with a constant factor column."""
    X = _design(rows)
    y = np.array([r.y for r in rows], dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return coef


def oos_r2(panel: list[PanelDay]) -> dict[str, Any]:
    """Predictable rolling fit and its out-of-sample R² against the prior mean."""
    rows = [r for r in panel if r.y is not None]
    ys = np.array([float(r.y) for r in rows if r.y is not None], dtype=float)
    prior_means = np.cumsum(ys) / np.arange(1, len(ys) + 1) if len(ys) else ys
    sse_model = sse_mean = 0.0
    n_oos = 0
    for t in range(MIN_FIT_DAYS, len(rows)):
        coef = _fit(rows[max(0, t - FIT_WINDOW) : t])
        y = float(ys[t])
        pred = float(_design([rows[t]])[0] @ coef)
        prior_mean = float(prior_means[t - 1])
        sse_model += (y - pred) ** 2
        sse_mean += (y - prior_mean) ** 2
        n_oos += 1
    r2 = 1.0 - sse_model / sse_mean if n_oos and sse_mean > 0 else None
    last = _fit(rows[-FIT_WINDOW:]) if len(rows) >= MIN_FIT_DAYS else None
    return {
        "n_days": len(rows),
        "n_oos_days": n_oos,
        "oos_r2": r2,
        "betas": dict(zip(("alpha", *FACTORS), (float(c) for c in last))) if last is not None else None,
    }


def decide(r2: float) -> str:
    if r2 >= BUILD_R2:
        return "build"
    if r2 < DROP_R2:
        return "drop"
    return "neither"


def _study_dict(row: TrustFactorStudy) -> dict[str, Any]:
    return {
        "status": "decided",
        "spec_version": row.spec_version,
        "spec": row.spec_json,
        "decision": row.decision,
        "oos_r2": row.oos_r2,
        "n_days": row.n_days,
        "n_oos_days": row.n_oos_days,
        "betas": (row.detail_json or {}).get("betas"),
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


def recorded_study(db: Session, user_id: str) -> TrustFactorStudy | None:
    return (
        db.query(TrustFactorStudy)
        .filter(TrustFactorStudy.user_id == user_id, TrustFactorStudy.spec_version == SPEC_VERSION)
        .one_or_none()
    )


def run_factor_study(
    db: Session,
    user_id: str,
    *,
    today: date | None = None,
    loader: Loader | None = None,
    fx_loader: FxLoader | None = None,
    refresh_factors: bool = False,
) -> dict[str, Any]:
    """Run the study once; record it the first time it has enough out-of-sample days.

    The caller owns the commit. An existing record is returned unchanged.
    """
    existing = recorded_study(db, user_id)
    if existing is not None:
        return _study_dict(existing)
    panel = build_panel(
        db, study_picks(db, user_id), today=today, loader=loader, fx_loader=fx_loader,
        refresh_factors=refresh_factors,
    )
    fit = oos_r2(panel)
    if fit["oos_r2"] is None or fit["n_oos_days"] < MIN_OOS_DAYS:
        return {
            "status": "waiting",
            "spec_version": SPEC_VERSION,
            "n_days": fit["n_days"],
            "n_days_needed": MIN_FIT_DAYS + MIN_OOS_DAYS,
        }
    row = TrustFactorStudy(
        user_id=user_id,
        spec_version=SPEC_VERSION,
        spec_json=spec(passive_core_symbol(db)),
        decision=decide(float(fit["oos_r2"])),
        oos_r2=float(fit["oos_r2"]),
        n_days=int(fit["n_days"]),
        n_oos_days=int(fit["n_oos_days"]),
        detail_json={
            "betas": fit["betas"],
            "first_day": panel[0].day.isoformat(),
            "last_day": panel[-1].day.isoformat(),
        },
    )
    db.add(row)
    db.flush()
    return _study_dict(row)


def freeze_neutral_series(
    db: Session,
    user_id: str,
    *,
    today: date | None = None,
    loader: Loader | None = None,
    fx_loader: FxLoader | None = None,
    refresh_factors: bool = False,
) -> int:
    """Freeze ``ideas_neutral`` for every frozen F1 day not yet written; only after ``build``.

    The caller owns the commit.
    """
    study = recorded_study(db, user_id)
    if study is None or study.decision != "build":
        return 0
    ideas = {
        r.day: r
        for r in db.query(TrustDailyActiveReturn).filter(
            TrustDailyActiveReturn.user_id == user_id, TrustDailyActiveReturn.series == SERIES_IDEAS
        )
    }
    done = {
        d
        for (d,) in db.query(TrustDailyActiveReturn.day).filter(
            TrustDailyActiveReturn.user_id == user_id, TrustDailyActiveReturn.series == SERIES_IDEAS_NEUTRAL
        )
    }
    observed = sorted(d for d, r in ideas.items() if r.value is not None and r.n_open > 0)
    if not any(d not in done for d in observed):
        return 0
    # Training history: the study's days before the first F1 day, then every
    # F1 day in order (written, unwarmable or not), each with its frozen value.
    history = [
        r
        for r in build_panel(
            db, study_picks(db, user_id), today=today, loader=loader, fx_loader=fx_loader,
            refresh_factors=refresh_factors,
        )
        if r.day < observed[0]
    ]
    live = {
        r.day: r
        for r in build_panel(
            db, live_picks(db, user_id), today=today, loader=loader, fx_loader=fx_loader, with_y=False
        )
    }
    last_live = max(live, default=None)
    written = 0
    for d in observed:
        row = live.get(d)
        if row is None:
            if last_live is not None and d < last_live:
                continue  # later days have every factor: this one never will, so no value
            break  # the factor closes are not in yet: wait for them
        y = float(ideas[d].value)  # type: ignore[arg-type]
        if d not in done and len(history) >= MIN_FIT_DAYS:
            coef = _fit(history[-FIT_WINDOW:])
            factor_part = float(np.dot(coef[1:], [row.x[f] for f in FACTORS]))
            db.add(TrustDailyActiveReturn(
                user_id=user_id,
                series=SERIES_IDEAS_NEUTRAL,
                day=d,
                n_open=ideas[d].n_open,
                n_stale=ideas[d].n_stale,
                value=y - factor_part,
                benchmark=ideas[d].benchmark,
                detail_json={
                    "factor_part": factor_part,
                    "betas": dict(zip(FACTORS, (float(c) for c in coef[1:]))),
                },
            ))
            written += 1
        # Too little history (F1 days older than most back-filled ones) leaves
        # a day without a value; it still trains the later fits.
        history.append(PanelDay(day=d, y=y, x=row.x))
    if written:
        db.flush()
    return written


def factor_neutral_summary(db: Session, user_id: str) -> dict[str, Any]:
    """The study's state and, once built, the secondary row (descriptive only)."""
    study = recorded_study(db, user_id)
    if study is None:
        out: dict[str, Any] = {
            "study": {
                "status": "waiting",
                "spec_version": SPEC_VERSION,
                "n_backfilled_picks": db.query(DiscoverCandidateSnapshot)
                .filter(
                    DiscoverCandidateSnapshot.user_id == user_id,
                    DiscoverCandidateSnapshot.backfilled.is_(True),
                    DiscoverCandidateSnapshot.picked.is_(True),
                    DiscoverCandidateSnapshot.instrument_group == "stock",
                )
                .count(),
            },
            "row": None,
        }
    else:
        out = {"study": _study_dict(study), "row": None}
    out["gate"] = {"build": BUILD_R2, "drop": DROP_R2, "min_oos_days": MIN_OOS_DAYS, "min_fit_days": MIN_FIT_DAYS}
    if study is None or study.decision != "build":
        return out
    rows = (
        db.query(TrustDailyActiveReturn)
        .filter(
            TrustDailyActiveReturn.user_id == user_id,
            TrustDailyActiveReturn.series == SERIES_IDEAS_NEUTRAL,
            TrustDailyActiveReturn.day >= PRIMARY_TEST_START,
        )
        .order_by(TrustDailyActiveReturn.day)
        .all()
    )
    values = [float(r.value) for r in rows if r.value is not None]
    n = len(values)
    ci = fv.mean_ci_hac(values, fv.newey_west_lag(n), 0.9) if values else None
    out["row"] = {
        "n_days": n,
        "first_day": rows[0].day.isoformat() if rows else None,
        "last_day": rows[-1].day.isoformat() if rows else None,
        "mean_21d": float(np.mean(values)) * HORIZON_DAYS if values else None,
        "mean_ci_21d": [ci[0] * HORIZON_DAYS, ci[1] * HORIZON_DAYS] if ci else None,
    }
    return out


__all__ = [
    "BUILD_R2",
    "DROP_R2",
    "FACTOR_ETFS",
    "FACTORS",
    "SERIES_IDEAS_NEUTRAL",
    "SPEC_VERSION",
    "build_panel",
    "decide",
    "factor_neutral_summary",
    "freeze_neutral_series",
    "live_picks",
    "oos_r2",
    "run_factor_study",
    "spec",
    "study_picks",
]
