"""The one message format of every event-desk CFD signal (ADR 0028), and its evidence line.

Built from stored rows only (no network read): the position, its journal row and the ``fill`` ledger event,
whose payload keeps the reason, the evidence numbers and whether the message is paper only. Text is plain,
neutral and honest about how strong its evidence is. Nothing here places an order.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskCfdJournal, DeskPosition
from app.foundation.settings import get_setting_row

from . import bankroll, cfd_instruments, scoring_events, service
from . import calendar as cal
from .event_limits import EVENT_LIMITS
from .ledger import events_for

REPLAY_KEY = "desk_signal_replay_json"
RESEARCH = {
    "ta_trend:cfd": "Research: moving-average crosses show little edge after costs in large caps and indices; this tests it.",
    "ta_band:cfd": "Research: fading a Bollinger break works in ranges and loses in strong trends; untested after costs here.",
    "ta_rsi2:cfd": "Research: short dips in an uptrend bounce, but costs eat most of it (honest out-of-sample Sharpe about 0.1-0.35).",
    "hype_fade:cfd": "Research: after retail herding, such stocks gave back about 5 % over 20 days (Barber, Huang, Odean, Schwarz).",
    "news_drift:cfd": "Research: real company news drifts for days in the same direction (Chan 2003; Jiang, Li, Wang 2021).",
    "owner_call:cfd": "Your own call: scored against a coin-flip placebo like every other cell.",
}


def verdict_word(s: dict[str, Any]) -> str:
    if s["n"] < s["n_min"]:
        return "too early"
    if s["mean_net"] is None or s["mean_net"] <= 0:
        return "failing"
    return "passing" if s.get("passes") else "not significant"


def cell_state(db: Session, cell: str) -> dict[str, Any]:
    card = scoring_events.scorecard_events(db)
    s = dict(card["cells"].get(cell) or {"n": 0, "n_min": card["n_min"], "mean_net": None})
    s["n_min"] = card["n_min"]
    s["verdict"] = verdict_word(s)
    return s


def evidence_line(db: Session, cell: str) -> str:
    raw = get_setting_row(db, REPLAY_KEY)
    try:
        replay = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        replay = None
    rp = (replay or {}).get(cell) if isinstance(replay, dict) else None
    if rp and rp.get("n"):
        r = f"replay 2016-26 n={rp['n']}, {Decimal(str(rp['mean_eur'])):+.2f} EUR a trade"
    else:
        r = "replay 2016-26 n/a (no history for this rule)"
    s = cell_state(db, cell)
    mean = f", {s['mean_net']:+.2f} EUR a trade" if s["mean_net"] is not None else ""
    return f"Evidence: {r} | live paper {s['n']} trades{mean} ({s['verdict']})."


def paper_only_reason(db: Session, user_id: str, cell: str, now: datetime) -> str:
    """Why this message carries no order details, or ``""``: the bankroll stop, or a failing cell."""
    g = bankroll.guard(db, user_id, now)
    if not g.ok:
        return f"Bankroll stop: {g.reason}. Paper only until {g.resumes}."
    if cell_state(db, cell)["verdict"] == "failing":
        return "This cell is failing its own test (40 or more paper trades, mean at or below zero). Paper only."
    return ""


def _label(position: DeskPosition, journal: DeskCfdJournal) -> str:
    key = journal.market_key
    return key.split(":", 1)[1] if ":" in key else key


def _fill_payload(db: Session, position: DeskPosition) -> dict[str, Any]:
    rows = events_for(db, position.id, "fill")
    if not rows:
        return {}
    try:
        return json.loads(rows[0].payload)
    except ValueError:
        return {}


def _inst(db: Session, journal: DeskCfdJournal) -> cfd_instruments.CfdInstrument | None:
    return cfd_instruments.by_key(journal.market_key) or cfd_instruments.for_symbol(
        db, journal.market_key.split(":", 1)[-1])


def open_text(db: Session, position: DeskPosition, journal: DeskCfdJournal, now: datetime) -> str:
    fill = _fill_payload(db, position)
    inst = _inst(db, journal)
    cell = position.cell or ""
    only = fill.get("paper_only") or ""
    if not only and position.status == "open" and journal.status == "sent":
        # Rebuilt on every tick: a bankroll stop (or a failing cell) since the fill switches a trade the owner
        # has not entered yet to paper only. One already entered keeps its stop: the owner needs it.
        only = paper_only_reason(db, position.user_id, cell, now)
    side = position.direction
    name = inst.t212_name if inst else journal.market_key
    lines = [f"{_label(position, journal)} {side}: CFD, manual (Trading 212: {name})",
             f"Why: {fill.get('reason') or cell}", RESEARCH.get(cell, ""), evidence_line(db, cell)]
    qty = Decimal(journal.quantity_suggested or 0)
    exposure = Decimal(journal.exposure_eur or 0)
    entry = Decimal(journal.index_level_est or 0)
    if inst is not None and not only:
        margin = exposure * inst.margin_rate
        lines.append(f"Quantity {qty:.4f} (~ {exposure:,.0f} EUR, margin ~ {margin:,.0f} EUR, leverage "
                     f"{Decimal(1) / inst.margin_rate:.0f}x). Loss at the stop ~ {fill.get('loss_at_stop', '5')} EUR, "
                     "overnight to the exit date included.")
        pts = Decimal(journal.stop_points or 0)
        stop = entry - pts if side == "long" else entry + pts
        lines.append(f"Stop: {stop:,.2f} ({fill.get('stop_text', 'ATR stop')}). Exit: by {_exit_text(position)}, "
                     "or when I tell you.")
    elif inst is not None:
        lines.append(f"Exit (paper): by {_exit_text(position)}.")
    if inst is not None:
        assumed = " (assumed)" if inst.spread_assumed else ""
        night = exposure * inst.overnight_rate(side)
        fx = ", 0.5 % FX on the result" if inst.usd else ""
        lines.append(f"Costs: spread ~ {inst.spread_bp / 100:.2f} %{assumed}, overnight ~ {night:,.2f} EUR per night "
                     f"on {exposure:,.0f} EUR{fx}.")
        if inst.kind == "index" and fill.get("expiry"):
            lines.append(f"Contract rolls {fill['expiry']}; I close the paper trade an hour before and tell you.")
    g = bankroll.real_results(db, position.user_id, now)
    lines.append(f"Bankroll: month {g.month_eur:+.0f} EUR of the {EVENT_LIMITS.monthly_real_loss_stop:.0f} EUR stop.")
    if only:
        lines.append(only)
    lines.append(f"Paper-scored as {cell}. If Trading 212 has no CFD on {_label(position, journal)}, skip; "
                 "nothing is ordered for you.")
    return "\n".join(x for x in lines if x)


def _exit_text(position: DeskPosition) -> str:
    at = service._aware(position.max_exit_at)
    return at.astimezone(cal.BERLIN).strftime("%Y-%m-%d at %H:%M Berlin") if at else "?"


def live_text(db: Session, position: DeskPosition, journal: DeskCfdJournal, now: datetime) -> str:
    from . import cfd

    parts = [open_text(db, position, journal, now)]
    if position.status == "closed":
        end = service._aware(position.exit_at)
        hhmm = end.astimezone(cal.BERLIN).strftime("%H:%M") if end else "?"
        parts.insert(0, f"CLOSED {hhmm}: paper result {Decimal(position.net_eur or 0):+.2f} EUR after costs "
                        f"({position.exit_reason})")
    else:
        last, entry = Decimal(position.last_bid or 0), Decimal(position.entry_price or 0)
        if last > 0 and entry > 0 and last != entry:
            move = (last / entry - 1) * (1 if position.direction == "long" else -1)
            pnl = move * entry * Decimal(position.quantity or 0)
            parts.append(f"Now ({now.astimezone(cal.BERLIN):%H:%M}): paper {pnl:+.2f} EUR before costs.")
    extra = cfd._journal_line(journal)
    if extra:
        parts.append("\n".join(extra))
    return "\n".join(parts)


def close_text(db: Session, position: DeskPosition, reason: str, journal: DeskCfdJournal | None) -> str:
    label = (position.product_name or "CFD").replace(" CFD (Trading 212, manual)", "")
    why = {"stop": "stop-loss level reached", "time": "the holding time is up", "halt": "desk halted",
           "owner_close": "you closed it", "rollover": "its contract rolls within the hour: close yours too",
           "opposite_cross": "the EMA cross reversed", "middle_band": "the price is back at the middle band",
           "rsi_50": "RSI(2) crossed 50"}.get(reason, reason)
    text = (f"{label}: CLOSE now ({why}). Paper result {Decimal(position.net_eur or 0):+.2f} EUR after costs "
            f"({position.cell}).")
    if journal is not None and journal.status in ("entered", "closed") and journal.exit_level is None:
        missing = "<exit price>" if journal.entry_level is not None else "<entry price> <exit price>"
        text += (f"\nTo log your trade ({position.id[:8]}), reply to this message with: {missing}, as Trading 212 "
                 "shows them.")
    return text
