"""The index rule as a manual Trading 212 CFD signal (ADR 0027 amendment 2026-11-20). Paper only.

Trading 212's API has no CFD endpoint and the app never gets one: the desk
sends the owner a Telegram line ("CFD: buy US 500 now, close at 21:57") and
scores the same trade on paper as the cell ``index_momentum:cfd``.

Pricing. The entry and exit are the underlying's reference price (SPY or QQQ
on Alpaca, the Xetra ETF proxy on sc) at the signal minute; Trading 212's own
CFD quote is not readable. Costs are fixed in the pre-registered spec: the
assumed round-trip spread in basis points of the exposure, plus Trading 212's
0.5 % FX fee, charged here on the result of a USD index (its fee page gives no basis: our assumption).
No order fee, and no
overnight financing: the position is closed three minutes before the close.

Size. The EUR exposure loses ``max_loss_per_trade`` (5 EUR) on a one-sigma
adverse move of the day, capped at ``MAX_EXPOSURE_EUR``; the simulated stop
sits one daily sigma away. These are the owner's own Trading 212 funds, so the
Scalable pot and the live-rehearsal counts do not apply (``would_take_live``
stays False: no automated path can ever exist for a CFD).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition
from app.foundation.scalable.cli import ScalableCli

from . import calendar as cal
from . import cfd_journal, prereg, service
from .ledger import append_event
from .limits import LIMITS
from .underlyings import Underlying

logger = logging.getLogger(__name__)

CELL = "index_momentum:cfd"
#: The event desk's cells (ADR 0028; pinned equal to ``prereg_events.EVENT_CELLS`` by a test). Their CFDs are
#: managed by ``events.py`` (overnight interest, rollover, rule exits), never by :func:`manage`.
EVENT_CELL_NAMES = ("ta_trend:cfd", "ta_band:cfd", "ta_rsi2:cfd", "hype_fade:cfd", "news_drift:cfd", "owner_call:cfd")
SPEC = prereg.SPEC["cfd"]
SPREAD_BP: dict[str, Decimal] = {k: Decimal(v) for k, v in SPEC["spread_bp"].items()}
FX_FEE = Decimal(SPEC["fx_fee_on_result"])
MAX_EXPOSURE_EUR = Decimal(SPEC["max_exposure_eur"])
#: Trading 212 CFD facts as the owner's app showed them on 2026-11-23 (about 17:40 Berlin). Display
#: and the owner's calculator only: paper scoring keeps the pre-registered SPREAD_BP and FX_FEE.
T212 = {
    "spread_bp": {"DAX": Decimal("0.5"), "ES50": Decimal("3.8"), "SPX": Decimal("0.75"), "NDX": Decimal("0.6")},
    "margin_rate": Decimal("0.05"),  # 1:20 on major indices (ESMA retail)
    "overnight_rate": Decimal("0.000083"),  # 0.0083 % of the position value per night
    "fx_fee_on_result": Decimal("0.005"),  # USD markets, charged on the P&L at close
    "min_order_eur": Decimal("20"),  # minimum order value (TECH100: 0.000816 units)
    "quantity_decimals": 4,
    "observed": "2026-11-23",
}
EVENT_OWNER_CLOSE: Any = None  # set by events.py (a hook, so that cfd never imports the event engine)
SESSION_MINUTES = {"XETR": 510, "XNYS": 390}  # 09:00-17:30 Berlin, 09:30-16:00 New York
#: The market's name in the Trading 212 app and the index ticker for its level (quantity hint).
MARKETS: dict[str, tuple[str, str, str]] = {  # key -> (Trading 212 name, level ticker, currency)
    # Names as the Trading 212 app shows them (checked 2026-11-20): search the ticker in brackets.
    "DAX": ("Germany 40 (GER40)", "^GDAXI", "EUR"),
    "ES50": ("EU 50 (EU50)", "^STOXX50E", "EUR"),
    "SPX": ("USA 500 (USA500)", "^GSPC", "USD"),
    "NDX": ("USA Tech 100 (TECH100)", "^NDX", "USD"),
}


def _index_level(db: Session, ticker: str) -> Decimal | None:
    """The last daily close of the index (for the quantity hint only; never used in scoring)."""
    try:
        from app.foundation.market import history

        rows = history(db, ticker, days=10)
        closes = [r.get("close") for r in rows if r.get("close") is not None]
        return Decimal(str(closes[-1])) if closes else None
    except SQLAlchemyError as exc:  # the price cache's own write (e.g. a bar another job just stored)
        db.rollback()
        logger.info("desk cfd level read failed: %s", type(exc).__name__)
        return None
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk cfd level read failed: %s", type(exc).__name__)
        return None


def _eur_per_usd(db: Session, now: datetime) -> Decimal | None:
    try:
        from app.foundation.ecb_fx import eur_per_unit

        rate, _src = eur_per_unit(db, "USD", now.date())
        return Decimal(str(rate)) if rate else None
    except SQLAlchemyError as exc:
        db.rollback()
        logger.info("desk cfd fx read failed: %s", type(exc).__name__)
        return None
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk cfd fx read failed: %s", type(exc).__name__)
        return None


def quantity_hint(db: Session, key: str, exposure_eur: Decimal, now: datetime) -> str:
    """'about 0.09 (US 500 near 6,800)' from yesterday's index close; '' when unknown."""
    name, ticker, ccy = MARKETS[key]
    level = _index_level(db, ticker)
    if level is None or level <= 0:
        return ""
    eur_level = level
    if ccy == "USD":
        fx = _eur_per_usd(db, now)
        if fx is None:
            return ""
        eur_level = level * fx
    qty = exposure_eur / eur_level
    return f"about {qty:.2f} ({name} near {level:,.0f} {ccy} at the last close)"


def open_signal(db: Session, user_id: str, key: str, underlying: Underlying, direction: str,
                exit_at: datetime, cli: ScalableCli, now: datetime) -> DeskPosition | None:
    """Open the paper CFD at the reference price; the caller sends :func:`open_message`."""
    ref, sigma = prepare_signal(db, cli, underlying, now)
    return record_signal(db, user_id, key, underlying, direction, exit_at, now, ref, sigma)


def prepare_signal(db: Session, cli: ScalableCli, underlying: Underlying,
                   now: datetime) -> tuple[Decimal | None, Decimal | None]:
    """The read half of :func:`open_signal`: the reference price and the daily sigma."""
    return service.underlying_price(db, cli, underlying, now=now), service.underlying_sigma(db, cli, underlying)


@dataclass(frozen=True)
class CfdContext:
    """What the owner needs to place the manual Trading 212 trade: display only, never used in scoring."""
    key: str
    r1: Decimal
    proxy_prev_close: Decimal
    index_prev_close: Decimal
    index_level_est: Decimal
    currency: str
    eur_per_unit: Decimal  # 1 for EUR, else the ECB rate
    sigma: Decimal
    session_minutes: int
    enter_by: datetime
    exit_at: datetime
    exposure_eur: Decimal
    quantity_t212: Decimal
    stop_points: Decimal
    typical_30m_points: Decimal
    spread_points: Decimal
    margin_eur: Decimal
    overnight_eur_per_night: Decimal


def _index_prev_close(db: Session, ticker: str, day) -> Decimal | None:
    """The last daily close of *ticker* on a date strictly before *day* (today's partial bar never counts)."""
    try:
        from app.foundation.market import history

        rows = history(db, ticker, days=10)
        closes = [r for r in rows if r.get("close") is not None and str(r.get("date"))[:10] < day.isoformat()]
        closes.sort(key=lambda r: str(r["date"])[:10])
        return Decimal(str(closes[-1]["close"])) if closes else None
    except SQLAlchemyError as exc:
        db.rollback()
        logger.info("desk cfd index close read failed: %s", type(exc).__name__)
        return None
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk cfd index close read failed: %s", type(exc).__name__)
        return None


def prepare_context(db: Session, key: str, underlying: Underlying, r1: Decimal | None,
                    proxy_prev_close: Decimal | None, ref: Decimal | None, sigma: Decimal | None,
                    enter_by: datetime, exit_at: datetime, now: datetime) -> CfdContext | None:
    """The read half of the v2 message. ``None`` when a price, sigma or the index close is missing (the
    message then falls back to the short text)."""
    if r1 is None or proxy_prev_close is None or proxy_prev_close <= 0 or ref is None or ref <= 0 \
            or sigma is None or sigma <= 0:
        return None
    name, ticker, ccy = MARKETS[key]
    day = exit_at.astimezone(cal.BERLIN).date()
    index_prev = _index_prev_close(db, ticker, day)
    if index_prev is None or index_prev <= 0:
        return None
    eur_per_unit = Decimal(1) if ccy == "EUR" else _eur_per_usd(db, now)
    if eur_per_unit is None:
        return None
    level_est = index_prev * ref / proxy_prev_close
    level_eur = level_est * eur_per_unit
    minutes = SESSION_MINUTES.get(underlying.exchange, 510)
    exposure = min(LIMITS.max_loss_per_trade / sigma, MAX_EXPOSURE_EUR).quantize(Decimal("0.01"))
    return CfdContext(
        key=key, r1=r1, proxy_prev_close=proxy_prev_close, index_prev_close=index_prev,
        index_level_est=level_est, currency=ccy, eur_per_unit=eur_per_unit, sigma=sigma, session_minutes=minutes,
        enter_by=enter_by, exit_at=exit_at, exposure_eur=exposure,
        quantity_t212=(exposure / level_eur).quantize(Decimal(1).scaleb(-T212["quantity_decimals"]), rounding=ROUND_DOWN),
        stop_points=sigma * level_est,
        typical_30m_points=sigma * (Decimal(30) / Decimal(minutes)).sqrt() * level_est,
        spread_points=T212["spread_bp"][key] / Decimal(10000) * level_est,
        margin_eur=exposure * T212["margin_rate"],
        overnight_eur_per_night=exposure * T212["overnight_rate"])


def context_from_journal(j: Any) -> CfdContext | None:
    """The message context from a journal row. Fields the message never shows (the proxy and index closes,
    sigma, the session length) are 0."""
    if j.r1 is None or j.quantity_suggested is None or j.exposure_eur is None or j.stop_points is None \
            or j.enter_by is None or j.exit_at is None or j.index_level_est is None:
        return None
    zero = Decimal(0)
    exposure = Decimal(j.exposure_eur)
    return CfdContext(
        key=j.market_key, r1=Decimal(j.r1), proxy_prev_close=zero, index_prev_close=zero,
        index_level_est=Decimal(j.index_level_est), currency=j.currency, eur_per_unit=Decimal(j.eur_per_unit or 1),
        sigma=zero, session_minutes=0, enter_by=service._aware(j.enter_by) or j.enter_by,
        exit_at=service._aware(j.exit_at) or j.exit_at,
        exposure_eur=exposure, quantity_t212=Decimal(j.quantity_suggested), stop_points=Decimal(j.stop_points),
        typical_30m_points=Decimal(j.typical_30m_points or 0), spread_points=Decimal(j.spread_points or 0),
        margin_eur=exposure * T212["margin_rate"], overnight_eur_per_night=exposure * T212["overnight_rate"])


def record_signal(db: Session, user_id: str, key: str, underlying: Underlying, direction: str, exit_at: datetime,
                  now: datetime, ref: Decimal | None, sigma: Decimal | None,
                  context: CfdContext | None = None) -> DeskPosition | None:
    """The write half of :func:`open_signal` (no network read). *context* only adds display keys to the
    ``fill`` event; the paper position is the same with or without it."""
    base = {"signal": "index_momentum", "underlying": underlying.isin, "direction": direction, "product_type": "cfd"}
    if ref is None or ref <= 0 or sigma is None or sigma <= 0:
        append_event(db, "guard_verdict", service.MODE, {**base, "ok": False, "reasons": ["no_reference_price"]},
                     now=now)
        return None
    exposure = min(LIMITS.max_loss_per_trade / sigma, MAX_EXPOSURE_EUR).quantize(Decimal("0.01"))
    stop = ref * (1 - sigma) if direction == "long" else ref * (1 + sigma)
    name = MARKETS[key][0]
    position = DeskPosition(
        user_id=user_id, mode=service.MODE, cell=CELL, signal="index_momentum", product_type="cfd",
        isin=underlying.isin, issuer="Trading 212", underlying_isin=underlying.isin,
        underlying_label=underlying.label, direction=direction, liquid=True, leverage_or_omega=Decimal(1),
        units=None, quantity=(exposure / ref).quantize(Decimal("0.00000001")), entry_price=ref,
        entry_fee=Decimal(0), entry_at=now.astimezone(UTC), stop_price=stop.quantize(Decimal("0.0001")),
        max_exit_at=exit_at.astimezone(UTC), spread_pct=SPREAD_BP[key] / Decimal(10000), underlying_entry=ref,
        last_bid=ref, last_quote_at=now.astimezone(UTC), status="open", would_take_live=False,
        prereg_hash=prereg.PREREG_HASH, product_name=f"{name} CFD (Trading 212, manual)",
        created_at=now.astimezone(UTC))
    db.add(position)
    db.flush()
    payload = {
        "product_type": "cfd", "market": name, "reference_price": str(ref), "exposure_eur": str(exposure),
        "spread_bp": str(SPREAD_BP[key]), "stop": str(position.stop_price), "would_take_live": False,
        "manual": True, "max_exit_at": exit_at.isoformat()}
    if context is not None:
        payload.update({"index_level_est": str(context.index_level_est), "quantity_t212": str(context.quantity_t212),
                        "stop_points": str(context.stop_points), "r1": str(context.r1)})
    append_event(db, "fill", service.MODE, payload, position.id, now=now)
    cfd_journal.create_for_signal(db, position, context, key=key)  # the owner's own record, not the ledger
    return position


def _reason(position: DeskPosition, key: str, ctx: CfdContext | None) -> str:
    label = position.underlying_label
    us = MARKETS[key][2] == "USD"
    until = "10:00 New York (16:00 Berlin)" if us else "09:30 Berlin"
    if ctx is None:
        return f"{label} went {'up' if position.direction == 'long' else 'down'} from yesterday's close to {until}"
    return f"{label} {ctx.r1:+.2%} from yesterday's close to {until}"


def evidence_line(db: Session) -> str:
    """One honest line on how strong the index rule's evidence is: the 2016-26 replay and the live paper cell."""
    from . import scoring

    s = scoring.scorecard(db)["cells"][CELL]
    if s["n"] < s["n_min"]:
        word = "too early"
    elif s["mean_net"] is None or s["mean_net"] <= 0:
        word = "failing"
    else:
        word = "passing" if s.get("passes") else "not significant"
    mean = f", {s['mean_net']:+.2f} EUR a trade" if s["mean_net"] is not None else ""
    return (f"Evidence: replay 2016-26 -0.18 EUR a trade (SPY, -0.13 QQQ) | live paper {s['n']} trades{mean} ({word}).")


def open_message(db: Session, position: DeskPosition, key: str, now: datetime,
                 ctx: CfdContext | None = None) -> str:
    name = MARKETS[key][0]
    exit_at = service._aware(position.max_exit_at)
    exit_local = exit_at.astimezone(cal.BERLIN).strftime("%H:%M") if exit_at else "?"
    if ctx is None:
        exposure = Decimal(position.entry_price or 0) * Decimal(position.quantity or 0)
        side = "BUY" if position.direction == "long" else "SELL"
        stop_move = abs(Decimal(position.stop_price or 0) / Decimal(position.entry_price or 1) - 1) * 100
        lines = [
            f"CFD signal (manual, Trading 212): {side} {name} now",
            f"Index rule: {_reason(position, key, None)}.",
            f"Size: {exposure:.0f} EUR exposure" + (f", quantity {h}" if (h := quantity_hint(db, key, exposure, now)) else ""),
            f"Stop-loss {stop_move:.2f} % away (about {LIMITS.max_loss_per_trade} EUR). Close at {exit_local} Berlin; "
            "I will message you.",
            "Paper-scored here as index_momentum:cfd. Most retail CFD accounts lose money; nothing is ordered for you.",
        ]
        return "\n".join(lines)
    enter_by = ctx.enter_by.astimezone(cal.BERLIN).strftime("%H:%M")
    lines = [
        f"{key} {position.direction}: CFD, manual (Trading 212: {name})",
        f"Why: {_reason(position, key, ctx)} (rule: the last 30 minutes follow it)",
        f"Quantity {ctx.quantity_t212:.4f} (\u2248 {ctx.exposure_eur:,.0f} EUR, margin \u2248 {ctx.margin_eur:,.2f} EUR). "
        f"Enter by {enter_by} Berlin, otherwise skip today.",
        f"Stop: {ctx.stop_points:,.0f} pts from your fill (1 daily sigma; a 27-minute move this large is rare)",
        f"Typical last-30-minute move \u00b1{ctx.typical_30m_points:,.0f} pts; spread \u2248 {ctx.spread_points:,.1f} pts "
        "(Trading 212, observed)",
        f"Index at the signal \u2248 {ctx.index_level_est:,.0f}; Trading 212's price follows the futures and can differ by "
        "a few points. The stop counts from your fill.",
        f"Close at {exit_local} Berlin; I will tell you.",
    ]
    if ctx.currency == "USD":
        lines.append(f"USD market: {T212['fx_fee_on_result'] * 100:.1f} % FX fee on the result at close.")
    if db is not None:
        lines.append(evidence_line(db))
    lines.append("Paper-scored as index_momentum:cfd. Most retail CFD accounts lose money; nothing is ordered for you.")
    return "\n".join(lines)


PAPER_WINDOW = timedelta(minutes=10)


def paper_lines(db: Session, position: DeskPosition) -> list[str]:
    """The index rule's paper knock-out and warrant on the same underlying, opened with this CFD."""
    from . import instrument

    entry = service._aware(position.entry_at)
    if entry is None:
        return []
    rows = db.query(DeskPosition).filter(
        DeskPosition.user_id == position.user_id, DeskPosition.signal == "index_momentum",
        DeskPosition.underlying_isin == position.underlying_isin, DeskPosition.product_type != "cfd",
        DeskPosition.status.in_(("open", "closed"))).order_by(DeskPosition.product_type, DeskPosition.id).all()
    out = []
    for p in rows:
        at = service._aware(p.entry_at)
        if at is not None and abs(at - entry) <= PAPER_WINDOW:
            out.append(f"  {instrument.short_line(p)}")
    return out


def live_text(db: Session, position: DeskPosition, journal: Any, now: datetime,
              ctx: CfdContext | None = None) -> str | None:
    """The CFD message as it stands at *now*: the v2 text, the paper legs of the same signal, and a status
    block that appears only when something changed (a moved price, the owner's answer, the passed entry
    window, the close). ``None`` when the journal row cannot rebuild the message. No network read."""
    ctx = ctx or context_from_journal(journal)
    if ctx is None:
        return None
    key = journal.market_key
    parts = [open_message(db, position, key, now, ctx)]
    if lines := paper_lines(db, position):
        parts.append("Also on paper (no action):\n" + "\n".join(lines))
    status: list[str] = []
    closed = position.status == "closed"
    local = now.astimezone(cal.BERLIN)
    exit_at = service._aware(position.max_exit_at) or ctx.exit_at
    if closed:
        end = service._aware(position.exit_at)
        hhmm = end.astimezone(cal.BERLIN).strftime("%H:%M") if end else "?"
        parts.insert(0, f"CLOSED {hhmm}: paper result {Decimal(position.net_eur or 0):+.2f} EUR after spread and "
                        f"fees ({position.exit_reason})")
    else:
        last, entry = Decimal(position.last_bid or 0), Decimal(position.entry_price or 0)
        if last > 0 and entry > 0 and last != entry:
            level_now = ctx.index_level_est * last / entry
            move = (last / entry - 1) * (1 if position.direction == "long" else -1)
            pnl = move * entry * Decimal(position.quantity or 0)
            mins = max(0, -int(-(exit_at - now).total_seconds() // 60))
            status.append(f"Now ({local:%H:%M}): {position.underlying_label} index \u2248 {level_now:,.0f} "
                          f"({level_now - ctx.index_level_est:+,.0f} pts since the signal), paper {pnl:+.2f} EUR "
                          f"before costs. Exit {exit_at.astimezone(cal.BERLIN):%H:%M} (in {mins} min).")
        if journal.status == "sent" and now > ctx.enter_by:
            status.append(f"Entry window passed ({ctx.enter_by.astimezone(cal.BERLIN):%H:%M}). "
                          "Skip unless you are already in.")
    status += _journal_line(journal)
    if status:
        parts.append("\n".join(status))
    return "\n".join(parts)


def _journal_line(j: Any) -> list[str]:
    if j.status == "skipped":
        return ["Your trade: skipped"]
    qty = j.quantity if j.quantity is not None else j.quantity_suggested
    if j.status == "entered":
        if j.entry_level is None:
            return ["Your trade: entered (reply to this message with your fill price)"]
        stop = cfd_journal.stop_level(j)
        tail = f", stop {stop:,.1f}" if stop is not None else ""
        return [f"Your trade: entered {Decimal(j.entry_level):,.1f} \u00d7 {Decimal(qty or 0):.4f}{tail}"]
    if j.status == "closed":
        if j.entry_level is not None and j.exit_level is not None:
            res = f", result {Decimal(j.real_pnl_eur):+.2f} EUR" if j.real_pnl_eur is not None else ""
            return [f"Your trade: {Decimal(j.entry_level):,.1f} \u2192 {Decimal(j.exit_level):,.1f}{res}"]
        return ["Your trade: closed"]
    return []


def close(db: Session, position: DeskPosition, price: Decimal, reason: str, now: datetime) -> bool:
    """Paper exit at the reference price: spread and (USD) currency fee are the only costs. ``False``
    when the position was no longer open (closed concurrently, see ``service.claim_open``)."""
    if not service.claim_open(db, position):
        return False
    entry = Decimal(position.entry_price or 0)
    qty = Decimal(position.quantity or 0)
    exposure = entry * qty
    raw = price / entry - 1 if entry > 0 else Decimal(0)
    move = raw if position.direction == "long" else -raw
    gross = move * exposure
    usd = position.underlying_isin.startswith("US")
    fx_fee = (abs(gross) * FX_FEE) if usd else Decimal(0)
    spread_cost = Decimal(position.spread_pct or 0) * exposure
    position.exit_price, position.exit_at, position.exit_reason = price, now.astimezone(UTC), reason
    position.exit_fee = fx_fee.quantize(Decimal("0.0001"))
    position.net_eur = (gross - spread_cost - fx_fee).quantize(Decimal("0.0001"))
    position.underlying_move = move
    position.status = "closed"
    append_event(db, "exit", service.MODE, {
        "reason": reason, "price": str(price), "product_type": "cfd", "gross_eur": str(gross.quantize(Decimal("0.0001"))),
        "spread_eur": str(spread_cost.quantize(Decimal("0.0001"))), "fx_fee_eur": str(position.exit_fee),
        "net_eur": str(position.net_eur), "underlying_move": str(move)}, position.id, now=now)
    return True


def owner_close(db: Session, position: DeskPosition, cli: ScalableCli, now: datetime,
                underlying: Underlying | None = None) -> bool:
    """``/close`` or the app's Close on a paper CFD: exit at a fresh reference price now. Without one
    nothing is closed (``False``): the last stored price may be minutes old."""
    if position.status != "open" or position.product_type != "cfd":
        return False
    if position.cell in EVENT_CELL_NAMES:
        # The event desk's own close (overnight interest, its costs): ``events`` registers it on import.
        return EVENT_OWNER_CLOSE is not None and EVENT_OWNER_CLOSE(db, position, cli, now)
    price = service.underlying_price(db, cli, service.underlying_of(position, underlying), now=now)
    if price is None or price <= 0:
        return False
    return close(db, position, price, "owner_close", now)


def close_any(db: Session, position: DeskPosition, cli: ScalableCli, now: datetime,
              underlying: Underlying | None = None) -> bool:
    """The owner's close for any desk position: CFDs here, Scalable products in ``service.owner_close``."""
    if position.product_type == "cfd":
        return owner_close(db, position, cli, now, underlying)
    return service.owner_close(db, position, cli, now, underlying)


def _stopped(position: DeskPosition, price: Decimal) -> bool:
    stop = Decimal(position.stop_price or 0)
    return price <= stop if position.direction == "long" else price >= stop


def manage(db: Session, cli: ScalableCli, now: datetime, underlyings: dict[str, Underlying],
           errors: list[str] | None = None) -> list[tuple[DeskPosition, str]]:
    """Close open paper CFDs at the exit time, at the stop, on a halt, or when the price is lost.
    Every price is read first, then the closes are written (as ``service.manage_open_positions``)."""
    closed: list[tuple[DeskPosition, str]] = []
    exits: list[tuple[DeskPosition, Decimal, str]] = []
    halted = service.is_halted(db)
    rows = db.query(DeskPosition).filter(DeskPosition.status == "open", DeskPosition.mode == service.MODE,
                                         DeskPosition.product_type == "cfd",
                                         or_(DeskPosition.cell.is_(None), DeskPosition.cell.notin_(EVENT_CELL_NAMES))).all()
    for p in rows:
        # Only a fresh price moves the stop and resets the quote-lost clock (a frozen proxy quote is None).
        price = service.underlying_price(db, cli, service.underlying_of(p, underlyings.get(p.underlying_isin)),
                                         now=now, errors=errors)
        if price is not None and price > 0:
            p.last_bid, p.last_quote_at = price, now.astimezone(UTC)
        exit_at = service._aware(p.max_exit_at)
        last_at = service._aware(p.last_quote_at)
        reason = None
        if halted:
            reason = "halt"
        elif price is not None and price > 0 and _stopped(p, price):
            reason = "stop"
        elif exit_at is not None and now >= exit_at:
            reason = "time"
        elif last_at is not None and (now - last_at).total_seconds() > LIMITS.quote_lost_s:
            reason = "quote_lost"
        if reason is None:
            continue
        if (price is None or price <= 0) and reason in ("halt", "time") and service.waits_for_price(p, now):
            continue  # as the Scalable products: wait for a fresh price (10 minutes, or the day's last tick)
        px = price if price is not None and price > 0 else Decimal(p.last_bid or 0)
        if px <= 0:
            continue
        exits.append((p, px, reason))
    for p, px, reason in exits:
        if close(db, p, px, reason, now):
            closed.append((p, reason))
    return closed


def close_message(position: DeskPosition, reason: str, ctx_or_journal: Any = None) -> str:
    """The close line, always a new message so that it notifies. With a context or journal row (anything
    with ``exposure_eur``) it names the rule's exit and what holding on would cost."""
    name = (position.product_name or "CFD").replace(" CFD (Trading 212, manual)", "")
    exposure = getattr(ctx_or_journal, "exposure_eur", None)
    if exposure is None:
        why = {"time": "3 minutes before the close", "stop": "stop-loss level reached", "halt": "desk halted",
               "quote_lost": "price feed lost", "owner_close": "you closed it"}.get(reason, reason)
        return (f"CFD: CLOSE your {name} position now ({why}). Paper result {position.net_eur} EUR "
                f"after spread and fees.")
    exit_at = service._aware(position.exit_at) or service._aware(position.max_exit_at)
    hhmm = exit_at.astimezone(cal.BERLIN).strftime("%H:%M") if exit_at else "?"
    why = {"time": f"{hhmm}, the rule's exit", "stop": "stop-loss level reached", "halt": "desk halted",
           "quote_lost": "price feed lost", "owner_close": "you closed it"}.get(reason, reason)
    night = Decimal(exposure) * T212["overnight_rate"]
    text = (f"{position.underlying_label} CFD: CLOSE now ({why}). Paper result {position.net_eur} EUR after spread and "
            f"fees.\nThe rule's trade ends here. Holding on is a new trade without a tested edge: overnight interest "
            f"\u2248 {night:,.2f} EUR per night on {Decimal(exposure):,.0f} EUR, plus gap risk.")
    status = getattr(ctx_or_journal, "status", None)
    if status in ("entered", "closed") and getattr(ctx_or_journal, "exit_level", 0) is None:
        missing = "<exit price>" if getattr(ctx_or_journal, "entry_level", None) is not None \
            else "<entry price> <exit price>"
        text += (f"\nTo log your trade ({position.id[:8]}), reply to this message with: {missing}, as Trading 212 "
                 "shows them.")
    return text
