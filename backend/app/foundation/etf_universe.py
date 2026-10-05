"""ETF universe provider wrapping justetf-scraping with local file cache.

Provides the full UCITS ETF universe from justETF.com, with ISIN-level
metadata (name, domicile, currency, distribution policy, TER, etc.) and
derived German tax classification (teilfreistellung_class).

The overview data is cached locally to avoid hammering justETF on every
discover pipeline run.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta
from typing import Any

from app.foundation.fund_class import (
    BOND_KEYWORDS as _BOND_KEYWORDS,
    MONEY_MARKET_KEYWORDS as _MONEY_MARKET_KEYWORDS,
    class_from_etf_universe_hint,
)

# Backward-compat re-export (canonical home: app.foundation.fund_class).
from app.foundation.fund_class import (  # noqa: F401
    KNOWN_BOND_KEYWORDS as _KNOWN_BOND_KEYWORDS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths & TTL
# ---------------------------------------------------------------------------

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_BACKEND_ROOT, ".cache", "etf_universe")
_CACHE_FILE = "etf_overview.json"
_DEFAULT_TTL_HOURS = 24

# Suffix appended to justETF ticker to form an exchange-tradable symbol
_XETRA_SUFFIX = ".DE"

# justETF long-only strategy filter
_LONG_ONLY = "epg-longOnly"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Keyword vocabularies (_MONEY_MARKET_KEYWORDS / _BOND_KEYWORDS /
# _KNOWN_BOND_KEYWORDS) now live canonically in
# app.foundation.fund_class and are imported above — the unified fund
# classifier (todo 22) is the single source of truth for them.


def is_money_market_fund(etf_name: str) -> bool:
    """True if *etf_name* matches a known overnight-rate/money-market fund pattern.

    Distinct from :func:`_derive_tf_class`'s broader bond detection: this is
    specifically for identifying cash-equivalent instruments (near-zero
    volatility, mechanically bound to short-term policy rates) so scoring and
    LLM prompting can avoid treating them like equities.
    """
    name_lower = etf_name.lower()
    return any(kw in name_lower for kw in _MONEY_MARKET_KEYWORDS)


def is_bond_fund(etf_name: str) -> bool:
    """True if *etf_name* matches a known duration-bearing bond-fund keyword
    pattern — distinct from a money-market/cash-equivalent fund.

    Callers must check :func:`is_money_market_fund` first: "short term" and
    "kurzlauf" alone are ambiguous between a genuinely short-duration bond
    fund and an overnight-rate fund, so this function does not attempt to
    disambiguate them itself.
    """
    name_lower = etf_name.lower()
    return any(kw in name_lower for kw in _BOND_KEYWORDS)


def _derive_tf_class(etf_name: str, strategy: str | None) -> str:
    """Derive the German tax teilfreistellung_class for a MATCHED universe record.

    Demoted to a HINT per the unified fund classifier (todo 22): this keyword
    logic is only valid inside the etf_universe match path — i.e. for records
    whose ISIN/composition membership in the UCITS universe is established.
    Never apply it to holdings without composition/ISIN evidence; use
    ``app.foundation.fund_class.classify_fund_class`` instead.
    """
    return class_from_etf_universe_hint(etf_name, strategy)


def _safe_float(value: float | str | None, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        logger.debug("_safe_float: could not convert %r to float, using %s", value, default)
        return default


def _to_xetra_symbol(
    ticker: object, wkn: object, isin: str
) -> str | None:
    """Build an exchange-tradable symbol (e.g. ``"EUNL.DE"``).

    Priority: ticker → wkn → None.
    Handles pandas NA / NaN values gracefully.
    """
    import pandas as pd

    def _safe(val: object) -> str | None:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        s = str(val).strip()
        return s if s and s != "nan" else None

    base = _safe(ticker) or _safe(wkn)
    if base:
        return f"{base.upper()}{_XETRA_SUFFIX}"
    return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class EtfUniverseProvider:
    """Wraps ``justetf_scraping`` to deliver the UCITS ETF universe.

    Caches the overview DataFrame as JSON on disk. Callers should call
    ``get_universe()`` which returns a list of dicts with keys:

        symbol, isin, name, domicile_country, currency, dividends,
        replication, ter, strategy, tf_class
    """

    def __init__(self, cache_ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
        self.cache_ttl = timedelta(hours=cache_ttl_hours)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_universe(self) -> list[dict[str, Any]]:
        """Return the full long-only ETF universe as a list of enriched dicts.

        Uses a file cache when fresh; falls back to a live justETF request.
        """
        cached = self._load_cache()
        if cached is not None:
            logger.info("EtfUniverseProvider: using cached data (%d ETFs)", len(cached))
            return cached

        logger.info("EtfUniverseProvider: cache miss, fetching from justETF…")
        data = self._fetch_from_justetf()
        self._save_cache(data)
        return data

    def get_universe_cached(self) -> list[dict[str, Any]]:
        """Return the locally cached universe when fresh, else ``[]``.

        Unlike :meth:`get_universe` this NEVER triggers a live justETF fetch,
        so it is safe to call from request paths that must not block (e.g. the
        tax cockpit's evidence lookup).
        """
        cached = self._load_cache()
        return cached if cached is not None else []

    def lookup_by_isin(self, isin: str) -> dict[str, Any] | None:
        """Return a single ETF record by ISIN, or ``None``."""
        universe = self.get_universe()
        for etf in universe:
            if etf.get("isin") == isin:
                return etf
        return None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_from_justetf() -> list[dict[str, Any]]:
        """Fetch overview from justETF and return enriched records."""
        import justetf_scraping  # slow import, keep local

        try:
            df = justetf_scraping.load_overview(strategy=_LONG_ONLY)
        except Exception:
            logger.exception("Failed to fetch ETF overview from justETF")
            return []

        if df is None or df.empty:
            logger.warning("justETF returned no data")
            return []

        records: list[dict[str, Any]] = []
        for isin, row in df.iterrows():
            if isin is None or (isinstance(isin, float) and isin != isin) or (isinstance(isin, str) and isin.strip() in ("", "nan")):
                logger.warning("EtfUniverseProvider: skipping ETF row with NaN/empty ISIN")
                continue
            isin_str = str(isin)
            name = row.get("name") or ""
            strategy_val = row.get("strategy") or "Long-only"
            ticker = row.get("ticker")
            wkn = row.get("wkn")

            symbol = _to_xetra_symbol(ticker, wkn, isin_str)
            if not symbol:
                logger.warning(
                    "EtfUniverseProvider: skipping ETF — cannot build "
                    "Xetra symbol (ticker=%s, wkn=%s, isin=%s). "
                    "The ETF will not appear in the universe.",
                    ticker, wkn, isin_str,
                )
                continue

            records.append({
                "symbol": symbol,
                "isin": isin_str,
                "name": name,
                "domicile_country": row.get("domicile_country") or "",
                "currency": row.get("currency") or "",
                "dividends": row.get("dividends") or "",
                "replication": row.get("replication") or "",
                "ter": _safe_float(row.get("ter"), default=0.0),
                "strategy": strategy_val,
                "tf_class": _derive_tf_class(name, strategy_val),
            })

        logger.info("EtfUniverseProvider: fetched %d UCITS ETFs from justETF", len(records))
        return records

    # ------------------------------------------------------------------
    # File cache
    # ------------------------------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(_CACHE_DIR, _CACHE_FILE)

    def _is_cache_fresh(self) -> bool:
        path = self._cache_path()
        if not os.path.isfile(path):
            return False
        age = time.time() - os.path.getmtime(path)
        return age < self.cache_ttl.total_seconds()

    def _load_cache(self) -> list[dict[str, Any]] | None:
        if not self._is_cache_fresh():
            return None
        path = self._cache_path()
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("EtfUniverseProvider: cache read failed: %s", exc)
            return None

    def _save_cache(self, data: list[dict[str, Any]]) -> None:
        path = self._cache_path()
        # Write a sibling file and rename it over the cache: writing in place
        # let a concurrent reader (the API while the worker refreshes, or a
        # parallel test) read a truncated file, fail to parse it, and classify
        # every fund as "other" (0% Teilfreistellung) for that call.
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            logger.info("EtfUniverseProvider: cached %d ETFs to %s", len(data), path)
        except OSError as exc:
            logger.warning("EtfUniverseProvider: cache write failed: %s", exc)
