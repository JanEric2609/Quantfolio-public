"""Watchlist buy-alert checker.

Periodically compares each watchlist item's target_price against the latest
quote.  When the current price drops to or below the target price, a Nudge
is created and the alert_triggered / alert_triggered_at columns are set.

Runs as a scheduled APScheduler job (see services/jobs.py).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Nudge, WatchlistItem
from app.foundation.market import quote
from app.foundation.notify import send_notification

logger = logging.getLogger(__name__)


def check_watchlist_alerts(db: Session) -> dict[str, Any]:
    """Scan all watchlist items with a target_price and fire alerts.

    Returns a summary dict suitable for job-logging.
    """
    items: list[WatchlistItem] = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.target_price.isnot(None))
        .all()
    )
    checked = 0
    fired = 0
    errors = 0

    for item in items:
        if not item.ticker:
            continue
        if item.target_price is None:
            continue
        checked += 1
        try:
            q = quote(db, item.ticker)
            current_price = q.get("price")
            if current_price is None:
                continue

            # Alert fires when current price ≤ target price and hasn't already
            # been triggered since the last target change.
            if current_price <= float(item.target_price):
                # Avoid re-firing for the same target unless the user updated it
                if item.alert_triggered:
                    continue

                item.alert_triggered = True
                item.alert_triggered_at = datetime.now(UTC)
                fired += 1

                # Create nudge
                payload = {
                    "ticker": item.ticker,
                    "name": item.name,
                    "target_price": float(item.target_price),
                    "current_price": current_price,
                }
                nudge = Nudge(
                    user_id=item.user_id,
                    source="watchlist_buy_alert",
                    severity="info",
                    payload_json=json.dumps(payload),
                )
                db.add(nudge)

                # Send in-app bell notification (flush first so nudge.id is available)
                db.flush()
                send_notification(db, item.user_id, "bell", nudge)

                logger.info(
                    "Buy alert fired: %s %s target=%.4f current=%.4f",
                    item.ticker,
                    item.name,
                    float(item.target_price),
                    current_price,
                )
            else:
                # Price above target — reset alert so it can fire again if
                # price drops back down in the future.
                if item.alert_triggered:
                    item.alert_triggered = False
                    item.alert_triggered_at = None
        except Exception:
            errors += 1
            logger.exception("Error checking alert for %s", item.ticker)

    db.commit()

    return {"checked": checked, "fired": fired, "errors": errors}


def run_alert_check() -> dict[str, Any]:
    """Entry point called by the scheduler job — opens its own session."""
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        return check_watchlist_alerts(db)
    finally:
        db.close()
