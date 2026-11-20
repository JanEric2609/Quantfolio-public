"""Write performance ledger snapshots to database."""

import json
from datetime import datetime
from sqlalchemy.orm import Session
from app.foundation.models.entities import PerformanceLedgerEntry
from app.lab.performance_ledger.ex_post_risk import ExPostRiskMetrics, ex_post_risk_to_dict


def write_ledger_snapshot(
    db: Session,
    composite_id: str,
    as_of: datetime,
    twr: float,
    mwr: float,
    dispersion: float | None = None,
    ex_post_risk: ExPostRiskMetrics | None = None,
    snapshot_meta: dict | None = None,
) -> PerformanceLedgerEntry:
    """
    Write a performance ledger snapshot.

    Args:
        db: Database session
        composite_id: Composite ID
        as_of: Snapshot date/time
        twr: Time-weighted return
        mwr: Money-weighted return
        dispersion: Asset-weighted dispersion (optional)
        ex_post_risk: ExPostRiskMetrics object (optional)
        snapshot_meta: Additional metadata dict

    Returns:
        Created PerformanceLedgerEntry
    """
    ex_post_risk_json = "{}"
    if ex_post_risk:
        ex_post_risk_json = json.dumps(ex_post_risk_to_dict(ex_post_risk), default=str)

    snapshot_meta_json = "{}"
    if snapshot_meta:
        snapshot_meta_json = json.dumps(snapshot_meta, default=str)

    entry = PerformanceLedgerEntry(
        composite_id=composite_id,
        as_of=as_of,
        twr=twr,
        mwr=mwr,
        dispersion=dispersion,
        ex_post_risk_json=ex_post_risk_json,
        snapshot_meta_json=snapshot_meta_json,
    )
    db.add(entry)
    db.flush()
    return entry
