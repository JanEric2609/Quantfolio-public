"""The position lifecycle (ADR 0027): propose, fill, hold, close. Paper only.

Every state change is one ledger event written in the same transaction as the
position update; the caller commits.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli, ScalableError
from app.foundation.settings import get_setting_row, upsert_public_settings

from . import calendar as cal
from . import news, prereg
from .guard import DeskState, Proposal, Verdict, check_entry, check_exit
from .ledger import append_event, events_for
from .limits import LIMITS
from .products import Candidate, select_product
from .sizing import size_position
from .underlyings import Underlying, daily_sigma, last_close_per_day

logger = logging.getLogger(__name__)

HALT_KEY = "desk_halt_json"
HEALTH_KEY = "desk_health_json"
MODE = "paper"


# -- halt and health (internal app_settings state, written only here) --------------


def halt_info(db: Session) -> dict[str, Any]:
    value = get_setting_row(db, HALT_KEY)
    return value if isinstance(value, dict) else {"halted": False}


def is_halted(db: Session) -> bool:
    return bool(halt_info(db).get("halted"))


def set_halt(db: Session, halted: bool, reason: str, by: str, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    upsert_public_settings(db, {HALT_KEY: {"halted": halted, "reason": reason[:200], "at": now.isoformat(), "by": by}})
    append_event(db, "halt" if halted else "resume", MODE, {"reason": reason[:200], "by": by}, now=now)
    db.commit()


def health(db: Session) -> dict[str, Any]:
    value = get_setting_row(db, HEALTH_KEY)
    return value if isinstance(value, dict) else {}


def set_health(db: Session, **fields: Any) -> None:
    upsert_public_settings(db, {HEALTH_KEY: {**health(db), **fields}})


# -- live rehearsal: what the live limits would have said ---------------------------


def _day_start(now: datetime) -> datetime:
    local = now.astimezone(cal.BERLIN)
    return datetime.combine(local.date(), datetime.min.time(), tzinfo=cal.BERLIN).astimezone(UTC)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def build_state(db: Session, user_id: str, now: datetime) -> DeskState:
    """Counts over positions that live would have taken (``would_take_live``), in time order."""
    start = _day_start(now)
    rows = db.query(DeskPosition).filter(
        DeskPosition.user_id == user_id, DeskPosition.mode == MODE, DeskPosition.would_take_live.is_(True),
        DeskPosition.status.in_(("open", "closed")),
    ).all()
    open_rows = [r for r in rows if r.status == "open"]
    today = [r for r in rows if (e := _aware(r.entry_at)) and e >= start]
    loss = sum((-(r.net_eur) for r in rows if r.status == "closed" and (x := _aware(r.exit_at)) and x >= start
                and r.net_eur is not None and r.net_eur < 0), Decimal(0))
    used = sum((Decimal(r.entry_price or 0) * (r.units or 0) for r in open_rows), Decimal(0))
    return DeskState(halted=is_halted(db), open_live=len(open_rows), entries_today=len(today),
                     loss_today=loss, pot_used=used)


# -- underlying reference price and volatility -------------------------------------


def underlying_of(position: DeskPosition, known: Underlying | None = None) -> Underlying:
    """The position's underlying: the alias when it is one, else rebuilt from the row (single stocks)."""
    if known is not None:
        return known
    from .underlyings import ALIASES

    isin = position.underlying_isin or ""
    alias = next((u for u in ALIASES.values() if u.isin == isin), None)
    if alias is not None:  # an index: its quoted proxy (ETF or Alpaca symbol) is the price source
        return alias
    return Underlying(isin, position.underlying_label or isin, "XNYS" if isin.startswith("US") else "XETR",
                      bool(position.liquid))


def underlying_price(db: Session, cli: ScalableCli, u: Underlying) -> Decimal | None:
    """A reference price for the underlying: Alpaca for the US indices, an ETF proxy or the line itself."""
    try:
        if u.alpaca_symbol:
            provider = news.alpaca_for(db)
            if provider is None:
                return None
            quote = provider.get_quote(u.alpaca_symbol)
            data = quote.get("data") if isinstance(quote, dict) else None
            return Decimal(str(data["close"])) if data and data.get("close") else None
        q = market.get_quote(cli, u.proxy_isin or u.isin)
        return q.mid if q else None
    except (ScalableError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        logger.info("desk underlying read failed: %s", type(exc).__name__)
        return None


def underlying_sigma(db: Session, cli: ScalableCli, u: Underlying) -> Decimal | None:
    """Daily sigma over the last 20 sessions: daily Alpaca bars for the US indices, else sc's 3m chart."""
    try:
        if u.alpaca_symbol:
            provider = news.alpaca_for(db)
            if provider is None:
                return None
            result = provider.get_history(u.alpaca_symbol, days=40)
            rows = (result.get("data") if isinstance(result, dict) else None) or []
            return daily_sigma([r["close"] for r in rows if r.get("close")])
        points = market.get_chart(cli, u.proxy_isin or u.isin, "3m")
        return daily_sigma(last_close_per_day(points))
    except (ScalableError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        logger.info("desk underlying read failed: %s", type(exc).__name__)
        return None


# -- proposing ---------------------------------------------------------------------


@dataclass
class ProposalResult:
    position: DeskPosition | None
    reason: str | None = None  # why nothing was proposed
    nonce: str | None = None
    selection_size: int = 0
    notes: list[str] | None = None


def _cell(signal: str, product_type: str) -> str:
    return f"{signal}:{product_type}"


def _quote_age(now: datetime, ts: datetime | None) -> float:
    return (now - ts).total_seconds() if ts else float("inf")


def propose(
    db: Session, user_id: str, *, signal: str, underlying: Underlying, direction: str, product_type: str,
    cli: ScalableCli, now: datetime, event_flag: bool = False, horizon_days: int = 1,
    max_exit_at: datetime | None = None, symbol: str | None = None,
) -> ProposalResult:
    """Pick a product, size it, run the guard and write the ledger. No fill happens here."""
    sigma = underlying_sigma(db, cli, underlying)
    base = {"signal": signal, "underlying": underlying.isin, "direction": direction, "product_type": product_type}
    if sigma is None or sigma <= 0:
        append_event(db, "guard_verdict", MODE, {**base, "ok": False, "reasons": ["no_volatility"]}, now=now)
        return ProposalResult(None, "no_volatility")
    selection = select_product(cli, underlying.isin, direction, product_type, now, sigma=sigma, liquid=underlying.liquid)
    append_event(db, "candidates", MODE, {
        **base, "searched": selection.searched, "sigma": str(sigma),
        "candidates": [c.as_json() for c in selection.candidates], "dropped": selection.dropped[:50]}, now=now)
    chosen = selection.chosen
    if chosen is None:
        append_event(db, "guard_verdict", MODE, {**base, "ok": False, "reasons": ["no_candidate"]}, now=now)
        return ProposalResult(None, "no_candidate", selection_size=selection.searched)

    state = build_state(db, user_id, now)
    sizing = size_position(chosen.ask, chosen.leverage_or_omega, sigma, LIMITS.pot - state.pot_used)
    window_end = max_exit_at or cal.max_exit_at(now, days=horizon_days)
    earnings = news.earnings_in_window(db, symbol, now.date(), window_end.date())
    macro = tuple(cal.macro_events_between(now.astimezone(cal.BERLIN).date(), window_end.date())) \
        if underlying.is_index else ()
    proposal = Proposal(
        signal=signal, product_type=product_type, units=sizing.units, ask=chosen.ask, notional=sizing.notional,
        worst_case_loss=sizing.worst_case_loss, spread_pct=chosen.spread_pct, liquid=underlying.liquid,
        quote_age_s=chosen.quote_age_s, exchange=underlying.exchange, days_to_expiry=chosen.days_to_expiry,
        earnings_in_window=earnings, event_flag=event_flag, macro_events=macro, is_index=underlying.is_index)
    verdict = check_entry(state, proposal, now)
    append_event(db, "guard_verdict", MODE, {
        **base, "isin": chosen.isin, "ok": verdict.ok, "paper_ok": verdict.paper_ok,
        "reasons": list(verdict.reasons), "units": sizing.units, "stop": str(sizing.stop_price)}, now=now)

    position = DeskPosition(
        user_id=user_id, mode=MODE, cell=_cell(signal, product_type), signal=signal, product_type=product_type,
        isin=chosen.isin, issuer=chosen.issuer, underlying_isin=underlying.isin, underlying_label=underlying.label,
        direction=direction, liquid=underlying.liquid, leverage_or_omega=chosen.leverage_or_omega,
        units=sizing.units, entry_price=chosen.ask, stop_price=sizing.stop_price, max_exit_at=window_end,
        spread_pct=chosen.spread_pct, status="proposed", would_take_live=False, prereg_hash=prereg.PREREG_HASH,
        product_name=chosen.name[:120] or None, wkn=chosen.wkn, barrier_or_strike=chosen.barrier_or_strike,
        expiry=chosen.expiry, created_at=now.astimezone(UTC))
    db.add(position)
    db.flush()
    if not verdict.paper_ok:
        position.status = "rejected"
        append_event(db, "proposal", MODE, {**_proposal_payload(position, chosen, sizing, verdict), "rejected": True},
                     position.id, now=now)
        return ProposalResult(position, ",".join(verdict.paper_reasons))
    nonce = secrets.token_urlsafe(8)
    headlines = news.headlines(db, cli, underlying, now - timedelta(hours=24), symbol)
    append_event(db, "proposal", MODE, {
        **_proposal_payload(position, chosen, sizing, verdict), "nonce": nonce, "proposed_at": now.isoformat(),
        "news_seen": [h["title"] for h in headlines[:5]], "earnings_in_window": earnings}, position.id, now=now)
    return ProposalResult(position, None, nonce, selection.searched, [h["title"] for h in headlines[:3]])


def _proposal_payload(position: DeskPosition, c: Candidate, sizing, verdict: Verdict) -> dict[str, Any]:
    return {
        "isin": c.isin, "wkn": c.wkn, "name": c.name, "issuer": c.issuer, "product_type": c.product_type,
        "barrier_or_strike": None if c.barrier_or_strike is None else str(c.barrier_or_strike),
        "expiry": c.expiry.isoformat() if c.expiry else None, "units": sizing.units,
        "ask": str(c.ask), "bid": str(c.bid), "spread_pct": str(c.spread_pct), "stop": str(sizing.stop_price),
        "stop_fraction": str(sizing.stop_fraction), "notional": str(sizing.notional),
        "worst_case_loss": str(sizing.worst_case_loss), "leverage_or_omega": str(c.leverage_or_omega),
        "would_take_live": verdict.ok, "live_reasons": list(verdict.reasons),
    }


# -- taking and filling -------------------------------------------------------------


@dataclass
class TapResult:
    status: str  # filled | refused | expired | replay | reask | unknown | not_open | no_quote
    position: DeskPosition | None = None
    nonce: str | None = None
    message: str = ""


def _latest_proposal(db: Session, position_id: str) -> dict[str, Any] | None:
    events = events_for(db, position_id, "proposal")
    return json.loads(events[-1].payload) if events else None


def take(db: Session, position_id: str, nonce: str, cli: ScalableCli, now: datetime,
         underlying: Underlying | None = None) -> TapResult:
    """A tap on "Paper: take": single-use nonce, 180 s window, fill at a fresh quote."""
    position = db.get(DeskPosition, position_id)
    proposal = _latest_proposal(db, position_id) if position else None
    if position is None or proposal is None or proposal.get("nonce") != nonce:
        append_event(db, "tap", MODE, {"result": "unknown", "position_id": position_id}, now=now)
        return TapResult("unknown", message="Unknown proposal.")
    if any(json.loads(e.payload).get("nonce") == nonce for e in events_for(db, position_id, "tap")):
        append_event(db, "tap", MODE, {"result": "replay", "nonce": nonce}, position_id, now=now)
        return TapResult("replay", position, message="Already used.")
    if position.status != "proposed":
        append_event(db, "tap", MODE, {"result": "not_open", "nonce": nonce, "status": position.status},
                     position_id, now=now)
        return TapResult("not_open", position, message=f"Proposal is {position.status}.")
    proposed_at = datetime.fromisoformat(proposal["proposed_at"])
    if now - proposed_at > timedelta(seconds=LIMITS.tap_window_s):
        position.status = "expired"
        append_event(db, "tap", MODE, {"result": "expired", "nonce": nonce}, position_id, now=now)
        return TapResult("expired", position, message="Expired.")
    quote = market.get_quote(cli, position.isin)
    if quote is None or quote.outdated or _quote_age(now, quote.ts_utc) > LIMITS.max_quote_age_s:
        append_event(db, "tap", MODE, {"result": "no_quote", "nonce": nonce}, position_id, now=now)
        return TapResult("no_quote", position, message="No fresh quote; try again.")
    old = Decimal(proposal["ask"])
    if abs(quote.ask / old - 1) > LIMITS.reask_move:
        new_nonce = secrets.token_urlsafe(8)
        sizing = size_position(quote.ask, Decimal(position.leverage_or_omega or 1), _sigma_from_stop(position, old),
                               LIMITS.pot)
        position.entry_price, position.stop_price, position.units = quote.ask, sizing.stop_price, sizing.units
        append_event(db, "tap", MODE, {"result": "reask", "nonce": nonce, "old_ask": str(old),
                                       "new_ask": str(quote.ask)}, position_id, now=now)
        append_event(db, "proposal", MODE, {**proposal, "ask": str(quote.ask), "bid": str(quote.bid),
                                            "stop": str(sizing.stop_price), "units": sizing.units,
                                            "nonce": new_nonce, "proposed_at": now.isoformat(), "reprice": True},
                     position_id, now=now)
        return TapResult("reask", position, new_nonce, "The price moved; please confirm again.")
    append_event(db, "tap", MODE, {"result": "taken", "nonce": nonce}, position_id, now=now)
    # cli is passed so the underlying's entry price is stored: the placebo needs the underlying move.
    refused = open_position(db, position, quote, now, underlying_of(position, underlying), cli)
    if refused:
        return TapResult("refused", position, message="Not filled: " + ", ".join(refused) + ".")
    return TapResult("filled", position, message="Paper position opened.")


def _sigma_from_stop(position: DeskPosition, old_ask: Decimal) -> Decimal:
    """Recover sigma from the stored stop so a re-ask keeps the same stop fraction."""
    lev = Decimal(position.leverage_or_omega or 1)
    fraction = (old_ask - Decimal(position.stop_price or old_ask)) / old_ask
    return fraction / lev if lev else fraction


#: Entry reasons that also refuse a fill: the desk was halted, or the clock ran past the entry
#: window, between the proposal and the tap. Price and size were checked at the proposal (a
#: re-ask re-sizes), so quote noise inside the 180 s window does not refuse a fill.
FILL_BLOCKING = frozenset({"halted", "after_cutoff", "outside_window", "underlying_closed"})


def open_position(db: Session, position: DeskPosition, quote: market.Quote, now: datetime,
                  underlying: Underlying | None = None, cli: ScalableCli | None = None) -> tuple[str, ...]:
    """Fill at the quote's ask and record the fill, the simulated stop and the live rehearsal.

    Returns the reasons the fill was refused (the position is then ``rejected``), else ``()``."""
    state = build_state(db, position.user_id, now)
    units = position.units or 0
    stop = Decimal(position.stop_price or 0)
    proposal = Proposal(
        signal=position.signal, product_type=position.product_type, units=units, ask=quote.ask,
        notional=quote.ask * units, worst_case_loss=(quote.ask - stop) * units + 2 * LIMITS.fee_per_order,
        spread_pct=Decimal(position.spread_pct or 0), liquid=position.liquid, quote_age_s=_quote_age(now, quote.ts_utc),
        exchange="XNYS" if (underlying and underlying.exchange == "XNYS") else "XETR",
        days_to_expiry=99 if position.product_type == "warrant" else None,
        is_index=bool(underlying and underlying.is_index))
    verdict = check_entry(state, proposal, now)
    refused = tuple(r for r in verdict.reasons if r in FILL_BLOCKING)
    if refused:
        position.status = "rejected"
        append_event(db, "error", MODE, {"code": "refused_at_fill", "reasons": list(refused)}, position.id, now=now)
        return refused
    position.entry_price = quote.ask
    position.entry_fee = LIMITS.fee_per_order
    position.entry_at = now.astimezone(UTC)
    position.last_bid, position.last_quote_at = quote.bid, now.astimezone(UTC)
    position.spread_pct = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2)
    position.status = "open"
    position.would_take_live = verdict.ok
    if position.max_exit_at is None:
        position.max_exit_at = cal.max_exit_at(now)
    if underlying is not None and cli is not None:
        position.underlying_entry = underlying_price(db, cli, underlying)
    append_event(db, "fill", MODE, {
        "price": str(quote.ask), "bid": str(quote.bid), "units": units, "fee": str(LIMITS.fee_per_order),
        "would_take_live": verdict.ok, "live_reasons": list(verdict.reasons)}, position.id, now=now)
    append_event(db, "stop_set", MODE, {"stop": str(stop), "simulated": True,
                                        "max_exit_at": position.max_exit_at.isoformat()}, position.id, now=now)
    return ()


def open_directly(db: Session, position: DeskPosition, cli: ScalableCli, now: datetime,
                  underlying: Underlying) -> bool:
    """Rule-based signals open without a tap: fill at a fresh quote."""
    quote = market.get_quote(cli, position.isin)
    if quote is None or quote.outdated or _quote_age(now, quote.ts_utc) > LIMITS.max_quote_age_s:
        position.status = "rejected"
        append_event(db, "error", MODE, {"code": "no_fresh_quote_at_fill", "isin": position.isin}, position.id, now=now)
        return False
    return not open_position(db, position, quote, now, underlying, cli)


# -- closing -----------------------------------------------------------------------


def close_position(db: Session, position: DeskPosition, bid: Decimal, reason: str, now: datetime,
                   underlying_exit: Decimal | None = None) -> None:
    units = position.units or 0
    entry = Decimal(position.entry_price or 0)
    fee = LIMITS.fee_per_order
    position.exit_price, position.exit_fee, position.exit_at, position.exit_reason = bid, fee, now.astimezone(UTC), reason
    position.net_eur = ((bid - entry) * units - Decimal(position.entry_fee or 0) - fee).quantize(Decimal("0.0001"))
    if underlying_exit and position.underlying_entry and Decimal(position.underlying_entry) > 0:
        raw = underlying_exit / Decimal(position.underlying_entry) - 1
        position.underlying_move = raw if position.direction == "long" else -raw
    position.status = "closed"
    append_event(db, "exit", MODE, {
        "reason": reason, "price": str(bid), "units": units, "fee": str(fee), "net_eur": str(position.net_eur),
        "underlying_move": None if position.underlying_move is None else str(position.underlying_move)},
        position.id, now=now)


def expire_proposals(db: Session, now: datetime) -> int:
    count = 0
    for p in db.query(DeskPosition).filter(DeskPosition.status == "proposed", DeskPosition.mode == MODE).all():
        proposal = _latest_proposal(db, p.id)
        if proposal is None or now - datetime.fromisoformat(proposal["proposed_at"]) > timedelta(
                seconds=LIMITS.tap_window_s):
            p.status = "expired"
            append_event(db, "expired", MODE, {"reason": "no_tap"}, p.id, now=now)
            count += 1
    return count


def manage_open_positions(db: Session, cli: ScalableCli, now: datetime, underlyings: dict[str, Underlying]) -> list[
        tuple[DeskPosition, str]]:
    """Quote every open position once and apply ``check_exit``. Returns the positions closed this tick."""
    closed: list[tuple[DeskPosition, str]] = []
    halted = is_halted(db)
    rows = db.query(DeskPosition).filter(DeskPosition.status == "open", DeskPosition.mode == MODE,
                                         DeskPosition.product_type != "cfd").all()  # CFDs: cfd.manage
    for p in rows:
        quote = None
        try:
            quote = market.get_quote(cli, p.isin)
        except ScalableError:
            quote = None
        fresh = quote is not None and not quote.outdated and _quote_age(now, quote.ts_utc) <= LIMITS.max_quote_age_s
        bid = quote.bid if fresh and quote else None
        if bid is not None:
            p.last_bid, p.last_quote_at = bid, now.astimezone(UTC)
        reason = check_exit(p, bid, now, halted=halted)
        if reason is None:
            continue
        exit_bid = bid if bid is not None else Decimal(p.last_bid or 0)
        if exit_bid <= 0:
            continue
        close_position(db, p, exit_bid, reason, now, underlying_price(db, cli, underlying_of(p, underlyings.get(p.underlying_isin))))
        closed.append((p, reason))
    return closed


def owner_close(db: Session, position: DeskPosition, cli: ScalableCli, now: datetime,
                underlying: Underlying | None = None) -> bool:
    if position.status != "open":
        return False
    if position.product_type == "cfd":
        return False  # a CFD closes through cfd.owner_close (no import back from here)
    quote = market.get_quote(cli, position.isin)
    bid = quote.bid if quote else Decimal(position.last_bid or 0)
    if bid <= 0:
        return False
    close_position(db, position, bid, "owner_close", now, underlying_price(db, cli, underlying_of(position, underlying)))
    return True
