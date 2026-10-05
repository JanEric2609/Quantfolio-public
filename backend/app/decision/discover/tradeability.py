"""Tradeability assessor for Discover pipeline.

Determines whether a candidate is likely tradeable at each of the user's
brokers (DKB and Scalable Capital): funds by domicile (PRIIPs), shares by
listing venue, with a provider registry lookup for listings outside the EU and
the US. The result carries one badge per broker; the pipeline drops a
candidate only when neither broker is likely to offer it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.recommendation_execution import dkb_search_url, scalable_security_url
from app.foundation.providers.registry import build_provider_registry

logger = logging.getLogger(__name__)

__all__ = ["assess", "dkb_search_url", "scalable_security_url", "unknown"]

# Instrument types sold as funds: PRIIPs requires a KID for EU retail sales.
_FUND_TYPES = {"etf", "fund", "bond", "money_market"}

# EU-domicile ISIN prefixes for UCITS eligibility
_EU_ISIN_PREFIXES = {"IE", "LU", "DE", "FR", "NL", "AT", "BE", "ES", "IT", "FI"}

# EU venue suffixes that strongly suggest Xetra/Euronext/Borsa Italiana/etc. access
_EU_VENUE_SUFFIXES = {".DE", ".F", ".PA", ".AS", ".MI", ".MC", ".SW", ".HE", ".L", ".BR", ".CO", ".ST", ".OL", ".VI", ".WA", ".PR", ".BD", ".BM"}


def _has_eu_venue_suffix(symbol: str) -> bool:
    sym = symbol.upper()
    return any(sym.endswith(suf.upper()) for suf in _EU_VENUE_SUFFIXES)


def _isin_prefix(isin: str | None) -> str:
    if not isin:
        return ""
    return isin[:2].upper()


def _registry_confirms_eu_venue(db: Session, symbol: str) -> bool:
    """Best-effort registry lookup: if fundamentals return a known EU exchange, treat as EU."""
    try:
        registry = build_provider_registry(db)
        result = registry.get_fundamentals(symbol)
        if result.get("ok"):
            data = result.get("data", {})
            exchange = (data.get("exchange") or data.get("Exchange") or "").upper()
            if exchange in {
                "GER", "XETRA", "FRA", "PAR", "AMS", "MIL", "MAD", "STO", "HEL",
                "OSL", "VIE", "SWX", "LSE", "BME", "BRU", "LIS", "CPH", "PRA",
                "BUD", "WAR", "ATH",
            }:
                return True
    except Exception:
        logger.debug("Registry EU venue check failed for %s", symbol, exc_info=True)
    return False


def _is_us_listing(symbol: str, isin: str | None) -> bool:
    """A US primary listing: a US ISIN, or a bare ticker in yfinance's US form.

    yfinance writes every non-US listing with a venue suffix (``SAP.DE``,
    ``7203.T``); US tickers carry none, and share classes use a dash
    (``BRK-B``). FX pairs (``=X``), indices (``^``) and futures (``=F``) are
    not securities.
    """
    if _isin_prefix(isin) == "US":
        return True
    sym = symbol.upper()
    if "." in sym or "=" in sym or sym.startswith("^"):
        return False
    return bool(sym) and sym.replace("-", "").isalnum()


def unknown(isin: str | None, error: str) -> dict[str, Any]:
    """Result for a check that could not run: unknown, never "likely".

    The orchestrator's fallback used to mark such a candidate likely
    tradeable at medium confidence, i.e. a crash read as evidence. None
    still passes its ``is not False`` gate, so the candidate is kept; the
    dossier shows it as unchecked.
    """
    return {
        "likely_tradeable": None,
        "confidence": "unknown",
        "reasons": ["Tradeability check failed — confirm at your broker"],
        "manual_check_url": dkb_search_url(isin),
        "brokers": {
            "dkb": {"likely": None, "confidence": "unknown", "url": dkb_search_url(isin)},
            "scalable": {"likely": None, "confidence": "unknown", "url": scalable_security_url(isin)},
        },
        "error": error,
    }


def assess(
    db: Session,
    symbol: str,
    isin: str | None,
    name: str,
    instrument_type: str | None = None,
) -> dict[str, Any]:
    """Assess whether a candidate is likely tradeable at DKB and at Scalable.

    Funds and single shares follow different rules:

    * A fund (ETF, bond or money-market fund) sold to an EU retail investor
      needs a PRIIPs key information document. US-domiciled ETFs do not
      publish one, so DKB blocks them; the domicile (ISIN prefix) decides.
    * A single share needs no KID. DKB reaches US shares through Tradegate,
      gettex and LS Exchange (no foreign-venue fee) or directly on NYSE and
      Nasdaq, so a US listing is tradeable. Other listings are judged by venue
      as before.

    Until 2026-09 both went through the fund rule, so every US share (the
    index screen supplies no ISIN and no venue suffix) came out "low" and the
    gate rejected all of them: the 2026-09-26 run lost its top 9 names.

    ``instrument_type`` is the pipeline's classification; when omitted it is
    resolved here with the same classifier.

    Returns:
        {
            "likely_tradeable": bool,
            "confidence": "high" | "medium" | "low",
            "reasons": list[str],
            "manual_check_url": str,
            "instrument_type": str,
        }
    """
    reasons: list[str] = []
    sym = symbol.upper()
    isin_pre = _isin_prefix(isin)
    if instrument_type is None:
        instrument_type = classify_instrument(symbol, None, name)
    is_fund = instrument_type in _FUND_TYPES
    # Shares listed outside the EU and the US: DKB routes orders to many foreign
    # exchanges directly; Scalable trades on gettex/Xetra only, whose foreign
    # lines cover mostly large caps. Both stay "uncertain" there.
    dkb_note = scalable_note = ""

    if is_fund:
        if isin and isin_pre in _EU_ISIN_PREFIXES:
            reasons.append(f"EU-domiciled fund ({isin_pre}) — UCITS, with the key information document EU retail sales need")
            confidence = "high"
        elif isin:
            reasons.append(
                f"Fund domiciled outside the EU ({isin_pre}) — no PRIIPs key information document, "
                "so EU brokers block it for retail clients"
            )
            confidence = "low"
        elif _has_eu_venue_suffix(sym):
            reasons.append("Fund listed on an EU venue — EU venues list UCITS funds for retail clients")
            confidence = "high"
        elif _is_us_listing(sym, isin):
            reasons.append(
                "US-listed fund — no PRIIPs key information document, so EU brokers block it for retail clients"
            )
            confidence = "low"
        else:
            reasons.append("No ISIN and no EU venue — the fund's domicile cannot be checked")
            confidence = "low"
        dkb_confidence = scalable_confidence = confidence
    elif _has_eu_venue_suffix(sym):
        reasons.append("EU venue suffix detected — likely available on Xetra/Euronext")
        dkb_confidence = scalable_confidence = "high"
    elif _is_us_listing(sym, isin):
        reasons.append("US-listed share — single shares need no key information document")
        dkb_note = "DKB trades US shares on Tradegate, gettex and LS Exchange or directly on NYSE/Nasdaq"
        scalable_note = "Scalable trades US shares on gettex (most large and mid caps)"
        dkb_confidence = scalable_confidence = "medium"
    elif _registry_confirms_eu_venue(db, sym):
        reasons.append("Provider registry confirms EU exchange listing")
        dkb_confidence = scalable_confidence = "medium"
    else:
        reasons.append("Listed outside the EU and the US — availability uncertain")
        dkb_note = "DKB routes to many foreign exchanges directly"
        scalable_note = "Scalable has gettex/Xetra lines for some foreign shares"
        dkb_confidence = scalable_confidence = "low"

    reasons.append("likely — confirm at your broker")

    brokers = {
        "dkb": {
            "likely": dkb_confidence in ("high", "medium"),
            "confidence": dkb_confidence,
            "note": dkb_note or None,
            "url": dkb_search_url(isin),
        },
        "scalable": {
            "likely": scalable_confidence in ("high", "medium"),
            "confidence": scalable_confidence,
            "note": scalable_note or None,
            "url": scalable_security_url(isin),
        },
    }
    likely = any(b["likely"] for b in brokers.values())
    order = ("low", "medium", "high")
    confidence = max((b["confidence"] for b in brokers.values()), key=order.index)
    manual_check_url = dkb_search_url(isin)

    logger.debug(
        "tradeability assess: %s type=%s likely=%s confidence=%s reasons=%s",
        sym, instrument_type, likely, confidence, reasons,
    )

    return {
        "likely_tradeable": likely,
        "confidence": confidence,
        "reasons": reasons,
        "manual_check_url": manual_check_url,
        "instrument_type": instrument_type,
        "brokers": brokers,
    }
