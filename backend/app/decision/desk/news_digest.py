"""The morning news digest: one Telegram message with what the wire stored overnight about the owner's holdings.

The alert path (``newswire.alert_decision``) tells the owner about the few items that matter at once. Everything
else the wire stores about a holding (rated 1 or higher, or not rated) used to be seen only on the Desk page; this
reads it back once a day. Silent when there is nothing: no "nothing happened" message.

Sources, all already stored: ``news_wire_items`` (Alpaca/Benzinga, German ad-hoc, Handelsblatt headlines that name
a holding, SEC 8-K) and ``news_items`` (provider headlines per ticker). Central-bank items rated 2 or 3 go in a
macro line. Read-only: nothing here rates, trades or blocks.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.foundation.models.entities import NewsItem, NewsWireItem

from . import calendar as cal
from .newswire import SOURCE_LABELS, _norm_headline, watchlist

PER_HOLDING = 3
MAX_HOLDINGS = 10
MACRO_ITEMS = 3
KEY = "desk_news_digest_json"  # internal state (no catalog entry): the date the digest last went out


def window_start(now: datetime) -> datetime:
    """24 hours back; 72 on a Monday, so the weekend is covered."""
    local = now.astimezone(cal.BERLIN)
    return now - timedelta(hours=72 if local.weekday() == 0 else 24)


def digest_due(now: datetime) -> bool:
    """The 08:17 Berlin run of the hourly reconcile job on a weekday."""
    local = now.astimezone(cal.BERLIN)
    return local.weekday() < 5 and local.hour == 8


def _cut(text: str, n: int = 150) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build(db: Session, user_id: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    since = window_start(now)
    watch = watchlist(db, user_id)
    rows = db.query(NewsWireItem).filter(
        NewsWireItem.received_at >= since, NewsWireItem.relevance.like("holding:%"),
        NewsWireItem.alert.notin_(("baseline", "dup_headline", "stale_dup"))).all()
    rows = [r for r in rows if r.materiality is None or r.materiality >= 1]
    by_holding: dict[str, list[tuple[int, str, str]]] = {}
    seen: set[str] = set()
    for r in sorted(rows, key=lambda r: (-(r.materiality if r.materiality is not None else 1), r.received_at), reverse=False):
        key = _norm_headline(r.headline)
        if key in seen:
            continue
        seen.add(key)
        mark = f"{r.materiality}/3 {r.direction}" if r.materiality is not None else "not rated"
        if r.alert == "sent":
            mark += ", alerted"
        src = SOURCE_LABELS.get(r.source, r.source)
        by_holding.setdefault(r.relevance[8:], []).append((r.materiality or 0, _cut(r.headline), f"{src}; {mark}"))

    # Provider headlines per ticker (the app's news refresh), only for holdings with a US-style ticker key.
    tickers = {why[8:]: sym for sym, why in watch.items() if why.startswith("holding:") and sym.isalpha() and len(sym) <= 5}
    if tickers:
        for it in db.query(NewsItem).filter(NewsItem.ticker.in_(list(tickers.values())),
                                            NewsItem.published_at >= since).order_by(NewsItem.published_at.desc()).all():
            key = _norm_headline(it.title)
            label = next((lab for lab, sym in tickers.items() if sym == it.ticker), it.ticker or "")
            if key in seen or not label:
                continue
            seen.add(key)
            by_holding.setdefault(label, []).append((0, _cut(it.title), it.source))

    macro = db.query(NewsWireItem).filter(
        NewsWireItem.received_at >= since, NewsWireItem.relevance == "central_bank",
        NewsWireItem.alert != "baseline", NewsWireItem.materiality >= 2).order_by(NewsWireItem.received_at.desc()) \
        .limit(MACRO_ITEMS).all()

    if not by_holding and not macro:
        return None
    lines = ["News on your holdings since yesterday:"]
    ordered = sorted(by_holding.items(), key=lambda kv: -max(m for m, _, _ in kv[1]))[:MAX_HOLDINGS]
    for label, items in ordered:
        share = getattr(watch, "weights", {}).get(f"holding:{label}")
        lines.append(f"\n{label}" + (f" ({share:g} % of your book)" if share else ""))
        for _, headline, how in sorted(items, key=lambda t: -t[0])[:PER_HOLDING]:
            lines.append(f"- {headline} ({how})")
    if len(by_holding) > MAX_HOLDINGS:
        lines.append(f"\n+ {len(by_holding) - MAX_HOLDINGS} more holdings with news on the News page.")
    if macro:
        lines.append("\nCentral banks")
        lines += [f"- {_cut(m.headline)} ({SOURCE_LABELS.get(m.source, m.source)}; {m.materiality}/3)" for m in macro]
    return "\n".join(lines)


def send(db: Session, user_id: str, now: datetime | None = None) -> dict[str, object]:
    """Send the digest once per Berlin day; a day with nothing sends nothing."""
    from app.foundation.settings import get_setting_row, upsert_public_settings

    from . import telegram

    now = now or datetime.now(UTC)
    today = now.astimezone(cal.BERLIN).date().isoformat()
    last = get_setting_row(db, KEY)
    if isinstance(last, dict) and last.get("date") == today:
        return {"skipped": "already_sent"}
    text = build(db, user_id, now)
    if text is None:
        upsert_public_settings(db, {KEY: {"date": today, "sent": False}})
        return {"skipped": "nothing_new"}
    # The day is recorded only after delivery: a failed send leaves the slot free for a retry.
    if not telegram.alert(db, user_id, text, preview=False):
        return {"skipped": "not_delivered"}
    upsert_public_settings(db, {KEY: {"date": today, "sent": True}})
    return {"sent": True}
