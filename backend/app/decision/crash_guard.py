"""Crash guard: keep the plan when the market falls.

The money an investor loses to bad timing is usually larger than any edge a
strategy finds: Morningstar's "Mind the Gap" puts the gap between what funds
return and what their investors earn at about 1.2 points a year, mostly from
selling after falls and buying after rises. This module does three things
when the core (MSCI World, ``eur_prices.benchmark_ticker``) is well below its
high:

1. **Says where the market is**: the fall from its high and a level
   (correction from 10 %, bear market from 20 %, crash from 30 %).
2. **Puts it in context with the app's own data**: every earlier fall of 10 %
   or more in the stored history, how deep it went and how long the way back
   took. Nothing is quoted from outside; the numbers are this series'.
3. **Watches what the investor does**: a savings plan paused or overdue, or
   core ETF units sold since the fall began. These are the actions that turn a
   paper loss into a real one.

The owner's own crash rule (setting ``crash_plan_note``, written in calm
times) is shown back next to it. The worker's daily job sends a Telegram
message when the level gets worse, when a recovery is complete, and when a
savings plan stops or core units are sold during a fall.

Information only: nothing here trades, and DKB and Scalable stay read-only.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# (threshold, level, label), most severe first.
LEVELS: tuple[tuple[float, str, str], ...] = (
    (0.30, "crash", "Crash"),
    (0.20, "bear", "Bear market"),
    (0.10, "correction", "Correction"),
)
LEVEL_ORDER = {"calm": 0, "correction": 1, "bear": 2, "crash": 3}
EPISODE_MIN_DEPTH = 0.10
HISTORY_DAYS = 365 * 30
# Internal state for the daily job (no catalog entry, see AGENTS.md).
STATE_KEY = "crash_guard_state_json"
# Units sold below this share of the core position are rounding, not a sale.
MIN_SOLD_SHARE = 0.01


@dataclass
class Episode:
    """One fall of EPISODE_MIN_DEPTH or more from a high, and the way back."""

    peak_date: str
    trough_date: str
    depth: float
    months_to_trough: float
    recovery_date: str | None
    months_to_recover: float | None  # trough to the old high; None while not recovered


@dataclass
class MarketState:
    ticker: str
    as_of: str | None
    close: float | None
    peak: float | None
    peak_date: str | None
    drawdown: float  # 0.0 at the high, 0.25 is 25 % below it
    level: str
    level_label: str
    history_start: str | None
    episodes: list[Episode] = field(default_factory=list)


def _months(a: date, b: date) -> float:
    return round((b - a).days / 30.4375, 1)


def level_for(drawdown: float) -> tuple[str, str]:
    for threshold, level, label in LEVELS:
        if drawdown >= threshold:
            return level, label
    return "calm", "Calm"


def find_episodes(closes: list[tuple[date, float]], min_depth: float = EPISODE_MIN_DEPTH) -> list[Episode]:
    """Every fall of *min_depth* or more below a running high, oldest first.

    An episode starts at a high, reaches its lowest close, and ends when the
    close is back at that high. The last one may still be open.
    """
    episodes: list[Episode] = []
    if not closes:
        return episodes
    peak_day, peak = closes[0]
    trough_day, trough = peak_day, peak
    in_fall = False
    for day, close in closes[1:]:
        if close >= peak:
            if in_fall:
                episodes.append(Episode(
                    peak_date=peak_day.isoformat(), trough_date=trough_day.isoformat(),
                    depth=round(1 - trough / peak, 4), months_to_trough=_months(peak_day, trough_day),
                    recovery_date=day.isoformat(), months_to_recover=_months(trough_day, day),
                ))
            peak_day, peak = day, close
            trough_day, trough = day, close
            in_fall = False
            continue
        if close < trough:
            trough_day, trough = day, close
        # Rounded as in market_state_from, so a fall of exactly 10 % is an episode there and here.
        if round(1 - trough / peak, 4) >= min_depth:
            in_fall = True
    if in_fall:
        episodes.append(Episode(
            peak_date=peak_day.isoformat(), trough_date=trough_day.isoformat(),
            depth=round(1 - trough / peak, 4), months_to_trough=_months(peak_day, trough_day),
            recovery_date=None, months_to_recover=None,
        ))
    return episodes


def market_state_from(ticker: str, closes: list[tuple[date, float]]) -> MarketState:
    if not closes:
        return MarketState(ticker, None, None, None, None, 0.0, "calm", "Calm", None)
    # The all-time high of the stored series (the latest day on a tie).
    peak_day, peak = max(closes, key=lambda item: (item[1], item[0]))
    last_day, last = closes[-1]
    # Rounded before the level, so 20 % computed as 0.19999… is a bear market.
    drawdown = round(max(0.0, 1 - last / peak), 4) if peak > 0 else 0.0
    level, label = level_for(drawdown)
    return MarketState(
        ticker=ticker, as_of=last_day.isoformat(), close=round(last, 4), peak=round(peak, 4),
        peak_date=peak_day.isoformat(), drawdown=drawdown, level=level, level_label=label,
        history_start=closes[0][0].isoformat(), episodes=find_episodes(closes),
    )


def _closes(db: Session, ticker: str) -> list[tuple[date, float]]:
    """The benchmark's closes in EUR: a configured benchmark quoted in another
    currency would otherwise show FX moves as falls (a EUR listing passes through)."""
    from app.foundation.eur_prices import eur_closes

    closes = eur_closes(db, ticker, days=HISTORY_DAYS, allow_live=False)
    return sorted((date.fromisoformat(day), close) for day, close in closes.items())


def index_name(ticker: str | None) -> str:
    """What the alerts call the benchmark: MSCI World for the default, else its ticker."""
    from app.foundation.eur_prices import DEFAULT_BENCHMARK

    return "MSCI World" if not ticker or ticker.upper() == DEFAULT_BENCHMARK else ticker


def market_state(db: Session) -> MarketState:
    from app.foundation.eur_prices import benchmark_ticker

    ticker = benchmark_ticker(db)
    return market_state_from(ticker, _closes(db, ticker))


# --- what the investor did ------------------------------------------------------


def _core_units_by_isin(
    db: Session, user_id: str, core_isins: set[str], since: date,
) -> dict[str, tuple[str, float, float]]:
    """Per core ISIN: name, the most units held on a book day since *since*, and the units held now.

    Per ISIN, not summed: units of two ETFs are not interchangeable, so selling
    one while buying another must still read as a sale. "Now" is the synced
    broker positions (``live_positions``, what the book snapshots record): a
    fully sold position is dropped there and in the day's snapshot rather than
    written as 0, so only a comparison with the current positions sees it, and
    a day no sync ran leaves them, and so the comparison, unchanged.
    """
    from sqlalchemy import func

    from app.foundation.live_positions import live_positions
    from app.foundation.models.entities import BookPositionSnapshot

    rows = (
        db.query(
            BookPositionSnapshot.isin, BookPositionSnapshot.snapshot_date,
            func.max(BookPositionSnapshot.name), func.sum(BookPositionSnapshot.quantity),
        )
        .filter(
            BookPositionSnapshot.user_id == user_id,
            BookPositionSnapshot.isin.in_(sorted(core_isins)),
            BookPositionSnapshot.snapshot_date >= since,
        )
        .group_by(BookPositionSnapshot.isin, BookPositionSnapshot.snapshot_date)
        .all()
    )
    highs: dict[str, tuple[str, float]] = {}
    for isin, _day, name, qty in rows:
        label, high = highs.get(isin, (name or isin, 0.0))
        highs[isin] = (label, max(high, float(qty or 0)))
    if not highs:
        return {}
    now: dict[str, float] = {}
    for pos in live_positions(db, user_id, include_unreconciled=True):
        if pos.isin in highs:
            now[pos.isin] = now.get(pos.isin, 0.0) + float(pos.quantity or 0)
    return {isin: (name, high, now.get(isin, 0.0)) for isin, (name, high) in highs.items()}


def behaviour(db: Session, user_id: str, state: MarketState) -> list[dict[str, Any]]:
    """Savings plans not running, and core units sold since the fall began."""
    from app.decision.monthly_plan import plan_settings, running_savings_plans

    if state.level == "calm" or not state.peak_date:
        return []
    cfg = plan_settings(db)
    findings: list[dict[str, Any]] = []
    for plan in running_savings_plans(db, user_id, cfg):
        if plan["running"]:
            continue
        reason = "paused" if plan["not_running_reason"] == "paused" else "overdue"
        findings.append({
            "id": f"plan-{plan['broker']}-{plan['isin'] or plan['name']}",
            "kind": "savings_plan_stopped",
            "title": f"Savings plan {reason}: {plan['name']} ({plan['broker_label']})",
            "detail": "Buying through a fall is what makes the recovery count. A paused plan buys nothing at the lower prices.",
        })
    held = _core_units_by_isin(db, user_id, set(cfg["core_isins"]), date.fromisoformat(state.peak_date))
    for isin, (name, high, now) in sorted(held.items()):
        if high > 0 and (high - now) / high >= MIN_SOLD_SHARE:
            findings.append({
                "id": f"core-sold-{isin}",
                "kind": "core_sold",
                "title": f"Core ETF units sold during the fall: {name} ({(high - now) / high:.0%} of the position)",
                "detail": "Selling after a fall turns a paper loss into a real one and misses the way back. "
                          "If this was planned (a withdrawal, a rebalance), ignore this.",
            })
    return findings


# --- the whole picture ----------------------------------------------------------


def _message(state: MarketState) -> str:
    if state.level == "calm":
        return "Markets are within 10 % of their high. Nothing to do: keep the plan running."
    done = [e for e in state.episodes if e.recovery_date is not None]
    lines = [f"{index_name(state.ticker)} is {state.drawdown:.0%} below its high of {state.peak_date}."]
    if done:
        worst = max(done, key=lambda e: e.depth)
        longest = max(done, key=lambda e: e.months_to_recover or 0)
        lines.append(
            f"Since {state.history_start[:4] if state.history_start else 'the start of the data'}, "
            f"{len(done)} earlier fall{'s' if len(done) > 1 else ''} of 10 % or more all recovered; "
            f"the deepest was {worst.depth:.0%} ({worst.peak_date[:7]}), the slowest way back took "
            f"{longest.months_to_recover:.0f} months."
        )
    lines.append(
        "Your plan does not change: keep the savings plan running, do not sell the core. "
        "New money now buys more units for the same euros."
    )
    return " ".join(lines)


def crash_guard(db: Session, user_id: str) -> dict[str, Any]:
    from app.foundation.settings import get_public_settings

    state = market_state(db)
    note = str(get_public_settings(db).get("crash_plan_note") or "").strip()
    return {
        "market": {k: v for k, v in asdict(state).items() if k != "episodes"},
        "episodes": [asdict(e) for e in state.episodes],
        "message": _message(state),
        "plan_note": note or None,
        "behaviour": behaviour(db, user_id, state),
        "active": state.level != "calm",
    }


# --- the daily alert ------------------------------------------------------------


def _load_state(db: Session) -> dict[str, Any]:
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, STATE_KEY)
    return stored if isinstance(stored, dict) else {}


def run_daily(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Alert on a new worst level since the last high, the return to the high, and new behaviour findings.

    Only a level worse than any reached since the market was last at its
    high sends a message, so a market moving back and forth around 20 %
    alerts once, not on every crossing.
    """
    from app.foundation.models.entities import User
    from app.foundation.notify import alert_owners
    from app.foundation.settings import upsert_public_settings

    when = now or datetime.now(UTC)
    state = market_state(db)
    if state.peak is None:
        # No prices (an empty cache) is not a market at its high: send nothing
        # and keep the stored state, or the recovery message would fire on a gap.
        logger.warning("Crash guard skipped: no closes for %s", state.ticker)
        return {"level": None, "drawdown": None, "sent": [], "skipped": "no_prices"}
    previous = _load_state(db)
    if previous.get("ticker") and previous["ticker"] != state.ticker:
        # The benchmark was changed: the stored level, peak and findings belong
        # to the old index, so start over rather than compare across the two.
        previous = {}
    worst_level = str(previous.get("worst_level") or "calm")
    seen: set[str] = set(previous.get("behaviour_seen") or [])
    sent: list[str] = []

    # Back at the high: either today's close is the high, or a later high was set
    # since the last run (a missed day, a backfill) and a new fall started from it.
    new_high = bool(previous.get("peak_date")) and str(state.peak_date) > str(previous["peak_date"])
    if worst_level != "calm" and (state.drawdown == 0.0 or new_high):
        text = (f"Quantfolio crash guard: {index_name(state.ticker)} is back at its high. "
                "Anyone who kept the plan running came through it whole.")
        alert_owners(db, source="crash_guard", title="Markets back at their high", telegram_text=text,
                     body=text, severity="info", href="/plan")
        sent.append("recovered")
        worst_level = "calm"
        seen = set()
    elif new_high:
        seen = set()

    if LEVEL_ORDER[state.level] > LEVEL_ORDER.get(worst_level, 0):
        note = _note(db)
        text = f"Quantfolio crash guard: {state.level_label.lower()}.\n\n{_message(state)}"
        if note:
            text += f"\n\nYour own rule, written in calm times:\n{note}"
        alert_owners(db, source="crash_guard", title=f"{state.level_label}: {index_name(state.ticker)} {state.drawdown:.0%} below its high",
                     telegram_text=text, body=_message(state), severity="warning", href="/plan")
        sent.append(f"level:{state.level}")
        worst_level = state.level

    if state.level != "calm":
        for (user_id,) in db.query(User.id).filter(User.role == "admin").all():
            for finding in behaviour(db, user_id, state):
                # Per user: one admin's finding must not silence the same one for another.
                key = f"{user_id}:{finding['id']}"
                if key in seen:
                    continue
                text = f"Quantfolio crash guard: {finding['title']}.\n\n{finding['detail']}"
                alert_owners(db, source="crash_guard", title=finding["title"], telegram_text=text,
                             body=finding["detail"], severity="warning", href="/plan")
                seen.add(key)
                sent.append(finding["id"])

    upsert_public_settings(db, {STATE_KEY: {
        "ticker": state.ticker, "worst_level": worst_level, "behaviour_seen": sorted(seen),
        "peak_date": state.peak_date,
        "level": state.level, "drawdown": state.drawdown, "checked_at": when.isoformat(),
    }})
    return {"level": state.level, "drawdown": state.drawdown, "sent": sent}


def _note(db: Session) -> str | None:
    from app.foundation.settings import get_public_settings

    return str(get_public_settings(db).get("crash_plan_note") or "").strip() or None


def register_crash_guard_job(scheduler: Any | None = None) -> str:
    """Daily at 07:00 UTC, after the 06:30 price backfill."""
    from app.foundation.jobs import register_cron_job

    def _inner() -> None:
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as db:
            result = run_daily(db)
            logger.info("crash_guard_daily: %s", result)

    return register_cron_job("crash_guard_daily", _inner, scheduler=scheduler, hour=7, minute=0)
