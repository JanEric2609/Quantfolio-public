from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("score_truth_set", Path(__file__).parents[1] / "scripts" / "score_truth_set.py")
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_scores_numeric_within_tolerance_and_exact_fields():
    truth = [{"ticker": "AAA", "ebitda": "1000", "eps": "", "currency": "EUR", "period_months": "6", "total_debt": "500"}]
    provided = {"AAA": {"ebitda": 1015, "currency": "EUR", "period_months": 12, "total_debt": None}}
    out = mod.score(truth, provided, 0.02)
    assert out["fields"]["ebitda"] == [1, 1]
    assert out["fields"]["currency"] == [1, 1]
    assert out["fields"]["period_months"] == [0, 1]
    assert out["fields"]["total_debt"] == [0, 1]
    assert "eps" not in out["fields"]
    assert len(out["misses"]) == 2


def test_template_loads_skipping_comments(tmp_path):
    p = Path(__file__).parents[2] / "docs" / "research" / "truth-set" / "truth_set_template.csv"
    rows = mod.load_truth(p)
    assert rows and rows[0]["ticker"] == "WKL.AS"
