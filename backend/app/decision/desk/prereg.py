"""The pre-registered spec and its hash (ADR 0027). A change fails ``tests/test_desk.py`` (the pinned hash).

Only positions entered after ``registered_at`` count. Changing anything here needs
an ADR amendment and a new registration, never an edit of an old one.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskRegistration
from app.foundation.quant_metrics import record_trial

from .ledger import append_event, canonical_json
from .limits import LIMITS

SIGNALS = ("owner_call", "index_momentum")
PRODUCTS = ("knockout", "warrant")
#: Scalable products for both signals, plus the index rule as a manual Trading 212 CFD (amendment 2026-11-20).
CELLS = (*(f"{s}:{p}" for s in SIGNALS for p in PRODUCTS), "index_momentum:cfd")
TRIAL_CONTEXT = "desk"

SPEC = {
    "version": 2,
    "cells": list(CELLS),
    "metric": "mean net_eur per closed paper position (fees and spread included)",
    "n_min": 40,
    "alpha": "0.05",
    "multiple_testing": "Holm across the 5 cells; a cell with n < n_min counts with p = 1",
    "bootstrap": {"kind": "day-block", "draws": 10000, "seed": 20261120, "p": "share of draws with mean <= 0"},
    "placebo": {
        "draws": 2000, "seed": 20261121, "direction_p": "0.5", "percentile": 95,
        "opposite_result": "-(leverage_or_omega) * underlying_move * notional - round_trip_cost",
        "missing_move": "trades without a recorded underlying move are left out of the placebo and its "
                        "comparison mean; at most 10 % of a cell's trades may lack one, else there is no placebo "
                        "and the cell cannot pass",
    },
    "signals": {
        "owner_call": "the owner's /idea call, taken by tap within 180 s, filled at a fresh quote",
        "index_momentum": {
            "rule": "Gao, Han, Li, Zhou (2018): sign of the return from the previous close to open+30min; "
                    "enter close-30min, exit close-3min",
            "indices": ["DAX", "ES50", "SPX", "NDX"],
            "skips": ["r1 == 0", "half day", "macro event on the day", "Friday or pre-holiday for the US rule"],
            "version": 1,
        },
    },
    "cfd": {
        "cell": "index_momentum:cfd",
        "execution": "manual by the owner on Trading 212 from a Telegram line; scored on paper only",
        "price": "the underlying's reference price (SPY/QQQ on Alpaca, the Xetra ETF proxy on sc) at entry and exit",
        "spread_bp": {"DAX": "2", "ES50": "4", "SPX": "2", "NDX": "2"},
        "fx_fee_on_result": "0.005",
        "fx_fee_applies_to": "USD indices (SPX, NDX)",
        "exposure": "max_loss_per_trade / daily sigma, capped at max_exposure_eur",
        "max_exposure_eur": "1000",
        "stop": "one daily sigma of the underlying, simulated on the reference price",
        "overnight_financing": "none: closed 3 minutes before the close",
    },
    "limits": LIMITS.as_json(),
}
PREREG_HASH = hashlib.sha256(canonical_json(SPEC).encode("ascii")).hexdigest()


def register(db: Session, *, now: datetime | None = None) -> DeskRegistration:
    """Idempotent: the first desk start records the registration, the event and one trial per cell."""
    now = now or datetime.now(UTC)
    row = db.query(DeskRegistration).filter(DeskRegistration.prereg_hash == PREREG_HASH).one_or_none()
    if row is None:
        row = DeskRegistration(prereg_hash=PREREG_HASH, spec=canonical_json(SPEC), registered_at=now.astimezone(UTC))
        db.add(row)
        db.flush()
        append_event(db, "registered", "paper", {"prereg_hash": PREREG_HASH, "cells": list(CELLS)}, now=now)
    for cell in CELLS:
        # Rows of one family with the same config_hash count once (ADR 0025): hash per cell.
        cell_hash = hashlib.sha256(f"{PREREG_HASH}:{cell}".encode("ascii")).hexdigest()
        record_trial(db, TRIAL_CONTEXT, f"{cell}:{PREREG_HASH[:12]}", {"cell": cell}, config_hash=cell_hash)
    db.commit()
    return row


def registered_at(db: Session) -> datetime | None:
    row = db.query(DeskRegistration).filter(DeskRegistration.prereg_hash == PREREG_HASH).one_or_none()
    if row is None or row.registered_at is None:
        return None
    return row.registered_at if row.registered_at.tzinfo else row.registered_at.replace(tzinfo=UTC)
