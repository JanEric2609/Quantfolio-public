"""Is this trade worth it? The cost-versus-edge check, in shadow.

Every order costs money for sure (fee, half the bid-ask spread, tax on a
realised gain) and earns its source's edge only on average. The check asks
one question per trade: does the edge this source has *shown* beat the cost?

    edge  = (2p - 1) * m * notional
    block when edge < cost

``p`` is the chance the call beats MSCI World over its 21-trading-day horizon
(a sell: that the sold name lags it), ``m`` the mean size of the excess
return when it does or does not. ``p`` comes from, in this order:

1. the call's calibrated conviction (``DiscoveryPrediction.conviction_calibrated``,
   ADR 0018), when the advisor logged one for that ticker and day;
2. the sleeve's own resolved trades, shrunk toward a coin flip by
   :data:`PRIOR_DATES` pseudo-dates (a sleeve with ten trading days of
   history moves ``p`` only a little);
3. nothing: ``p = 0.5``, so no edge, and any cost blocks the trade.

That third case is the honest default and the reason the check runs in
**shadow** first (owner's decision, 2026-10-14): the paper books trade as
before; each trade gets a :class:`PaperTradeCheck` row with the verdict, and
after its horizon the row is marked with what the trade actually added
against MSCI World net of cost. Adding the blocked trades' contributions back
gives the NAV the book would have had with the check on; the owner decides
after two to three months whether to enforce it.

The counterfactual is benchmark-relative, the same definition of a hit the
calibrator uses: a blocked buy's money is taken to sit in MSCI World instead,
a blocked sell's position is kept. Each trade's contribution is

    contribution = sign * notional * (r_asset - r_benchmark) - cost

over the trade's horizon, which makes contributions additive across trades.
It is an estimate: it ignores what a later trade did with the same shares.

Tax: with a valid NV certificate (Control Center, Tax) a paper sale's gain is
taken as tax-free; otherwise the flat KESt+Soli on the gain after
Teilfreistellung (30 % for an equity ETF) is counted in full. That overstates
the cost (the tax would be paid at some later sale anyway), so it is a bound,
not a forecast. Estimates only, not tax advice.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    DiscoveryPrediction,
    PaperHolding,
    PaperPortfolio,
    PaperTrade,
    PaperTradeCheck,
)

logger = logging.getLogger(__name__)

HORIZON_TRADING_DAYS = 21
#: Pseudo-dates of a coin flip the sleeve's own hit rate is shrunk toward.
PRIOR_DATES = 20
#: Typical size of a 21-day excess return when a sleeve has no history yet.
DEFAULT_MEAN_ABS_EXCESS = 0.04
#: Half the bid-ask spread paid on each order, by instrument kind.
HALF_SPREAD_BPS = {"etf": 5.0, "stock": 10.0}
#: Flat tax a realised gain pays without an NV certificate: KESt 25 % + Soli 5.5 %.
FLAT_TAX_RATE = 0.25 * 1.055
EQUITY_ETF_EXEMPT_SHARE = 0.30
#: The user's own trades are not the system's; only managed sleeves are checked.
MANUAL = "manual"
#: How far back a first run looks for unchecked trades.
BACKFILL_DAYS = 120
#: The advisor books a trade and then, in the same cycle, stores the call it
#: came from; a call stored later than this belongs to another cycle.
SAME_CYCLE = timedelta(hours=1)
#: Calendar days after its horizon a check is frozen on the last closes there
#: are (a holiday or a delisting leaves no close on the horizon day itself).
RESOLVE_GRACE_DAYS = 7
#: A check is marked only when both prices start within this many calendar
#: days of the trade (a longer gap would measure a different period).
START_SLACK_DAYS = 5
#: Units below this count as no position (float residue of Numeric quantities).
_EMPTY = 1e-9


@dataclass(frozen=True)
class Edge:
    p_hit: float | None
    source: str  # "calibrated" | "track_record" | "none"
    mean_abs_excess: float
    detail: str


@dataclass(frozen=True)
class Verdict:
    cost_eur: float
    edge_eur: float
    verdict: str  # "pass" | "block"
    reason: str


def assess(*, notional: float, cost_eur: float, edge: Edge) -> Verdict:
    """Pure: the verdict on one trade of *notional* euros."""
    p = edge.p_hit if edge.p_hit is not None else 0.5
    edge_eur = (2.0 * p - 1.0) * edge.mean_abs_excess * notional
    if edge_eur >= cost_eur:
        reason = (
            f"Expected edge €{edge_eur:,.2f} covers the cost €{cost_eur:,.2f} "
            f"({edge.detail})."
        )
        return Verdict(cost_eur, edge_eur, "pass", reason)
    if edge.p_hit is None or edge_eur <= 0:
        why = f"no shown edge ({edge.detail})"
    else:
        why = f"expected edge €{edge_eur:,.2f} is below it ({edge.detail})"
    return Verdict(cost_eur, edge_eur, "block", f"Costs €{cost_eur:,.2f}; {why}.")


#: Paper asset types that are funds (``instrument_taxonomy.to_paper_asset_type``).
_FUND_KINDS = ("etf", "fund", "bond", "money_market")


def _is_etf(asset_type: str | None) -> bool:
    """A fund of any kind: it pays the ETF spread."""
    kind = (asset_type or "").lower()
    return any(k in kind for k in _FUND_KINDS)


def _is_equity_fund(asset_type: str | None) -> bool:
    """A fund that gets the 30 % Teilfreistellung. Bond and money-market funds
    get none, so they are taxed in full (the safe direction, as in ``fund_class``)."""
    kind = (asset_type or "").lower()
    return _is_etf(kind) and "bond" not in kind and "money" not in kind


def half_spread_eur(notional: float, asset_type: str | None) -> float:
    bps = HALF_SPREAD_BPS["etf" if _is_etf(asset_type) else "stock"]
    return notional * bps / 10_000.0


def traded_asset_type(db: Session, trade: PaperTrade) -> str:
    """What the trade traded: recorded at execution, else its holding, else the
    same evidence ladder that typed the holding (plan 2026-10-14, M2)."""
    from app.decision.paper_portfolio import resolve_paper_asset_type

    if trade.asset_type:
        return trade.asset_type
    if trade.holding is not None and trade.holding.asset_type:
        return trade.holding.asset_type
    isin = trade.isin or (trade.holding.isin if trade.holding is not None else None)
    return resolve_paper_asset_type(db, trade.ticker, isin=isin)


def sale_tax_eur(gain_eur: float | None, *, etf: bool, nv_valid: bool) -> float:
    """Tax a sale's realised gain pays now (0 under an NV certificate or for a loss)."""
    if nv_valid or gain_eur is None or gain_eur <= 0:
        return 0.0
    taxable = gain_eur * (1.0 - EQUITY_ETF_EXEMPT_SHARE if etf else 1.0)
    return taxable * FLAT_TAX_RATE


def horizon_end(day: date) -> date:
    return np.busday_offset(np.datetime64(day), HORIZON_TRADING_DAYS, roll="forward").astype(object)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


def _calibrated_p(db: Session, trade: PaperTrade) -> float | None:
    """The calibrated conviction of the call behind this trade.

    The advisor stores its call just after booking the trade, so the window
    reaches :data:`SAME_CYCLE` past it; a later cycle's call was not known
    when the trade was made.
    """
    when = _utc(trade.date)
    row = (
        db.query(DiscoveryPrediction.conviction_calibrated)
        .filter(
            DiscoveryPrediction.portfolio_id == trade.portfolio_id,
            DiscoveryPrediction.symbol == trade.ticker,
            DiscoveryPrediction.direction == trade.side,
            DiscoveryPrediction.conviction_calibrated.isnot(None),
            DiscoveryPrediction.predicted_at >= when - timedelta(days=1),
            DiscoveryPrediction.predicted_at <= when + SAME_CYCLE,
        )
        .order_by(DiscoveryPrediction.predicted_at.desc())
        .first()
    )
    return float(row[0]) if row is not None else None


def track_record(db: Session, portfolio_id: str, before: date) -> tuple[float | None, int, float]:
    """``(shrunk hit rate, distinct dates, mean |excess|)`` of the sleeve's resolved trades.

    Only trades whose horizon ended before *before* count, so a verdict never
    uses an outcome that was not known on its day. Trades of one day share one
    market move, so each date counts once (its mean hit).
    """
    rows = (
        db.query(PaperTradeCheck.traded_on, PaperTradeCheck.side,
                 PaperTradeCheck.asset_return, PaperTradeCheck.benchmark_return)
        .filter(
            PaperTradeCheck.portfolio_id == portfolio_id,
            PaperTradeCheck.resolved.is_(True),
            PaperTradeCheck.horizon_end < before,
            PaperTradeCheck.asset_return.isnot(None),
            PaperTradeCheck.benchmark_return.isnot(None),
        )
        .all()
    )
    if not rows:
        return None, 0, DEFAULT_MEAN_ABS_EXCESS
    by_day: dict[date, list[float]] = defaultdict(list)
    sizes: list[float] = []
    for traded_on, side, r_asset, r_bench in rows:
        excess = (1.0 if side == "buy" else -1.0) * (float(r_asset) - float(r_bench))
        by_day[traded_on].append(1.0 if excess > 0 else 0.0)
        sizes.append(abs(excess))
    n = len(by_day)
    hits = sum(sum(v) / len(v) for v in by_day.values())
    p = (hits + 0.5 * PRIOR_DATES) / (n + PRIOR_DATES)
    return p, n, float(np.mean(sizes)) if sizes else DEFAULT_MEAN_ABS_EXCESS


def edge_for(db: Session, trade: PaperTrade, traded_on: date) -> Edge:
    p_track, n_dates, mean_abs = track_record(db, trade.portfolio_id, traded_on)
    calibrated = _calibrated_p(db, trade)
    if calibrated is not None:
        return Edge(calibrated, "calibrated", mean_abs, f"calibrated hit chance {calibrated:.0%}")
    if p_track is not None:
        return Edge(
            p_track, "track_record", mean_abs,
            f"{n_dates} resolved trading days, hit chance {p_track:.0%} after shrinkage",
        )
    return Edge(None, "none", mean_abs, "no resolved trades or calibrated calls yet")


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _avg_cost_before(db: Session, trade: PaperTrade) -> float | None:
    """Average cost of the units this sale sold, as it stood just before the sale.

    Seeded holdings have no buy trade, so the position the run started with is
    found by undoing every trade from the current holding; the trades up to
    this sale are then replayed forward from there. ``None`` when the basis
    cannot be known (a seeded position fully sold and rebought before it).
    A sale booked since migration 0138 carries its basis, which wins.
    """
    if trade.side == "sell" and trade.avg_cost is not None:
        return float(trade.avg_cost)
    trades = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == trade.portfolio_id, PaperTrade.ticker == trade.ticker)
        .order_by(PaperTrade.date, PaperTrade.created_at, PaperTrade.id)
        .all()
    )
    holding = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == trade.portfolio_id, PaperHolding.ticker == trade.ticker)
        .first()
    )
    qty = float(holding.quantity) if holding is not None else 0.0
    avg: float | None = float(holding.avg_buy_price) if holding is not None and holding.avg_buy_price else None
    if qty <= _EMPTY:
        qty, avg = 0.0, None
    for past in reversed(trades):
        q, price = float(past.quantity), float(past.price)
        if past.side == "buy":
            before = qty - q
            avg = (qty * avg - q * price) / before if before > _EMPTY and avg is not None else None
            qty = max(before, 0.0)
        else:
            if qty <= _EMPTY:
                avg = None  # the cost of a position sold out is not in what remains
            qty += q
    for past in trades:
        if past.id == trade.id:
            return avg
        q, price = float(past.quantity), float(past.price)
        if past.side == "buy":
            if qty <= _EMPTY:
                avg = price
            elif avg is not None:
                avg = (qty * avg + q * price) / (qty + q)
            qty += q
        else:
            qty -= q
            if qty <= _EMPTY:
                qty, avg = 0.0, None
    return None


def _nv_valid(db: Session, user_id: str, year: int) -> bool:
    from app.foundation.tax_allowances import nv_status, settings_for_user

    return bool(nv_status(settings_for_user(db, user_id), year)["valid_for_year"])


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


def _close_on_or_after(closes: dict[str, float], day: date) -> tuple[str, float] | None:
    for key in sorted(closes):
        if key >= day.isoformat():
            return key, closes[key]
    return None


def _close_on_or_before(closes: dict[str, float], day: date) -> tuple[str, float] | None:
    keys = [k for k in sorted(closes) if k <= day.isoformat()]
    return (keys[-1], closes[keys[-1]]) if keys else None


def _first_key_from(closes: dict[str, float], day: str) -> str:
    return min(k for k in closes if k >= day)


def _last_key_to(closes: dict[str, float], day: str) -> str:
    return max(k for k in closes if k <= day)


def _paired_returns(
    asset: dict[str, float], bench: dict[str, float], start: date, end: date,
) -> tuple[float, float, str] | None:
    """``(asset return, benchmark return, last date)`` over the same dates for both.

    Both start at the first day each has a close on or after *start* (within
    :data:`START_SLACK_DAYS`) and end at the last day both have one by *end*,
    so a gap in one series never compares a short period with a full one.
    """
    first_a, first_b = _close_on_or_after(asset, start), _close_on_or_after(bench, start)
    if first_a is None or first_b is None:
        return None
    latest_start = (start + timedelta(days=START_SLACK_DAYS)).isoformat()
    if first_a[0] > latest_start or first_b[0] > latest_start:
        return None
    last_a, last_b = _close_on_or_before(asset, end), _close_on_or_before(bench, end)
    if last_a is None or last_b is None:
        return None
    begin, finish = max(first_a[0], first_b[0]), min(last_a[0], last_b[0])
    if finish < begin:
        return None
    start_a, start_b = asset[_first_key_from(asset, begin)], bench[_first_key_from(bench, begin)]
    end_a, end_b = asset[_last_key_to(asset, finish)], bench[_last_key_to(bench, finish)]
    if start_a <= 0 or start_b <= 0:
        return None
    return end_a / start_a - 1.0, end_b / start_b - 1.0, finish


def mark(check: PaperTradeCheck, asset: dict[str, float], bench: dict[str, float], today: date) -> None:
    """Mark *check* to the latest closes; freeze it once its horizon has a close or the grace has passed.

    Past the grace a check whose prices stop more than ``START_SLACK_DAYS``
    before its horizon (or never start) is frozen without an outcome: it then
    counts neither in the track record nor in the result.
    """
    past_grace = today >= check.horizon_end + timedelta(days=RESOLVE_GRACE_DAYS)
    paired = _paired_returns(asset, bench, check.traded_on, min(check.horizon_end, today))
    earliest_end = (check.horizon_end - timedelta(days=START_SLACK_DAYS)).isoformat()
    if paired is None or (past_grace and paired[2] < earliest_end):
        check.asset_return = check.benchmark_return = check.contribution_eur = None
        check.resolved = past_grace
        return
    ra, rb, finish = paired
    check.asset_return, check.benchmark_return = ra, rb
    sign = 1.0 if check.side == "buy" else -1.0
    check.contribution_eur = sign * check.notional_eur * (ra - rb) - check.cost_eur
    check.resolved = finish >= check.horizon_end.isoformat() or past_grace


# ---------------------------------------------------------------------------
# The daily job
# ---------------------------------------------------------------------------


def _managed_portfolios(db: Session) -> list[PaperPortfolio]:
    return db.query(PaperPortfolio).filter(PaperPortfolio.managed_by != MANUAL).all()


def trade_costs(db: Session, trade: PaperTrade, *, nv_valid: bool) -> tuple[float, float, float]:
    """The trade's fee, half-spread and sale tax in EUR."""
    notional = float(trade.value)
    asset_type = traded_asset_type(db, trade)
    fee = float(trade.fee or 0)
    spread = half_spread_eur(notional, asset_type)
    tax = 0.0
    if trade.side == "sell":
        basis = _avg_cost_before(db, trade)
        gain = (float(trade.price) - basis) * float(trade.quantity) if basis is not None else None
        tax = sale_tax_eur(gain, etf=_is_equity_fund(asset_type), nv_valid=nv_valid)
    return fee, spread, tax


def check_trade(db: Session, trade: PaperTrade, *, nv_valid: bool) -> PaperTradeCheck:
    traded_on = _utc(trade.date).date()
    notional = float(trade.value)
    fee, spread, tax = trade_costs(db, trade, nv_valid=nv_valid)
    cost = fee + spread + tax
    edge = edge_for(db, trade, traded_on)
    verdict = assess(notional=notional, cost_eur=cost, edge=edge)
    check = PaperTradeCheck(
        trade_id=trade.id, portfolio_id=trade.portfolio_id, traded_on=traded_on,
        ticker=trade.ticker, side=trade.side, notional_eur=notional,
        fee_eur=fee, spread_eur=spread, tax_eur=tax, cost_eur=cost,
        p_hit=edge.p_hit, p_source=edge.source, mean_abs_excess=edge.mean_abs_excess,
        edge_eur=verdict.edge_eur, verdict=verdict.verdict, reason=verdict.reason,
        horizon_end=horizon_end(traded_on),
    )
    db.add(check)
    db.flush()
    return check


def recost(db: Session, check: PaperTradeCheck, trade: PaperTrade, *, nv_valid: bool) -> bool:
    """Re-cost a stored check with the trade's asset type (plan 2026-10-14, M2).

    The edge stays as it was stored at the check: only the fee, spread and tax
    are recomputed, then the verdict and, once marked, the contribution.
    Returns whether anything changed.
    """
    fee, spread, tax = trade_costs(db, trade, nv_valid=nv_valid)
    if abs(spread - check.spread_eur) < 0.005 and abs(tax - check.tax_eur) < 0.005:
        return False
    cost = fee + spread + tax
    reason = check.reason or ""
    detail = reason[reason.rfind("(") + 1:reason.rfind(")")] if "(" in reason else check.p_source
    edge = Edge(check.p_hit, check.p_source, check.mean_abs_excess, detail)
    verdict = assess(notional=check.notional_eur, cost_eur=cost, edge=edge)
    check.fee_eur, check.spread_eur, check.tax_eur, check.cost_eur = fee, spread, tax, cost
    check.edge_eur, check.verdict, check.reason = verdict.edge_eur, verdict.verdict, verdict.reason
    if check.asset_return is not None and check.benchmark_return is not None:
        sign = 1.0 if check.side == "buy" else -1.0
        check.contribution_eur = sign * check.notional_eur * (check.asset_return - check.benchmark_return) - cost
    return True


def run_daily(db: Session, today: date | None = None) -> dict[str, Any]:
    """Check new trades of every managed sleeve, then mark the open checks.

    Trades are checked oldest first, and before each one every earlier check
    whose horizon has ended is marked, so a backfill sees the same track
    record a daily run would have seen on that day.
    """
    from app.foundation import eur_prices

    today = today or datetime.now(UTC).date()
    since = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
    checked = marked = 0
    for portfolio in _managed_portfolios(db):
        nv_by_year: dict[int, bool] = {}
        trades = (
            db.query(PaperTrade)
            .outerjoin(PaperTradeCheck, PaperTradeCheck.trade_id == PaperTrade.id)
            .filter(
                PaperTrade.portfolio_id == portfolio.id,
                PaperTradeCheck.id.is_(None),
                PaperTrade.date >= since,
            )
            .order_by(PaperTrade.date)
            .all()
        )
        open_checks = (
            db.query(PaperTradeCheck)
            .filter(PaperTradeCheck.portfolio_id == portfolio.id, PaperTradeCheck.resolved.is_(False))
            .all()
        )
        if not trades and not open_checks:
            continue
        earliest = min([c.traded_on for c in open_checks] + [_utc(t.date).date() for t in trades])
        days = (today - earliest).days + 10
        closes: dict[str, dict[str, float]] = {}

        def series(ticker: str) -> dict[str, float]:
            if ticker not in closes:
                closes[ticker] = eur_prices.eur_closes(db, ticker, days=days, allow_live=False)
            return closes[ticker]

        bench = eur_prices.eur_closes(db, eur_prices.benchmark_ticker(db), days=days, allow_live=False)
        for trade in trades:
            traded_on = _utc(trade.date).date()
            due = [c for c in open_checks if not c.resolved and c.horizon_end < traded_on]
            for check in due:
                mark(check, series(check.ticker), bench, today)
                marked += 1
            if due:
                db.flush()
            year = traded_on.year
            if year not in nv_by_year:
                nv_by_year[year] = _nv_valid(db, portfolio.user_id, year)
            open_checks.append(check_trade(db, trade, nv_valid=nv_by_year[year]))
            checked += 1
        for check in open_checks:
            if not check.resolved:
                mark(check, series(check.ticker), bench, today)
                marked += 1
    db.commit()
    return {"checked": checked, "marked": marked}


def summary(db: Session, portfolio_id: str, nav_eur: float | None = None) -> dict[str, Any]:
    """The shadow result for one sleeve: what the check would have changed."""
    rows = (
        db.query(PaperTradeCheck)
        .filter(PaperTradeCheck.portfolio_id == portfolio_id)
        .order_by(PaperTradeCheck.traded_on.desc())
        .all()
    )
    blocked = [r for r in rows if r.verdict == "block"]
    passed = [r for r in rows if r.verdict == "pass"]

    def total(items: list[PaperTradeCheck], attr: str) -> float:
        return round(sum(float(getattr(r, attr) or 0.0) for r in items), 2)

    blocked_contribution = total(blocked, "contribution_eur")
    return {
        "mode": "shadow",
        "trades_checked": len(rows),
        "would_block": len(blocked),
        "cost_paid_eur": total(rows, "cost_eur"),
        "cost_of_blocked_eur": total(blocked, "cost_eur"),
        # What the blocked trades added against MSCI World, net of cost; the
        # check would have given this up (negative: it would have saved it).
        "blocked_contribution_eur": blocked_contribution,
        "passed_contribution_eur": total(passed, "contribution_eur"),
        "open": sum(1 for r in rows if not r.resolved),
        "nav_eur": nav_eur,
        "checked_nav_eur": round(nav_eur - blocked_contribution, 2) if nav_eur is not None else None,
        "recent": [
            {
                "trade_id": r.trade_id, "traded_on": r.traded_on.isoformat(), "ticker": r.ticker,
                "side": r.side, "notional_eur": round(r.notional_eur, 2), "cost_eur": round(r.cost_eur, 2),
                "tax_eur": round(r.tax_eur, 2), "edge_eur": round(r.edge_eur, 2), "p_hit": r.p_hit,
                "p_source": r.p_source, "verdict": r.verdict, "reason": r.reason,
                "contribution_eur": None if r.contribution_eur is None else round(r.contribution_eur, 2),
                "resolved": r.resolved,
            }
            for r in rows[:50]
        ],
        "estimate": True,
        "not_tax_advice": True,
    }


def register_trade_check_job(scheduler: Any | None = None) -> str:
    """Daily at 22:30 UTC, after the day's cycles and the closing prices."""
    from app.foundation.jobs import register_cron_job

    def _inner() -> None:
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as db:
            logger.info("trade_check_daily: %s", run_daily(db))

    return register_cron_job("trade_check_daily", _inner, scheduler=scheduler, hour=22, minute=30)


__all__ = [
    "Edge", "Verdict", "assess", "edge_for", "half_spread_eur", "horizon_end", "mark",
    "register_trade_check_job", "run_daily", "sale_tax_eur", "summary", "track_record",
]
