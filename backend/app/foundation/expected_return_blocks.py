"""Building-block expected-return estimator (M5 Option 1, er_mode="blocks").

Audit F14 rationale: the trailing 3y CAGR anchor has ~zero out-of-sample
predictive value (Goyal-Welch 2008; Vanguard found R²≈0 at the 1-year horizon;
DeBondt-Thaler reversal implies the wrong SIGN at horizons ≥2y). Industry
practice ships building-block decompositions instead (Vanguard-style: dividend
yield + trend earnings growth + valuation reversion). This module implements
that estimator as a PURE function — no db, no network; callers fetch inputs.

Methodology (blueprint M5 Option 1 SUPERSEDED by Oracle corrections):

- Equity/ETF: ``raw = div_yield + smoothed_trend_eps_growth + cape_reversion``.
- Money-market: current yield path (never any trailing CAGR — XEON.DE
  incident precedent, see expected_return.py).
- Bond: explicitly deferred (no YTM source exists in the ingestion path —
  verified; see the TODO in :func:`building_block_er`).

Shrinkage is Bühlmann-style FIXED-credibility shrinkage toward the asset-class
grand mean::

    er = grand_mean + w * (raw - grand_mean),  w pinned at SHRINKAGE_W_CAP

Oracle verdict, binding: this is NOT James-Stein and must never be described
as such (no loss-function optimality claim, no data-derived w). ``w`` is fixed
at the cap by design — conservative by construction; the constant exists so a
future data-driven credibility weight can replace the pin without touching
call sites.

Grand means are ANNUALLY-REFRESHED CONSTANTS with documented source — never
runtime-computed from the same universe they anchor (circularity).
"""
from __future__ import annotations

from typing import Any

# Long-run nominal asset-class returns, Dimson-Marsh-Staunton-style convention
# (cf. the Credit Suisse Global Investment Returns Yearbook lineage of long-run
# equity-premium studies): world equities ~7% nominal, diversified bonds ~4%,
# cash/money-market ~2%. Order-of-magnitude anchors for shrinkage, not forecasts.
# SOURCE: long-run asset-class study convention (DMS-style); REFRESHED: annually,
# last set 2026-08-23. Next refresh due January 2027.
GRAND_MEAN_ANNUAL: dict[str, float] = {
    "equity": 0.07,
    "etf": 0.07,
    "bond": 0.04,
    "money_market": 0.02,
}

# Fixed credibility weight. Pinned at the cap by design (conservative); a
# future data-driven weight replaces the pin, not the call sites.
SHRINKAGE_W_CAP = 0.40

# CAPE reversion term clamp, annualized percentage points (±2pp per Oracle).
CAPE_REVERSION_CLAMP_PPTS = 2.0

# Single-period realized EPS growth is a NOISY estimator of the smoothed trend
# (one boom year prints +40% and means nothing about the next decade), so it is
# clamped to a plausible long-run trend band before entering the sum.
_EPS_GROWTH_CLAMP = (-0.05, 0.15)

_CAPE_REVERSION_HALF_LIFE_YEARS = 10.0


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cape_term(
    cape_ratio: float | None, market_avg_cape: float | None, concerns: list[str]
) -> float:
    """Halfway reversion toward the long-run average CAPE over 10y, annualized.

    Optional unlike dividend/growth: absent valuation input degrades to a 0.0
    term plus an explicit concern, never to a missing estimate.
    """
    ratio = _as_float(cape_ratio)
    avg = _as_float(market_avg_cape)
    if ratio is None or avg is None or ratio <= 0:
        concerns.append("cape_unavailable")
        return 0.0
    raw = 0.5 * (avg / ratio - 1.0) / _CAPE_REVERSION_HALF_LIFE_YEARS
    clamp = CAPE_REVERSION_CLAMP_PPTS / 100.0
    return max(-clamp, min(clamp, raw))


def building_block_er(
    instrument_type: str,
    fundamentals: dict[str, Any],
    cape_ratio: float | None,
    market_avg_cape: float | None,
    current_yield_annual: float | None,
) -> dict[str, Any]:
    """Forward-looking annual expected return from fundamental building blocks.

    Args:
        instrument_type: equity | etf | money_market | bond.
        fundamentals: provider payload; reads ``dividend_yield`` and
            ``earnings_growth`` (annual fractions) for equity/etf.
        cape_ratio: current cyclically-adjusted PE (None until a macro feed
            exists — term degrades to 0.0 with a ``cape_unavailable`` concern).
        market_avg_cape: long-run average CAPE paired with ``cape_ratio``.
        current_yield_annual: money-market current yield (annual fraction);
            the dispatcher passes the trailing_1m annualized reading as a
            documented proxy (XEON.DE precedent).

    Returns:
        ``{"er_annual": float|None, "components": dict, "concerns": list[str]}``.
        ``er_annual`` is None whenever a REQUIRED block is missing (then the
        caller falls back to the trailing path); optional blocks degrade to
        zero/neutral with concerns instead.
    """
    concerns: list[str] = []

    if instrument_type == "bond":
        # TODO(M5 Option 1 follow-up): land when YTM ingestion exists in the
        # provider chain. A bond's expected return is its yield to maturity;
        # fabricating one from trailing price CAGR would embed duration/
        # rate-regime noise into a forward estimate.
        concerns.append("bond_ytm_unavailable")
        return {"er_annual": None, "components": {}, "concerns": concerns}

    if instrument_type == "money_market":
        cy = _as_float(current_yield_annual)
        if cy is None:
            concerns.append("current_yield_unavailable")
            return {"er_annual": None, "components": {}, "concerns": concerns}
        gm = GRAND_MEAN_ANNUAL["money_market"]
        return {
            "er_annual": gm + SHRINKAGE_W_CAP * (cy - gm),
            "components": {"current_yield": cy, "grand_mean": gm, "w": SHRINKAGE_W_CAP},
            "concerns": concerns,
        }

    if instrument_type in ("equity", "etf"):
        div_yield = _as_float(fundamentals.get("dividend_yield"))
        eps_growth_raw = _as_float(fundamentals.get("earnings_growth"))
        if div_yield is None or eps_growth_raw is None:
            concerns.append("building_blocks_incomplete")
            return {
                "er_annual": None,
                "components": {
                    "div_yield": div_yield,
                    "eps_growth_trend": None,
                    "cape_term": None,
                },
                "concerns": concerns,
            }
        eps_growth_trend = max(_EPS_GROWTH_CLAMP[0], min(_EPS_GROWTH_CLAMP[1], eps_growth_raw))
        cape = _cape_term(cape_ratio, market_avg_cape, concerns)
        raw = div_yield + eps_growth_trend + cape
        gm = GRAND_MEAN_ANNUAL[instrument_type]
        return {
            "er_annual": gm + SHRINKAGE_W_CAP * (raw - gm),
            "components": {
                "div_yield": div_yield,
                "eps_growth_trend": eps_growth_trend,
                "cape_term": cape,
                "raw": raw,
                "grand_mean": gm,
                "w": SHRINKAGE_W_CAP,
            },
            "concerns": concerns,
        }

    concerns.append("building_blocks_unsupported_type")
    return {"er_annual": None, "components": {}, "concerns": concerns}
