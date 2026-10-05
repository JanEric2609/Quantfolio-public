"""Pulse mode — quick portfolio health check (no LLM)."""
from __future__ import annotations

from .analyzer import compute_gap_analysis


def run_pulse_check(holdings: list[dict], regime: dict | None, regime_weights: dict | None = None, lookthrough: dict | None = None) -> dict:
    """Quick health check combining gap analysis and regime awareness.

    Args:
        holdings: List of holding dicts.
        regime: Regime snapshot dict.
        regime_weights: MWU weights (action → weight) for adjusting check severity.
        lookthrough: Optional ETF lookthrough dict from portfolio_lookthrough().
    """
    gap = compute_gap_analysis(holdings, lookthrough)
    checks: list[dict] = []

    # Regime check — severity adjusted by MWU weights
    if regime:
        reg_label = regime.get("label", "unknown")
        reg_confidence = regime.get("confidence", 0)

        # Check if regime signal is trusted based on MWU weights
        regime_weight = 1.0
        if regime_weights:
            equity_w = regime_weights.get("buy_equity", 0.2) + regime_weights.get("hold_cash", 0.2)
            regime_weight = min(1.0, equity_w * 2.5)  # scale to [0, 1]

        if reg_label in ("bear", "high_vol") and reg_confidence > 0.5:
            severity = "warning"
            if regime_weight < 0.3:
                severity = "info"  # MWU suggests regime signal isn't reliable here
            elif regime_weight > 0.7:
                severity = "critical"  # regime signal proven reliable
            checks.append({
                "type": "regime",
                "severity": severity,
                "message": f"Adverse regime detected: {reg_label}",
                "detail": f"VIX: {regime.get('vix')}, confidence: {reg_confidence:.0%}, mwu_weight: {regime_weight:.2f}",
            })
        elif reg_label == "bull":
            checks.append({
                "type": "regime",
                "severity": "info",
                "message": "Bullish regime — favorable for equities",
                "detail": f"Confidence: {reg_confidence:.0%}",
            })

    # Drift checks from gap analysis
    for s in gap.get("suggestions", []):
        checks.append(s)

    # Concentration check
    conc = gap.get("concentration", 0)
    if conc > 50:
        checks.append({
            "type": "concentration",
            "severity": "warning" if conc < 70 else "critical",
            "message": "High concentration risk",
            "detail": f"Concentration score: {conc:.0f}/100",
        })

    # Determine overall status
    severities = [c["severity"] for c in checks]
    if "critical" in severities:
        status = "critical"
    elif "warning" in severities:
        status = "warning"
    else:
        status = "info"

    return {
        "status": status,
        "checks": checks,
        "concentration": conc,
        "drift_count": len(gap.get("drift", [])),
        "allocation": gap.get("asset_allocation", {}),
    }
