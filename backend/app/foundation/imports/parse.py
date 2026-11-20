"""CSV parsing and transaction extraction."""

import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_ROWS = 50_000


@dataclass
class ParsedTransaction:
    """A parsed transaction candidate."""

    date: datetime
    description: str
    amount: float
    currency: str
    isin: str | None
    ticker: str | None
    side: str  # "buy" or "sell"
    quantity: float | None
    # Unit price and fees, when the statement carries them. Together with
    # ``quantity`` they are what tax-lot seeding needs
    # (tax_cockpit.seed_lots_from_activity: cost = quantity * price + fees).
    price: float | None = None
    fees: float | None = None


def parse_csv(file_content: bytes, format_type: str) -> tuple[list[ParsedTransaction], list[str]]:
    """Parse CSV file into transaction candidates.

    Returns:
        (list of transactions, list of errors)
    """
    if len(file_content) > _MAX_UPLOAD_BYTES:
        return [], [f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB"]

    if format_type == "dkb":
        return _parse_dkb_csv(file_content)
    elif format_type == "comdirect":
        try:
            return _parse_comdirect_csv(file_content)
        except NotImplementedError:
            return [], ["Comdirect CSV parsing not yet implemented"]
    elif format_type == "trade_republic":
        try:
            return _parse_trade_republic_csv(file_content)
        except NotImplementedError:
            return [], ["Trade Republic CSV parsing not yet implemented"]
    else:
        return [], [f"Unknown format: {format_type}"]


def _parse_dkb_csv(file_content: bytes) -> tuple[list[ParsedTransaction], list[str]]:
    """Parse DKB CSV format."""
    errors = []
    transactions = []

    try:
        df = pd.read_csv(
            io.BytesIO(file_content),
            encoding="cp1252",
            encoding_errors="replace",
            nrows=_MAX_ROWS,
        )
    except Exception:
        log.exception("Failed to read DKB CSV")
        return [], ["Failed to parse DKB CSV"]

    for idx, row in df.iterrows():
        try:
            # DKB format: Buchungstag, Wertstellung, Umsatztyp, Begünstigter / Auftraggeber, Verwendungszweck, Betrag, Saldo
            booking_date = pd.to_datetime(str(row.get("Buchungstag", ""))).to_pydatetime()
            amount_str = str(row.get("Betrag", "0")).replace(".", "").replace(",", ".")
            amount = float(amount_str)
            currency = "EUR"

            description = str(row.get("Verwendungszweck", ""))
            umsatztyp = str(row.get("Umsatztyp", ""))

            # Determine side and extract ISIN if possible
            side = "buy" if amount < 0 else "sell"
            isin = _extract_isin(description)
            ticker = None
            quantity, price, fees = _extract_trade_fields(row, description, abs(amount), side)

            # Only create transaction if it's a security transaction (has ISIN or looks like one)
            if isin or "Wertpapier" in umsatztyp or "Security" in description:
                transactions.append(
                    ParsedTransaction(
                        date=booking_date,
                        description=description,
                        amount=abs(amount),
                        currency=currency,
                        isin=isin,
                        ticker=ticker,
                        side=side,
                        quantity=quantity,
                        price=price,
                        fees=fees,
                    )
                )
        except Exception:
            log.exception("Failed to parse DKB CSV row %s", idx)
            errors.append(f"Row {idx}: failed to parse row")

    return transactions, errors


def _parse_amount(val: Any) -> float:
    """Parse float from string handling German (1.234,56 or -1500,50) and US (1,234.56 or -500.00) formats."""
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("EUR", "").replace("€", "").strip()
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _get_col_value(row: Any, candidates: list[str]) -> Any:
    for c in candidates:
        if c in row and pd.notna(row[c]):
            return row[c]
    # Check lowercase/strip matches
    for k, v in row.items():
        k_clean = str(k).strip().lower()
        for c in candidates:
            if c.lower() in k_clean and pd.notna(v):
                return v
    return None


def _parse_comdirect_csv(file_content: bytes) -> tuple[list[ParsedTransaction], list[str]]:
    """Parse Comdirect CSV format."""
    errors: list[str] = []
    transactions: list[ParsedTransaction] = []
    try:
        df = pd.read_csv(
            io.BytesIO(file_content),
            delimiter=";",
            encoding="latin1",
            encoding_errors="replace",
            skipinitialspace=True,
            nrows=_MAX_ROWS,
        )
    except Exception:
        try:
            df = pd.read_csv(
                io.BytesIO(file_content),
                encoding="utf-8",
                encoding_errors="replace",
                nrows=_MAX_ROWS,
            )
        except Exception:
            log.exception("Failed to read Comdirect CSV")
            return [], ["Failed to parse Comdirect CSV"]

    for idx, row in df.iterrows():
        try:
            date_val = _get_col_value(row, ["Buchungstag", "Datum", "Wertstellung"])
            if pd.isna(date_val) or not str(date_val).strip():
                continue
            booking_date = pd.to_datetime(str(date_val), dayfirst=True).to_pydatetime()
            raw_amount = _get_col_value(row, ["Betrag", "Umsatz in EUR", "Betrag EUR", "Betrag in EUR", "Umsatz"])
            amount = _parse_amount(raw_amount)
            currency = "EUR"
            description = str(_get_col_value(row, ["Buchungstext", "Vorgang", "Verwendungszweck", "Text"]) or "")
            isin = _extract_isin(description)
            side = "buy" if amount < 0 else "sell"
            quantity, price, fees = _extract_trade_fields(row, description, abs(amount), side)
            if isin or "Wertpapier" in description or "Kauf" in description or "Verkauf" in description:
                transactions.append(
                    ParsedTransaction(
                        date=booking_date,
                        description=description,
                        amount=abs(amount),
                        currency=currency,
                        isin=isin,
                        ticker=None,
                        side=side,
                        quantity=quantity,
                        price=price,
                        fees=fees,
                    )
                )
        except Exception:
            errors.append(f"Row {idx}: failed to parse row")
    return transactions, errors


def _parse_trade_republic_csv(file_content: bytes) -> tuple[list[ParsedTransaction], list[str]]:
    """Parse Trade Republic CSV format."""
    errors: list[str] = []
    transactions: list[ParsedTransaction] = []
    try:
        df = pd.read_csv(
            io.BytesIO(file_content),
            delimiter=";",
            encoding="utf-8",
            encoding_errors="replace",
            nrows=_MAX_ROWS,
        )
    except Exception:
        try:
            df = pd.read_csv(
                io.BytesIO(file_content),
                encoding="utf-8",
                encoding_errors="replace",
                nrows=_MAX_ROWS,
            )
        except Exception:
            log.exception("Failed to read Trade Republic CSV")
            return [], ["Failed to parse Trade Republic CSV"]

    for idx, row in df.iterrows():
        try:
            date_val = _get_col_value(row, ["Datum", "Date", "timestamp"])
            if pd.isna(date_val) or not str(date_val).strip():
                continue
            booking_date = pd.to_datetime(str(date_val)).to_pydatetime()
            raw_amount = _get_col_value(row, ["Betrag", "Wert", "Amount"])
            amount = _parse_amount(raw_amount)
            currency = "EUR"
            description = str(_get_col_value(row, ["Name", "Description", "Typ"]) or "")
            isin_raw = _get_col_value(row, ["ISIN"])
            isin = str(isin_raw) if isin_raw and str(isin_raw) != "nan" else None
            if not isin:
                isin = _extract_isin(description)
            tx_type = str(_get_col_value(row, ["Typ"]) or "").lower()
            side = "sell" if ("verkauf" in tx_type or "sell" in tx_type or (amount > 0 and "kauf" not in tx_type)) else "buy"
            quantity, price, fees = _extract_trade_fields(row, description, abs(amount), side)
            transactions.append(
                ParsedTransaction(
                    date=booking_date,
                    description=description,
                    amount=abs(amount),
                    currency=currency,
                    isin=isin,
                    ticker=None,
                    side=side,
                    quantity=quantity,
                    price=price,
                    fees=fees,
                )
            )
        except Exception:
            errors.append(f"Row {idx}: failed to parse row")
    return transactions, errors


# Header spellings of the trade columns. Matched case-insensitively and EXACTLY
# (not as substrings): "Währungskurs" must never be read as the price.
_QUANTITY_COLUMNS = ("stück", "stueck", "stk", "anzahl", "menge", "shares", "quantity", "qty")
_PRICE_COLUMNS = ("kurs", "ausführungskurs", "ausfuehrungskurs", "preis", "stückpreis", "price")
_FEE_COLUMNS = ("gebühren", "gebuehren", "gebühr", "gebuehr", "provision", "entgelt", "fee", "fees")

# Labelled amounts inside free text ("10,5 Stück ... Kurs 80,12 EUR"). The label is
# required so an unrelated number in a reference is never taken for a quantity.
# The unit word must end there ("Stückzinsen" is accrued interest, not a quantity).
_UNIT = r"(?:(?:Stück|Stueck)(?![a-zäöüß])|Stk\.?|St\.)"
_QUANTITY_TEXT = re.compile(
    # The lookbehind keeps the tail of an ISIN or order number ("...Y983 Stück") from
    # being read as the count.
    rf"(?:{_UNIT}\s*[:=]?\s*(\d[\d.,]*)|(?<![\w.,])(\d[\d.,]*)\s*{_UNIT})",
    re.IGNORECASE,
)
_PRICE_TEXT = re.compile(
    r"\b(?:Ausführungskurs|Ausfuehrungskurs|Kurs|Preis)(?![a-zäöüß])\s*[:=]?\s*(?:EUR\s*)?(\d[\d.,]*)",
    re.IGNORECASE,
)
_FEE_TEXT = re.compile(
    r"\b(?:Gebühren|Gebuehren|Gebühr|Gebuehr|Provision|Entgelt)(?![a-zäöüß])\s*[:=]?\s*(?:EUR\s*)?(\d[\d.,]*)",
    re.IGNORECASE,
)


def _text_number(token: str | None) -> str | None:
    """A regex-captured number without the sentence punctuation that trails it."""
    return token.rstrip(".,") if token else None


def _exact_column(row: Any, candidates: tuple[str, ...]) -> Any:
    """The value of the first column whose header equals one of *candidates*."""
    wanted = set(candidates)
    for key, value in row.items():
        if str(key).strip().lower() in wanted and pd.notna(value) and str(value).strip():
            return value
    return None


def _positive(value: Any) -> float | None:
    """A strictly positive number (sells and TR export shares as negatives), else None."""
    number = abs(_parse_amount(value)) if value is not None else 0.0
    return number if number > 0 else None


def _parse_quantity(value: Any) -> float | None:
    """A share count: ``1.000`` is a thousand (German grouping), ``10,5`` ten and a half."""
    if value is None:
        return None
    token = str(value).strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", token):
        token = token.replace(".", "")
    return _positive(token)


def _extract_trade_fields(
    row: Any, description: str, amount: float, side: str,
) -> tuple[float | None, float | None, float | None]:
    """``(quantity, unit price, fees)`` for one statement row; each None when absent.

    Read from explicit quantity/price/fee columns when the file has them, else
    from labelled text in the description. When only a quantity is known the
    price is derived from the cash amount, which for a buy is
    ``quantity * price + fees`` (and ``quantity * price - fees`` for a sell), so
    the lot's cost basis equals what actually left the account. Never guessed:
    no quantity means no price either.
    """
    quantity = _parse_quantity(_exact_column(row, _QUANTITY_COLUMNS))
    if quantity is None:
        match = _QUANTITY_TEXT.search(description)
        if match:
            quantity = _parse_quantity(_text_number(match.group(1) or match.group(2)))
    if quantity is None:
        return None, None, None

    fees_raw = _exact_column(row, _FEE_COLUMNS)
    if fees_raw is None:
        fee_match = _FEE_TEXT.search(description)
        fees_raw = _text_number(fee_match.group(1)) if fee_match else None
    fees = _positive(fees_raw)

    price_raw = _exact_column(row, _PRICE_COLUMNS)
    if price_raw is None:
        price_match = _PRICE_TEXT.search(description)
        price_raw = _text_number(price_match.group(1)) if price_match else None
    price = _positive(price_raw)
    if price is None and amount > 0:
        net = amount - (fees or 0.0) if side == "buy" else amount + (fees or 0.0)
        price = net / quantity if net > 0 else None
    return quantity, price, fees


def _extract_isin(description: str) -> str | None:
    """Extract ISIN from description text."""
    import re

    match = re.search(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", description)
    return match.group(0) if match else None
