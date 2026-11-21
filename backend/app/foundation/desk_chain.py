"""Hash-chain arithmetic and verification for ``desk_events`` (ADR 0027).

Lives in foundation so the Control Center and the API can verify the chain
without importing the decision layer; ``decision/desk/ledger.py`` appends.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskEvent
from app.foundation.models.entities.desk import GENESIS_HASH


def canonical_json(obj: Any) -> str:
    """Sorted, compact, ASCII-only: the prod database is SQL_ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _iso(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def compute_hash(prev_hash: str, *, seq: int, created_at: datetime, kind: str, mode: str,
                 position_id: str | None, payload: Any) -> str:
    body = canonical_json({"seq": seq, "created_at_iso": _iso(created_at), "kind": kind, "mode": mode,
                           "position_id": position_id, "payload": payload})
    return hashlib.sha256((prev_hash + body).encode("ascii")).hexdigest()


def _verify_from(db: Session, after_seq: int, prev: str) -> tuple[bool, int | None, int, str]:
    """Check the rows after *after_seq*, chained on *prev*: ``(ok, first_bad_seq, last_seq, last_hash)``."""
    expected_seq = after_seq + 1
    stmt = select(DeskEvent).where(DeskEvent.seq > after_seq).order_by(DeskEvent.seq)
    for row in db.execute(stmt).scalars():
        try:
            payload = json.loads(row.payload)
        except ValueError:
            return False, row.seq, after_seq, prev
        want = compute_hash(prev, seq=row.seq, created_at=row.created_at, kind=row.kind, mode=row.mode,
                            position_id=row.position_id, payload=payload)
        if row.seq != expected_seq or row.prev_hash != prev or row.row_hash != want:
            return False, row.seq, after_seq, prev
        prev, after_seq, expected_seq = row.row_hash, row.seq, expected_seq + 1
    return True, None, after_seq, prev


def verify_chain(db: Session) -> tuple[bool, int | None]:
    """``(True, None)`` for an intact chain, else ``(False, first_bad_seq)``. Re-hashes every row."""
    ok, bad, _seq, _hash = _verify_from(db, 0, GENESIS_HASH)
    return ok, bad


#: How often :func:`verify_chain_cached` re-hashes the whole chain instead of only the new rows.
FULL_VERIFY_EVERY_S = 600.0
_checkpoint: dict[str, Any] = {"seq": 0, "hash": GENESIS_HASH, "full_at": float("-inf")}
_checkpoint_lock = threading.Lock()


def _row_hashes_to(row: DeskEvent, want: str) -> bool:
    try:
        payload = json.loads(row.payload)
    except ValueError:
        return False
    return compute_hash(row.prev_hash, seq=row.seq, created_at=row.created_at, kind=row.kind, mode=row.mode,
                        position_id=row.position_id, payload=payload) == want


def verify_chain_cached(db: Session) -> tuple[bool, int | None]:
    """:func:`verify_chain` for pages polled every 30 s: only the rows after the last verified one, with
    a full pass at most every ``FULL_VERIFY_EVERY_S`` (or whenever the checkpoint row changed). An edit to
    an older row shows within that interval; the API's chain endpoint always re-hashes everything."""
    with _checkpoint_lock:
        cp = dict(_checkpoint)
    now = time.monotonic()
    full = now - cp["full_at"] >= FULL_VERIFY_EVERY_S
    if not full and cp["seq"]:
        # The checkpoint row must still be there, carry its hash, and hash to it (its payload unchanged).
        row = db.execute(select(DeskEvent).where(DeskEvent.seq == cp["seq"])).scalars().first()
        full = row is None or row.row_hash != cp["hash"] or not _row_hashes_to(row, cp["hash"])
    start, prev = (0, GENESIS_HASH) if full else (cp["seq"], cp["hash"])
    ok, bad, last_seq, last_hash = _verify_from(db, start, prev)
    with _checkpoint_lock:
        if ok:
            _checkpoint.update(seq=last_seq, hash=last_hash, full_at=now if full else cp["full_at"])
        else:
            _checkpoint.update(seq=0, hash=GENESIS_HASH, full_at=float("-inf"))
    return ok, bad
