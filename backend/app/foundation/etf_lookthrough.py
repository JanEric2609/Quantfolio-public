"""ETF look-through analysis.

Expands ETF holdings into their underlying constituents to compute
effective portfolio-level metrics (HHI, sector/region exposure, top
constituents) that account for diversification inside ETFs.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset, Holding, Portfolio
from app.foundation.etf_currency import etf_country_weights
from app.foundation.etf_lookup import EtfComposition, get_etf_composition
from app.foundation.portfolio_service import main_portfolio
from app.foundation.portfolio_utils import holding_market_value

logger = logging.getLogger(__name__)


def _resolve_etf_ticker(db: Session, holding: Holding) -> str | None:
    """Resolve a tradable ticker for an ETF holding.

    Priority:
    1. ``holding.ticker`` (from classification / DKB sync)
    2. ``Asset.symbol`` looked up by ISIN
    3. ``EtfUniverseProvider().lookup_by_isin(isin)`` → symbol
    """
    if holding.ticker:
        return holding.ticker

    if holding.isin:
        asset = db.query(Asset).filter(Asset.isin == holding.isin).first()
        if asset and asset.symbol:
            return asset.symbol

        try:
            from app.foundation.etf_universe import EtfUniverseProvider

            provider = EtfUniverseProvider()
            etf_record = provider.lookup_by_isin(holding.isin)
            if etf_record and etf_record.get("symbol"):
                return etf_record["symbol"]
        except Exception as exc:
            logger.debug("ETF universe lookup failed for %s: %s", holding.isin, exc)

    return None


def portfolio_lookthrough(db: Session, user_id: str, portfolio_id: str | None = None) -> dict[str, Any]:
    """Compute look-through portfolio metrics.

    Returns a dict with:
    - ``effective_hhi``: Herfindahl-Hirschman Index across all underlying
      holdings (ETF constituents + direct stocks).
    - ``sector_exposure``: aggregated sector percentages.
    - ``region_exposure``: country weights of the ETFs' tracked indices
      (``etf_currency.etf_country_weights``), weighted by holding.
    - ``top_constituents``: top 15 underlying holdings by effective weight.
    - ``lookthrough_count``: total number of underlying positions.
    - ``etf_holdings_count``: number of ETF positions in the portfolio.
    - ``direct_holdings_count``: number of non-ETF positions.
    - ``has_lookthrough_data``: whether any ETF composition was resolved.
    """
    if portfolio_id:
        portfolio = db.get(Portfolio, portfolio_id)
    else:
        portfolio = main_portfolio(db, user_id)
    if portfolio is None:
        return {
            "effective_hhi": 0.0,
            "sector_exposure": {},
            "region_exposure": {},
            "top_constituents": [],
            "lookthrough_count": 0,
            "etf_holdings_count": 0,
            "direct_holdings_count": 0,
            "has_lookthrough_data": False,
        }
    holdings: list[Holding] = (
        db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    )

    if not holdings:
        return {
            "effective_hhi": 0.0,
            "sector_exposure": {},
            "region_exposure": {},
            "top_constituents": [],
            "lookthrough_count": 0,
            "etf_holdings_count": 0,
            "direct_holdings_count": 0,
            "has_lookthrough_data": False,
        }

    # Compute total portfolio value and per-holding weights
    values: list[float] = []
    total_value = 0.0
    for h in holdings:
        val = holding_market_value(db, h)
        values.append(val)
        total_value += val

    if total_value <= 0:
        return {
            "effective_hhi": 0.0,
            "sector_exposure": {},
            "region_exposure": {},
            "top_constituents": [],
            "lookthrough_count": len(holdings),
            "etf_holdings_count": sum(1 for h in holdings if h.asset_type == "etf"),
            "direct_holdings_count": sum(1 for h in holdings if h.asset_type != "etf"),
            "has_lookthrough_data": False,
        }

    holding_weights = [v / total_value for v in values]

    # Expand ETFs into constituents
    sector_map: dict[str, float] = {}
    region_map: dict[str, float] = {}
    constituent_map: dict[str, dict[str, Any]] = {}

    etf_count = 0
    direct_count = 0
    has_lookthrough = False

    for h, hw in zip(holdings, holding_weights):
        if h.asset_type == "etf":
            etf_count += 1
            # Countries of the tracked index (yfinance has none), by ISIN.
            countries = etf_country_weights(db, h.isin)
            if countries is not None:
                for country, weight in countries.weights.items():
                    region_map[country] = region_map.get(country, 0.0) + hw * weight
            ticker = _resolve_etf_ticker(db, h)
            composition: EtfComposition | None = None
            if ticker:
                composition = get_etf_composition(ticker)

            if composition and composition.holdings:
                has_lookthrough = True
                # Add each constituent with effective weight = holding_weight * constituent_weight
                for constituent in composition.holdings:
                    eff_weight = hw * constituent.weight
                    key = constituent.ticker.upper()
                    if key in constituent_map:
                        constituent_map[key]["weight"] += eff_weight
                    else:
                        constituent_map[key] = {
                            "ticker": constituent.ticker,
                            "name": constituent.name,
                            "weight": eff_weight,
                        }
                for sector, weight in composition.sectors.items():
                    sector_map[sector] = sector_map.get(sector, 0.0) + hw * weight
            else:
                # Graceful degradation: treat ETF as single position
                key = (h.ticker or h.isin or h.name or "unknown").upper()
                if key in constituent_map:
                    constituent_map[key]["weight"] += hw
                else:
                    constituent_map[key] = {
                        "ticker": h.ticker or h.isin or "UNKNOWN",
                        "name": h.name or "Unknown ETF",
                        "weight": hw,
                    }
        else:
            direct_count += 1
            key = (h.ticker or h.isin or h.name or "unknown").upper()
            if key in constituent_map:
                constituent_map[key]["weight"] += hw
            else:
                constituent_map[key] = {
                    "ticker": h.ticker or h.isin or "UNKNOWN",
                    "name": h.name or "Unknown",
                    "weight": hw,
                }
            # For non-ETF, we don't have sector/region data from composition,
            # but we could use Asset sector if available
            if h.isin:
                asset = db.query(Asset).filter(Asset.isin == h.isin).first()
                if asset and asset.sector:
                    sector_map[asset.sector] = sector_map.get(asset.sector, 0.0) + hw

    # Compute effective HHI from aggregated constituent weights
    effective_hhi = sum(c["weight"] ** 2 for c in constituent_map.values()) if constituent_map else 0.0

    # Normalize sector / region to percentages (they already sum to ~1 if all holdings covered)
    sector_exposure = {k: round(v, 4) for k, v in sorted(sector_map.items(), key=lambda x: -x[1])}
    region_exposure = {k: round(v, 4) for k, v in sorted(region_map.items(), key=lambda x: -x[1])}

    # Top 15 constituents by effective weight
    top_constituents = sorted(
        constituent_map.values(),
        key=lambda x: x["weight"],
        reverse=True,
    )[:15]
    # Round weights for display
    for c in top_constituents:
        c["weight"] = round(c["weight"], 4)

    lookthrough_count = len(constituent_map)

    return {
        "effective_hhi": round(effective_hhi, 4),
        "sector_exposure": sector_exposure,
        "region_exposure": region_exposure,
        "top_constituents": top_constituents,
        "lookthrough_count": lookthrough_count,
        "etf_holdings_count": etf_count,
        "direct_holdings_count": direct_count,
        "has_lookthrough_data": has_lookthrough,
    }
