from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.core.net import pin_url_to_ip, validate_outbound_url
from app.foundation.settings import get_public_settings, get_secret

logger = logging.getLogger(__name__)


def obsidian_status(db: Session) -> dict[str, Any]:
    settings = get_public_settings(db)
    secret = get_secret(db, "obsidian")
    enabled = bool(settings.get("obsidian_enabled", False))
    url = settings.get("obsidian_rest_url", "http://127.0.0.1:27123")
    if not enabled:
        return {"enabled": False, "configured": bool(secret), "available": False, "message": "Obsidian REST integration is disabled."}
    try:
        target_url = url.rstrip("/") + "/"
        target_url, resolved_ip = validate_outbound_url(target_url)
        pinned_url, hostname, extensions = pin_url_to_ip(target_url, resolved_ip)
        with httpx.Client(timeout=4, headers=_headers(secret)) as client:
            response = client.get(pinned_url, headers={"Host": hostname}, extensions=extensions)
        return {"enabled": True, "configured": bool(secret), "available": response.status_code < 500, "message": "Obsidian REST endpoint responded."}
    except Exception:
        logger.exception("Obsidian status check failed for %s", url)
        return {"enabled": True, "configured": bool(secret), "available": False, "message": "Obsidian REST endpoint unavailable"}


def query_obsidian(db: Session, query: str, limit: int = 10) -> dict[str, Any]:
    settings = get_public_settings(db)
    secret = get_secret(db, "obsidian")
    if not settings.get("obsidian_enabled", False):
        return {"ok": False, "results": [], "warnings": ["Obsidian REST integration is disabled."]}
    url = settings.get("obsidian_rest_url", "http://127.0.0.1:27123").rstrip("/")
    try:
        search_url, resolved_ip = validate_outbound_url(f"{url}/search/")
        pinned_url, hostname, extensions = pin_url_to_ip(search_url, resolved_ip)
        with httpx.Client(timeout=8, headers=_headers(secret)) as client:
            response = client.post(
                pinned_url,
                json={"query": query, "contextLength": 120},
                headers={"Host": hostname},
                extensions=extensions,
            )
            response.raise_for_status()
            data = response.json()
        rows = data if isinstance(data, list) else data.get("results", [])
        return {"ok": True, "results": rows[:limit], "warnings": []}
    except Exception:
        logger.exception("Obsidian query failed")
        return {"ok": False, "results": [], "warnings": ["Obsidian query failed"]}


def _headers(secret: tuple[str, dict[str, Any]] | None) -> dict[str, str]:
    if not secret or not secret[0]:
        return {}
    return {"Authorization": f"Bearer {secret[0]}"}
