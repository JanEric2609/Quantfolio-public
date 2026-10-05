"""Tests for pulse mode (quick health check)."""
from app.decision.portfolio_advisor.pulse import run_pulse_check


def test_pulse_check_empty_holdings():
    result = run_pulse_check([], regime=None)
    assert result["status"] == "info"
    assert len(result["checks"]) >= 0


def test_pulse_check_with_regime():
    holdings = [
        {"name": "Tech Stock", "current_value": 80000, "asset_type": "stock"},
        {"name": "Bond ETF", "current_value": 20000, "asset_type": "bond"},
    ]
    regime = {"label": "high_vol", "confidence": 0.8, "vix": 25.0}
    result = run_pulse_check(holdings, regime)
    assert len(result["checks"]) > 0
    assert any(c["type"] == "regime" for c in result["checks"])
    assert result["status"] in ("info", "warning", "critical")


def test_pulse_check_concentration_warning():
    holdings = [{"name": "Single Stock", "current_value": 100000, "asset_type": "stock"}]
    result = run_pulse_check(holdings, regime=None)
    assert any(c["type"] == "concentration" for c in result["checks"])
