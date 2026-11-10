"""Mandate B — the rules control (2026-11-09).

Mandates A and B used to be two LLM prompts that differed by one sentence of
style; on prod they held 12 of the same 13 names and sat 0.46 % apart, so
comparing them taught nothing. B is now the same review on the same
candidates under the same risk limits, decided by fixed rules instead of a
model. A minus B is then the value the LLM adds over a plain momentum book —
the question the research pass of 2026-11-09 found to be the one this app can
actually answer (LLM stock picks show little skill once memorisation is
controlled; a rules sleeve is the standard control).

The rules are frozen here, before the run starts, so the comparison cannot be
tuned after the fact:

1. Candidates: the newest completed Discover run's shortlist, exactly what A
   sees, minus the core ETF.
2. Signal: 12-1 momentum (return from 12 months to 1 month ago, EUR closes).
   Only names with positive momentum are eligible (absolute momentum).
3. Risk filter: with 4 or more eligible names, the most volatile quarter is
   dropped (the low-volatility anomaly; high-volatility momentum crashes worst).
4. Selection: up to ``TOP_K`` names. A holding is kept while it still ranks in
   the top ``HOLD_RANK`` (a buffer: entering is harder than staying, which
   halves turnover without changing the signal).
5. Weights: inverse volatility across the selection, scaled to the satellite
   share (1 − the mandate's minimum ETF weight), each capped at the mandate's
   single-position limit; whatever the caps leave goes to the core ETF.
6. Crash guard: when the core ETF's last 24 months are negative (a bear
   market) *and* its last month is over 10 % more volatile than its last year, the
   stock share is halved and the rest goes to the core ETF. Momentum's worst
   losses came in exactly that state, in the rebound after a fall (Daniel &
   Moskowitz 2016, "Momentum crashes"; scaling momentum down when volatility
   is high roughly doubled its Sharpe ratio, Barroso & Santa-Clara 2015).
   Most of a crash is the short leg, which this long-only book does not hold,
   so the guard is a half step, not an exit.
7. Trades: only where a position is off target by more than ``BAND`` of NAV
   and the order is at least ``MIN_ORDER_EUR``. Sells first, then buys from
   the cash they free.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import LlmPortfolioDecision, PaperHolding, PaperPortfolio

logger = logging.getLogger(__name__)

RULES_STYLE = "rules_control"
TOP_K = 5
HOLD_RANK = 8
BAND = 0.02
MIN_ORDER_EUR = 25.0
LOOKBACK_DAYS = 252
SKIP_DAYS = 21
MIN_CLOSES = LOOKBACK_DAYS + 1
HORIZON_WEEKS = 4
#: Crash guard (rule 6): market lookback for the bear test, the two
#: volatility windows, and the stock share kept when it fires.
BEAR_DAYS = 504
VOL_SHORT_DAYS = 21
VOL_LONG_DAYS = 252
CRASH_SCALE = 0.5
#: "More volatile" means clearly so: a month at its year's level is noise.
VOL_RATIO = 1.1

RULES_CONFIG: dict[str, Any] = {
    "style": RULES_STYLE,
    # A's risk limits, so the two mandates differ only in how names are chosen.
    "max_single_position_pct": 0.15,
    "min_etf_pct": 0.40,
    "top_k": TOP_K,
    "hold_rank": HOLD_RANK,
    "momentum": "12-1",
    "rebalance_band": BAND,
    "crash_guard": "24m market return < 0 and 1m vol > 1.1 x 12m vol: stock share x0.5",
}
RULES_NAME = "Mandate B — Rules control"


def _closes(db: Session, symbol: str, days: int = 420) -> list[float]:
    """EUR closes, oldest first (stubbed in tests)."""
    from app.foundation.eur_prices import eur_closes

    series = eur_closes(db, symbol, days=days)
    return [series[d] for d in sorted(series)]


def _ann_vol(closes: list[float]) -> float | None:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 252)


def crash_state(market: list[float]) -> dict[str, Any]:
    """Rule 6 on the core ETF's closes, oldest first (pure).

    ``{"known", "bear", "vol_1m", "vol_12m", "active"}``; with less than 24
    months of closes the guard is unknown and stays off.
    """
    if len(market) < BEAR_DAYS + 1 or market[-1 - BEAR_DAYS] <= 0:
        return {"known": False, "active": False}
    bear = market[-1] / market[-1 - BEAR_DAYS] - 1.0 < 0
    short = _ann_vol(market[-1 - VOL_SHORT_DAYS:])
    long = _ann_vol(market[-1 - VOL_LONG_DAYS:])
    high_vol = short is not None and long is not None and short > VOL_RATIO * long
    return {
        "known": True, "bear": bear,
        "vol_1m": None if short is None else round(short, 4),
        "vol_12m": None if long is None else round(long, 4),
        "active": bool(bear and high_vol),
    }


def momentum_and_vol(closes: list[float]) -> tuple[float | None, float | None]:
    """12-1 momentum and annualised volatility over the last year (pure)."""
    if len(closes) < MIN_CLOSES:
        return None, None
    window = closes[-MIN_CLOSES:]
    start, end = window[0], window[-1 - SKIP_DAYS]
    if start <= 0 or end <= 0:
        return None, None
    rets = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None, None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var * 252)
    return end / start - 1.0, vol if vol > 0 else None


def rules_targets(
    signals: dict[str, tuple[float, float]],
    *,
    held: set[str],
    satellite_share: float,
    max_single: float,
    top_k: int = TOP_K,
    hold_rank: int = HOLD_RANK,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Satellite target weights and the ranking behind them (pure).

    *signals* maps ticker → (12-1 momentum, annualised vol).
    """
    eligible = [(t, m, v) for t, (m, v) in signals.items() if m > 0 and v > 0]
    dropped_for_vol: set[str] = set()
    if len(eligible) >= 4:
        by_vol = sorted(eligible, key=lambda x: x[2], reverse=True)
        dropped_for_vol = {t for t, _, _ in by_vol[: len(eligible) // 4]}
    ranked = sorted(
        ((t, m, v) for t, m, v in eligible if t not in dropped_for_vol), key=lambda x: x[1], reverse=True
    )
    rank_of = {t: i + 1 for i, (t, _, _) in enumerate(ranked)}

    kept = [t for t in held if rank_of.get(t, 10**6) <= hold_rank]
    kept.sort(key=lambda t: rank_of[t])
    selected = kept[:top_k]
    for t, _, _ in ranked:
        if len(selected) >= top_k:
            break
        if t not in selected:
            selected.append(t)

    vol_of = {t: v for t, _, v in eligible}
    inv = {t: 1.0 / vol_of[t] for t in selected}
    total = sum(inv.values())
    weights = {t: min(max_single, satellite_share * w / total) for t, w in inv.items()} if total else {}

    ranking = []
    for t, (m, v) in sorted(signals.items(), key=lambda kv: kv[1][0], reverse=True):
        if t in dropped_for_vol:
            status = "dropped: most volatile quarter"
        elif m <= 0:
            status = "dropped: momentum not positive"
        elif t in weights:
            status = "kept (buffer)" if t in kept and rank_of[t] > top_k else "selected"
        else:
            status = "ranked below the cut"
        ranking.append({
            "ticker": t, "momentum_12_1": round(m, 4), "vol": round(v, 4),
            "rank": rank_of.get(t), "status": status, "target_weight": round(weights.get(t, 0.0), 4),
        })
    return weights, ranking


def _book(
    db: Session, portfolio: PaperPortfolio, paper: Any
) -> tuple[dict[str, tuple[float, float, str | None]], float]:
    """ticker → (EUR value, quantity, isin), and cash."""
    from app.foundation.paper_cash import paper_cash_balance

    paper_quote_eur = paper.paper_quote_eur

    book: dict[str, tuple[float, float, str | None]] = {}
    for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).all():
        if not h.ticker or not h.quantity:
            continue
        price = paper_quote_eur(db, h.ticker).get("price") or h.avg_buy_price or 0
        book[h.ticker.upper()] = (float(h.quantity) * float(price), float(h.quantity), h.isin)
    return book, float(paper_cash_balance(portfolio, db))


def adopt_rules_config(db: Session, portfolio: PaperPortfolio) -> None:
    """Move an existing Mandate B portfolio onto the rules config (one time)."""
    config = json.loads(portfolio.mandate_config_json or "{}")
    if config.get("style") != RULES_STYLE:
        portfolio.mandate_config_json = json.dumps(RULES_CONFIG)
        portfolio.name = RULES_NAME
        db.flush()


def run_rules_review(
    db: Session, user_id: str, portfolio: PaperPortfolio, mandate: str = "B", *, paper: Any
) -> dict[str, Any]:
    """Rebalance the sleeve to the rules' targets and journal it like a review.

    *paper* is the ``app.decision.paper_portfolio`` module, handed in by
    ``review.run_mandate_review``: the decision-loop independence contract
    lets only ``review`` and ``mirror`` reach the paper book directly.
    """
    from app.decision.llm_portfolio.assessment import core_etf_ticker
    from app.decision.llm_portfolio.gates import check_buy_gates
    from app.decision.llm_portfolio.shortlist import latest_shortlist

    def execute_trade(*args: Any, **kwargs: Any) -> Any:
        return paper.execute_trade(*args, **kwargs)

    def paper_order_fee(*args: Any, **kwargs: Any) -> float:
        return float(paper.paper_order_fee(*args, **kwargs))

    def paper_quote_eur(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return paper.paper_quote_eur(*args, **kwargs)

    adopt_rules_config(db, portfolio)
    config = json.loads(portfolio.mandate_config_json or "{}")
    max_single = float(config.get("max_single_position_pct", 0.15))
    satellite_share = 1.0 - float(config.get("min_etf_pct", 0.40))
    core = core_etf_ticker(db)

    candidates = {c.symbol.upper(): c for c in latest_shortlist(db, user_id) if c.symbol.upper() != core}
    signals: dict[str, tuple[float, float]] = {}
    no_history: list[str] = []
    for sym in candidates:
        mom, vol = momentum_and_vol(_closes(db, sym))
        if mom is None or vol is None:
            no_history.append(sym)
        else:
            signals[sym] = (mom, vol)

    crash = crash_state(_closes(db, core, days=760))
    if crash["active"]:
        satellite_share *= CRASH_SCALE

    book, cash = _book(db, portfolio, paper)
    nav = sum(v for v, _, _ in book.values()) + cash
    held = {t for t in book if t != core}
    # A holding no longer on the shortlist has no signal; it ranks below the cut.
    satellite, ranking = rules_targets(signals, held=held, satellite_share=satellite_share, max_single=max_single)
    targets = {**satellite, core: 1.0 - sum(satellite.values())}

    orders: list[dict[str, Any]] = []
    if nav > 0:
        # Sells first: they fund the buys.
        for ticker, (value, qty, isin) in sorted(book.items()):
            target_value = targets.get(ticker, 0.0) * nav
            excess = value - target_value
            if value <= 0:
                continue
            # A name the rules no longer hold is sold outright, however small;
            # otherwise only a drift past the band is worth an order.
            if target_value > 0 and (excess <= BAND * nav or excess < MIN_ORDER_EUR):
                continue
            sell_qty = qty if target_value <= 0 else qty * excess / value
            price = value / qty
            fee = paper_order_fee(db, ticker, sell_qty * price, isin=isin, side="sell")
            try:
                execute_trade(db, portfolio.id, ticker, "sell", sell_qty, price, confidence=None,
                              rationale="Rules control: above target", fee=fee, isin=isin)
                orders.append({"side": "sell", "ticker": ticker, "eur": round(sell_qty * price, 2)})
            except ValueError as exc:
                orders.append({"side": "sell", "ticker": ticker, "skipped": str(exc)})
        book, cash = _book(db, portfolio, paper)
        for ticker, weight in sorted(targets.items(), key=lambda kv: -kv[1]):
            value = book.get(ticker, (0.0, 0.0, None))[0]
            gap = weight * nav - value
            if gap <= BAND * nav or gap < MIN_ORDER_EUR:
                continue
            pq = paper_quote_eur(db, ticker)
            eur = None if pq.get("stale") else pq.get("price")
            if not eur or eur <= 0:
                orders.append({"side": "buy", "ticker": ticker, "skipped": "no EUR price"})
                continue
            notional = min(gap, cash - 1.0)
            if notional < MIN_ORDER_EUR:
                orders.append({"side": "buy", "ticker": ticker, "skipped": "not enough cash"})
                continue
            price = Decimal(str(eur))
            quantity = (Decimal(str(notional)) / price).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
            cand = candidates.get(ticker)
            isin = (cand.isin if cand is not None else None) or book.get(ticker, (0.0, 0.0, None))[2]
            gate = check_buy_gates(db, user_id, config, ticker, isin, quantity, price, mandate=mandate)
            if not gate["passed"]:
                orders.append({"side": "buy", "ticker": ticker, "skipped": gate.get("reason") or "gate"})
                continue
            fee = paper_order_fee(db, ticker, float(quantity * price), isin=isin)
            try:
                execute_trade(db, portfolio.id, ticker, "buy", float(quantity), float(price), confidence=None,
                              rationale="Rules control: below target", fee=fee, isin=isin)
                cash -= float(quantity * price) + fee
                orders.append({"side": "buy", "ticker": ticker, "eur": round(float(quantity * price), 2)})
            except ValueError as exc:
                orders.append({"side": "buy", "ticker": ticker, "skipped": str(exc)})

    traded = [o for o in orders if "eur" in o]
    selected = [r["ticker"] for r in ranking if r["target_weight"] > 0]
    thesis = (
        f"Rules: 12-1 momentum on {len(signals)} shortlisted name(s) with a year of prices"
        + (f" ({len(no_history)} without)" if no_history else "")
        + f"; holds {', '.join(selected) or 'no stock'} at inverse-volatility weights, "
        f"{targets[core]:.0%} in {core}"
        + (" (crash guard on: market down over 24 months and volatile, stock share halved)" if crash["active"] else "")
        + f". {len(traded)} order(s)."
    )
    decision = {
        "action": "rebalance" if traded else "hold",
        "ticker": "",
        "quantity": 0,
        "thesis": thesis,
        "rules": {**RULES_CONFIG, "core": core},
        "ranking": ranking,
        "targets": {t: round(w, 4) for t, w in targets.items()},
        "orders": orders,
        "no_history": no_history,
        "crash_guard": crash,
        # Pre-registered: does the sleeve beat EUNL.DE over the next 4 weeks?
        "expectation": {
            "metric": "benchmark_excess_pct", "direction": "increase", "magnitude": 0.0,
            "horizon_weeks": HORIZON_WEEKS,
        },
    }
    row = LlmPortfolioDecision(
        portfolio_id=portfolio.id,
        review_date=datetime.now(UTC),
        mandate=mandate,
        decision_json=json.dumps(decision, default=str),
        reflection_json=json.dumps({"rules": True, "trade_created": bool(traded)}),
        context_token_estimate=0,
        status="completed",
        horizon_weeks=HORIZON_WEEKS,
    )
    db.add(row)
    db.commit()
    logger.info("Mandate %s rules review: %d order(s), selection %s", mandate, len(traded), selected)
    return {
        "status": "completed",
        "mandate": mandate,
        "portfolio_id": portfolio.id,
        "decision_id": row.id,
        "decisions": [decision],
        "trades": traded,
    }
