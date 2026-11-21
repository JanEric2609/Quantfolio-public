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
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition
from app.foundation.scalable.cli import ScalableCli

from . import calendar as cal
from . import prereg, service
from .ledger import append_event
from .limits import LIMITS
from .underlyings import Underlying

logger = logging.getLogger(__name__)

CELL = "index_momentum:cfd"
SPEC = prereg.SPEC["cfd"]
SPREAD_BP: dict[str, Decimal] = {k: Decimal(v) for k, v in SPEC["spread_bp"].items()}
FX_FEE = Decimal(SPEC["fx_fee_on_result"])
MAX_EXPOSURE_EUR = Decimal(SPEC["max_exposure_eur"])
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
    base = {"signal": "index_momentum", "underlying": underlying.isin, "direction": direction, "product_type": "cfd"}
    ref = service.underlying_price(db, cli, underlying, now=now)
    sigma = service.underlying_sigma(db, cli, underlying)
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
    append_event(db, "fill", service.MODE, {
        "product_type": "cfd", "market": name, "reference_price": str(ref), "exposure_eur": str(exposure),
        "spread_bp": str(SPREAD_BP[key]), "stop": str(position.stop_price), "would_take_live": False,
        "manual": True, "max_exit_at": exit_at.isoformat()}, position.id, now=now)
    return position


def open_message(db: Session, position: DeskPosition, key: str, now: datetime) -> str:
    name = MARKETS[key][0]
    exposure = Decimal(position.entry_price or 0) * Decimal(position.quantity or 0)
    side = "BUY" if position.direction == "long" else "SELL"
    exit_at = service._aware(position.max_exit_at)
    exit_local = exit_at.astimezone(cal.BERLIN).strftime("%H:%M") if exit_at else "?"
    stop_move = abs(Decimal(position.stop_price or 0) / Decimal(position.entry_price or 1) - 1) * 100
    lines = [
        f"CFD signal (manual, Trading 212): {side} {name} now",
        f"Index rule: {position.underlying_label} first half hour was {'up' if side == 'BUY' else 'down'}.",
        f"Size: {exposure:.0f} EUR exposure" + (f", quantity {h}" if (h := quantity_hint(db, key, exposure, now)) else ""),
        f"Stop-loss {stop_move:.2f} % away (about {LIMITS.max_loss_per_trade} EUR). Close at {exit_local} Berlin; "
        "I will message you.",
        "Paper-scored here as index_momentum:cfd. Most retail CFD accounts lose money; nothing is ordered for you.",
    ]
    return "\n".join(lines)


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
                                         DeskPosition.product_type == "cfd").all()
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
        px = price if price is not None and price > 0 else Decimal(p.last_bid or 0)
        if px <= 0:
            continue
        exits.append((p, px, reason))
    for p, px, reason in exits:
        if close(db, p, px, reason, now):
            closed.append((p, reason))
    return closed


def close_message(position: DeskPosition, reason: str) -> str:
    name = (position.product_name or "CFD").replace(" CFD (Trading 212, manual)", "")
    why = {"time": "3 minutes before the close", "stop": "stop-loss level reached", "halt": "desk halted",
           "quote_lost": "price feed lost", "owner_close": "you closed it"}.get(reason, reason)
    return (f"CFD: CLOSE your {name} position now ({why}). Paper result {position.net_eur} EUR "
            f"after spread and fees.")
