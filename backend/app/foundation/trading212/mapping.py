"""Pure Trading 212 JSON → records (no DB, no network). Decimal throughout.

Each mapper skips a malformed item (logged) rather than raising: the API is in
beta, and one odd row must never cost the rest. The ISIN is the key; T212's
own ticker (``CMG_US_EQ``) is kept as ``t212_ticker`` and never used as an
app ticker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "trading212"
LOT_SOURCE = "trading212_sync"
LABEL = "Trading 212"

# Fill types that move shares without a trade at a price (splits, spin-offs, …).
TRADE_FILL = "TRADE"
# Order statuses whose fill is booked.
FILLED_STATUSES = frozenset({"FILLED", "PARTIALLY_FILLED"})
# Cash transactions that cross the account boundary (TWR/MWR cash flows).
EXTERNAL_CASH = frozenset({"DEPOSIT", "WITHDRAW", "TRANSFER"})
INTEREST_TYPES = frozenset({"INTEREST_ON_FREE_CASH", "LENDING_INTEREST"})


def dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def when(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _isin(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if len(text) == 12 and text[:2].isalpha() and text.isalnum() else None


def _ccy(value: Any, default: str = "EUR") -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 3 and text.isalpha() else default


@dataclass(frozen=True)
class AccountSummary:
    account_id: str | None
    currency: str
    total_value: Decimal | None
    available_to_trade: Decimal | None
    in_pies: Decimal | None
    reserved_for_orders: Decimal | None
    investments_value: Decimal | None
    investments_cost: Decimal | None

    @property
    def free_cash(self) -> Decimal | None:
        """Cash outside pies: available plus what open orders hold back."""
        if self.available_to_trade is None and self.reserved_for_orders is None:
            return None
        return (self.available_to_trade or Decimal("0")) + (self.reserved_for_orders or Decimal("0"))


def map_summary(body: Any) -> AccountSummary:
    body = _obj(body)
    cash = _obj(body.get("cash"))
    inv = _obj(body.get("investments"))
    return AccountSummary(
        account_id=str(body["id"]) if body.get("id") is not None else None,
        currency=_ccy(body.get("currency")),
        total_value=dec(body.get("totalValue")),
        available_to_trade=dec(cash.get("availableToTrade")),
        in_pies=dec(cash.get("inPies")),
        reserved_for_orders=dec(cash.get("reservedForOrders")),
        investments_value=dec(inv.get("currentValue")),
        investments_cost=dec(inv.get("totalCost")),
    )


@dataclass(frozen=True)
class PositionRecord:
    isin: str
    t212_ticker: str | None
    name: str
    currency: str  # the instrument's
    quantity: Decimal
    quantity_in_pies: Decimal | None
    avg_buy_price: Decimal | None  # in the instrument's currency
    current_price: Decimal | None  # in the instrument's currency
    current_value: Decimal | None  # in the account currency (EUR)
    total_cost: Decimal | None  # in the account currency


def map_positions(body: Any) -> list[PositionRecord]:
    items = body if isinstance(body, list) else (body.get("items") if isinstance(body, dict) else None)
    out: list[PositionRecord] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        inst = _obj(item.get("instrument"))
        isin = _isin(inst.get("isin"))
        qty = dec(item.get("quantity"))
        if isin is None or qty is None or qty <= 0:
            logger.info("Trading 212 position skipped: no ISIN or quantity")
            continue
        wallet = _obj(item.get("walletImpact"))
        out.append(PositionRecord(
            isin=isin,
            t212_ticker=str(inst.get("ticker") or "")[:32] or None,
            name=str(inst.get("name") or isin)[:200],
            currency=_ccy(inst.get("currency")),
            quantity=qty,
            quantity_in_pies=dec(item.get("quantityInPies")),
            avg_buy_price=dec(item.get("averagePricePaid")),
            current_price=dec(item.get("currentPrice")),
            current_value=dec(wallet.get("currentValue")),
            total_cost=dec(wallet.get("totalCost")),
        ))
    return out


@dataclass(frozen=True)
class FillRecord:
    """One executed order (or a share movement such as a split)."""

    id: str
    order_id: str | None
    isin: str | None
    t212_ticker: str | None
    name: str
    side: str  # BUY | SELL
    fill_type: str
    filled_at: datetime | None
    quantity: Decimal  # always positive
    price: Decimal | None  # instrument currency
    instrument_currency: str
    net_value: Decimal | None  # account currency, unsigned
    fx_rate: Decimal | None
    fees: Decimal  # account currency
    account_currency: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.fill_type == TRADE_FILL

    def price_in_account_currency(self) -> Decimal | None:
        """The fill price in EUR, from the fill's own FX rate.

        The API does not say which way ``fxRate`` points, so the reading
        whose price × quantity lands nearer the booked value wins.
        """
        if self.price is None:
            return None
        if self.instrument_currency == self.account_currency or not self.fx_rate:
            return self.price
        a, b = self.price * self.fx_rate, self.price / self.fx_rate
        if self.net_value is None or not self.quantity:
            return b
        target = abs(self.net_value)
        return a if abs(a * self.quantity - target) <= abs(b * self.quantity - target) else b


def map_orders(items: list[dict[str, Any]]) -> list[FillRecord]:
    out: list[FillRecord] = []
    for item in items:
        order = _obj(item.get("order"))
        fill = item.get("fill") if isinstance(item.get("fill"), dict) else None
        if fill is None:
            continue  # cancelled or rejected: nothing moved
        status = str(order.get("status") or "").upper()
        if status and status not in FILLED_STATUSES:
            continue
        fill_id = fill.get("id")
        qty = dec(fill.get("quantity"))
        side = str(order.get("side") or "").upper()
        if fill_id is None or qty is None or qty == 0:
            logger.info("Trading 212 fill skipped: no id or quantity")
            continue
        if side not in {"BUY", "SELL"}:
            side = "BUY" if qty > 0 else "SELL"
        inst = _obj(order.get("instrument"))
        wallet = _obj(fill.get("walletImpact"))
        account_ccy = _ccy(wallet.get("currency"))
        fees = Decimal("0")
        for tax in wallet.get("taxes") or []:
            if isinstance(tax, dict):
                amount = dec(tax.get("quantity"))
                if amount is not None:
                    fees += abs(amount)
        net = dec(wallet.get("netValue"))
        out.append(FillRecord(
            id=str(fill_id),
            order_id=str(order["id"]) if order.get("id") is not None else None,
            isin=_isin(inst.get("isin")),
            t212_ticker=str(inst.get("ticker") or order.get("ticker") or "")[:32] or None,
            name=str(inst.get("name") or inst.get("isin") or order.get("ticker") or "")[:200],
            side=side,
            fill_type=str(fill.get("type") or TRADE_FILL).upper(),
            filled_at=when(fill.get("filledAt")),
            quantity=abs(qty),
            price=dec(fill.get("price")),
            instrument_currency=_ccy(inst.get("currency") or order.get("currency"), account_ccy),
            net_value=abs(net) if net is not None else None,
            fx_rate=dec(wallet.get("fxRate")),
            fees=fees,
            account_currency=account_ccy,
            raw={"order": {k: order.get(k) for k in ("id", "status", "side", "type", "initiatedFrom", "ticker")},
                 "fill": {k: fill.get(k) for k in ("id", "type", "filledAt", "price", "quantity")},
                 "wallet": {k: wallet.get(k) for k in ("netValue", "fxRate", "realisedProfitLoss", "currency")},
                 "taxes": wallet.get("taxes") or []},
        ))
    return out


@dataclass(frozen=True)
class DividendRecord:
    reference: str
    isin: str | None
    t212_ticker: str | None
    name: str
    paid_on: datetime | None
    amount_eur: Decimal  # what was credited, in EUR
    gross_per_share: Decimal | None  # in ticker_currency
    quantity: Decimal | None
    ticker_currency: str
    dividend_type: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def gross_native(self) -> Decimal | None:
        if self.gross_per_share is None or self.quantity is None:
            return None
        return self.gross_per_share * self.quantity


def map_dividends(items: list[dict[str, Any]]) -> list[DividendRecord]:
    out: list[DividendRecord] = []
    for item in items:
        ref = item.get("reference")
        amount = dec(item.get("amountInEuro"))
        if amount is None and _ccy(item.get("currency"), "") == "EUR":
            amount = dec(item.get("amount"))
        if not ref or amount is None:
            logger.info("Trading 212 dividend skipped: no reference or EUR amount")
            continue
        inst = _obj(item.get("instrument"))
        out.append(DividendRecord(
            reference=str(ref)[:80],
            isin=_isin(inst.get("isin")),
            t212_ticker=str(inst.get("ticker") or item.get("ticker") or "")[:32] or None,
            name=str(inst.get("name") or item.get("ticker") or "")[:200],
            paid_on=when(item.get("paidOn")),
            amount_eur=amount,
            gross_per_share=dec(item.get("grossAmountPerShare")),
            quantity=dec(item.get("quantity")),
            ticker_currency=_ccy(item.get("tickerCurrency") or inst.get("currency")),
            dividend_type=str(item.get("type") or "")[:60],
            raw={k: item.get(k) for k in ("reference", "amount", "amountInEuro", "currency", "grossAmountPerShare",
                                          "quantity", "paidOn", "type", "ticker", "tickerCurrency")},
        ))
    return out


@dataclass(frozen=True)
class CashRecord:
    reference: str
    kind: str  # DEPOSIT | WITHDRAW | FEE | TRANSFER | INTEREST_ON_FREE_CASH | LENDING_INTEREST
    amount: Decimal  # signed as T212 books it
    currency: str
    occurred_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


def map_transactions(items: list[dict[str, Any]]) -> list[CashRecord]:
    out: list[CashRecord] = []
    for item in items:
        ref = item.get("reference")
        amount = dec(item.get("amount"))
        if not ref or amount is None:
            logger.info("Trading 212 transaction skipped: no reference or amount")
            continue
        out.append(CashRecord(
            reference=str(ref)[:80],
            kind=str(item.get("type") or "").upper()[:40],
            amount=amount,
            currency=_ccy(item.get("currency")),
            occurred_at=when(item.get("dateTime")),
            raw={k: item.get(k) for k in ("reference", "type", "amount", "currency", "dateTime")},
        ))
    return out


def cash_activity_type(kind: str) -> str:
    """Wealth-ledger type: only deposits, withdrawals and transfers cross the boundary."""
    if kind in EXTERNAL_CASH:
        return "cashflow"
    if kind in INTEREST_TYPES:
        return "interest"
    if kind == "FEE":
        return "fee"
    return "other"


@dataclass(frozen=True)
class PieRecord:
    id: str
    name: str
    status: str | None
    cash: Decimal | None
    invested: Decimal | None
    value: Decimal | None
    result: Decimal | None
    result_pct: Decimal | None
    dividends_gained: Decimal | None
    dividends_reinvested: Decimal | None
    dividends_in_cash: Decimal | None
    dividend_action: str | None
    goal: Decimal | None
    end_date: date | None
    instruments: list[dict[str, Any]] = field(default_factory=list)


def map_pie_list(body: Any) -> list[dict[str, Any]]:
    """The pies of ``GET /equity/pies``; a body that is not a list is unreadable, never "no pies"."""
    if not isinstance(body, list):
        raise ValueError("the pie list is not a list")
    return [p for p in body if isinstance(p, dict) and p.get("id") is not None]


def map_pie(summary: dict[str, Any], detail: Any, positions_by_ticker: dict[str, PositionRecord]) -> PieRecord:
    """One pie from its list row and its detail (``None`` detail: list fields only)."""
    result = _obj(summary.get("result"))
    divs = _obj(summary.get("dividendDetails"))
    detail = _obj(detail)
    settings = _obj(detail.get("settings"))
    instruments: list[dict[str, Any]] = []
    for inst in detail.get("instruments") or []:
        if not isinstance(inst, dict):
            continue
        ticker = str(inst.get("ticker") or "")
        pos = positions_by_ticker.get(ticker)
        res = _obj(inst.get("result"))
        instruments.append({
            "t212_ticker": ticker or None,
            "isin": pos.isin if pos else None,
            "name": pos.name if pos else ticker,
            "expected_share": _str(dec(inst.get("expectedShare"))),
            "current_share": _str(dec(inst.get("currentShare"))),
            "owned_quantity": _str(dec(inst.get("ownedQuantity"))),
            "value": _str(dec(res.get("priceAvgValue"))),
            "result": _str(dec(res.get("priceAvgResult"))),
            "issues": [
                {"name": str(i.get("name") or ""), "severity": str(i.get("severity") or "")}
                for i in inst.get("issues") or [] if isinstance(i, dict)
            ],
        })
    end = when(settings.get("endDate"))
    return PieRecord(
        id=str(summary["id"]),
        name=str(settings.get("name") or f"Pie {summary['id']}")[:120],
        status=str(summary.get("status")) if summary.get("status") else None,
        cash=dec(summary.get("cash")),
        invested=dec(result.get("priceAvgInvestedValue")),
        value=dec(result.get("priceAvgValue")),
        result=dec(result.get("priceAvgResult")),
        result_pct=dec(result.get("priceAvgResultCoef")),
        dividends_gained=dec(divs.get("gained")),
        dividends_reinvested=dec(divs.get("reinvested")),
        dividends_in_cash=dec(divs.get("inCash")),
        dividend_action=str(settings.get("dividendCashAction")) if settings.get("dividendCashAction") else None,
        goal=dec(settings.get("goal")),
        end_date=end.date() if end else None,
        instruments=instruments,
    )


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def map_instrument_types(body: Any, isins: set[str]) -> dict[str, str]:
    """ISIN → T212 instrument type (ETF, STOCK, …) for the ISINs asked for."""
    out: dict[str, str] = {}
    for item in body if isinstance(body, list) else []:
        if not isinstance(item, dict):
            continue
        isin = _isin(item.get("isin"))
        kind = str(item.get("type") or "").upper()
        if isin in isins and kind:
            out[isin] = kind
    return out
