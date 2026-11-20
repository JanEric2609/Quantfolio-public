"""Source withholding tax on dividends, by the issuer's ISIN country prefix.

What a German resident actually loses at source before the dividend reaches
the broker (statutory / treaty rates as of 2026, treaty rates where the
German treaty lowers the domestic one). Used to credit paper dividends net,
so a paper sleeve is not flattered against the real book. Reclaims are not
modelled, nor are franked-dividend differences (AU). German KapESt is not a
source tax here and counts as 0 for this comparison, as do Irish/Luxembourg
domiciled funds (UCITS distributions are not taxed at source).

Source: OECD/BZSt treaty tables and national statutory rates, owner decision
2026-10-05; TW/KR and the Luxembourg equity rate added 2026-10-06. Revisit
each January.
"""
from __future__ import annotations

import re

# ISIN country prefix -> source withholding tax rate on dividends. Irish and
# Luxembourg *funds* (UCITS ETFs) are not taxed at source; their *equities*
# are (``EQUITY_RATE_OF_FUND_DOMICILE``).
WITHHOLDING_TAX_BY_COUNTRY: dict[str, float] = {
    "US": 0.15,
    "NL": 0.15,
    "FR": 0.128,
    "CH": 0.35,
    "IE": 0.0,  # funds only; equities: EQUITY_RATE_OF_FUND_DOMICILE
    "LU": 0.0,  # funds only; equities: EQUITY_RATE_OF_FUND_DOMICILE
    "DE": 0.0,
    "GB": 0.0,
    "JP": 0.15315,
    "CA": 0.15,
    "DK": 0.27,
    "ES": 0.19,
    "IT": 0.26,
    "SE": 0.30,
    "NO": 0.25,
    "FI": 0.35,
    "BE": 0.30,
    "AT": 0.275,
    "AU": 0.15,
    "TW": 0.21,  # domestic rate; the treaty allows 10 %, the rest needs a reclaim
    "KR": 0.22,  # domestic rate incl. local tax; the treaty allows 15 %
}
DEFAULT_WITHHOLDING_TAX = 0.15
IRISH_EQUITY_DWT = 0.25
# Fund domiciles whose shares (not funds) are taxed at source.
EQUITY_RATE_OF_FUND_DOMICILE = {"IE": IRISH_EQUITY_DWT, "LU": 0.15}

_FUND_NAME = re.compile(
    r"\b(etf|etc|etp|ucits|fund|fonds|ishares|vanguard|xtrackers|amundi|spdr|lyxor|invesco)\b",
    re.IGNORECASE,
)
_FUND_ASSET_TYPES = {"etf", "fund", "mutual_fund", "money_market", "bond_fund", "etc", "etp"}


def looks_like_fund(name: str | None = None, asset_type: str | None = None) -> bool:
    """True for an ETF/fund, judged by the stored asset type or the instrument name."""
    if (asset_type or "").strip().lower() in _FUND_ASSET_TYPES:
        return True
    return bool(name) and _FUND_NAME.search(str(name)) is not None


def withholding_rate(isin: str | None, *, is_fund: bool | None = None) -> float:
    """Source tax rate for a dividend of the issuer with this ISIN.

    15 % when the ISIN is unknown. Irish and Luxembourg issuers are 0 % only
    when they are a fund (``is_fund=True``); an equity, or an instrument whose
    nature is unknown (``None``/``False``), pays 25 % (IE) or 15 % (LU).
    """
    code = (isin or "").strip().upper()[:2]
    if code in EQUITY_RATE_OF_FUND_DOMICILE:
        return 0.0 if is_fund else EQUITY_RATE_OF_FUND_DOMICILE[code]
    if len(code) == 2 and code.isalpha():
        return WITHHOLDING_TAX_BY_COUNTRY.get(code, DEFAULT_WITHHOLDING_TAX)
    return DEFAULT_WITHHOLDING_TAX
