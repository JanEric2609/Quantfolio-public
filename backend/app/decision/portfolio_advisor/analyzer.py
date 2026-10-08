"""Portfolio analysis: gap, concentration, drift detection."""
from __future__ import annotations


def compute_gap_analysis(
    holdings: list[dict],
    lookthrough: dict | None = None,
    *,
    target: dict[str, float] | None = None,
) -> dict:
    """Analyze asset allocation gaps vs *target*.

    The target (asset type → fraction of the book, 0-1) is an explicit
    argument: the single source of targets is the monthly plan's sleeves
    (``decision.monthly_plan.sleeve_targets_from``), and this module defines
    none of its own (ADR 0019 §1). Without a target no drift is computed.
    """
    if not holdings:
        return {"asset_allocation": {}, "concentration": 0, "drift": [], "suggestions": []}

    total = sum(h.get("current_value", 0) or 0 for h in holdings)
    if total <= 0:
        return {"asset_allocation": {}, "concentration": 0, "drift": [], "suggestions": []}

    by_type: dict[str, float] = {}
    for h in holdings:
        at = h.get("asset_type", "other")
        by_type[at] = by_type.get(at, 0) + (h.get("current_value", 0) or 0)

    allocation = {k: round(v / total * 100, 1) for k, v in sorted(by_type.items())}
    concentration = compute_concentration_score(holdings)
    drifts = detect_drift(holdings, target, total) if target else []
    suggestions = _generate_suggestions(allocation, drifts, concentration, holdings, lookthrough)
    return {
        "asset_allocation": allocation,
        "concentration": round(concentration, 1),
        "drift": drifts,
        "suggestions": suggestions,
    }


def compute_concentration_score(holdings: list[dict]) -> float:
    """Herfindahl-style concentration (0-100)."""
    values = [h.get("current_value", 0) or 0 for h in holdings]
    total = sum(values)
    if total <= 0:
        return 0.0
    hhi = sum((v / total) ** 2 for v in values)
    return round(hhi * 100, 1)


def detect_drift(positions: list[dict], target: dict[str, float], total: float) -> list[dict]:
    """Measure drift from target allocation."""
    by_type: dict[str, float] = {}
    for p in positions:
        at = p.get("asset_type", "other")
        by_type[at] = by_type.get(at, 0) + (p.get("current_value", 0) or 0)

    drifts = []
    for asset_type, target_pct in target.items():
        actual_pct = (by_type.get(asset_type, 0) / total * 100) if total > 0 else 0
        target_pct_val = target_pct * 100
        drift_pct = round(actual_pct - target_pct_val, 1)
        if abs(drift_pct) > 2:
            drifts.append({
                "asset_type": asset_type,
                "actual_pct": round(actual_pct, 1),
                "target_pct": round(target_pct_val, 1),
                "drift_pct": drift_pct,
                "direction": "overweight" if drift_pct > 0 else "underweight",
                "suggested_action": _suggest_action(asset_type, drift_pct),
            })
    return sorted(drifts, key=lambda d: abs(d["drift_pct"]), reverse=True)


def _suggest_action(asset_type: str, drift_pct: float) -> str:
    if drift_pct > 5:
        return f"Reduce {asset_type} allocation by {abs(drift_pct):.0f}%"
    elif drift_pct < -5:
        return f"Increase {asset_type} allocation by {abs(drift_pct):.0f}%"
    return "Monitor"


def _generate_suggestions(
    allocation: dict,
    drifts: list[dict],
    concentration: float,
    holdings: list[dict],
    lookthrough: dict | None = None,
) -> list[dict]:
    suggestions = []
    for d in drifts:
        suggestions.append({
            "type": "rebalance",
            "severity": "warning" if abs(d["drift_pct"]) > 5 else "info",
            "message": d["suggested_action"],
            "detail": f"{d['asset_type']}: {d['actual_pct']}% vs target {d['target_pct']}%",
        })
    if concentration > 50:
        suggestions.append({
            "type": "concentration",
            "severity": "warning",
            "message": "Portfolio is heavily concentrated",
            "detail": f"Concentration score: {concentration:.0f}/100. Consider diversifying.",
        })

    etf_count = sum(1 for h in holdings if h.get("asset_type") == "etf")
    if etf_count > 0:
        if lookthrough and lookthrough.get("lookthrough_count", 0) > 0:
            underlying = lookthrough["lookthrough_count"]
            suggestions.append({
                "type": "diversification",
                "severity": "info",
                "message": (
                    f"Your {etf_count} ETF position{'s' if etf_count > 1 else ''} diversify into "
                    f"approximately {underlying} underlying stocks — your effective concentration "
                    f"is much lower than it appears."
                ),
                "detail": (
                    f"Effective HHI: {lookthrough.get('effective_hhi', 0):.2f} vs "
                    f"holding-level HHI: {concentration / 100:.2f}"
                ),
            })
        else:
            suggestions.append({
                "type": "diversification",
                "severity": "info",
                "message": (
                    f"Your portfolio contains {etf_count} ETF position{'s' if etf_count > 1 else ''}. "
                    f"ETFs provide built-in diversification across many underlying securities."
                ),
                "detail": "Look-through analysis not available for these ETFs.",
            })
    return suggestions
