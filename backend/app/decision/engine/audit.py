"""Immutable append-only audit trail for the headless portfolio engine.

Thread-safe, in-memory only. Consumer handles external persistence.
"""

from __future__ import annotations

import json
from datetime import datetime
from threading import Lock
from typing import Any

from app.decision.engine.types import AuditEntry, EventType


class AuditTrail:
    """Append-only audit trail storing AuditEntry records.

    Thread-safe via reentrant lock. Entries are never mutated after creation.
    Sequence numbers are monotonically increasing per portfolio_id.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = Lock()
        self._counters: dict[str, int] = {}

    def record(
        self,
        event_type: EventType,
        portfolio_id: str,
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AuditEntry:
        """Record a new audit entry.

        If idempotency_key is provided and an entry with the same key already
        exists, the existing entry is returned instead of creating a duplicate.
        """
        with self._lock:
            if idempotency_key is not None:
                existing = self._find_by_idempotency_key(
                    idempotency_key, portfolio_id
                )
                if existing is not None:
                    return existing

            seq = self._next_sequence(portfolio_id)
            entry = AuditEntry(
                event_type=event_type,
                portfolio_id=portfolio_id,
                details=details or {},
                run_id=run_id,
                idempotency_key=idempotency_key,
                sequence=seq,
            )
            self._entries.append(entry)
            return entry

    def _next_sequence(self, portfolio_id: str) -> int:
        """Return the next monotonically increasing sequence number for a portfolio.

        Resets per AuditTrail instance (i.e., per process lifetime).
        """
        seq = self._counters.get(portfolio_id, 0) + 1
        self._counters[portfolio_id] = seq
        return seq

    def _find_by_idempotency_key(
        self, key: str, portfolio_id: str | None = None
    ) -> AuditEntry | None:
        """Find an existing entry by idempotency_key, searching newest first.

        If portfolio_id is provided, only match entries for that portfolio.
        """
        for entry in reversed(self._entries):
            if entry.idempotency_key == key:
                if portfolio_id is None or entry.portfolio_id == portfolio_id:
                    return entry
        return None

    def by_portfolio(self, portfolio_id: str) -> list[AuditEntry]:
        """Return all entries for a given portfolio, ordered by sequence."""
        with self._lock:
            return [e for e in self._entries if e.portfolio_id == portfolio_id]

    def by_run(self, run_id: str) -> list[AuditEntry]:
        """Return all entries for a given run_id."""
        with self._lock:
            return [e for e in self._entries if e.run_id == run_id]

    def by_event_type(self, event_type: EventType) -> list[AuditEntry]:
        """Return all entries of a given event type."""
        with self._lock:
            return [e for e in self._entries if e.event_type == event_type]

    def by_time_range(self, start: datetime, end: datetime) -> list[AuditEntry]:
        """Return entries with timestamps in [start, end) range."""
        with self._lock:
            return [e for e in self._entries if start <= e.timestamp < end]

    def latest(self, portfolio_id: str, n: int = 10) -> list[AuditEntry]:
        """Return the most recent n entries for a portfolio, newest first."""
        if n <= 0:
            return []
        with self._lock:
            filtered = [e for e in self._entries if e.portfolio_id == portfolio_id]
            return list(reversed(filtered))[:n]

    def to_dicts(self) -> list[dict[str, Any]]:
        """Export all entries as plain dicts (for serialization)."""
        with self._lock:
            return [e.model_dump(mode="json") for e in self._entries]

    def to_json(self) -> str:
        """Export all entries as a JSON string."""
        return json.dumps(self.to_dicts(), default=str, ensure_ascii=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        return f"<AuditTrail entries={len(self)}>"
