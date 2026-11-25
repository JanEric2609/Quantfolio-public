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

from . import bankroll, cfd_instruments, cfd_journal, scoring_events, service
from . import calendar as cal
from .cell_names import cell_name
from .replay_store import REPLAY_KEY, replay_summary  # noqa: F401 - re-exported
from .event_limits import EVENT_LIMITS
from .ledger import events_for

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


#: A rule whose 2016-26 replay (``scripts/signal_replay.py``) loses after costs at least this clearly
#: (t-value of the mean per trade, over at least ``REPLAY_MIN_N`` trades) is sent as paper only: the desk
#: still scores it exactly as registered, but its message carries no quantity or stop to act on.
REPLAY_LOSS_T = -2.0
REPLAY_MIN_N = 100


def replay_loses(db: Session, cell: str) -> str:
    """``""``, or the reason the replay rules the cell out for real money."""
    rp = replay_summary(db, cell)
    try:
        n, mean, t = int(rp["n"]), float(rp["mean_eur"]), float(rp["t"])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return ""
    if n >= REPLAY_MIN_N and mean < 0 and t <= REPLAY_LOSS_T:
        return (f"The 2016-26 replay loses after costs ({mean:+.2f} EUR a trade over {n:,} trades, t {t:.1f}). "
                "Paper only.")
    return ""


def evidence_line(db: Session, cell: str) -> str:
    rp = replay_summary(db, cell)
    if rp and rp.get("n"):
        r = f"replay 2016-26 n={rp['n']}, {Decimal(str(rp['mean_eur'])):+.2f} EUR a trade"
    else:
        r = "replay 2016-26 n/a (no history for this rule)"
    s = cell_state(db, cell)
    mean = f", {s['mean_net']:+.2f} EUR a trade" if s["mean_net"] is not None else ""
    return f"Evidence: {r} | live paper {s['n']} trades{mean} ({s['verdict']})."


def paper_only_reason(db: Session, user_id: str, cell: str, now: datetime) -> str:
    """Why this message carries no order details, or ``""``: the bankroll stop, a failing cell, or a replay
    that loses after costs (:func:`replay_loses`)."""
    g = bankroll.guard(db, user_id, now)
    if not g.ok:
        return f"Bankroll stop: {g.reason}. Paper only until {g.resumes}."
    if cell_state(db, cell)["verdict"] == "failing":
        return (f"This rule is failing its own test ({cell_state(db, cell)['n_min']} or more paper trades, mean at "
                "or below zero). Paper only.")
    return replay_loses(db, cell)


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
    head = f"{_label(position, journal)} {side}: CFD, manual (Trading 212: {name})"
    # The action first (what to do, how big, where the stop is), the evidence after it. A message without
    # order details says so in its first line, not at the bottom.
    lines = [head + (" - PAPER ONLY, no order details" if only else ""),
             f"Why: {fill.get('reason') or cell_name(cell)}"]
    qty = Decimal(journal.quantity_suggested or 0)
    exposure = Decimal(journal.exposure_eur or 0)
    entry = Decimal(journal.index_level_est or 0)
    if inst is not None and not only:
        margin = exposure * inst.margin_rate
        lines.append(f"Quantity {qty:.4f} (~ {exposure:,.0f} EUR, margin ~ {margin:,.0f} EUR, leverage "
                     f"{Decimal(1) / inst.margin_rate:.0f}x). Loss at the stop ~ "
                     f"{fill.get('loss_at_stop', EVENT_LIMITS.risk_per_trade)} EUR, overnight to the exit date included.")
        pts = Decimal(journal.stop_points or 0)
        stop = entry - pts if side == "long" else entry + pts
        lines.append(f"Stop: {stop:,.2f} ({fill.get('stop_text', 'ATR stop')}). Exit: by {_exit_text(position)}; "
                     "the desk tells you if it ends sooner.")
    elif inst is not None:
        lines.append(f"Exit (paper): by {_exit_text(position)}.")
    lines += [RESEARCH.get(cell, ""), evidence_line(db, cell)]
    if inst is not None:
        assumed = " (assumed)" if inst.spread_assumed else ""
        night = exposure * inst.overnight_rate(side)
        fx = ", 0.5 % FX on the result" if inst.usd else ""
        lines.append(f"Costs: spread ~ {inst.spread_bp / 100:.2f} %{assumed}, overnight ~ {night:,.2f} EUR per night "
                     f"on {exposure:,.0f} EUR{fx}.")
        if inst.kind == "index" and fill.get("expiry"):
            lines.append(f"Contract rolls {fill['expiry']}; the desk closes the paper trade an hour before and "
                         "tells you.")
    g = bankroll.real_results(db, position.user_id, now)
    lines.append(f"Bankroll: month {g.month_eur:+.0f} EUR of the {EVENT_LIMITS.monthly_real_loss_stop:.0f} EUR stop.")
    if only:
        lines.append(only)
    lines.append(f"Paper-scored as {cell_name(cell)}. If Trading 212 has no CFD on {_label(position, journal)}, "
                 "skip; nothing is ordered for you.")
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
    result = f"Paper result {Decimal(position.net_eur or 0):+.2f} EUR after costs ({cell_name(position.cell)})."
    if journal is not None and journal.status == "skipped":
        # The owner said they did not take it: nothing to close, the paper score only.
        return f"{label}: paper trade closed ({why.removesuffix(': close yours too')}). {result}"
    own = _own_stop_not_reached(position, reason, journal)
    if own is not None:
        # The owner filled at another price, so their stop sits elsewhere: the paper stop is not theirs.
        text = (f"{label}: the paper trade was stopped out at {own[0]}. Your own stop for your fill, {own[1]}, is "
                f"not reached, so this is not a close signal for your trade. The desk no longer tracks it: keep "
                f"your stop in Trading 212 and the planned exit ({_exit_text(position)}), or close it when you "
                f"choose. {result}")
    else:
        # Kept under the bankroll stop on purpose: closing only lowers the exposure, and the owner may hold the
        # trade without having logged it yet. The stop withholds new entries, never the exit.
        text = f"{label}: CLOSE now ({why}). {result}"
    if journal is not None and journal.status in ("entered", "closed") and journal.exit_level is None:
        missing = "<exit price>" if journal.entry_level is not None else "<entry price> <exit price>"
        text += (f"\nTo log your trade ({position.id[:8]}), reply to this message with: {missing}, as Trading 212 "
                 "shows them.")
    return text


def _own_stop_not_reached(position: DeskPosition, reason: str, journal: DeskCfdJournal | None
                          ) -> tuple[str, str] | None:
    """On a paper stop of a trade the owner entered with a fill: (paper level, own stop) as text when the
    owner's own stop is not reached at the closing price, else ``None``. Levels are in the units Trading 212
    shows (the index level, or the share price): the paper price is scaled by the signal's level."""
    if reason != "stop" or journal is None or journal.status != "entered":
        return None
    own = cfd_journal.stop_level(journal)
    entry, px, level = (Decimal(position.entry_price or 0), Decimal(position.exit_price or 0),
                        Decimal(journal.index_level_est or 0))
    if own is None or entry <= 0 or px <= 0 or level <= 0:
        return None
    now_level = level * px / entry
    reached = now_level <= own if position.direction == "long" else now_level >= own
    if reached:
        return None
    nd = 2 if ":" in (journal.market_key or "") else 0
    return f"{now_level:,.{nd}f}", f"{own:,.{nd}f}"
