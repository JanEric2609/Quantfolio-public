"""The currencies an ETF holding is exposed to, looking through to its index.

Verification (risk report, FX stress) used to treat a holding as exposed to
the currency it is quoted in. For a UCITS index ETF that is the wrong
question: EUNL.DE is quoted in EUR on Xetra, but on 2026-09-28 73.3% of
MSCI World traded in USD, 5.9% in JPY, 3.4% in GBP and only 8.2% in EUR. A
portfolio of nothing but EUNL.DE showed no dollar exposure at all, and the
"Dollar Slump" scenario left it untouched.

For an ETF on :data:`ETF_INDEX` the exposure is its index's currency
weights: the trading currencies of the constituents, which is what an
unhedged fund's value moves with. The weights come from a tracking fund's
published daily holdings (State Street SPDR's UCITS range publishes every
holding with its trading currency); :func:`refresh_index_currency_weights`
reloads them monthly into ``index_currency_weights``, and :data:`SEED`
stands in until the first refresh. Weights older than :data:`STALE_AFTER`
are flagged. A currency-hedged share class is exposed to its hedge
currency. Anything else keeps its quote currency (basis ``"listing"``).

Owner decision 2026-09-29: an index-weights table rather than estimating
exposures from returns (noisy by 10-20 points) or leaving the quote
currency.

The same files list each constituent's country, so the refresh also stores
country weights (:data:`COUNTRY_SEED` until then): the ETF page's geographic
breakdown (:func:`etf_country_weights`). yfinance has no country data for
funds. Owner decision 2026-09-30.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Weights older than this are flagged: index country mixes drift slowly,
#: but a refresh that has silently failed for half a year should be visible.
STALE_AFTER = timedelta(days=183)

_SEED_AS_OF = date(2026, 9, 28)

#: Currency weights per index, from SPDR's daily holdings files of
#: 2026-09-28 (currencies under 0.1% folded into "OTHER"). Indices of one
#: country's listings are 100% that currency by construction.
SEED: dict[str, dict[str, float]] = {
    "msci_world": {"USD": 0.733, "EUR": 0.0819, "JPY": 0.0588, "GBP": 0.0335, "CAD": 0.0332, "CHF": 0.0216, "AUD": 0.0153, "SEK": 0.0071, "HKD": 0.004, "SGD": 0.0039, "DKK": 0.0038, "ILS": 0.002, "NOK": 0.0015, "OTHER": 0.0004},  # noqa: E501
    "msci_acwi": {"USD": 0.648, "EUR": 0.073, "JPY": 0.0517, "TWD": 0.034, "GBP": 0.0297, "CAD": 0.0293, "KRW": 0.0255, "HKD": 0.0224, "CHF": 0.0189, "AUD": 0.0134, "INR": 0.0128, "SEK": 0.0063, "BRL": 0.0042, "CNY": 0.004, "ZAR": 0.0035, "SGD": 0.0035, "DKK": 0.0033, "SAR": 0.0028, "ILS": 0.0019, "MXN": 0.0018, "AED": 0.0014, "PLN": 0.0013, "THB": 0.0013, "NOK": 0.0013, "MYR": 0.001, "OTHER": 0.0037},  # noqa: E501
    "msci_acwi_imi": {"USD": 0.6357, "EUR": 0.0694, "JPY": 0.058, "TWD": 0.0342, "GBP": 0.0312, "CAD": 0.0307, "KRW": 0.0249, "HKD": 0.022, "CHF": 0.0186, "AUD": 0.0147, "INR": 0.0146, "SEK": 0.0068, "SGD": 0.0047, "BRL": 0.0044, "DKK": 0.0042, "ZAR": 0.0035, "SAR": 0.0029, "ILS": 0.0029, "CNY": 0.0027, "MXN": 0.0021, "AED": 0.0016, "MYR": 0.0015, "PLN": 0.0013, "NOK": 0.0013, "THB": 0.0011, "NZD": 0.001, "OTHER": 0.004},  # noqa: E501
    "msci_em": {"TWD": 0.2899, "KRW": 0.2135, "HKD": 0.1565, "INR": 0.1073, "BRL": 0.0349, "CNY": 0.034, "ZAR": 0.0292, "SAR": 0.0238, "USD": 0.0161, "MXN": 0.0161, "AED": 0.0124, "PLN": 0.0115, "MYR": 0.0093, "THB": 0.0091, "EUR": 0.006, "QAR": 0.0051, "KWD": 0.0044, "IDR": 0.0044, "CLP": 0.0037, "TRY": 0.0037, "HUF": 0.0032, "PHP": 0.0025, "COP": 0.0016, "CZK": 0.0013, "OTHER": 0.0005},  # noqa: E501
    "msci_europe": {"EUR": 0.5403, "GBP": 0.2207, "CHF": 0.1426, "SEK": 0.0471, "DKK": 0.0246, "USD": 0.0146, "NOK": 0.01, "OTHER": 0.0001},  # noqa: E501
    "sp500": {"USD": 1.0},
    "nasdaq_100": {"USD": 1.0},
    "euro_stoxx_50": {"EUR": 1.0},
    "dax": {"EUR": 1.0},
}

_COUNTRY_SEED_AS_OF = date(2026, 9, 29)

#: Country weights per index: the "Trade Country Name" column of the same
#: SPDR files, 2026-09-29, countries under 0.1% folded into "Other". This is
#: the ETF page's geographic breakdown. S&P 500 members must be US-domiciled;
#: Nasdaq 100, EURO STOXX 50 and DAX (Airbus) span several countries and
#: have no file here, so they get none.
COUNTRY_SEED: dict[str, dict[str, float]] = {
    "msci_world": {"United States": 0.7301, "Japan": 0.0579, "United Kingdom": 0.0342, "Canada": 0.0332, "France": 0.0225, "Switzerland": 0.0216, "Germany": 0.0208, "Australia": 0.0153, "Netherlands": 0.0139, "Spain": 0.0096, "Sweden": 0.008, "Italy": 0.0079, "Singapore": 0.0044, "Hong Kong": 0.0042, "Denmark": 0.0038, "Israel": 0.0027, "Finland": 0.0027, "Belgium": 0.0027, "Norway": 0.0015, "Ireland": 0.0011, "Other": 0.0019},  # noqa: E501
    "msci_acwi": {"United States": 0.6438, "Japan": 0.051, "Taiwan": 0.0341, "United Kingdom": 0.0303, "Canada": 0.0293, "South Korea": 0.0257, "China": 0.0234, "France": 0.0199, "Switzerland": 0.0189, "Germany": 0.0183, "Australia": 0.0134, "India": 0.0128, "Netherlands": 0.0125, "Spain": 0.0084, "Sweden": 0.0071, "Italy": 0.0069, "Brazil": 0.0048, "Singapore": 0.0039, "Hong Kong": 0.0038, "South Africa": 0.0035, "Denmark": 0.0033, "Saudi Arabia": 0.0028, "Finland": 0.0025, "Israel": 0.0024, "Belgium": 0.0021, "Mexico": 0.0018, "United Arab Emirates": 0.0014, "Poland": 0.0013, "Thailand": 0.0013, "Norway": 0.0012, "Other": 0.008},  # noqa: E501
    "msci_acwi_imi": {"United States": 0.632, "Japan": 0.0571, "Taiwan": 0.0342, "United Kingdom": 0.0322, "Canada": 0.0307, "South Korea": 0.0251, "China": 0.0218, "France": 0.0197, "Switzerland": 0.0186, "Germany": 0.0184, "Australia": 0.0147, "India": 0.0145, "Netherlands": 0.0112, "Sweden": 0.0076, "Spain": 0.0075, "Italy": 0.0066, "Singapore": 0.0051, "Brazil": 0.0049, "Denmark": 0.0043, "Hong Kong": 0.0039, "South Africa": 0.0036, "Israel": 0.0034, "Belgium": 0.003, "Saudi Arabia": 0.0029, "Mexico": 0.0021, "Finland": 0.0018, "United Arab Emirates": 0.0016, "Malaysia": 0.0015, "Poland": 0.0013, "Norway": 0.0013, "Thailand": 0.0011, "Other": 0.0065},  # noqa: E501
    "msci_em": {"Taiwan": 0.2875, "South Korea": 0.2155, "China": 0.1969, "India": 0.1072, "Brazil": 0.0398, "South Africa": 0.0295, "Saudi Arabia": 0.0236, "Mexico": 0.0161, "United Arab Emirates": 0.0125, "Poland": 0.0114, "Malaysia": 0.0092, "Thailand": 0.0092, "Greece": 0.006, "Qatar": 0.0051, "Kuwait": 0.0044, "Indonesia": 0.0044, "Peru": 0.0043, "Chile": 0.0037, "Turkey": 0.0036, "Hungary": 0.0032, "Philippines": 0.0025, "Colombia": 0.0016, "Czech Republic": 0.0013, "Other": 0.0014},  # noqa: E501
    "msci_europe": {"United Kingdom": 0.2249, "France": 0.1489, "Switzerland": 0.1424, "Germany": 0.137, "Netherlands": 0.0915, "Spain": 0.0633, "Sweden": 0.0533, "Italy": 0.0517, "Denmark": 0.0246, "Finland": 0.0187, "Belgium": 0.0174, "Norway": 0.0099, "Ireland": 0.0068, "Austria": 0.0061, "Portugal": 0.0034},  # noqa: E501
    "sp500": {"United States": 1.0},
}

#: Indices without their own tracking-fund file, read through a close one.
#: FTSE All-World and MSCI ACWI differ in a few classifications (FTSE counts
#: Korea as developed), not in which currencies make up the index; STOXX
#: Europe 600 and MSCI Europe cover the same markets.
PROXY_INDEX: dict[str, str] = {
    "ftse_all_world": "msci_acwi",
    "stoxx_europe_600": "msci_europe",
}

#: SPDR UCITS funds whose daily holdings file carries each index.
_SPDR_FILES: dict[str, str] = {
    "msci_world": "sppw-gy",
    "msci_acwi": "spyy-gy",
    "msci_acwi_imi": "spyi-gy",
    "msci_em": "spym-gy",
    "msci_europe": "ero-fp",
}
_SPDR_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-{code}.xlsx"

_HEDGED = "hedged:"


def _isins(index_key: str, *isins: str) -> dict[str, str]:
    return {isin: index_key for isin in isins}


#: Plain index ETFs (unhedged share classes in any currency, which share the
#: fund's exposure) and currency-hedged classes (``"hedged:EUR"``), curated
#: from the justETF universe by ISIN on 2026-09-29. Sector, factor, ESG,
#: regional-subset and leveraged variants are left out on purpose. Extend
#: with the ``etf_index_map`` public setting (ISIN -> index key).
ETF_INDEX: dict[str, str] = {
    **_isins(
        "msci_world",
        "IE00B4L5Y983", "IE000OHHIBC6", "IE00B0M62Q58",  # iShares
        "IE00BJ0KDQ92", "IE00BK1PV551",  # Xtrackers
        "IE000BI8OT95", "IE000CNSFAR2", "FR001400U5Q4",  # Amundi
        "IE00B4X9L533", "IE000UQND7H4",  # HSBC
        "IE00BD4TXV59", "IE00B7KQ7B66", "LU0340285161",  # UBS
        "IE00B60SX394", "IE000K8VMPE9",  # Invesco
        "IE000W8HP9L8", "IE0008FB2WZ1", "IE000A0GH076", "IE000Y2ZYZ66",  # BNP Paribas
        "LU3086271106", "LU3173209654", "LU3086271288",
        "DE000ETFL508", "FR001400YYJ0", "IE000B5MAME7", "LU3078637660", "LU3078637405",
    ),
    **_isins(
        "msci_acwi",
        "IE00B6R52259", "IE0002FCUS29", "IE00BYM11H29", "IE00BJXFZ989",
        "LU3243907741", "LU3086265710", "FR0014017NX3",
    ),
    **_isins("msci_acwi_imi", "IE00B3YLTY66", "IE000DD75KQ5"),
    **_isins(
        "msci_em",
        "IE00BKM4GZ66", "IE00BD45KH83",  # iShares Core MSCI EM IMI (same currency mix)
        "IE00B4L5YC18", "IE00B0M63177", "IE00BTJRMP35", "IE000GWA2J58",
        "LU0950674175", "LU0480132876", "IE00B3Z3FS74", "LU1437017350", "LU1737652583",
        "LU2277591868", "IE00B5SSQT16", "IE000KCS7J59", "IE00B469F816", "IE00B3DWVS88",
    ),
    **_isins(
        "msci_europe",
        "IE00B4K48X80", "IE00B1YZSC51", "IE000MAO75G5", "LU0274209237", "LU1242369327",
        "LU1437015735", "LU1737652310", "FR0010261198", "LU0446734104", "LU0950668524",
        "LU3254395307", "LU1291099718", "LU3086268573", "DE000ETFL284", "IE00BKWQ0Q14",
        "IE00B5BD5K76", "IE000ZQOIPB1", "LU3046617984", "IE00B60SWY32",
    ),
    **_isins(
        "stoxx_europe_600",
        "DE0002635307", "DE000A2QP4B6", "IE0004YCLDW1", "IE000XSFZL82", "LU0328475792",
        "FR0011550193", "FR0011550672", "IE00B60SWW18",
    ),
    **_isins(
        "sp500",
        "IE00B5BMR087", "IE0031442068", "IE00B3XXRP09", "IE00BFMXXD54", "IE00B3YCGJ38",
        "IE00BYML9W36", "IE00B5KQNG97", "IE000JZ473P7", "FR0011550177", "FR0011550185",
        "FR0011550680", "LU2993390504", "LU2993390769", "LU2993390686", "IE000Z9SJA06",
        "IE000UP2BIZ9", "IE00BD4TXW66", "IE00B7K93397", "IE00B4JY5R22", "LU2997383372",
        "IE000YIXESS9", "IE000AY6KP29", "DE000ETFL631", "IE000UBAW7M3", "IE000DN1DKF8",
    ),
    **_isins(
        "ftse_all_world",
        "IE00BK5BQT80", "IE00B3RBWM25", "IE000716YHJ7", "IE0000QLH0G6", "IE000L6ZMMC4",
        "IE00097WZHZ9", "IE000OL3XN92",
    ),
    **_isins(
        "euro_stoxx_50",
        "DE0005933956", "IE00B53L3W79", "IE0008471009", "LU0380865021", "LU0274211217",
        "FR0007054358", "LU1681047236", "LU1681047319", "LU1681047400", "IE00B4K6B022",
        "DE000ETFL029", "IE00B60SWX25", "IE00B5B5TG76", "FR0012740983", "FR0012739431",
        "LU0136234068", "LU0950668367", "IE000LOAUP03",
    ),
    **_isins(
        "nasdaq_100",
        "IE00B53SZB19", "DE000A0F5UF5", "IE0032077012", "IE00BFZXGZ54", "IE00BMFKG444",
        "IE000SB4G4I4", "IE0003RQ9F90", "DE000ETFL623", "IE000A2YGZU5",
    ),
    **_isins(
        "dax",
        "DE0005933931", "DE000A2QP331", "LU0274211480", "LU1349386927", "DE000ETFL011",
        "DE000ETFL060", "LU2611732046", "LU3206583067", "FR0010655712", "LU0252633754",
        "LU2090062436",
    ),
    # Currency-hedged share classes: exposed to the hedge currency.
    **_isins(f"{_HEDGED}EUR", "IE000TB15RC6", "IE00BYM11K57", "IE00BYVDRD78", "LU1600334798", "IE00BD34DK07"),
    **_isins(f"{_HEDGED}CHF", "IE000N6LBS91", "IE00BYM11L64", "LU1589327680", "IE00BD34DB16"),
    **_isins(f"{_HEDGED}GBP", "IE000KLSD4Y8", "IE00BD34DL14"),
    **_isins(f"{_HEDGED}USD", "IE00BYM11J43"),
}


@dataclass(frozen=True)
class CurrencyExposure:
    """How a holding's value splits across currencies (weights sum to 1)."""

    weights: dict[str, float]
    basis: str  # "index_lookthrough" | "hedged" | "listing"
    index_key: str | None = None
    as_of: date | None = None
    source: str | None = None
    stale: bool = False
    notes: list[str] = field(default_factory=list)

    def foreign_share(self, base_currency: str) -> float:
        """Share of the value not in *base_currency*."""
        return max(0.0, 1.0 - self.weights.get(base_currency.upper(), 0.0))


def _index_map(db: Session | None) -> dict[str, str]:
    merged = dict(ETF_INDEX)
    if db is None:
        return merged
    try:
        from app.foundation.settings import get_public_settings

        extra = get_public_settings(db).get("etf_index_map") or {}
        if isinstance(extra, dict):
            merged.update({str(k).upper(): str(v) for k, v in extra.items() if v})
    except Exception:  # noqa: BLE001 - a bad setting must not break verification
        logger.debug("etf_currency: etf_index_map setting unreadable", exc_info=True)
    return merged


def index_weights(db: Session | None, index_key: str, *, today: date | None = None) -> CurrencyExposure | None:
    """The stored (else seeded) currency weights of *index_key*."""
    key = PROXY_INDEX.get(index_key, index_key)
    notes = [f"{index_key} read through {key}"] if key != index_key else []
    weights: dict[str, float] | None = None
    as_of, source = _SEED_AS_OF, "seed: SPDR holdings 2026-09-28"
    if db is not None:
        try:
            from app.foundation.models.entities import IndexCurrencyWeights

            row = db.get(IndexCurrencyWeights, key)
        except Exception:  # noqa: BLE001 - table missing before its migration
            row = None
        if row is not None and row.weights_json:
            weights, as_of, source = dict(row.weights_json), row.as_of, row.source
    if weights is None:
        seeded = SEED.get(key)
        if seeded is None:
            return None
        weights = dict(seeded)
    total = sum(weights.values())
    if total <= 0:
        return None
    structural = key in ("sp500", "nasdaq_100", "euro_stoxx_50", "dax")
    stale = not structural and ((today or datetime.now(UTC).date()) - as_of) > STALE_AFTER
    return CurrencyExposure(
        weights={k: v / total for k, v in weights.items()},
        basis="index_lookthrough",
        index_key=index_key,
        as_of=None if structural else as_of,
        source="single-currency index" if structural else source,
        stale=stale,
        notes=notes,
    )


def holding_currency_exposure(
    db: Session | None,
    *,
    isin: str | None,
    asset_type: str | None,
    listing_currency: str,
    today: date | None = None,
) -> CurrencyExposure:
    """Currency weights of one holding: index look-through for a mapped ETF,
    the hedge currency for a hedged class, else its listing currency."""
    listing = CurrencyExposure(weights={listing_currency.upper(): 1.0}, basis="listing")
    if not isin or (asset_type or "").lower() not in ("etf", "fund"):
        return listing
    target = _index_map(db).get(isin.upper())
    if target is None:
        return listing
    if target.startswith(_HEDGED):
        return CurrencyExposure(weights={target[len(_HEDGED):].upper(): 1.0}, basis="hedged")
    return index_weights(db, target, today=today) or listing


def exposure_for_holding(db: Session | None, holding: Any, *, today: date | None = None) -> CurrencyExposure:
    """:func:`holding_currency_exposure` for a ``Holding``-shaped object.

    The listing currency is the one the holding is priced in (resolved from
    its ticker), not the booking currency: DKB books an AAPL position bought
    at Tradegate in EUR.
    """
    from app.foundation.data_backbone.listing_currency import resolve_currency

    ticker = getattr(holding, "ticker", None)
    listing = resolve_currency(db, ticker) if ticker else (getattr(holding, "currency", None) or "EUR").upper()
    return holding_currency_exposure(
        db,
        isin=getattr(holding, "isin", None),
        asset_type=getattr(holding, "asset_type", None),
        listing_currency=listing,
        today=today,
    )


@dataclass(frozen=True)
class IndexCountries:
    """An index's country weights (summing to 1) and where they came from."""

    index_key: str
    weights: dict[str, float]
    as_of: date | None
    source: str
    stale: bool = False
    read_through: str | None = None  # the proxy index whose weights these are


def index_country_weights(db: Session | None, index_key: str, *, today: date | None = None) -> IndexCountries | None:
    """The stored (else seeded) country weights of *index_key*."""
    key = PROXY_INDEX.get(index_key, index_key)
    weights: dict[str, float] | None = None
    as_of: date | None = _COUNTRY_SEED_AS_OF
    source = "SPDR holdings, seed"
    if db is not None:
        try:
            from app.foundation.models.entities import IndexCurrencyWeights

            row = db.get(IndexCurrencyWeights, key)
        except Exception:  # noqa: BLE001 - table or column missing before its migration
            row = None
        if row is not None and row.country_weights_json:
            weights, as_of, source = dict(row.country_weights_json), row.as_of, row.source
    if weights is None:
        seeded = COUNTRY_SEED.get(key)
        if seeded is None:
            return None
        weights = dict(seeded)
    total = sum(weights.values())
    if total <= 0:
        return None
    if key == "sp500":
        as_of, source = None, "single-country index"
    stale = as_of is not None and ((today or datetime.now(UTC).date()) - as_of) > STALE_AFTER
    return IndexCountries(
        index_key, {k: v / total for k, v in weights.items()}, as_of, source, stale,
        read_through=key if key != index_key else None,
    )


def etf_country_weights(db: Session | None, isin: str | None) -> IndexCountries | None:
    """Country weights of the index an ETF (by ISIN) tracks, if it is mapped.

    Currency-hedged share classes are mapped to their hedge, not their
    index, so they get none.
    """
    if not isin:
        return None
    target = _index_map(db).get(isin.upper())
    if target is None or target.startswith(_HEDGED):
        return None
    return index_country_weights(db, target)


# --- refresh ------------------------------------------------------------------

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_rows(content: bytes) -> list[dict[str, str | None]]:
    """Rows of an .xlsx file's first sheet as {column letter: text}."""
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            for item in ElementTree.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{_XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{_XLSX_NS}t")))
        sheet = sorted(n for n in book.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        out: list[dict[str, str | None]] = []
        for row in ElementTree.fromstring(book.read(sheet)).iter(f"{_XLSX_NS}row"):
            cells: dict[str, str | None] = {}
            for cell in row.iter(f"{_XLSX_NS}c"):
                match = re.match(r"[A-Z]+", cell.get("r") or "")
                if match is None:
                    continue
                kind, value = cell.get("t"), cell.find(f"{_XLSX_NS}v")
                if kind == "inlineStr":
                    text: str | None = "".join(t.text or "" for t in cell.iter(f"{_XLSX_NS}t"))
                elif value is None:
                    text = None
                elif kind == "s":
                    text = shared[int(value.text or 0)]
                else:
                    text = value.text
                cells[match.group()] = text
            out.append(cells)
        return out


@dataclass(frozen=True)
class SpdrHoldings:
    """What one SPDR holdings file says about its index (weights sum to 1)."""

    currencies: dict[str, float]
    countries: dict[str, float] | None
    as_of: date | None


def parse_spdr_holdings(content: bytes) -> tuple[dict[str, float], date | None]:
    """Currency weights (summing to 1) and the as-of date of an SPDR holdings file."""
    parsed = parse_spdr_file(content)
    return parsed.currencies, parsed.as_of


def parse_spdr_file(content: bytes) -> SpdrHoldings:
    """Currency and country weights and the as-of date of an SPDR holdings file.

    Countries come from the "Trade Country Name" column; None when a file
    has no such column.
    """
    rows = _xlsx_rows(content)
    as_of: date | None = None
    header_index = None
    for i, row in enumerate(rows):
        label = (row.get("A") or "").strip().lower()
        if label.startswith("holdings as of") and row.get("B"):
            try:
                as_of = datetime.strptime(str(row["B"]).strip(), "%d-%b-%Y").date()
            except ValueError:
                as_of = None
        if "Currency" in row.values() and "Percent of Fund" in row.values():
            header_index = i
            break
    if header_index is None:
        raise ValueError("no holdings header (Currency / Percent of Fund) in file")
    columns = {v: k for k, v in rows[header_index].items() if v}
    currencies: dict[str, float] = defaultdict(float)
    countries: dict[str, float] = defaultdict(float)
    country_column = columns.get("Trade Country Name")
    for row in rows[header_index + 1:]:
        try:
            weight = float(row.get(columns["Percent of Fund"]) or "")
        except ValueError:
            continue
        currencies[(row.get(columns["Currency"]) or "").strip().upper() or "OTHER"] += weight
        if country_column is not None:
            countries[(row.get(country_column) or "").strip() or "Other"] += weight
    total = sum(currencies.values())
    if total <= 0:
        raise ValueError("holdings file has no weights")
    return SpdrHoldings(
        currencies={k: v / total for k, v in currencies.items()},
        countries={k: v / total for k, v in countries.items()} if country_column is not None else None,
        as_of=as_of,
    )


def _download(code: str) -> bytes:
    import httpx

    response = httpx.get(
        _SPDR_URL.format(code=code),
        headers={"User-Agent": "Quantfolio/1.0 (index currency weights)"},
        timeout=60.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise ValueError(f"{code}: not an xlsx file")
    return response.content


def refresh_index_currency_weights(
    db: Session, *, fetch: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Reload each index's currency and country weights from its SPDR holdings file.

    One index failing leaves its stored (or seeded) weights in place.
    Commits per index.
    """
    from app.foundation.models.entities import IndexCurrencyWeights

    get = fetch or _download
    report: dict[str, Any] = {"updated": [], "failed": {}}
    for index_key, code in _SPDR_FILES.items():
        try:
            parsed = parse_spdr_file(get(code))
            row = db.get(IndexCurrencyWeights, index_key)
            if row is None:
                row = IndexCurrencyWeights(index_key=index_key)
                db.add(row)
            row.weights_json = {k: round(v, 6) for k, v in sorted(parsed.currencies.items(), key=lambda kv: -kv[1])}
            if parsed.countries is not None:
                row.country_weights_json = {
                    k: round(v, 6) for k, v in sorted(parsed.countries.items(), key=lambda kv: -kv[1])
                }
            row.as_of = parsed.as_of or datetime.now(UTC).date()
            row.source = f"SPDR holdings {code}"
            row.fetched_at = datetime.now(UTC)
            db.commit()
            report["updated"].append(index_key)
        except Exception as exc:  # noqa: BLE001 - one index must not stop the others
            db.rollback()
            report["failed"][index_key] = str(exc)
            logger.warning("etf_currency: refresh of %s from %s failed: %s", index_key, code, exc)
    logger.info("etf_currency refresh: %s", report)
    return report
