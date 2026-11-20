"""Euro-area macro series and the dossier's macro fact block (plan 2026-10-12, D11 / WP8).

Stored in ``macro_indicators`` with ``source="ecb"``:

=================  ===================================  ===========
name               ECB Data Portal key                  unit
=================  ===================================  ===========
``ECB_DFR``        ``FM.D.U2.EUR.4F.KR.DFR.LEV``        % p.a.
``ECB_MRO``        ``FM.D.U2.EUR.4F.KR.MRR_FR.LEV``     % p.a.
``HICP_EA``        ``HICP.M.U2.N.000000.4D0.ANR``       % y/y
``HICP_EA_CORE``   ``HICP.M.U2.N.XEF000.4D0.ANR``       % y/y
=================  ===================================  ===========

€STR is stored by :mod:`app.foundation.estr`. All keys were checked against
the live API on 2026-10-12. The ECB discontinued its ``ICP`` dataset on
2026-02-04 when HICP moved to ECOICOP 2 (``ICP.M.U2.N.000000.4.ANR`` ends at
2025-12); the ``HICP`` dataset replaces it, with a data-provider dimension
(``4D0``) added to the key.

The Fed's target range comes from FRED (``DFEDTARU``, the upper limit;
``source="fred"``), fetched by the macro refresh job.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ECB_SERIES: dict[str, str] = {
    "ECB_DFR": "FM.D.U2.EUR.4F.KR.DFR.LEV",
    "ECB_MRO": "FM.D.U2.EUR.4F.KR.MRR_FR.LEV",
    "HICP_EA": "HICP.M.U2.N.000000.4D0.ANR",
    "HICP_EA_CORE": "HICP.M.U2.N.XEF000.4D0.ANR",
}
ECB_SOURCE = "ecb"
#: FRED series for the Fed funds target range, upper limit.
FED_FUNDS_SERIES = "DFEDTARU"
BACKFILL_DAYS = 365 * 3
RECENT_DAYS = 70
#: A move in the policy rate smaller than this is no change (basis points).
_RATE_EPS = 0.001


def store_series(db: Session, name: str, observations: Iterable[dict[str, Any]], source: str = ECB_SOURCE) -> int:
    """Upsert ``[{"date", "value"}]`` rows under ``name``. Does not commit."""
    from app.foundation.models.entities import MacroIndicator

    existing = {
        row.date: row
        for row in db.query(MacroIndicator).filter(MacroIndicator.name == name, MacroIndicator.source == source)
    }
    written = 0
    now = datetime.now(UTC)
    for obs in observations:
        raw_day, value = obs.get("date"), obs.get("value")
        if raw_day is None or value is None:
            continue
        try:
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
            val = Decimal(str(float(value)))
        except (TypeError, ValueError):
            continue
        row = existing.get(day)
        if row is None:
            row = MacroIndicator(name=name, value=val, date=day, source=source, fetched_at=now, stale=False)
            db.add(row)
            existing[day] = row
        else:
            row.value = val
            row.fetched_at = now
        written += 1
    return written


def refresh_ecb_series(db: Session, provider: Any) -> dict[str, Any]:
    """Fetch every :data:`ECB_SERIES` (a 3-year backfill the first time) and store it; commits."""
    from app.foundation.models.entities import MacroIndicator

    end = datetime.now(UTC).date()
    out: dict[str, Any] = {}
    for name, key in ECB_SERIES.items():
        stored = db.query(MacroIndicator).filter(
            MacroIndicator.name == name, MacroIndicator.source == ECB_SOURCE,
        ).count()
        start = end - timedelta(days=BACKFILL_DAYS if stored < 30 else RECENT_DAYS)
        try:
            result = provider.get_history(key, start=start.isoformat(), end=end.isoformat())
        except Exception as exc:  # noqa: BLE001 - one series failing must not stop the rest
            out[name] = {"ok": False, "error": str(exc)}
            continue
        if not result.get("ok"):
            out[name] = {"ok": False, "error": result.get("error")}
            continue
        out[name] = {"ok": True, "written": store_series(db, name, result.get("data") or [])}
    db.commit()
    return out


def _series(db: Session, name: str, source: str, since: date) -> list[tuple[date, float]]:
    from app.foundation.models.entities import MacroIndicator

    rows = (
        db.query(MacroIndicator.date, MacroIndicator.value)
        .filter(MacroIndicator.name == name, MacroIndicator.source == source, MacroIndicator.date >= since)
        .order_by(MacroIndicator.date)
        .all()
    )
    return [(d, float(v)) for d, v in rows]


def _direction(net_pp: float) -> str:
    if net_pp > _RATE_EPS:
        return "hikes"
    if net_pp < -_RATE_EPS:
        return "cuts"
    return "unchanged"


def policy_rate_fact(db: Session, name: str, source: str, *, today: date | None = None) -> dict[str, Any] | None:
    """Level, last change and 12-month net direction of a policy rate; None without data."""
    today = today or datetime.now(UTC).date()
    rows = _series(db, name, source, today - timedelta(days=400 + 365))
    if not rows:
        return None
    level_day, level = rows[-1]
    last_change = None
    for (_d0, v0), (d1, v1) in zip(rows, rows[1:]):
        if abs(v1 - v0) > _RATE_EPS:
            last_change = {"date": d1.isoformat(), "from": v0, "to": v1}
    year_ago = [v for d, v in rows if d <= today - timedelta(days=365)]
    net = (level - year_ago[-1]) if year_ago else None
    return {
        "level_pct": level,
        "as_of": level_day.isoformat(),
        "last_change": last_change,
        "net_change_12m_pp": round(net, 4) if net is not None else None,
        "direction_12m": _direction(net) if net is not None else None,
    }


def _latest(db: Session, name: str, source: str, today: date) -> dict[str, Any] | None:
    rows = _series(db, name, source, today - timedelta(days=150))
    if not rows:
        return None
    d, v = rows[-1]
    return {"value_pct": v, "month": d.strftime("%Y-%m")}


def macro_fact_block(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """The dossier's macro facts: what may be said about rates and inflation.

    ``rate_direction_12m`` is the ECB's net 12-month direction (``hikes`` /
    ``cuts`` / ``unchanged``), or None when there is no data: a thesis may
    then use no rate-direction words at all.
    """
    today = today or datetime.now(UTC).date()
    ecb = policy_rate_fact(db, "ECB_DFR", ECB_SOURCE, today=today)
    fed = policy_rate_fact(db, FED_FUNDS_SERIES, "fred", today=today)
    return {
        "ecb_deposit_rate": ecb,
        "fed_funds_upper": fed,
        "hicp_euro_area": _latest(db, "HICP_EA", ECB_SOURCE, today),
        "hicp_euro_area_core": _latest(db, "HICP_EA_CORE", ECB_SOURCE, today),
        "rate_direction_12m": {
            "ecb": ecb.get("direction_12m") if ecb else None,
            "fed": fed.get("direction_12m") if fed else None,
        },
    }
