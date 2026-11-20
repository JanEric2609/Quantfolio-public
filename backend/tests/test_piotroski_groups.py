from app.foundation.piotroski import _score


def test_valuation_checks_are_grouped_apart_from_quality():
    out = _score({"roe": 0.2, "pe_ratio": 15.0, "ev_ebitda": 10.0, "profit_margin": 0.1,
                  "free_cash_flow_yield": 0.05, "gross_margin": 0.4})
    groups = {s["name"]: s["group"] for s in out["signals"]}
    assert groups["P/E 0–40"] == "valuation"
    assert groups["EV/EBITDA < 25"] == "valuation"
    assert sum(g == "valuation" for g in groups.values()) == 2
    assert groups["ROE > 0"] == "quality"
