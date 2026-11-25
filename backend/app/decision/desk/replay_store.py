"""The stored 2016-26 replay summaries (``desk_signal_replay_json``), read only (a leaf module: ``cfd`` and
``event_messages`` both quote them in their evidence line)."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.settings import get_setting_row

REPLAY_KEY = "desk_signal_replay_json"


def replay_summary(db: Session, cell: str) -> dict[str, Any] | None:
    """The cell's pooled replay summary, or None (no history, unreadable)."""
    raw = get_setting_row(db, REPLAY_KEY)
    try:
        replay = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    rp = replay.get(cell) if isinstance(replay, dict) else None
    return rp if isinstance(rp, dict) else None
