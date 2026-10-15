"""The plan's one-year bands: stored each month before the year starts, scored once it has passed (plan D6).

Each month, on the first evening the daily job runs:

1. **Two bands per user with a book**, for the book's real return over the next
   365 days, at 10, 50 and 90 %:
   - ``planner``: the projection's own model (``wealth_planner.return_band``):
     the published real return, the book's volatility, Student-t shocks and
     drift uncertainty;
   - ``long_history``: the JST bootstrap's one-year quantiles for the book's
     mix (``long_history.plan_fan.one_year_band``), when the data file is
     installed.
   The value band adds the planned contributions to the book's value.
2. **Scores.** Once a band's year has passed: the book's time-weighted return
   (``book_performance``, so deposits do not count as growth), less euro-area
   HICP inflation for the end month. Inside the band or not.

One band a month for a one-year horizon means twelve overlapping years: they
are not independent. The coverage interval uses only bands whose years do not
overlap, so it says little until several years have passed.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import PlanBandForecast, PlanBandScore, User

logger = logging.getLogger(__name__)

MODELS = ("planner", "long_history")
LABELS = {
    "planner": "The projection's model (published return, your book's volatility)",
    "long_history": "History: 8 euro-area countries since 1870, your book's mix",
}
PLANNER_VERSION = "planner-t5-drift-v1"
HORIZON_DAYS = 365
#: Days a band's start or end may lie from the nearest unit-value day.
MAX_GAP_DAYS = 7
#: After this many days past its end, a band that still cannot be scored is left unscored.
GIVE_UP_DAYS = 75
#: At most this many days without every price (held flat) in a scored year, about 2 %.
MAX_FLAT_DAYS = 5
COVERAGE_LEVEL = 0.8
HICP = "HICP_EA"


def _value(start: float, monthly: float, r: float) -> float:
    """The book's value in a year: the start grows by *r*; the year's contributions earn about half of it."""
    return start * (1.0 + r) + 12.0 * monthly * (1.0 + r) ** 0.5


def book_mix(positions: list[Any]) -> Any:
    """The book's shares of equity, bonds and money-market funds, from each position's fund class."""
    from app.foundation.etf_classes import BOND_CLASSES, classify_etf
    from app.lab.long_history.plan_fan import Mix

    shares = {"equity": 0.0, "bond": 0.0, "cash": 0.0}
    for p in positions:
        value = float(p.value or 0)
        if value <= 0:
            continue
        cls = classify_etf(p.name or "", isin=p.isin)
        key = "bond" if cls in BOND_CLASSES else "cash" if cls == "money_market" else "equity"
        shares[key] += value
    total = sum(shares.values())
    if total <= 0:
        return Mix()
    return Mix(shares["equity"] / total, shares["bond"] / total, shares["cash"] / total)


def publish_due(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """Store this month's bands for every user with a book, unless they exist. Commits."""
    from app.foundation.live_positions import combined_positions
    from app.foundation.settings import get_public_settings
    from app.foundation.wealth_planner import (
        DEFAULT_CALIBRATION_YEARS,
        DEFAULT_CMA_REAL_RETURN,
        DEFAULT_NU,
        book_volatility,
        return_band,
    )
    from app.lab.long_history.plan_fan import MODEL_VERSION as JST_VERSION
    from app.lab.long_history.plan_fan import SOURCE, load_histories, one_year_band

    today = today or datetime.now(UTC).date()
    month_start = today.replace(day=1)
    settings = get_public_settings(db)
    monthly = float(settings.get("monthly_contribution_eur") or 0)
    real_return = float(settings.get("mc_cma_real_return", DEFAULT_CMA_REAL_RETURN))
    history = load_histories()
    written = 0
    skipped: list[str] = []
    for (user_id,) in db.query(User.id).all():
        positions = combined_positions(db, user_id, include_unreconciled=True)
        start = sum(float(p.value or 0) for p in positions if float(p.value or 0) > 0)
        if start <= 0:
            continue
        have = {
            m for (m,) in db.query(PlanBandForecast.model).filter(
                PlanBandForecast.user_id == user_id, PlanBandForecast.forecast_day >= month_start,
            )
        }
        bands: list[tuple[str, str, dict[int, float], dict[str, Any]]] = []
        if "planner" not in have:
            vol, vol_source = book_volatility(db, user_id)
            bands.append(("planner", PLANNER_VERSION, return_band(real_return, vol), {
                "real_return": real_return, "volatility": vol, "volatility_source": vol_source,
                "nu": DEFAULT_NU, "calibration_years": DEFAULT_CALIBRATION_YEARS,
            }))
        if "long_history" not in have:
            if history is None:
                skipped.append("long_history: the JST file is not installed")
            else:
                mix = book_mix(positions)
                w = mix.weights()
                bands.append(("long_history", JST_VERSION, one_year_band(history[0], mix), {
                    "mix": {"equity": float(w[0]), "bond": float(w[1]), "cash": float(w[2])},
                    "homes": [h.country for h in history[0]], "source": SOURCE,
                }))
        for model, version, band, inputs in bands:
            db.add(PlanBandForecast(
                user_id=user_id, model=model, model_version=version, forecast_day=today,
                end_day=today + timedelta(days=HORIZON_DAYS), p10=band[10], p50=band[50], p90=band[90],
                start_value=start, monthly_contribution=monthly,
                value_p10=_value(start, monthly, band[10]), value_p50=_value(start, monthly, band[50]),
                value_p90=_value(start, monthly, band[90]), inputs_json=inputs,
            ))
            written += 1
    db.commit()
    return {"bands": written, "skipped": skipped}


def _inflation(db: Session, end: date) -> float | None:
    """Euro-area HICP for *end*'s month, % on a year earlier, as a fraction; None until it is published."""
    from app.foundation.models.entities import MacroIndicator

    first = end.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    row = (
        db.query(MacroIndicator.value)
        .filter(MacroIndicator.name == HICP, MacroIndicator.source == "ecb",
                MacroIndicator.date >= first, MacroIndicator.date <= last)
        .order_by(MacroIndicator.date.desc())
        .first()
    )
    return float(row[0]) / 100.0 if row else None


def _unit_values(db: Session, user_id: str, start: date) -> tuple[list[tuple[date, float]], str | None]:
    """The book's unit values from *start*, and why they cannot be scored yet (None when they can).

    A holding with no price at all is left out of the unit value, and a day
    without every price holds it flat; a score is final, so it waits for both
    to clear (up to :data:`MAX_FLAT_DAYS` flat days are accepted).
    """
    from app.foundation.book_performance import compute_book_performance

    perf = compute_book_performance(db, user_id, start=start)
    series = [(date.fromisoformat(p["date"]), float(p["unit_value"])) for p in perf.get("series") or []
              if p.get("unit_value")]
    if perf.get("unpriced"):
        return series, f"no price for {', '.join(perf['unpriced'])}"
    if int(perf.get("days_without_full_prices") or 0) > MAX_FLAT_DAYS:
        return series, f"{perf['days_without_full_prices']} days without every price"
    return series, None


def score_due(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """Score every band whose year has passed, when the book's return and the inflation exist. Commits."""
    today = today or datetime.now(UTC).date()
    pending = (
        db.query(PlanBandForecast)
        .outerjoin(PlanBandScore, PlanBandScore.forecast_id == PlanBandForecast.id)
        .filter(PlanBandScore.id.is_(None), PlanBandForecast.end_day <= today,
                PlanBandForecast.end_day >= today - timedelta(days=GIVE_UP_DAYS))
        .all()
    )
    groups: dict[tuple[str, date], list[PlanBandForecast]] = defaultdict(list)
    for f in pending:
        groups[(f.user_id, f.forecast_day)].append(f)
    scored = 0
    waiting: list[str] = []
    for (user_id, day), rows in groups.items():
        end = rows[0].end_day
        inflation = _inflation(db, end)
        if inflation is None:
            waiting.append(f"{day}: HICP for {end:%Y-%m} not stored yet")
            continue
        series, incomplete = _unit_values(db, user_id, day)
        if incomplete:
            waiting.append(f"{day}: {incomplete}")
            continue
        first = next(((d, u) for d, u in series if d >= day), None)
        last = next(((d, u) for d, u in reversed(series) if d <= end), None)
        if (first is None or last is None or (first[0] - day).days > MAX_GAP_DAYS
                or (end - last[0]).days > MAX_GAP_DAYS or first[1] <= 0):
            waiting.append(f"{day}: no unit value near both ends of the year")
            continue
        nominal = last[1] / first[1] - 1.0
        real = (1.0 + nominal) / (1.0 + inflation) - 1.0
        for f in rows:
            side = -1 if real < f.p10 else 1 if real > f.p90 else 0
            db.add(PlanBandScore(forecast_id=f.id, start_day=first[0], end_day=last[0], nominal_return=nominal,
                                 inflation=inflation, real_return=real, inside=side == 0, side=side))
            scored += 1
    db.commit()
    return {"scored": scored, "waiting": waiting}


def run_plan_bands(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """The daily entry point: publish this month's bands if due, then score what is due."""
    return {"publish": publish_due(db, today=today), "score": score_due(db, today=today)}


def summary(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Per model: the latest band, the scores so far and the coverage over years that do not overlap."""
    from app.foundation.forecast_verification import coverage

    rows = (
        db.query(PlanBandForecast, PlanBandScore)
        .outerjoin(PlanBandScore, PlanBandScore.forecast_id == PlanBandForecast.id)
        .filter(PlanBandForecast.user_id == user_id)
        .order_by(PlanBandForecast.forecast_day)
        .all()
    )
    out = []
    for model in MODELS:
        mine = [(f, s) for f, s in rows if f.model == model]
        if not mine:
            continue
        latest = mine[-1][0]
        scored = [(f, s) for f, s in mine if s is not None]
        independent: list[tuple[PlanBandForecast, PlanBandScore]] = []
        for f, s in scored:
            if not independent or f.forecast_day >= independent[-1][0].end_day:
                independent.append((f, s))
        cov = coverage([s.inside for _, s in independent], COVERAGE_LEVEL) if independent else None
        unscored = [f.end_day for f, s in mine if s is None]
        out.append({
            "model": model,
            "label": LABELS[model],
            "latest": {
                "forecast_day": latest.forecast_day.isoformat(), "end_day": latest.end_day.isoformat(),
                "p10": latest.p10, "p50": latest.p50, "p90": latest.p90,
                "start_value": latest.start_value, "monthly_contribution": latest.monthly_contribution,
                "value_p10": latest.value_p10, "value_p50": latest.value_p50, "value_p90": latest.value_p90,
            },
            "n_bands": len(mine),
            "n_scored": len(scored),
            "inside": sum(1 for _, s in scored if s.inside),
            "below": sum(1 for _, s in scored if s.side < 0),
            "above": sum(1 for _, s in scored if s.side > 0),
            "n_independent": len(independent),
            "coverage_low": cov.ci_low if cov else None,
            "coverage_high": cov.ci_high if cov else None,
            "first_check": min(unscored).isoformat() if unscored else None,
        })
    return out
