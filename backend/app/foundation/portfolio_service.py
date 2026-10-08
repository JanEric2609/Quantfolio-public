import json
import logging
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    ActivityLedgerEntry,
    Asset,
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    DkbTransaction,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    PriceCache,
)
from app.foundation.live_positions import (
    BROKER_LABELS,
    BROKER_SOURCES,
    SYNCED_HOLDING_SOURCES,
    asset_type_for,
    live_positions,
    manual_holdings_filter,
)
from app.foundation.portfolio.name_resolver import _is_real_name
from app.foundation.portfolio_utils import holding_market_value

logger = logging.getLogger(__name__)

_wealth_cache: dict[str, tuple[float, dict]] = {}
_WEALTH_CACHE_TTL = 30  # seconds

DKB_TRADABLE_ISINS = {
    "IE00B4L5Y983",  # iShares Core MSCI World UCITS ETF
    "IE00B5BMR087",  # iShares Core S&P 500 UCITS ETF
    "DE0008469008",  # DAX ETF proxy examples can be extended in settings later
}


def main_portfolio(db: Session, user_id: str) -> Portfolio:
    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
    if portfolio is None:
        portfolio = Portfolio(user_id=user_id, name="Main Portfolio", currency="EUR")
        db.add(portfolio)
        db.commit()
        db.refresh(portfolio)
    return portfolio


def broker_totals(db: Session, user_id: str) -> tuple[Decimal, Decimal, int]:
    """Securities value, cash and position count at brokers other than DKB.

    DKB's own numbers come from ``dkb_*`` tables in the callers; this adds
    every broker synced into ``broker_positions`` (Scalable Capital today) and
    the cash accounts it mirrored into ``connected_accounts``.
    """
    positions = [p for p in live_positions(db, user_id) if p.source != "dkb"]
    security = sum((p.value for p in positions), Decimal("0"))
    cash = sum(
        (
            Decimal(account.balance or 0)
            for account in db.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.source.in_(BROKER_SOURCES),
                ConnectedAccount.account_type != "depot",
            )
        ),
        Decimal("0"),
    )
    return security, cash, len(positions)


def record_book_positions(db: Session, user_id: str, on: date | None = None) -> int:
    """Store the day's positions at every synced broker (``BookPositionSnapshot``).

    Replaces the rows of the day, so a sync later in the day wins and a
    position sold since is dropped. Broker truth: positions also entered by
    hand are kept (``include_unreconciled``). Does not commit. Returns the
    number of rows written.
    """
    from app.foundation.live_positions import live_positions
    from app.foundation.models.entities import BookPositionSnapshot

    day = on or date.today()
    db.query(BookPositionSnapshot).filter(
        BookPositionSnapshot.user_id == user_id, BookPositionSnapshot.snapshot_date == day,
    ).delete(synchronize_session=False)
    rows = [
        BookPositionSnapshot(
            user_id=user_id, snapshot_date=day, source=pos.source, account_id=pos.account_id,
            isin=pos.isin, ticker=pos.ticker, name=pos.name or pos.isin, quantity=pos.quantity,
            current_price=pos.current_price, current_value=pos.current_value, currency=pos.currency or "EUR",
        )
        for pos in live_positions(db, user_id, include_unreconciled=True)
        if pos.isin and pos.quantity is not None
    ]
    db.add_all(rows)
    return len(rows)


def snapshot_book_positions(db: Session, user_id: str) -> dict[str, Any]:
    """Today's positions at every broker and the day's whole-book PortfolioSnapshot.

    Runs after every DKB and Scalable sync and daily at 20:00 UTC. Manual
    holdings are valued at market (cost only without a price), the same as
    ``sync_dkb_to_wealth_ledger``: both write the day's ``dkb_mirror`` row,
    which used to flip between cost and market depending on which ran last.
    """
    today = date.today()

    accounts = db.execute(
        select(DkbAccount.id).where(DkbAccount.user_id == user_id)
    ).scalars().all()
    positions = []
    if accounts:
        positions = db.execute(
            select(DkbPosition).where(DkbPosition.account_id.in_(accounts))
        ).scalars().all()
    snapshots_created = record_book_positions(db, user_id, today)
    if snapshots_created:
        logger.info("Stored %d book position snapshots for user %s on %s", snapshots_created, user_id, today)

    from app.foundation.portfolio_service import main_portfolio

    portfolio = main_portfolio(db, user_id)
    manual_holdings = db.query(Holding).filter(
        Holding.portfolio_id == portfolio.id,
        manual_holdings_filter(),
    ).all() if portfolio else []

    manual_value = sum((Decimal(str(holding_market_value(db, h))) for h in manual_holdings), Decimal("0"))

    dkb_security_value = Decimal("0")
    for pos in positions:
        if pos.current_value is not None:
            dkb_security_value += Decimal(str(pos.current_value))
        elif pos.current_price is not None:
            dkb_security_value += Decimal(str(pos.current_price)) * Decimal(str(pos.quantity))

    cash_value = Decimal("0")
    if accounts:
        account_balances = db.execute(
            select(DkbAccount.balance, DkbAccount.type).where(
                DkbAccount.id.in_(accounts)
            )
        ).all()
        for balance, acct_type in account_balances:
            if acct_type != "depot":
                cash_value += Decimal(str(balance))

    broker_security_value, broker_cash_value, broker_count = broker_totals(db, user_id)
    dkb_security_value += broker_security_value
    cash_value += broker_cash_value

    existing_portfolio_snapshot = (
        db.query(PortfolioSnapshot)
        .filter(
            PortfolioSnapshot.user_id == user_id,
            PortfolioSnapshot.date == today,
            PortfolioSnapshot.source == "dkb_mirror",
        )
        .one_or_none()
    )
    if existing_portfolio_snapshot is None:
        portfolio_snapshot = PortfolioSnapshot(
            user_id=user_id, date=today, source="dkb_mirror"
        )
        db.add(portfolio_snapshot)
    else:
        portfolio_snapshot = existing_portfolio_snapshot

    # Verification (risk, stress, alerts) reads snapshots by portfolio_id, so a
    # row without one is invisible to it.
    if portfolio is not None:
        portfolio_snapshot.portfolio_id = portfolio.id
    portfolio_snapshot.cash_value = cash_value
    portfolio_snapshot.security_value = dkb_security_value + manual_value
    portfolio_snapshot.total_value = cash_value + dkb_security_value + manual_value
    portfolio_snapshot.currency = "EUR"
    portfolio_snapshot.payload_json = json.dumps(
        {
            "dkb_positions": len(positions),
            "broker_positions": broker_count,
            "manual_holdings": len(manual_holdings),
            "manual_value": str(manual_value),
        },
        default=str,
    )

    db.commit()
    logger.info(
        "Created PortfolioSnapshot for user %s: total=%.2f (DKB=%.2f, manual=%.2f, cash=%.2f)",
        user_id,
        float(portfolio_snapshot.total_value),
        float(dkb_security_value),
        float(manual_value),
        float(cash_value),
    )

    return {
        "snapshots_created": snapshots_created,
        "positions_count": len(positions),
        "manual_holdings_count": len(manual_holdings),
        "portfolio_snapshot_created": True,
        "date": today.isoformat(),
    }


def sync_dkb_positions_to_holdings(db: Session, user_id: str) -> dict[str, int]:
    """Create or update Holding records from every synced broker position.

    Bridges DKB (``dkb_positions``) and the other brokers (``broker_positions``)
    into the Holding table, so every Holding-based reader (Quant Lab, drift,
    allocation) sees the real book. One row per ISIN: the same ETF held at DKB
    and Scalable is one holding with the summed quantity (owner decision
    2026-09-30); ``dkb_available`` says whether DKB holds part of it. The name
    is kept for its many callers.
    """
    from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker

    has_dkb = db.query(DkbAccount.id).filter(DkbAccount.user_id == user_id).first() is not None
    has_broker = db.query(ConnectedAccount.id).filter(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.source.in_(BROKER_SOURCES),
    ).first() is not None
    if not has_dkb and not has_broker:
        return {"created": 0, "updated": 0, "ticker_resolved": 0, "stale_removed": 0}

    positions = live_positions(db, user_id)
    portfolio = main_portfolio(db, user_id)
    existing = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    if not positions:
        stale = [h for h in existing if h.source in SYNCED_HOLDING_SOURCES]
        for stale_holding in stale:
            db.delete(stale_holding)
        db.commit()
        return {"created": 0, "updated": 0, "ticker_resolved": 0, "stale_removed": len(stale)}

    # Only index synced holdings here. Including manual holdings meant a synced
    # position sharing an ISIN with a manually-entered holding would overwrite the
    # manual row's quantity/cost/name below — silently clobbering user data. A
    # manual holding for the same ISIN is left untouched and stands for that
    # position: uq_holdings_portfolio_isin allows one row per ISIN, so adding a
    # synced row beside it raised IntegrityError and failed the whole sync.
    by_isin = {h.isin: h for h in existing if h.isin and h.source in SYNCED_HOLDING_SOURCES}
    manual_isins = {h.isin for h in existing if h.isin and h.source not in SYNCED_HOLDING_SOURCES}

    # Aggregate positions by ISIN: sum quantities, compute weighted avg_buy_price
    aggregated: dict[str, dict] = {}
    # Pre-fetch Asset records for type classification
    asset_isins = {pos.isin for pos in positions if pos.isin}
    asset_map: dict[str, Asset] = {}
    if asset_isins:
        for a in db.query(Asset).filter(Asset.isin.in_(list(asset_isins))).all():
            if a.isin:
                asset_map[a.isin] = a
    for pos in positions:
        if not pos.isin:
            continue  # skip positions without ISIN to avoid delete/recreate churn
        if pos.isin not in aggregated:
            existing_asset = asset_map.get(pos.isin)
            aggregated[pos.isin] = {
                "quantity": Decimal("0"),
                "total_cost": Decimal("0"),
                "avg_buy_unknown": False,
                "avg_buy_price": None,
                "name": pos.name,
                "asset_type": existing_asset.asset_type if existing_asset else "etf",
                "broker_asset_type": None,
                "sources": set(),
            }
        agg = aggregated[pos.isin]
        # The broker's own security type beats the Asset table and the "etf"
        # default: a share mirrored as "etf" was estimated a Vorabpauschale.
        broker_type = asset_type_for(pos.security_type)
        if broker_type and _broker_type_applies(broker_type, agg["asset_type"]):
            agg["asset_type"] = agg["broker_asset_type"] = broker_type
        agg["sources"].add(pos.source)
        agg["quantity"] += Decimal(str(pos.quantity or 0))
        if pos.avg_buy_price is not None:
            agg["total_cost"] += Decimal(str(pos.quantity or 0)) * Decimal(str(pos.avg_buy_price))
        else:
            agg["avg_buy_unknown"] = True
        if pos.name:
            agg["name"] = pos.name
    for agg in aggregated.values():
        if agg["quantity"] > 0 and not agg["avg_buy_unknown"]:
            agg["avg_buy_price"] = agg["total_cost"] / agg["quantity"]

    stale = [h for h in existing if h.source in SYNCED_HOLDING_SOURCES and h.isin and h.isin not in aggregated]
    for stale_holding in stale:
        db.delete(stale_holding)
    db.flush()
    stale_removed = len(stale)

    created = 0
    updated = 0
    for isin, agg in aggregated.items():
        holding = by_isin.get(isin)
        on_dkb = "dkb" in agg["sources"]
        source = "dkb_sync" if on_dkb else "broker_sync"
        if holding is None and isin in manual_isins:
            logger.info("Position sync: %s is held as a manual holding; not adding a synced row", isin)
            continue
        if holding is None:
            holding = Holding(
                portfolio_id=portfolio.id,
                isin=isin,
                ticker=None,
                name=agg["name"],
                asset_type=agg.get("asset_type", "etf"),
                quantity=agg["quantity"],
                avg_buy_price=agg["avg_buy_price"],
                currency="EUR",
                source=source,
                dkb_available=on_dkb,
            )
            db.add(holding)
            by_isin[isin] = holding
            created += 1
        else:
            holding.quantity = agg["quantity"]
            holding.avg_buy_price = agg["avg_buy_price"]
            holding.source = source
            matched_asset = asset_map.get(isin)
            if agg["broker_asset_type"] and _broker_type_applies(agg["broker_asset_type"], holding.asset_type):
                holding.asset_type = agg["broker_asset_type"]
            elif matched_asset is not None:
                holding.asset_type = matched_asset.asset_type
            if not _is_real_name(holding.name, holding.ticker or "", isin):
                holding.name = agg["name"]
            holding.dkb_available = on_dkb
            updated += 1

    db.flush()

    ticker_result = resolve_isin_to_ticker(db, user_id)
    # A synced holding follows its position's listing, read after the
    # resolver ran: it re-points positions to pinned listings. Filling only an
    # empty ticker left the prod holding for IE00B4L5Y983 on IWDA.L (the USD
    # London line yfinance picked before PR #273 pinned EUNL.DE), so every
    # holding-based reader priced the Xetra savings plan off the wrong venue.
    # A different listing for a synced holding is set with the
    # isin_ticker_overrides setting, which the resolver applies. DKB's listing
    # wins when both brokers hold the ISIN (it comes first).
    position_tickers: dict[str, str] = {}
    for pos in live_positions(db, user_id):
        if pos.isin and pos.ticker:
            position_tickers.setdefault(pos.isin, pos.ticker)
    for isin in aggregated:
        holding = by_isin.get(isin)
        ticker = position_tickers.get(isin)
        if holding and ticker and holding.ticker != ticker:
            holding.ticker = ticker

    db.commit()
    return {
        "created": created,
        "updated": updated,
        "ticker_resolved": ticker_result.get("resolved", 0),
        "stale_removed": stale_removed,
    }


# Kinds of fund the Asset table or a holding can know about that a broker
# reports only as a generic "ETF" (Scalable has no money-market type).
_FUND_KINDS = frozenset({"money_market", "bond"})


def _broker_type_applies(broker_type: str, current: str | None) -> bool:
    """Whether the broker's type replaces *current*: a generic "etf" never
    overwrites the more specific money-market or bond fund it already is."""
    return not (broker_type == "etf" and current in _FUND_KINDS)


def dkb_availability(isin: str | None) -> bool:
    return bool(isin and isin.upper() in DKB_TRADABLE_ISINS)


def allocation_by_asset_type(holdings: list[Holding]) -> dict[str, float]:
    if not holdings:
        return {}

    db = None
    try:
        db = Session.object_session(holdings[0])
    except Exception:
        pass

    latest_prices: dict[str, Decimal] = {}
    dkb_values: dict[str, Decimal] = {}
    if db is not None:
        tickers = {h.ticker for h in holdings if h.ticker}
        isins = {h.isin for h in holdings if h.isin}

        if tickers:
            rows = (
                db.query(PriceCache.ticker, PriceCache.date, PriceCache.close)
                .filter(
                    PriceCache.ticker.in_(list(tickers)),
                    PriceCache.stale.is_(False),
                )
                .order_by(PriceCache.ticker, PriceCache.date.desc())
                .all()
            )
            seen: set[str] = set()
            for ticker, _date, close in rows:
                if ticker not in seen:
                    latest_prices[ticker] = Decimal(close)
                    seen.add(ticker)

        # Only the holdings' owner's synced positions value them: another user
        # holding the same ISIN must never move these percentages.
        owner = db.get(Portfolio, holdings[0].portfolio_id)
        owner_id = owner.user_id if owner is not None else None
        if isins and owner_id is not None:
            # Every broker's depot that holds the ISIN adds its value; a
            # position held back for reconciliation is the manual row's to value.
            for pos in live_positions(db, owner_id):
                if pos.isin in isins and pos.current_value is not None:
                    dkb_values[pos.isin] = dkb_values.get(pos.isin, Decimal("0")) + Decimal(pos.current_value)

    totals: dict[str, Decimal] = defaultdict(Decimal)
    grand_total = Decimal("0")
    for holding in holdings:
        price = Decimal("0")

        if holding.ticker and holding.ticker in latest_prices:
            price = latest_prices[holding.ticker]
        elif holding.isin and holding.isin in dkb_values:
            if holding.quantity and Decimal(holding.quantity) > 0:
                price = dkb_values[holding.isin] / Decimal(holding.quantity)
        else:
            price = Decimal(holding.avg_buy_price or 0)

        if price <= 0:
            continue

        value = Decimal(holding.quantity) * price
        totals[holding.asset_type] += value
        grand_total += value

    if grand_total == 0:
        return {}
    return {asset_type: float((value / grand_total) * 100) for asset_type, value in totals.items()}


def portfolio_performance(holdings: list[Holding]) -> dict:
    """Invested value for the holdings-based portfolio.

    A time-weighted return series is not yet wired for this portfolio (it needs a
    daily snapshot history + cashflow feed, which the research performance ledger
    provides separately). Rather than fabricate zeroed `twr`/`day_change`/period
    values — indistinguishable from a real flat-return portfolio — this returns
    the genuine invested value plus an explicit `not_implemented` marker so the
    client never mistakes "not computed" for "0% return" (issue #134).
    """
    known_holdings = [h for h in holdings if h.avg_buy_price is not None]
    unknown_count = len(holdings) - len(known_holdings)
    invested = sum(
        (Decimal(h.quantity) * h.avg_buy_price for h in known_holdings if h.avg_buy_price is not None),
        Decimal("0"),
    )
    return {
        "currency": "EUR",
        "total_value": float(invested),
        "not_implemented": True,
        "unknown_cost_basis_holdings": unknown_count,
        "detail": (
            "Time-weighted return and period changes are not yet computed for this "
            "portfolio. See the research performance ledger for TWR."
        ),
    }


def sync_dkb_to_wealth_ledger(db: Session, user_id: str) -> dict[str, int]:
    accounts = db.query(DkbAccount).filter(DkbAccount.user_id == user_id).all()
    connected_by_dkb_id: dict[str, ConnectedAccount] = {}
    for account in accounts:
        external_id = account.iban or account.id
        try:
            connected = (
                db.query(ConnectedAccount)
                .filter(
                    ConnectedAccount.user_id == user_id,
                    ConnectedAccount.source == "dkb",
                    ConnectedAccount.external_id == external_id,
                )
                .one_or_none()
            )
        except MultipleResultsFound:
            logger.error(
                "DKB sync: multiple ConnectedAccount rows for user_id=%s external_id=...%s",
                user_id, str(external_id)[-4:],
            )
            raise
        if connected is None:
            connected = ConnectedAccount(
                user_id=user_id,
                source="dkb",
                external_id=external_id,
                name=f"DKB {account.type.upper()}",
                institution="DKB",
            )
            db.add(connected)
            db.flush()
        connected.account_type = account.type
        connected.iban = account.iban
        connected.currency = account.currency
        connected.balance = account.balance
        connected.last_synced = account.last_synced
        connected.raw_json = json.dumps({"dkb_account_id": account.id}, default=str)
        connected_by_dkb_id[account.id] = connected

    ledger_created = 0
    transactions = (
        db.query(DkbTransaction)
        .filter(DkbTransaction.account_id.in_([account.id for account in accounts]))
        .all()
        if accounts
        else []
    )
    seen_ledger_hashes: set[str] = set()
    for tx in transactions:
        if tx.amount is None or Decimal(tx.amount) == 0:
            continue
        if not tx.reference or not tx.reference.strip():
            continue
        account = connected_by_dkb_id.get(tx.account_id)
        if tx.dedupe_hash in seen_ledger_hashes:
            continue
        try:
            exists = (
                db.query(ActivityLedgerEntry)
                .filter(
                    ActivityLedgerEntry.user_id == user_id,
                    ActivityLedgerEntry.source == "dkb",
                    ActivityLedgerEntry.dedupe_hash == tx.dedupe_hash,
                )
                .one_or_none()
            )
        except MultipleResultsFound:
            logger.error(
                "DKB sync: multiple ActivityLedgerEntry rows for user_id=%s dedupe_hash=%s",
                user_id, tx.dedupe_hash[:20],
            )
            raise
        if exists:
            seen_ledger_hashes.add(tx.dedupe_hash)
            continue
        seen_ledger_hashes.add(tx.dedupe_hash)
        db.add(
            ActivityLedgerEntry(
                user_id=user_id,
                connected_account_id=account.id if account else None,
                source="dkb",
                external_id=tx.id,
                dedupe_hash=tx.dedupe_hash,
                activity_type="cashflow",
                date=tx.date,
                amount=tx.amount,
                currency=tx.currency,
                description=tx.reference[:300],
                review_state="trusted",
                raw_json=json.dumps({"dkb_transaction_id": tx.id, "account_id": tx.account_id}, default=str),
            )
        )
        ledger_created += 1

    portfolio = main_portfolio(db, user_id)
    manual_holdings = db.query(Holding).filter(
        Holding.portfolio_id == portfolio.id,
        manual_holdings_filter(),
    ).all()
    # Value manual holdings at market (latest cached price, FX-adjusted) rather
    # than cost basis; holding_market_value falls back to cost when no price exists.
    manual_value = sum((Decimal(str(holding_market_value(db, holding))) for holding in manual_holdings), Decimal("0"))
    cash_value = sum((Decimal(account.balance) for account in accounts if account.type != "depot"), Decimal("0"))
    security_value = Decimal("0")
    for position in (
        db.query(DkbPosition)
        .filter(DkbPosition.account_id.in_([account.id for account in accounts]))
        .all()
        if accounts
        else []
    ):
        if position.current_value is not None:
            security_value += Decimal(position.current_value)
        elif position.current_price is not None:
            security_value += Decimal(position.current_price) * Decimal(position.quantity)

    broker_security_value, broker_cash_value, broker_count = broker_totals(db, user_id)
    security_value += broker_security_value
    cash_value += broker_cash_value

    snapshot_date = date.today()
    current_total = cash_value + security_value + manual_value

    # Compute total_return_pct from the earliest known snapshot for this user
    earliest_snap = (
        db.query(PortfolioSnapshot)
        .filter(
            PortfolioSnapshot.user_id == user_id,
            PortfolioSnapshot.source == "dkb_mirror",
            PortfolioSnapshot.total_value > 0,
        )
        .order_by(PortfolioSnapshot.date.asc())
        .first()
    )
    if earliest_snap and earliest_snap.total_value and earliest_snap.total_value > 0:
        base_value = Decimal(str(earliest_snap.total_value))
        total_return_pct = (current_total - base_value) / base_value if base_value else Decimal("0")
    else:
        total_return_pct = Decimal("0")

    positions_count = (
        db.query(DkbPosition)
        .filter(DkbPosition.account_id.in_([account.id for account in accounts]))
        .count()
        if accounts
        else 0
    )
    payload_json = json.dumps(
        {
            "accounts": len(accounts),
            "positions": positions_count + broker_count,
            "manual_holdings": len(manual_holdings),
            "total_return_pct": float(total_return_pct),
        }
    )

    def _apply(snap: PortfolioSnapshot) -> None:
        # Verification (risk, stress, alerts) reads snapshots by portfolio_id.
        snap.portfolio_id = portfolio.id
        snap.cash_value = cash_value
        snap.security_value = security_value + manual_value
        snap.total_value = current_total
        snap.currency = "EUR"
        snap.total_return_pct = total_return_pct
        snap.payload_json = payload_json

    def _fetch_existing() -> PortfolioSnapshot | None:
        try:
            return (
                db.query(PortfolioSnapshot)
                .filter(
                    PortfolioSnapshot.user_id == user_id,
                    PortfolioSnapshot.date == snapshot_date,
                    PortfolioSnapshot.source == "dkb_mirror",
                )
                .one_or_none()
            )
        except MultipleResultsFound:
            logger.error(
                "DKB sync: multiple PortfolioSnapshot rows for user_id=%s date=%s source=dkb_mirror",
                user_id, snapshot_date,
            )
            raise

    snapshot = _fetch_existing()
    if snapshot is None:
        snapshot = PortfolioSnapshot(user_id=user_id, date=snapshot_date, source="dkb_mirror")
        db.add(snapshot)
    _apply(snapshot)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request created today's dkb_mirror snapshot between our
        # existence check and this commit (uq_portfolio_snapshots_user_date_source).
        # This path runs on read endpoints (GET /snapshots, /activity), so it must
        # never 500: roll back, re-fetch the winning row, update it, and commit.
        db.rollback()
        snapshot = _fetch_existing()
        if snapshot is None:
            raise
        _apply(snapshot)
        db.commit()
    sync_dkb_positions_to_holdings(db, user_id)
    return {"accounts": len(accounts), "ledger_created": ledger_created}


def invalidate_wealth_cache(user_id: str) -> None:
    """Drop *user_id*'s cached wealth summary (this process only) after a sync wrote new data."""
    _wealth_cache.pop(user_id, None)


def wealth_summary(db: Session, user_id: str) -> dict:
    """Return wealth summary with short TTL cache to avoid redundant reconciliation.

    The cache is keyed by user_id and expires after 30 seconds.
    DKB sync calls sync_dkb_to_wealth_ledger directly, bypassing this cache.
    """
    now = time.time()
    cached = _wealth_cache.get(user_id)
    if cached and (now - cached[0]) < _WEALTH_CACHE_TTL:
        return cached[1]

    sync_dkb_to_wealth_ledger(db, user_id)
    portfolio = main_portfolio(db, user_id)
    holdings = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    manual_holdings = [h for h in holdings if h.source not in SYNCED_HOLDING_SOURCES]
    # The bridged holding carries the asset type (the broker's own, else the
    # Asset table's); a synced position has none of its own.
    synced_asset_types = {
        h.isin: h.asset_type for h in holdings if h.source in SYNCED_HOLDING_SOURCES and h.isin and h.asset_type
    }
    manual_value = sum((Decimal(str(holding_market_value(db, h))) for h in manual_holdings), Decimal("0"))
    accounts = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user_id).order_by(ConnectedAccount.source, ConnectedAccount.name).all()
    cash_value = sum((Decimal(account.balance) for account in accounts if account.account_type != "depot"), Decimal("0"))
    dkb_security_value = Decimal("0")
    broker_security_value = Decimal("0")
    positions = []
    for position in live_positions(db, user_id):
        value = position.value
        if position.source == "dkb":
            dkb_security_value += value
        else:
            broker_security_value += value
        positions.append(
            {
                "id": position.id,
                "source": position.source,
                "broker": position.broker_label,
                "account_id": position.account_id,
                "isin": position.isin,
                "symbol": position.ticker,
                "name": position.name,
                "asset_type": synced_asset_types.get(position.isin),
                "quantity": float(position.quantity),
                "current_price": float(position.current_price) if position.current_price is not None else None,
                "current_value": float(value),
                "last_synced": position.last_synced,
            }
        )
    for holding in manual_holdings:
        value = Decimal(str(holding_market_value(db, holding)))
        positions.append(
            {
                "id": holding.id,
                "source": holding.source,
                "isin": holding.isin,
                "symbol": holding.ticker,
                "name": holding.name,
                "asset_type": holding.asset_type,
                "quantity": float(holding.quantity),
                "current_value": float(value),
                "currency": holding.currency,
            }
        )

    total_value = cash_value + dkb_security_value + broker_security_value + manual_value
    thirty_days_ago = date.today() - timedelta(days=30)
    ledger_rows = (
        db.query(ActivityLedgerEntry)
        .filter(
            ActivityLedgerEntry.user_id == user_id,
            ActivityLedgerEntry.date >= thirty_days_ago,
            # Only money moving in or out: imported buys/sells carry an
            # unsigned amount and would otherwise read as income.
            ActivityLedgerEntry.activity_type == "cashflow",
            # A broker's first-sync opening balance is a contribution for
            # TWR only; it is not money that came in this month.
            or_(ActivityLedgerEntry.external_id.is_(None), ~ActivityLedgerEntry.external_id.startswith("opening:")),
        )
        .all()
    )
    income = sum((Decimal(row.amount) for row in ledger_rows if Decimal(row.amount) > 0), Decimal("0"))
    outflow = sum((abs(Decimal(row.amount)) for row in ledger_rows if Decimal(row.amount) < 0), Decimal("0"))
    latest_snapshot = (
        db.query(PortfolioSnapshot)
        .filter(PortfolioSnapshot.user_id == user_id)
        .order_by(PortfolioSnapshot.date.desc(), PortfolioSnapshot.created_at.desc())
        .first()
    )
    # The combined book split by where it is held: securities and cash per
    # broker, with that broker's last sync (Home shows one row per broker).
    split: dict[str, dict[str, Any]] = {}

    def _row(source: str) -> dict[str, Any]:
        label = BROKER_LABELS.get(source, "Entered by hand" if source == "manual" else source.title())
        return split.setdefault(source, {"source": source, "label": label, "securities": Decimal("0"),
                                         "cash": Decimal("0"), "last_synced": None})

    for position in positions:
        source = position["source"] if position["source"] in BROKER_LABELS else "manual"
        _row(source)["securities"] += Decimal(str(position["current_value"]))
    for account in accounts:
        row = _row(account.source if account.source in BROKER_LABELS else "manual")
        if account.account_type != "depot":
            row["cash"] += Decimal(account.balance)
        if account.last_synced and (row["last_synced"] is None or account.last_synced > row["last_synced"]):
            row["last_synced"] = account.last_synced
    by_broker = [
        {**row, "securities": float(row["securities"]), "cash": float(row["cash"]),
         "total": float(row["securities"] + row["cash"])}
        for row in sorted(split.values(), key=lambda r: (r["source"] == "manual", r["label"]))
    ]

    result = {
        "currency": "EUR",
        "total_value": float(total_value),
        "by_broker": by_broker,
        "cash_value": float(cash_value),
        "security_value": float(dkb_security_value + broker_security_value + manual_value),
        "manual_value": float(manual_value),
        "dkb_security_value": float(dkb_security_value),
        "broker_security_value": float(broker_security_value),
        "accounts": [
            {
                "id": account.id,
                "source": account.source,
                "name": account.name,
                "institution": account.institution,
                "type": account.account_type,
                "iban": account.iban,
                "balance": float(account.balance),
                "currency": account.currency,
                "last_synced": account.last_synced,
            }
            for account in accounts
        ],
        "positions": positions,
        "cashflow_30d": {"income": float(income), "outflow": float(outflow), "net": float(income - outflow)},
        "last_snapshot": latest_snapshot.date.isoformat() if latest_snapshot else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _wealth_cache[user_id] = (time.time(), result)
    return result
