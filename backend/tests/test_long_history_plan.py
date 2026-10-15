"""ADR 0022's harness on synthetic data (the JST file never enters the tests)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.lab.long_history import PlanCosts, bootstrap_indices, certainty_equivalent, home_panel, simulate_rule
from app.lab.long_history.plan_bootstrap import BILL, BOND, GLOBAL, haircut

FREE = PlanCosts(ter=(0.0, 0.0, 0.0, 0.0, 0.0), trade_cost=0.0, taxed=False)


def _raw() -> pd.DataFrame:
    rows = []
    for year in range(1900, 1906):
        # Home "USA": equity +10 %, no inflation. "UK": equity 0 % but its currency gains 10 % a year.
        rows.append({"country": "USA", "year": year, "eq_tr": 0.10, "bond_tr": 0.03, "bill_rate": 0.01,
                     "cpi": 100.0, "xrusd": 1.0, "gdp": 100.0})
        rows.append({"country": "UK", "year": year, "eq_tr": 0.0, "bond_tr": 0.02, "bill_rate": 0.01,
                     "cpi": 100.0, "xrusd": 1.0 / 1.1 ** (year - 1900), "gdp": 100.0 * 1.1 ** -(year - 1900)})
    return pd.DataFrame(rows)


def test_foreign_equity_is_converted_into_home_currency_and_gdp_weighted():
    panel = home_panel(_raw(), "USA")
    assert list(panel.columns) == ["global_eq", "home_eq", "intl_eq", "bond", "bill"]
    # UK equity in dollars: (1 + 0) * 1.1 - 1 = +10 %; equal dollar GDP: global = 10 %.
    assert panel["intl_eq"].iloc[0] == pytest.approx(0.10)
    assert panel["global_eq"].iloc[0] == pytest.approx(0.10)
    assert panel.index.min() == 1901  # the first year has no previous CPI or FX


def test_the_bootstrap_draws_blocks_from_the_pool():
    rng = np.random.default_rng(0)
    pool = np.arange(50, 100)
    idx = bootstrap_indices(pool, 2000, 30, rng)
    assert idx.min() >= 50 and idx.max() < 100
    cont = np.mean((np.diff(idx, axis=1) == 1) | (np.diff(idx, axis=1) == -49))
    assert cont == pytest.approx(0.9, abs=0.02)  # mean block 10 years


def test_zero_returns_and_no_costs_give_back_the_contributions():
    returns = np.zeros((40, 5))
    idx = np.zeros((10, 30), dtype=int)
    for rule in ("R0", "R1", "R2", "R3", "R4", "R5"):
        out = simulate_rule(returns, idx, rule, 30, FREE)
        assert out["wealth"] == pytest.approx(np.full(10, 12.0 * 30))


def test_bonds_cost_growth_when_equity_beats_them():
    returns = np.zeros((1, 5))
    returns[0, :3] = 0.05
    idx = np.zeros((1, 30), dtype=int)
    r0 = simulate_rule(returns, idx, "R0", 30, FREE)["wealth"][0]
    r3 = simulate_rule(returns, idx, "R3", 30, FREE)["wealth"][0]
    assert r3 < r0


def test_rebalancing_gains_above_the_allowance_are_taxed():
    returns = np.zeros((2, 5))
    returns[0, GLOBAL] = 0.5
    returns[1, BOND] = 0.0
    idx = np.zeros((1, 6), dtype=int)
    taxed = simulate_rule(returns, idx, "R3", 6, PlanCosts(ter=(0, 0, 0, 0, 0), trade_cost=0.0, allowance=0.0))
    free = simulate_rule(returns, idx, "R3", 6, FREE)
    assert taxed["wealth"][0] < free["wealth"][0]


def test_r5_holds_the_withdrawal_in_bills_in_its_last_five_years():
    returns = np.zeros((1, 5))
    returns[0, BILL] = 0.0
    idx = np.zeros((3, 30), dtype=int)
    out = simulate_rule(returns, idx, "R5", 30, FREE)
    assert not out["withdrawal_short"].any()


def test_the_haircut_only_lowers_equity():
    returns = np.zeros((3, 5))
    returns[:, GLOBAL] = 0.06
    returns[:, BILL] = 0.01
    cut = haircut(returns)
    assert cut[0, GLOBAL] == pytest.approx(0.06 - 0.3 * 0.05)
    neg = returns.copy()
    neg[:, GLOBAL] = -0.02
    assert haircut(neg)[0, GLOBAL] == pytest.approx(-0.02)


def test_the_certainty_equivalent_punishes_spread():
    w = np.array([50.0, 150.0])
    assert certainty_equivalent(w, 5.0) < certainty_equivalent(w, 1.0) < 100.0
