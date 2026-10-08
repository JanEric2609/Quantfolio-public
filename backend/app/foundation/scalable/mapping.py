"""Pure mapping from ``sc --json`` payloads to plain records.

Field names follow the CLI's own projections (``src/broker_projections.rs``,
``src/overnight_projections.rs`` in ScalableCapital/scalable-cli). Money and
quantities arrive as JSON numbers or decimal strings depending on the field,
so every value goes through ``to_decimal`` (``Decimal(str(x))``), never float
arithmetic.

Every function here takes the ``result`` object of a broker or overnight
query (``ScalableCli.run`` unwraps it). A payload without the keys a
projection always carries raises ``ScalableProtocolError``: a changed output
format must stop the sync, not store zeros.

Status and cash-type values seen on a real account (2026-10): SETTLED,
PENDING, CANCELLED; DEPOSIT. Unknown values are never guessed into cash
flows; they land as ``needs_review`` ledger rows.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.foundation.text_safety import ascii_safe

from .cli import ScalableProtocolError

SOURCE = "scalable"


def as_dict(value: Any) -> dict[str, Any]:
    """*value* when it is an object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _require(data: Any, key: str, label: str, kind: type | tuple[type, ...] | None = None) -> Any:
    """``data[key]``, refusing a payload that lacks a key sc's projection always writes."""
    if not isinstance(data, dict) or key not in data:
        raise ScalableProtocolError("unexpected_shape", f"sc {label} output has no '{key}'")
    value = data[key]
    if kind is not None and not isinstance(value, kind):
        raise ScalableProtocolError("unexpected_shape", f"sc {label} output has an invalid '{key}'")
    return value


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        # Money objects sometimes come as {"amount": .., "currency": ..}.
        for key in ("amount", "value"):
            if key in value:
                return to_decimal(value[key])
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("time") or value.get("date")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            # A bare date only: "2026-01-01junk" is malformed, not midnight.
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clean_text(value: Any, limit: int) -> str:
    """Broker-supplied text is untrusted: strip controls and bidi overrides, cap length."""
    text = ascii_safe(str(value) if value is not None else "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = " ".join(text.split())
    return text[:limit]


def clean_isin(value: Any) -> str | None:
    text = (str(value).strip().upper() if value is not None else "")
    if len(text) == 12 and text[:2].isalpha() and text.isalnum():
        return text
    return None


@dataclass(frozen=True)
class PositionRecord:
    isin: str
    name: str
    security_type: str | None
    quantity: Decimal
    pending_quantity: Decimal | None
    avg_buy_price: Decimal | None
    current_price: Decimal | None
    current_value: Decimal | None
    currency: str
    price_timestamp: datetime | None
    price_outdated: bool


@dataclass(frozen=True)
class CashRecord:
    cash_balance: Decimal | None
    buying_power: Decimal | None
    pending_buy_orders_amount: Decimal | None
    possible_taxes: Decimal | None


@dataclass(frozen=True)
class OverviewRecord:
    account_id: str | None
    portfolio_id: str | None
    total: Decimal | None
    securities: Decimal | None
    crypto: Decimal | None
    valuation_at: datetime | None


@dataclass(frozen=True)
class SavingsPlanRecord:
    isin: str | None
    name: str
    amount: Decimal | None
    frequency: str | None
    day_of_month: int | None
    next_execution_date: str | None
    kind: str
    # Yearly increase of the amount in percent (sc: configuration.dynamizationRate).
    dynamization_rate: Decimal | None = None
    # True when sc marks the plan paused/inactive/suspended; sc sends no such field
    # today, so this stays False until it does.
    paused: bool = False
    payment_method: str | None = None


@dataclass(frozen=True)
class OvernightRecord:
    balance: Decimal | None
    interest_rate: Decimal | None
    next_payout_date: str | None


@dataclass(frozen=True)
class TransactionRecord:
    id: str
    kind: str  # security | cash | non_trade | eltif | unknown
    type: str | None
    status: str | None
    is_cancellation: bool
    occurred_at: datetime | None
    description: str
    currency: str
    isin: str | None
    side: str | None
    quantity: Decimal | None
    amount: Decimal | None
    cash_type: str | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class TradeDetailsRecord:
    id: str
    average_price: Decimal | None
    total_amount: Decimal | None
    fees: Decimal | None
    taxes: Decimal | None
    filled_quantity: Decimal | None
    # Cash rows (dividends, interest): the gross before tax and the tax the
    # bank withheld, when sc reports them.
    cash_gross: Decimal | None = None
    cash_tax: Decimal | None = None


def map_holdings(data: dict[str, Any]) -> list[PositionRecord]:
    out: list[PositionRecord] = []
    for item in _require(data, "items", "broker holdings", list):
        if not isinstance(item, dict):
            continue
        isin = clean_isin(item.get("isin"))
        quantity = to_decimal(item.get("quantity"))
        if isin is None or quantity is None or quantity <= 0:
            continue
        currency = str(item.get("valuation_currency") or item.get("quote_currency") or "EUR").upper()[:3]
        out.append(
            PositionRecord(
                isin=isin,
                name=clean_text(item.get("name"), 200) or isin,
                security_type=clean_text(item.get("security_type"), 32) or None,
                quantity=quantity,
                pending_quantity=to_decimal(item.get("pending_quantity")),
                # fifo_price is the FIFO cost per unit.
                avg_buy_price=to_decimal(item.get("fifo_price")),
                current_price=to_decimal(item.get("quote_mid_price")),
                current_value=to_decimal(item.get("valuation")),
                currency=currency,
                price_timestamp=parse_ts(item.get("quote_timestamp_utc")),
                price_outdated=bool(item.get("quote_is_outdated")),
            )
        )
    return out


def map_cash(data: dict[str, Any]) -> CashRecord:
    _require(data, "cash_balance", "broker cash-breakdown")
    return CashRecord(
        cash_balance=to_decimal(data.get("cash_balance")),
        buying_power=to_decimal(data.get("buying_power")),
        pending_buy_orders_amount=to_decimal(data.get("pending_buy_orders_amount")),
        possible_taxes=to_decimal(data.get("possible_taxes")),
    )


def map_overview(data: dict[str, Any]) -> OverviewRecord:
    valuation = _require(data, "valuation", "broker overview", dict)
    timestamps = as_dict(data.get("timestamps"))
    return OverviewRecord(
        account_id=clean_text(data.get("account_id"), 128) or None,
        portfolio_id=clean_text(data.get("portfolio_id"), 128) or None,
        total=to_decimal(valuation.get("total")),
        securities=to_decimal(valuation.get("securities")),
        crypto=to_decimal(valuation.get("crypto")),
        valuation_at=parse_ts(timestamps.get("valuation_timestamp_utc")),
    )


def map_context(data: dict[str, Any]) -> str | None:
    context = data.get("context") if isinstance(data.get("context"), dict) else data
    value = (context or {}).get("portfolio_id")
    return clean_text(value, 128) or None


_PAUSED_STATES = {"PAUSED", "INACTIVE", "SUSPENDED"}


def _plan_paused(item: dict[str, Any]) -> bool:
    """Defensive: read a paused flag or status/state field if sc ever sends one."""
    if item.get("paused") is True:
        return True
    for key in ("status", "state"):
        value = item.get(key)
        if isinstance(value, str) and value.strip().upper() in _PAUSED_STATES:
            return True
    return False


def map_savings_plans(data: dict[str, Any]) -> list[SavingsPlanRecord]:
    out: list[SavingsPlanRecord] = []
    for item in _require(data, "items", "broker savings-plans", list):
        if not isinstance(item, dict):
            continue
        day = item.get("day_of_month")
        config = as_dict(item.get("configuration"))
        dynamization = config.get("dynamizationRate", config.get("dynamization_rate", item.get("dynamization_rate")))
        out.append(
            SavingsPlanRecord(
                isin=clean_isin(item.get("isin")),
                name=clean_text(item.get("name") or item.get("ticker"), 200),
                amount=to_decimal(item.get("amount")),
                frequency=clean_text(item.get("frequency"), 32) or None,
                day_of_month=int(day) if isinstance(day, int) or (isinstance(day, str) and day.isdigit()) else None,
                next_execution_date=clean_text(item.get("next_execution_date"), 32) or None,
                kind=clean_text(item.get("kind"), 16) or "security",
                dynamization_rate=to_decimal(dynamization) if dynamization is not None else None,
                paused=_plan_paused(item),
                payment_method=clean_text(item.get("payment_method") or config.get("paymentMethod"), 48) or None,
            )
        )
    return out


def map_overnight(data: dict[str, Any]) -> OvernightRecord:
    _require(data, "balance", "overnight")
    payout = data.get("next_payout_date")
    return OvernightRecord(
        balance=to_decimal(data.get("balance")),
        interest_rate=to_decimal(data.get("interest_rate")),
        next_payout_date=str(payout) if payout is not None else None,
    )


_KINDS = {
    "BrokerSecurityTransactionSummary": "security",
    "BrokerCashTransactionSummary": "cash",
    "BrokerNonTradeSecurityTransactionSummary": "non_trade",
    "BrokerEltifTransactionSummary": "eltif",
}


def map_transactions(data: dict[str, Any]) -> tuple[list[TransactionRecord], str | None]:
    """Return one page of transactions and the cursor for the next page."""
    out: list[TransactionRecord] = []
    for item in _require(data, "items", "broker transactions", list):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        kind = _KINDS.get(str(item.get("summary_type") or ""), "unknown")
        isin = clean_isin(item.get("isin") or item.get("related_isin"))
        quantity = to_decimal(item.get("quantity") if kind != "eltif" else item.get("eltif_quantity"))
        out.append(
            TransactionRecord(
                id=clean_text(item.get("id"), 120),
                kind=kind,
                type=clean_text(item.get("type"), 48) or None,
                status=(clean_text(item.get("status"), 32) or None),
                is_cancellation=bool(item.get("is_cancellation")),
                occurred_at=parse_ts(item.get("last_event_datetime")),
                description=clean_text(item.get("description"), 300),
                currency=str(item.get("currency") or "EUR").upper()[:3],
                isin=isin,
                side=(clean_text(item.get("side"), 8).upper() or None),
                quantity=quantity,
                amount=to_decimal(item.get("amount")),
                cash_type=(clean_text(item.get("cash_transaction_type"), 48).upper() or None),
                raw={k: item.get(k) for k in ("id", "summary_type", "type", "status", "cash_transaction_type", "security_transaction_type")},
            )
        )
    cursor = data.get("cursor")
    return out, (str(cursor) if cursor else None)


def map_overnight_interest(data: dict[str, Any]) -> tuple[list[TransactionRecord], str | None]:
    """One page of overnight-account interest credits, as cash transactions.

    Only rows sc types INTEREST are kept (the query already filters on it);
    transfers between the broker cash and the overnight account stay inside
    Scalable and are not flows.
    """
    out: list[TransactionRecord] = []
    for item in _require(data, "items", "overnight transactions", list):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        cash_type = clean_text(item.get("cash_transaction_type"), 48).upper() or None
        if cash_type != "INTEREST":
            continue
        out.append(
            TransactionRecord(
                id="overnight:" + clean_text(item.get("id"), 100),
                kind="cash",
                type=clean_text(item.get("type"), 48) or None,
                status=clean_text(item.get("status"), 32) or None,
                is_cancellation=bool(item.get("is_cancellation")),
                occurred_at=parse_ts(item.get("last_event_datetime")),
                description=clean_text(item.get("description"), 300) or "Scalable Capital overnight interest",
                currency=str(item.get("currency") or "EUR").upper()[:3],
                isin=None,
                side=None,
                quantity=None,
                amount=to_decimal(item.get("amount")),
                cash_type=cash_type,
                raw={k: item.get(k) for k in ("id", "type", "status", "cash_transaction_type")},
            )
        )
    cursor = data.get("cursor")
    return out, (str(cursor) if cursor else None)


def map_trade_details(data: dict[str, Any]) -> TradeDetailsRecord:
    if not isinstance(data, dict):
        raise ScalableProtocolError("unexpected_shape", "sc broker transaction details output is not an object")
    trade = data.get("security_trade") or {}
    cash_tax = as_dict(as_dict(data.get("cash")).get("tax_details"))
    amounts = trade.get("trade_transaction_amounts") or {}
    shares = trade.get("number_of_shares") or {}
    taxes = to_decimal(trade.get("taxes"))
    aggregated = trade.get("aggregated_transaction_taxes")
    if taxes is None and isinstance(aggregated, dict):
        taxes = to_decimal(aggregated.get("total_tax"))
    fees = to_decimal(trade.get("fee"))
    if fees is None:
        parts = [to_decimal(amounts.get(k)) for k in ("transaction_fee", "venue_fee", "crypto_spread_fee")]
        known = [p for p in parts if p is not None]
        fees = sum(known, Decimal("0")) if known else None
    return TradeDetailsRecord(
        id=clean_text(data.get("id"), 120),
        average_price=to_decimal(trade.get("average_price")),
        total_amount=to_decimal(trade.get("total_amount")),
        fees=fees,
        taxes=taxes,
        filled_quantity=to_decimal(shares.get("filled")),
        cash_gross=to_decimal(cash_tax.get("gross_amount")),
        cash_tax=to_decimal(cash_tax.get("tax_amount")),
    )


# --- ledger classification --------------------------------------------------

EXECUTED_STATUSES = frozenset({"FILLED", "EXECUTED", "SETTLED", "COMPLETED", "BOOKED", "DONE"})
# Statuses that say a row has not (or will never) execute. Held back like any
# unconfirmed row, but known: they never turn the sync into a warning.
NOT_EXECUTED_STATUSES = frozenset(
    {"PENDING", "OPEN", "WAITING_FOR_EXECUTION", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}
)
_EXTERNAL_CASH = ("DEPOSIT", "WITHDRAW", "TRANSFER", "PAYOUT", "PAY_OUT", "PAY_IN", "PAYIN", "SDDI", "DIRECT_DEBIT", "REFERRAL")


def status_unconfirmed(tx: TransactionRecord) -> bool:
    """True when the row's status does not say it was executed.

    Trades must carry an executed status (an empty one is not proof). Cash rows
    are booked movements; one is held back only when it names a status that is
    not an executed one (pending, cancelled, ...). Held-back rows are not
    written; syncs re-read the last seven days, so a row that settles within
    that window is imported then. The sync log names every status it held
    back so ``EXECUTED_STATUSES`` can be extended.
    """
    status = (tx.status or "").upper()
    if tx.kind in {"security", "eltif"}:
        return status not in EXECUTED_STATUSES
    if tx.kind == "cash":
        return bool(status) and status not in EXECUTED_STATUSES
    return False


def ledger_activity_type(tx: TransactionRecord) -> str | None:
    """Activity type for the wealth ledger, or None to skip the row.

    ``cashflow`` is the only type TWR/MWR treat as money crossing the
    portfolio boundary, so it is reserved for deposits and withdrawals: a
    transfer from the DKB giro then nets against the DKB statement line.
    Dividends, interest, taxes and fees are return, not external flows.
    """
    if tx.is_cancellation or status_unconfirmed(tx):
        return None
    if tx.kind in {"security", "eltif"}:
        if tx.side == "BUY":
            return "buy"
        if tx.side == "SELL":
            return "sell"
        return "other"
    if tx.kind == "non_trade":
        return "corporate_action"
    if tx.kind == "cash":
        cash_type = tx.cash_type or ""
        if "DIVIDEND" in cash_type or "DISTRIBUTION" in cash_type:
            return "dividend"
        if "INTEREST" in cash_type:
            return "interest"
        if "TAX" in cash_type:
            return "tax"
        if "FEE" in cash_type:
            return "fee"
        if any(token in cash_type for token in _EXTERNAL_CASH):
            return "cashflow"
        return "other"
    return "other"
