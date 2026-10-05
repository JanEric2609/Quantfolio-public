"""Deterministic trade-proposal builder (foundation-tier).

Given candidates + the current paper book, propose sized trades within a risk
envelope:

1. Per symbol: Monte-Carlo forward-return distribution (GBM calibrated from
   price history via ``quant_mc``) -> P5/P50/P95 + prob(positive).
2. skfolio optimiser (``quant_optim``) proposes target weights over
   {current book} u {candidates}.
3. Resulting portfolio risk envelope: VaR, CVaR, max drawdown.

No LLM here. Instrument-type-aware in the same sense as the discover
pipeline (ETFs carry no single-name factor features; the MC distribution
applies to both).

This module only touches foundation-tier services (market, quant_metrics,
quant_mc, quant_optim, instrument_taxonomy) and has no decision-loop imports
of its own, so both ``advisor`` and ``llm_portfolio`` can depend on it
directly without tripping the "decision-loop internals are facade-only" or
"decision-loop packages are independent" import-linter contracts. Originally
lived at ``advisor/decision.py``; ``advisor.decision`` is now a thin
re-export shim over this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.models.entities import PaperHolding
from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.market import history as market_history
from app.foundation.quant_metrics import historical_cvar, historical_var, max_drawdown
from app.foundation.quant_mc.calibration import calibrate_gbm
from app.foundation.quant_mc.distributions import NormalDistribution
from app.foundation.quant_mc.paths import GBMEuler

logger = logging.getLogger(__name__)

# Forward horizon for MC simulation, in trading days (matches the prediction
# scoring horizon).
MC_HORIZON_TRADING_DAYS = 21
MC_N_PATHS = 2000
_HISTORY_DAYS = 365 * 2


@dataclass
class McSummary:
    """Forward-return distribution summary for one symbol."""

    symbol: str
    instrument_type: str
    p5: float
    p50: float
    p95: float
    prob_positive: float
    spot: float


@dataclass
class TradeProposal:
    """Deterministic proposal: suggested weights + MC summaries + envelope."""

    portfolio_id: str
    suggested_weights: dict[str, float]
    mc_summaries: dict[str, McSummary]
    risk_envelope: dict[str, float | None]
    current_book: dict[str, float]
    optimizer_status: str
    notes: list[str] = field(default_factory=list)
    # Aligned price series per symbol — internal, lets the risk gate recompute
    # the envelope after dropping names, and lets a council-context builder
    # derive a correlation matrix via quant.correlation_matrix_from_price_matrix
    # (pandas builds a DataFrame from a dict of Series directly). Not serialised.
    price_series: dict[str, Any] = field(default_factory=dict)
    # Per-symbol instrument_type (F10) — lets the risk gate's remediation
    # loop respect GROWTH_FLOOR instead of always dropping growth names
    # first (a money-market name's MC P5 tail is closest to zero).
    instrument_types: dict[str, str] = field(default_factory=dict)


def concentration_from_weights(weights: dict[str, float]) -> float:
    """Herfindahl-Hirschman concentration index over *weights* (0=diffuse, 1=single-name).

    Normalises first so callers may pass raw (non-unit-sum) weights.
    """
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return 0.0
    return sum((w / total) ** 2 for w in weights.values() if w > 0)


def _price_series(db: Session, symbol: str, days: int = _HISTORY_DAYS) -> pd.Series | None:
    """Daily closes in EUR: the paper book trades and values in EUR, so spot must be EUR too."""
    from app.foundation.eur_prices import to_eur

    rows = market_history(db, symbol, days=days)
    if not rows:
        return None
    closes = {str(r["date"])[:10]: float(r["close"]) for r in rows if r.get("close") is not None}
    eur, _ccy = to_eur(db, symbol, closes, days=days + 30)
    if len(eur) < 60:
        return None
    series = pd.Series(eur, dtype=float).sort_index()
    series.index = pd.to_datetime(series.index)
    return series


def _mc_summary(symbol: str, prices: pd.Series, instrument_type: str) -> McSummary | None:
    """Calibrate GBM from history and simulate a forward-return distribution."""
    try:
        params = calibrate_gbm(prices.to_numpy())
        # calibrate_gbm returns per-step (daily) mu/sigma; GBMEuler consumes
        # them against T expressed in the same per-step unit, so simulate with
        # T = n_steps (days) and dt = 1 day.
        sim = GBMEuler(mu=params.mu, sigma=params.sigma)
        spot = float(prices.iloc[-1])
        paths = sim.simulate(
            S0=spot,
            T=float(MC_HORIZON_TRADING_DAYS),
            n_steps=MC_HORIZON_TRADING_DAYS,
            n_paths=MC_N_PATHS,
            rng=np.random.default_rng(42),
            distribution=NormalDistribution(),
        )
        terminal_returns = paths[:, -1] / spot - 1.0
        return McSummary(
            symbol=symbol,
            instrument_type=instrument_type,
            p5=float(np.percentile(terminal_returns, 5)),
            p50=float(np.percentile(terminal_returns, 50)),
            p95=float(np.percentile(terminal_returns, 95)),
            prob_positive=float((terminal_returns > 0).mean()),
            spot=spot,
        )
    except Exception:
        logger.debug("MC summary failed for %s", symbol, exc_info=True)
        return None


def _portfolio_risk_envelope(
    weights: dict[str, float],
    series_by_symbol: dict[str, pd.Series],
) -> dict[str, float | None]:
    """Compute daily VaR/CVaR (positive loss magnitudes) + max drawdown for the
    weighted portfolio implied by *weights* over the aligned return history."""
    active = {s: w for s, w in weights.items() if w > 0 and s in series_by_symbol}
    if not active:
        return {"var_95_daily": None, "cvar_95_daily": None, "max_drawdown": None}
    frame = pd.DataFrame({s: series_by_symbol[s] for s in active}).dropna()
    if len(frame) < 30:
        return {"var_95_daily": None, "cvar_95_daily": None, "max_drawdown": None}
    rets = frame.pct_change().dropna()
    total = sum(active.values())
    w = pd.Series({s: v / total for s, v in active.items()})
    port_rets = (rets * w).sum(axis=1)

    # var_95/cvar_95 must come from quant_metrics' paired historical_var/
    # historical_cvar (both keyed off the same empirical order statistic)
    # rather than an independently computed VaR — CVaR is only guaranteed
    # >= VaR when both share a tail definition. risk_gate.py's 3% daily
    # var_95_daily ceiling is calibrated against this empirical estimate.
    var_95 = historical_var(port_rets.tolist(), confidence=0.95)
    cvar_95 = float(historical_cvar(port_rets.tolist(), confidence=0.95))
    mdd = abs(max_drawdown(port_rets.tolist()).get("max_drawdown", 0.0))
    return {
        "var_95_daily": round(var_95, 5),
        "cvar_95_daily": round(cvar_95, 5),
        "max_drawdown": round(mdd, 5),
    }


def build_trade_proposal(
    db: Session,
    portfolio_id: str,
    candidates: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
) -> TradeProposal:
    """Build the deterministic trade proposal for the advisor cycle.

    Args:
        db: Active session.
        portfolio_id: Paper portfolio the proposal targets.
        candidates: Discover candidates (dicts with at least ``symbol``;
            optional ``source``).
        config: Optional overrides (``objective``, ``risk_measure``,
            ``max_candidates``).

    Returns:
        TradeProposal — never raises for data gaps; degrades with notes.
    """
    cfg = config or {}
    max_candidates = int(cfg.get("max_candidates", 10))
    notes: list[str] = []

    book_rows = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).all()
    # Quantities now; marked to market once the price series are loaded below.
    # Cost basis is NOT interchangeable with market value here: the caller
    # compares ``current_book`` against ``target_weight * total_value``, and
    # ``total_value`` is a market-value figure. Mixing the two makes a winner
    # look underweight and a loser look overweight.
    book_quantities = {
        h.ticker.upper(): float(h.quantity) for h in book_rows if h.ticker
    }
    book_cost_basis = {
        h.ticker.upper(): float(h.quantity * h.avg_buy_price) for h in book_rows if h.ticker
    }
    # Bypass fix (F9, #1): a held position's stored asset_type/name is the
    # best signal we have for it — classify_instrument(sym) with no name or
    # source (the old fallback below) can't detect a money-market/bond fund
    # by name and silently falls through to "equity". A specific stored
    # asset_type ("etf"/"money_market"/"bond") is trusted directly; "stock"
    # (the ambiguous default) is re-derived via classify_instrument with the
    # holding's real name so a pre-backfill mis-tagged row still resolves
    # correctly here even before the one-time asset_type backfill runs.
    book_types: dict[str, str] = {}
    for h in book_rows:
        if not h.ticker:
            continue
        sym = h.ticker.upper()
        book_types[sym] = (
            h.asset_type
            if h.asset_type in ("etf", "money_market", "bond")
            else classify_instrument(sym, name=h.name)
        )

    cand_symbols: list[str] = []
    cand_types: dict[str, str] = {}
    for cand in candidates[:max_candidates]:
        sym = str(cand["symbol"]).upper()
        if sym in cand_types:
            continue
        cand_symbols.append(sym)
        cand_types[sym] = classify_instrument(sym, cand.get("source"), cand.get("name"))

    universe = list(dict.fromkeys([*book_quantities.keys(), *cand_symbols]))

    series_by_symbol: dict[str, pd.Series] = {}
    mc_summaries: dict[str, McSummary] = {}
    for sym in universe:
        series = _price_series(db, sym)
        if series is None:
            notes.append(f"{sym}: insufficient price history — excluded")
            continue
        series_by_symbol[sym] = series
        summary = _mc_summary(
            sym, series, cand_types.get(sym) or book_types.get(sym) or classify_instrument(sym)
        )
        if summary is not None:
            mc_summaries[sym] = summary

    # Mark the book to market; fall back to cost basis where no series loaded.
    current_book: dict[str, float] = {}
    for sym, qty in book_quantities.items():
        series = series_by_symbol.get(sym)
        if series is not None and len(series):
            current_book[sym] = qty * float(series.iloc[-1])
        else:
            current_book[sym] = book_cost_basis.get(sym, 0.0)
            notes.append(f"{sym}: no price series — book marked at cost basis")

    # Optimizer over the evaluable universe.
    suggested_weights: dict[str, float] = {}
    optimizer_status = "unavailable"
    evaluable = [s for s in universe if s in series_by_symbol]
    # Computed unconditionally (not just inside the optimizer try-block) so
    # it's still available on the returned TradeProposal for the risk gate's
    # remediation loop (F10) even when the optimizer path is skipped/fails.
    instrument_types = {
        s: cand_types.get(s) or book_types.get(s) or classify_instrument(s)
        for s in evaluable
    }
    if len(evaluable) >= 2:
        try:
            from app.foundation.quant_optim import run_optimisation

            price_matrix = {
                s: {
                    str(cast(pd.Timestamp, idx).date()): float(v)
                    for idx, v in series_by_symbol[s].items()
                }
                for s in evaluable
            }
            from app.foundation.settings import get_risk_free_rate

            total_book_value = sum(current_book.values())
            previous_weights = (
                {s: current_book.get(s, 0.0) / total_book_value for s in evaluable}
                if total_book_value > 0
                else None
            )

            result = run_optimisation(
                price_matrix,
                objective=str(cfg.get("objective", "max_ratio")),
                risk_measure=str(cfg.get("risk_measure", "cvar")),
                risk_free_rate=get_risk_free_rate(db),
                instrument_types=instrument_types,
                previous_weights=previous_weights,
                max_turnover=0.20,
            )
            optimizer_status = str(result.get("status", "unavailable"))
            if result.get("weights"):
                suggested_weights = {
                    str(k).upper(): float(v) for k, v in result["weights"].items()
                }
        except Exception:
            logger.exception("build_trade_proposal: optimiser failed for %s", portfolio_id)
            notes.append("optimizer failed — no suggested weights")
    else:
        notes.append("fewer than 2 evaluable symbols — optimizer skipped")

    if not suggested_weights and evaluable:
        # Honest fallback: equal weight over evaluable names, flagged in notes.
        suggested_weights = {s: 1.0 / len(evaluable) for s in evaluable}
        notes.append("optimizer unavailable — equal-weight fallback")

    envelope = _portfolio_risk_envelope(suggested_weights, series_by_symbol)

    return TradeProposal(
        portfolio_id=portfolio_id,
        suggested_weights=suggested_weights,
        mc_summaries=mc_summaries,
        risk_envelope=envelope,
        current_book=current_book,
        optimizer_status=optimizer_status,
        notes=notes,
        price_series=series_by_symbol,
        instrument_types=instrument_types,
    )
