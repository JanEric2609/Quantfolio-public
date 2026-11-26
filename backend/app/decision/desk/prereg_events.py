"""The event desk's own registration (ADR 0028): seven cells, the rules, the costs and the limits, hashed.

A second registration beside ``prereg.py``, which stays untouched. Only positions entered after
``registered_at_events`` count. Changing anything here needs an ADR amendment and a new registration.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.foundation import technical
from app.foundation.models.entities import DeskRegistration
from app.foundation.quant_metrics import record_trial

from . import cfd_instruments, prereg
from .event_limits import EVENT_LIMITS
from .ledger import append_event, canonical_json

EVENT_CELLS = ("ta_trend:cfd", "ta_band:cfd", "ta_rsi2:cfd", "hype_fade:cfd", "news_drift:cfd", "owner_call:cfd",
               "strong_event:cfd")
TRIAL_CONTEXT = "desk_events"

EVENT_SPEC = {
    "version": 2,
    "extends": prereg.PREREG_HASH,
    "cells": list(EVENT_CELLS),
    "metric": "mean net_eur per closed paper position (spread, FX fee and overnight interest included)",
    "n_min": 40,
    "alpha": "0.05",
    "multiple_testing": "Holm across the 7 event cells; a cell with n < n_min counts with p = 1",
    "bootstrap": prereg.SPEC["bootstrap"],
    "placebo": prereg.SPEC["placebo"],
    "rules": technical.EVENT_RULES,
    "costs": cfd_instruments.COSTS_JSON,
    "limits": EVENT_LIMITS.as_json(),
}
EVENT_HASH = hashlib.sha256(canonical_json(EVENT_SPEC).encode("ascii")).hexdigest()


def register_events(db: Session, *, now: datetime | None = None) -> DeskRegistration:
    """Idempotent: one registration row, one ledger event and one trial per cell."""
    now = now or datetime.now(UTC)
    row = db.query(DeskRegistration).filter(DeskRegistration.prereg_hash == EVENT_HASH).one_or_none()
    if row is None:
        row = DeskRegistration(prereg_hash=EVENT_HASH, spec=canonical_json(EVENT_SPEC), registered_at=now.astimezone(UTC))
        db.add(row)
        db.flush()
        append_event(db, "registered", "paper", {"prereg_hash": EVENT_HASH, "cells": list(EVENT_CELLS)}, now=now)
    for cell in EVENT_CELLS:
        cell_hash = hashlib.sha256(f"{EVENT_HASH}:{cell}".encode("ascii")).hexdigest()
        record_trial(db, TRIAL_CONTEXT, f"{cell}:{EVENT_HASH[:12]}", {"cell": cell}, config_hash=cell_hash)
    db.commit()
    return row


def registered_at_events(db: Session) -> datetime | None:
    row = db.query(DeskRegistration).filter(DeskRegistration.prereg_hash == EVENT_HASH).one_or_none()
    if row is None or row.registered_at is None:
        return None
    return row.registered_at if row.registered_at.tzinfo else row.registered_at.replace(tzinfo=UTC)
