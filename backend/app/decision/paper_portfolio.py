"""Paper portfolio service: simulated AI trading with performance tracking.

Every amount is in EUR: prices are restated from the listing's currency
(``eur_prices``), trades are booked in EUR and dividends are credited to cash
in EUR on their ex-date (``paper_cash_flows``). A portfolio is seeded at the
market value of the synced book, so its return starts at 0; it then has no
external flows, so ``NAV / baseline - 1`` is its time-weighted return. Its
benchmark is the same starting value in MSCI World EUR from the inception
date (a passive book run alongside it). A reset archives the run so far
(``paper_portfolio_archives``) and starts a new one.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.live_positions import combined_positions, synced_cash
from app.foundation.models.entities import (
    MetricsSnapshot,
    PaperCashFlow,
    PaperHolding,
    PaperPortfolio,
    PaperPortfolioArchive,
    PaperSnapshot,
    PaperTrade,
)
from app.foundation.models.entities._core import now_utc
from app.foundation.market import quote as market_quote

from app.foundation import quant_metrics
from app.foundation.metrics_snapshots import upsert_metrics_snapshot
from app.foundation.paper_cash import MAX_QUOTE_AGE_TRADING_DAYS
from app.foundation.settings import get_risk_free_rate

logger = logging.getLogger(__name__)

_FALLBACK_CASH = Decimal("100000")
METRICS_CONTEXT = "paper_portfolio"
# How far back a daily run looks for dividends it has not credited yet.
DIVIDEND_LOOKBACK_DAYS = 60


def paper_quote_eur(
    db: Session, ticker: str, *, rate_cache: dict[str, dict[str, float] | None] | None = None,
) -> dict[str, Any]:
    """Latest price of ``ticker`` in EUR, with the listing price and currency it came from.

    ``price`` is ``None`` when there is no quote or no exchange rate: a
    dollar price must never be booked as euros. ``stale`` is True when the
    price is older than :data:`MAX_QUOTE_AGE_TRADING_DAYS`
    trading days: value at it, never trade at it.
    """
    from app.foundation.eur_prices import eur_price

    try:
        q = market_quote(db, ticker)
    except Exception as exc:  # noqa: BLE001 - a provider outage means no price
        logger.warning("market_quote failed for %s: %s", ticker, exc)
        return {"price": None, "local": None, "currency": None}
    local = q.get("price")
    if not local or float(local) <= 0:
        return {"price": None, "local": None, "currency": q.get("currency")}
    # Staleness is the quote's own age; the provider-failure flag alone only
    # matters when the age is unknown.
    stale = _quote_is_stale(db, ticker, provider_stale=bool(q.get("stale")))
    return {
        "price": eur_price(db, ticker, float(local), rate_cache=rate_cache),
        "local": float(local),
        "currency": q.get("currency"),
        "stale": stale,
    }


def _quote_is_stale(db: Session, ticker: str, *, provider_stale: bool = False) -> bool:
    """True when the newest cached bar of *ticker* is older than :data:`MAX_QUOTE_AGE_TRADING_DAYS`.

    With no cached bar the age is unknown: stale only if the provider failed.
    """
    import numpy as np

    from app.foundation.models.entities import PriceCache

    row = (
        db.query(PriceCache.date)
        .filter(PriceCache.ticker == ticker.upper())
        .order_by(PriceCache.date.desc())
        .first()
    )
    if row is None or row[0] is None:
        return provider_stale
    last = row[0].date() if isinstance(row[0], datetime) else row[0]
    today = now_utc().date()
    if last >= today:
        return False
    return int(np.busday_count(last, today)) > MAX_QUOTE_AGE_TRADING_DAYS


def inception_date(portfolio: PaperPortfolio) -> date:
    start = portfolio.inception_at or portfolio.created_at
    return start.date() if start is not None else date.today()


def resolve_paper_asset_type(db: Session, ticker: str, isin: str | None = None) -> str:
    """Best-effort ``asset_type`` for a newly opened ``PaperHolding`` row.

    New paper positions default to ``"stock"`` unconditionally regardless of
    what was actually bought — confirmed in prod: XEON.DE (a money-market
    ETF) paper positions are stored with ``asset_type='stock'``. Resolution
    follows the shared evidence ladder in
    :func:`app.foundation.instrument_taxonomy.resolve_holding_asset_type`
    (T3.1, DEC-E): an ``Asset`` row matched by ISIN is authoritative, then
    the classify_instrument heuristics using the best Discover-sourced name.
    A symbol-level Asset match is retained only for tickers without an ISIN
    — with an ISIN in hand, a symbol-matched row is cross-identifier
    guesswork, so heuristics on the real name are the safer fallback.
    Defaults to ``"stock"`` when nothing is known — unchanged behaviour for
    tickers with no available metadata.
    """
    from app.foundation.models.entities import Asset
    from app.foundation.instrument_taxonomy import resolve_holding_asset_type

    ticker_u = ticker.upper()
    if not isin:
        asset = db.query(Asset).filter(Asset.symbol == ticker_u).one_or_none()
        if asset is not None and asset.asset_type:
            return asset.asset_type

    name = resolve_instrument_name(db, ticker_u, isin=isin)
    return resolve_holding_asset_type(
        db,
        isin=isin,
        symbol=ticker_u,
        name=None if name == ticker_u else name,
    )


def resolve_instrument_name(db: Session, ticker: str, isin: str | None = None) -> str:
    """Best-effort real instrument name (see ``foundation.instrument_names``)."""
    from app.foundation.instrument_names import resolve_instrument_name as _resolve

    return _resolve(db, ticker, isin)


def get_or_create_paper_portfolio(db: Session, user_id: str) -> tuple[PaperPortfolio, bool]:
    """Return existing manual paper portfolio for *user_id*, creating one if needed.

    On first creation, seeds from DKB accounts/positions. If no DKB sync
    exists, falls back to €100k initial cash.

    Returns:
        (portfolio, seeded_from_dkb) — seeded_from_dkb is True when the
        portfolio was freshly seeded from a real DKB sync, False otherwise.
    """
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == "manual")
        .first()
    )
    if portfolio is not None:
        # Determine seeded_from_dkb based on whether holdings exist
        has_holdings = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).count() > 0
        seeded_from_dkb = has_holdings and portfolio.initial_cash != _FALLBACK_CASH
        return portfolio, seeded_from_dkb

    # Try to seed from the synced accounts (DKB, Scalable) — synced_cash sums ONLY
    # cash accounts; a depot's balance is its holdings market value, not spendable cash.
    synced_total, has_dkb = synced_cash(db, user_id)
    total_cash = synced_total if has_dkb else _FALLBACK_CASH

    portfolio = PaperPortfolio(
        user_id=user_id,
        name="Manual Baseline",
        currency="EUR",
        initial_cash=total_cash,
        # Establish the baseline at creation so the NULL fallback never folds
        # later manual buys into the cost basis. When holdings are seeded from
        # DKB below, recompute_baseline_value overwrites this with the seeded
        # cost basis included.
        baseline_value=total_cash,
        mandate="manual",
        managed_by="manual",
        inception_at=now_utc(),
    )
    db.add(portfolio)
    try:
        db.commit()
        db.refresh(portfolio)
        logger.info("Created manual paper portfolio for user %s (initial_cash=%.2f)", user_id, float(total_cash))
    except IntegrityError:
        db.rollback()
        portfolio = (
            db.query(PaperPortfolio)
            .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == "manual")
            .first()
        )
        if portfolio is None:
            raise
        return portfolio, False

    seeded_from_dkb = False
    _set_benchmark_base(portfolio, _benchmark_base_quote(db))
    if has_dkb:
        _seed_holdings(db, portfolio.id, user_id)
        recompute_baseline_value(db, portfolio.id)
        seeded_from_dkb = True
    else:
        db.commit()

    return portfolio, seeded_from_dkb


def reseed_manual_portfolio(db: Session, portfolio_id: str, user_id: str) -> tuple[int, bool]:
    """Reset the manual paper portfolio (see :func:`reset_portfolio`).

    Returns:
        (holdings_count, seeded_from_dkb)
    """
    result = reset_portfolio(db, portfolio_id, reason="reseed from the synced book")
    return result["holdings_count"], result["seeded_from_real_book"]


def _rows(query, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for row in query.all():
        item = {}
        for col in columns:
            value = getattr(row, col, None)
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, date):  # datetime is a date
                value = value.isoformat()
            item[col] = value
        out.append(item)
    return out


def reset_portfolio(db: Session, portfolio_id: str, *, reason: str = "reset") -> dict[str, Any]:
    """Archive the current run and start a new one from the synced book at market value.

    Holdings, trades, snapshots, dividends, metrics snapshots and scorecards
    of the run so far move into one ``PaperPortfolioArchive`` row; nothing is
    lost. Decisions, advice cards and competition records stay where they
    are: they record what was decided, not what the book was worth. The new
    run starts today with the synced positions (DKB, Scalable) at today's
    EUR prices plus the synced cash, or EUR 100,000 in cash when nothing is
    synced, so its return starts at 0.
    """
    from app.foundation.models.entities import AdvisorScorecard

    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    holdings_q = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id)
    trades_q = db.query(PaperTrade).filter(PaperTrade.portfolio_id == portfolio_id)
    snaps_q = db.query(PaperSnapshot).filter(PaperSnapshot.portfolio_id == portfolio_id)
    flows_q = db.query(PaperCashFlow).filter(PaperCashFlow.portfolio_id == portfolio_id)
    metrics_q = db.query(MetricsSnapshot).filter(MetricsSnapshot.portfolio_id == portfolio_id)
    cards_q = db.query(AdvisorScorecard).filter(AdvisorScorecard.portfolio_id == portfolio_id)

    # Everything that may touch the network (and so commit a price-cache row)
    # happens before the first change: the archive, the deletes, the seed and
    # the baseline then go in ONE transaction, committed once at the end.
    try:
        final = get_summary(db, portfolio_id)
    except Exception as exc:  # noqa: BLE001 - a pricing outage must not block a reset
        logger.warning("reset: final NAV unavailable for %s: %s", portfolio_id, exc)
        final = {}
    total_cash, has_accounts = synced_cash(db, portfolio.user_id)
    plan = _plan_seed(db, portfolio.user_id) if has_accounts else []
    bench_base = _benchmark_base_quote(db)

    payload: dict[str, Any] = {
        "final": {
            "total_value": final.get("total_value"),
            "total_return_pct": final.get("total_return_pct"),
            "benchmark": final.get("benchmark"),
            "benchmark_return_pct": (final.get("benchmark") or {}).get("total_return_pct"),
            "as_of": now_utc().isoformat(),
        },
        "portfolio": {
            "name": portfolio.name, "mandate": portfolio.mandate, "currency": portfolio.currency,
            "initial_cash": str(portfolio.initial_cash),
            "baseline_value": None if portfolio.baseline_value is None else str(portfolio.baseline_value),
            "created_at": portfolio.created_at.isoformat() if portfolio.created_at else None,
            "inception_at": inception.isoformat() if (inception := portfolio.inception_at) else None,
        },
        "holdings": _rows(holdings_q, ("id", "isin", "ticker", "name", "asset_type", "quantity", "avg_buy_price", "currency")),
        "trades": _rows(trades_q, ("id", "date", "ticker", "side", "quantity", "price", "value", "fee",
                                   "confidence", "rationale", "ai_decision_id")),
        "snapshots": _rows(snaps_q, ("date", "total_value", "cash_balance", "securities_value", "total_return_pct",
                                     "sharpe", "max_drawdown")),
        "cash_flows": _rows(flows_q, ("date", "kind", "ticker", "quantity", "amount_per_unit", "currency", "amount_eur")),
        "metrics_snapshots": _rows(metrics_q, ("as_of", "context", "sharpe", "sortino", "calmar", "cvar_95",
                                               "max_drawdown", "volatility")),
        "scorecards": _rows(cards_q, ("window_start", "window_end", "n_predictions", "n_resolved", "sharpe", "sortino",
                                      "calmar")),
    }
    archive = PaperPortfolioArchive(
        portfolio_id=portfolio_id, user_id=portfolio.user_id, inception_at=portfolio.inception_at or portfolio.created_at,
        reason=reason[:200], payload_json=json.dumps(payload, default=str),
    )
    try:
        db.add(archive)
        for q in (trades_q, flows_q, snaps_q, metrics_q, cards_q, holdings_q):
            q.delete(synchronize_session=False)
        portfolio.initial_cash = total_cash if has_accounts else _FALLBACK_CASH
        portfolio.inception_at = now_utc()
        _set_benchmark_base(portfolio, bench_base)
        db.flush()
        db.expire_all()
        _apply_seed(db, portfolio_id, plan)
        baseline = recompute_baseline_value(db, portfolio_id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    holdings_count = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).count()
    logger.info("reset paper portfolio %s (%s): archive %s, %d holdings, baseline %.2f",
                portfolio_id, reason, archive.id, holdings_count, float(baseline))
    return {
        "id": portfolio_id,
        "archive_id": archive.id,
        "holdings_count": holdings_count,
        "baseline_value": float(baseline),
        "inception_at": inception.isoformat() if (inception := portfolio.inception_at) else None,
        "seeded_from_real_book": has_accounts,
        "archived": {k: len(v) for k, v in payload.items() if isinstance(v, list)},
    }


def _plan_seed(db: Session, user_id: str) -> list[dict[str, Any]]:
    """The synced positions (DKB, Scalable) as holdings to seed, one per ISIN, at today's EUR price.

    Does all the price lookups (which may fetch live and commit a cache row)
    so :func:`_apply_seed` can run inside one transaction without network.
    The cost of a seeded unit is its market price at the seed, not what was
    paid for it at the broker: the paper run starts today, so its return
    starts at 0 and measures only what happens from here.
    """
    from app.foundation.instrument_taxonomy import resolve_holding_asset_type

    # Manual holdings are not seeded, so a broker position awaiting
    # reconciliation with a hand-entered twin is kept rather than lost.
    positions = combined_positions(db, user_id, include_unreconciled=True)
    logger.info("_seed_holdings: found %d synced positions for user %s", len(positions), user_id)
    rate_cache: dict[str, dict[str, float] | None] = {}
    plan: list[dict[str, Any]] = []
    for pos in positions:
        if pos.quantity <= 0:
            continue
        price: Decimal | None = None
        if pos.ticker:
            eur = paper_quote_eur(db, pos.ticker, rate_cache=rate_cache)["price"]
            if eur and eur > 0:
                price = Decimal(str(eur))
        if price is None and pos.current_price and pos.current_price > 0:
            # The broker's own price (the brokers report in EUR).
            price = Decimal(str(pos.current_price))
        if price is None:
            costed = [d for d in pos.depots if d.avg_buy_price is not None and d.avg_buy_price > 0]
            qty = sum((d.quantity for d in costed), Decimal("0"))
            if qty > 0:
                price = sum((d.quantity * Decimal(d.avg_buy_price or 0) for d in costed), Decimal("0")) / qty
        if price is None:
            logger.warning("_seed_holdings: no price for %s; not seeded", pos.key)
            continue
        plan.append({
            "isin": pos.isin or None, "ticker": pos.ticker, "name": pos.name, "quantity": pos.quantity,
            "price": price,
            "asset_type": resolve_holding_asset_type(db, isin=pos.isin or None, symbol=pos.ticker, name=pos.name),
        })
    return plan


def _apply_seed(db: Session, portfolio_id: str, plan: list[dict[str, Any]]) -> None:
    """Add the planned holdings. No commit: the caller owns the transaction."""
    for item in plan:
        db.add(PaperHolding(
            portfolio_id=portfolio_id, isin=item["isin"], ticker=item["ticker"], name=item["name"],
            quantity=item["quantity"], avg_buy_price=item["price"], asset_type=item["asset_type"],
            currency="EUR",
        ))
    db.flush()


def _seed_holdings(db: Session, portfolio_id: str, user_id: str) -> None:
    """Copy the synced positions into PaperHoldings (see :func:`_plan_seed`) and commit."""
    _apply_seed(db, portfolio_id, _plan_seed(db, user_id))
    db.commit()


def _benchmark_base_quote(db: Session) -> tuple[float | None, datetime]:
    """The benchmark's current fresh EUR quote, and the moment it was taken.

    Looked up with the same source (:func:`paper_quote_eur`) the sleeve's
    holdings are seeded at, so both start at the same instant. ``None`` when
    there is no fresh quote (the close on/before inception is used instead).
    """
    from app.foundation.eur_prices import benchmark_ticker

    try:
        q = paper_quote_eur(db, benchmark_ticker(db).upper())
    except Exception as exc:  # noqa: BLE001 - no benchmark base is not fatal
        logger.warning("benchmark base quote failed: %s", exc)
        return None, now_utc()
    price = q.get("price")
    if price and price > 0 and not q.get("stale"):
        return float(price), now_utc()
    return None, now_utc()


def _set_benchmark_base(portfolio: PaperPortfolio, base: tuple[float | None, datetime]) -> None:
    price, at = base
    portfolio.benchmark_base_price = None if price is None else Decimal(str(price))
    portfolio.benchmark_base_at = at if price is not None else None


def seed_paper_portfolio_from_real(
    db: Session,
    user_id: str,
    *,
    # Default mandate of the advisor loop's main sleeve. The ADVISOR_MANDATE
    # constant lives in app.decision.advisor.cycle; it is inlined here (same
    # literal) so this module stays dependency-free w.r.t. the decision loop.
    mandate: str = "advisor",
    name: str = "Advisor Loop",
) -> PaperPortfolio:
    """Seed a paper sleeve as a clone of the real book + cash (D0).

    At t0 the portfolio mirrors the user's current synced holdings (DKB, Scalable) plus the real
    cash balance (non-depot accounts). Idempotent — an existing portfolio for
    *mandate* is returned unchanged; the seed basis (as-of timestamp, source)
    is recorded in ``mandate_config_json`` for auditability. Champion and
    challenger sleeves both seed this way so evolution compares strategies,
    not starting points (PR2 C3).
    """
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == mandate)
        .one_or_none()
    )
    if portfolio is not None:
        return portfolio

    # Real cash = non-depot account balances at every synced bank and broker
    # (depot balances mirror holdings).
    total_cash, _ = synced_cash(db, user_id)

    seed_basis = {
        "seeded_from": "real_book",
        "as_of": now_utc().isoformat(),
        "source": "synced_positions+non_depot_cash",
        "cash_seeded": float(total_cash),
    }
    portfolio = PaperPortfolio(
        user_id=user_id,
        name=name,
        initial_cash=total_cash,
        managed_by="llm",
        mandate=mandate,
        mandate_config_json=json.dumps(seed_basis),
        inception_at=now_utc(),
    )
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)

    _set_benchmark_base(portfolio, _benchmark_base_quote(db))
    _seed_holdings(db, portfolio.id, user_id)
    recompute_baseline_value(db, portfolio.id)
    db.refresh(portfolio)
    logger.info(
        "seed_paper_portfolio_from_real: created advisor portfolio %s (cash=%.2f)",
        portfolio.id,
        float(total_cash),
    )
    return portfolio


def _current_cash_balance(portfolio: PaperPortfolio, db: Session) -> Decimal:
    """Current cash (see ``foundation.paper_cash.paper_cash_balance``)."""
    from app.foundation.paper_cash import paper_cash_balance

    return paper_cash_balance(portfolio, db)


def current_cash_balance(portfolio: PaperPortfolio, db: Session) -> Decimal:
    """Cash of a paper portfolio after trades, fees and dividends (what ``execute_trade`` checks)."""
    return _current_cash_balance(portfolio, db)


def paper_order_fee(
    db: Session, ticker: str, notional: float, *, isin: str | None = None, name: str | None = None,
    side: str = "buy",
) -> float:
    """Scalable fee for one paper order (0.99 EUR; a Prime ETF buy from 250 EUR is free)."""
    from app.foundation.broker_fees import scalable_order_fee

    if notional <= 0:
        return 0.0
    if name is None:
        name = resolve_instrument_name(db, ticker.upper(), isin)
    return scalable_order_fee(float(notional), name, side)


def _cash_flows_total(db: Session, portfolio_id: str) -> Decimal:
    rows = db.query(PaperCashFlow.amount_eur).filter(PaperCashFlow.portfolio_id == portfolio_id).all()
    return sum((Decimal(r[0] or 0) for r in rows), Decimal("0"))


def _seeded_cost_basis(db: Session, portfolio_id: str) -> Decimal:
    """Cost basis of currently-held paper holdings (sum of quantity * avg_buy_price)."""
    rows = (
        db.query(PaperHolding.quantity, PaperHolding.avg_buy_price)
        .filter(PaperHolding.portfolio_id == portfolio_id)
        .all()
    )
    return sum(((q or Decimal("0")) * (p or Decimal("0")) for q, p in rows), Decimal("0"))


def _baseline_value(db: Session, portfolio: PaperPortfolio) -> Decimal:
    """Return the invested-capital basis used for total_return_pct.

    This is the *starting total value* (initial cash + cost basis of seeded
    holdings), NOT the cash sleeve. Using ``initial_cash`` alone as the basis
    produced a phantom return because seeded securities were added for free.

    Prefers the stored ``baseline_value``; falls back to a live computation for
    legacy rows where it was never set.
    """
    if portfolio.baseline_value is not None and portfolio.baseline_value > 0:
        return portfolio.baseline_value
    fallback = (portfolio.initial_cash or Decimal("0")) + _seeded_cost_basis(db, portfolio.id)
    return fallback if fallback > 0 else (portfolio.initial_cash or Decimal("0"))


def recompute_baseline_value(db: Session, portfolio_id: str, *, commit: bool = True) -> Decimal:
    """Set and persist baseline_value = initial_cash + cost basis of seeded holdings.

    Call this after any seed/reseed so total_return_pct is measured against the
    true starting capital.
    """
    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")
    baseline = (portfolio.initial_cash or Decimal("0")) + _seeded_cost_basis(db, portfolio_id)
    portfolio.baseline_value = baseline
    if commit:
        db.commit()
    return baseline


def get_holdings(db: Session, portfolio_id: str) -> list[dict[str, Any]]:
    """Return paper holdings enriched with current market values."""
    holdings = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio_id)
        .order_by(PaperHolding.ticker.asc())
        .all()
    )
    if not holdings:
        return []

    portfolio = db.get(PaperPortfolio, portfolio_id)
    cash_balance = _current_cash_balance(portfolio, db) if portfolio else Decimal("0")

    result: list[dict[str, Any]] = []
    total_securities_value = Decimal("0")

    rate_cache: dict[str, dict[str, float] | None] = {}
    for h in holdings:
        quote = paper_quote_eur(db, h.ticker, rate_cache=rate_cache) if h.ticker else {"price": None}
        current_price: float | None = quote.get("price")
        has_price = current_price is not None and current_price > 0
        # A stale quote still values the holding, but it is not "priced".
        stale = has_price and bool(quote.get("stale"))
        priced = has_price and not stale
        if stale:
            logger.warning("Quote for %s is stale; valued at the last price", h.ticker)
        if not has_price:
            # Valued at cost, and flagged: no quote, or no exchange rate for it.
            current_price = float(h.avg_buy_price) if h.avg_buy_price and h.avg_buy_price > 0 else None
            logger.warning("No EUR price for %s; valued at cost", h.ticker)
        market_value = Decimal("0")
        unrealized_pnl = Decimal("0")
        return_pct = 0.0
        if current_price is not None and current_price > 0:
            market_value = h.quantity * Decimal(str(current_price))
            total_securities_value += market_value
            unrealized_pnl = market_value - (h.quantity * h.avg_buy_price)
            if h.avg_buy_price and h.avg_buy_price > 0:
                return_pct = float((Decimal(str(current_price)) - h.avg_buy_price) / h.avg_buy_price)

        result.append(
            {
                "id": h.id,
                "ticker": h.ticker,
                "isin": h.isin,
                "name": h.name,
                "asset_type": h.asset_type,
                "quantity": float(h.quantity),
                "avg_buy_price": float(h.avg_buy_price),
                "current_price": current_price,
                "price_local": quote.get("local"),
                "quote_currency": quote.get("currency"),
                "priced": priced,
                "stale": stale,
                "market_value": float(market_value),
                "unrealized_pnl": float(unrealized_pnl),
                "return_pct": return_pct,
                "currency": "EUR",
            }
        )

    total_value = cash_balance + total_securities_value
    for row in result:
        row["weight_pct"] = float(Decimal(str(row["market_value"])) / total_value) if total_value > 0 else 0.0

    return result


def get_trades(db: Session, portfolio_id: str, limit: int = 50) -> list[PaperTrade]:
    """Return recent paper trades ordered by date desc."""
    return (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == portfolio_id)
        .order_by(PaperTrade.date.desc())
        .limit(limit)
        .all()
    )


def get_snapshots(db: Session, portfolio_id: str, days: int = 365) -> list[PaperSnapshot]:
    """Return paper snapshots for charting."""
    cutoff = date.today() - timedelta(days=days)
    return (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id, PaperSnapshot.date >= cutoff)
        .order_by(PaperSnapshot.date.asc())
        .all()
    )


def _isins_by_ticker(db: Session, portfolio_id: str, tickers: set[str]) -> dict[str, str]:
    """Best-effort ISIN per ticker: the paper holding's, else the asset registry's."""
    from app.foundation.models.entities import Asset

    out: dict[str, str] = {}
    if not tickers:
        return out
    for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).all():
        if h.ticker and h.isin:
            out[h.ticker.upper()] = h.isin
    missing = [t for t in tickers if t not in out]
    if missing:
        for symbol, isin in db.query(Asset.symbol, Asset.isin).filter(Asset.symbol.in_(missing)).all():
            if symbol and isin:
                out.setdefault(symbol.upper(), isin)
    return out


def _fund_flags(
    db: Session, portfolio_id: str, tickers: set[str], isins: dict[str, str]
) -> dict[str, bool]:
    """Whether each ticker is a fund, from the holding's/asset's type and the instrument name."""
    from app.foundation.withholding_tax import looks_like_fund

    held = {
        (h.ticker or "").upper(): h
        for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).all()
    }
    flags: dict[str, bool] = {}
    for t in tickers:
        h = held.get(t)
        name = h.name if h is not None and h.name and h.name.upper() != t else resolve_instrument_name(db, t, isins.get(t))
        flags[t] = looks_like_fund(name, h.asset_type if h is not None else None)
    return flags


def credit_dividends(db: Session, portfolio_id: str, today: date | None = None) -> list[dict[str, Any]]:
    """Credit cash dividends not yet booked, per share held at the ex-date, in EUR on that date.

    Looks at every instrument held now or traded since the start of the
    window. The quantity held at the ex-date is today's quantity less the
    net buys from the ex-date on (a buy on the ex-date gets no dividend).
    Amounts are net of the source withholding tax a German resident suffers
    (``withholding_tax.withholding_rate``, by the issuer's ISIN country
    prefix; 15 % when the ISIN is unknown). ``amount_per_unit`` keeps the
    gross per-share figure, ``amount_eur`` the net credit.
    """
    from app.foundation.withholding_tax import withholding_rate

    from app.foundation.data_backbone.listing_currency import resolve_quote_currency
    from app.foundation.eur_prices import eur_price
    from app.foundation.market import dividend_history

    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")
    today = today or now_utc().date()
    inception_day = inception_date(portfolio)
    start = max(inception_day, today - timedelta(days=DIVIDEND_LOOKBACK_DAYS))
    held = {
        (h.ticker or "").upper(): Decimal(h.quantity or 0)
        for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).all()
        if h.ticker
    }
    trades = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == portfolio_id, PaperTrade.date >= start)
        .all()
    )
    tickers = set(held) | {t.ticker.upper() for t in trades}
    isins = _isins_by_ticker(db, portfolio_id, tickers)
    fund_flags = _fund_flags(db, portfolio_id, tickers, isins)
    booked = {
        (f.ticker, f.date)
        for f in db.query(PaperCashFlow).filter(PaperCashFlow.portfolio_id == portfolio_id, PaperCashFlow.kind == "dividend")
    }
    credited: list[dict[str, Any]] = []
    rate_cache: dict[str, dict[str, float] | None] = {}
    for ticker in sorted(tickers):
        try:
            rows = dividend_history(db, ticker, start)
        except Exception as exc:  # noqa: BLE001 - one ticker must not stop the rest
            logger.warning("credit_dividends: no dividend history for %s: %s", ticker, exc)
            continue
        for row in rows:
            ex = date.fromisoformat(str(row["ex_date"])[:10])
            # Strictly after the inception day: the seed price of that day is
            # already ex-dividend, so an ex-date on it was never earned.
            if ex < start or ex <= inception_day or ex > today or (ticker, ex) in booked:
                continue
            qty = held.get(ticker, Decimal("0"))
            for t in trades:
                if t.ticker.upper() == ticker and t.date.date() >= ex:
                    qty += -t.quantity if t.side == "buy" else t.quantity
            if qty <= 0:
                continue
            per_unit_eur = eur_price(db, ticker, float(row["amount"]), on=ex.isoformat(), rate_cache=rate_cache)
            if per_unit_eur is None:
                logger.warning("credit_dividends: no EUR rate for %s on %s; not credited yet", ticker, ex)
                continue
            net_factor = Decimal(str(1.0 - withholding_rate(isins.get(ticker), is_fund=fund_flags.get(ticker))))
            amount = (qty * Decimal(str(per_unit_eur)) * net_factor).quantize(Decimal("0.01"))
            db.add(PaperCashFlow(
                portfolio_id=portfolio_id, date=ex, kind="dividend", ticker=ticker, quantity=qty,
                amount_per_unit=Decimal(str(row["amount"])), currency=(resolve_quote_currency(db, ticker) or "EUR")[:3],
                amount_eur=amount,
            ))
            booked.add((ticker, ex))
            credited.append({"ticker": ticker, "ex_date": ex.isoformat(), "quantity": float(qty), "amount_eur": float(amount)})
    if credited:
        db.commit()
    return credited


def passive_benchmark(
    db: Session, portfolio: PaperPortfolio, baseline: Decimal | float, *, series: bool = False,
) -> dict[str, Any]:
    """The same starting value in MSCI World EUR (the configured benchmark) from the inception date."""
    from app.foundation.eur_prices import benchmark_ticker, eur_closes

    symbol = benchmark_ticker(db).upper()
    start = inception_date(portfolio)
    out: dict[str, Any] = {"symbol": symbol, "start": start.isoformat(), "available": False}
    closes = eur_closes(db, symbol, days=(date.today() - start).days + 15)
    keys = sorted(closes)

    # Start: the benchmark's EUR quote at the instant the sleeve was seeded
    # (same source as the holdings). Older rows have none: fall back to the
    # last close on or before the inception date.
    stored = portfolio.benchmark_base_price
    if stored is not None and stored > 0:
        base = float(stored)
        out["base_source"] = "seed_quote"
    else:
        before = [k for k in keys if k <= start.isoformat()]
        if not before or (start - date.fromisoformat(before[-1])).days > 5:
            return out
        base = closes[before[-1]]
        out["base_source"] = "close_on_inception"

    # End: the quote the NAV is valued with, so both ends are like-for-like;
    # the latest cached close only when there is no fresh quote.
    end_quote = paper_quote_eur(db, symbol)
    end_price = end_quote.get("price")
    if end_price and end_price > 0 and not end_quote.get("stale"):
        end, as_of = float(end_price), now_utc().date().isoformat()
    elif keys:
        end, as_of = closes[keys[-1]], keys[-1]
    else:
        return out
    ret = end / base - 1.0
    out.update({
        "available": True,
        "as_of": as_of,
        "total_return_pct": ret,
        "value": float(baseline) * (1.0 + ret),
    })
    if series:
        out["series"] = {k: float(baseline) * closes[k] / base for k in keys if k >= start.isoformat()}
    return out


def get_summary(db: Session, portfolio_id: str) -> dict[str, Any]:
    """Return portfolio summary dict."""
    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    holdings = get_holdings(db, portfolio_id)
    cash_balance = _current_cash_balance(portfolio, db)
    securities_value = sum(Decimal(str(h.get("market_value", 0))) for h in holdings)
    total_value = cash_balance + securities_value
    baseline = _baseline_value(db, portfolio)
    total_return_pct = float((total_value - baseline) / baseline) if baseline else 0.0
    stale_quotes = sorted(h["ticker"] for h in holdings if h.get("stale"))

    latest_snapshot = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id)
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    latest_metrics = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.portfolio_id == portfolio_id, MetricsSnapshot.context == METRICS_CONTEXT)
        .order_by(MetricsSnapshot.as_of.desc())
        .first()
    )
    # Fall back to the legacy PaperSnapshot columns for portfolios whose most
    # recent compute_metrics() run predates the metrics_snapshots table.
    # Coalesce per *value*, not per row: every metrics_snapshots column is
    # nullable, so keying the fallback on row presence let a snapshot row with
    # a NULL sharpe blank out a perfectly good legacy value.
    def _coalesce(metric_value: float | None, legacy_value: float | None) -> float | None:
        return metric_value if metric_value is not None else legacy_value

    sharpe = _coalesce(
        latest_metrics.sharpe if latest_metrics else None,
        latest_snapshot.sharpe if latest_snapshot else None,
    )
    max_dd = _coalesce(
        latest_metrics.max_drawdown if latest_metrics else None,
        latest_snapshot.max_drawdown if latest_snapshot else None,
    )

    trade_count = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == portfolio_id)
        .count()
    )
    # Days of snapshot history behind the metrics: an annualised Sharpe from
    # a few weeks is noise, and the UI withholds it below a year.
    first_snapshot = (
        db.query(PaperSnapshot.date)
        .filter(PaperSnapshot.portfolio_id == portfolio_id)
        .order_by(PaperSnapshot.date.asc())
        .first()
    )
    history_days = (
        (latest_snapshot.date - first_snapshot[0]).days
        if first_snapshot is not None and latest_snapshot is not None
        else 0
    )

    benchmark = passive_benchmark(db, portfolio, baseline)
    if benchmark.get("available"):
        benchmark["excess_return_pct"] = total_return_pct - benchmark["total_return_pct"]
    return {
        "id": portfolio.id,
        "name": portfolio.name,
        "currency": portfolio.currency,
        "inception_at": inception_date(portfolio).isoformat(),
        "dividends_eur": float(_cash_flows_total(db, portfolio.id)),
        "benchmark": benchmark,
        "initial_cash": float(portfolio.initial_cash),
        "baseline_value": float(baseline),
        "total_value": float(total_value),
        "cash_balance": float(cash_balance),
        "securities_value": float(securities_value),
        "total_return_pct": total_return_pct,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "holding_count": len(holdings),
        "trade_count": trade_count,
        "history_days": history_days,
        # Holdings valued at a quote older than MAX_QUOTE_AGE_TRADING_DAYS.
        "stale_quotes": stale_quotes,
    }


def snapshot_paper_portfolio(
    db: Session, portfolio_id: str, as_of: date | None = None
) -> dict[str, Any]:
    """Create or update the daily PaperSnapshot of a date from current holdings.

    Valuation convention: every dated row ends up as that date's END-OF-DAY
    valuation. The 00:00 UTC job passes ``as_of`` = the date that just ended,
    valuing at its last close and overwriting the provisional row the 10:00
    advisor cycle wrote that day (which, called without ``as_of``, snapshots
    "today" at the live quote right after trading).
    """
    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    # UTC, not the process-local civil date. These snapshot dates become
    # MetricsSnapshot.as_of, and advisor/scorecard.py already keys its rows on
    # now_utc().date() — two clocks writing the same column produced an
    # off-by-one for late-evening runs in a Berlin (UTC+1/+2) deployment.
    today = as_of or now_utc().date()
    try:
        credit_dividends(db, portfolio_id, today)
    except Exception as exc:  # noqa: BLE001 - a missing dividend must not cost the snapshot
        logger.warning("credit_dividends failed for %s: %s", portfolio_id, exc)
    cash_balance = _current_cash_balance(portfolio, db)
    holdings = get_holdings(db, portfolio_id)
    securities_value = sum(Decimal(str(h.get("market_value", 0))) for h in holdings)
    total_value = cash_balance + securities_value
    baseline = _baseline_value(db, portfolio)
    total_return_pct = float((total_value - baseline) / baseline) if baseline else 0.0
    stale_quotes = sorted(h["ticker"] for h in holdings if h.get("stale"))

    existing = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id, PaperSnapshot.date == today)
        .one_or_none()
    )
    if existing is None:
        snap = PaperSnapshot(
            portfolio_id=portfolio_id,
            date=today,
            total_value=total_value,
            cash_balance=cash_balance,
            securities_value=securities_value,
            total_return_pct=Decimal(str(total_return_pct)),
            currency=portfolio.currency,
        )
        db.add(snap)
    else:
        existing.total_value = total_value
        existing.cash_balance = cash_balance
        existing.securities_value = Decimal(str(securities_value))
        existing.total_return_pct = Decimal(str(total_return_pct))

    db.commit()
    logger.info("Snapshot paper portfolio %s on %s: total=%.2f", portfolio_id, today.isoformat(), float(total_value))
    return {
        "date": today.isoformat(),
        "total_value": float(total_value),
        "cash_balance": float(cash_balance),
        "securities_value": float(securities_value),
        "total_return_pct": total_return_pct,
        "stale_quotes": stale_quotes,
    }


def compute_metrics(
    db: Session,
    portfolio_id: str,
    periods_per_year: int = quant_metrics.CALENDAR_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Compute Sharpe and max_drawdown from snapshot history and update latest snapshot.

    Annualisation convention: **calendar days** (365 periods/year) — paper
    snapshots are taken daily including weekends, so per-period returns are
    calendar-day returns; annualising them at 252 would overstate every
    ratio. See the convention table in
    ``docs/adr/0003-quant-metrics-conventions.md`` (decision 4);
    ``periods_per_year`` makes that cadence an explicit parameter while the
    default preserves the historical behaviour exactly.

    Raises:
        ValueError: If ``periods_per_year`` is not positive (validated before
            any database access).
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year!r}")

    snapshots = get_snapshots(db, portfolio_id, days=365)
    if len(snapshots) < 20:
        return {"updated": False, "reason": "insufficient snapshot history", "samples": len(snapshots)}

    values = [float(s.total_value) for s in snapshots]
    returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values)) if values[i - 1] != 0]
    if len(returns) < 2:
        return {"updated": False, "reason": "insufficient returns", "samples": len(returns)}

    # Paper snapshots are taken daily including weekends (calendar cadence),
    # unlike market-data-driven metrics which use TRADING_DAYS_PER_YEAR.
    report = quant_metrics.full_risk_report(
        returns,
        risk_free=get_risk_free_rate(db),
        periods_per_year=periods_per_year,
    )
    sharpe = report.get("sharpe")
    max_dd = report.get("drawdown", {}).get("max_drawdown")

    latest = snapshots[-1]
    latest.sharpe = sharpe
    latest.max_drawdown = max_dd

    upsert_metrics_snapshot(
        db,
        portfolio_id=portfolio_id,
        as_of=latest.date,
        context=METRICS_CONTEXT,
        values={
            "sharpe": sharpe,
            "sortino": report.get("sortino"),
            "calmar": report.get("calmar"),
            "cvar_95": report.get("historical_cvar"),
            "max_drawdown": max_dd,
            "volatility": report.get("annualised_volatility"),
        },
    )
    db.commit()

    return {"updated": True, "sharpe": sharpe, "max_drawdown": max_dd, "samples": len(returns)}


def execute_trade(
    db: Session,
    portfolio_id: str,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    confidence: float | None = None,
    rationale: str | None = None,
    fee: float = 0.0,
) -> dict[str, Any]:
    """Execute a simulated buy or sell trade in the paper portfolio.

    ``price`` is per unit **in EUR** (see :func:`paper_quote_eur`); the book
    holds EUR only.

    Args:
        fee: Commission for this order. A buy must cover ``value + fee`` in
            cash; a sell nets ``value - fee``. Defaults to 0 so manual trades
            and existing callers are unaffected — the advisor cycle passes a
            real figure from ``services/advisor/costs.py``.
    """
    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if price <= 0:
        raise ValueError("price must be positive")
    if fee < 0:
        raise ValueError("fee must not be negative")
    qty = Decimal(str(quantity))
    trade_price = Decimal(str(price))
    value = qty * trade_price
    trade_fee_amount = Decimal(str(fee))

    if side == "buy":
        cash = _current_cash_balance(portfolio, db)
        required = value + trade_fee_amount
        if cash < required:
            raise ValueError(f"Insufficient cash: {float(cash):.2f} < {float(required):.2f}")

        holding = (
            db.query(PaperHolding)
            .filter(PaperHolding.portfolio_id == portfolio_id, PaperHolding.ticker == ticker.upper())
            .first()
        )
        if holding is None:
            holding = PaperHolding(
                portfolio_id=portfolio_id,
                ticker=ticker.upper(),
                name=resolve_instrument_name(db, ticker.upper()),
                asset_type=resolve_paper_asset_type(db, ticker.upper()),
                quantity=qty,
                avg_buy_price=trade_price,
                currency=portfolio.currency,
            )
            db.add(holding)
            db.flush()
        else:
            total_cost = (holding.quantity * holding.avg_buy_price) + (qty * trade_price)
            holding.quantity += qty
            holding.avg_buy_price = total_cost / holding.quantity if holding.quantity > 0 else Decimal("0")
    else:  # sell
        holding = (
            db.query(PaperHolding)
            .filter(PaperHolding.portfolio_id == portfolio_id, PaperHolding.ticker == ticker.upper())
            .first()
        )
        if holding is None or holding.quantity < qty:
            raise ValueError(f"Insufficient holdings: {float(holding.quantity) if holding else 0} < {float(qty)}")
        if trade_fee_amount >= value:
            # Proceeds would not cover the commission — a trade that only
            # destroys cash. The advisor's minimum-ticket rule normally keeps
            # orders far above this, so reject rather than book negative cash.
            raise ValueError(
                f"Fee {float(trade_fee_amount):.2f} exceeds sale proceeds {float(value):.2f}"
            )
        holding.quantity -= qty
        if holding.quantity <= 0:
            db.delete(holding)
            holding = None

    trade = PaperTrade(
        portfolio_id=portfolio_id,
        holding_id=holding.id if holding else None,
        ticker=ticker.upper(),
        side=side,
        quantity=qty,
        price=trade_price,
        value=value,
        fee=trade_fee_amount,
        confidence=confidence,
        rationale=rationale,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    return {
        "id": trade.id,
        "ticker": trade.ticker,
        "side": trade.side,
        "quantity": float(trade.quantity),
        "price": float(trade.price),
        "value": float(trade.value),
        "fee": float(trade.fee or 0),
        "confidence": trade.confidence,
        "rationale": trade.rationale,
    }
