"""Scheduler registration for the desk (ADR 0027): a 1-minute monitor and an hourly reconcile."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.foundation.jobs import register_cron_job, register_interval_job

logger = logging.getLogger(__name__)


def _owner(db: Any) -> Any:
    from app.foundation.models.entities import User

    return db.query(User).filter(User.role == "admin").order_by(User.created_at).first()


def monitor_tick(db: Any, now: datetime | None = None, cli: Any = None) -> dict[str, Any]:
    """One desk tick: register the spec, fire index signals, expire proposals, manage open positions, news."""
    from app.foundation.scalable import market
    from app.foundation.scalable import service as scalable_service
    from app.foundation.scalable.cli import ScalableError

    from app.foundation.models.entities import DeskPosition

    from . import calendar as cal
    from . import cfd, instrument, prereg, service, telegram
    from .ledger import append_event
    from .signals import index_momentum
    from .underlyings import ALIASES, Underlying

    now = now or datetime.now(UTC)
    local = now.astimezone(cal.BERLIN)
    if local.weekday() > 4 or not (8 <= local.hour < 22):
        return {"skipped": "outside_hours"}
    owner = _owner(db)
    if owner is None:
        return {"skipped": "no_admin"}
    prereg.register(db, now=now)
    # Messages go out only after the tick's commit: a failed tick rolls back its closes, and an
    # exit alert sent before that would be sent again when the next tick closes them for real.
    outbox: list[str] = []
    try:
        cli = cli or scalable_service.make_cli(db)
        underlyings: dict[str, Underlying] = {u.isin: u for u in ALIASES.values()}
        for p in db.query(DeskPosition).filter(DeskPosition.status == "open").all():
            underlyings.setdefault(p.underlying_isin, service.underlying_of(p))  # a US stock is XNYS
        # Stops, horizons and knock-outs first: they must not depend on the index rule's reads succeeding.
        expired = service.expire_proposals(db, now)
        closed = service.manage_open_positions(db, cli, now, underlyings)
        for position, reason in closed:
            outbox.append(f"Desk paper exit ({reason}), net {position.net_eur} EUR:\n"
                          + instrument.order_line(position, "Sell", position.exit_price))
        for position, reason in cfd.manage(db, cli, now, underlyings):
            outbox.append(cfd.close_message(position, reason))
            closed.append((position, reason))
        opened: list[str] = []
        errors: list[str] = []
        fired = index_momentum.fire_due_signals(db, owner.id, cli, now, opened, errors) \
            if not service.is_halted(db) else []
        seen = _news_check(db, cli, now, underlyings, outbox) if now.minute % 5 == 0 else 0
        # A read the index rule could not make (an expired session, the sync holding the lock) no longer
        # fails the tick; it is still logged once per new code and shown as the desk's last error.
        last_error = errors[-1] if errors else None
        if last_error and service.health(db).get("last_error") != last_error:
            append_event(db, "error", service.MODE, {"code": last_error}, now=now)
        service.set_health(db, last_tick_at=now.isoformat(), wrapper_outdated=False, last_error=last_error,
                           macro_years=sorted(cal.MACRO_EVENTS_BY_YEAR))
        db.commit()
        if fired:
            # Built after the commit: the CFD line reads index history, whose cache write commits, and a
            # fill saved before a failed tick would never be announced (the signal counts as fired).
            outbox = _announce_fills(db, now, opened) + outbox
        for text in outbox:
            telegram.alert(db, owner.id, text)
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


def _announce_fills(db: Any, now: datetime, position_ids: list[str]) -> list[str]:
    """Name exactly what the index rule bought this tick (*position_ids*, from ``fire_due_signals``), and
    the manual CFD line; sent after the commit. By id, not by entry time: a second index firing a minute
    later must not announce the first one's fills (and its "CFD: BUY") again."""
    from app.foundation.models.entities import DeskPosition

    from . import cfd, instrument
    from .underlyings import ALIASES

    out: list[str] = []
    keys = {u.isin: k for k, u in ALIASES.items()}
    if not position_ids:
        return out
    rows = db.query(DeskPosition).filter(DeskPosition.id.in_(position_ids), DeskPosition.status == "open").all()
    rows.sort(key=lambda p: position_ids.index(p.id))  # the order they were opened in
    for p in rows:
        if p.product_type == "cfd":
            key = keys.get(p.underlying_isin)
            if key:
                out.append(cfd.open_message(db, p, key, now))
        else:
            out.append("Desk paper buy (index rule, no real order):\n" + instrument.describe(p))
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


def reconcile_external(db: Any, now: datetime | None = None) -> int:
    """Log real Scalable trades in derivative ISINs that the desk did not make (the owner's app trades)."""
    from app.foundation.models.entities import (
        ActivityLedgerEntry, DeskEvent, DeskPosition, DerivativeTerms,
    )

    from . import service
    from .ledger import append_event

    now = now or datetime.now(UTC)
    owner = _owner(db)
    if owner is None:
        return 0
    from app.foundation.live_positions import live_positions

    isins = {p.isin for p in live_positions(db, owner.id) if (p.security_type or "").upper() in ("DERIVATIVE", "WARRANT")}
    isins |= {i for (i,) in db.query(DerivativeTerms.isin).filter(DerivativeTerms.user_id == owner.id)}
    if not isins:
        return 0
    logged: set[str] = set()
    for ev in db.query(DeskEvent).filter(DeskEvent.kind == "external_trade"):
        logged.add(json.loads(ev.payload).get("ledger_id", ""))
    count = 0
    rows = db.query(ActivityLedgerEntry).filter(
        ActivityLedgerEntry.user_id == owner.id, ActivityLedgerEntry.source == "scalable",
        ActivityLedgerEntry.activity_type.in_(("buy", "sell")), ActivityLedgerEntry.isin.in_(isins)).all()
    for row in rows:
        if row.id in logged:
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
