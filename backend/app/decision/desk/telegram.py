"""Telegram side of the desk: /idea, /desk, /stop, /close and the "Paper: take" button (ADR 0027).

Registered at startup by ``register_desk_telegram()`` (worker and API); the
foundation's update handler never imports this package. Only an admin's linked
chat reaches these handlers. Resuming a halt is possible only from the app.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation import telegram_bot, telegram_updates
from app.foundation.models.entities import DeskPosition, User
from app.foundation.scalable import market
from app.foundation.scalable import service as scalable_service
from app.foundation.scalable.cli import ScalableError

from . import cfd, service
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
        result = service.take(db, parts[2], parts[3], cli, datetime.now(UTC), underlying)
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
        size = f"{p.units}x {p.wkn or p.isin}" if p.product_type != "cfd" else f"{p.quantity:.4f} (manual CFD)"
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


def register_desk_telegram() -> None:
    telegram_updates.register_command_handler("/idea", handle_idea)
    telegram_updates.register_command_handler("/desk", handle_desk)
    telegram_updates.register_command_handler("/stop", handle_stop)
    telegram_updates.register_command_handler("/close", handle_close)
    telegram_updates.register_callback_handler("desk", handle_take)


def alert(db: Session, user_id: str, text: str) -> None:
    """A desk message to the owner's linked chat, if any."""
    account = telegram_bot.get_linked_account(db, user_id)
    if account is not None:
        telegram_bot.send_telegram_message(db, account.chat_id, text)


