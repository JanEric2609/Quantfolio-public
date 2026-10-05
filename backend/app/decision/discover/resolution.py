"""Discovery prediction resolution (P1, reworked in Phase 2).

Resolves pending ``DiscoveryPrediction`` rows whose ``resolve_at`` horizon has
passed. The returned list is the input to the scoring pipeline.

How a prediction is measured:

- **Window.** Entry is the last close *before* the prediction day (the close
  the signal was computed on); exit is the first close on or after the horizon
  date. Both come from one freshly loaded price series, so the window is the
  horizon and not "whenever the job happened to run".
- **Total return.** The series is the provider's adjusted close (yfinance
  ``auto_adjust``), and entry and exit are read from the same load, so a
  dividend paid inside the window is counted instead of showing up as a drop.
  ``price_at_prediction`` is only a fallback entry when the series does not
  reach back far enough.
- **EUR.** A non-EUR listing is converted with the ``<CCY>EUR=X`` rate on the
  entry and exit dates. Without a rate the local-currency return is kept and
  the outcome says so.
- **Against the passive core.** The same window is measured on the passive
  core (``passive_core_ticker``, EUNL.DE by default): the alternative to any
  pick is simply buying more of the ETF. ``excess_return`` is the
  direction-signed difference, and a hit means ``excess_return > 0``. Without
  benchmark data the raw return stands in and the outcome says so.

A prediction whose horizon close is not in the data yet stays pending and is
retried the next night, for up to ``RESOLUTION_GRACE_DAYS``. After that it is
marked ``delisted`` with the reason in ``score_json["outcome"]``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.data_backbone.listing_currency import resolve_currency
from app.foundation import market
from app.foundation.models.entities import DiscoveryPrediction
from app.foundation.models.entities._core import now_utc
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK = "EUNL.DE"
# Keep retrying a missing horizon close this many days past resolve_at.
RESOLUTION_GRACE_DAYS = 7
# An entry close may predate the prediction day by a weekend plus holidays.
MAX_ENTRY_GAP_DAYS = 7
# Benchmark and FX closes may trail the stock's date by exchange holidays.
MAX_REFERENCE_GAP_DAYS = 5
_HISTORY_PAD_DAYS = 10

Series = list[tuple[date, float]]
SeriesLoader = Callable[[Session, str, int], Series]


def load_closes(db: Session, symbol: str, days: int) -> Series:
    """Adjusted daily closes for *symbol*, oldest first, as ``(date, close)``.

    Live provider fetches are allowed: this runs in the nightly job, and a
    symbol nobody holds is otherwise never refreshed. ``allow_live`` alone
    does not cover that: :func:`market.history` serves stored bars without a
    fetch whenever any exist, so a pick that left the Discover universe (or
    the FX pair) kept its old series, and a missing horizon close past the
    grace window would have been recorded as ``delisted``. Catch it up first.
    """
    market.refresh_stale_bars(db, symbol, max_age_days=1.5)
    rows = market.history(db, symbol, days=days, allow_live=True)
    out: dict[date, float] = {}
    for row in rows:
        close, day = row.get("close"), row.get("date")
        if close is None or day is None:
            continue
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day[:10])
        if float(close) > 0:
            out[day] = float(close)
    return sorted(out.items())


def _last_on_or_before(series: Series, day: date, max_gap: int) -> tuple[date, float] | None:
    for d, close in reversed(series):
        if d <= day:
            return (d, close) if (day - d).days <= max_gap else None
    return None


def _first_on_or_after(series: Series, day: date) -> tuple[date, float] | None:
    for d, close in series:
        if d >= day:
            return d, close
    return None


def benchmark_symbol(db: Session) -> str:
    """The passive core every prediction is measured against."""
    try:
        value = get_public_settings(db).get("passive_core_ticker")
    except Exception:  # pragma: no cover - settings table unavailable
        value = None
    return str(value or DEFAULT_BENCHMARK).upper()


class _Series:
    """Per-batch cache so each symbol and FX pair is loaded once."""

    def __init__(self, db: Session, loader: SeriesLoader, days: int):
        self._db, self._loader, self._days = db, loader, days
        self._cache: dict[str, Series] = {}

    def get(self, symbol: str) -> Series:
        key = symbol.upper()
        if key not in self._cache:
            try:
                self._cache[key] = self._loader(self._db, key, self._days)
            except Exception as exc:
                logger.warning("resolution: price load failed for %s: %s", key, exc)
                self._cache[key] = []
        return self._cache[key]

    def currency(self, symbol: str) -> str:
        """ISO currency of *symbol*'s prices: the provider-reported one when
        recorded, else the listing suffix (which read IWDA.L as GBP)."""
        return resolve_currency(self._db, symbol)

    def eur_factor(self, currency: str, start: date, end: date) -> float | None:
        """Growth of one unit of *currency* in EUR from *start* to *end*."""
        if currency == "EUR":
            return 1.0
        fx = self.get(f"{currency}EUR=X")
        a = _last_on_or_before(fx, start, MAX_REFERENCE_GAP_DAYS)
        b = _last_on_or_before(fx, end, MAX_REFERENCE_GAP_DAYS)
        if a is None or b is None:
            return None
        return b[1] / a[1]


def resolve_due_predictions(
    db: Session,
    *,
    now: datetime | None = None,
    loader: SeriesLoader | None = None,
) -> list[dict]:
    """Resolve every pending prediction whose horizon has elapsed.

    Rows whose horizon close is not available yet are left pending (see the
    module docstring). Failures for a single row are logged and skipped so the
    batch always continues.

    Args:
        db: Active SQLAlchemy session.
        now: Clock override for tests.
        loader: Price-series loader override for tests; defaults to
            :func:`load_closes`.

    Returns:
        A list of result dicts suitable for ``score_batch``.
    """
    cutoff = now or now_utc()
    pending = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.outcome_status == "pending",
            DiscoveryPrediction.resolve_at <= cutoff,
        )
        .all()
    )
    if not pending:
        logger.info("resolution: no due predictions")
        return []

    earliest = min(_as_date(p.predicted_at) for p in pending)
    days = (cutoff.date() - earliest).days + MAX_ENTRY_GAP_DAYS + _HISTORY_PAD_DAYS
    series = _Series(db, loader or load_closes, days)
    benchmark = benchmark_symbol(db)

    resolved: list[dict] = []
    waiting = 0
    for pred in pending:
        try:
            result = _resolve_one(pred, cutoff, series, benchmark)
        except Exception as exc:  # pragma: no cover - defensive batch safety
            logger.warning(
                "resolution: failed to resolve prediction %s (%s): %s", pred.id, pred.symbol, exc
            )
            continue
        if result is None:
            waiting += 1
            continue
        resolved.append(result)

    if resolved:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.info(
        "resolution: resolved %d of %d due predictions (%d waiting for a horizon close)",
        len(resolved), len(pending), waiting,
    )
    return resolved


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _resolve_one(
    pred: DiscoveryPrediction, cutoff: datetime, series: _Series, benchmark: str
) -> dict | None:
    """Resolve a single prediction and mutate its row; ``None`` means wait."""
    predicted_on = _as_date(pred.predicted_at)
    horizon_on = _as_date(pred.resolve_at)
    resolve_at = pred.resolve_at if pred.resolve_at.tzinfo else pred.resolve_at.replace(tzinfo=cutoff.tzinfo)
    past_grace = cutoff - resolve_at > timedelta(days=RESOLUTION_GRACE_DAYS)

    closes = series.get(pred.symbol)
    exit_ = _first_on_or_after(closes, horizon_on)
    entry = _last_on_or_before(closes, predicted_on - timedelta(days=1), MAX_ENTRY_GAP_DAYS)
    entry_basis = "close"
    if entry is None and pred.price_at_prediction:
        entry = (predicted_on, float(pred.price_at_prediction))
        entry_basis = "stored_price"

    status = "resolved"
    exit_basis = "horizon_close"
    if exit_ is None or entry is None:
        if not past_grace:
            return None
        reason = "no_price_data" if not closes else (
            "no_close_after_horizon" if exit_ is None else "no_entry_price"
        )
        last = _last_after(closes, entry[0]) if entry is not None and exit_ is None else None
        if last is None:
            return _finish(pred, None, None, None, "delisted", {"reason": reason})
        # Delisted (or stopped trading) before the horizon: score it at its last
        # close, as CRSP does with a delisting return, instead of dropping it.
        # Dropping failed picks is survivorship bias; the Trust page counts them.
        exit_, status, exit_basis = last, "delisted", "last_close"

    assert entry is not None and exit_ is not None  # every other path returned above
    entry_on, entry_px = entry
    exit_on, exit_px = exit_
    currency = series.currency(pred.symbol)
    local_return = exit_px / entry_px - 1.0
    fx = series.eur_factor(currency, entry_on, exit_on)
    stock_return = (1.0 + local_return) * fx - 1.0 if fx is not None else local_return

    bench_return = _benchmark_return(series, benchmark, entry_on, exit_on)
    if bench_return is None and not past_grace:
        # The benchmark lags (e.g. not refreshed yet): wait rather than score
        # this one on the raw move while its batch-mates get the excess.
        return None

    # "sell"/"bear" bet on a fall; "buy"/"bull" and a logged hold ("neutral")
    # keep the sign of the move — flipping a hold would score it backwards.
    sign = -1.0 if pred.direction in ("sell", "bear") else 1.0
    realised = sign * stock_return
    excess = sign * (stock_return - bench_return) if bench_return is not None else None
    outcome = {
        "entry_date": entry_on.isoformat(),
        "exit_date": exit_on.isoformat(),
        "entry_price": entry_px,
        "exit_price": exit_px,
        "entry_basis": entry_basis,
        "currency": currency,
        "return_currency": "EUR" if fx is not None else currency,
        "local_return": local_return,
        "benchmark": benchmark,
        "hit_basis": "excess" if excess is not None else "raw",
        "exit_basis": exit_basis,
    }
    if status == "delisted":
        outcome["reason"] = "no_close_after_horizon"
    return _finish(pred, realised, bench_return, excess, status, outcome)


def _last_after(series: Series, after: date) -> tuple[date, float] | None:
    """The last close strictly after *after*, or ``None``."""
    if series and series[-1][0] > after:
        return series[-1]
    return None


def _benchmark_return(series: _Series, benchmark: str, start: date, end: date) -> float | None:
    closes = series.get(benchmark)
    a = _last_on_or_before(closes, start, MAX_REFERENCE_GAP_DAYS)
    b = _last_on_or_before(closes, end, MAX_REFERENCE_GAP_DAYS)
    if a is None or b is None or b[0] <= a[0]:
        return None
    fx = series.eur_factor(series.currency(benchmark), a[0], b[0])
    if fx is None:
        return None
    return (b[1] / a[1]) * fx - 1.0


def _finish(
    pred: DiscoveryPrediction,
    realised: float | None,
    bench_return: float | None,
    excess: float | None,
    status: str,
    outcome: dict[str, Any],
) -> dict:
    pred.realised_return = realised
    pred.benchmark_return = bench_return
    pred.excess_return = excess
    pred.outcome_status = status
    pred.score_json = {**(pred.score_json or {}), "outcome": outcome}
    return {
        "prediction_id": pred.id,
        "user_id": pred.user_id,
        "run_id": pred.run_id,
        # Set only on the advisor's rows: its paper sleeve (see scoring.ic_group).
        "portfolio_id": pred.portfolio_id,
        "symbol": pred.symbol,
        "direction": pred.direction,
        "predicted_on": _as_date(pred.predicted_at).isoformat(),
        "conviction": pred.conviction,
        "conviction_calibrated": pred.conviction_calibrated,
        "realised_return": realised,
        "benchmark_return": bench_return,
        "excess_return": excess,
        "outcome_status": status,
    }
