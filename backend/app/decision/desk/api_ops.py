"""What the HTTP layer needs from the desk, as plain-dict reads and two actions (ADR 0027)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.desk_chain import verify_chain, verify_chain_cached
from app.foundation.models.entities import DeskCfdJournal, DeskEvent, DeskPosition
from app.foundation.settings import get_setting_row
from app.foundation.scalable import market
from app.foundation.scalable import service as scalable_service
from app.foundation.scalable.cli import ScalableError

from . import calendar as cal
from . import cfd, cfd_journal, intraday, prereg, service
from .limits import LIMITS
from .signals import owner_calls
from .underlyings import Underlying


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def position_dict(p: DeskPosition) -> dict[str, Any]:
    def num(v: Any) -> str | None:
        return None if v is None else str(v)

    return {
        "id": p.id, "short_id": p.id[:8], "mode": p.mode, "cell": p.cell, "signal": p.signal,
        "product_type": p.product_type, "isin": p.isin, "issuer": p.issuer, "underlying": p.underlying_label,
        "direction": p.direction, "liquid": p.liquid, "units": p.units, "entry_price": num(p.entry_price),
        "stop_price": num(p.stop_price), "entry_at": _iso(p.entry_at), "max_exit_at": _iso(p.max_exit_at),
        "exit_price": num(p.exit_price), "exit_at": _iso(p.exit_at), "exit_reason": p.exit_reason,
        "net_eur": num(p.net_eur), "status": p.status, "would_take_live": p.would_take_live,
        "last_bid": num(p.last_bid), "last_quote_at": _iso(p.last_quote_at), "created_at": _iso(p.created_at),
        "product_name": p.product_name, "wkn": p.wkn, "barrier_or_strike": num(p.barrier_or_strike),
        "expiry": p.expiry.isoformat() if p.expiry else None, "leverage_or_omega": num(p.leverage_or_omega),
        "quantity": num(p.quantity),
    }


def desk_status(db: Session, user_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    state = service.build_state(db, user_id, now)
    ok, bad = verify_chain_cached(db)  # polled every 30 s: new rows only, a full pass every 10 minutes
    reg = prereg.registered_at(db)
    return {
        "mode": "paper", "banner": "Paper trading: no real orders are placed", "limits": LIMITS.as_json(),
        "halt": service.halt_info(db), "health": service.health(db),
        "usage_today": {
            "entries": state.entries_today, "max_entries": LIMITS.max_entries_per_day,
            "loss": str(state.loss_today), "daily_loss": str(LIMITS.daily_loss),
            "open_live": state.open_live, "max_open_live": LIMITS.max_open_live,
            "pot_used": str(state.pot_used), "pot": str(LIMITS.pot),
        },
        "chain": {"ok": ok, "first_bad_seq": bad}, "prereg_hash": prereg.PREREG_HASH,
        "registered_at": _iso(reg), "macro_calendar_years": sorted(cal.MACRO_EVENTS_BY_YEAR),
    }


def list_positions(db: Session, user_id: str, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """The owner's positions, newest first. Without *status*: every open one, then the newest *limit*
    others, never the logged external trades (a first reconcile adds hundreds at once and would push open
    positions, and their Close buttons, off the page; the scorecard counts them)."""
    query = db.query(DeskPosition).filter(DeskPosition.user_id == user_id)
    if status:
        return [position_dict(p) for p in
                query.filter(DeskPosition.status == status).order_by(DeskPosition.created_at.desc()).limit(limit)]
    open_rows = query.filter(DeskPosition.status == "open").order_by(DeskPosition.created_at.desc()).all()
    rest = query.filter(DeskPosition.status != "open", DeskPosition.signal != "external") \
        .order_by(DeskPosition.created_at.desc()).limit(limit).all()
    return [position_dict(p) for p in [*open_rows, *rest]]


def list_events(db: Session, after_seq: int = 0, limit: int = 200, *, latest: bool = False) -> list[dict[str, Any]]:
    q = db.query(DeskEvent).filter(DeskEvent.seq > after_seq)
    if latest:
        rows = list(reversed(q.order_by(DeskEvent.seq.desc()).limit(limit).all()))
    else:
        rows = q.order_by(DeskEvent.seq).limit(limit).all()
    return [{"seq": e.seq, "created_at": _iso(e.created_at), "kind": e.kind, "mode": e.mode,
             "position_id": e.position_id, "payload": json.loads(e.payload), "row_hash": e.row_hash}
            for e in rows]


def list_news(db: Session, limit: int = 50, sent_only: bool = False) -> list[dict[str, Any]]:
    """The news wire, newest first (alert-only: nothing trades on it)."""
    from app.foundation.models.entities import NewsWireItem

    q = db.query(NewsWireItem)
    if sent_only:
        q = q.filter(NewsWireItem.alert == "sent")
    rows = q.order_by(NewsWireItem.received_at.desc()).limit(limit).all()
    return [{"id": r.id, "source": r.source, "published_at": _iso(r.published_at), "received_at": _iso(r.received_at),
             "headline": r.headline, "symbols": [s for s in r.symbols.split(",") if s], "url": r.url,
             "relevance": r.relevance, "materiality": r.materiality, "direction": r.direction, "scope": r.scope,
             "rated_ms": r.rated_ms, "alert": r.alert} for r in rows]


def chain_status(db: Session) -> dict[str, Any]:
    ok, bad = verify_chain(db)
    return {"ok": ok, "first_bad_seq": bad, "events": db.query(DeskEvent).count()}


class DeskActionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _cli(db: Session):
    try:
        return scalable_service.make_cli(db)
    except ScalableError as exc:
        raise DeskActionError(409, f"Scalable is not available: {exc.code}") from exc


def submit_idea(db: Session, user_id: str, text: str) -> dict[str, Any]:
    """The same path as ``/idea`` on Telegram; the tap becomes a confirm within 180 s."""
    if service.is_halted(db):
        raise DeskActionError(409, "The desk is halted.")
    try:
        idea = owner_calls.parse_idea(text if text.startswith("/idea") else "/idea " + text)
    except owner_calls.IdeaParseError as exc:
        raise DeskActionError(422, str(exc)) from exc
    try:
        outcome = owner_calls.start_idea(db, user_id, idea, _cli(db), datetime.now(UTC), clock=_wall_clock)
    except market.WrapperOutdated as exc:
        db.rollback()  # a half-built idea is never stored
        raise DeskActionError(409, "wrapper_outdated: run infra/scalable/install.sh again") from exc
    except ScalableError as exc:
        db.rollback()
        raise DeskActionError(502, f"Could not read the market: {exc.code}") from exc
    db.commit()
    if outcome.error:
        raise DeskActionError(422, outcome.error)
    out = []
    for pt, res in zip(idea.product_types, outcome.proposals, strict=True):
        out.append({
            "product_type": pt, "reason": res.reason, "nonce": res.nonce,
            "position": position_dict(res.position) if res.position else None, "news": res.notes or [],
            "message": owner_calls.format_proposal(res, outcome.underlying or Underlying("", "", "XETR", False), pt),
        })
    return {"proposals": out, "tap_window_s": LIMITS.tap_window_s}


def take_proposal(db: Session, user_id: str, position_id: str, nonce: str) -> dict[str, Any]:
    position = db.get(DeskPosition, position_id)
    if position is None or position.user_id != user_id:
        raise DeskActionError(404, "Unknown proposal.")
    underlying = service.underlying_of(position)
    try:
        result = service.take(db, position_id, nonce, _cli(db), datetime.now(UTC), underlying, clock=_wall_clock)
    except ScalableError as exc:
        db.rollback()
        raise DeskActionError(502, f"Could not read the market: {exc.code}") from exc
    db.commit()
    return {"status": result.status, "message": result.message, "nonce": result.nonce,
            "position": position_dict(result.position) if result.position else None}


def close_position_api(db: Session, user_id: str, position_id: str) -> dict[str, Any]:
    position = db.get(DeskPosition, position_id)
    if position is None or position.user_id != user_id:
        raise DeskActionError(404, "Unknown position.")
    underlying = service.underlying_of(position)
    try:
        ok = cfd.close_any(db, position, _cli(db), datetime.now(UTC), underlying)
    except ScalableError as exc:
        db.rollback()
        raise DeskActionError(502, f"Could not read the market: {exc.code}") from exc
    db.commit()
    return {"closed": ok, "position": position_dict(position)}


def _journal_dict(db: Session, j: DeskCfdJournal) -> dict[str, Any]:
    p = db.get(DeskPosition, j.position_id)

    def num(v: Any) -> str | None:
        return None if v is None else str(v)

    stop = cfd_journal.stop_level(j)
    return {
        "position_id": j.position_id, "short_id": j.position_id[:8], "created_at": _iso(j.created_at),
        "market_key": j.market_key, "direction": j.direction, "currency": j.currency, "status": j.status,
        "r1": num(j.r1), "index_level_est": num(j.index_level_est), "quantity_suggested": num(j.quantity_suggested),
        "quantity": num(j.quantity), "exposure_eur": num(j.exposure_eur), "stop_points": num(j.stop_points),
        "stop_level": num(stop), "entry_level": num(j.entry_level), "exit_level": num(j.exit_level),
        "entered_at": _iso(j.entered_at), "closed_at": _iso(j.closed_at), "real_pnl_eur": num(j.real_pnl_eur),
        "paper_net_eur": num(p.net_eur) if p else None, "paper_status": p.status if p else None,
        "nights_held": cfd_journal.nights_held(j), "note": j.note, "rule": j.rule,
    }


def intraday_view(db: Session, user_id: str) -> dict[str, Any]:
    """The Intraday page's data: the board snapshot, Trading 212's facts and the owner's last 30 journal rows."""
    stored = get_setting_row(db, intraday.BOARD_KEY)
    board = {k: v for k, v in stored.items() if k != "ctx"} if isinstance(stored, dict) else None
    age = None
    if board and board.get("as_of"):
        try:
            age = max(0, int((datetime.now(UTC) - datetime.fromisoformat(board["as_of"])).total_seconds()))
        except ValueError:
            age = None
    t212 = {k: ({a: str(b) for a, b in v.items()} if isinstance(v, dict) else str(v)) for k, v in cfd.T212.items()}
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id) \
        .order_by(DeskCfdJournal.created_at.desc()).limit(30).all()
    return {"board": board, "board_age_s": age, "t212": t212, "journal": [_journal_dict(db, j) for j in rows]}


def journal_action(db: Session, user_id: str, position_id: str, action: str, level: Any, quantity: Any,
                   note: str | None) -> dict[str, Any]:
    """The owner's answer on a CFD signal: the same functions and checks as Telegram's buttons and ``/cfd``."""
    journal = cfd_journal.for_position(db, position_id)
    if journal is None or journal.user_id != user_id:
        raise DeskActionError(404, "Unknown trade.")
    now = datetime.now(UTC)
    try:
        if action == "entered":
            cfd_journal.mark_entered(db, journal, now, level, quantity)
        elif action == "skipped":
            cfd_journal.mark_skipped(db, journal, now)
        elif action == "closed":
            cfd_journal.mark_closed(db, journal, now, level)
        else:
            raise DeskActionError(422, "Unknown action.")
        if note is not None:
            journal.note = note[:200]
        db.commit()
    except cfd_journal.JournalError as exc:
        db.rollback()
        raise DeskActionError(422, str(exc)) from exc
    return _journal_dict(db, journal)
