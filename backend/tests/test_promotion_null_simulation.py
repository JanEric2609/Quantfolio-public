"""WP10: the promotion decision keeps its stated error rate under a null with the real overlap structure."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "promotion_null_sim.py"


def _load():
    spec = importlib.util.spec_from_file_location("promotion_null_sim", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_overlapping_newey_west_test_promotes_far_too_many_null_challengers():
    sim = _load()
    out = sim.simulate("hln", sims=600, seed=3)
    assert out["overall_rate"] > 4 * out["stated_overall"]            # the defect the harness reproduces


@pytest.mark.slow
def test_the_nonoverlapping_test_keeps_the_stated_overall_level():
    sim = _load()
    out = sim.simulate("every21", sims=6000, seed=11)
    low, high = out["ci95"]
    assert low < 1.6 * out["stated_overall"] and out["overall_rate"] < 2.0 * out["stated_overall"]
    assert max(out["first_promotion_by_look"]) < 2 * out["stated_per_look"]
