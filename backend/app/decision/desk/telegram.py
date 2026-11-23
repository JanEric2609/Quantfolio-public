"""Telegram side of the desk: /idea, /desk, /stop, /close, /cfd and the "Paper: take" and CFD buttons (ADR 0027).

``/cfd`` is the owner's journal of the manual Trading 212 CFD trades: ``/cfd`` lists today's rows,
``/cfd in <price> [quantity] [id]``, ``/cfd out <price> [id]``, ``/cfd skip [id]``. The CFD message carries
the buttons ``cfd:in:<position id>`` (Entered), ``cfd:skip:<position id>`` (Skipped) and
``cfd:out:<position id>`` (Closed, after Entered). All of it is the owner's own record, never the ledger.

Registered at startup by ``register_desk_telegram()`` (worker and API); the
foundation's update handler never imports this package. Only an admin's linked
chat reaches these handlers. Resuming a halt is possible only from the app.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation import telegram_bot, telegram_updates
from app.foundation.models.entities import DeskPosition, User
from app.foundation.scalable import market
from app.foundation.scalable import service as scalable_service
from app.foundation.scalable.cli import ScalableError

from . import cfd, cfd_journal, service
from .guard import DeskState
from .limits import LIMITS
from .signals import owner_calls


#: ``/close`` takes the 8-character id ``/desk`` shows (or more of the id): hex digits and dashes only.
SHORT_ID = re.compile(r"[0-9a-f][0-9a-f-]{7,35}")


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _is_admin(db: Session, account: Any) -> bool:
    user = db.get(User, account.user_id)
    return bool(user and user.role == "admin")


def _say(db: Session, chat_id: str, text: str, markup: dict[str, Any] | None = None) -> None:
    telegram_bot.send_telegram_message(db, chat_id, text, markup) if markup else telegram_bot.send_telegram_message(
        db, chat_id, text)


def handle_idea(db: Session, account: Any, chat_id: str, text: str) -> None:
    if not _is_admin(db, account):
        return _say(db, chat_id, "The desk is for the admin account.")
    try:
        idea = owner_calls.parse_idea(text)
    except owner_calls.IdeaParseError as exc:
        return _say(db, chat_id, str(exc))
    if service.is_halted(db):
        return _say(db, chat_id, "The desk is halted. Resume it in the app first.")
    try:
        outcome = owner_calls.start_idea(db, account.user_id, idea, scalable_service.make_cli(db),
                                         datetime.now(UTC), clock=_wall_clock)
    except market.WrapperOutdated:
        db.rollback()  # a half-built idea (one product proposed, the other failed) is never stored
        return _say(db, chat_id, "The sc wrapper needs install.sh again before the desk can read quotes.")
    except ScalableError as exc:
        db.rollback()
        return _say(db, chat_id, f"Could not read the market: {exc.code}.")
    db.commit()
    if outcome.error or outcome.underlying is None:
        return _say(db, chat_id, outcome.error or "Unknown underlying.")
    for pt, result in zip(idea.product_types, outcome.proposals, strict=True):
        message = owner_calls.format_proposal(result, outcome.underlying, pt)
        if result.position is not None and result.nonce:
            _say(db, chat_id, message, owner_calls.take_markup(result.position.id, result.nonce))
        else:
            _say(db, chat_id, message)


def handle_take(db: Session, account: Any, chat_id: str, data: str) -> str:
    parts = data.split(":")
    if len(parts) != 4:
        return "Bad button."
    position = db.get(DeskPosition, parts[2])
    if position is not None and position.user_id != account.user_id:
        return "Unknown proposal."  # another account's button (as the API's take answers 404)
    # The position's own underlying (an index alias, or the stock on its own exchange: a US stock is XNYS).
    underlying = service.underlying_of(position) if position else None
    try:
        cli = scalable_service.make_cli(db)  # no Scalable session is a ScalableError too
        result = service.take(db, parts[2], parts[3], cli, datetime.now(UTC), underlying, clock=_wall_clock)
    except ScalableError as exc:
        db.rollback()
        return f"Market read failed: {exc.code}"
    db.commit()
    if result.status == "reask" and result.position is not None and result.nonce:
        _say(db, chat_id, f"The price moved: new ask {result.position.entry_price:.4f}, {result.position.units} units, "
                          f"stop {result.position.stop_price:.4f}. Tap again to take it.",
             owner_calls.take_markup(result.position.id, result.nonce))
    return result.message


def handle_desk(db: Session, account: Any, chat_id: str, text: str) -> None:
    if not _is_admin(db, account):
        return _say(db, chat_id, "The desk is for the admin account.")
    now = datetime.now(UTC)
    state: DeskState = service.build_state(db, account.user_id, now)
    open_rows = db.query(DeskPosition).filter(DeskPosition.user_id == account.user_id,
                                              DeskPosition.status == "open").all()
    lines = [
        "Desk (PAPER only, no real orders)" + (" - HALTED" if state.halted else ""),
        f"Live rehearsal today: entries {state.entries_today}/{LIMITS.max_entries_per_day}, "
        f"loss {state.loss_today:.2f}/{LIMITS.daily_loss} EUR, open {state.open_live}/{LIMITS.max_open_live}",
        f"Open paper positions: {len(open_rows)}",
    ]
    for p in open_rows[:10]:
        if p.product_type == "cfd":
            j = cfd_journal.for_position(db, p.id)
            size = f"{(j.quantity_suggested if j and j.quantity_suggested is not None else p.quantity):.4f} (manual CFD)"
        else:
            size = f"{p.units}x {p.wkn or p.isin}"
        lines.append(f"  {p.id[:8]} {p.underlying_label} {p.direction} {p.product_type} {size} stop {p.stop_price}")
    _say(db, chat_id, "\n".join(lines))


def handle_stop(db: Session, account: Any, chat_id: str, text: str) -> None:
    if not _is_admin(db, account):
        return _say(db, chat_id, "The desk is for the admin account.")
    reason = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else "stopped from Telegram"
    service.set_halt(db, True, reason, "telegram")
    _say(db, chat_id, "Desk halted. Open paper positions close at the next tick. Resume only from the app.")


def handle_close(db: Session, account: Any, chat_id: str, text: str) -> None:
    if not _is_admin(db, account):
        return _say(db, chat_id, "The desk is for the admin account.")
    parts = text.split()
    if len(parts) != 2 or not SHORT_ID.fullmatch(parts[1].lower()):
        return _say(db, chat_id, "Usage: /close <8-character id from /desk>")
    # A literal prefix (no LIKE wildcards) of the owner's own open positions.
    prefix = parts[1].lower()
    matches = [p for p in db.query(DeskPosition).filter(DeskPosition.user_id == account.user_id,
                                                        DeskPosition.status == "open").all()
               if p.id.lower().startswith(prefix)]
    if len(matches) != 1:
        return _say(db, chat_id, "No single open position with that id.")
    pos = matches[0]
    try:
        ok = cfd.close_any(db, pos, scalable_service.make_cli(db), datetime.now(UTC), service.underlying_of(pos))
    except ScalableError as exc:
        db.rollback()
        return _say(db, chat_id, f"Could not read the market: {exc.code}.")
    db.commit()
    _say(db, chat_id, f"Closed at {pos.exit_price} (net {pos.net_eur} EUR)." if ok else "Could not close it.")


def cfd_markup(journal: Any) -> dict[str, Any] | None:
    """The buttons for a journal row's state: Entered/Skipped while unanswered, Closed once entered."""
    if journal is None:
        return None
    pid = journal.position_id
    if journal.status == "sent":
        return {"inline_keyboard": [[{"text": "Entered", "callback_data": f"cfd:in:{pid}"},
                                     {"text": "Skipped", "callback_data": f"cfd:skip:{pid}"}]]}
    if journal.status == "entered":
        return {"inline_keyboard": [[{"text": "Closed", "callback_data": f"cfd:out:{pid}"}]]}
    return None


def handle_cfd_button(db: Session, account: Any, chat_id: str, data: str) -> str:
    parts = data.split(":")
    if len(parts) != 3 or parts[1] not in ("in", "skip", "out"):
        return "Bad button."
    if not _is_admin(db, account):
        return "The desk is for the admin account."
    journal = cfd_journal.for_position(db, parts[2])
    if journal is None or journal.user_id != account.user_id:
        return "Unknown trade."
    now = datetime.now(UTC)
    try:
        if parts[1] == "in":
            cfd_journal.mark_entered(db, journal, now)
        elif parts[1] == "skip":
            cfd_journal.mark_skipped(db, journal, now)
        else:
            cfd_journal.mark_closed(db, journal, now)
        db.commit()
    except cfd_journal.JournalError as exc:
        db.rollback()
        return str(exc)
    if parts[1] == "in":
        _say(db, chat_id, f"Noted. Send your fill: /cfd in 24500.5 0.0113 {journal.position_id[:8]} (quantity optional)")
        return "Entered."
    if parts[1] == "out":
        _say(db, chat_id, f"Noted. Send your exit level: /cfd out 25500.5 {journal.position_id[:8]}")
        return "Closed."
    return "Skipped."


def _cfd_line(j: Any) -> str:
    bits = [f"{j.market_key} {j.direction} {j.status}"]
    if j.quantity_suggested is not None:
        bits.append(f"suggested {Decimal(j.quantity_suggested):.4f}")
    if j.entry_level is not None:
        bits.append(f"in {Decimal(j.entry_level):,.1f}" + (f" x {Decimal(j.quantity):.4f}" if j.quantity else ""))
    if j.exit_level is not None:
        bits.append(f"out {Decimal(j.exit_level):,.1f}")
    if j.real_pnl_eur is not None:
        bits.append(f"result {Decimal(j.real_pnl_eur):+.2f} EUR")
    return f"  {j.position_id[:8]} " + ", ".join(bits)


def _is_quantity(token: str) -> bool:
    """A number, not an id: an 8+ digit run is a position-id prefix (hex digits happen to be decimal)."""
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", token)) and not (token.isdigit() and len(token) >= 8)


def handle_cfd(db: Session, account: Any, chat_id: str, text: str) -> None:
    if not _is_admin(db, account):
        return _say(db, chat_id, "The desk is for the admin account.")
    now = datetime.now(UTC)
    parts = text.split()
    usage = "Usage: /cfd | /cfd in <price> [quantity] [id] | /cfd out <price> [id] | /cfd skip [id]"
    if len(parts) == 1:
        rows = cfd_journal.today_rows(db, account.user_id, now)
        return _say(db, chat_id, "CFD journal today:\n" + "\n".join(_cfd_line(r) for r in rows)
                    if rows else "No CFD signal today.")
    action, args = parts[1].lower(), parts[2:]
    try:
        if action == "in":
            if not args:
                return _say(db, chat_id, usage)
            level = cfd_journal.parse_number(args[0])
            rest = args[1:]
            quantity = None
            if rest and _is_quantity(rest[0]):
                quantity = cfd_journal.parse_number(rest[0])
                rest = rest[1:]
            journal = cfd_journal.find_for_owner(db, account.user_id, rest[0] if rest else None, now)
            if journal is None:
                return _say(db, chat_id, "No open CFD trade found (add the id from /cfd).")
            cfd_journal.mark_entered(db, journal, now, level, quantity)
            db.commit()
            stop = cfd_journal.stop_level(journal)
            tail = f" Stop for your fill: {stop:,.1f} ({Decimal(journal.stop_points):,.0f} pts)" \
                if stop is not None and journal.stop_points is not None else ""
            return _say(db, chat_id, f"Noted: in at {level:,.1f}." + tail)
        if action == "out":
            if not args:
                return _say(db, chat_id, usage)
            level = cfd_journal.parse_number(args[0])
            journal = cfd_journal.find_for_owner(db, account.user_id, args[1] if len(args) > 1 else None, now)
            if journal is None:
                return _say(db, chat_id, "No open CFD trade found (add the id from /cfd).")
            cfd_journal.mark_closed(db, journal, now, level)
            position = db.get(DeskPosition, journal.position_id)
            db.commit()
            real = f"{Decimal(journal.real_pnl_eur):+.2f} EUR" if journal.real_pnl_eur is not None else "not computed"
            paper = f"{Decimal(position.net_eur):+.2f} EUR" if position and position.net_eur is not None else "still open"
            return _say(db, chat_id, f"Noted: out at {level:,.1f}. Your result {real} (before overnight interest); "
                                     f"paper result {paper}.")
        if action == "skip":
            journal = cfd_journal.find_for_owner(db, account.user_id, args[0] if args else None, now)
            if journal is None:
                return _say(db, chat_id, "No open CFD trade found (add the id from /cfd).")
            cfd_journal.mark_skipped(db, journal, now)
            db.commit()
            return _say(db, chat_id, "Noted: skipped.")
    except cfd_journal.JournalError as exc:
        db.rollback()
        return _say(db, chat_id, str(exc))
    return _say(db, chat_id, usage)


def register_desk_telegram() -> None:
    telegram_updates.register_command_handler("/cfd", handle_cfd)
    telegram_updates.register_callback_handler("cfd", handle_cfd_button)
    telegram_updates.register_command_handler("/idea", handle_idea)
    telegram_updates.register_command_handler("/desk", handle_desk)
    telegram_updates.register_command_handler("/stop", handle_stop)
    telegram_updates.register_command_handler("/close", handle_close)
    telegram_updates.register_callback_handler("desk", handle_take)


def alert(db: Session, user_id: str, text: str, markup: dict[str, Any] | None = None) -> None:
    """A desk message to the owner's linked chat, if any."""
    account = telegram_bot.get_linked_account(db, user_id)
    if account is not None:
        telegram_bot.send_telegram_message(db, account.chat_id, text, markup) if markup else \
            telegram_bot.send_telegram_message(db, account.chat_id, text)




def alert_with_id(db: Session, user_id: str, text: str, markup: dict[str, Any] | None = None) -> int | None:
    """Like :func:`alert`, returning the Telegram message id (so the message can be edited), or None."""
    account = telegram_bot.get_linked_account(db, user_id)
    if account is None:
        return None
    return telegram_bot.send_telegram_message_id(db, account.chat_id, text, markup)


def edit_alert(db: Session, user_id: str, message_id: int, text: str, markup: dict[str, Any] | None = None) -> bool:
    account = telegram_bot.get_linked_account(db, user_id)
    if account is None:
        return False
    return telegram_bot.edit_telegram_message(db, account.chat_id, message_id, text, markup)
