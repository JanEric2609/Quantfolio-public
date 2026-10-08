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


def test_uniform_regime_weights_give_a_plain_warning_not_critical():
    """Nothing learned yet (uniform weights) must not read as a proven regime signal."""
    from app.decision.portfolio_advisor.pulse import run_pulse_check

    uniform = {a: 1 / 6 for a in ("buy_equity", "buy_bond", "hold_cash", "hold_position", "sell", "rebalance")}
    out = run_pulse_check([], {"label": "bear", "confidence": 0.9}, regime_weights=uniform)
    regime = [c for c in out["checks"] if c["type"] == "regime"]
    assert regime and regime[0]["severity"] == "warning"
    learned = {**uniform, "buy_equity": 0.45, "hold_cash": 0.35}
    out = run_pulse_check([], {"label": "bear", "confidence": 0.9}, regime_weights=learned)
    assert [c for c in out["checks"] if c["type"] == "regime"][0]["severity"] == "critical"
