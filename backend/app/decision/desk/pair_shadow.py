"""DAX and Euro Stoxx 50 together: what a 0.5R + 0.5R pair would have done (ADR 0027 amendment 2026-11-25).

The two indices share about 30 % of their weight, so two equal-risk legs on the same side carry about
1.95 R of one factor (``edge_math.pair_risk_multiple``). The registered cells score each index on its own and
stay as registered. This module is a read-only shadow: from the closed ``index_momentum:cfd`` positions of
one Berlin day where both fired, it adds the paper result of the pair at full size and at half size per leg.
It writes nothing, opens nothing and is not part of the pass test.
"""
from __future__ import annotations

import statistics
from datetime import UTC
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition

from . import calendar as cal
from .underlyings import ALIASES

PAIR = ("DAX", "ES50")
CELL = "index_momentum:cfd"


def pair_days(db: Session) -> list[dict[str, Any]]:
    """One row per day on which both indices closed a paper CFD: the legs' EUR results and the two sizings."""
    isins = {ALIASES[k].isin: k for k in PAIR}
    rows = db.query(DeskPosition).filter(
        DeskPosition.cell == CELL, DeskPosition.status == "closed", DeskPosition.underlying_isin.in_(list(isins)),
        DeskPosition.net_eur.isnot(None), DeskPosition.entry_at.isnot(None)).all()
    by_day: dict[str, dict[str, DeskPosition]] = defaultdict(dict)
    for p in rows:
        if p.entry_at is None or p.net_eur is None:
            continue
        entry = p.entry_at if p.entry_at.tzinfo else p.entry_at.replace(tzinfo=UTC)
        by_day[entry.astimezone(cal.BERLIN).date().isoformat()][isins[p.underlying_isin]] = p
    out = []
    for day, legs in sorted(by_day.items()):
        if len(legs) < len(PAIR):
            continue
        a, b = (float(legs[k].net_eur or 0) for k in PAIR)
        out.append({"day": day, "same_side": legs["DAX"].direction == legs["ES50"].direction,
                    "dax_eur": a, "es50_eur": b, "full_eur": a + b, "half_eur": 0.5 * (a + b)})
    return out


def report(db: Session) -> dict[str, Any]:
    days = pair_days(db)
    same = [d for d in days if d["same_side"]]
    def mean(key: str, rows: list[dict[str, Any]]) -> float | None:
        return statistics.fmean(d[key] for d in rows) if rows else None
    return {
        "days_both_fired": len(days), "days_same_side": len(same),
        "mean_full_eur": mean("full_eur", same), "mean_half_eur": mean("half_eur", same),
        "worst_full_eur": min((d["full_eur"] for d in same), default=None),
        "worst_half_eur": min((d["half_eur"] for d in same), default=None),
        "note": "shadow only: the registered cells are scored per index, unchanged",
        "estimate": True, "not_tax_advice": True,
    }
