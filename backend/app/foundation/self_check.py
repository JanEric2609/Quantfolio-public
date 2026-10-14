"""Daily self-check: tell the owner on Telegram when something is broken.

Runs from cron on the app server (``infra/cron/quantfolio-self-check``),
deliberately outside the worker, so a dead worker is reported too. It takes
the error and warning items of the Control Center's attention list
(``control_center.attention_items``: worker, jobs, prices, brokers, backups,
evidence gate, disk, app errors, NV certificate) and adds the one check that
needs the network: whether the off-host backup server answers.

With problems it sends them to every admin's linked Telegram chat and the
in-app bell, every day until fixed. Without problems it stays quiet, except
on Mondays, when it sends a one-line "all fine" so silence never means the
check itself stopped.

    python -m app.foundation.self_check            # print, store, notify
    python -m app.foundation.self_check --dry-run  # print only
"""
from __future__ import annotations

import argparse
import logging
import socket
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SELF_CHECK_KEY = "self_check_json"
PROBE_TIMEOUT_SECONDS = 5.0
# Telegram caps a message at 4,096 characters.
MAX_MESSAGE = 3_800
WEEKLY_OK_WEEKDAY = 0  # Monday


def _probe_offsite(target: str) -> dict[str, Any] | None:
    """An attention-style item when host:port does not accept a TCP connection."""
    target = target.strip()
    if not target:
        return None
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        return {
            "id": "backup-offsite-config", "severity": "warning", "title": "Backup server address is invalid",
            "detail": f"'{target}' is not host:port.", "href": "/settings/storage", "action": "Fix",
        }
    try:
        with socket.create_connection((host, int(port)), timeout=PROBE_TIMEOUT_SECONDS):
            return None
    except OSError as exc:
        return {
            "id": "backup-offsite-down", "severity": "error", "title": "Backup server does not answer",
            "detail": f"{target}: {exc.strerror or type(exc).__name__}. The nightly container backups "
                      "cannot run, so there is no off-host copy. Switch the backup server on or fix its address.",
            "href": "/settings/storage", "action": "Check",
        }


def collect_problems(db: Session, *, probe: bool = True) -> list[dict[str, Any]]:
    """Error and warning items, most severe first. Info items are not problems."""
    from app.foundation.control_center import attention_items
    from app.foundation.settings import get_public_settings

    problems = [item for item in attention_items(db) if item["severity"] in ("error", "warning")]
    if probe:
        offsite = _probe_offsite(str(get_public_settings(db).get("backup_offsite_probe") or ""))
        if offsite is not None:
            problems.append(offsite)
    order = {"error": 0, "warning": 1}
    return sorted(problems, key=lambda item: order[item["severity"]])


def format_message(problems: list[dict[str, Any]]) -> str:
    """Plain text for Telegram (no markup: details contain paths and underscores)."""
    if not problems:
        return "Quantfolio self-check: all fine."
    errors = sum(1 for p in problems if p["severity"] == "error")
    head = f"Quantfolio self-check: {len(problems)} problem{'s' if len(problems) > 1 else ''}"
    if errors:
        head += f" ({errors} serious)"
    lines = [head, ""]
    for item in problems:
        mark = "❗" if item["severity"] == "error" else "⚠️"
        lines.append(f"{mark} {item['title']}")
        lines.append(f"   {item['detail']}")
    lines += ["", "Details: Control Center → Overview."]
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def _notify(db: Session, problems: list[dict[str, Any]], text: str) -> bool:
    """Telegram to every admin with a linked chat, plus a bell notification when there are problems."""
    from app.foundation.notify import alert_owners

    worst = problems[0]["severity"] if problems else "info"
    return alert_owners(
        db, source="self_check",
        title=f"Self-check: {len(problems)} problem{'s' if len(problems) > 1 else ''}",
        telegram_text=text,
        body="; ".join(p["title"] for p in problems)[:1000] or None,
        severity="error" if worst == "error" else "warning",
        href="/settings",
        bell=bool(problems),
    )


def run_self_check(db: Session, *, notify: bool = True, probe: bool = True, now: datetime | None = None) -> dict[str, Any]:
    """Collect, store as ``self_check_json`` and (optionally) notify; returns the stored result."""
    from app.foundation.app_errors import prune_errors
    from app.foundation.settings import upsert_public_settings

    when = now or datetime.now(UTC)
    try:
        prune_errors(db, now=when)
    except Exception:
        db.rollback()
        logger.warning("Pruning old app errors failed", exc_info=True)
    problems = collect_problems(db, probe=probe)
    text = format_message(problems)
    notified = False
    if notify and (problems or when.weekday() == WEEKLY_OK_WEEKDAY):
        notified = _notify(db, problems, text)
    result = {"ran_at": when.isoformat(), "problems": problems, "notified": notified}
    upsert_public_settings(db, {SELF_CHECK_KEY: result})
    return result


def last_result(db: Session) -> dict[str, Any] | None:
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, SELF_CHECK_KEY)
    return stored if isinstance(stored, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily self-check: report problems on Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="print the problems; store and send nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.foundation.core.db import SessionLocal

    with SessionLocal() as db:
        if args.dry_run:
            print(format_message(collect_problems(db)))
            return 0
        result = run_self_check(db)
    print(format_message(result["problems"]))
    print(f"notified: {result['notified']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
