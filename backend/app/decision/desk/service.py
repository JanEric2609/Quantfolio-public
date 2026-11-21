"""The position lifecycle (ADR 0027): propose, fill, hold, close. Paper only.

Every state change is one ledger event written in the same transaction as the
position update; the caller commits.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli, ScalableError
from app.foundation.settings import get_setting_row, upsert_public_settings

from . import calendar as cal
from . import news, prereg
from .guard import DeskState, Proposal, Verdict, check_entry, check_exit
from .ledger import append_event, desk_write_lock, events_for
from .limits import LIMITS
from .products import Candidate, select_product
from .sizing import size_position
from .underlyings import Underlying, daily_sigma, last_close_per_day

logger = logging.getLogger(__name__)

HALT_KEY = "desk_halt_json"
HEALTH_KEY = "desk_health_json"
#: The news stream's own state: written by the stream thread only, so it never races the monitor's
#: read-modify-write of ``HEALTH_KEY`` (two writers on one JSON row lose each other's fields).
STREAM_KEY = "desk_news_stream_json"
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
    """The monitor's health fields plus the news stream's (two keys, one writer each)."""
    value = get_setting_row(db, HEALTH_KEY)
    out = dict(value) if isinstance(value, dict) else {}
    stream = get_setting_row(db, STREAM_KEY)
    if isinstance(stream, dict):
        out.update(stream)
    return out


def set_health(db: Session, **fields: Any) -> None:
    """Merge *fields* into the monitor's key. Only the 1-minute monitor tick calls this."""
    value = get_setting_row(db, HEALTH_KEY)
    upsert_public_settings(db, {HEALTH_KEY: {**(value if isinstance(value, dict) else {}), **fields}})


def set_stream_health(db: Session, state: str, at: datetime) -> None:
    """The news stream thread's state, in its own key. ``news_stream_at`` is when the state began: a
    reconnect loop repeating the same failure keeps it, so "down for 15 minutes" can be seen."""
    value = get_setting_row(db, STREAM_KEY)
    old = value if isinstance(value, dict) else {}
    since = old.get("news_stream_at") if old.get("news_stream") == state and old.get("news_stream_at") else at.isoformat()
    upsert_public_settings(db, {STREAM_KEY: {"news_stream": state, "news_stream_at": since}})


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


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)[:60]


#: A reference price older than this is not used (no CFD fill, stop or exit at it, no knock-out read).
REF_MAX_AGE_S = LIMITS.max_quote_age_s


def underlying_price(db: Session, cli: ScalableCli, u: Underlying, *, now: datetime,
                     errors: list[str] | None = None) -> Decimal | None:
    """A fresh reference price for the underlying, or ``None``: Alpaca for the US indices, else an ETF
    proxy or the line itself on sc. An outdated quote, or one older than ``REF_MAX_AGE_S`` at *now*, is
    ``None``: a frozen price must not move stops, fill a CFD or reset the quote-lost clock.

    The price is in the proxy's own units (the DAX ETF in EUR, SPY in USD, a US stock's gettex line in
    EUR), never index points: compare it only with levels converted by :func:`barrier_in_reference`.

    An outdated wrapper propagates (the tick fails and says so); any other failed read is ``None`` and
    its code goes to *errors*, so the monitor can show why prices are missing."""
    try:
        if u.alpaca_symbol:
            provider = news.alpaca_for(db)
            if provider is None:
                return None
            quote = provider.get_quote(u.alpaca_symbol)
            if not isinstance(quote, dict):
                return None
            data = quote.get("data")
            as_of = (quote.get("quality") or {}).get("as_of")
            if not data or not data.get("close") or not isinstance(as_of, datetime):
                return None
            if _quote_age(now, _aware(as_of)) > REF_MAX_AGE_S:
                return None
            return Decimal(str(data["close"]))
        q = market.get_quote(cli, u.proxy_isin or u.isin)
        if q is None or q.outdated or _quote_age(now, q.ts_utc) > REF_MAX_AGE_S:
            return None
        return q.mid
    except market.WrapperOutdated:
        raise
    except (ScalableError, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk underlying read failed: %s", type(exc).__name__)
        if errors is not None:
            errors.append(_error_code(exc))
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
    except market.WrapperOutdated:
        raise
    except (ScalableError, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
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


@dataclass
class PreparedProposal:
    """Everything a proposal needs from the network, read before anything is written (see ``propose``)."""
    user_id: str
    signal: str
    product_type: str
    direction: str
    underlying: Underlying
    base: dict[str, Any]
    now: datetime
    clock: Callable[[], datetime] | None
    sigma: Decimal | None = None
    selection: Any = None
    refusal: str | None = None  # why nothing is proposed (written as a guard verdict)
    underlying_ref: Decimal | None = None
    barrier_ref: Decimal | None = None
    window_end: datetime | None = None
    sizing: Any = None
    verdict: Verdict | None = None
    earnings_known: bool | None = None
    headlines: list[dict[str, Any]] = field(default_factory=list)


def propose(
    db: Session, user_id: str, *, signal: str, underlying: Underlying, direction: str, product_type: str,
    cli: ScalableCli, now: datetime, event_flag: bool = False, horizon_days: int = 1,
    max_exit_at: datetime | None = None, symbol: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProposalResult:
    """Pick a product, size it, run the guard and write the ledger. No fill happens here.

    All market reads come first (:func:`prepare_proposal`), the ledger writes last
    (:func:`record_proposal`): the first write takes the desk's write lock until the commit, and the
    monitor's stops must not wait behind twenty sc calls.

    *symbol* (earnings calendar, US headlines) defaults to the underlying's own ticker, if known.

    *clock* (the owner's ideas: the wall clock) re-reads the time after the market reads, so quote ages
    are measured when the quotes are in and the 180 s tap window starts when the proposal is ready, not
    before twenty sc calls. Without it, *now* is used throughout (the index rule, tests)."""
    prep = prepare_proposal(db, user_id, signal=signal, underlying=underlying, direction=direction,
                            product_type=product_type, cli=cli, now=now, event_flag=event_flag,
                            horizon_days=horizon_days, max_exit_at=max_exit_at, symbol=symbol, clock=clock)
    return record_proposal(db, prep)


def prepare_proposal(
    db: Session, user_id: str, *, signal: str, underlying: Underlying, direction: str, product_type: str,
    cli: ScalableCli, now: datetime, event_flag: bool = False, horizon_days: int = 1,
    max_exit_at: datetime | None = None, symbol: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PreparedProposal:
    """The read half of :func:`propose`: volatility, product search and quotes, the reference price,
    earnings, the guard verdict and headlines. Writes nothing."""
    symbol = symbol or underlying.symbol
    base = {"signal": signal, "underlying": underlying.isin, "direction": direction, "product_type": product_type}
    prep = PreparedProposal(user_id, signal, product_type, direction, underlying, base, now, clock)
    prep.sigma = sigma = underlying_sigma(db, cli, underlying)
    if sigma is None or sigma <= 0:
        prep.refusal = "no_volatility"
        return prep
    prep.selection = selection = select_product(cli, underlying.isin, direction, product_type, now, sigma=sigma,
                                                liquid=underlying.liquid, clock=clock)
    prep.now = now = clock() if clock else now
    chosen = selection.chosen
    if chosen is None:
        prep.refusal = "no_candidate"
        return prep
    # A knock-out's barrier in the reference price's units, from the listing's distance and a reference
    # price read in the same snapshot (the distance is only true for the level it was measured at).
    # Read for every product: the fill falls back to it when its own read fails (the placebo needs the move).
    prep.underlying_ref = underlying_price(db, cli, underlying, now=now)
    if product_type == "knockout":
        prep.barrier_ref = barrier_in_reference(direction, prep.underlying_ref, chosen.barrier_distance)
        if prep.barrier_ref is None:  # a knock-out the desk could not watch for its barrier is not proposed
            prep.refusal = NO_REFERENCE
            return prep
    prep.window_end = window_end = max_exit_at or cal.max_exit_at(now, days=horizon_days)
    if window_end <= now:
        # "today" after 21:30 (or any horizon on a flat-by day): the position would close at the next tick.
        prep.refusal = EXIT_PASSED
        return prep
    state = build_state(db, user_id, now)
    # Paper is sized against the whole pot: the pot left over is a live-only cap (``pot_exceeded``).
    prep.sizing = sizing = size_position(chosen.ask, chosen.leverage_or_omega, sigma, LIMITS.pot)
    prep.earnings_known = news.earnings_in_window(db, symbol, now.date(), window_end.date())
    # A US stock whose earnings calendar cannot be read is not "no earnings" (``earnings_unknown``; the
    # owner's "event" accepts it). Other listings are not covered by the calendar (US only).
    earnings_unknown = prep.earnings_known is None and not underlying.is_index and underlying.exchange == "XNYS"
    macro = tuple(cal.macro_events_between(now.astimezone(cal.BERLIN).date(), window_end.date())) \
        if underlying.is_index else ()
    proposal = Proposal(
        signal=signal, product_type=product_type, units=sizing.units, ask=chosen.ask, notional=sizing.notional,
        worst_case_loss=sizing.worst_case_loss, spread_pct=chosen.spread_pct, liquid=underlying.liquid,
        quote_age_s=chosen.quote_age_s, exchange=underlying.exchange, days_to_expiry=chosen.days_to_expiry,
        earnings_in_window=bool(prep.earnings_known), earnings_unknown=earnings_unknown, event_flag=event_flag,
        macro_events=macro, is_index=underlying.is_index)
    prep.verdict = check_entry(state, proposal, now)
    if prep.verdict.paper_ok:
        prep.headlines = news.headlines(db, cli, underlying, now - timedelta(hours=24), symbol)
    return prep


def record_proposal(db: Session, prep: PreparedProposal) -> ProposalResult:
    """The write half of :func:`propose`: the ledger events and the position. Reads no market data."""
    base, now, selection = prep.base, prep.now, prep.selection
    if prep.refusal == "no_volatility":
        append_event(db, "guard_verdict", MODE, {**base, "ok": False, "reasons": ["no_volatility"]}, now=now)
        return ProposalResult(None, "no_volatility")
    append_event(db, "candidates", MODE, {
        **base, "searched": selection.searched, "sigma": str(prep.sigma),
        "candidates": [c.as_json() for c in selection.candidates], "dropped": selection.dropped[:50]}, now=now)
    if prep.refusal is not None:
        append_event(db, "guard_verdict", MODE, {**base, "ok": False, "reasons": [prep.refusal]}, now=now)
        return ProposalResult(None, prep.refusal, selection_size=selection.searched)
    chosen, sizing, verdict = selection.chosen, prep.sizing, prep.verdict
    assert chosen is not None and verdict is not None and prep.window_end is not None
    append_event(db, "guard_verdict", MODE, {
        **base, "isin": chosen.isin, "ok": verdict.ok, "paper_ok": verdict.paper_ok,
        "reasons": list(verdict.reasons), "units": sizing.units, "stop": str(sizing.stop_price)}, now=now)
    position = DeskPosition(
        user_id=prep.user_id, mode=MODE, cell=_cell(prep.signal, prep.product_type), signal=prep.signal,
        product_type=prep.product_type, isin=chosen.isin, issuer=(chosen.issuer or "")[:32] or None,
        underlying_isin=prep.underlying.isin, underlying_label=prep.underlying.label[:64],
        direction=prep.direction, liquid=prep.underlying.liquid, leverage_or_omega=chosen.leverage_or_omega,
        units=sizing.units, entry_price=chosen.ask, stop_price=sizing.stop_price,
        max_exit_at=prep.window_end.astimezone(UTC), spread_pct=chosen.spread_pct, status="proposed",
        would_take_live=False, prereg_hash=prereg.PREREG_HASH, product_name=chosen.name[:120] or None,
        wkn=chosen.wkn, barrier_or_strike=chosen.barrier_or_strike, expiry=chosen.expiry,
        created_at=now.astimezone(UTC))
    db.add(position)
    db.flush()
    refs = _ref_payload(prep.underlying_ref, prep.barrier_ref)
    if not verdict.paper_ok:
        position.status = "rejected"
        append_event(db, "proposal", MODE, {**_proposal_payload(position, chosen, sizing, verdict), "rejected": True,
                                            **refs}, position.id, now=now)
        return ProposalResult(position, ",".join(verdict.paper_reasons))
    nonce = secrets.token_urlsafe(8)
    proposed_at = prep.clock() if prep.clock else now  # the tap window starts when the proposal goes out
    earnings = "unknown" if prep.earnings_known is None else bool(prep.earnings_known)
    append_event(db, "proposal", MODE, {
        **_proposal_payload(position, chosen, sizing, verdict), **refs,
        "nonce": nonce, "proposed_at": proposed_at.isoformat(),
        "news_seen": [h["title"] for h in prep.headlines[:5]], "earnings_in_window": earnings}, position.id, now=now)
    return ProposalResult(position, None, nonce, selection.searched, [h["title"] for h in prep.headlines[:3]])


def _ref_payload(underlying_ref: Decimal | None, barrier_ref: Decimal | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if underlying_ref is not None:
        out["underlying_ref"] = str(underlying_ref)
    if barrier_ref is not None:
        out["barrier_ref"] = str(barrier_ref)
    return out


def _proposal_payload(position: DeskPosition, c: Candidate, sizing, verdict: Verdict) -> dict[str, Any]:
    return {
        "isin": c.isin, "wkn": c.wkn, "name": c.name, "issuer": c.issuer, "product_type": c.product_type,
        "barrier_or_strike": None if c.barrier_or_strike is None else str(c.barrier_or_strike),
        "barrier_distance": None if c.barrier_distance is None else str(c.barrier_distance),
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
         underlying: Underlying | None = None, clock: Callable[[], datetime] | None = None) -> TapResult:
    """A tap on "Paper: take": single-use nonce, 180 s window, fill at a fresh quote.

    The position row is locked for the whole tap: two taps at once (Telegram and the page, or a
    re-delivered callback) run one after the other, and the second sees the first's tap as used.
    *clock* (live taps: the wall clock) is read again once the lock is held: a tap that waited for the
    monitor's tick is judged (tap window, quote age, fill time) when it runs, not when it arrived."""
    desk_write_lock(db)  # before the row lock: one lock order on every desk write path
    now = clock() if clock else now
    position = db.get(DeskPosition, position_id, with_for_update=True, populate_existing=True)
    proposal = _latest_proposal(db, position_id) if position else None
    if position is None or proposal is None or proposal.get("nonce") != nonce:
        append_event(db, "tap", MODE, {"result": "unknown", "position_id": position_id}, now=now)
        return TapResult("unknown", message="Unknown proposal.")
    # "No fresh quote" does not use the nonce up: the owner may tap the same button again.
    if any((ev := json.loads(e.payload)).get("nonce") == nonce and ev.get("result") != "no_quote"
           for e in events_for(db, position_id, "tap")):
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
        if sizing.units < 1:
            position.status = "rejected"
            append_event(db, "tap", MODE, {"result": "refused", "nonce": nonce, "reason": "too_small",
                                           "old_ask": str(old), "new_ask": str(quote.ask)}, position_id, now=now)
            return TapResult("refused", position, message="Not filled: at the new price not even one unit fits the limits.")
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
#: window, between the proposal and the tap, or the position holds no unit. Price and size were checked at the proposal (a
#: re-ask re-sizes), so quote noise inside the 180 s window does not refuse a fill.
FILL_BLOCKING = frozenset({"halted", "after_cutoff", "outside_window", "underlying_closed", "too_small"})
#: The position's latest exit is already past: it would close at the next tick for ``horizon``.
EXIT_PASSED = "exit_time_passed"
#: A knock-out whose underlying price could not be read: its barrier could not be watched.
NO_REFERENCE = "no_reference_price"


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
    max_exit = _aware(position.max_exit_at)
    if max_exit is not None and max_exit <= now:
        refused += (EXIT_PASSED,)
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
        position.max_exit_at = cal.max_exit_at(now).astimezone(UTC)
    proposal = _latest_proposal(db, position.id) or {}
    if underlying is not None and cli is not None:
        position.underlying_entry = underlying_price(db, cli, underlying, now=now)
    if position.underlying_entry is None and proposal.get("underlying_ref"):
        # The read at the proposal, seconds to 3 minutes ago, rather than no move for the placebo.
        position.underlying_entry = Decimal(proposal["underlying_ref"])
    barrier_ref = proposal.get("barrier_ref")  # anchored at the proposal
    append_event(db, "fill", MODE, {
        "price": str(quote.ask), "bid": str(quote.bid), "units": units, "fee": str(LIMITS.fee_per_order),
        "would_take_live": verdict.ok, "live_reasons": list(verdict.reasons),
        "underlying_entry": None if position.underlying_entry is None else str(position.underlying_entry),
        "barrier_ref": barrier_ref}, position.id, now=now)
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


def claim_open(db: Session, position: DeskPosition) -> bool:
    """Take the position out of ``open`` with a conditional UPDATE: of two closes at once (the monitor's
    stop and the owner's Close), the second waits for the first's row lock and then changes no row.
    ``False``: someone else closed it; the in-memory object is refreshed."""
    desk_write_lock(db)  # before the row lock: one lock order on every desk write path
    changed = db.query(DeskPosition).filter(DeskPosition.id == position.id, DeskPosition.status == "open") \
        .update({DeskPosition.status: "closed"}, synchronize_session=False)
    if changed != 1:
        db.refresh(position)
        return False
    return True


def close_position(db: Session, position: DeskPosition, bid: Decimal, reason: str, now: datetime,
                   underlying_exit: Decimal | None = None) -> bool:
    """Close at *bid*. ``False`` when the position was no longer open (closed concurrently)."""
    if not claim_open(db, position):
        return False
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
    return True


def _past_window(db: Session, p: DeskPosition, now: datetime) -> bool:
    proposal = _latest_proposal(db, p.id)
    return proposal is None or now - datetime.fromisoformat(proposal["proposed_at"]) > timedelta(
        seconds=LIMITS.tap_window_s)


def expire_proposals(db: Session, now: datetime) -> int:
    """Expire proposals past the tap window. The desk's write lock is taken only when one is due. A
    proposal a tap is filling right now (its row locked by ``take``) is skipped this tick, and the status
    changes only where it is still ``proposed``: a tap that committed ``open`` (or a re-ask with a new
    window) is never overwritten with ``expired``."""
    due = [p.id for p in db.query(DeskPosition).filter(DeskPosition.status == "proposed",
                                                         DeskPosition.mode == MODE).all() if _past_window(db, p, now)]
    if not due:
        return 0
    count = 0
    desk_write_lock(db)  # before the row locks: one lock order on every desk write path
    rows = db.query(DeskPosition).filter(DeskPosition.id.in_(due), DeskPosition.status == "proposed") \
        .with_for_update(skip_locked=True).populate_existing().all()
    for p in rows:
        if not _past_window(db, p, now):  # re-asked while we looked
            continue
        changed = db.query(DeskPosition).filter(DeskPosition.id == p.id, DeskPosition.status == "proposed") \
            .update({DeskPosition.status: "expired"}, synchronize_session=False)
        if changed != 1:
            db.refresh(p)
            continue
        p.status = "expired"
        append_event(db, "expired", MODE, {"reason": "no_tap"}, p.id, now=now)
        count += 1
    return count


def manage_open_positions(db: Session, cli: ScalableCli, now: datetime, underlyings: dict[str, Underlying],
                          errors: list[str] | None = None) -> list[tuple[DeskPosition, str]]:
    """Quote every open position once and apply ``check_exit``. Returns the positions closed this tick.

    An outdated wrapper propagates: no position is judged on reads that were never allowed. Any other
    failed quote counts as no quote, and its code goes to *errors* (shown as the desk's last error).

    Every read first, then the closes: a close takes the desk's write lock until the tick commits, and
    the owner's taps and closes must not wait behind the other positions' quotes."""
    closed: list[tuple[DeskPosition, str]] = []
    exits: list[tuple[DeskPosition, Decimal, str, Decimal | None]] = []
    halted = is_halted(db)
    rows = db.query(DeskPosition).filter(DeskPosition.status == "open", DeskPosition.mode == MODE,
                                         DeskPosition.product_type != "cfd").all()  # CFDs: cfd.manage
    for p in rows:
        quote = None
        try:
            quote = market.get_quote(cli, p.isin)
        except market.WrapperOutdated:
            raise
        except ScalableError as exc:
            quote = None
            if errors is not None:
                errors.append(_error_code(exc))
        fresh = quote is not None and not quote.outdated and _quote_age(now, quote.ts_utc) <= LIMITS.max_quote_age_s
        bid = quote.bid if fresh and quote else None
        if bid is not None:
            p.last_bid, p.last_quote_at = bid, now.astimezone(UTC)
        u = underlying_of(p, underlyings.get(p.underlying_isin))
        if not bid:  # no quote, or a zero bid: a knock-out past its barrier is worth nothing
            level = _knocked_out_at(db, cli, p, u, now)
            if level is not None:
                exits.append((p, Decimal(0), "knock_out", level))
                continue
        reason = check_exit(p, bid, now, halted=halted)
        if reason is None:
            continue
        exit_bid = bid if bid is not None else Decimal(p.last_bid or 0)
        if exit_bid <= 0:
            continue
        exits.append((p, exit_bid, reason, underlying_exit_level(db, cli, u, now)))
    for p, price, reason, level in exits:
        if close_position(db, p, price, reason, now, level):
            closed.append((p, reason))
    return closed


#: How old the reference's last chart point may be when no fresh price can be read at an exit.
EXIT_LEVEL_MAX_AGE = timedelta(minutes=30)


def underlying_exit_level(db: Session, cli: ScalableCli, u: Underlying, now: datetime) -> Decimal | None:
    """The underlying's level at an exit, for the placebo's move: a fresh price, else the reference's
    last chart point (Alpaca 1-minute close, sc chart mid) of the past 30 minutes, else ``None``."""
    price = underlying_price(db, cli, u, now=now)
    if price is not None:
        return price
    path = reference_path(db, cli, u, now - EXIT_LEVEL_MAX_AGE, now, closes=True)
    return path[-1] if path else None


def barrier_in_reference(direction: str, reference: Decimal | None, distance: Any) -> Decimal | None:
    """The knock-out barrier in the reference price's own units, or ``None``.

    The issuer's barrier is in the underlying's points and currency (DAX 19,000 points, a US stock in
    USD); the reference the desk can read is a proxy (the DAX ETF at 200 EUR, SPY, a US stock's EUR line).
    The listing's distance to the barrier is a fraction of the underlying's level, so the same fraction
    of the proxy's price *read in the same snapshot* is the barrier in the proxy's units; that level is
    then fixed, like the barrier itself. What it leaves out: the proxy's tracking drift and, for a US
    stock's EUR line, the EUR/USD move since the proposal (a two-day hold: tenths of a percent against a
    barrier at least 3 daily sigmas away)."""
    if distance in (None, "") or not reference:
        return None
    try:
        d = abs(Decimal(str(distance)))
    except ArithmeticError:
        return None
    if not (0 < d < 1):
        return None
    ref = Decimal(reference)
    return (ref * (1 - d) if direction == "long" else ref * (1 + d)).quantize(Decimal("0.000001"))


def _barrier_ref_of(db: Session, p: DeskPosition) -> Decimal | None:
    fills = events_for(db, p.id, "fill")
    raw = json.loads(fills[-1].payload).get("barrier_ref") if fills else None
    return Decimal(raw) if raw else None


def reference_path(db: Session, cli: ScalableCli, u: Underlying, since: datetime, now: datetime, *,
                   closes: bool = False) -> list[Decimal]:
    """The reference's lows and highs since *since*: Alpaca 1-minute bars for the US indices (low and
    high of each bar; with *closes*, each bar's close), else sc's 7-day chart of the proxy (one mid per
    point). Oldest first; empty when unreadable."""
    try:
        if u.alpaca_symbol:
            provider = news.alpaca_for(db)
            if provider is None:
                return []
            out: list[Decimal] = []
            for bar in provider.get_intraday_bars(u.alpaca_symbol, "1Min", since, now):
                if not _regular_us_session(bar["t"]):
                    continue  # SPY's pre- and after-market prints: the index (the barrier's level) is not set then
                fields = (bar.get("c"),) if closes else (bar.get("l"), bar.get("h"))
                out += [Decimal(v) for v in fields if v is not None]
            return out
        return [mid for ts, mid in market.get_chart(cli, u.proxy_isin or u.isin, "7d") if since <= ts <= now]
    except market.WrapperOutdated:
        raise
    except (ScalableError, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk underlying path read failed: %s", type(exc).__name__)
        return []


def _regular_us_session(ts: datetime) -> bool:
    """A bar starting inside NYSE's regular hours (09:30-16:00 New York), as ``compute_r1_us`` uses."""
    local = ts.astimezone(cal.NEW_YORK)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 0)


def _knocked_out_at(db: Session, cli: ScalableCli, p: DeskPosition, u: Underlying, now: datetime) -> Decimal | None:
    """The reference level that crossed the knock-out barrier since the fill, else ``None``.

    A knocked-out product has no bid; closing it at its last good bid would score a -100 % trade as a
    small loss. Knock-out depends on the path: a touch counts even when the level has recovered, so the
    reference's lows (long) or highs (short) since the fill are read, plus the current price. Both sides
    are in the reference's units (:func:`barrier_in_reference`), never index points against an ETF
    price. Unknown barrier or no readable price: ``None`` (the quote-lost rule still applies)."""
    if p.product_type != "knockout":
        return None
    barrier = _barrier_ref_of(db, p)
    entry_at = _aware(p.entry_at)
    if barrier is None or entry_at is None:
        return None
    levels = reference_path(db, cli, u, entry_at, now)
    current = underlying_price(db, cli, u, now=now)
    if current is not None:
        levels.append(current)
    levels = [v for v in levels if v > 0]
    if not levels:
        return None
    worst = min(levels) if p.direction == "long" else max(levels)
    crossed = worst <= barrier if p.direction == "long" else worst >= barrier
    return worst if crossed else None


def owner_close(db: Session, position: DeskPosition, cli: ScalableCli, now: datetime,
                underlying: Underlying | None = None) -> bool:
    if position.status != "open":
        return False
    if position.product_type == "cfd":
        return False  # a CFD closes through cfd.owner_close (no import back from here)
    quote = market.get_quote(cli, position.isin)
    u = underlying_of(position, underlying)
    fresh = quote is not None and not quote.outdated and _quote_age(now, quote.ts_utc) <= LIMITS.max_quote_age_s
    if not (fresh and quote and quote.bid > 0):
        level = _knocked_out_at(db, cli, position, u, now)
        if level is not None:
            return close_position(db, position, Decimal(0), "knock_out", now, level)
        return False  # no fresh quote: the paper close would be at a stale price
    return close_position(db, position, quote.bid, "owner_close", now, underlying_exit_level(db, cli, u, now))
