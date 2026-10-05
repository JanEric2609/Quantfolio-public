"""LLM audit logging for diagnostics and compliance."""

import json

from sqlalchemy.orm import Session

from app.foundation.models.entities import AuditLog


def record_llm_call(
    db: Session,
    backend: str,
    model: str,
    task_type: str,
    tokens_in: int,
    tokens_out: int,
    is_degraded: bool = False,
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    """Record an LLM call to audit logs.

    Args:
        db: Database session
        backend: Backend name ("local_llama", "anthropic", "openai")
        model: Model identifier
        task_type: Task type ("interactive", "batch_research", "routine")
        tokens_in: Input tokens
        tokens_out: Output tokens
        is_degraded: Whether this was a degraded (fallback) call
        error: Error message if the call failed
        user_id: ID of user who initiated the call, or None for system/anonymous calls
    """
    payload = {
        "backend": backend,
        "model": model,
        "task_type": task_type,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "is_degraded": is_degraded,
    }

    event_type = "llm_call_failed" if error else "llm_call"
    severity = "warning" if is_degraded else "info"
    if error:
        severity = "error"
        payload["error"] = error

    if error:
        message = f"LLM call failed: {error}"
    else:
        message = f"LLM call: {model} via {backend} ({tokens_in}→{tokens_out} tokens)"

    audit_entry = AuditLog(
        source="llm",
        event_type=event_type,
        severity=severity,
        message=message,
        payload_json=json.dumps(payload),
        user_id=user_id,
    )
    db.add(audit_entry)
    db.commit()
