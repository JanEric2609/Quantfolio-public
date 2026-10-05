"""Factor API endpoints — factor zoo, Fama-French, attribution, rotation, smart beta."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.quant import factor_exposures
from app.foundation.providers.registry import build_provider_registry
from app.foundation import market as market_service

router = APIRouter(tags=["quant-factors"])
logger = logging.getLogger(__name__)


@router.get("/factors/zoo")
def factors_zoo(_user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.quant_factors import list_factor_zoo
    return {"factors": list_factor_zoo()}


@router.get("/factors/ff3")
def factors_ff3(_user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.quant_factors import get_ff3_returns
    return get_ff3_returns()


@router.get("/factors/attribution")
def factors_attribution(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.quant_factors import get_ff3_returns, compute_factor_attribution

    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    returns, port_dates = svc.returns_list(matrix, weights) if matrix else ([], [])
    if not returns:
        return {"status": "unavailable", "message": "No portfolio returns.", "diagnostics": diagnostics}

    ff3 = get_ff3_returns()
    if ff3.get("status") != "completed":
        from app.interface.api.quant._common import compute_factor_model
        factor_result = compute_factor_model(db, user.id)
        return {**factor_result, "source": "etf_proxy_fallback", "diagnostics": diagnostics}

    # The portfolio series is in EUR and Ken French's factors are USD returns;
    # regressing one on the other read EURUSD as a large-cap tilt (ADR 0007
    # amendment 2). Without a rate the EUR series is used as is.
    from app.foundation.market import usd_per_unit_by_date
    from app.foundation.quant_factors import restate_in_usd

    usd_rates = usd_per_unit_by_date(db, "EUR", allow_live=False)
    if usd_rates:
        returns, port_dates = restate_in_usd(returns, port_dates, usd_rates)
    port_by_date: dict[str, float] = dict(zip(port_dates, returns))
    factor_by_date: dict[str, dict[str, float]] = {}
    for fname, date_vals in ff3["factors"].items():
        factor_by_date[fname] = {d: float(v) for d, v in date_vals.items()}

    common_dates: set[str] = set(port_by_date.keys())
    for fvals in factor_by_date.values():
        common_dates &= set(fvals.keys())
    aligned_dates = sorted(common_dates)

    if len(aligned_dates) < 20:
        return {
            "status": "unavailable",
            "message": "Not enough overlapping dates between portfolio and FF3 factors.",
            "diagnostics": diagnostics,
        }

    trimmed_port = [port_by_date[d] for d in aligned_dates]
    trimmed_factors = {fname: [fvals[d] for d in aligned_dates] for fname, fvals in factor_by_date.items()}

    result = compute_factor_attribution(trimmed_port, trimmed_factors)
    result["source"] = "fama_french_europe_5"
    result["diagnostics"] = diagnostics
    return result


@router.get("/factors/dashboard")
def factors_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    try:
        svc = PortfolioPriceService(db, user.id)
        matrix, weights, diagnostics = svc.price_matrix()
        if not matrix:
            return {
                "available": False,
                "factor_exposures": [],
                "technical_signals": [],
                "diagnostics": diagnostics,
            }

        portfolio_returns = svc.weighted_returns(matrix, weights)
        if len(portfolio_returns) < 30:
            return {
                "available": False,
                "factor_exposures": [],
                "technical_signals": [],
                "diagnostics": diagnostics | {"overlap_rows": len(portfolio_returns)},
            }

        factor_frame = svc.factor_proxy_returns()
        exposures: dict[str, float] = {}
        if not factor_frame.empty and len(factor_frame) >= 20:
            result = factor_exposures(portfolio_returns, factor_frame)
            exposures = result.get("exposures", {})

        frame = svc.to_frame(matrix)
        proxies = svc._get_factor_proxies()
        total_weight = float(sum(max(0.0, w) for w in weights.values())) or 1.0
        factor_exposures_list = [
            {
                "name": name,
                "score": round(exposures.get(name, 0.0), 4),
                # `weights` is keyed by ticker (portfolio holdings), while `name`
                # here is a factor label ("market"/"size"/...) — look up the
                # proxy ticker `proxies[name]` maps to, not the label itself.
                "weight": round(float(weights.get(proxies[name], 0)) / total_weight, 4) if total_weight else 0.0,
                "benchmark_weight": round(1.0 / len(proxies), 4),
            }
            for name in proxies
        ]

        latest = frame.iloc[-1]
        prev = frame.iloc[-2] if len(frame) > 1 else latest
        technical_signals = []
        for col in frame.columns:
            pct = float((latest[col] - prev[col]) / prev[col]) if prev[col] != 0 else 0.0
            signal = "bullish" if pct > 0.005 else "bearish" if pct < -0.005 else "neutral"
            technical_signals.append({"name": col, "value": round(float(latest[col]), 2), "signal": signal})

        return {
            "available": True,
            "factor_exposures": factor_exposures_list,
            "technical_signals": technical_signals,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        logger.warning("Factor exposure analysis failed: %s", exc, exc_info=True)
        return {
            "available": False,
            "factor_exposures": [],
            "technical_signals": [],
            "error": "Factor analysis unavailable. Check server logs for details.",
        }


@router.get("/factors/rotation")
def factors_rotation(
    window: int = Query(default=60, ge=30, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    try:
        svc = PortfolioPriceService(db, user.id)
        matrix, weights, diagnostics = svc.price_matrix()
        if not matrix:
            return {
                "available": False,
                "window_days": window,
                "factors": [],
                "diagnostics": diagnostics,
            }

        from app.foundation.quant import price_matrix_to_returns

        factor_prices = svc.factor_proxy_prices()
        proxies = svc._get_factor_proxies()

        returns_frame = price_matrix_to_returns(factor_prices)
        if returns_frame.empty or len(returns_frame) < 90:
            return {
                "available": False,
                "window_days": window,
                "factors": [],
                "diagnostics": diagnostics | {"price_rows": len(returns_frame)},
            }

        factor_list = []
        for fname in proxies:
            if fname not in returns_frame.columns:
                continue
            series = returns_frame[fname]
            r30 = float(series.tail(30).add(1).prod() - 1) if len(series) >= 30 else 0.0
            r60 = float(series.tail(60).add(1).prod() - 1) if len(series) >= 60 else 0.0
            r90 = float(series.tail(90).add(1).prod() - 1) if len(series) >= 90 else 0.0
            momentum = r30 - r90 if len(series) >= 90 else 0.0
            factor_list.append({
                "name": fname,
                "return_30d": round(r30, 4),
                "return_60d": round(r60, 4),
                "return_90d": round(r90, 4),
                "momentum": round(momentum, 4),
            })

        return {
            "available": True,
            "window_days": window,
            "factors": factor_list,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        logger.warning("Factor rotation analysis failed: %s", exc, exc_info=True)
        return {
            "available": False,
            "window_days": window,
            "factors": [],
            "error": "Factor rotation unavailable. Check server logs for details.",
        }


@router.get("/factors/smart-beta")
def factors_smart_beta(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    etf_universe = [
        {"ticker": "IS3R.DE", "name": "iShares Edge MSCI World Momentum Factor UCITS", "factor": "momentum"},
        {"ticker": "IS3Q.DE", "name": "iShares Edge MSCI World Quality Factor UCITS", "factor": "quality"},
        {"ticker": "IS3S.DE", "name": "iShares Edge MSCI World Value Factor UCITS", "factor": "value"},
        {"ticker": "IS3U.DE", "name": "iShares Edge MSCI World Size Factor UCITS", "factor": "size"},
        {"ticker": "IS3V.DE", "name": "iShares Edge MSCI World Min Vol Factor UCITS", "factor": "low_volatility"},
    ]
    try:
        svc = PortfolioPriceService(db, user.id)
        registry = build_provider_registry(db)
        matrix, weights, diagnostics = svc.price_matrix()

        etf_results = []
        for etf in etf_universe:
            try:
                quote_result = registry.get_quote(symbol=etf["ticker"])
                quote_data = quote_result.get("data") or {}
                price = quote_data.get("price") or quote_data.get("close") or 0.0

                tracking_error = 0.0
                overlap_score = 0.0
                if matrix:
                    import pandas as pd

                    etf_rows = market_service.history(db, etf["ticker"], days=252)
                    if etf_rows:
                        etf_prices = pd.Series(
                            {pd.to_datetime(row["date"]): float(row["close"]) for row in etf_rows}
                        ).sort_index()
                        etf_returns = etf_prices.pct_change().dropna()
                        port_returns = svc.weighted_returns(matrix, weights)
                        aligned = pd.concat(
                            [port_returns.rename("portfolio"), etf_returns.rename("etf")],
                            axis=1,
                            join="inner",
                        ).dropna()
                        if len(aligned) >= 10:
                            import numpy as np

                            p_vals = aligned["portfolio"].to_numpy(dtype=float)
                            e_vals = aligned["etf"].to_numpy(dtype=float)
                            diff = p_vals - e_vals
                            diff_std = float(np.std(diff, ddof=1))
                            tracking_error = round(diff_std * (252**0.5), 4)
                            corr_matrix = np.corrcoef(p_vals, e_vals)
                            corr_val = float(corr_matrix[0, 1]) if corr_matrix.shape == (2, 2) else 0.0
                            overlap_score = round(corr_val, 4) if not np.isnan(corr_val) else 0.0

                etf_results.append({
                    "ticker": etf["ticker"],
                    "name": etf["name"],
                    "factor": etf["factor"],
                    "price": round(float(price), 2) if price else None,
                    "tracking_error": tracking_error,
                    "overlap_score": overlap_score,
                })
            except Exception:
                etf_results.append({
                    "ticker": etf["ticker"],
                    "name": etf["name"],
                    "factor": etf["factor"],
                    "price": None,
                    "tracking_error": 0.0,
                    "overlap_score": 0.0,
                })

        return {
            "available": True,
            "etfs": etf_results,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        logger.warning("Smart-beta analysis failed: %s", exc, exc_info=True)
        return {
            "available": False,
            "etfs": [],
            "error": "Smart-beta analysis unavailable. Check server logs for details.",
        }
