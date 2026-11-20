"""Hash-chain arithmetic and verification for ``desk_events`` (ADR 0027).

Lives in foundation so the Control Center and the API can verify the chain
without importing the decision layer; ``decision/desk/ledger.py`` appends.
"""
from __future__ import annotations

import hashlib
import json
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


def verify_chain(db: Session) -> tuple[bool, int | None]:
    """``(True, None)`` for an intact chain, else ``(False, first_bad_seq)``."""
    prev, expected_seq = GENESIS_HASH, 1
    for row in db.execute(select(DeskEvent).order_by(DeskEvent.seq)).scalars():
        try:
            payload = json.loads(row.payload)
        except ValueError:
            return False, row.seq
        want = compute_hash(prev, seq=row.seq, created_at=row.created_at, kind=row.kind, mode=row.mode,
                            position_id=row.position_id, payload=payload)
        if row.seq != expected_seq or row.prev_hash != prev or row.row_hash != want:
            return False, row.seq
        prev, expected_seq = row.row_hash, expected_seq + 1
    return True, None
