"""Tests for portfolio analyzer."""
from app.decision.portfolio_advisor.analyzer import compute_gap_analysis, compute_concentration_score, detect_drift


def test_compute_gap_analysis_no_holdings():
    result = compute_gap_analysis([])
    assert result == {"asset_allocation": {}, "concentration": 0, "drift": [], "suggestions": []}


def test_compute_concentration_score_single_holding():
    holdings = [
        {"name": "A", "current_value": 100000, "asset_type": "stock"},
    ]
    score = compute_concentration_score(holdings)
    assert score > 80  # heavily concentrated


def test_compute_concentration_score_diversified():
    """10 equal-weight holdings should score low concentration."""
    holdings = [{"name": f"H{i}", "current_value": 10000, "asset_type": "stock"} for i in range(10)]
    score = compute_concentration_score(holdings)
    assert score < 30


def test_detect_drift():
    """60% stock vs 50% target = 10% overweight drift."""
    positions = [
        {"name": "A", "current_value": 60000, "asset_type": "stock"},
        {"name": "B", "current_value": 30000, "asset_type": "bond"},
        {"name": "C", "current_value": 10000, "asset_type": "cash"},
    ]
    target = {"stock": 0.5, "bond": 0.3, "cash": 0.2}
    total = 100000
    drifts = detect_drift(positions, target, total)
    assert any(d["asset_type"] == "stock" and abs(d["drift_pct"] - 10) < 1 for d in drifts)
