"""Deterministic growth/value style classification for the Discover dossier.

ADR 0014 §6: the dossier prompt used to hand the LLM a ticker and a large
trailing return and let it *guess* a style/factor mandate — which produced the
"IS3S.DE (MSCI World Value Factor) described as growth" defect. Style is a
deterministic function of an index's stated mandate and, failing that, its
holdings' known sector/style tilt — never something the LLM should re-derive.
This module computes it in Python and the result is passed to the LLM as an
established fact.

Same maintenance-surface tradeoff already accepted for the bond/money-market
keyword vocabulary in ``app.foundation.fund_class``: the proxy lists here need
occasional review as holdings/index-naming drift, in exchange for being
inspectable and unit-testable instead of an opaque model inference.
"""
from __future__ import annotations

# Index/fund-name keywords, checked first — an explicit factor mandate in the
# name is the strongest signal available (e.g. "MSCI World Value Factor",
# "Nasdaq-100 Growth"). Longer/more specific tokens are irrelevant here since
# growth/value/quality/momentum rarely co-occur in one index name.
_INDEX_NAME_KEYWORDS: dict[str, str] = {
    "value": "value",
    "growth": "growth",
    "momentum": "momentum",
    "quality": "quality",
    "min vol": "low_volatility",
    "minimum volatility": "low_volatility",
    "low volatility": "low_volatility",
}

# Top-10-holdings proxy lists — the actual tickers the ADR verified for the
# five disputed dossiers (IS3S.DE -> Micron/Cisco/Verizon/Toyota/AT&T = value;
# XAIX.DE -> Microsoft/Amazon/Apple/NVIDIA/Alphabet = growth), extended with
# other widely-held mega-caps in the same style cluster. Matched against
# holdings tickers/names case-insensitively.
_GROWTH_HOLDINGS = {
    "MSFT", "MICROSOFT", "AMZN", "AMAZON", "AAPL", "APPLE", "NVDA", "NVIDIA",
    "GOOGL", "GOOG", "ALPHABET", "META", "TSLA", "TESLA", "AVGO", "BROADCOM",
    "NFLX", "NETFLIX", "ADBE", "ADOBE", "CRM", "SALESFORCE",
}
_VALUE_HOLDINGS = {
    "MU", "MICRON", "CSCO", "CISCO", "VZ", "VERIZON", "TM", "TOYOTA", "T",
    "AT&T", "XOM", "EXXON", "CVX", "CHEVRON", "JPM", "JPMORGAN", "PG",
    "PROCTER", "JNJ", "JOHNSON", "KO", "COCA-COLA", "PFE", "PFIZER",
}

# Fraction of top-10 holdings that must fall in one cluster to call it —
# below this, holdings are too mixed to classify from the top-10 alone.
_HOLDINGS_MAJORITY_THRESHOLD = 0.5


def _match_index_name(index_name: str | None) -> str | None:
    if not index_name:
        return None
    lowered = index_name.lower()
    for keyword, style in _INDEX_NAME_KEYWORDS.items():
        if keyword in lowered:
            return style
    return None


def _match_holdings(holdings: list[dict] | None) -> str | None:
    if not holdings:
        return None
    growth_hits = 0
    value_hits = 0
    counted = 0
    for h in holdings:
        ticker = str(h.get("ticker") or "").strip().upper()
        name = str(h.get("name") or "").strip().upper()
        if ticker in _GROWTH_HOLDINGS or name in _GROWTH_HOLDINGS:
            growth_hits += 1
            counted += 1
        elif ticker in _VALUE_HOLDINGS or name in _VALUE_HOLDINGS:
            value_hits += 1
            counted += 1
    if counted == 0:
        return None
    if growth_hits / len(holdings) >= _HOLDINGS_MAJORITY_THRESHOLD:
        return "growth"
    if value_hits / len(holdings) >= _HOLDINGS_MAJORITY_THRESHOLD:
        return "value"
    return None


def classify_style(
    instrument_type: str,
    holdings: list[dict] | None = None,
    index_name: str | None = None,
) -> dict[str, str | None]:
    """Classify a fund's growth/value/momentum/quality/low_volatility style.

    Only meaningful for funds (ETFs) — equities don't carry a "style" mandate
    in the same sense (a stock's own factor exposures are a different,
    already-existing quant signal, not this classifier's job).

    Returns ``{"style": ... | None, "basis": str}``. ``style`` is ``None``
    (never guessed) when neither the index name nor the top-10 holdings give
    a confident signal — callers must render that as "not established", not
    omit the field silently, so the LLM is never left to fill the gap itself.
    """
    if instrument_type not in ("etf", "fund"):
        return {"style": None, "basis": "not_applicable_to_equities"}

    from_index = _match_index_name(index_name)
    if from_index is not None:
        return {"style": from_index, "basis": f"index_name:{index_name}"}

    from_holdings = _match_holdings(holdings)
    if from_holdings is not None:
        return {"style": from_holdings, "basis": "top_10_holdings_majority"}

    return {"style": None, "basis": "insufficient_evidence"}
