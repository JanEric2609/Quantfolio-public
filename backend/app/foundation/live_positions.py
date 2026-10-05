"""Every synced broker position, whatever the broker: DKB (``dkb_positions``)
plus the generic ``broker_positions`` table (Scalable Capital, …).

Readers that value the real book use ``live_positions`` (one row per depot)
or ``combined_positions`` (one row per instrument, depots summed) and count
manual holdings with ``manual_holdings_filter``; none of them needs to know
which brokers exist. Adding a broker means writing ``BrokerPosition`` rows,
nothing here. Only the DKB sync, its repair tools and this module read
``DkbPosition`` directly (``tests/test_live_positions_readers.py`` keeps that
list from growing).

Reconciliation with hand-entered holdings: a broker position whose ISIN is
also a manual ``Holding`` is left out until the owner decides (Scalable →
Accounts → "Review"), so a Scalable position the owner already typed in by
hand is never counted twice. The decision per ISIN lives in the broker's depot
``ConnectedAccount.raw_json["reconcile"]``: ``"replace"`` deletes the manual
row (after which the broker position counts), ``"keep_both"`` counts both
(e.g. the same ETF also held at a third broker).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    BrokerPosition,
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    Holding,
    Portfolio,
)

# Holding.source values written by the synced-holdings mirror. Everything else
# (manual, csv imports, …) is a holding the owner maintains.
SYNCED_HOLDING_SOURCES: tuple[str, ...] = ("dkb_sync", "broker_sync")

# ConnectedAccount / BrokerPosition sources of brokers synced into the generic
# tables (DKB keeps its own dkb_* tables).
BROKER_SOURCES: tuple[str, ...] = ("scalable",)

BROKER_LABELS = {"dkb": "DKB", "scalable": "Scalable Capital"}

# A broker's security type (Scalable's GraphQL ``type``) -> Holding.asset_type.
# Only investment funds owe Vorabpauschale and get a Teilfreistellung; a share
# held as "etf" would be taxed as a fund.
_ASSET_TYPES = {
    "ETF": "etf", "FUND": "etf", "ELTIF": "etf",
    "STOCK": "stock", "EQ": "stock", "EQUITY": "stock", "SHARE": "stock",
    "BOND": "bond",
    "ETC": "etc", "ETN": "etc",
}


def asset_type_for(security_type: str | None) -> str | None:
    """Holding.asset_type for a broker's security type, or None when unknown."""
    return _ASSET_TYPES.get((security_type or "").strip().upper())


@dataclass(frozen=True)
class LivePosition:
    id: str
    source: str  # "dkb" | "scalable" | …
    account_id: str
    isin: str
    ticker: str | None
    name: str
    quantity: Decimal
    avg_buy_price: Decimal | None
    current_price: Decimal | None
    current_value: Decimal | None
    currency: str
    last_synced: datetime | None
    # The broker's own type (Scalable: ETF, STOCK, ...); DKB reports none.
    security_type: str | None = None

    @property
    def value(self) -> Decimal:
        """Market value in EUR as the broker reported it (price × qty when it reports none, or 0)."""
        if self.current_value is not None and Decimal(self.current_value) > 0:
            return Decimal(self.current_value)
        if self.current_price is not None:
            return Decimal(self.current_price) * Decimal(self.quantity)
        return Decimal(self.current_value or 0)

    @property
    def broker_label(self) -> str:
        return BROKER_LABELS.get(self.source, self.source.title())


@dataclass(frozen=True)
class CombinedPosition:
    """One instrument across every depot that holds it (keyed by ISIN)."""

    isin: str
    ticker: str | None
    name: str
    quantity: Decimal
    value: Decimal
    # Quantity-weighted over the depots that report a cost; None when none does.
    avg_buy_price: Decimal | None
    current_price: Decimal | None
    security_type: str | None
    depots: tuple[LivePosition, ...]

    @property
    def key(self) -> str:
        return self.isin or (self.ticker or "").upper()

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.source for p in self.depots))


def manual_holdings_filter() -> ColumnElement[bool]:
    return Holding.source.not_in(SYNCED_HOLDING_SOURCES)


def is_synced_holding(holding: Any) -> bool:
    return getattr(holding, "source", None) in SYNCED_HOLDING_SOURCES


def _manual_isins(db: Session, user_id: str) -> set[str]:
    rows = (
        db.query(Holding.isin)
        .join(Portfolio, Holding.portfolio_id == Portfolio.id)
        .filter(Portfolio.user_id == user_id, manual_holdings_filter(), Holding.isin.is_not(None))
        .all()
    )
    return {str(isin).upper() for (isin,) in rows if isin}


def reconcile_decisions(account: ConnectedAccount | None) -> dict[str, str]:
    if account is None:
        return {}
    try:
        raw = json.loads(account.raw_json or "{}")
    except ValueError:
        return {}
    decisions = raw.get("reconcile") if isinstance(raw, dict) else None
    return {str(k).upper(): str(v) for k, v in (decisions or {}).items()} if isinstance(decisions, dict) else {}


def broker_positions(db: Session, user_id: str, *, source: str | None = None) -> list[BrokerPosition]:
    query = db.query(BrokerPosition).filter(BrokerPosition.user_id == user_id)
    if source:
        query = query.filter(BrokerPosition.source == source)
    return query.order_by(BrokerPosition.source, BrokerPosition.isin).all()


def pending_reconciliation(db: Session, user_id: str) -> list[BrokerPosition]:
    """Broker positions held back because a manual holding has the same ISIN."""
    manual = _manual_isins(db, user_id)
    if not manual:
        return []
    decided = _decisions_by_account(db, user_id)
    return [
        p for p in broker_positions(db, user_id)
        if p.isin.upper() in manual and decided.get(p.connected_account_id, {}).get(p.isin.upper()) != "keep_both"
    ]


def _decisions_by_account(db: Session, user_id: str) -> dict[str, dict[str, str]]:
    accounts = (
        db.query(ConnectedAccount)
        .filter(ConnectedAccount.user_id == user_id, ConnectedAccount.account_type == "depot")
        .all()
    )
    return {a.id: reconcile_decisions(a) for a in accounts}


def live_positions(db: Session, user_id: str, *, include_unreconciled: bool = False) -> list[LivePosition]:
    """All synced positions of *user_id*, DKB first, then other brokers."""
    out: list[LivePosition] = []
    dkb_rows = (
        db.query(DkbPosition)
        .join(DkbAccount, DkbPosition.account_id == DkbAccount.id)
        .filter(DkbAccount.user_id == user_id)
        .all()
    )
    for p in dkb_rows:
        out.append(
            LivePosition(
                id=p.id, source="dkb", account_id=p.account_id, isin=(p.isin or "").upper(),
                ticker=p.ticker, name=p.name, quantity=Decimal(p.quantity or 0),
                avg_buy_price=p.avg_buy_price, current_price=p.current_price,
                current_value=p.current_value, currency="EUR", last_synced=p.last_synced,
            )
        )

    rows = broker_positions(db, user_id)
    if rows:
        manual = set() if include_unreconciled else _manual_isins(db, user_id)
        decided = _decisions_by_account(db, user_id) if manual else {}
        for p in rows:
            isin = p.isin.upper()
            if isin in manual and decided.get(p.connected_account_id, {}).get(isin) != "keep_both":
                continue
            out.append(
                LivePosition(
                    id=p.id, source=p.source, account_id=p.connected_account_id, isin=isin,
                    ticker=p.ticker, name=p.name, quantity=Decimal(p.quantity or 0),
                    avg_buy_price=p.avg_buy_price, current_price=p.current_price,
                    current_value=p.current_value, currency=p.currency or "EUR",
                    last_synced=p.last_synced, security_type=p.security_type,
                )
            )
    return out


def combined_positions(db: Session, user_id: str, *, include_unreconciled: bool = False) -> list[CombinedPosition]:
    """``live_positions`` summed per instrument (ISIN, else ticker).

    DKB comes first, so its ticker and name win when both brokers hold the
    ISIN (the listing the price backfill already follows). A reader that does
    not add manual holdings itself passes ``include_unreconciled=True``, or a
    broker position with an unreviewed hand-entered twin is lost entirely.
    """
    groups: dict[str, list[LivePosition]] = {}
    for pos in live_positions(db, user_id, include_unreconciled=include_unreconciled):
        key = pos.isin or (pos.ticker or "").upper()
        if key:
            groups.setdefault(key, []).append(pos)
    out: list[CombinedPosition] = []
    for depots in groups.values():
        first = depots[0]
        quantity = sum((p.quantity for p in depots), Decimal("0"))
        costed = [(p.quantity, Decimal(p.avg_buy_price)) for p in depots if p.avg_buy_price is not None]
        costed_qty = sum((qty for qty, _ in costed), Decimal("0"))
        avg = (
            sum((qty * price for qty, price in costed), Decimal("0")) / costed_qty
            if costed_qty
            else None
        )
        out.append(
            CombinedPosition(
                isin=first.isin,
                ticker=next((p.ticker for p in depots if p.ticker), None),
                name=next((p.name for p in depots if p.name), first.name),
                quantity=quantity,
                value=sum((p.value for p in depots), Decimal("0")),
                avg_buy_price=avg,
                current_price=next((p.current_price for p in depots if p.current_price is not None), None),
                security_type=next((p.security_type for p in depots if p.security_type), None),
                depots=tuple(depots),
            )
        )
    return out


def live_prices_by_isin(db: Session, user_id: str) -> dict[str, Decimal]:
    """ISIN -> the broker's last price for every synced position (DKB wins a tie)."""
    prices: dict[str, Decimal] = {}
    for pos in live_positions(db, user_id):
        if pos.isin and pos.current_price is not None:
            prices.setdefault(pos.isin, Decimal(pos.current_price))
    return prices


def set_position_ticker(db: Session, position: LivePosition, ticker: str) -> None:
    """Store a resolved ticker on the synced row behind *position* (no commit)."""
    model = DkbPosition if position.source == "dkb" else BrokerPosition
    row = db.get(model, position.id)
    if row is not None:
        row.ticker = ticker


def synced_cash(db: Session, user_id: str) -> tuple[Decimal, bool]:
    """Cash at every synced bank and broker, and whether any account is synced.

    Only non-depot accounts count: a depot's balance is the value of its
    securities, not spendable cash. DKB reads ``dkb_accounts``; other brokers
    read their mirrored ``connected_accounts`` (cash, Tagesgeld).
    """
    dkb = db.query(DkbAccount).filter(DkbAccount.user_id == user_id).all()
    brokers = (
        db.query(ConnectedAccount)
        .filter(ConnectedAccount.user_id == user_id, ConnectedAccount.source.in_(BROKER_SOURCES))
        .all()
    )
    total = sum((Decimal(a.balance or 0) for a in dkb if a.type != "depot"), Decimal("0"))
    total += sum((Decimal(a.balance or 0) for a in brokers if a.account_type != "depot"), Decimal("0"))
    return total, bool(dkb or brokers)


def live_tickers(db: Session, user_id: str | None = None) -> set[str]:
    """Tickers of every synced position (all users when *user_id* is None)."""
    dkb = db.query(DkbPosition.ticker).filter(DkbPosition.ticker.is_not(None), DkbPosition.ticker != "")
    broker = db.query(BrokerPosition.ticker).filter(BrokerPosition.ticker.is_not(None), BrokerPosition.ticker != "")
    if user_id is not None:
        dkb = dkb.join(DkbAccount, DkbPosition.account_id == DkbAccount.id).filter(DkbAccount.user_id == user_id)
        broker = broker.filter(BrokerPosition.user_id == user_id)
    return {t for (t,) in dkb.all() if t} | {t for (t,) in broker.all() if t}
