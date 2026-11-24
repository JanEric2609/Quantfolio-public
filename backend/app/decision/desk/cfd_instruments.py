"""What the event desk (ADR 0028) can trade as a manual Trading 212 CFD: the four indices and 20 large stocks.

Facts come from the owner's Trading 212 CFD account (observed 2026-11-23); values marked ``assumed`` are
defaults until the owner reads them. Paper scoring uses these numbers, and the messages show "(assumed)"
next to the assumed ones. Display and paper costs only: nothing here can place an order.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.foundation import technical

from . import cfd, service
from .underlyings import ALIASES, ISIN_RE, Underlying

logger = logging.getLogger(__name__)

#: Observed or assumed Trading 212 facts. ``assumed`` marks values the owner has not read yet.
T212_STOCK = {
    "margin_rate": Decimal("0.2"),  # 1:5 (NVDA, SAP)
    "overnight_long": Decimal("0.000694"),  # 0.0694 % of the value per night, about 25 %/yr (NVDA, SAP)
    "overnight_short": Decimal("0.000251"),  # 0.0251 % per night
    "fx_fee_on_result": Decimal("0.005"),
    "quantity_step": Decimal("0.0001"),
    "default_spread_bp": Decimal("15"),  # unknown names
    "observed": "2026-11-23",
}
T212_INDEX = {
    "margin_rate": Decimal("0.05"),
    "overnight_long": Decimal("0.000083"),
    "overnight_short": Decimal("0.000139"),
    "quantity_step": Decimal("0.0001"),
    "observed": "2026-11-23",
}
#: Per-name spread in basis points (the worst seen); names not listed use the default and show "(assumed)".
STOCK_SPREAD_BP: dict[str, Decimal] = {"NVDA": Decimal("7.5"), "AAPL": Decimal("17"), "SAP": Decimal("9")}
#: Trading 212 expires an index CFD a day before the futures' third Friday (Berlin time). One entry per contract.
INDEX_EXPIRY: dict[str, str] = {"SPX": "2026-12-17T15:00", "NDX": "2026-12-17T15:00",
                                "DAX": "2026-12-17T13:00", "ES50": "2026-12-17T13:00"}
INDEX_EXPIRY_ASSUMED = {"SPX": True, "NDX": False, "DAX": True, "ES50": False}


@dataclass(frozen=True)
class CfdInstrument:
    key: str  # "SPX" ... or "US:NVDA", "DE:SAP"
    t212_name: str
    kind: str  # index | stock
    currency: str  # EUR | USD
    underlying: Underlying
    level_ticker: str  # daily bars for the technical rules
    spread_bp: Decimal
    margin_rate: Decimal
    overnight_long: Decimal  # fraction of the position value per night
    overnight_short: Decimal
    quantity_step: Decimal
    spread_assumed: bool = False

    def overnight_rate(self, direction: str) -> Decimal:
        return self.overnight_long if direction == "long" else self.overnight_short

    @property
    def usd(self) -> bool:
        return self.currency == "USD"


def _index(key: str) -> CfdInstrument:
    name, ticker, ccy = cfd.MARKETS[key]
    return CfdInstrument(
        key=key, t212_name=name, kind="index", currency=ccy, underlying=ALIASES[key], level_ticker=ticker,
        spread_bp=cfd.SPREAD_BP[key], margin_rate=T212_INDEX["margin_rate"],
        overnight_long=T212_INDEX["overnight_long"], overnight_short=T212_INDEX["overnight_short"],
        quantity_step=T212_INDEX["quantity_step"])


def _stock(ticker: str, isin: str, label: str, country: str) -> CfdInstrument:
    us = country == "US"
    known = STOCK_SPREAD_BP.get(ticker)
    spread = known if known is not None else T212_STOCK["default_spread_bp"]
    under = Underlying(isin, label, "XNYS" if us else "XETR", True, symbol=ticker if us else f"{ticker}.DE",
                       alpaca_symbol=ticker if us else None)
    return CfdInstrument(
        key=f"{country}:{ticker}", t212_name=f"{label} ({ticker})", kind="stock", currency="USD" if us else "EUR",
        underlying=under, level_ticker=ticker if us else f"{ticker}.DE", spread_bp=spread,
        margin_rate=T212_STOCK["margin_rate"], overnight_long=T212_STOCK["overnight_long"],
        overnight_short=T212_STOCK["overnight_short"], quantity_step=T212_STOCK["quantity_step"],
        spread_assumed=known is None)


INDICES: tuple[CfdInstrument, ...] = tuple(_index(k) for k in ("SPX", "NDX", "DAX", "ES50"))
_STOCK_ROWS = (
    ("AAPL", "US0378331005", "Apple", "US"), ("MSFT", "US5949181045", "Microsoft", "US"),
    ("NVDA", "US67066G1040", "NVIDIA", "US"), ("AMZN", "US0231351067", "Amazon", "US"),
    ("META", "US30303M1027", "Meta Platforms", "US"), ("GOOGL", "US02079K3059", "Alphabet", "US"),
    ("TSLA", "US88160R1014", "Tesla", "US"), ("AMD", "US0079031078", "Advanced Micro Devices", "US"),
    ("NFLX", "US64110L1060", "Netflix", "US"), ("AVGO", "US11135F1012", "Broadcom", "US"),
    ("SAP", "DE0007164600", "SAP", "DE"), ("SIE", "DE0007236101", "Siemens", "DE"),
    ("ALV", "DE0008404005", "Allianz", "DE"), ("DTE", "DE0005557508", "Deutsche Telekom", "DE"),
    ("MBG", "DE0007100000", "Mercedes-Benz", "DE"), ("BAS", "DE000BASF111", "BASF", "DE"),
    ("IFX", "DE0006231004", "Infineon", "DE"), ("MUV2", "DE0008430026", "Munich Re", "DE"),
    ("DBK", "DE0005140008", "Deutsche Bank", "DE"), ("ADS", "DE000A1EWWW0", "Adidas", "DE"),
)
STOCKS: tuple[CfdInstrument, ...] = tuple(_stock(*row) for row in _STOCK_ROWS)
UNIVERSE: tuple[CfdInstrument, ...] = (*INDICES, *STOCKS)
_BY_KEY = {i.key: i for i in UNIVERSE}
_BY_SYMBOL = {i.underlying.symbol: i for i in STOCKS if i.underlying.symbol}
_BY_ISIN = {i.underlying.isin: i for i in UNIVERSE}
COSTS_JSON: dict[str, dict[str, str]] = {
    "index": {"spread_bp": {i.key: str(i.spread_bp) for i in INDICES}, "fx_fee_on_result": "0.005"},  # type: ignore[dict-item]
    "stock": {"spread_bp": {i.key: str(i.spread_bp) for i in STOCKS}, "default_spread_bp": "15",  # type: ignore[dict-item]
              "fx_fee_on_result": "0.005"},
}

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def by_key(key: str) -> CfdInstrument | None:
    return _BY_KEY.get(key)


def for_symbol(db: Session | None, symbol: str) -> CfdInstrument | None:
    """A listed instrument, or an ad-hoc stock instrument (US defaults) for any US ticker or Xetra ISIN/.DE
    ticker. ``None`` for garbage."""
    text = (symbol or "").strip().upper()
    if not text:
        return None
    if text in _BY_KEY:
        return _BY_KEY[text]
    if text in ALIASES:
        return _BY_KEY[text]
    if text in _BY_SYMBOL:
        return _BY_SYMBOL[text]
    if text in _BY_ISIN:
        return _BY_ISIN[text]
    if ISIN_RE.fullmatch(text):
        us = text.startswith("US")
        if not (us or text.startswith("DE")):
            return None
        return _adhoc(text, text, "US" if us else "DE", symbol=None)
    if _TICKER_RE.fullmatch(text):
        if text.endswith(".DE"):
            return _adhoc(text, text[:-3], "DE", symbol=text)
        if "." not in text:
            return _adhoc(text, text, "US", symbol=text)
    return None


def _adhoc(label: str, ticker: str, country: str, symbol: str | None) -> CfdInstrument:
    us = country == "US"
    sym = symbol or (ticker if us else None)
    under = Underlying(ticker if ISIN_RE.fullmatch(ticker) else ticker, label, "XNYS" if us else "XETR", False,
                       symbol=sym, alpaca_symbol=sym if us else None)
    return CfdInstrument(
        key=f"{country}:{ticker}", t212_name=label, kind="stock", currency="USD" if us else "EUR", underlying=under,
        level_ticker=sym or ticker, spread_bp=T212_STOCK["default_spread_bp"], margin_rate=T212_STOCK["margin_rate"],
        overnight_long=T212_STOCK["overnight_long"], overnight_short=T212_STOCK["overnight_short"],
        quantity_step=T212_STOCK["quantity_step"], spread_assumed=True)


def reference_price(db: Session, cli, inst: CfdInstrument, now: datetime, errors: list[str] | None = None
                    ) -> Decimal | None:
    """The price paper scoring uses: ``service.underlying_price`` (Alpaca's quote for US names and index
    proxies, sc for Xetra), ``None`` when stale or missing."""
    return service.underlying_price(db, cli, inst.underlying, now=now, errors=errors)


def daily_bars(db: Session, inst: CfdInstrument, days: int = 400) -> list[technical.Bar]:
    from app.foundation.market import history

    rows = history(db, inst.level_ticker, days=days)
    return technical.bars_from_rows(rows)


def atr14(db: Session, inst: CfdInstrument, day: date | None = None) -> Decimal | None:
    """Average true range over 14 daily bars before *day* (today's partial bar excluded)."""
    bars = daily_bars(db, inst, days=60)
    if day is not None:
        bars = [b for b in bars if b.date < day]
    value = technical.atr(bars, 14)
    return None if value is None else Decimal(str(value))


def index_expiry(key: str, today: date) -> datetime:
    """The contract's expiry (Berlin time), or the assumed one: the Thursday before the third Friday of
    Mar/Jun/Sep/Dec at 13:00."""
    from . import calendar as cal

    raw = INDEX_EXPIRY.get(key)
    if raw:
        got = datetime.fromisoformat(raw).replace(tzinfo=cal.BERLIN)
        if got.date() >= today:
            return got
    return assumed_expiry(today)


def assumed_expiry(today: date) -> datetime:
    from . import calendar as cal

    for year in (today.year, today.year + 1):
        for month in (3, 6, 9, 12):
            first = date(year, month, 1)
            third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
            thursday = third_friday - timedelta(days=1)
            if thursday >= today:
                return datetime(thursday.year, thursday.month, thursday.day, 13, 0, tzinfo=cal.BERLIN)
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["CfdInstrument", "COSTS_JSON", "INDEX_EXPIRY", "INDICES", "STOCKS", "UNIVERSE", "T212_INDEX",
           "T212_STOCK", "assumed_expiry", "atr14", "by_key", "daily_bars", "for_symbol", "index_expiry",
           "reference_price"]
