"""ECB euro foreign exchange reference rates, by date, for tax conversions.

§ 20 Abs. 4 S. 1 Hs. 2 EStG: a gain on a position bought or sold in a foreign
currency is computed with each leg converted at the rate of its own day. The
ECB reference rate (published about 16:00 CET on TARGET days) is the rate
German banks and the BMF use for this; Yahoo's ``EUR<ccy>=X`` bars are market
quotes at an arbitrary time of day, so they are only the fallback.

Rates come from the ECB Data Portal (``EXR.D.<CCY>.EUR.SP00.A``, units of the
currency per euro), one request per currency and calendar year, memoised for
the life of the process. A day without a fixing (weekend, TARGET holiday) uses
the last fixing at most :data:`MAX_CARRY_DAYS` before it.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from threading import Lock

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
MAX_CARRY_DAYS = 5
_TIMEOUT = 15.0

_cache: dict[tuple[str, int], dict[date, float]] = {}
_lock = Lock()


def _fetch_year(ccy: str, year: int) -> dict[date, float]:
    """``{day: units of ccy per EUR}`` for one calendar year; empty on any failure."""
    params = {"startPeriod": f"{year}-01-01", "endPeriod": f"{year}-12-31", "format": "csvdata"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(ECB_URL.format(ccy=ccy), params=params)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - an outage means "no ECB rate", never a crash
        logger.warning("ECB reference rates for %s %s unavailable: %s", ccy, year, exc)
        return {}
    out: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            value = float(row["OBS_VALUE"])
            day = date.fromisoformat(row["TIME_PERIOD"][:10])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            out[day] = value
    return out


def _year(ccy: str, year: int) -> dict[date, float]:
    key = (ccy, year)
    with _lock:
        if key in _cache:
            return _cache[key]
    rates = _fetch_year(ccy, year)
    # A failed fetch is not cached, so the next sync tries again.
    if rates:
        with _lock:
            _cache[key] = rates
    return rates


def ecb_eur_per_unit(currency: str, on: date) -> float | None:
    """EUR per one unit of *currency* at the ECB reference rate of *on* (or the last fixing before it)."""
    ccy = currency.upper()
    if ccy == "EUR":
        return 1.0
    for back in range(MAX_CARRY_DAYS + 1):
        day = on - timedelta(days=back)
        rate = _year(ccy, day.year).get(day)
        if rate:
            return 1.0 / rate
    return None


def eur_per_unit(db: Session, currency: str, on: date) -> tuple[float | None, str]:
    """``(EUR per unit, source)``: the ECB reference rate, else the market rate from stored FX bars."""
    ccy = currency.upper()
    if ccy == "EUR":
        return 1.0, "identity"
    rate = ecb_eur_per_unit(ccy, on)
    if rate is not None:
        return rate, "ecb"
    from app.foundation.eur_prices import MAX_FX_CARRY_DAYS, eur_per_unit_by_date

    days = max(40, (date.today() - on).days + 40)
    rates = eur_per_unit_by_date(db, ccy, days=days) or {}
    for back in range(MAX_FX_CARRY_DAYS + 1):
        value = rates.get((on - timedelta(days=back)).isoformat())
        if value:
            return value, "market"
    return None, "none"


def clear_cache() -> None:
    with _lock:
        _cache.clear()
