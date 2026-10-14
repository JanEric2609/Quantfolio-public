"""MC request validation (found by the authenticated API scan)."""
import pytest
from pydantic import ValidationError

from app.interface.api.quant_mc import MAX_GRID_CELLS, McSpecSchema

VALID = {
    "model": "gbm_euler",
    "model_params": {"S0": 100.0, "mu": 0.05, "sigma": 0.2},
    "payoff": "european_call",
    "payoff_params": {"strike": 100.0},
}


def test_the_documented_example_is_valid():
    McSpecSchema(**VALID)
    McSpecSchema(**{**VALID, "distribution": "nig", "distribution_params": {"alpha": 2.0, "beta": 0.5}})


@pytest.mark.parametrize("change", [
    {"horizon_steps": 0},  # dt = T / n_steps divided by zero
    {"T": 0},
    {"n_paths": 0},
    {"n_paths": 1_000_000, "horizon_steps": MAX_GRID_CELLS // 1_000_000 + 1},  # memory
    {"distribution": "nig"},  # alpha and beta missing
    {"distribution": "nig", "distribution_params": {"alpha": 0.5, "beta": 1.0}},  # alpha <= |beta|
    {"distribution": "student_t", "distribution_params": {"nu": 2}},  # infinite variance
    {"payoff_params": {}},  # strike missing
    {"payoff_params": {"strike": "100"}},
    {"model_params": {"S0": float("nan")}},
    {"model_params": {"S0": -1}},
    {"distribution_params": {"bogus": 1.0}},
])
def test_unbuildable_requests_are_refused(change):
    with pytest.raises(ValidationError):
        McSpecSchema(**{**VALID, **change})
