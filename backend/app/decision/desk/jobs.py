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
    try:
        cli = cli or scalable_service.make_cli(db)
        underlyings: dict[str, Underlying] = {u.isin: u for u in ALIASES.values()}
        for p in db.query(DeskPosition).filter(DeskPosition.status == "open").all():
            underlyings.setdefault(p.underlying_isin, Underlying(p.underlying_isin, p.underlying_label, "XETR", p.liquid))
        fired = index_momentum.fire_due_signals(db, owner.id, cli, now) if not service.is_halted(db) else []
        if fired:
            _announce_fills(db, owner.id, now)
        expired = service.expire_proposals(db, now)
        closed = service.manage_open_positions(db, cli, now, underlyings)
        for position, reason in closed:
            telegram.alert(db, owner.id, f"Desk paper exit ({reason}), net {position.net_eur} EUR:\n"
                                         + instrument.order_line(position, "Sell", position.exit_price))
        for position, reason in cfd.manage(db, cli, now, underlyings):
            telegram.alert(db, owner.id, cfd.close_message(position, reason))
            closed.append((position, reason))
        seen = _news_check(db, cli, owner.id, now, underlyings) if now.minute % 5 == 0 else 0
        service.set_health(db, last_tick_at=now.isoformat(), wrapper_outdated=False, last_error=None,
                           macro_years=sorted(cal.MACRO_EVENTS_BY_YEAR))
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


def _announce_fills(db: Any, user_id: str, now: datetime) -> None:
    """Name exactly what the index rule bought this tick, and send the manual CFD line."""
    from app.foundation.models.entities import DeskPosition

    from . import cfd, instrument, telegram
    from .underlyings import ALIASES

    keys = {u.isin: k for k, u in ALIASES.items()}
    rows = db.query(DeskPosition).filter(DeskPosition.signal == "index_momentum", DeskPosition.status == "open",
                                         DeskPosition.entry_at >= now - timedelta(minutes=1)).all()
    for p in rows:
        if p.product_type == "cfd":
            key = keys.get(p.underlying_isin)
            if key:
                telegram.alert(db, user_id, cfd.open_message(db, p, key, now))
        else:
            telegram.alert(db, user_id, "Desk paper buy (index rule, no real order):\n" + instrument.describe(p))


def _news_check(db: Any, cli: Any, user_id: str, now: datetime, underlyings: dict[str, Any]) -> int:
    """New headlines on open positions' and pending proposals' underlyings: logged and sent, never acted on."""
    from app.foundation.models.entities import DeskEvent, DeskPosition

    from . import news, service, telegram
    from .ledger import append_event

    count = 0
    rows = db.query(DeskPosition).filter(DeskPosition.status.in_(("open", "proposed"))).all()
    for p in rows:
        u = underlyings.get(p.underlying_isin)
        if u is None:
            continue
        known: set[str] = set()
        for ev in db.query(DeskEvent).filter(DeskEvent.position_id == p.id, DeskEvent.kind == "news_seen"):
            known.update(json.loads(ev.payload).get("ids", []))
        fresh = [h for h in news.headlines(db, cli, u, now - timedelta(hours=6)) if h["id"] not in known]
        if fresh:
            append_event(db, "news_seen", service.MODE, {"ids": [h["id"] for h in fresh],
                                                        "titles": [h["title"] for h in fresh[:5]]}, p.id, now=now)
            telegram.alert(db, user_id, f"Desk news on {p.underlying_label} (logged only): " + " | ".join(
                h["title"] for h in fresh[:3]))
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
