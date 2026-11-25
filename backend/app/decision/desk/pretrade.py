"""Pre-trade context for the manual CFD message: trend, range, volatility regime, stretch, events, live news.

Display only (never used in scoring or sizing). All indicators are computed from the daily bars Quantfolio already
stores (``foundation/technical.py``), up to yesterday's close; nothing is fetched from a new source. Every line says
what it means in plain words, because the owner is still learning the indicators. A missing input drops its line.

* Trend: the 9-day average against the 26-day average (the owner's chart uses the same pair on 5-minute bars) and
  the price against the 50-day average.
* Range: ATR(14), the average daily range in index points; the rule's stop is shown in multiples of it.
* Volatility regime: the last 20 days' realised volatility against the last 250 days (the 2016-26 replay came near
  break-even only on high-volatility days).
* Stretch: the close against the daily Bollinger band (20, 2).
* Events: macro days from the desk calendar, and the latest rated headline of the last hour that touches the index
  or the central banks.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.foundation import technical
from app.foundation.models.entities import NewsWireItem

from . import calendar as cal

logger = logging.getLogger(__name__)

HIGH_VOL_PCTILE = 2 / 3
LOW_VOL_PCTILE = 1 / 3
NEWS_WINDOW = timedelta(minutes=60)


def _realised(closes: list[float], n: int = 20) -> list[float]:
    """Rolling n-day volatility of daily log returns, one value per day from day n."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
    return [statistics.pstdev(rets[i - n:i]) for i in range(n, len(rets) + 1)]


def snapshot(bars: list[technical.Bar]) -> dict[str, Any] | None:
    """The indicator state at the last bar's close. ``None`` when there are fewer than 60 bars."""
    if len(bars) < 60:
        return None
    closes = [b.close for b in bars]
    last = closes[-1]
    e9, e26 = technical.ema_series(closes, 9), technical.ema_series(closes, 26)
    sma50 = technical.sma(closes, 50)
    a = technical.atr(bars, 14)
    if not e9 or not e26 or sma50 is None or not a:
        return None
    fast_above = e9[-1] > e26[-1]
    price_above = last > sma50
    trend = "up" if fast_above and price_above else "down" if not fast_above and not price_above else "mixed"
    vols = _realised(closes[-251:])
    pct = sum(1 for v in vols if v <= vols[-1]) / len(vols) if vols else None
    regime = None if pct is None else "high" if pct >= HIGH_VOL_PCTILE else "low" if pct <= LOW_VOL_PCTILE else "normal"
    band = technical.bollinger(closes, 20, 2.0)
    stretch = None
    if band:
        lower, _mid, upper = band
        stretch = "above the upper band" if last > upper else "below the lower band" if last < lower else "inside the bands"
    return {"last": last, "trend": trend, "atr_points": a, "atr_pct": a / last, "vol_regime": regime,
            "vol_percentile": pct, "stretch": stretch, "rsi14": technical.rsi(closes, 14)}


def events(today: date) -> list[str]:
    """Macro days today or tomorrow from the desk calendar, as 'CPI today'."""
    out = []
    for kind, d in cal.macro_events_between(today, today + timedelta(days=1)):
        out.append(f"{kind} {'today' if d == today.isoformat() else 'tomorrow'}")
    return out


def latest_news(db: Session, key: str, now: datetime) -> tuple[str, int] | None:
    """(headline, minutes ago) of the newest rated headline in the last hour for this index or a central bank."""
    try:
        rows = (db.query(NewsWireItem).filter(NewsWireItem.received_at >= now - NEWS_WINDOW)
                .order_by(NewsWireItem.received_at.desc()).limit(200).all())
    except SQLAlchemyError as exc:
        db.rollback()
        logger.info("desk pretrade news read failed: %s", type(exc).__name__)
        return None
    for r in rows:
        touches = r.relevance in (f"desk:{key}", "central_bank") or (r.scope == "market" and (r.materiality or 0) >= 2)
        if touches and (r.materiality is None or r.materiality >= 1):
            at = r.received_at if r.received_at.tzinfo else r.received_at.replace(tzinfo=now.tzinfo)
            return r.headline, max(0, int((now - at).total_seconds() // 60))
    return None


def lines(snap: dict[str, Any] | None, direction: str, stop_points: float, evts: list[str],
          news: tuple[str, int] | None) -> list[str]:
    """The context lines in plain words; empty when nothing is known."""
    out: list[str] = []
    if snap:
        word = {"up": "UP (the 9-day average is above the 26-day one and the price above its 50-day average)",
                "down": "DOWN (9-day below 26-day, price below its 50-day average)",
                "mixed": "MIXED (the averages disagree, so no clear trend)"}[snap["trend"]]
        agrees = (snap["trend"] == "up") == (direction == "long")
        verdict = "" if snap["trend"] == "mixed" else (" This trade goes with it." if agrees else " This trade goes against it.")
        out.append(f"Trend: {word}.{verdict}")
        atr = snap["atr_points"]
        out.append(f"A normal day moves about {atr:,.0f} pts (ATR); the stop is {stop_points / atr:.1f} times that.")
        bits = []
        if snap["vol_regime"]:
            bits.append(f"volatility is {snap['vol_regime']} (20-day vs the past year)")
        if snap["stretch"] and snap["stretch"] != "inside the bands":
            bits.append(f"price is {snap['stretch']} (stretched)")
        if bits:
            out.append("Now: " + "; ".join(bits) + ".")
    if evts:
        out.append("Event: " + ", ".join(evts) + " (prices can jump).")
    if news:
        head = news[0] if len(news[0]) <= 110 else news[0][:107] + "..."
        out.append(f"News {news[1]} min ago: {head}")
    return out


def context_lines(db: Session, key: str, ticker: str, direction: str, stop_points: float, now: datetime) -> list[str]:
    """Everything above for one signal; a failed read only removes its line."""
    snap = None
    try:
        from app.foundation.market import history

        snap = snapshot([b for b in technical.bars_from_rows(history(db, ticker, days=420))
                         if b.date < now.astimezone(cal.BERLIN).date()])
    except SQLAlchemyError as exc:
        db.rollback()
        logger.info("desk pretrade bars read failed: %s", type(exc).__name__)
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as exc:
        logger.info("desk pretrade bars read failed: %s", type(exc).__name__)
    return lines(snap, direction, stop_points, events(now.astimezone(cal.BERLIN).date()), latest_news(db, key, now))
