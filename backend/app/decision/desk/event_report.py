"""What the Signals page and the weekly digest show (ADR 0028): both registrations' cells, the owner's own
trades beside the paper ones, the open event positions and the bankroll. Plain dicts, read only."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskCfdJournal, DeskPosition
from app.foundation.settings import get_setting_row

from . import bankroll, cfd, cfd_journal, event_messages, pair_shadow, prereg, prereg_events, scoring, scoring_events
from . import calendar as cal
from .cell_names import cell_name
from .event_limits import EVENT_LIMITS

REPLAY_KEY = event_messages.REPLAY_KEY


def _replay(db: Session) -> dict[str, Any]:
    raw = get_setting_row(db, REPLAY_KEY)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _cell_row(cell: str, s: dict[str, Any], registration: str, replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "cell": cell, "name": cell_name(cell), "registration": registration, "n": s["n"], "n_min": s["n_min"], "mean_net": s["mean_net"],
        "hit_rate": s["hit_rate"], "p_holm": s.get("p_holm"), "passes": bool(s.get("passes")),
        "verdict": event_messages.verdict_word(s), "placebo_percentile": s.get("placebo_percentile"),
        "placebo_p95": s.get("placebo_p95"), "replay": replay.get(cell),
    }


def _journal(db: Session, user_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).order_by(
        DeskCfdJournal.created_at.desc()).limit(200).all()
    out, per_cell = [], {}
    for j in rows:
        p = db.get(DeskPosition, j.position_id)
        cell = p.cell if p is not None else None
        real = None if j.real_pnl_eur is None else float(j.real_pnl_eur)
        paper = None if p is None or p.net_eur is None else float(p.net_eur)
        out.append({"position_id": j.position_id, "cell": cell, "market_key": j.market_key, "status": j.status,
                    "real_pnl_eur": real, "paper_net_eur": paper, "rule": j.rule,
                    "created_at": j.created_at.isoformat() if j.created_at else None})
        if cell and real is not None and j.rule == "registered":  # a manual plan is not this cell's trade
            c = per_cell.setdefault(cell, {"n": 0, "real_total": 0.0, "paper_total_same_trades": 0.0})
            c["n"] += 1
            c["real_total"] += real
            c["paper_total_same_trades"] += paper or 0.0
    return out[:50], per_cell


def _open(db: Session) -> list[dict[str, Any]]:
    out = []
    for p in db.query(DeskPosition).filter(DeskPosition.status == "open", DeskPosition.cell.in_(prereg_events.EVENT_CELLS)):
        entry, last = Decimal(p.entry_price or 0), Decimal(p.last_bid or 0)
        pnl = None
        if entry > 0 and last > 0:
            move = (last / entry - 1) * (1 if p.direction == "long" else -1)
            pnl = float((move * entry * Decimal(p.quantity or 0)).quantize(Decimal("0.01")))
        out.append({"id": p.id, "short_id": p.id[:8], "cell": p.cell, "instrument": p.underlying_label,
                    "direction": p.direction, "entry_price": str(p.entry_price), "stop_price": str(p.stop_price),
                    "last": None if not last else str(last), "paper_pnl_eur": pnl,
                    "max_exit_at": p.max_exit_at.isoformat() if p.max_exit_at else None})
    return out


def signals_overview(db: Session, user_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    replay = _replay(db)
    first = scoring.scorecard(db)
    events = scoring_events.scorecard_events(db)
    journal, per_cell = _journal(db, user_id)
    cells = [*(_cell_row(c, {**s, "n_min": first["n_min"]}, "first", replay) for c, s in first["cells"].items()),
             *(_cell_row(c, {**s, "n_min": events["n_min"]}, "events", replay) for c, s in events["cells"].items())]
    for row in cells:
        row["real"] = per_cell.get(row["cell"])
    g = bankroll.guard(db, user_id, now)
    return {
        "estimate": True, "not_tax_advice": True,
        "registrations": {"first": {"hash": prereg.PREREG_HASH, "registered_at": first["registered_at"]},
                          "events": {"hash": prereg_events.EVENT_HASH, "registered_at": events["registered_at"]}},
        "cells": cells, "open_positions": _open(db), "journal": journal,
        "bankroll": {"month_eur": str(g.month_eur), "day_eur": str(g.day_eur), "ok": g.ok, "reason": g.reason,
                     "resumes": g.resumes, "pot": str(EVENT_LIMITS.pot),
                     "monthly_stop": str(EVENT_LIMITS.monthly_real_loss_stop),
                     "daily_stop": str(EVENT_LIMITS.daily_real_loss_stop), "max_open": EVENT_LIMITS.max_open,
                     "open_real": bankroll.real_results(db, user_id, now).open_real},
        "owner_call": next((c for c in cells if c["cell"] == "owner_call:cfd"), None),
        "pair_shadow": pair_shadow.report(db),
        "limits": EVENT_LIMITS.as_json(),
    }


def weekly_digest(db: Session, user_id: str, now: datetime | None = None) -> str:
    """The Sunday message: each cell's n, mean and verdict, the owner's real result, best and worst trade."""
    now = now or datetime.now(UTC)
    ov = signals_overview(db, user_id, now)
    lines = ["Desk week (paper cells; nothing here is advice):"]
    scored = [c for c in ov["cells"] if c["n"]]
    for c in scored:
        mean = "-" if c["mean_net"] is None else f"{c['mean_net']:+.2f} EUR"
        lines.append(f"- {cell_name(c['cell'])}: {c['n']} trades, mean {mean} ({c['verdict']})")
    if not scored:
        lines.append("- No cell has a closed paper trade yet.")
    week_ago = now - timedelta(days=7)
    month_start = now.astimezone(cal.BERLIN).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id, DeskCfdJournal.status == "closed",
                                           DeskCfdJournal.real_pnl_eur.isnot(None)).all()
    week = [r for r in rows if r.closed_at and cfd_journal._aware(r.closed_at) >= week_ago]
    month = [r for r in rows if r.closed_at and cfd_journal._aware(r.closed_at) >= month_start]
    lines.append(f"Your logged trades: week {sum(Decimal(str(r.real_pnl_eur)) for r in week):+.2f} EUR ({len(week)}), "
                 f"month {sum(Decimal(str(r.real_pnl_eur)) for r in month):+.2f} EUR ({len(month)}).")
    paper = db.query(DeskPosition).filter(DeskPosition.status == "closed", DeskPosition.cell.in_(
        [*prereg_events.EVENT_CELLS, cfd.CELL]), DeskPosition.exit_at >= week_ago, DeskPosition.net_eur.isnot(None)).all()
    if paper:
        best, worst = max(paper, key=lambda p: Decimal(str(p.net_eur))), min(paper, key=lambda p: Decimal(str(p.net_eur)))
        lines.append(f"Paper best: {best.underlying_label} {best.net_eur:+.2f} EUR ({cell_name(best.cell)}); "
                     f"worst: {worst.underlying_label} {worst.net_eur:+.2f} EUR ({cell_name(worst.cell)}).")
    else:
        lines.append("No paper trades closed this week.")
    held = ov["open_positions"]
    if held:
        lines.append("Still open: " + ", ".join(
            f"{h['instrument']} {h['direction']} ({cell_name(h['cell'])})" for h in held) + ".")
    else:
        lines.append("Still open: nothing.")
    pair = ov["pair_shadow"]
    if pair["days_same_side"]:
        lines.append(
            f"DAX + ES50 on the same side ({pair['days_same_side']} days, shadow): mean {pair['mean_full_eur']:+.2f} EUR "
            f"at full size, {pair['mean_half_eur']:+.2f} at half size per leg; worst day {pair['worst_full_eur']:+.2f} / "
            f"{pair['worst_half_eur']:+.2f}.")
    b = ov["bankroll"]
    lines.append(f"Bankroll: month {Decimal(b['month_eur']):+.0f} of the {b['monthly_stop']} EUR stop."
                 + ("" if b["ok"] else f" Paper only until {b['resumes']}."))
    return "\n".join(lines)
