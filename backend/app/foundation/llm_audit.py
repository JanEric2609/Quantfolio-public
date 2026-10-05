from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import LlmAuditEvent


SENSITIVE_PATTERNS = [
    re.compile(r"\b[A-Z]{2}[0-9A-Z]{10}\b"),
    re.compile(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2,8})?\b"),
]


def record_llm_audit(
    db: Session,
    *,
    user_id: str | None,
    agent: str,
    purpose: str,
    prompt: Any,
    response: Any,
    pinned: bool = False,
) -> LlmAuditEvent:
    row = LlmAuditEvent(
        user_id=user_id,
        agent=agent,
        purpose=purpose,
        prompt_json=json.dumps(prompt, default=str),
        response_json=json.dumps(response, default=str),
        pinned=pinned,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def apply_prompt_retention_policy(
    db: Session,
    *,
    now: datetime | None = None,
    raw_hours: int = 24,
    delete_days: int = 14,
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    redacted = 0
    deleted = 0
    redact_before = now - timedelta(hours=raw_hours)
    delete_before = now - timedelta(days=delete_days)
    for row in db.query(LlmAuditEvent).filter(LlmAuditEvent.pinned.is_(False), LlmAuditEvent.deleted_at.is_(None)).all():
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        if created <= delete_before:
            row.prompt_json = "{}"
            row.response_json = "{}"
            row.retention_state = "deleted"
            row.deleted_at = now
            deleted += 1
        elif created <= redact_before and row.retention_state == "raw":
            row.redacted_prompt_json = _redact_json(row.prompt_json)
            row.redacted_response_json = _redact_json(row.response_json)
            row.prompt_json = "{}"
            row.response_json = "{}"
            row.retention_state = "redacted"
            row.redacted_at = now
            redacted += 1
    db.commit()
    return {"redacted": redacted, "deleted": deleted}


def _redact_json(raw: str) -> str:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        value = raw
    return json.dumps(_redact(value), default=str)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SENSITIVE_PATTERNS:
            redacted = pattern.sub("[redacted]", redacted)
        return redacted
    if isinstance(value, (int, float)):
        return "[redacted-number]"
    return value
