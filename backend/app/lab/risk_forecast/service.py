"""Daily risk forecasts for every paper sleeve, the real book and the benchmark, and their scores (plan D3).

Each trading day after the close:

1. **Targets.** Every paper sleeve (holdings and cash), each user's real book
   (synced positions and cash) and the benchmark. Their weights are frozen
   that day.
2. **Forecasts.** The frozen-weight portfolio's daily EUR returns over about
   3 years go to :func:`models.forecast` for 1 and 21 trading days. Rows are
   append-only, one per target, day and horizon.
3. **Scores.** Every forecast whose horizon has passed gets a score: the
   frozen-weight return over the horizon and the VaR and ES e-values.

Prices come from stored bars only (``allow_live=False``): the daily backfill
keeps held symbols current, and a job must not hang on a provider. A symbol
without history is left out of the returns and its weight reported as
``unpriced``. Its day returns count as 0, which understates risk, so the
summary says so.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    PaperHolding,
    PaperPortfolio,
    RiskForecastEntry,
    RiskForecastScore,
    User,
)
from app.lab.risk_forecast.backtest import e_process, es_e, var_e
from app.lab.risk_forecast.models import MODEL_VERSION, forecast

logger = logging.getLogger(__name__)

HORIZONS = (1, 21)
#: Calendar days of closes read per symbol: about 3 years of trading days plus room for the scores.
HISTORY_DAYS = 1150


@dataclass
class Target:
    key: str
    label: str
    user_id: str | None
    #: symbol -> share of the target's value; "cash" earns 0.
    weights: dict[str, float]
    unpriced: float = 0.0
    notes: list[str] = field(default_factory=list)


class _Closes:
    """EUR closes per symbol, read once per run."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict[str, dict[str, float]] = {}

    def get(self, symbol: str) -> dict[str, float]:
        if symbol not in self._cache:
            from app.foundation.eur_prices import eur_closes

            self._cache[symbol] = eur_closes(self.db, symbol, days=HISTORY_DAYS, allow_live=False)
        return self._cache[symbol]

    def last(self, symbol: str) -> float | None:
        series = self.get(symbol)
        return series[max(series)] if series else None


def _normalise(values: dict[str, float]) -> dict[str, float]:
    total = sum(v for v in values.values() if v > 0)
    return {k: v / total for k, v in values.items() if v > 0} if total > 0 else {}


def paper_targets(db: Session, closes: _Closes) -> list[Target]:
    from app.foundation.paper_cash import paper_cash_balance

    out = []
    for p in db.query(PaperPortfolio).all():
        values: dict[str, float] = {}
        unpriced = 0.0
        for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == p.id).all():
            qty = float(h.quantity or 0)
            if qty <= 0 or not h.ticker:
                continue
            price = closes.last(h.ticker)
            if price is None:
                unpriced += qty * float(h.avg_buy_price or 0)
                continue
            values[h.ticker.upper()] = values.get(h.ticker.upper(), 0.0) + qty * price
        cash = float(paper_cash_balance(p, db))
        if cash > 0:
            values["cash"] = cash
        total = sum(values.values()) + unpriced
        weights = _normalise(values)
        if weights:
            out.append(Target(f"paper:{p.id}", p.name, p.user_id, weights,
                              unpriced=unpriced / total if total > 0 else 0.0))
    return out


def book_targets(db: Session, closes: _Closes) -> list[Target]:
    from app.foundation.live_positions import combined_positions, synced_cash

    out = []
    for (user_id,) in db.query(User.id).all():
        positions = combined_positions(db, user_id, include_unreconciled=True)
        if not positions:
            continue
        values: dict[str, float] = {}
        unpriced = 0.0
        for pos in positions:
            value = float(pos.value or 0)
            if value <= 0:
                continue
            if not pos.ticker or not closes.get(pos.ticker):
                unpriced += value
                continue
            values[pos.ticker.upper()] = values.get(pos.ticker.upper(), 0.0) + value
        cash = synced_cash(db, user_id)[0]
        if float(cash) > 0:
            values["cash"] = float(cash)
        total = sum(values.values()) + unpriced
        weights = _normalise(values)
        if weights:
            out.append(Target(f"book:{user_id}", "Real book", user_id, weights,
                              unpriced=unpriced / total if total > 0 else 0.0))
    return out


def benchmark_target(db: Session) -> Target:
    from app.foundation.eur_prices import benchmark_ticker

    symbol = benchmark_ticker(db)
    return Target(f"benchmark:{symbol}", f"Benchmark ({symbol})", None, {symbol: 1.0})


def portfolio_returns(weights: dict[str, float], closes: _Closes, calendar: list[str]) -> np.ndarray:
    """Daily returns of the frozen weights on *calendar* (a missing day's return is 0)."""
    out = np.zeros(len(calendar) - 1)
    for symbol, w in weights.items():
        if symbol == "cash":
            continue
        series = closes.get(symbol)
        prev: float | None = None
        for i, day in enumerate(calendar):
            close = series.get(day)
            if close is not None and prev is not None and i > 0 and prev > 0:
                out[i - 1] += w * (close / prev - 1.0)
            if close is not None:
                prev = close
    return out


def _calendar(db: Session, closes: _Closes, today: date) -> list[str]:
    from app.foundation.eur_prices import benchmark_ticker

    return sorted(d for d in closes.get(benchmark_ticker(db)) if d <= today.isoformat())


def run_forecasts(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """Forecast every target as of the last trading day up to *today*, then score what is due. Commits."""
    today = today or datetime.now(UTC).date()
    closes = _Closes(db)
    calendar = _calendar(db, closes, today)
    if len(calendar) < 2:
        return {"forecasts": 0, "scored": 0, "reason": "no benchmark closes"}
    day = date.fromisoformat(calendar[-1])
    targets = [benchmark_target(db), *paper_targets(db, closes), *book_targets(db, closes)]
    written = 0
    skipped: list[str] = []
    for t in targets:
        r = portfolio_returns(t.weights, closes, calendar)
        for h in HORIZONS:
            exists = db.query(RiskForecastEntry.id).filter(
                RiskForecastEntry.target == t.key, RiskForecastEntry.forecast_day == day,
                RiskForecastEntry.horizon_days == h,
            ).first()
            if exists:
                continue
            f = forecast(r, h)
            if f is None:
                skipped.append(f"{t.key}:h{h}")
                continue
            weights = dict(t.weights)
            if t.unpriced:
                weights["unpriced"] = round(t.unpriced, 6)
            db.add(RiskForecastEntry(
                user_id=t.user_id, target=t.key, label=t.label[:160], forecast_day=day, horizon_days=h,
                model_version=MODEL_VERSION, model_hash=f.model_hash, sigma=f.sigma, var95=f.var95,
                var975=f.var975, es975=f.es975, n_obs=f.n_obs, n_calibration=f.n_calibration,
                weights_json=weights,
            ))
            written += 1
    db.flush()
    scored = score_due(db, closes, calendar)
    db.commit()
    return {"day": day.isoformat(), "targets": len(targets), "forecasts": written, "scored": scored,
            "skipped": skipped}


def score_due(db: Session, closes: _Closes, calendar: list[str]) -> int:
    """Score every unscored forecast whose horizon lies inside *calendar*. Does not commit."""
    index = {d: i for i, d in enumerate(calendar)}
    pending = (
        db.query(RiskForecastEntry)
        .outerjoin(RiskForecastScore, RiskForecastScore.forecast_id == RiskForecastEntry.id)
        .filter(RiskForecastScore.id.is_(None))
        .all()
    )
    n = 0
    for f in pending:
        i = index.get(f.forecast_day.isoformat())
        if i is None or i + f.horizon_days >= len(calendar):
            continue
        window = calendar[i:i + f.horizon_days + 1]
        weights = {k: float(v) for k, v in (f.weights_json or {}).items() if k != "unpriced"}
        daily = portfolio_returns(weights, closes, window)
        realized = float(np.prod(1.0 + daily) - 1.0)
        loss = -realized
        db.add(RiskForecastScore(
            forecast_id=f.id, end_day=date.fromisoformat(window[-1]), realized_return=realized, loss=loss,
            var95_breach=loss > f.var95, var_e=var_e(loss, f.var95, 0.95), es_e=es_e(loss, f.var975, f.es975, 0.975),
        ))
        n += 1
    return n


def non_overlapping(scored: list[tuple[RiskForecastEntry, RiskForecastScore]]
                    ) -> list[tuple[RiskForecastEntry, RiskForecastScore]]:
    """Scored forecasts (by forecast day) whose windows do not overlap, greedily from the first."""
    kept: list[tuple[RiskForecastEntry, RiskForecastScore]] = []
    for f, s in scored:
        if not kept or f.forecast_day >= kept[-1][1].end_day:
            kept.append((f, s))
    return kept


def summary(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Per target of *user_id* (and the benchmark): the latest forecasts and how the scored ones did."""
    from app.foundation.forecast_verification import coverage

    rows = (
        db.query(RiskForecastEntry, RiskForecastScore)
        .outerjoin(RiskForecastScore, RiskForecastScore.forecast_id == RiskForecastEntry.id)
        .filter((RiskForecastEntry.user_id == user_id) | (RiskForecastEntry.user_id.is_(None)))
        .order_by(RiskForecastEntry.target, RiskForecastEntry.horizon_days, RiskForecastEntry.forecast_day)
        .all()
    )
    by_target: dict[str, dict[str, Any]] = {}
    for f, s in rows:
        t = by_target.setdefault(f.target, {"target": f.target, "label": f.label, "horizons": {}})
        hz = t["horizons"].setdefault(f.horizon_days, {"latest": None, "scored": []})
        hz["latest"] = f
        if s is not None:
            hz["scored"].append((f, s))
    out = []
    for t in by_target.values():
        horizons = []
        for h, hz in sorted(t["horizons"].items()):
            latest: RiskForecastEntry = hz["latest"]
            scored = hz["scored"]
            # Overlapping multi-day windows are dependent: the e-process uses only windows that start
            # on or after the previous kept one ends.
            used = non_overlapping(scored)
            cov = coverage([not sc.var95_breach for _, sc in used], 0.95) if used else None
            e_var = e_process(sc.var_e for _, sc in used)
            e_es = e_process(sc.es_e for _, sc in used)
            horizons.append({
                "horizon_days": h,
                "forecast_day": latest.forecast_day.isoformat(),
                "sigma": latest.sigma, "var95": latest.var95, "es975": latest.es975,
                "unpriced_share": float((latest.weights_json or {}).get("unpriced", 0.0)),
                "n_scored": len(used),
                "breaches": sum(1 for _, sc in used if sc.var95_breach),
                "coverage_low": cov.ci_low if cov and cov.ci_low is not None else None,
                "coverage_high": cov.ci_high if cov and cov.ci_high is not None else None,
                "e_var": e_var[-1] if e_var else 1.0,
                "e_es": e_es[-1] if e_es else 1.0,
            })
        out.append({"target": t["target"], "label": t["label"], "horizons": horizons})
    return out
