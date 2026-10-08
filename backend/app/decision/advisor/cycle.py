"""Autonomous daily paper-trade cycle (advisor loop D0/D1).

One honest cycle: discover candidates → deterministic quant proposal (C1) →
hard risk gate (C2) → LLM decision within the envelope (C3) → paper trades →
prediction ledger rows → NAV snapshot → audit-trail decision record.

Paper trading is fully autonomous; nothing here touches real money (DKB stays
read-only). Idempotent per day — safe to re-run.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    LlmPortfolioDecision,
    PaperPortfolio,
    PaperTrade,
)
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.costs import affordable_notional, resolve_fee_schedule, trade_fee
from app.decision.advisor.decision import build_trade_proposal
from app.decision.advisor.llm_decision import (
    _SYSTEM_PROMPT,
    TradeDecision,
    build_decision_schema,
    decide_trades,
)
from app.foundation import provenance
from app.foundation.llm.sampling import QWEN_SAMPLING
from app.foundation.paper_cash import MAX_QUOTE_AGE_TRADING_DAYS
from app.decision.advisor.risk_gate import check_risk_gate
from app.decision.advisor.rl_signal import latest_rl_portfolio_signal
from app.decision.discover.calibrator import CALIBRATOR_VERSION
from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS
from app.decision.discover.predictor import store_prediction
from app.foundation.quant_proposal import TradeProposal
from app.decision.paper_portfolio import (
    execute_trade,
    get_summary,
    paper_quote_eur,
    resolve_instrument_name,
    seed_paper_portfolio_from_real,
    snapshot_paper_portfolio,
)

logger = logging.getLogger(__name__)

ADVISOR_MANDATE = "advisor"
# Fields of llama-server's identity that change what the model writes; the
# advisor's cohort includes them (ADR 0018 §10).
_LLM_COHORT_KEYS = ("model_path", "build_info", "chat_template_sha256", "server_sampling")
# Reuse a completed discover run up to this many days old before flagging it stale.
_DISCOVER_STALE_DAYS = 7

# Trade-flow policy. Defaults live here; each is overridable per strategy via
# ``AdvisorStrategy.config_json`` so champion and challenger can run different
# turnover regimes and the competition can test whether trading more helps.
#
# The sleeve is seeded as a mirror of a ~fully-invested real book, so it starts
# with almost no cash. Buys are therefore financed by sells — see
# ``_ordered_decisions``, which settles every sell before any buy so proceeds
# are available within the same cycle.
DEFAULT_MAX_TURNOVER_PCT = 0.15  # gross notional traded per cycle, as a share of NAV
DEFAULT_MIN_TICKET_PCT = 0.01  # skip orders smaller than this share of NAV
# Per-position sell cap (2026-09-01 underperformance fix): the overall turnover
# budget above bounds gross notional across the whole cycle, but says nothing
# about how much of any *one* position can be liquidated in a single shot. The
# champion sleeve's first rebalance after a JSON-parsing outage resumed spent
# ~100% of that cycle's turnover budget draining its single largest holding
# (a diversified ETF) into a set of concentrated single-country bets that
# missed the rally the ETF then captured — one rebalance fully exiting an
# overweight in one cycle, on the strength of one rosy forecast for the
# replacement name. Capping any single position's sell to roughly a third of
# its current value per cycle forces multi-cycle conviction to fully exit a
# position, so a transient forecast can no longer liquidate a core holding in
# one shot. Applies to both model-directed sells (``_plan_decision``) and
# optimiser-driven funding trims (``_raise_cash``) — a funding sell is still a
# sell of the same position and must respect the same per-cycle ceiling.
DEFAULT_MAX_SELL_PCT_OF_POSITION = 1.0 / 3.0
# Cumulative sell-rate cap (2026-09 follow-up to the per-cycle cap above, ADR
# 0013): historical replay of the champion sleeve's actual Jul-Aug drift found
# the per-cycle cap alone insufficient — the aggregate turnover budget was
# already keeping each cycle's sell small, so ~15 individually-compliant sells
# (2.7%-8.8% of the position each) compounded across three weeks into a ~57%
# reduction of a single core holding, none of which ever tripped the per-cycle
# 1/3 cap. This bounds cumulative erosion over a trailing window instead of
# just the current cycle: a position may not lose more than
# DEFAULT_MAX_CUMULATIVE_SELL_PCT of its window-start value across
# DEFAULT_CUMULATIVE_SELL_WINDOW_DAYS, in addition to (never instead of) the
# per-cycle cap above. "Window-start value" is reconstructed from the trade
# log (current value + sells - buys within the window), not from a daily
# per-holding price snapshot, since none is stored — an approximation that
# ignores price drift within the window, in keeping with the per-cycle cap's
# own use of "current value" as its baseline.
DEFAULT_MAX_CUMULATIVE_SELL_PCT = 1.0 / 3.0
# Calendar days, not trading days, since no trading-calendar dependency
# exists elsewhere in this codebase; ~28 calendar days approximates 20
# trading days (the window locked in during the fix's design grilling).
DEFAULT_CUMULATIVE_SELL_WINDOW_DAYS = 28
# Core-holding floor (ADR 0013, shipped alongside the cumulative cap as
# defense-in-depth): a diversified/index holding (asset_type == "etf") may
# never be sold below this share of NAV, a hard block with no override path —
# unlike the two caps above, this is a floor on the position's own weight,
# not a rate limit on how fast it can shrink. Single-stock/satellite
# positions are deliberately excluded: a satellite pick that has lost
# conviction is meant to be fully exitable.
DEFAULT_CORE_HOLDING_FLOOR_PCT = 0.20


def _latest_candidates(db: Session, user_id: str) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (shortlisted candidate dicts, run_id, stale) from the newest
    completed discover run."""
    run = (
        db.query(DiscoverRun)
        .filter(DiscoverRun.user_id == user_id, DiscoverRun.status == "completed")
        .order_by(DiscoverRun.created_at.desc())
        .first()
    )
    if run is None:
        return [], None, True
    created_at = run.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    stale = created_at is None or created_at < datetime.now(UTC) - timedelta(days=_DISCOVER_STALE_DAYS)
    rows = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.run_id == run.id, DiscoverCandidate.status == "shortlisted")
        .all()
    )
    candidates = [
        {"symbol": c.symbol, "isin": c.isin, "name": c.name, "source": c.source}
        for c in rows
    ]
    return candidates, run.id, stale


# Terminal cycle statuses. An idle day (no candidates, or no trade deltas) is
# still a day the cycle ran — re-running it in the same day would only repeat
# the same LLM call for the same answer. Only "failed" is worth retrying.
_RAN_TODAY_STATUSES = ("completed", "idle_no_candidates", "idle_no_trades")

# A failed cycle is worth retrying, but not without bound. Two schedulers drive
# the champion sleeve every weekday — the 10:00 advisor job and the 10:30
# evolution round — and a manual ``/api/advisor/cycle/run`` adds more on top.
# Because "failed" is (deliberately) not a terminal status, every one of those
# triggers re-ran the full cycle, burning an LLM call and stacking another
# failed row on the same day. Cap the attempts so a bad day costs a bounded
# number of calls instead of one per trigger.
_MAX_FAILED_ATTEMPTS_PER_DAY = 3


def _already_ran_today(db: Session, portfolio_id: str, mandate: str = ADVISOR_MANDATE) -> bool:
    today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    statuses = [
        row[0]
        for row in db.query(LlmPortfolioDecision.status)
        .filter(
            LlmPortfolioDecision.portfolio_id == portfolio_id,
            LlmPortfolioDecision.mandate == mandate,
            LlmPortfolioDecision.review_date >= today_start,
        )
        .all()
    ]
    if any(status in _RAN_TODAY_STATUSES for status in statuses):
        return True
    return sum(1 for status in statuses if status == "failed") >= _MAX_FAILED_ATTEMPTS_PER_DAY


class PlannedTrade:
    """A sized, priced order that has not been sent to the book yet.

    Planning is separated from execution so the turnover budget and the
    minimum-ticket floor can be applied across the whole decision set before
    anything is committed.
    """

    __slots__ = ("decision", "side", "price", "notional")

    def __init__(self, decision: TradeDecision, side: str, price: float, notional: float) -> None:
        self.decision = decision
        self.side = side
        self.price = price
        self.notional = notional

    @property
    def quantity(self) -> float:
        return self.notional / self.price if self.price > 0 else 0.0


def _cumulative_sell_totals(
    db: Session, portfolio_id: str, window_days: int
) -> dict[str, tuple[float, float]]:
    """Per-ticker ``(sell_value_eur, buy_value_eur)`` from trades in the
    trailing *window_days*, used to reconstruct each position's window-start
    value without a daily per-holding price snapshot (none is stored)."""
    cutoff = now_utc() - timedelta(days=window_days)
    rows = (
        db.query(PaperTrade.ticker, PaperTrade.side, PaperTrade.value)
        .filter(PaperTrade.portfolio_id == portfolio_id, PaperTrade.date >= cutoff)
        .all()
    )
    totals: dict[str, list[float]] = {}
    for ticker, side, value in rows:
        if not ticker:
            continue
        bucket = totals.setdefault(ticker.upper(), [0.0, 0.0])
        v = float(value or 0.0)
        if side == "sell":
            bucket[0] += v
        elif side == "buy":
            bucket[1] += v
    return {ticker: (sell_v, buy_v) for ticker, (sell_v, buy_v) in totals.items()}


def _effective_sell_cap(
    ticker: str,
    current_value: float,
    total_value: float,
    *,
    max_sell_pct_of_position: float,
    cumulative_totals: dict[str, tuple[float, float]],
    max_cumulative_sell_pct: float,
    instrument_types: dict[str, str],
    core_holding_floor_pct: float,
) -> float:
    """The most a position may be sold this cycle, folding in all three
    guardrails: the per-cycle cap, the trailing-window cumulative cap, and
    (for a diversified/index holding) the core-holding floor. Returns the
    tightest of whichever apply."""
    cap = current_value * max_sell_pct_of_position

    sell_v, buy_v = cumulative_totals.get(ticker.upper(), (0.0, 0.0))
    window_start_value = max(0.0, current_value + sell_v - buy_v)
    cumulative_cap = window_start_value * max_cumulative_sell_pct
    cumulative_remaining = max(0.0, cumulative_cap - sell_v)
    cap = min(cap, cumulative_remaining)

    if instrument_types.get(ticker.upper()) == "etf":
        floor_value = total_value * core_holding_floor_pct
        cap = min(cap, max(0.0, current_value - floor_value))

    return cap


# A cached bar older than this many trading days is no basis for a fill.
MAX_FALLBACK_BAR_AGE_TRADING_DAYS = MAX_QUOTE_AGE_TRADING_DAYS


def _bar_age_trading_days(proposal: TradeProposal, ticker: str) -> int | None:
    """Trading days between the last bar behind ``mc.spot`` and today, or ``None`` if unknown."""
    import numpy as np

    series = proposal.price_series.get(ticker)
    if series is None or len(series) == 0:
        return None
    try:
        last = series.index[-1].date()
    except AttributeError:
        return None
    today = now_utc().date()
    if last >= today:
        return 0
    return int(np.busday_count(last, today))


def _resolve_execution_prices(
    db: Session, proposal: TradeProposal, tickers: set[str]
) -> dict[str, float | None]:
    """EUR fill price per ticker: the quote holdings are valued at.

    Fills used to take ``mc.spot`` (the last cached history close, possibly
    days old) while the book is valued at :func:`paper_quote_eur`, so every
    trade booked an instant gain or loss against its own valuation. Now the
    fill uses the valuation quote; only when there is none does it fall back
    to ``mc.spot``, and then only if that bar is at most
    :data:`MAX_FALLBACK_BAR_AGE_TRADING_DAYS` trading days old. ``None`` means
    no current price (the order is skipped).
    """
    prices: dict[str, float | None] = {}
    rate_cache: dict[str, dict[str, float] | None] = {}
    for ticker in sorted(tickers):
        q = paper_quote_eur(db, ticker, rate_cache=rate_cache)
        quote = None if q.get("stale") else q.get("price")
        if quote and quote > 0:
            prices[ticker] = float(quote)
            continue
        mc = proposal.mc_summaries.get(ticker)
        age = _bar_age_trading_days(proposal, ticker)
        if mc is not None and mc.spot and mc.spot > 0 and age is not None and (
            age <= MAX_FALLBACK_BAR_AGE_TRADING_DAYS
        ):
            prices[ticker] = float(mc.spot)
        else:
            prices[ticker] = None
    return prices


def _record_sale(
    proposal: TradeProposal, sell_caps: dict[str, float] | None, ticker: str, value: float
) -> None:
    """Take an executed sell off the book and off the ticker's remaining sell cap.

    ``sell_caps`` and ``proposal.current_book`` are computed once per cycle;
    without this a name the model already sold could be sold again as a
    funding sell, and each funding raise could trim the same name again, so a
    cycle's total sells of one position could exceed the per-cycle,
    cumulative-window and ETF-floor caps. Both guardrails measure what is
    still sellable, so the same decrement serves all three.
    """
    if sell_caps is not None and ticker in sell_caps:
        sell_caps[ticker] = max(0.0, sell_caps[ticker] - value)
    if ticker in proposal.current_book:
        proposal.current_book[ticker] = max(0.0, proposal.current_book[ticker] - value)


def _plan_decision(
    decision: TradeDecision,
    proposal: TradeProposal,
    total_value: float,
    min_ticket: float,
    max_sell_pct_of_position: float = DEFAULT_MAX_SELL_PCT_OF_POSITION,
    sell_caps: dict[str, float] | None = None,
    prices: dict[str, float | None] | None = None,
) -> tuple[PlannedTrade | None, str | None]:
    """Size one target-weight decision into an order, or explain the skip.

    Returns ``(planned, skip_reason)`` — exactly one is non-None.
    """
    if prices is not None:
        # Execution prices resolved by the cycle (valuation quote, see
        # _resolve_execution_prices).
        price = prices.get(decision.ticker)
        if not price or price <= 0:
            logger.warning("advisor cycle: no current price for %s — trade skipped", decision.ticker)
            return None, "no current price"
    else:
        mc = proposal.mc_summaries.get(decision.ticker)
        price = mc.spot if mc is not None else None
        if not price or price <= 0:
            logger.warning("advisor cycle: no price for %s — trade skipped", decision.ticker)
            return None, "no market price available"

    current_value = proposal.current_book.get(decision.ticker, 0.0)
    target_value = decision.target_weight * total_value
    delta_value = target_value - current_value

    if decision.action == "buy" and delta_value <= 0:
        logger.info("advisor cycle: buy %s target already met — skipped", decision.ticker)
        return None, (
            f"target weight already met (holding €{current_value:,.0f} ≥ "
            f"target €{target_value:,.0f})"
        )
    if decision.action == "sell":
        if current_value <= 0:
            return None, "nothing held to sell"
        # Trim to the target, never liquidate on a target the position is
        # already at or below — a sell at an unchanged/higher target is a hold.
        if current_value <= target_value:
            return None, (
                f"sell target €{target_value:,.0f} is at or above the current "
                f"€{current_value:,.0f} position — treated as hold"
            )
        delta_value = -(current_value - target_value)
        sell_cap = (
            sell_caps.get(decision.ticker, current_value * max_sell_pct_of_position)
            if sell_caps is not None
            else current_value * max_sell_pct_of_position
        )
        if abs(delta_value) > sell_cap:
            # Multi-cycle conviction required to fully exit a position — see
            # DEFAULT_MAX_SELL_PCT_OF_POSITION / DEFAULT_MAX_CUMULATIVE_SELL_PCT
            # / DEFAULT_CORE_HOLDING_FLOOR_PCT, whichever binds tightest.
            delta_value = -sell_cap

    notional = abs(delta_value)
    if notional <= 0:
        return None, "computed quantity rounded to zero"
    if notional < min_ticket:
        # A €20 order against a €40k book is noise, and once commission is
        # charged it is almost entirely fee.
        return None, (
            f"below minimum ticket (€{notional:,.0f} < €{min_ticket:,.0f})"
        )

    side = "buy" if delta_value > 0 else "sell"
    return PlannedTrade(decision, side, float(price), notional), None


def _ordered_decisions(decisions: list[TradeDecision]) -> list[TradeDecision]:
    """Sells before buys, each ranked by descending confidence.

    Two things depend on this order. Sells settle first so their proceeds fund
    the buys in the same cycle — without it a buy hits the cash gate and is
    rejected before any sell has raised the money, which is what kept a sleeve
    mirroring a fully-invested book from ever rotating. Within each side the
    highest-conviction orders go first, so when the turnover cap binds it is
    the least-convinced trades that get dropped.
    """
    tradeable = [d for d in decisions if d.action in ("buy", "sell")]
    return sorted(
        tradeable,
        key=lambda d: (0 if d.action == "sell" else 1, -d.confidence),
    )


def _raise_cash(
    db: Session,
    portfolio: PaperPortfolio,
    proposal: TradeProposal,
    total_value: float,
    shortfall: float,
    *,
    exclude: set[str],
    min_ticket: float,
    budget: float,
    fee_schedule: dict[str, Any],
    max_sell_pct_of_position: float = DEFAULT_MAX_SELL_PCT_OF_POSITION,
    sell_caps: dict[str, float] | None = None,
    prices: dict[str, float | None] | None = None,
    names: dict[str, str] | None = None,
) -> tuple[float, float, list[dict[str, Any]]]:
    """Trim overweight positions to fund a buy the sleeve cannot afford.

    Pre-cutover Qwen3-8B did not propose sells even when the prompt states that buys must
    be financed by them: given one large position, almost no cash and an explicit
    funding rule, it returned three buys, no sells, and no decision at all for
    its own largest holding (observed 2026-08 pre-cutover; mitigation kept as a model-agnostic defence after the Qwen3.5-9B upgrade). Waiting for the model to comply means the sleeve
    never trades, so funding is made deterministic instead.

    This invents no view. ``suggested_weights`` is the optimiser's target book;
    anything held above its target is overweight by the optimiser's own
    reckoning, and trimming it toward that target is the trade the optimiser is
    already asking for. The risk gate has vetted those weights, and the
    resulting sells are attributed to the optimiser rather than the model —
    they are deliberately kept out of the intent ledger.

    Positions are trimmed most-overweight-first, never below target, never
    below the minimum ticket, and never past *budget*.

    Returns:
        ``(net_proceeds, notional_used, records)``.
    """
    remaining = shortfall
    proceeds = 0.0
    notional_used = 0.0
    records: list[dict[str, Any]] = []
    if remaining <= 0 or budget < min_ticket:
        return 0.0, 0.0, records

    overweights: list[tuple[float, str]] = []
    for sym, current_value in proposal.current_book.items():
        if sym in exclude or current_value <= 0:
            continue
        target_value = proposal.suggested_weights.get(sym, 0.0) * total_value
        excess = current_value - target_value
        if excess >= min_ticket:
            overweights.append((excess, sym))
    overweights.sort(reverse=True)

    for excess, sym in overweights:
        if remaining <= 0 or budget < min_ticket:
            break
        if prices is not None:
            price = prices.get(sym)
        else:
            mc = proposal.mc_summaries.get(sym)
            price = mc.spot if mc is not None else None
        if not price or price <= 0:
            continue
        name = (names or {}).get(sym)
        # Gross the ask up so the commission does not eat into the shortfall.
        wanted = remaining + trade_fee(remaining, fee_schedule, name, side="sell")
        held_value = proposal.current_book.get(sym, 0.0)
        sell_cap = (
            sell_caps.get(sym, held_value * max_sell_pct_of_position)
            if sell_caps is not None
            else held_value * max_sell_pct_of_position
        )
        notional = min(wanted, excess, budget, sell_cap)
        if notional < min_ticket:
            continue
        fee = trade_fee(notional, fee_schedule, name, side="sell")
        if fee >= notional:
            continue
        try:
            trade = execute_trade(
                db,
                portfolio.id,
                sym,
                "sell",
                notional / price,
                float(price),
                rationale=(
                    "Funding sell: trimmed toward the optimiser's target weight to "
                    "finance a buy the sleeve could not otherwise afford."
                ),
                fee=fee,
            )
        except ValueError as exc:
            logger.warning("advisor cycle: funding sell %s rejected: %s", sym, exc)
            continue
        net = notional - fee
        _record_sale(proposal, sell_caps, sym, float(trade.get("value") or notional))
        proceeds += net
        notional_used += notional
        remaining -= net
        budget -= notional
        # Marked so the UI and the audit trail can tell an optimiser-driven
        # trim apart from a sell the model actually chose.
        trade["funding_sell"] = True
        records.append(trade)

    return proceeds, notional_used, records


def _advisor_stamp(
    db: Session,
    *,
    run_id: str | None,
    portfolio_id: str,
    system_suffix: str | None,
    decisions: list[TradeDecision],
    attempts: list[dict[str, Any]],
    injected_llm: bool,
) -> dict[str, Any]:
    """Provenance for this cycle's ledger rows (ADR 0018 §10).

    The advisor's conviction is the LLM's own confidence, so — unlike
    Discover — the served model, its sampling and the prompt are part of the
    cohort. Never raises: a failure is a degradation flag on the stamp.
    """
    flags: list[str] = []
    try:
        llm = {"available": False, "error": "injected llm_call"} if injected_llm else provenance.llm_server_identity(db)
        if not llm.get("available"):
            flags.append("llm_identity_unavailable")
        last = attempts[-1] if attempts else {}
        parsed_as = last.get("parsed_as") if isinstance(last, dict) else None
        if parsed_as not in (None, "strict"):
            flags.append(f"llm_output_{parsed_as}")
        if len(attempts) > 1:
            flags.append("llm_retried")
        if not decisions:
            flags.append("llm_no_decisions")
        prompt = _SYSTEM_PROMPT if not system_suffix else f"{_SYSTEM_PROMPT}\n{system_suffix}"
        return provenance.stamp(
            source="advisor",
            cohort_spec={
                "prompt_template_hash": provenance.canonical_hash(prompt),
                "decision_schema_hash": provenance.canonical_hash(build_decision_schema(None)),
                "client_sampling": dict(QWEN_SAMPLING),
                "llm": {k: llm.get(k) for k in _LLM_COHORT_KEYS},
                "calibrator_version": CALIBRATOR_VERSION,
                "horizon_days": DEFAULT_HORIZON_DAYS,
            },
            details={
                "run_id": run_id,
                "portfolio_id": portfolio_id,
                "llm_server": llm,
                "llm_parse_status": parsed_as,
                "llm_attempts": len(attempts),
                "output_sha256": provenance.canonical_hash([asdict(d) for d in decisions]),
            },
            degradation_flags=flags,
        )
    except Exception as exc:  # noqa: BLE001 - provenance must never fail the cycle
        logger.exception("advisor provenance stamp failed")
        return {
            "provenance_schema_version": 0,
            "source": "advisor",
            "run_id": run_id,
            "degradation_flags": ["stamp_failed"],
            "error": str(exc)[:200],
        }


def _open_prediction_intents(db: Session, portfolio_id: str) -> set[tuple[str, str]]:
    """``(symbol, direction)`` pairs this sleeve already has unresolved.

    Used to dedupe the intent ledger: repeating an unchanged view daily inflates
    the resolved-decision count without adding independent evidence, because
    every repeat covers overlapping windows on the same name.
    """
    from app.foundation.models.entities import DiscoveryPrediction

    rows = (
        db.query(DiscoveryPrediction.symbol, DiscoveryPrediction.direction)
        .filter(
            DiscoveryPrediction.portfolio_id == portfolio_id,
            DiscoveryPrediction.outcome_status == "pending",
        )
        .all()
    )
    return {(str(symbol).upper(), str(direction)) for symbol, direction in rows}


def _execute_planned(
    db: Session,
    portfolio: PaperPortfolio,
    planned: PlannedTrade,
    fee: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Send one planned order to the book. Returns ``(trade, skip_reason)``."""
    try:
        return (
            execute_trade(
                db,
                portfolio.id,
                planned.decision.ticker,
                planned.side,
                planned.quantity,
                planned.price,
                confidence=planned.decision.confidence,
                rationale=planned.decision.thesis or None,
                fee=fee,
            ),
            None,
        )
    except ValueError as exc:
        # Insufficient cash/holdings — an honest skip, not a crash.
        logger.warning(
            "advisor cycle: trade %s %s rejected: %s", planned.side, planned.decision.ticker, exc
        )
        return None, f"{planned.side} rejected: {exc}"


def _record_cycle(
    db: Session,
    portfolio: PaperPortfolio,
    mandate: str,
    payload: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
) -> LlmPortfolioDecision:
    """Persist the audit row for one cycle.

    Every terminating path writes one of these, including the idle ones. An
    early return that wrote nothing left the UI showing the last successful
    cycle indefinitely, so a loop starved of candidates was indistinguishable
    from a loop that was working.
    """
    row = LlmPortfolioDecision(
        portfolio_id=portfolio.id,
        review_date=now_utc(),
        mandate=mandate,
        decision_json=json.dumps(payload, default=str),
        status=status,
        error=error,
    )
    db.add(row)
    db.commit()
    return row


def run_advisor_cycle(
    db: Session,
    user_id: str,
    *,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
    config: dict[str, Any] | None = None,
    strategy: Any | None = None,
) -> dict[str, Any]:
    """Run one autonomous advisor cycle for *user_id* (D1).

    Returns a CycleResult dict:
        ``{status, portfolio_id, trades, predictions, blocked, errors, snapshot}``

    Idempotent per day — a completed decision record for today short-circuits.

    When *strategy* (an ``AdvisorStrategy``) is given, the cycle drives that
    strategy's sleeve with its config (prompt framing, risk ceilings, breadth)
    and injects its active reflection lessons (PR2 A3/C3). Without one, the
    user's champion is used when it exists, else the plain PR1 behaviour.
    """
    result: dict[str, Any] = {
        "status": "skipped",
        "portfolio_id": None,
        "trades": [],
        "skipped": [],
        "predictions": [],
        "blocked": [],
        "errors": [],
        "snapshot": None,
    }

    if strategy is None:
        from app.decision.advisor.strategy import get_champion

        strategy = get_champion(db, user_id)

    portfolio: PaperPortfolio | None = None
    if strategy is not None and strategy.portfolio_id:
        portfolio = db.get(PaperPortfolio, strategy.portfolio_id)
    if portfolio is None:
        portfolio = seed_paper_portfolio_from_real(db, user_id)
        if strategy is not None:
            strategy.portfolio_id = portfolio.id
            db.flush()
    result["portfolio_id"] = portfolio.id
    mandate = portfolio.mandate or ADVISOR_MANDATE

    # Strategy config is the base; explicit call config overrides it.
    merged_config: dict[str, Any] = {
        **((strategy.config_json or {}) if strategy is not None else {}),
        **(config or {}),
    }
    config = merged_config

    lessons: list[str] = []
    if strategy is not None:
        from app.decision.advisor.reflection import get_active_lessons

        lessons = get_active_lessons(db, strategy.id)

    if _already_ran_today(db, portfolio.id, mandate):
        result["status"] = "already_ran_today"
        return result

    candidates, run_id, stale = _latest_candidates(db, user_id)
    if stale and run_id is not None:
        logger.warning("advisor cycle: newest discover run %s is stale (>%dd)", run_id, _DISCOVER_STALE_DAYS)
    if stale:
        result["errors"].append(
            f"discover run {run_id or '(none)'} is stale (>{_DISCOVER_STALE_DAYS}d) — "
            "candidates are frozen, so target weights are already met"
        )
    if not candidates:
        reason = "no completed discover run with shortlisted candidates"
        result["status"] = "no_candidates"
        result["errors"].append(reason)
        # NAV must still advance and the idle day must still be visible.
        result["snapshot"] = snapshot_paper_portfolio(db, portfolio.id)
        _record_cycle(
            db,
            portfolio,
            mandate,
            {
                "decisions": [],
                "strategy_id": strategy.id if strategy is not None else None,
                "lessons_injected": lessons,
                "discover_run_id": run_id,
                "discover_run_stale": stale,
                "notes": [reason],
                "errors": result["errors"],
            },
            status="idle_no_candidates",
            error=reason,
        )
        logger.warning("advisor cycle for user %s: no candidates — nothing to trade", user_id)
        return result

    # C1 → C2 → C3
    proposal = build_trade_proposal(db, portfolio.id, candidates, config=config)
    # Advisory-only (Track D2): a validated RL policy's rollout weights, read
    # if one exists. Never blocks or overrides the deterministic MC/skfolio
    # proposal above — decide_trades only ever surfaces it as one more input
    # in the LLM's prompt. Fails open (None) on any lookup error, same
    # discipline as every other gated signal in this codebase.
    try:
        rl_signal = latest_rl_portfolio_signal(db, user_id)
    except Exception:
        logger.error("advisor cycle: rl_signal lookup failed for %s", user_id, exc_info=True)
        rl_signal = None
    gate_result = check_risk_gate(
        proposal,
        ceilings=(config or {}).get("risk_ceilings"),
        series_by_symbol=proposal.price_series,
        instrument_types=proposal.instrument_types,
    )
    result["blocked"] = gate_result.blocked

    summary = get_summary(db, portfolio.id)
    total_value = float(summary.get("total_value") or 0.0)
    cash_balance = float(summary.get("cash_balance") or 0.0)

    fee_schedule = resolve_fee_schedule(config)
    # Instrument names drive the Prime-ETF fee exemption; fills use the same
    # EUR quote the book is valued at.
    names = {
        t: resolve_instrument_name(db, t)
        for t in set(proposal.mc_summaries) | set(proposal.current_book)
    }
    max_turnover_pct = float((config or {}).get("max_turnover_pct", DEFAULT_MAX_TURNOVER_PCT))
    min_ticket_pct = float((config or {}).get("min_ticket_pct", DEFAULT_MIN_TICKET_PCT))
    max_sell_pct_of_position = float(
        (config or {}).get("max_sell_pct_of_position", DEFAULT_MAX_SELL_PCT_OF_POSITION)
    )
    max_cumulative_sell_pct = float(
        (config or {}).get("max_cumulative_sell_pct", DEFAULT_MAX_CUMULATIVE_SELL_PCT)
    )
    cumulative_sell_window_days = int(
        (config or {}).get("cumulative_sell_window_days", DEFAULT_CUMULATIVE_SELL_WINDOW_DAYS)
    )
    core_holding_floor_pct = float(
        (config or {}).get("core_holding_floor_pct", DEFAULT_CORE_HOLDING_FLOOR_PCT)
    )
    turnover_budget = max(0.0, max_turnover_pct * total_value)
    min_ticket = max(0.0, min_ticket_pct * total_value)

    # Folds the per-cycle cap, the trailing-window cumulative cap, and the
    # core-holding floor into one per-ticker figure — computed once per cycle
    # (not per decision) so both the prompt shown to the model and the
    # executor's clamp always agree on the same number.
    cumulative_totals = _cumulative_sell_totals(db, portfolio.id, cumulative_sell_window_days)
    sell_caps = {
        ticker: _effective_sell_cap(
            ticker,
            current_value,
            total_value,
            max_sell_pct_of_position=max_sell_pct_of_position,
            cumulative_totals=cumulative_totals,
            max_cumulative_sell_pct=max_cumulative_sell_pct,
            instrument_types=proposal.instrument_types,
            core_holding_floor_pct=core_holding_floor_pct,
        )
        for ticker, current_value in proposal.current_book.items()
    }

    # Kept so a cycle that traded nothing carries the evidence of why in its own
    # audit row. ``llm_attempts`` is the endpoint's own account of each call —
    # finish_reason, token usage, whether the schema was honoured — which is the
    # only way to tell a model that wrote prose from a completion that was cut
    # off at the token limit.
    llm_raw_responses: list[str] = []
    llm_attempts: list[dict[str, Any]] = []
    decisions, errors = decide_trades(
        db,
        proposal,
        gate_result,
        lessons=lessons,
        llm_call=llm_call,
        system_suffix=(config or {}).get("prompt_framing"),
        cash_eur=cash_balance,
        total_value_eur=total_value,
        fee_schedule=fee_schedule,
        min_ticket_eur=min_ticket,
        max_turnover_eur=turnover_budget,
        raw_responses=llm_raw_responses,
        attempts=llm_attempts,
        rl_signal=rl_signal,
        max_sell_pct_of_position=max_sell_pct_of_position,
        max_sellable_eur_by_ticker=sell_caps,
        instrument_names=names,
    )
    result["errors"].extend(errors)
    if not decisions:
        logger.warning(
            "advisor cycle: no usable decisions for %s; attempts: %s; raw LLM responses: %s",
            portfolio.id, llm_attempts, llm_raw_responses,
        )

    # Sells settle before buys so their proceeds fund the same cycle's buys;
    # within each side the highest-conviction orders go first, so the turnover
    # cap drops the least-convinced trades rather than whichever the model
    # happened to list last.
    executed: list[tuple[TradeDecision, dict[str, Any]]] = []
    skipped: list[dict[str, str]] = []
    ordered = _ordered_decisions(decisions)
    exec_prices = _resolve_execution_prices(
        db,
        proposal,
        {d.ticker for d in ordered}
        | {t for t, v in proposal.current_book.items() if v > 0},
    )
    remaining_budget = turnover_budget
    # A rotation spends the cap twice — once selling, once buying. Left
    # unchecked the sells run first and can exhaust the budget on their own,
    # leaving the sleeve half-rotated and sitting in cash, which is worse than
    # not having traded. Reserve half the budget for the buy side whenever
    # there are buys waiting on the proceeds.
    remaining_sell_budget = (
        turnover_budget / 2 if any(d.action == "buy" for d in ordered) else turnover_budget
    )
    # Names the model wants to buy must not be sold to fund themselves.
    buy_targets = {d.ticker for d in ordered if d.action == "buy"}
    funding_sells: list[dict[str, Any]] = []
    available_cash = cash_balance
    total_fees = 0.0
    for decision in ordered:
        planned, skip_reason = _plan_decision(
            decision, proposal, total_value, min_ticket, max_sell_pct_of_position, sell_caps,
            exec_prices,
        )
        if planned is None:
            if skip_reason is not None:
                skipped.append(
                    {"ticker": decision.ticker, "action": decision.action, "reason": skip_reason}
                )
            continue

        allowance = remaining_budget
        if planned.side == "sell":
            allowance = min(allowance, remaining_sell_budget)

        if planned.notional > allowance:
            # Clip into the remaining allowance when a real order still fits;
            # otherwise the cap is spent and the rest of the queue waits for
            # tomorrow's cycle.
            if allowance < min_ticket:
                skipped.append({
                    "ticker": decision.ticker,
                    "action": decision.action,
                    "reason": (
                        f"turnover cap reached ({max_turnover_pct:.0%} of NAV, "
                        f"€{turnover_budget:,.0f})"
                    ),
                })
                continue
            planned.notional = allowance

        name = names.get(decision.ticker)
        fee = trade_fee(planned.notional, fee_schedule, name, side=planned.side)

        if planned.side == "buy" and planned.notional + fee > available_cash:
            # The model proposed a buy it cannot pay for and did not offer a
            # sell to fund it. Raise the cash by trimming the book toward the
            # optimiser's own target weights, reserving budget for the buy.
            shortfall = planned.notional + fee - available_cash
            raised, raised_notional, records = _raise_cash(
                db,
                portfolio,
                proposal,
                total_value,
                shortfall,
                exclude=buy_targets,
                min_ticket=min_ticket,
                budget=max(0.0, remaining_budget - planned.notional),
                fee_schedule=fee_schedule,
                max_sell_pct_of_position=max_sell_pct_of_position,
                sell_caps=sell_caps,
                prices=exec_prices,
                names=names,
            )
            if records:
                funding_sells.extend(records)
                result["trades"].extend(records)
                available_cash += raised
                remaining_budget -= raised_notional
                remaining_sell_budget -= raised_notional
                total_fees += sum(float(r.get("fee") or 0.0) for r in records)
            if planned.notional + fee > available_cash:
                # The raise is bounded by the turnover cap and by how overweight
                # the book actually is, so it routinely lands short of the full
                # ask: at a 15% cap an 8% target weight leaves the sell able to
                # fund only the budget the buy did not already reserve. Buy what
                # the cash now covers instead of letting the order be rejected —
                # selling into cash and not buying is worse than not trading.
                affordable = min(
                    affordable_notional(available_cash, fee_schedule, name), remaining_budget
                )
                if affordable < min_ticket:
                    skipped.append({
                        "ticker": decision.ticker,
                        "action": decision.action,
                        "reason": (
                            f"unfundable — €{available_cash:,.0f} available after funding "
                            f"sells, below the €{min_ticket:,.0f} minimum ticket"
                        ),
                    })
                    continue
                planned.notional = affordable
                fee = trade_fee(planned.notional, fee_schedule, name, side=planned.side)

        trade, skip_reason = _execute_planned(db, portfolio, planned, fee)
        if trade is not None:
            executed.append((decision, trade))
            result["trades"].append(trade)
            remaining_budget -= planned.notional
            if planned.side == "sell":
                remaining_sell_budget -= planned.notional
                _record_sale(
                    proposal, sell_caps, decision.ticker, float(trade.get("value") or planned.notional)
                )
                available_cash += planned.notional - fee
            else:
                available_cash -= planned.notional + fee
            total_fees += fee
        elif skip_reason is not None:
            skipped.append({"ticker": decision.ticker, "action": decision.action, "reason": skip_reason})
    result["skipped"] = skipped
    result["funding_sells"] = funding_sells
    result["fees_paid"] = round(total_fees, 2)
    result["turnover_used"] = round(turnover_budget - remaining_budget, 2)
    result["turnover_budget"] = round(turnover_budget, 2)

    # Prediction ledger rows (21-trading-day horizon), calibrated immediately:
    # raw confidence stays on ``conviction``, the history-mapped value lands on
    # ``conviction_calibrated`` (PR2 B1).
    #
    # Every decision is logged as *intent*, whether or not it reached the book.
    # Tying the ledger to execution meant a buy rejected for insufficient cash
    # — the model's highest-conviction call — left no evidence at all, so in a
    # cash-starved sleeve the calibration axis could never become computable.
    # ``signal_breakdown.traded`` records which entries became real fills, which
    # keeps reasoning quality separable from execution quality.
    #
    # Holds are logged as ``neutral`` rows priced at the MC spot: a magnitude
    # forecast, not a directional bet. The scorecard scores them for
    # Mincer-Zarnowitz and excludes them from the directional Brier.
    #
    # Deduped against still-open rows: re-logging an unchanged view every day
    # would clear the 20-resolved evolution threshold with ~20 overlapping
    # observations of the same call over the same horizon, understating the
    # standard errors on both Brier and the MZ regression. Only a change of
    # view earns a new row.
    from app.decision.discover.calibrator import calibrate_prediction

    executed_tickers = {decision.ticker for decision, _ in executed}
    prices_by_ticker: dict[str, float | None] = {
        decision.ticker: trade.get("price") for decision, trade in executed
    }
    open_intents = _open_prediction_intents(db, portfolio.id)

    # One row per ticker per cycle. A model that returns both a buy and a hold
    # for the same name has expressed one view, not two — the entry that
    # actually moved the book wins, then any directional call, then the hold.
    def _intent_rank(decision: TradeDecision) -> int:
        if decision.ticker in executed_tickers:
            return 0
        return 1 if decision.action in ("buy", "sell") else 2

    best_by_ticker: dict[str, TradeDecision] = {}
    for decision in decisions:
        incumbent = best_by_ticker.get(decision.ticker)
        if incumbent is None or _intent_rank(decision) < _intent_rank(incumbent):
            best_by_ticker[decision.ticker] = decision

    ledger_entries: list[tuple[TradeDecision, float | None]] = []
    deduped: list[dict[str, str]] = []
    for decision in best_by_ticker.values():
        direction = decision.action if decision.action in ("buy", "sell") else "neutral"
        price = prices_by_ticker.get(decision.ticker)
        if price is None:
            mc = proposal.mc_summaries.get(decision.ticker)
            if mc is None or not mc.spot:
                # Without a mark there is nothing to resolve the forecast against.
                continue
            price = mc.spot
        if (decision.ticker, direction) in open_intents:
            deduped.append({"ticker": decision.ticker, "direction": direction})
            continue
        open_intents.add((decision.ticker, direction))
        ledger_entries.append((decision, price))
    result["predictions_deduped"] = deduped

    stamp = _advisor_stamp(
        db,
        run_id=run_id,
        portfolio_id=portfolio.id,
        system_suffix=(config or {}).get("prompt_framing"),
        decisions=decisions,
        attempts=llm_attempts,
        injected_llm=llm_call is not None,
    )
    calibrated_by_ticker: dict[str, float | None] = {}
    for decision, price in ledger_entries:
        mc = proposal.mc_summaries.get(decision.ticker)
        pred = store_prediction(
            db,
            symbol=decision.ticker,
            composite_score=decision.confidence,
            signal_breakdown={
                "suggested_weight": proposal.suggested_weights.get(decision.ticker),
                "risk_envelope": gate_result.envelope,
                "traded": decision.ticker in executed_tickers,
            },
            direction_hint=decision.action if decision.action in ("buy", "sell") else "neutral",
            user_id=user_id,
            run_id=run_id or "advisor-cycle",
            horizon_days=DEFAULT_HORIZON_DAYS,
            # Deliberately NOT routed through app.foundation.expected_return's
            # anchor selector (Phase 3 of unified-portfolio-engine-implementation.md):
            # that estimator is backward-looking/realised, while this is a
            # genuine forward simulation (GBM calibrated from history, method
            # effectively "monte_carlo_p50") — a better position-sizing input
            # for the advisor loop than a historical anchor would be.
            expected_return=mc.p50 if mc else None,
            expected_return_low=mc.p5 if mc else None,
            expected_return_high=mc.p95 if mc else None,
            thesis=decision.thesis or None,
            mc_prob_positive=mc.prob_positive if mc else None,
            price_at_prediction=price,
            portfolio_id=portfolio.id,
            provenance_json={
                **stamp,
                "decision_sha256": provenance.canonical_hash(asdict(decision)),
                "traded": decision.ticker in executed_tickers,
            },
        )
        pred = calibrate_prediction(db, pred.id, user_id=user_id) or pred
        calibrated_by_ticker[decision.ticker] = pred.conviction_calibrated
        result["predictions"].append(pred.id)
    db.commit()

    # NAV history must advance every run (graduation expects daily snapshots).
    result["snapshot"] = snapshot_paper_portfolio(db, portfolio.id)

    # Audit trail. ``skipped`` matters as much as ``decisions``: it is the only
    # place a rejected buy (insufficient cash, target already met) is visible.
    # "failed" is reserved for a cycle that got nothing usable back from the
    # model. ``decide_trades`` returns per-decision validation errors *alongside*
    # the decisions it did accept, so a single hallucinated ticker in an
    # otherwise fine response used to condemn the whole run — and because
    # "failed" is not terminal, every later trigger that day re-ran it. That is
    # what turned a working loop into 16 "failed" records over 11 trading days.
    # A run that produced usable decisions did its job even when nothing reached
    # the book; ``skipped`` says why, and the validation errors are still
    # recorded in ``errors``.
    #
    # ``result["trades"]`` rather than ``executed``: a cycle whose only fills
    # were funding sells did change the book, and calling that "idle_no_trades"
    # made the trade log and the cycle log contradict each other.
    if result["trades"]:
        cycle_status, result_status = "completed", "completed"
    elif decisions or not errors:
        cycle_status, result_status = "idle_no_trades", "no_trades"
    else:
        cycle_status, result_status = "failed", "failed"

    _record_cycle(
        db,
        portfolio,
        mandate,
        {
            "decisions": [
                {
                    "ticker": d.ticker,
                    "action": d.action,
                    "target_weight": d.target_weight,
                    "thesis": d.thesis,
                    "confidence": d.confidence,
                    "confidence_raw": d.confidence,
                    "confidence_calibrated": calibrated_by_ticker.get(d.ticker),
                }
                for d in decisions
            ],
            "skipped": skipped,
            "llm_raw_responses": llm_raw_responses,
            "llm_attempts": llm_attempts,
            "predictions_deduped": deduped,
            "strategy_id": strategy.id if strategy is not None else None,
            "lessons_injected": lessons,
            "blocked": gate_result.blocked,
            "risk_envelope": gate_result.envelope,
            "trade_flow": {
                "cash_before": round(cash_balance, 2),
                "turnover_budget": round(turnover_budget, 2),
                "turnover_used": round(turnover_budget - remaining_budget, 2),
                "max_turnover_pct": max_turnover_pct,
                "max_sell_pct_of_position": max_sell_pct_of_position,
                "max_cumulative_sell_pct": max_cumulative_sell_pct,
                "cumulative_sell_window_days": cumulative_sell_window_days,
                "core_holding_floor_pct": core_holding_floor_pct,
                "min_ticket_eur": round(min_ticket, 2),
                "fees_paid": round(total_fees, 2),
            },
            "funding_sells": [
                {
                    "ticker": t.get("ticker"),
                    "value": t.get("value"),
                    "fee": t.get("fee"),
                    "reason": "trimmed to optimiser target to fund a buy",
                }
                for t in funding_sells
            ],
            "optimizer_status": proposal.optimizer_status,
            "notes": proposal.notes,
            "discover_run_id": run_id,
            "discover_run_stale": stale,
            "errors": result["errors"],
        },
        status=cycle_status,
        error="; ".join(result["errors"]) if result["errors"] else None,
    )

    result["status"] = result_status
    logger.info(
        "advisor cycle for user %s: %s — %d trades, %d predictions, %d blocked",
        user_id,
        result["status"],
        len(result["trades"]),
        len(result["predictions"]),
        len(result["blocked"]),
    )
    return result
