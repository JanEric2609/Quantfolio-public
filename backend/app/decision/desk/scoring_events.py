"""The event desk's scorecard (ADR 0028): its six cells, Holm across them, positions of its own hash."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import prereg_events, scoring


def scorecard_events(db: Session) -> dict[str, Any]:
    return scoring._scorecard(db, prereg_events.registered_at_events(db), prereg_events.EVENT_CELLS,
                              prereg_events.EVENT_HASH, prereg_events.EVENT_HASH)
