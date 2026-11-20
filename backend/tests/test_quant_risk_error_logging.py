"""Risk endpoints must log — not silently swallow — computation failures (#136).

`value_at_risk` previously caught every exception and returned an empty
result with no log record, making failures invisible to operators. It now logs
at warning. (The efficient-frontier endpoint was removed on 2026-10-04.)
"""
import logging

from app.interface.api.quant import risk


class _User:
    id = "u1"


def test_value_at_risk_logs_computation_failure(monkeypatch, caplog):
    # Non-numeric prices make the pandas returns computation raise; the handler
    # must log rather than silently degrade to "unavailable".
    bad_matrix = (
        {"AAA": {"2024-01-01": "x", "2024-01-02": "y"}},
        {"AAA": 1.0},
    )
    monkeypatch.setattr(risk, "_portfolio_price_matrix", lambda db, uid: bad_matrix)

    with caplog.at_level(logging.WARNING, logger="app.interface.api.quant.risk"):
        result = risk.value_at_risk(db=object(), user=_User())

    assert result["status"] == "unavailable"
    assert any("var" in r.message.lower() or "risk" in r.message.lower() for r in caplog.records)
