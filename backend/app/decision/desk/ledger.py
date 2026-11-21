"""The append-only, hash-chained event ledger (ADR 0027).

Every state change of a position goes through :func:`append_event` in the same
transaction as the position update. ``row_hash = sha256(prev_hash +
canonical_json({seq, created_at_iso, kind, mode, position_id, payload}))``; the
unique ``seq`` and ``prev_hash`` make a forked chain fail on insert (one retry).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.desk_chain import canonical_json, compute_hash, verify_chain
from app.foundation.models.entities import DeskEvent
from app.foundation.models.entities.desk import DESK_EVENT_KINDS, GENESIS_HASH


#: The one lock every desk write takes first (a transaction-level PostgreSQL advisory lock). Lock order is
#: then always: this lock, a position row, the ledger's next seq. Without it the monitor (ledger seq, then
#: a position row) and an owner's Close (the row, then the ledger) could deadlock. The jobs' own advisory
#: locks use ``hashtext(job_name)`` too; no job has this name.
WRITE_LOCK_NAME = "desk_ledger_write"


def desk_write_lock(db: Session) -> None:
    """Take the desk's write lock for the rest of this transaction (re-entrant; a no-op on SQLite)."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": WRITE_LOCK_NAME})


def _append_once(db: Session, kind: str, mode: str, payload: dict[str, Any], position_id: str | None,
                 now: datetime) -> DeskEvent:
    last = db.execute(select(DeskEvent).order_by(DeskEvent.seq.desc()).limit(1)).scalars().first()
    seq = (last.seq + 1) if last else 1
    prev = last.row_hash if last else GENESIS_HASH
    # The payload text is canonical, so re-parsing it later reproduces the hashed value.
    payload_text = canonical_json(payload)
    created = now.astimezone(UTC)
    row = DeskEvent(
        seq=seq, created_at=created, kind=kind, mode=mode, position_id=position_id, payload=payload_text,
        prev_hash=prev,
        row_hash=compute_hash(prev, seq=seq, created_at=created, kind=kind, mode=mode,
                              position_id=position_id, payload=json.loads(payload_text)),
    )
    db.add(row)
    db.flush()
    return row


def append_event(db: Session, kind: str, mode: str, payload: dict[str, Any], position_id: str | None = None,
                 *, now: datetime | None = None) -> DeskEvent:
    if kind not in DESK_EVENT_KINDS:
        raise ValueError(f"unknown desk event kind: {kind}")
    now = now or datetime.now(UTC)
    desk_write_lock(db)
    for attempt in (1, 2):
        try:
            with db.begin_nested():
                return _append_once(db, kind, mode, payload, position_id, now)
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def event_count(db: Session) -> int:
    return int(db.execute(select(func.count(DeskEvent.id))).scalar_one())


def events_for(db: Session, position_id: str, kind: str | None = None) -> list[DeskEvent]:
    stmt = select(DeskEvent).where(DeskEvent.position_id == position_id).order_by(DeskEvent.seq)
    if kind:
        stmt = stmt.where(DeskEvent.kind == kind)
    return list(db.execute(stmt).scalars())


__all__ = ["append_event", "canonical_json", "compute_hash", "desk_write_lock", "event_count", "events_for",
           "verify_chain"]
