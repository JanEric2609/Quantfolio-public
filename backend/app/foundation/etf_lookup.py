"""ETF composition lookup service.

Fetches ETF holdings, sector breakdown, and geographic exposure.
Uses yfinance for live data with static fallback for common ETFs.
Caches results for 24 hours to avoid repeated API calls.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# In-memory cache (TTL: 24 hours)
_cache: dict[str, tuple[datetime, Any]] = {}
CACHE_TTL = timedelta(hours=24)


class EtfHolding(BaseModel):
    """Single ETF holding."""
    ticker: str
    weight: float
    name: str


class EtfComposition(BaseModel):
    """ETF composition data."""
    ticker: str
    name: str
    holdings: list[EtfHolding]
    sectors: dict[str, float]
    regions: dict[str, float]


# Static fallback data for common ETFs. No regions: the hard-coded ones were
# out of date (EUNL.DE "65% North America"; MSCI World was ~76% US and Canada
# on 2026-09-29). Regions come from the tracked index's holdings instead
# (``etf_currency.etf_country_weights``), see ``get_etf_profile``.
_STATIC_ETFS: dict[str, dict[str, Any]] = {
    "VWCE.DE": {
        "name": "Vanguard FTSE All-World UCITS ETF",
        "holdings": [
            {"ticker": "AAPL", "weight": 0.04, "name": "Apple Inc."},
            {"ticker": "MSFT", "weight": 0.03, "name": "Microsoft Corp."},
            {"ticker": "NVDA", "weight": 0.025, "name": "NVIDIA Corp."},
            {"ticker": "AMZN", "weight": 0.02, "name": "Amazon.com Inc."},
            {"ticker": "META", "weight": 0.015, "name": "Meta Platforms Inc."},
        ],
        "sectors": {
            "Technology": 0.25,
            "Healthcare": 0.12,
            "Financial Services": 0.15,
            "Consumer Discretionary": 0.10,
            "Industrials": 0.10,
            "Energy": 0.08,
            "Consumer Staples": 0.07,
            "Utilities": 0.05,
            "Real Estate": 0.04,
            "Materials": 0.02,
            "Communication Services": 0.02,
        },
    },
    "SPY": {
        "name": "SPDR S&P 500 ETF Trust",
        "holdings": [
            {"ticker": "AAPL", "weight": 0.07, "name": "Apple Inc."},
            {"ticker": "MSFT", "weight": 0.06, "name": "Microsoft Corp."},
            {"ticker": "NVDA", "weight": 0.05, "name": "NVIDIA Corp."},
            {"ticker": "AMZN", "weight": 0.04, "name": "Amazon.com Inc."},
            {"ticker": "META", "weight": 0.025, "name": "Meta Platforms Inc."},
        ],
        "sectors": {
            "Technology": 0.30,
            "Healthcare": 0.13,
            "Financial Services": 0.13,
            "Consumer Discretionary": 0.10,
            "Industrials": 0.09,
            "Energy": 0.04,
            "Consumer Staples": 0.06,
            "Utilities": 0.02,
            "Real Estate": 0.02,
            "Materials": 0.02,
            "Communication Services": 0.09,
        },
    },
    "EUNL.DE": {
        "name": "iShares Core MSCI World UCITS ETF",
        "holdings": [
            {"ticker": "AAPL", "weight": 0.045, "name": "Apple Inc."},
            {"ticker": "MSFT", "weight": 0.035, "name": "Microsoft Corp."},
            {"ticker": "NVDA", "weight": 0.03, "name": "NVIDIA Corp."},
            {"ticker": "AMZN", "weight": 0.025, "name": "Amazon.com Inc."},
            {"ticker": "META", "weight": 0.015, "name": "Meta Platforms Inc."},
        ],
        "sectors": {
            "Technology": 0.22,
            "Healthcare": 0.13,
            "Financial Services": 0.14,
            "Consumer Discretionary": 0.11,
            "Industrials": 0.11,
            "Energy": 0.06,
            "Consumer Staples": 0.07,
            "Utilities": 0.04,
            "Real Estate": 0.03,
            "Materials": 0.03,
            "Communication Services": 0.06,
        },
    },
    # IWDA.AS (Amsterdam) and IWDA.L (London) are the same iShares MSCI World
    # fund as EUNL.DE, just listed on different exchanges.  Use the same static
    # composition so the look-through works regardless of which ticker the user
    # holds, instead of degrading to a single opaque position.
    "IWDA.AS": {
        "name": "iShares Core MSCI World UCITS ETF (AMS)",
        "holdings": [
            {"ticker": "AAPL", "weight": 0.045, "name": "Apple Inc."},
            {"ticker": "MSFT", "weight": 0.035, "name": "Microsoft Corp."},
            {"ticker": "NVDA", "weight": 0.03, "name": "NVIDIA Corp."},
            {"ticker": "AMZN", "weight": 0.025, "name": "Amazon.com Inc."},
            {"ticker": "META", "weight": 0.015, "name": "Meta Platforms Inc."},
        ],
        "sectors": {
            "Technology": 0.22,
            "Healthcare": 0.13,
            "Financial Services": 0.14,
            "Consumer Discretionary": 0.11,
            "Industrials": 0.11,
            "Energy": 0.06,
            "Consumer Staples": 0.07,
            "Utilities": 0.04,
            "Real Estate": 0.03,
            "Materials": 0.03,
            "Communication Services": 0.06,
        },
    },
    "IWDA.L": {
        "name": "iShares Core MSCI World UCITS ETF (LSE)",
        "holdings": [
            {"ticker": "AAPL", "weight": 0.045, "name": "Apple Inc."},
            {"ticker": "MSFT", "weight": 0.035, "name": "Microsoft Corp."},
            {"ticker": "NVDA", "weight": 0.03, "name": "NVIDIA Corp."},
            {"ticker": "AMZN", "weight": 0.025, "name": "Amazon.com Inc."},
            {"ticker": "META", "weight": 0.015, "name": "Meta Platforms Inc."},
        ],
        "sectors": {
            "Technology": 0.22,
            "Healthcare": 0.13,
            "Financial Services": 0.14,
            "Consumer Discretionary": 0.11,
            "Industrials": 0.11,
            "Energy": 0.06,
            "Consumer Staples": 0.07,
            "Utilities": 0.04,
            "Real Estate": 0.03,
            "Materials": 0.03,
            "Communication Services": 0.06,
        },
    },
}


def _cache_key(ticker: str) -> str:
    """Generate normalized cache key for ticker."""
    normalized = ticker.upper().strip()
    return hashlib.md5(normalized.encode()).hexdigest()


def _get_static_fallback(ticker: str) -> EtfComposition | None:
    """Return static composition data for known ETFs."""
    normalized = ticker.upper().strip()

    # Try exact match first
    if normalized in _STATIC_ETFS:
        data = _STATIC_ETFS[normalized]
        return EtfComposition(
            ticker=normalized,
            name=data["name"],
            holdings=[EtfHolding(**h) for h in data["holdings"]],
            sectors=data["sectors"],
            regions={},
        )

    # Try without exchange suffix (e.g., VWCE from VWCE.DE)
    base_ticker = normalized.split(".")[0] if "." in normalized else None
    if base_ticker and base_ticker in _STATIC_ETFS:
        data = _STATIC_ETFS[base_ticker]
        return EtfComposition(
            ticker=normalized,
            name=data["name"],
            holdings=[EtfHolding(**h) for h in data["holdings"]],
            sectors=data["sectors"],
            regions={},
        )

    return None


def _parse_top_holdings(top_holdings: Any) -> list[EtfHolding]:
    """Holdings from yfinance's ``funds_data.top_holdings``.

    The frame is indexed by ``Symbol`` with ``Name`` and ``Holding Percent``
    (a fraction) columns. This used to read ``symbol``/``name``/
    ``holdingPercent`` columns that frame has never had, so every holding of
    every ETF came back as ``UNKNOWN`` at weight 0: look-through HHI dropped
    ETF positions and every ETF pair overlapped by 0.
    """
    if top_holdings is None or len(top_holdings) == 0:
        return []
    holdings: list[EtfHolding] = []
    for symbol, row in top_holdings.iterrows():
        try:
            weight = float(row.get("Holding Percent"))
        except (TypeError, ValueError):
            continue
        if not symbol or weight != weight or weight <= 0:
            continue
        name = row.get("Name")
        holdings.append(EtfHolding(
            ticker=str(symbol).strip().upper(),
            weight=weight,
            name=str(name).strip() if isinstance(name, str) and name.strip() else str(symbol),
        ))
    return holdings


def _fetch_yfinance_composition(ticker: str) -> EtfComposition | None:
    """Fetch ETF composition from yfinance."""
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)

        # Get holdings data
        holdings = []
        try:
            holdings = _parse_top_holdings(t.funds_data.top_holdings)
        except Exception as e:
            logger.warning(f"Failed to fetch holdings for {ticker}: {e}")

        # Get sector data
        sectors = {}
        try:
            sector_data = t.funds_data.sector_weightings
            if sector_data is not None:
                for sector, weight in sector_data.items():
                    sectors[sector] = float(weight)
        except Exception as e:
            logger.warning(f"Failed to fetch sectors for {ticker}: {e}")

        # Get ETF name
        name = ticker
        try:
            info = t.info
            if info and "longName" in info:
                name = info["longName"]
        except Exception:
            pass

        # Only return if we got some data. yfinance has no country weights
        # (FundsData never had ``country_weightings``); see get_etf_profile.
        if holdings or sectors:
            return EtfComposition(
                ticker=ticker.upper(),
                name=name,
                holdings=holdings,
                sectors=sectors,
                regions={},
            )

        return None

    except Exception as e:
        logger.error(f"yfinance failed for {ticker}: {e}")
        return None


def get_etf_composition(ticker: str) -> EtfComposition | None:
    """Get ETF composition data.
    
    Tries yfinance first, falls back to static data for common ETFs.
    Results are cached for 24 hours.
    
    Args:
        ticker: ETF ticker symbol (e.g., "VWCE.DE", "SPY")
    
    Returns:
        EtfComposition with holdings, sectors, regions, or None if unavailable.
    """
    key = _cache_key(ticker)
    now = datetime.now(UTC)

    # Check cache
    if key in _cache:
        cached_time, cached_data = _cache[key]
        if now - cached_time < CACHE_TTL:
            return cached_data

    # Try yfinance first
    result = _fetch_yfinance_composition(ticker)

    # Fall back to static data if yfinance fails
    if result is None:
        result = _get_static_fallback(ticker)

    # Cache the result (including None to avoid repeated failed lookups)
    _cache[key] = (now, result)

    return result


_INDEX_LABELS = {
    "msci_world": "MSCI World", "msci_acwi": "MSCI ACWI", "msci_acwi_imi": "MSCI ACWI IMI",
    "msci_em": "MSCI Emerging Markets", "msci_europe": "MSCI Europe", "sp500": "S&P 500",
    "ftse_all_world": "FTSE All-World", "stoxx_europe_600": "STOXX Europe 600",
}

#: Countries below this share are folded into "Other" in the profile's
#: breakdown: MSCI ACWI has 50 countries, most under 1%.
_REGION_MIN_SHARE = 0.01


def _isin_for_ticker(db: Any, ticker: str) -> str | None:
    """The ISIN behind *ticker*: an ISIN itself, an ``Asset`` or ``Holding``
    row with that symbol, else the security master."""
    if len(ticker) == 12 and ticker[:2].isalpha() and ticker[2:].isalnum():
        return ticker
    try:
        from app.foundation.models.entities import Asset, Holding

        for model, column in ((Asset, Asset.symbol), (Holding, Holding.ticker)):
            row = db.query(model.isin).filter(column == ticker, model.isin.isnot(None)).first()
            if row is not None and row[0]:
                return str(row[0])
        from app.foundation.data_engineering.pit_fundamentals import resolve_isin_for_symbol

        return resolve_isin_for_symbol(db, ticker)
    except Exception:  # noqa: BLE001 - a lookup failure means "no regions", not an error page
        logger.debug("ISIN lookup failed for %s", ticker, exc_info=True)
        return None


def _index_regions(db: Any, ticker: str) -> tuple[dict[str, float], str | None]:
    """Country weights of the index the ETF tracks, for display, and their source.

    yfinance has no country data; an ETF mapped to an index in
    ``etf_currency.ETF_INDEX`` gets that index's constituent countries from
    the monthly SPDR holdings refresh. Anything else gets none.
    """
    from app.foundation.etf_currency import etf_country_weights

    countries = etf_country_weights(db, _isin_for_ticker(db, ticker))
    if countries is None:
        return {}, None
    shown: dict[str, float] = {}
    other = 0.0
    for country, weight in sorted(countries.weights.items(), key=lambda kv: -kv[1]):
        if country != "Other" and weight >= _REGION_MIN_SHARE:
            shown[country] = round(weight, 4)
        else:
            other += weight
    if other > 0:
        shown["Other"] = round(other, 4)
    label = _INDEX_LABELS.get(countries.index_key, countries.index_key)
    if countries.read_through:
        label += f" (read through {_INDEX_LABELS.get(countries.read_through, countries.read_through)})"
    as_of = f" as of {countries.as_of.isoformat()}" if countries.as_of else ""
    stale = ", stale" if countries.stale else ""
    return shown, f"{label} constituents ({countries.source}{as_of}{stale})"


def get_etf_profile(db: Any, ticker: str) -> dict[str, Any] | None:
    """Return enriched ETF profile dict.

    Tries provider registry (OpenBB ETF holdings) first, then yfinance
    composition, then static fallback.  Also pulls TER / AUM from
    yfinance fundamentals.
    """
    from app.foundation.providers.registry import build_provider_registry
    from app.foundation.market import fundamentals as market_fundamentals

    ticker = ticker.upper().strip()
    registry = build_provider_registry(db)

    composition = get_etf_composition(ticker)
    holdings: list[dict[str, Any]] = []
    sectors: dict[str, float] = {}
    regions: dict[str, float] = {}
    if composition:
        holdings = [
            {"ticker": h.ticker, "weight": h.weight, "name": h.name}
            for h in composition.holdings
        ]
        sectors = composition.sectors

    regions, regions_source = _index_regions(db, ticker)

    provider_holdings: list[dict[str, Any]] = []
    try:
        etf_result = registry.get_etf_holdings(ticker)
        if etf_result.get("ok"):
            data = etf_result.get("data", {})
            if isinstance(data, list) and data:
                provider_holdings = [
                    {
                        "ticker": row.get("symbol") or row.get("ticker", "UNKNOWN"),
                        "weight": float(row.get("weight", 0)) / 100 if float(row.get("weight", 0)) > 1 else float(row.get("weight", 0)),
                        "name": row.get("name", row.get("symbol", "Unknown")),
                    }
                    for row in data
                ]
            elif isinstance(data, dict):
                rows = data.get("holdings", []) or data.get("data", [])
                if rows:
                    provider_holdings = [
                        {
                            "ticker": row.get("symbol") or row.get("ticker", "UNKNOWN"),
                            "weight": float(row.get("weight", 0)) / 100 if float(row.get("weight", 0)) > 1 else float(row.get("weight", 0)),
                            "name": row.get("name", row.get("symbol", "Unknown")),
                        }
                        for row in rows
                    ]
    except Exception as exc:
        logger.debug("Provider ETF holdings failed for %s: %s", ticker, exc)

    if len(provider_holdings) >= len(holdings):
        holdings = provider_holdings

    ter: float | None = None
    aum: float | None = None
    domicile: str | None = None
    replication: str | None = None
    policy: str | None = None
    ucits = False

    try:
        fund_result = market_fundamentals(db, ticker)
        fund_data = fund_result.get("data", {})
        if fund_data:
            raw_ter = fund_data.get("ter") or fund_data.get("annualReportExpenseRatio")
            if raw_ter is not None:
                try:
                    ter = float(raw_ter) * 100 if float(raw_ter) < 1 else float(raw_ter)
                except Exception:
                    ter = None
            raw_aum = fund_data.get("aum") or fund_data.get("totalAssets")
            if raw_aum is not None:
                try:
                    aum = float(raw_aum)
                except Exception:
                    aum = None
            domicile = fund_data.get("country") or fund_data.get("domicile")
    except Exception as exc:
        logger.debug("Fundamentals lookup failed for %s: %s", ticker, exc)

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        if ter is None:
            raw_ter = info.get("annualReportExpenseRatio")
            if raw_ter is not None:
                ter = float(raw_ter) * 100 if float(raw_ter) < 1 else float(raw_ter)
        if aum is None:
            raw_aum = info.get("totalAssets")
            if raw_aum is not None:
                aum = float(raw_aum)
        if domicile is None:
            domicile = info.get("country") or info.get("fundInceptionDate")
        fund_family = info.get("fundFamily")
        if fund_family:
            policy = fund_family
        category = info.get("category")
        if category and not replication:
            replication = category
    except Exception as exc:
        logger.debug("yfinance info lookup failed for %s: %s", ticker, exc)

    name = composition.name if composition else ticker
    if "ucits" in name.lower():
        ucits = True

    if not holdings and not sectors and ter is None and aum is None:
        return None

    return {
        "ticker": ticker,
        "name": composition.name if composition else ticker,
        "ter": round(ter, 4) if ter is not None else None,
        "size": aum,
        "domicile": domicile,
        "replication": replication,
        "policy": policy,
        "ucits": ucits,
        "top_holdings": holdings[:10],
        "sectors": sectors,
        "regions": regions,
        "regions_source": regions_source,
        "holdings_count": len(holdings),
    }


def tracking_stats(
    db: Any,
    etf_ticker: str,
    index_ticker: str,
    years: int = 3,
) -> dict[str, Any] | None:
    """Calculate tracking error, beta, and R² between an ETF and its benchmark.

    Uses ``market.history`` for both tickers and aligns on common dates.
    """
    from app.foundation.market import history as market_history
    from app.foundation.quant_metrics import beta as _beta, r_squared as _r_squared

    etf_ticker = etf_ticker.upper().strip()
    index_ticker = index_ticker.upper().strip()
    days = years * 365

    try:
        etf_rows = market_history(db, etf_ticker, days=days)
        idx_rows = market_history(db, index_ticker, days=days)
    except Exception as exc:
        logger.warning("History fetch failed for tracking stats: %s", exc)
        return None

    if not etf_rows or not idx_rows:
        return None

    etf_prices = {r["date"]: float(r["close"]) for r in etf_rows if r.get("close") is not None}
    idx_prices = {r["date"]: float(r["close"]) for r in idx_rows if r.get("close") is not None}

    common_dates = sorted(set(etf_prices.keys()) & set(idx_prices.keys()))
    if len(common_dates) < 60:
        return None

    etf_returns = [
        (etf_prices[d] / etf_prices[common_dates[i - 1]] - 1)
        for i, d in enumerate(common_dates)
        if i > 0
    ]
    idx_returns = [
        (idx_prices[d] / idx_prices[common_dates[i - 1]] - 1)
        for i, d in enumerate(common_dates)
        if i > 0
    ]

    if len(etf_returns) < 30 or len(idx_returns) < 30:
        return None

    diffs = [e - b for e, b in zip(etf_returns, idx_returns)]
    import math
    from statistics import stdev

    n = len(diffs)
    tracking_error = stdev(diffs) * math.sqrt(252) if n >= 2 else 0.0

    beta_val = _beta(etf_returns, idx_returns)
    r2_val = _r_squared(etf_returns, idx_returns)

    ann_etf = math.prod(1 + r for r in etf_returns) ** (252 / n) - 1 if n > 0 else 0.0
    ann_idx = math.prod(1 + r for r in idx_returns) ** (252 / n) - 1 if n > 0 else 0.0

    return {
        "etf_ticker": etf_ticker,
        "index_ticker": index_ticker,
        "tracking_error": round(tracking_error, 4),
        "beta": round(beta_val, 4),
        "r_squared": round(r2_val, 4),
        "annualised_return_diff": round(ann_etf - ann_idx, 4),
        "sample_days": n,
        "period_years": years,
    }
