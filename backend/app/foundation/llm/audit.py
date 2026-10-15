"""LLM audit logging for diagnostics and compliance.

One row per call through the router or ``_local_llm_sync`` (advisor, Discover
dossiers, reviews). Not yet audited: the Multi-Agent Council
(``llm_portfolio/agents/base.py``) and BLM views (``discover/blm.py``), which
post to the server directly, and embeddings.

The rows join the caller's transaction (they are never committed here), so a
caller that rolls back drops them: failure rows are best-effort.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import AuditLog, LlmAuditEvent

logger = logging.getLogger(__name__)

#: How long the per-call metadata row (``audit_logs``, source ``llm``) is kept.
#: The prompt and response bodies follow ``llm_audit``'s shorter retention.
CALL_METADATA_RETENTION_DAYS = 180
#: Longest response body kept with a call.
_MAX_BODY_CHARS = 20_000
#: Sampling keys copied from the request as sent.
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty",
                 "repeat_penalty", "max_tokens", "seed")


def prompt_sha256(messages: Any) -> str:
    """Stable hash of a chat prompt (canonical JSON)."""
    return hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()


def audit_completion(
    db: Session,
    *,
    backend: str,
    model: str,
    caller: str,
    messages: Any,
    sampling: dict[str, Any] | None,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: float,
    outcome: str,
    response_text: str | None = None,
    task_type: str | None = None,
    user_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """One row per LLM call (plan 2026-10-14, M7 / WP-D4.1). Never raises.

    The metadata (model, the sampling actually sent, prompt hash, tokens,
    latency, outcome, caller) goes to ``audit_logs`` and is kept for
    :data:`CALL_METADATA_RETENTION_DAYS`; the prompt and response bodies go to
    ``llm_audit_events``, which redacts them after a day and empties them after
    two weeks. Both rows join the caller's transaction (added, not committed),
    so an audit never commits half of someone else's work.
    """
    try:
        digest = prompt_sha256(messages)
        payload: dict[str, Any] = {
            "backend": backend,
            "model": model,
            "caller": caller,
            "task_type": task_type,
            "sampling": {k: v for k, v in (sampling or {}).items() if k in SAMPLING_KEYS},
            "prompt_sha256": digest,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": round(float(latency_ms), 1),
            "outcome": outcome,
            **(extra or {}),
        }
        ok = outcome == "ok"
        db.add(AuditLog(
            source="llm",
            event_type="llm_call" if ok else "llm_call_failed",
            severity="info" if ok else "warning",
            message=f"LLM call: {model} via {backend} for {caller} ({outcome}, {tokens_in}->{tokens_out} tokens)",
            payload_json=json.dumps(payload, default=str),
            user_id=user_id,
            correlation_id=digest[:36],
        ))
        db.add(LlmAuditEvent(
            user_id=user_id,
            agent=caller[-80:],
            purpose=(task_type or backend)[:120],
            prompt_json=json.dumps(messages, default=str),
            response_json=json.dumps({"content": (response_text or "")[:_MAX_BODY_CHARS], "meta": payload}, default=str),
        ))
    except Exception:  # noqa: BLE001 - an audit must never break the call it records
        logger.debug("llm audit: could not record a call from %s", caller, exc_info=True)


def prune_call_metadata(db: Session, *, now: datetime | None = None) -> int:
    """Delete LLM call metadata older than :data:`CALL_METADATA_RETENTION_DAYS`. Does not commit."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=CALL_METADATA_RETENTION_DAYS)
    return (
        db.query(AuditLog)
        .filter(AuditLog.source == "llm", AuditLog.created_at < cutoff)
        .delete(synchronize_session=False)
    )
