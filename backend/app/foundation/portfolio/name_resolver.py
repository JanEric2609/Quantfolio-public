"""Position name resolution for DKB holdings.

Resolution chain:

1. The ``name`` of a matching ``Asset`` row (by ISIN, then symbol) when a DB
   session is provided — curated Asset names outrank whatever the broker wire
   sent, so truncated wire fragments can never shadow the canonical name.
2. An explicit, non-empty ``name`` already on the position.
3. yfinance ``longName`` / ``shortName`` looked up by ISIN or ticker.
4. Ticker/symbol, ISIN tail, or ``"Unknown Position"`` fallback.

Steps 1–2 only accept names that pass :func:`_is_real_name`, which rejects
identifier echoes and truncated FinTS wire artifacts (e.g. ``"TERED SHS USD
(ACC) O.N."``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire-artifact rejection table.
#
# DKB's FinTS wire format truncates long security names into fragments such as
# "TERED SHS USD (ACC) O.N." (from "iShares Core MSCI World UCITS ETF USD
# (Acc) - Registered Shares ...").  A name is treated as a wire fragment when
# it carries any junk token AND none of the recognised issuer/fund markers;
# this keeps legitimate German wire conventions like "SAP SE O.N." passing.
# ---------------------------------------------------------------------------

_JUNK_TOKENS: frozenset[str] = frozenset({"SHS", "O.N.", "INH", "NA", "ACC"})
_REAL_NAME_MARKERS: frozenset[str] = frozenset({
    "AG", "AMUNDI", "ASA", "COMPANY", "CO", "CORP", "CORPORATION", "ETF",
    "FONDS", "FUND", "GROUP", "HOLDINGS", "INC", "INCORPORATED", "INDEX",
    "INVESCO", "ISHARES", "LIMITED", "LTD", "MSCI", "NV", "PLC", "SA",
    "SE", "SHARES", "SPDR", "TRUST", "UCITS", "VANGUARD", "WISDOMTREE",
    "WORLD", "XTRACKERS",
})


def _token_pattern(tokens: frozenset[str]) -> re.Pattern[str]:
    """Whole-word alternation for uppercase tokens; a trailing dot is optional."""
    alternation = "|".join(sorted(re.escape(token) for token in tokens))
    return re.compile(rf"(?<![A-Z0-9])(?:{alternation})\.?(?![A-Z0-9])")


_JUNK_TOKEN_RE = _token_pattern(_JUNK_TOKENS)
_REAL_NAME_MARKER_RE = _token_pattern(_REAL_NAME_MARKERS)


def _is_real_name(name: str | None, ticker: str, isin: str) -> bool:
    """True if *name* is a human-readable label, not a placeholder/identifier echo.

    Two gates:

    1. Identifier echo — the name must differ from both the ticker and ISIN.
    2. Wire-artifact heuristic — a fragment carrying a junk token
       (:data:`_JUNK_TOKENS`) with no recognised issuer/fund marker
       (:data:`_REAL_NAME_MARKERS`) is a truncated FinTS wire string.
    """
    if not name:
        return False
    cleaned = name.strip()
    if not cleaned:
        return False
    upper = cleaned.upper()
    if upper in {ticker.upper(), isin.upper()}:
        return False
    if _JUNK_TOKEN_RE.search(upper) and not _REAL_NAME_MARKER_RE.search(upper):
        return False
    return True


def _lookup_asset_name(db: Session, isin: str, ticker: str) -> Asset | None:
    """Return the best-matching :class:`Asset` row by ISIN, then by symbol."""
    if isin:
        asset = db.query(Asset).filter(Asset.isin == isin).first()
        if asset is not None:
            return asset
    if ticker:
        return db.query(Asset).filter(Asset.symbol == ticker.upper()).first()
    return None


def _resolve_name_via_yfinance(query: str) -> str | None:
    """Best-effort yfinance lookup of a human-readable name for an ISIN or ticker."""
    if not query:
        return None
    try:
        import yfinance as yf

        info = yf.Ticker(query).info
        name = info.get("longName") or info.get("shortName")
        if name and name.strip():
            return name.strip()
    except Exception:
        logger.debug("yfinance name lookup failed for %s", query, exc_info=True)
    return None


def resolve_position_name(pos: dict[str, Any], db: Session | None = None) -> str:
    """Return the best available human-readable name for a position.

    Resolution chain:

    1. The ``name`` of a matching :class:`Asset` row (by ISIN, then symbol)
       when a DB session is provided — Asset-FIRST, before the position's own
       name, so curated canonical names outrank broker wire strings.
    2. An explicit, non-empty ``name`` already on the position.
    3. yfinance ``longName``/``shortName`` looked up by ISIN or ticker
       (best-effort, network).  When this resolves a name and a matching
       Asset row exists with a placeholder name, the row's ``name`` is updated
       and committed so the lookup is not repeated on subsequent calls.
    4. Ticker/symbol, then the ISIN tail, then ``"Unknown Position"``.

    Steps 1–2 only accept names that pass :func:`_is_real_name`.

    *db* is optional: without it the resolver degrades to the pure in-memory
    fallback (steps 2 and 4), which keeps unit tests free of a DB dependency.
    """
    ticker = (pos.get("ticker") or pos.get("symbol") or "").strip()
    isin = (pos.get("isin") or "").strip()

    asset = None
    if db is not None:
        asset = _lookup_asset_name(db, isin, ticker)
        if asset is not None and _is_real_name(asset.name, ticker, isin):
            return asset.name

    name = (pos.get("name") or "").strip()
    if name and _is_real_name(name, ticker, isin):
        return name

    # Step 3 — yfinance. This block used to sit AFTER ``return name`` inside
    # the success branch above, making it unreachable dead code: wire-garbage
    # positions fell straight to the ticker/ISIN-tail fallbacks without ever
    # consulting yfinance, contradicting this docstring's step 3.
    resolved = _resolve_name_via_yfinance(isin) if isin else None
    if not resolved and ticker:
        resolved = _resolve_name_via_yfinance(ticker)
    if resolved:
        if db is not None and asset is not None and not _is_real_name(asset.name, ticker, isin):
            asset.name = resolved[:240]
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.debug(
                    "Failed to persist resolved name for %s",
                    isin or ticker,
                    exc_info=True,
                )
        return resolved

    if ticker:
        return ticker
    if isin:
        return isin[-6:]  # last 6 chars of ISIN as short identifier
    return "Unknown Position"
