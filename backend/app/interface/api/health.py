from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.core.config import get_settings
from app.foundation.core.db import get_db
from app.foundation.data_backbone.health_check import check_critical_tables, check_write_invariants

router = APIRouter()


def check_db(db: Session) -> bool:
    """Check if database is accessible.

    `get_db` yields a synchronous SQLAlchemy `Session`; do not await it.
    `db.execute(...)` returns a Result (not a coroutine), and awaiting a
    non-coroutine would raise TypeError that the outer try/except would
    silently swallow, leaving `db_up` always False.
    """
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_redis() -> bool:
    """Check if Redis is accessible."""
    settings = get_settings()
    if not settings.redis_url:
        return True  # Redis is optional
    try:
        import redis.asyncio
        redis_client = redis.asyncio.from_url(settings.redis_url)
        await redis_client.ping()
        await redis_client.close()
        return True
    except Exception:
        return False


@router.get("/healthz")
async def healthz(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Health check for Proxmox deployment (unauthenticated, local probes only).

    Probes the database, Redis and the schema. It deliberately makes no outbound
    request: an open endpoint that dials the LLM and the cloud providers on every
    call is a free amplifier. The LLM backends are probed by the authenticated
    ``GET /api/finagent/llm/health``.
    """
    db_up = check_db(db)
    redis_up = await check_redis()
    critical_tables = check_critical_tables(db)
    write_invariants = check_write_invariants(db)

    ok = db_up and redis_up and critical_tables["ok"] and write_invariants["ok"]

    return {
        "ok": ok,
        "timestamp": datetime.now(UTC).isoformat(),
        "version": "0.1.0",
        "checks": {
            "db_up": db_up,
            "redis_up": redis_up,
            "critical_tables_ok": critical_tables["ok"],
            "critical_tables_missing": critical_tables["missing"],
            "hypertable_write_invariants_ok": write_invariants["ok"],
            "hypertable_missing_id_default": write_invariants["missing_id_default"],
            "hypertable_missing_unique_index": write_invariants["missing_unique_index"],
        },
    }
