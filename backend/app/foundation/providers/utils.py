"""Shared utilities for provider implementations."""

from __future__ import annotations

# Known yfinance exchange suffixes for non-US listings.
# These are NOT share class identifiers — they indicate the exchange.
# Single-letter suffixes like .A / .B are class shares and are NOT included.
_EXCHANGE_SUFFIXES: set[str] = {
    "L",    # London Stock Exchange
    "AS",   # Euronext Amsterdam
    "DE",   # Xetra / Deutsche Börse
    "PA",   # Euronext Paris
    "MI",   # Borsa Italiana
    "TO",   # Toronto Stock Exchange
    "V",    # TSX Venture Exchange
    "CO",   # Copenhagen
    "ST",   # Stockholm
    "HE",   # Helsinki
    "VI",   # Vienna
    "MC",   # Madrid
    "BR",   # Euronext Brussels
    "LS",   # Luxembourg Stock Exchange
    "IR",   # Irish Stock Exchange
    "MU",   # Munich
    "F",    # Frankfurt
    "BE",   # Berlin
    "HM",   # Hamburg
    "HA",   # Hanover
    "SG",   # Stuttgart
    "DU",   # Düsseldorf
    "SW",   # SIX Swiss Exchange
    "OL",   # Oslo Børs
    # Outside Europe. Without these a Tokyo listing like 7203.T counted as a
    # US listing: US-only providers were asked for it and Discover regressed
    # its yen returns on US factors against SPY.
    "T",    # Tokyo Stock Exchange
    "HK",   # Hong Kong
    "KS",   # Korea Exchange
    "AX",   # ASX
}


# Map yfinance exchange suffixes to ISO 10383 MIC codes for providers
# (e.g. Twelve Data) that address non-US listings via a `mic_code` parameter.
_SUFFIX_TO_MIC: dict[str, str] = {
    "L": "XLON",
    "AS": "XAMS",
    "DE": "XETR",
    "PA": "XPAR",
    "MI": "XMIL",
    "TO": "XTSE",
    "V": "XTSX",
    "CO": "XCSE",
    "ST": "XSTO",
    "HE": "XHEL",
    "VI": "XWBO",
    "MC": "XMAD",
    "BR": "XBRU",
    "LS": "XLUX",
    "IR": "XDUB",
    "MU": "XMUN",
    "F": "XFRA",
    "BE": "XBER",
    "HM": "XHAM",
    "HA": "XHAN",
    "SG": "XSTU",
    "DU": "XDUS",
    "SW": "XSWX",
    "OL": "XOSL",
    "T": "XTKS",
    "HK": "XHKG",
    "KS": "XKRX",
    "AX": "XASX",
}

# MIC -> factor region of Ken French's data library (listing_region).
_EUROPE_MICS = {
    "XLON", "XAMS", "XETR", "XPAR", "XMIL", "XCSE", "XSTO", "XHEL", "XWBO", "XMAD", "XBRU",
    "XLUX", "XDUB", "XMUN", "XFRA", "XBER", "XHAM", "XHAN", "XSTU", "XDUS", "XSWX", "XOSL",
}


def split_exchange_suffix(symbol: str) -> tuple[str, str | None]:
    """Split a yfinance-style symbol into (base, MIC code or None).

    Examples:
        >>> split_exchange_suffix("EOAN.DE")
        ('EOAN', 'XETR')
        >>> split_exchange_suffix("AAPL")
        ('AAPL', None)
        >>> split_exchange_suffix("BRK.B")
        ('BRK.B', None)  # Class shares preserved
    """
    if "." in symbol:
        parts = symbol.rsplit(".", 1)
        mic = _SUFFIX_TO_MIC.get(parts[1].upper())
        if mic is not None:
            return parts[0], mic
    return symbol, None


def is_us_listing(symbol: str) -> bool:
    """True when the symbol carries no known non-US exchange suffix.

    US-only providers (Alpaca, Databento) must skip non-US listings instead
    of stripping the suffix: a stripped EU symbol can collide with a real US
    ticker (SHEL.AS → SHEL) and silently return the wrong price.
    """
    return split_exchange_suffix(symbol)[1] is None


def listing_region(symbol: str) -> str:
    """``"us"``, ``"europe"``, ``"japan"`` or ``"other"`` from the listing suffix.

    Picks the Fama-French factor file a single security is regressed on:
    factors must trade in the stock's own session.
    """
    mic = split_exchange_suffix(symbol)[1]
    if mic is None:
        return "us"
    if mic in _EUROPE_MICS:
        return "europe"
    if mic == "XTKS":
        return "japan"
    return "other"


def strip_exchange_suffix(symbol: str) -> str:
    """Remove yfinance-style exchange suffix from a ticker symbol.

    Many providers (OpenBB, Finnhub, Alpha Vantage) do not understand
    yfinance's exchange suffix format (e.g. ``EUNL.DE``, ``SHEL.AS``, ``VWCE.DE``).
    This helper strips the suffix so providers receive just the base symbol.

    Only strips known exchange suffixes — share class suffixes like ``.A``,
    ``.B``, ``.UN`` are preserved.

    Examples:
        >>> strip_exchange_suffix("EUNL.DE")
        'EUNL'
        >>> strip_exchange_suffix("SHEL.AS")
        'SHEL'
        >>> strip_exchange_suffix("VWCE.DE")
        'VWCE'
        >>> strip_exchange_suffix("AAPL")
        'AAPL'
        >>> strip_exchange_suffix("BRK.B")
        'BRK.B'  # Class shares preserved
    """
    if "." in symbol:
        parts = symbol.rsplit(".", 1)
        suffix = parts[1].upper()
        if suffix in _EXCHANGE_SUFFIXES:
            return parts[0]
    return symbol


# Listing suffix -> the unit the venue's prices are quoted in. Only a
# FALLBACK: a venue can list one security in several currencies (the LSE
# quotes IWDA.L and CSPX.L in USD, IHG.L and CPG.L moved to USD, CBE3.L is
# EUR, SHEL.L is in pence), so the provider's own metadata wins wherever it
# exists (app.foundation.data_backbone.listing_currency). On 2026-09-28 this
# rule matched yfinance for 765 of the 780 symbols with stored bars.
_SUFFIX_QUOTE_CURRENCY: dict[str, str] = {
    "L": "GBp",
    "SW": "CHF", "VX": "CHF",
    "ST": "SEK", "CO": "DKK", "OL": "NOK", "IC": "ISK",
    "T": "JPY", "HK": "HKD", "KS": "KRW", "AX": "AUD", "TO": "CAD", "V": "CAD",
}
_EURO_SUFFIXES = frozenset({
    "DE", "F", "BE", "MU", "STU", "HM", "HA", "DU", "SG",
    "PA", "AS", "BR", "LS", "MC", "MI", "VI", "IR", "HE",
})
# Index quote currencies, for the handful the app stores.
_INDEX_CURRENCY: dict[str, str] = {
    "^STOXX50E": "EUR", "^STOXX": "EUR", "^GDAXI": "EUR", "^FCHI": "EUR",
    "^FTSE": "GBP", "^N225": "JPY", "^SSMI": "CHF",
}

# Minor units: prices quoted in hundredths of the currency. Case matters for
# GBp (pence) versus GBP (pounds), so these are matched case-sensitively.
_MINOR_UNITS: dict[str, tuple[str, float]] = {
    "GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "GBx": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0), "ZAC": ("ZAR", 100.0), "ILA": ("ILS", 100.0),
}


def currency_unit(code: str | None) -> tuple[str, float]:
    """``(ISO currency, divisor)`` for a quote unit: ``"GBp"`` -> ``("GBP", 100.0)``.

    Returns ``("", 1.0)`` for an empty code. Upper-casing a quote unit before
    this call would turn pence into pounds, a silent 100x.
    """
    raw = (code or "").strip()
    if raw in _MINOR_UNITS:
        return _MINOR_UNITS[raw]
    return raw.upper(), 1.0


def listing_quote_currency(symbol: str) -> str:
    """Best-effort quote unit from the symbol alone (``"GBp"`` for an LSE line).

    FX pairs (``USDEUR=X``) are quoted in their second currency, indices by
    the small table above, anything else without a known suffix in USD.
    """
    s = symbol.strip()
    up = s.upper()
    if up.endswith("=X"):
        pair = up[:-2]
        return pair[-3:] if len(pair) >= 3 else "USD"
    if up.startswith("^"):
        return _INDEX_CURRENCY.get(up, "USD")
    if "." in up:
        suffix = up.rsplit(".", 1)[1]
        if suffix in _SUFFIX_QUOTE_CURRENCY:
            return _SUFFIX_QUOTE_CURRENCY[suffix]
        if suffix in _EURO_SUFFIXES:
            return "EUR"
    return "USD"


def listing_currency(symbol: str) -> str:
    """The ISO currency of :func:`listing_quote_currency` (pence -> GBP)."""
    return currency_unit(listing_quote_currency(symbol))[0]
