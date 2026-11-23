"""The intraday board of the four indices (ADR 0027 amendment 2026-11-23): a snapshot for the Intraday page.

Display only. Every number is derived from reads the rule already makes (the proxy's price, daily closes,
sigma) and from the owner's own journal; nothing here opens, closes or changes a position, and the registered
rule is not touched. The snapshot is one internal ``app_settings`` key (``desk_intraday_board_json``, like
``desk_health_json``: no catalog entry), rebuilt every five minutes by the monitor tick.

Reads come first, the one write (the key) last, so no network read happens under a write lock.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli, ScalableError
from app.foundation.settings import get_setting_row, upsert_public_settings

from . import calendar as cal
from . import cfd, cfd_journal, service
from .signals import index_momentum as im
from .underlyings import ALIASES

logger = logging.getLogger(__name__)

BOARD_KEY = "desk_intraday_board_json"
SKIPPABLE = (ScalableError, im.DataUnavailable, httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(cal.BERLIN).isoformat() if value else None


def _daily_ctx(db: Session, cli: ScalableCli, plan: im.Plan, now: datetime,
               cached: dict[str, Any] | None) -> dict[str, str] | None:
    """The proxy's and the index's previous closes and the daily sigma: once per day per index."""
    if cached and cached.get("day") == plan.day.isoformat():
        return cached
    _r1, proxy_prev = im.compute_r1_parts(db, cli, plan, now)
    index_prev = cfd._index_prev_close(db, cfd.MARKETS[plan.key][1], plan.day)
    sigma = service.underlying_sigma(db, cli, plan.underlying)
    if proxy_prev is None or index_prev is None or sigma is None:
        return None
    return {"day": plan.day.isoformat(), "proxy_prev_close": str(proxy_prev), "index_prev_close": str(index_prev),
            "sigma": str(sigma)}


def _cfd_today(db: Session, key: str, day: date) -> dict[str, Any] | None:
    u = ALIASES[key]
    rows = db.query(DeskPosition).filter(DeskPosition.cell == cfd.CELL, DeskPosition.underlying_isin == u.isin) \
        .order_by(DeskPosition.created_at.desc()).limit(3).all()
    for p in rows:
        at = service._aware(p.entry_at)
        if at is None or at.astimezone(cal.BERLIN).date() != day:
            continue
        j = cfd_journal.for_position(db, p.id)
        pnl = None
        if p.status == "open" and p.last_bid and p.entry_price and p.quantity:
            move = (Decimal(p.last_bid) / Decimal(p.entry_price) - 1) * (1 if p.direction == "long" else -1)
            pnl = move * Decimal(p.entry_price) * Decimal(p.quantity)
        return {"position_id": p.id, "status": p.status, "direction": p.direction, "entry_price": _s(p.entry_price),
                "paper_pnl_before_costs": _s(pnl.quantize(Decimal("0.01")) if pnl is not None else None),
                "paper_net_eur": _s(p.net_eur), "journal_status": j.status if j else None}
    return None


def _index_card(db: Session, cli: ScalableCli, key: str, now: datetime, day: date, notices: dict[str, Any],
                cached: dict[str, Any] | None, events: list[tuple[str, str]]) -> tuple[dict[str, Any], Any]:
    u = ALIASES[key]
    name, _ticker, ccy = cfd.MARKETS[key]
    card: dict[str, Any] = {"key": key, "label": u.label, "t212_name": name, "exchange": u.exchange, "currency": ccy,
                            "session": {"open": None, "close": None, "status": "closed"}, "enter_at": None,
                            "exit_at": None, "skip_reason": None, "macro_events_today": [k for k, _ in events]}
    times = cal.session_times(day, u.exchange)
    plan = next((p for p in im.plans_for_day(day) if p.key == key), None)
    ctx = cached
    if times is not None:
        o, c = times[0].astimezone(UTC), times[1].astimezone(UTC)
        status = "before" if now < o else ("open" if now < c else "closed")
        card["session"] = {"open": _iso(o), "close": _iso(c), "status": status}
    if plan is None:
        return card, ctx
    card["enter_at"], card["exit_at"] = _iso(plan.enter_at), _iso(plan.exit_at)
    card["skip_reason"] = im.skip_reason(plan)
    heads = (notices.get(f"{u.exchange}:{day.isoformat()}:headsup") or {}).get(key)
    if heads:
        card["r1"], card["direction"] = heads.get("r1"), heads.get("dir")
    card["cfd_today"] = _cfd_today(db, key, day)
    if card["session"]["status"] == "closed" and card["session"]["open"] is None:
        return card, ctx
    try:
        ctx = _daily_ctx(db, cli, plan, now, cached)
        price = service.underlying_price(db, cli, u, now=now)
        if ctx is None or price is None or price <= 0:
            card["error"] = "no_data"
            return card, ctx
        proxy_prev, index_prev, sigma = (Decimal(ctx["proxy_prev_close"]), Decimal(ctx["index_prev_close"]),
                                         Decimal(ctx["sigma"]))
        move = price / proxy_prev - 1
        level = index_prev * (1 + move)
        eur = Decimal(1) if ccy == "EUR" else (cfd._eur_per_usd(db, now) or Decimal(0))
        minutes = cfd.SESSION_MINUTES.get(u.exchange, 510)
        card.update({
            "proxy_price": str(price), "proxy_prev_close": ctx["proxy_prev_close"],
            "index_prev_close": ctx["index_prev_close"], "sigma": ctx["sigma"], "move_pct": str(move * 100),
            "index_level_est": str(level), "sigma_points": str(sigma * level),
            "typical_30m_points": str(sigma * (Decimal(30) / Decimal(minutes)).sqrt() * level),
            "spread_points": str(cfd.T212["spread_bp"][key] / Decimal(10000) * level),
            "eur_per_unit": str(eur)})
    except market.WrapperOutdated:
        raise
    except SKIPPABLE as exc:
        card["error"] = str(getattr(exc, "code", None) or type(exc).__name__)[:60]
    except SQLAlchemyError as exc:
        db.rollback()
        card["error"] = type(exc).__name__
    return card, ctx


def build_board(db: Session, cli: ScalableCli, now: datetime) -> dict[str, Any]:
    """The board for *now*. Reads only; :func:`store_board` writes it. One failing index records its own
    error and leaves the others intact. An outdated wrapper propagates."""
    day = now.astimezone(cal.BERLIN).date()
    previous = get_setting_row(db, BOARD_KEY)
    previous = previous if isinstance(previous, dict) else {}
    notices = service.health(db).get("cfd_notices") or {}
    events = cal.macro_events_between(day, day)
    ctxs: dict[str, Any] = dict(previous.get("ctx") or {})
    indices = []
    for key in im.INDEX_KEYS:
        card, ctx = _index_card(db, cli, key, now, day, notices, ctxs.get(key), events)
        if ctx is not None:
            ctxs[key] = ctx
        indices.append(card)
    return {"as_of": now.astimezone(UTC).isoformat(), "day": day.isoformat(), "indices": indices, "ctx": ctxs}


def store_board(db: Session, board: dict[str, Any]) -> None:
    upsert_public_settings(db, {BOARD_KEY: board})
