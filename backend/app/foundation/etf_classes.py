"""Asset class and tracked index of an exchange-traded product (plan 2026-10-12, WP4a).

Discover used to treat every ETF as "global equity": bonds, an €STR money
market fund and gold were all benchmarked against MSCI World, so CBE3 showed
-14.5 %/yr "excess" and gold +11 %/yr (audit H5). The lane also ranked nine
S&P 500 clones against each other by momentum (H6). This module gives each
product one class and one normalised index key, from its name (justETF has no
index field in its overview), with a curated override table for the funds
the app recommends or benchmarks against.

Network-free and deterministic; pinned by ``tests/test_etf_classes.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

EtfClass = Literal[
    "equity_broad",
    "equity_region",
    "equity_factor",
    "equity_sector_theme",
    "bond_govt_eur_short",
    "bond_govt_eur_mid",
    "bond_govt_eur_long",
    "bond_aggregate_eur_hedged",
    "bond_other",
    "money_market",
    "gold",
    "commodity_other",
    "leveraged_inverse",
    "other",
]

EQUITY_CLASSES = frozenset({"equity_broad", "equity_region", "equity_factor", "equity_sector_theme"})
BOND_CLASSES = frozenset({
    "bond_govt_eur_short", "bond_govt_eur_mid", "bond_govt_eur_long", "bond_aggregate_eur_hedged", "bond_other",
})


@dataclass(frozen=True)
class ReferenceFund:
    """A curated fund the app benchmarks a class against or suggests for it."""

    etf_class: str
    symbol: str
    isin: str
    name: str
    index: str


# Verified on justETF / the issuer on 2026-10-12 (ISIN, legal form, listing).
# CBE3.L is the EUR line on the LSE (the fund has no Xetra listing).
REFERENCE_FUNDS: dict[str, ReferenceFund] = {
    "equity_broad": ReferenceFund("equity_broad", "EUNL.DE", "IE00B4L5Y983",
                                  "iShares Core MSCI World UCITS ETF (Acc)", "MSCI World"),
    "bond_govt_eur_short": ReferenceFund("bond_govt_eur_short", "CBE3.L", "IE00B3VTMJ91",
                                         "iShares € Govt Bond 1-3yr UCITS ETF (Acc)", "Bloomberg Euro Govt 1-3Y"),
    "bond_govt_eur_mid": ReferenceFund("bond_govt_eur_mid", "EUNH.DE", "IE00B4WXJJ64",
                                       "iShares Core € Govt Bond UCITS ETF (Dist)", "Bloomberg Euro Govt"),
    "bond_govt_eur_long": ReferenceFund("bond_govt_eur_long", "EUNH.DE", "IE00B4WXJJ64",
                                        "iShares Core € Govt Bond UCITS ETF (Dist)", "Bloomberg Euro Govt"),
    "bond_aggregate_eur_hedged": ReferenceFund(
        "bond_aggregate_eur_hedged", "EUNA.DE", "IE00BDBRDM35",
        "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)", "Bloomberg Global Aggregate (EUR hedged)",
    ),
    "money_market": ReferenceFund("money_market", "XEON.DE", "LU0290358497",
                                  "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", "€STR"),
    "gold": ReferenceFund("gold", "4GLD.DE", "DE000A0S9GB0", "Xetra-Gold (ETC)", "Gold"),
}

# Curated class/index per ISIN; wins over the name rules. Index keys match the
# name-derived ones so two funds on one exposure share a key.
_OVERRIDES: dict[str, tuple[str, str]] = {
    "IE00B4L5Y983": ("equity_broad", "MSCI World"),
    "IE00BK5BQT80": ("equity_broad", "FTSE All-World"),
    "IE00B3RBWM25": ("equity_broad", "FTSE All-World"),
    "IE00BJ0KDQ92": ("equity_broad", "MSCI World"),
    "IE00B6R52259": ("equity_broad", "MSCI ACWI"),
    "IE00BKM4GZ66": ("equity_region", "MSCI EM IMI"),
    "IE00B4L5YC18": ("equity_region", "MSCI EM"),
    "IE00B3VTMJ91": ("bond_govt_eur_short", "Euro Govt 1-3Y"),
    "IE00B4WXJJ64": ("bond_govt_eur_mid", "Euro Govt"),
    "IE00BDBRDM35": ("bond_aggregate_eur_hedged", "Bloomberg Global Aggregate (EUR hedged)"),
    "LU0290358497": ("money_market", "€STR"),
    "DE000A0S9GB0": ("gold", "Gold"),
    "DE000A1E0HR8": ("gold", "Gold"),  # Xetra-Gold's sibling EUWAX Gold II
}



def curated_index_key(isin: str | None) -> str | None:
    """The curated index key for an ISIN on the override table, else None."""
    entry = _OVERRIDES.get((isin or "").upper())
    return entry[1] if entry else None


def is_curated(isin: str | None) -> bool:
    return (isin or "").upper() in _OVERRIDES


_LEVERAGED = re.compile(
    r"(\bleveraged\b|\binverse\b|\b[2-5]x\b|-[1-5]x\b|\bdaily short\b|\bshort daily\b|\bultra\b|\bbear\b)", re.I,
)
_MONEY_MARKET = re.compile(
    r"(overnight|€str|\bestr\b|eonia|money market|geldmarkt|\bcash\b(?!\s*flow)|\bt-bill|treasury bill|0-3 ?m|0-1 ?m)", re.I,
)
_GOLD = re.compile(r"\bgold\b", re.I)
_GOLD_EQUITY = re.compile(r"(miner|mining|producer|explorer)", re.I)
_COMMODITY = re.compile(r"(commodit|silver|platinum|palladium|\bcrude\b|\boil\b|\bbrent\b|\bwti\b|copper|metals?\b)", re.I)
_BOND = re.compile(
    r"(bond|govt|government|treasury|treasuries|aggregate|corporate|\bcorp\b|inflation[- ]linked|\bgilt|"
    r"\bbund\b|high yield|\bhy\b|sovereign|credit|fixed income|anleihe|rente|\btips\b)", re.I,
)
_EUR_GOVT = re.compile(r"(€|eur\b|euro|eurozone|euro area).*(govt|government|sovereign)|"
                       r"(govt|government|sovereign).*(€|\beur\b|euro|eurozone)", re.I)
_SHORT = re.compile(r"(\b0-[1-3]|\b1-3\b|1-3 ?y|1-3yr|short|kurzlauf)", re.I)
_MID = re.compile(r"(\b3-5\b|\b3-7\b|\b5-7\b|\b5-10\b|3-5 ?y|3-7 ?y|5-7 ?y)", re.I)
_LONG = re.compile(r"(\b7-10\b|\b10-15\b|\b15\+|\b20\+|\b25\+|10\+|long|7-10 ?y|10-15 ?y)", re.I)
_AGG_EUR_HEDGED = re.compile(r"aggregate.*(eur|€).*hedged|(eur|€).*hedged.*aggregate|euro aggregate", re.I)

_FACTOR = re.compile(
    r"(\bvalue\b|momentum|quality|min(imum)? vol|low vol|small ?cap|mid ?cap|dividend|equal weight|"
    r"multi[- ]?factor|\besg\b.*screened|enhanced|high yield equity|\bsize\b|growth\b)", re.I,
)
_SECTOR_THEME = re.compile(
    r"(technology|information tech|miners?\b|mining|\btech\b|health ?care|pharma|biotech|financials|\bbanks?\b|insurance|energy|"
    r"utilities|clean|renewable|solar|\bwind\b|\bwater\b|robot|automation|semiconductor|cyber|artificial intelligence|"
    r"\bai\b|defen[cs]e|aerospace|consumer|industrials|materials|real estate|reit|property|infrastructure|"
    r"blockchain|crypto|bitcoin|e-?sports|gaming|battery|hydrogen|lithium|cloud|digital|metaverse|"
    r"telecom|communication|food|agricultur|timber|uranium|nuclear|space|genomic|healthcare|innovation|"
    r"millennial|ageing|aging|smart city|mobility|electric vehicle|\bev\b|climate|transition)", re.I,
)
_BROAD = re.compile(
    r"(msci world\b|all[- ]world|\bacwi\b|all country world|ftse developed world|global all cap|"
    r"msci world imi|ftse global all cap|world equity)", re.I,
)


def classify_etf(name: str, *, isin: str | None = None, strategy: str | None = None) -> str:
    """The product's class from its curated override, else its name."""
    if isin and isin.upper() in _OVERRIDES:
        return _OVERRIDES[isin.upper()][0]
    n = name or ""
    if (strategy and "long-only" not in strategy.lower()) or _LEVERAGED.search(n):
        return "leveraged_inverse"
    if _MONEY_MARKET.search(n) and not re.search(r"\b(1-3|3-5|7-10)\b", n):
        # USD/GBP cash (T-bills, a hedged €STR share class) is a currency
        # position for a EUR investor, not cash.
        if _hedge_currency(n) not in (None, "EUR") or re.search(r"\b(usd|gbp|chf) treasury", n, re.I):
            return "bond_other"
        return "money_market"
    if _GOLD.search(n) and not _GOLD_EQUITY.search(n):
        return "gold"
    if _COMMODITY.search(n) and not re.search(r"(equity|stock|producer|miner)", n, re.I):
        return "commodity_other"
    if _BOND.search(n):
        if _AGG_EUR_HEDGED.search(n):
            return "bond_aggregate_eur_hedged"
        if _EUR_GOVT.search(n) and not re.search(r"(inflation|linked|floating)", n, re.I):
            if _SHORT.search(n):
                return "bond_govt_eur_short"
            if _LONG.search(n):
                return "bond_govt_eur_long"
            if _MID.search(n):
                return "bond_govt_eur_mid"
            return "bond_govt_eur_mid"  # a broad € govt index averages ~7y
        return "bond_other"
    if _SECTOR_THEME.search(n):
        return "equity_sector_theme"
    if _FACTOR.search(n):
        return "equity_factor"
    if _BROAD.search(n):
        return "equity_broad"
    if re.search(r"(etf|ucits|index|msci|ftse|s&p|stoxx|dax|nasdaq|russell|nikkei|topix|solactive)", n, re.I):
        return "equity_region"
    return "other"


# (pattern, canonical index) in priority order; the first match wins.
_INDEX_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"s&p 500 (equal weight)", "S&P 500 Equal Weight"),
    (r"s&p 500", "S&P 500"),
    (r"nasdaq[- ]?100", "Nasdaq-100"),
    (r"msci world small ?cap", "MSCI World Small Cap"),
    (r"msci world (value|momentum|quality|min(imum)? vol\w*|minimum volatility)", "MSCI World {1}"),
    (r"msci world (ex[- ]?usa?)", "MSCI World ex USA"),
    (r"msci world information tech\w*", "MSCI World Information Technology"),
    (r"msci world imi", "MSCI World IMI"),
    (r"msci world", "MSCI World"),
    (r"ftse all[- ]world", "FTSE All-World"),
    (r"msci acwi imi", "MSCI ACWI IMI"),
    (r"msci acwi|msci all country world", "MSCI ACWI"),
    (r"msci (em|emerging markets) imi", "MSCI EM IMI"),
    (r"msci (em|emerging markets)", "MSCI EM"),
    (r"ftse emerging", "FTSE Emerging"),
    (r"stoxx europe 600", "STOXX Europe 600"),
    (r"euro stoxx 50|eurostoxx 50", "EURO STOXX 50"),
    (r"msci emu", "MSCI EMU"),
    (r"msci europe", "MSCI Europe"),
    (r"ftse developed europe", "FTSE Developed Europe"),
    (r"ftse 100", "FTSE 100"),
    (r"ftse 250", "FTSE 250"),
    (r"msci usa", "MSCI USA"),
    (r"msci japan", "MSCI Japan"),
    (r"topix", "TOPIX"),
    (r"nikkei 225", "Nikkei 225"),
    (r"msci china", "MSCI China"),
    (r"msci india", "MSCI India"),
    (r"dax", "DAX"),
    (r"(€|eur|euro) govt bond 1-3", "Bloomberg Euro Govt 1-3Y"),
    (r"global aggregate", "Bloomberg Global Aggregate"),
)


_HEDGE = re.compile(r"\b(eur|usd|gbp|chf|€)[- ]?(daily )?hedged|hedged[- ]?(to )?\(?(eur|usd|gbp|chf)\b|\bh(eur|usd|gbp|chf)\b", re.I)


def _hedge_currency(name: str) -> str | None:
    m = _HEDGE.search(name or "")
    if not m:
        return None
    cur = next(g for g in m.groups() if g and g.lower() not in ("daily ", "to "))
    return "EUR" if cur in ("€", "eur", "EUR") else cur.upper()


# Index keys that name a broad market: a factor or sector fund whose name
# contains one ("FTSE All-World High Dividend Yield", "S&P 500 Information
# Technology") is a different exposure and must not share its key.
_BROAD_KEYS = frozenset({
    "S&P 500", "Nasdaq-100", "MSCI World", "MSCI World IMI", "FTSE All-World", "MSCI ACWI", "MSCI ACWI IMI",
    "MSCI EM", "MSCI EM IMI", "FTSE Emerging", "STOXX Europe 600", "EURO STOXX 50", "MSCI EMU", "MSCI Europe",
    "FTSE Developed Europe", "FTSE 100", "FTSE 250", "MSCI USA", "MSCI Japan", "TOPIX", "Nikkei 225",
    "MSCI China", "MSCI India", "DAX", "MSCI World ex USA",
})


def _stripped(n: str) -> str:
    stripped = re.sub(
        r"\b(ishares|vanguard|xtrackers|amundi|spdr|state street|invesco|lyxor|hsbc|ubs|l&g|wisdomtree|franklin|"
        r"jpmorgan|j\.p\. morgan|vaneck|global x|deka|ossiam|core|prime|edge|ucits|etf|etc|acc|dist|accumulating|"
        r"distributing|1c|2c|1d|usd|eur|gbp|ii|ie|plc|s\.a\.|unhedged|hedged|daily|securities|a|c)\b",
        " ", n,
    )
    stripped = re.sub(r"[()\-]", " ", stripped)
    return " ".join(stripped.split()).title()


def index_key(name: str, *, isin: str | None = None) -> str:
    """Normalised tracked-index name; the fund name stripped of wrappers when unknown.

    Two funds with the same key are the same exposure: only one of them is
    ever suggested (D4). Hedged and unhedged share classes differ in
    currency exposure, so the hedge currency stays part of the key.
    """
    if isin and isin.upper() in _OVERRIDES:
        return _OVERRIDES[isin.upper()][1]
    raw = name or ""
    n = raw.lower()
    hedge = _hedge_currency(raw)
    suffix = f" ({hedge} hedged)" if hedge else ""
    cls = classify_etf(raw)
    if cls == "money_market":
        return "€STR" + suffix
    if cls == "gold":
        return "Gold" + suffix
    if cls in ("bond_govt_eur_short", "bond_govt_eur_mid", "bond_govt_eur_long"):
        m = re.search(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*(y|yr|year)?", n)
        span = f" {m.group(1)}-{m.group(2)}Y" if m else ""
        if not m and re.search(r"(\d{1,2})\+", n):
            span = " " + re.search(r"(\d{1,2})\+", n).group(0) + "Y"  # type: ignore[union-attr]
        return "Euro Govt" + span + suffix
    for pattern, canonical in _INDEX_PATTERNS:
        m = re.search(pattern, n)
        if m:
            if "{1}" in canonical:
                canonical = canonical.replace("{1}", m.group(1).title())
            if cls in ("equity_factor", "equity_sector_theme") and canonical in _BROAD_KEYS:
                break
            return canonical + suffix
    return _stripped(n) + suffix


def benchmark_symbol_for(etf_class: str) -> str | None:
    """The D3 benchmark for an ETF class; ``ESTR`` for money market, None for gold/commodities."""
    if etf_class in EQUITY_CLASSES or etf_class in ("other", "leveraged_inverse"):
        return REFERENCE_FUNDS["equity_broad"].symbol
    if etf_class == "money_market":
        return ESTR_BENCHMARK
    if etf_class in ("gold", "commodity_other"):
        return None
    if etf_class == "bond_other":
        return REFERENCE_FUNDS["bond_aggregate_eur_hedged"].symbol
    ref = REFERENCE_FUNDS.get(etf_class)
    return ref.symbol if ref else None


#: Pseudo-symbol of the synthetic €STR total-return benchmark (compounded daily).
ESTR_BENCHMARK = "ESTR"


#: Broad indices whose funds count as diversified for a single-position cap (D6).
DIVERSIFIED_INDEX_KEYS = frozenset({
    "MSCI World", "MSCI World IMI", "MSCI ACWI", "MSCI ACWI IMI", "FTSE All-World", "S&P 500",
    "STOXX Europe 600", "MSCI EM IMI", "MSCI EM", "FTSE Developed World",
})
#: A fund with at least this many holdings is diversified whatever its name.
DIVERSIFIED_MIN_HOLDINGS = 100


def is_diversified_fund(name: str | None, *, isin: str | None = None, holdings_count: int | None = None) -> bool:
    """True for a broad index fund: one position, but no single-issuer risk (D6)."""
    if holdings_count is not None and holdings_count >= DIVERSIFIED_MIN_HOLDINGS:
        return True
    if not name and not isin:
        return False
    if classify_etf(name or "", isin=isin) == "leveraged_inverse":
        return False
    key = index_key(name or "", isin=isin)
    return re.sub(r" \([A-Z]{3} hedged\)$", "", key) in DIVERSIFIED_INDEX_KEYS
