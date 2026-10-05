"""ObsidianQuantSync: bidirectional sync between Obsidian vault and Quant pipeline.

Currently supports:
- Quant → Obsidian: research reports (existing in research_sync.py)
- Obsidian → Quant: factor hypothesis notes (NEW in this module)

Factor hypothesis notes in Obsidian should follow this frontmatter format:
```yaml
---
type: factor_hypothesis
name: momentum_quality
description: Quality-adjusted momentum factor
formula: rank(delta(close, 20) * roe)
regime: bull
confidence: 0.7
status: draft
---
```

The sync reads these notes, validates them, and creates FactorsLibrary entries.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.core.net import pin_url_to_ip, validate_outbound_url

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# Default vault path for factor hypotheses
FACTOR_HYPOTHESIS_PATH = "QuantLab/Factors"


def _headers(secret: str | None) -> dict[str, str]:
    """Build headers for Obsidian REST API."""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body_without_frontmatter).
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    fm_text = match.group(1)
    body = match.group(2)

    # Simple YAML parser for frontmatter (avoids pyyaml dependency)
    frontmatter: dict[str, Any] = {}
    current_key = None
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                # Parse common types
                if value.lower() in ("true", "yes"):
                    frontmatter[key] = True
                elif value.lower() in ("false", "no"):
                    frontmatter[key] = False
                elif value.replace(".", "").replace("-", "").isdigit():
                    frontmatter[key] = float(value) if "." in value else int(value)
                else:
                    frontmatter[key] = value.strip("\"'")
            else:
                current_key = key
                frontmatter[key] = []
        elif current_key and line.startswith("- "):
            item = line[2:].strip().strip("\"'")
            if isinstance(frontmatter.get(current_key), list):
                frontmatter[current_key].append(item)

    return frontmatter, body


def _get_obsidian_config(db: Session) -> tuple[str | None, str | None, bool]:
    """Get Obsidian REST API URL, secret, and enabled status."""
    from app.foundation.obsidian import obsidian_status
    from app.foundation.settings import get_public_settings, get_secret

    status = obsidian_status(db)
    if not status.get("enabled") or not status.get("available"):
        return None, None, False

    settings = get_public_settings(db)
    url = settings.get("obsidian_rest_url", "http://127.0.0.1:27123").rstrip("/")
    secret_tuple = get_secret(db, "obsidian")
    secret = secret_tuple[0] if secret_tuple else None
    return url, secret, True


def read_factor_hypotheses(db: Session) -> list[dict[str, Any]]:
    """Read factor hypothesis notes from Obsidian vault.

    Returns list of parsed factor hypotheses with frontmatter + body.
    """
    url, secret, enabled = _get_obsidian_config(db)
    if not enabled:
        return []

    headers = _headers(secret)
    hypotheses = []

    try:
        listing_url, resolved_ip = validate_outbound_url(f"{url}/vault/{FACTOR_HYPOTHESIS_PATH}")
        pinned_listing_url, hostname, extensions = pin_url_to_ip(listing_url, resolved_ip)
        with httpx.Client(timeout=15, headers=headers) as client:
            # List files in the factor hypothesis directory
            resp = client.get(pinned_listing_url, headers={"Host": hostname}, extensions=extensions)
            if resp.status_code >= 400:
                logger.debug("Obsidian directory listing failed: %s", resp.status_code)
                return []

            files = resp.json()
            if not isinstance(files, list):
                return []

            for file_info in files:
                if not isinstance(file_info, dict):
                    continue
                file_path = file_info.get("path", "")
                if not file_path.endswith(".md"):
                    continue

                # Read file content — reuse the IP already validated for this
                # host (same hostname, different vault path) instead of
                # re-resolving DNS per file.
                file_pinned_url, file_hostname, file_extensions = pin_url_to_ip(
                    f"{url}/vault/{file_path}", resolved_ip
                )
                file_resp = client.get(
                    file_pinned_url, headers={"Host": file_hostname}, extensions=file_extensions
                )
                if file_resp.status_code >= 400:
                    continue

                content_data = file_resp.json()
                content = content_data.get("content", "") if isinstance(content_data, dict) else ""

                frontmatter, body = _parse_frontmatter(content)
                if frontmatter.get("type") != "factor_hypothesis":
                    continue

                hypotheses.append({
                    "file_path": file_path,
                    "name": frontmatter.get("name", file_path.split("/")[-1].replace(".md", "")),
                    "description": frontmatter.get("description", ""),
                    "formula": frontmatter.get("formula", ""),
                    "regime": frontmatter.get("regime"),
                    "confidence": _safe_float(frontmatter.get("confidence", 0.5), 0.5),
                    "status": frontmatter.get("status", "draft"),
                    "body": body.strip(),
                    "frontmatter": frontmatter,
                })

    except Exception as exc:
        logger.warning("Failed to read Obsidian factor hypotheses: %s", exc)

    return hypotheses


def sync_hypotheses_to_factors(
    db: Session,
    user_id: str,
) -> dict[str, Any]:
    """Sync factor hypothesis notes from Obsidian into FactorsLibrary entries.

    Only creates entries for hypotheses with status != "retired".
    Skips entries where name already exists in the library.
    """
    from app.foundation.models.entities import FactorsLibrary

    hypotheses = read_factor_hypotheses(db)
    if not hypotheses:
        return {"synced": 0, "skipped": 0, "hypotheses": []}

    # Get existing factor names (FactorsLibrary is shared, not per-user)
    existing = {
        f.name for f in db.query(FactorsLibrary).all() if f.name
    }

    synced = 0
    skipped = 0
    created = []

    for hyp in hypotheses:
        name = hyp.get("name", "")
        status = hyp.get("status", "draft")

        if status == "retired":
            skipped += 1
            continue
        if name in existing:
            skipped += 1
            continue

        try:
            import datetime
            entry = FactorsLibrary(
                name=name,
                formula_json=hyp.get("formula", ""),
                source="obsidian_sync",
                created_at=datetime.datetime.utcnow(),
                ic_summary_json=json.dumps({
                    "regime": hyp.get("regime"),
                    "confidence": hyp.get("confidence"),
                    "body": hyp.get("body", ""),
                    "obsidian_path": hyp.get("file_path"),
                }, default=str),
            )
            db.add(entry)
            existing.add(name)
            synced += 1
            created.append(name)
        except Exception as exc:
            logger.warning("Failed to sync hypothesis %s: %s", name, exc)
            skipped += 1

    if synced > 0:
        db.commit()

    return {
        "synced": synced,
        "skipped": skipped,
        "created": created,
        "total_hypotheses": len(hypotheses),
    }


def get_sync_status(db: Session) -> dict[str, Any]:
    """Return the current sync status between Obsidian and Quant pipeline."""
    url, secret, enabled = _get_obsidian_config(db)

    if not enabled:
        return {
            "enabled": False,
            "message": "Obsidian integration not configured",
            "hypothesis_count": 0,
        }

    hypotheses = read_factor_hypotheses(db)

    return {
        "enabled": True,
        "vault_url": url,
        "hypothesis_count": len(hypotheses),
        "hypotheses": [
            {
                "name": h.get("name"),
                "status": h.get("status"),
                "regime": h.get("regime"),
                "confidence": h.get("confidence"),
            }
            for h in hypotheses
        ],
    }
