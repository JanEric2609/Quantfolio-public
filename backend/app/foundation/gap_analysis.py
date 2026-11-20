"""MSCI World factor gap analysis.

Compares a user's portfolio holdings against simplified MSCI World/ACWI
benchmark targets to surface underweight/overweight factor exposures.

BENCHMARK_TARGETS are approximate MSCI ACWI/World weights, stable enough
for personal portfolio guidance. Review annually.
"""
from __future__ import annotations

BENCHMARK_TARGETS: dict[str, float] = {
    "emerging_markets": 0.12,
    "small_cap": 0.10,
    "bonds": 0.00,
    "real_estate": 0.03,
    "quality_tilt": 0.20,
    "momentum_tilt": 0.20,
    "value_tilt": 0.15,
}

# Known ETF factor profiles — extend as new ETFs are recommended.
# Each entry maps to region + factors list. Factors must match BENCHMARK_TARGETS keys.
ETF_PROFILES: dict[str, dict] = {
    "EUNL.DE":  {"region": "DM",     "factors": []},
    "IWDA.AS":  {"region": "DM",     "factors": []},
    "URTH":     {"region": "DM",     "factors": []},
    "EIMI.L":   {"region": "EM",     "factors": []},
    "IUSN.DE":  {"region": "DM",     "factors": ["small_cap"]},
    "AGGH.L":   {"region": "global", "factors": ["bonds"]},
    "IAGG.L":   {"region": "global", "factors": ["bonds"]},
    "IWMO.L":   {"region": "DM",     "factors": ["momentum_tilt"]},
    "IWQU.L":   {"region": "DM",     "factors": ["quality_tilt"]},
    "IWVL.L":   {"region": "DM",     "factors": ["value_tilt"]},
    "IQQH.DE":  {"region": "DM",     "factors": ["real_estate"]},
    "VNRT.L":   {"region": "DM",     "factors": ["real_estate"]},
    "WSML.L":   {"region": "DM",     "factors": ["small_cap"]},
}

GAP_REPORTING_THRESHOLD = 0.05  # only report gaps larger than 5 percentage points


def _profile_for(ticker: str) -> dict:
    return ETF_PROFILES.get(ticker.upper(), {"region": "unknown", "factors": []})


def compute_factor_gaps(
    holdings: list[dict],
    stale_factors: set[str] | None = None,
) -> list[dict]:
    """Return list of factor gap dicts for the given holdings.

    Each gap: {"factor": str, "current": float, "target": float,
               "gap": float, "direction": "underweight"|"overweight"}

    holdings: list of {"ticker": str, "weight": float, ...}
    stale_factors: factors with decayed IC that should be excluded from reporting.
    """
    stale = stale_factors or set()
    actual: dict[str, float] = {k: 0.0 for k in BENCHMARK_TARGETS}

    for h in holdings:
        ticker = h.get("ticker", "").upper()
        weight = float(h.get("weight", 0.0))
        if not ticker or weight <= 0:
            continue
        profile = _profile_for(ticker)
        if profile["region"] == "EM":
            actual["emerging_markets"] += weight
        for factor in profile.get("factors", []):
            if factor in actual:
                actual[factor] += weight

    gaps = []
    for factor, target in BENCHMARK_TARGETS.items():
        if factor in stale:
            continue
        current = actual.get(factor, 0.0)
        gap = target - current
        if abs(gap) > GAP_REPORTING_THRESHOLD:
            gaps.append({
                "factor": factor,
                "current": round(current, 3),
                "target": round(target, 3),
                "gap": round(gap, 3),
                "direction": "underweight" if gap > 0 else "overweight",
            })

    # Risk metric comparison gaps require actual time-series returns, not single-period proxies.
    # Skip computing fake Sharpe/Sortino from single data points.
    risk_gaps: list[dict] = []

    all_gaps = gaps + risk_gaps
    return sorted(all_gaps, key=lambda x: abs(x["gap"]), reverse=True)
