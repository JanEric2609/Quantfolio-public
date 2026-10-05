"""Research sync: exports QuantLab research reports to Obsidian vault.

Generates markdown files from StockResearchReport entries and portfolio analysis,
syncing them to a configurable Obsidian vault path or via the Obsidian REST API.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import StockResearchReport

logger = logging.getLogger(__name__)


def generate_research_markdown(db: Session, user_id: str) -> list[dict[str, str]]:
    """Generate markdown content for each StockResearchReport of a user.

    Returns a list of dicts with keys: title, content, ticker, created_at.
    """
    reports = (
        db.query(StockResearchReport)
        .filter(StockResearchReport.user_id == user_id)
        .order_by(StockResearchReport.generated_at.desc())
        .all()
    )

    results: list[dict[str, str]] = []
    for report in reports:
        title = f"{report.ticker} Research Report"
        content = f"# {title}\n\n"
        content += f"**Ticker:** {report.ticker}  \n"
        content += f"**Generated:** {report.generated_at.isoformat()}  \n\n"
        if report.executive_summary:
            content += "## Executive Summary\n\n"
            content += report.executive_summary + "\n\n"
        try:
            report_data = json.loads(report.report_json)
            if isinstance(report_data, dict):
                for section_key, section_val in report_data.items():
                    if section_key in ("ticker", "generated_at", "expires_at"):
                        continue
                    heading = section_key.replace("_", " ").title()
                    content += f"## {heading}\n\n"
                    if isinstance(section_val, str):
                        content += section_val + "\n\n"
                    else:
                        content += json.dumps(section_val, indent=2) + "\n\n"
            elif isinstance(report_data, str):
                content += report_data + "\n\n"
        except (json.JSONDecodeError, ValueError):
            content += report.report_json + "\n\n"

        results.append({
            "title": title,
            "content": content,
            "ticker": report.ticker,
            "created_at": report.generated_at.isoformat(),
        })

    return results


def sync_to_obsidian(db: Session, user_id: str) -> dict[str, Any]:
    """Sync research reports to Obsidian if the integration is configured.

    If the Obsidian REST API is not enabled or configured, returns the generated
    markdown without attempting a sync.
    """
    from app.foundation.obsidian import obsidian_status, _headers
    from app.foundation.settings import get_public_settings

    status = obsidian_status(db)
    reports = generate_research_markdown(db, user_id)

    if not status.get("enabled") or not status.get("available"):
        return {
            "synced": False,
            "reports_count": len(reports),
            "reports": reports,
            "message": status.get("message", "Obsidian integration not available."),
        }

    settings = get_public_settings(db)
    secret_key = "obsidian"
    from app.foundation.settings import get_secret
    secret = get_secret(db, secret_key)
    url = settings.get("obsidian_rest_url", "http://127.0.0.1:27123").rstrip("/")

    headers = _headers(secret)
    synced_count = 0
    import httpx

    from app.foundation.core.net import pin_url_to_ip, validate_outbound_url

    try:
        _, resolved_ip = validate_outbound_url(f"{url}/vault/")
        with httpx.Client(timeout=12, headers=headers) as client:
            for report in reports:
                created = report.get("created_at", "")
                date_suffix = created[:10] if created else "unknown"
                vault_path = f"QuantLab/Research/{report['ticker']}_{date_suffix}.md"
                payload = {"file": vault_path, "content": report["content"]}
                # Reuse the IP already validated for this host (same
                # hostname, different vault path) instead of re-resolving.
                pinned_url, hostname, extensions = pin_url_to_ip(f"{url}/vault/{vault_path}", resolved_ip)
                resp = client.post(pinned_url, json=payload, headers={"Host": hostname}, extensions=extensions)
                if resp.status_code < 400:
                    synced_count += 1
                else:
                    logger.warning("Obsidian sync failed for %s: %s", report["ticker"], resp.status_code)
    except Exception:
        logger.exception("Error syncing to Obsidian")
        return {
            "synced": False,
            "reports_count": len(reports),
            "reports": reports,
            "message": "Failed to sync to Obsidian",
        }

    return {
        "synced": synced_count == len(reports),
        "reports_count": len(reports),
        "synced_count": synced_count,
        "reports": reports,
    }


def get_research_summary(db: Session, user_id: str) -> dict[str, Any]:
    """Return summary statistics of research reports for a user."""
    reports = (
        db.query(StockResearchReport)
        .filter(StockResearchReport.user_id == user_id)
        .all()
    )

    if not reports:
        return {
            "total_reports": 0,
            "unique_tickers": [],
            "ticker_counts": {},
            "latest_report_date": None,
        }

    ticker_counts: dict[str, int] = {}
    latest_date: datetime | None = None
    for report in reports:
        ticker_counts[report.ticker] = ticker_counts.get(report.ticker, 0) + 1
        if latest_date is None or report.generated_at > latest_date:
            latest_date = report.generated_at

    return {
        "total_reports": len(reports),
        "unique_tickers": sorted(ticker_counts.keys()),
        "ticker_counts": ticker_counts,
        "latest_report_date": latest_date.isoformat() if latest_date else None,
    }
