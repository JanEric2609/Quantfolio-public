"""Scheduler registration for the desk (ADR 0027): a 1-minute monitor and an hourly reconcile."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.foundation.jobs import register_cron_job, register_interval_job

logger = logging.getLogger(__name__)


def _owner(db: Any) -> Any:
    from app.foundation.models.entities import User

    return db.query(User).filter(User.role == "admin").order_by(User.created_at).first()


class OutMsg(str):
    """A message with the inline buttons it is sent with and the journal row it belongs to (a ``str``, so
    callers that only read the text keep working)."""

    markup: dict[str, Any] | None
    journal_id: str | None

    def __new__(cls, text: str, markup: dict[str, Any] | None = None, journal_id: str | None = None) -> OutMsg:
        obj = super().__new__(cls, text)
        obj.markup, obj.journal_id = markup, journal_id
        return obj


def monitor_tick(db: Any, now: datetime | None = None, cli: Any = None) -> dict[str, Any]:
    """One desk tick: register the spec, fire index signals, expire proposals, manage open positions, news."""
    from app.foundation.scalable import market
    from app.foundation.scalable import service as scalable_service
    from app.foundation.scalable.cli import ScalableError

    from app.foundation.models.entities import DeskPosition

    from . import calendar as cal
    from . import cfd, cfd_journal, instrument, intraday, prereg, service
    from .ledger import append_event
    from .signals import index_momentum
    from .underlyings import ALIASES, Underlying, ticker_for_isin

    # The live tick re-reads the clock for each quote age and fill (its sc reads take seconds each);
    # a given *now* (tests, replays) is used throughout.
    clock = _wall_clock if now is None else None
    now = now or datetime.now(UTC)
    local = now.astimezone(cal.BERLIN)
    if local.weekday() > 4 or not (8 <= local.hour < 22):
        return {"skipped": "outside_hours"}
    owner = _owner(db)
    if owner is None:
        return {"skipped": "no_admin"}
    prereg.register(db, now=now)
    # The tick commits after each stage and sends that stage's messages right after its commit: a
    # message never goes out for a change that is later rolled back (it would be sent twice), and a
    # later stage's failure cannot swallow the alerts of closes already saved. Each commit also ends
    # the desk's write lock, so the owner's taps and closes wait at most one stage.
    outbox: list[str] = []

    def send(texts: list[Any]) -> None:
        _send(db, owner.id, texts)

    try:
        cli = _CountingCli(cli or scalable_service.make_cli(db))  # counts the sc reads that worked
        underlyings: dict[str, Underlying] = {u.isin: u for u in ALIASES.values()}
        open_rows = db.query(DeskPosition).filter(DeskPosition.status == "open").all()
        pending = db.query(DeskPosition).filter(DeskPosition.status == "proposed").all()
        for p in [*open_rows, *pending]:
            if p.underlying_isin and p.underlying_isin not in underlyings:
                # A stock on its own exchange (a US stock is XNYS) with its ticker, for Alpaca's headlines.
                u = service.underlying_of(p)
                underlyings[p.underlying_isin] = replace(u, symbol=ticker_for_isin(db, u.isin, owner.id))
        errors: list[str] = []
        # Stops, horizons and knock-outs first: they must not depend on the index rule's reads succeeding.
        expired = service.expire_proposals(db, now)
        db.commit()
        closed = service.manage_open_positions(db, cli, now, underlyings, errors)
        cfd_closed = cfd.manage(db, cli, now, underlyings, errors)
        cfd_isins = {p.underlying_isin for p, _ in cfd_closed}
        grouped: dict[str, list[str]] = {}
        for position, reason in closed:
            if position.signal == "index_momentum" and position.underlying_isin in cfd_isins:
                # The index rule's own knock-out or warrant leaves with its CFD's close message.
                grouped.setdefault(position.underlying_isin, []).append(
                    f"  {instrument.short_line(position)}: {reason}, net {position.net_eur} EUR")
            else:
                outbox.append(f"Desk paper exit ({reason}), net {position.net_eur} EUR:\n"
                              + instrument.order_line(position, "Sell", position.exit_price))
        for position, reason in cfd_closed:
            text = cfd.close_message(position, reason, cfd_journal.for_position(db, position.id))
            if lines := grouped.get(position.underlying_isin):
                text += "\nAlso closed on paper:\n" + "\n".join(lines)
            outbox.append(text)
            closed.append((position, reason))
        db.commit()
        send(outbox)
        _live_edits(db, owner.id, now)
        opened: list[str] = []
        contexts: dict[str, Any] = {}
        fired = index_momentum.fire_due_signals(db, owner.id, cli, now, opened, errors, clock, contexts) \
            if not service.is_halted(db) else []
        db.commit()
        if fired:
            # Built after the commit: the CFD line reads index history, whose cache write commits, and a
            # fill saved before a failed tick would never be announced (the signal counts as fired).
            send(_announce_fills(db, now, opened, contexts))
        # The heads-up and get-ready messages: reads, then one health write; nothing here touches the ledger.
        previews = index_momentum.preview_due(db, cli, now) if not service.is_halted(db) else []
        db.commit()
        send(previews)
        news_out: list[str] = []
        seen = _news_check(db, cli, now, underlyings, news_out) if now.minute % 5 == 0 else 0
        # A read that failed (an expired session, the sync holding the lock) no longer fails the tick; it
        # is logged once per new code and shown as the desk's last error. An outdated wrapper raises
        # (below); the flag is cleared only by a tick that made wrapper reads, not by an idle one.
        last_error = errors[-1] if errors else None
        if last_error and service.health(db).get("last_error") != last_error:
            append_event(db, "error", service.MODE, {"code": last_error}, now=now)
        fields: dict[str, Any] = {"last_tick_at": now.isoformat(), "last_error": last_error,
                                  "macro_years": sorted(cal.MACRO_EVENTS_BY_YEAR)}
        if cli.reads_ok:  # only a tick whose sc reads went through shows the wrapper allows them
            fields["wrapper_outdated"] = False
        service.set_health(db, **fields)
        db.commit()
        send(news_out)
        if now.minute % 5 == 2 and not fired:  # never the news minute (:00/:05 ...) or an entry's (:00/:30)
            intraday.store_board(db, intraday.build_board(db, cli, now))
            db.commit()
        return {"fired": fired, "expired": expired, "closed": len(closed), "news": seen}
    except market.WrapperOutdated:
        db.rollback()
        service.set_health(db, wrapper_outdated=True, last_tick_at=now.isoformat())
        return {"skipped": "wrapper_outdated"}
    except ScalableError as exc:
        db.rollback()
        if service.health(db).get("last_error") != exc.code:  # an expired session would log every minute
            append_event(db, "error", service.MODE, {"code": exc.code}, now=now)
        service.set_health(db, last_error=exc.code, last_tick_at=now.isoformat())
        return {"skipped": exc.code}


class _CountingCli:
    """The tick's sc client, counting the reads that succeeded (``market`` calls ``cli.run``)."""

    def __init__(self, cli: Any) -> None:
        self._cli = cli
        self.reads_ok = 0

    def run(self, *args: str, **kwargs: Any) -> Any:
        out = self._cli.run(*args, **kwargs)
        self.reads_ok += 1
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cli, name)


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _send(db: Any, owner_id: str, texts: list[Any]) -> None:
    """Send the outbox. A CFD message (an :class:`OutMsg` with a journal id) keeps its Telegram message id,
    so that later ticks can edit it in place; the others go out as plain messages."""
    from . import telegram

    for text in texts:
        markup = getattr(text, "markup", None)
        journal_id = getattr(text, "journal_id", None)
        if journal_id:
            message_id = telegram.alert_with_id(db, owner_id, str(text), markup)
            if message_id is not None:
                _remember_message(db, journal_id, message_id, str(text))
        elif markup:
            telegram.alert(db, owner_id, str(text), markup)
        else:
            telegram.alert(db, owner_id, str(text))


def _remember_message(db: Any, journal_id: str, message_id: int, text: str) -> None:
    """Store the Telegram message id and the hash of the text sent (a small commit after the send: the
    stage's own changes were committed before sending)."""
    import hashlib

    from app.foundation.models.entities import DeskCfdJournal

    try:
        row = db.get(DeskCfdJournal, journal_id)
        if row is not None:
            row.telegram_message_id = message_id
            row.last_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("desk cfd message id not stored: %s", type(exc).__name__)


def _live_edits(db: Any, owner_id: str, now: datetime) -> None:
    """Edit each recent CFD message in place when its text changed (a moved price, the owner's answer, the
    close). At most one edit per message per tick; a failed edit is retried by the next tick. No network
    read: the text is built from stored rows (``cfd.manage`` has just refreshed the last price)."""
    import hashlib

    from app.foundation.models.entities import DeskCfdJournal, DeskPosition

    from . import cfd, telegram

    since = now.astimezone(UTC) - timedelta(hours=3)
    try:
        rows = db.query(DeskCfdJournal).filter(
            DeskCfdJournal.user_id == owner_id, DeskCfdJournal.telegram_message_id.isnot(None),
            DeskCfdJournal.created_at >= since).all()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("desk cfd live edit query failed: %s", type(exc).__name__)
        return
    for journal in rows:
        try:
            position = db.get(DeskPosition, journal.position_id)
            text = cfd.live_text(db, position, journal, now) if position is not None else None
            if text is None:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest == journal.last_text_hash:
                continue
            if telegram.edit_alert(db, owner_id, int(journal.telegram_message_id), text,
                                   telegram.cfd_markup(journal)):
                journal.last_text_hash = digest
                db.commit()
        except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            db.rollback()
            logger.warning("desk cfd live edit failed: %s", type(exc).__name__)


def _announce_fills(db: Any, now: datetime, position_ids: list[str],
                    contexts: dict[str, Any] | None = None) -> list[str]:
    """Name exactly what the index rule bought this tick (*position_ids*, from ``fire_due_signals``), one
    message per index: the manual CFD with its buttons, then the paper knock-out and warrant it opened
    ("Also on paper"). Sent after the commit. By id, not by entry time: a second index firing a minute
    later must not announce the first one's fills again. An index without a CFD (its price read failed)
    gets one message per paper position."""
    from app.foundation.models.entities import DeskPosition

    from . import cfd, cfd_journal, instrument, telegram
    from .underlyings import ALIASES

    out: list[str] = []
    keys = {u.isin: k for k, u in ALIASES.items()}
    if not position_ids:
        return out
    rows = db.query(DeskPosition).filter(DeskPosition.id.in_(position_ids), DeskPosition.status == "open").all()
    rows.sort(key=lambda p: position_ids.index(p.id))  # the order they were opened in
    groups: dict[str, list[Any]] = {}
    for p in rows:
        groups.setdefault(p.underlying_isin, []).append(p)

    def paper_message(p: Any) -> str:
        try:
            return "Desk paper buy (index rule, no real order):\n" + instrument.describe(p)
        except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
            db.rollback()
            logger.warning("desk fill message failed: %s", type(exc).__name__)
            return (f"Desk paper {p.product_type} opened on {p.underlying_label} ({p.direction}); details on "
                    "the Desk page.")

    for isin, members in groups.items():
        cfd_pos = next((p for p in members if p.product_type == "cfd"), None)
        paper = [p for p in members if p.product_type != "cfd"]
        key = keys.get(isin)
        if cfd_pos is None or not key:
            out.extend(paper_message(p) for p in paper)
            continue
        try:
            journal = cfd_journal.for_position(db, cfd_pos.id)
            ctx = (contexts or {}).get(cfd_pos.id) or (cfd.context_from_journal(journal) if journal else None)
            text = cfd.live_text(db, cfd_pos, journal, now, ctx) if journal is not None else None
            if text is None:  # no context (a price or the index close was missing): the short text
                text = cfd.open_message(db, cfd_pos, key, now, None)
                if paper:
                    text += "\nAlso on paper (no action):\n" + "\n".join(f"  {instrument.short_line(p)}" for p in paper)
            out.append(OutMsg(text, telegram.cfd_markup(journal) if journal else None,
                              journal.id if journal else None))
        except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
            # One message that cannot be built must not stop the others (exits, CFD closes) going out.
            db.rollback()
            logger.warning("desk fill message failed: %s", type(exc).__name__)
            side = "BUY" if cfd_pos.direction == "long" else "SELL"
            out.append(f"CFD signal (manual, Trading 212): {side} {cfd_pos.product_name or cfd_pos.underlying_label} "
                       "now. Size and stop: see the Desk page.")
            out.extend(paper_message(p) for p in paper)
    return out


def _news_check(db: Any, cli: Any, now: datetime, underlyings: dict[str, Any], outbox: list[str]) -> int:
    """New headlines on open positions' and pending proposals' underlyings: logged on each position,
    one message per underlying (an index signal holds a knock-out, a warrant and a CFD on the same
    one), never acted on."""
    from app.foundation.models.entities import DeskEvent, DeskPosition

    from . import news, service
    from .ledger import append_event

    count = 0
    by_underlying: dict[str, list[Any]] = {}
    for p in db.query(DeskPosition).filter(DeskPosition.status.in_(("open", "proposed"))).order_by(DeskPosition.id):
        by_underlying.setdefault(p.underlying_isin, []).append(p)
    for isin, positions in by_underlying.items():
        u = underlyings.get(isin)
        if u is None:
            continue
        known: set[str] = set()
        for ev in db.query(DeskEvent).filter(DeskEvent.position_id.in_([p.id for p in positions]),
                                             DeskEvent.kind == "news_seen"):
            known.update(json.loads(ev.payload).get("ids", []))
        fresh = [h for h in news.headlines(db, cli, u, now - timedelta(hours=6)) if h["id"] not in known]
        if not fresh:
            continue
        for p in positions:
            append_event(db, "news_seen", service.MODE, {"ids": [h["id"] for h in fresh],
                                                        "titles": [h["title"] for h in fresh[:5]]}, p.id, now=now)
        outbox.append(f"Desk news on {positions[0].underlying_label} (logged only): "
                      + " | ".join(h["title"] for h in fresh[:3]))
        count += len(fresh)
    return count


#: ISIN -> sc security type, for ISINs the owner traded but the desk never saw held (internal state).
ISIN_TYPES_KEY = "desk_isin_types_json"
DERIVATIVE_TYPES = ("DERIVATIVE", "WARRANT")
#: sc quotes per reconcile run to classify ISINs the depot snapshots never held.
MAX_TYPE_LOOKUPS = 10


def _known_security_types(db: Any, user_id: str) -> dict[str, str]:
    """ISIN -> security type from every Scalable depot snapshot, the live positions, the owner's derivative
    terms and the desk's own lookups."""
    from app.foundation.live_positions import live_positions
    from app.foundation.models.entities import ConnectedAccount, DerivativeTerms
    from app.foundation.settings import get_setting_row

    out: dict[str, str] = {}
    cached = get_setting_row(db, ISIN_TYPES_KEY)
    if isinstance(cached, dict):
        out.update({k: str(v) for k, v in cached.items()})
    for (raw,) in db.query(ConnectedAccount.raw_json).filter(ConnectedAccount.user_id == user_id,
                                                              ConnectedAccount.source == "scalable",
                                                              ConnectedAccount.account_type == "depot"):
        try:
            types = json.loads(raw or "{}").get("security_types")
        except (ValueError, AttributeError):
            types = None
        if isinstance(types, dict):
            out.update({k: str(v) for k, v in types.items()})
    for p in live_positions(db, user_id):
        if p.isin and p.security_type:
            out[p.isin] = p.security_type
    for (isin,) in db.query(DerivativeTerms.isin).filter(DerivativeTerms.user_id == user_id):
        out.setdefault(isin, "DERIVATIVE")
    return out


def _classify_unknown(db: Any, isins: list[str], types: dict[str, str], today: str) -> None:
    """Ask sc's quote for the security type of up to ``MAX_TYPE_LOOKUPS`` ISINs; answers are cached. sc names
    the type even without a bid or ask; an ISIN it does not know is marked ``?<date>`` and asked again the
    next day."""
    from app.foundation.scalable import market
    from app.foundation.scalable import service as scalable_service
    from app.foundation.scalable.cli import ScalableError, ScalableUsageError
    from app.foundation.settings import get_setting_row, upsert_public_settings

    if not isins:
        return
    found: dict[str, str] = {}
    try:
        cli = scalable_service.make_cli(db)
        for isin in isins[:MAX_TYPE_LOOKUPS]:
            try:
                kind = market.get_security_type(cli, isin)  # also without a bid or ask (knocked out, night)
            except ScalableUsageError as exc:
                if isinstance(exc, market.WrapperOutdated):
                    raise
                kind = None  # this ISIN only (refused as input): marked, the others are still asked
            found[isin] = kind or f"?{today}"
    except ScalableError as exc:  # no session, the sync's lock, an old wrapper: the rest is tried next hour
        logger.info("desk reconcile: type lookup stopped: %s", exc.code)
    if found:
        cached = get_setting_row(db, ISIN_TYPES_KEY)
        upsert_public_settings(db, {ISIN_TYPES_KEY: {**(cached if isinstance(cached, dict) else {}), **found}})
        types.update(found)


def reconcile_external(db: Any, now: datetime | None = None) -> int:
    """Log real Scalable trades in derivative ISINs that the desk did not make (the owner's app trades).

    The ISINs come from the activity ledger itself, not from what is held now: a knock-out bought and
    sold between two syncs is never in a depot snapshot. An ISIN of unknown type is classified by sc."""
    from app.foundation.models.entities import ActivityLedgerEntry, DeskEvent, DeskPosition

    from . import service
    from .ledger import append_event

    now = now or datetime.now(UTC)
    owner = _owner(db)
    if owner is None:
        return 0
    logged: set[str] = set()
    for ev in db.query(DeskEvent).filter(DeskEvent.kind == "external_trade"):
        logged.add(json.loads(ev.payload).get("ledger_id", ""))
    rows = [r for r in db.query(ActivityLedgerEntry).filter(
        ActivityLedgerEntry.user_id == owner.id, ActivityLedgerEntry.source == "scalable",
        ActivityLedgerEntry.activity_type.in_(("buy", "sell")), ActivityLedgerEntry.isin.isnot(None)).all()
        if r.id not in logged]
    if not rows:
        return 0
    types = _known_security_types(db, owner.id)
    today = now.astimezone(UTC).date().isoformat()
    unknown = {r.isin for r in rows if r.isin and (r.isin not in types or (types[r.isin].startswith("?")
                                                                         and types[r.isin] != f"?{today}"))}
    _classify_unknown(db, sorted(unknown), types, today)
    count = 0
    for row in rows:
        if types.get(row.isin or "", "").upper() not in DERIVATIVE_TYPES:
            continue
        position = DeskPosition(
            user_id=owner.id, mode=service.MODE, cell="external", signal="external", product_type="unknown",
            isin=row.isin or "", underlying_isin="", underlying_label=row.description[:64], direction="long",
            units=int(abs(row.quantity or 0)), entry_price=row.price, entry_at=datetime.combine(
                row.date, datetime.min.time(), tzinfo=UTC), status="closed", exit_reason="external",
            created_at=now)
        db.add(position)
        db.flush()
        append_event(db, "external_trade", service.MODE, {
            "ledger_id": row.id, "side": row.activity_type, "isin": row.isin, "quantity": str(row.quantity),
            "price": str(row.price), "date": row.date.isoformat()}, position.id, now=now)
        count += 1
    db.commit()
    return count


def register_desk_monitor_job(scheduler: Any | None = None) -> str:
    def desk_monitor_inner() -> dict[str, Any]:
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            return monitor_tick(db)
        finally:
            db.close()

    return register_interval_job("desk_monitor", desk_monitor_inner, scheduler=scheduler, minutes=1)


def register_desk_reconcile_job(scheduler: Any | None = None) -> str:
    def desk_reconcile_inner() -> dict[str, Any]:
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            return {"logged": reconcile_external(db)}
        finally:
            db.close()

    return register_cron_job("desk_reconcile", desk_reconcile_inner, scheduler=scheduler, minute=17)


def news_wire_tick(db: Any, now: datetime | None = None) -> dict[str, Any]:
    """Fed, ECB and SEC 8-K once a minute (the Alpaca stream runs in its own worker thread)."""
    from . import newswire

    owner = _owner(db)
    if owner is None:
        return {"skipped": "no_admin"}
    return newswire.poll_feeds(db, owner.id, now)


def register_news_wire_job(scheduler: Any | None = None) -> str:
    def news_wire_inner() -> dict[str, Any]:
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            return news_wire_tick(db)
        finally:
            db.close()

    return register_interval_job("desk_news_wire", news_wire_inner, scheduler=scheduler, minutes=1)
