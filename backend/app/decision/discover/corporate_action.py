"""A split or spin-off newer than the company's last reported quarter.

Corteva spun off its seed business (Vylor) on 2026-10-01 and its price fell
from about 79 to 12 USD. The price history was adjusted, but the trailing
ratios divided the new price by the old company's earnings and book value:
run 57c8f8b8 (2026-11-10) led CTVA's thesis with a P/E of 8.2 and showed a
P/B of 0.33 and an ROE of 4 %. Until the company reports a quarter after the
action, those ratios describe a different company, so they are not shown.

Yahoo lists a spin-off as a split (CTVA: 6.665), so one lookup covers both.
A real split within the window also hides the ratios until the next report;
that costs little, a wrong P/E costs more. Called for the shortlist only (one
request per name), like the earnings calendar.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# A quarter ends at most ~92 days after the action and its report follows
# within ~45 days (10-Q deadline for large filers).
RECENT_ACTION_DAYS = 140

# Ratios built on reported earnings, book value or revenue. Market cap is a
# price times shares outstanding and stays.
STALE_RATIO_KEYS: tuple[str, ...] = ("pe", "pb", "roe", "de_ratio", "revenue_growth")

CORPORATE_ACTION_CONCERN_PREFIX = "split or spin-off on "


def recent_corporate_action(
    symbol: str,
    *,
    today: date | None = None,
    fetch: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """The latest split or spin-off within :data:`RECENT_ACTION_DAYS`, or None.

    Returns ``{"date": "YYYY-MM-DD", "factor": float}``. Fail-open: an
    unreadable lookup is None.
    """
    if fetch is None:
        from app.foundation.data_backbone.ingest import fetch_split_events as fetch
    today = today or datetime.now(UTC).date()
    try:
        events = fetch(symbol)
    except Exception:  # noqa: BLE001 - no lookup is not "no action"; the dossier just keeps its ratios
        logger.debug("corporate action lookup failed for %s", symbol, exc_info=True)
        return None
    if events is None or len(events) == 0:
        return None
    latest: tuple[date, float] | None = None
    for stamp, factor in events.items():
        try:
            day = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
            value = float(factor)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value == 1.0:
            continue
        if latest is None or day > latest[0]:
            latest = (day, value)
    if latest is None:
        return None
    day, value = latest
    if not (today - timedelta(days=RECENT_ACTION_DAYS) <= day <= today):
        return None
    return {"date": day.isoformat(), "factor": round(value, 4)}


def attach_corporate_action(
    symbol: str,
    scores: dict[str, Any],
    *,
    today: date | None = None,
    fetch: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Hide the reported-figure ratios in ``scores["sentiment_fundamentals"]``
    after a recent split or spin-off, record it there as ``corporate_action``
    and add a concern. *scores* is returned unchanged when there is none."""
    action = recent_corporate_action(symbol, today=today, fetch=fetch)
    if action is None:
        return scores
    sf = scores.get("sentiment_fundamentals")
    sf = dict(sf) if isinstance(sf, dict) else {}
    sf.update(dict.fromkeys(STALE_RATIO_KEYS))
    sf["corporate_action"] = action
    scores["sentiment_fundamentals"] = sf
    concerns = [
        c for c in scores.get("concerns") or [] if not str(c).startswith(CORPORATE_ACTION_CONCERN_PREFIX)
    ]
    concerns.append(f"{CORPORATE_ACTION_CONCERN_PREFIX}{action['date']}: ratios from reported figures predate it")
    scores["concerns"] = concerns
    logger.info("Discover: %s had a split or spin-off on %s (%s); ratios hidden", symbol, action["date"], action["factor"])
    return scores


def corporate_action_text(action: Any) -> str | None:
    """The dossier phrase for a recorded action, or None."""
    if not isinstance(action, dict) or not action.get("date"):
        return None
    return (
        f"a split or spin-off on {action['date']}, after the last reported quarter, so the price-to-earnings, "
        "price-to-book, return on equity, debt and revenue growth figures are left out until the next report"
    )
