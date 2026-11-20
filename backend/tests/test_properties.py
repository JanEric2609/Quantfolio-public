"""Property tests (Hypothesis) on the money maths: a few invariants that must hold for any input (WP12.9)."""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.foundation.allocation import allocate_contribution, allocation_candidates
from app.foundation.quant_metrics import historical_cvar, historical_var, max_drawdown
from app.foundation.tax_calc.jurisdictions.de.allowances import (
    apply_sparer_pauschbetrag,
    kirchensteuer_amount,
    reduced_kest_amount,
    soli_amount,
)
from app.foundation.tax_calc.jurisdictions.de.income_tax import guenstigerpruefung, income_tax
from app.foundation.tax_calc.jurisdictions.de.teilfreistellung import apply_teilfreistellung

money = st.decimals(min_value=Decimal("0"), max_value=Decimal("5000000"), places=2)
rate = st.decimals(min_value=Decimal("0"), max_value=Decimal("0.09"), places=3)
daily_returns = st.lists(st.floats(min_value=-0.5, max_value=0.5, allow_nan=False), min_size=0, max_size=300)
quick = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@quick
@given(income=money, allowance=money, used=money)
def test_allowance_never_exceeds_income_or_goes_negative(income, allowance, used):
    after, used_now = apply_sparer_pauschbetrag(income, allowance, used)
    assert 0 <= used_now <= income
    assert 0 <= after <= income
    assert after + used_now == income


@quick
@given(kest=money, church=rate)
def test_surcharges_are_never_negative_and_never_above_the_kest(kest, church):
    assert soli_amount(kest) >= 0
    assert 0 <= kirchensteuer_amount(kest, church) <= kest * Decimal("0.09") + Decimal("0.01")


@quick
@given(base=money, wht=money, church=rate)
def test_kest_is_never_negative_and_never_above_the_flat_rate(base, wht, church):
    kest = reduced_kest_amount(base, wht, church)
    assert kest >= 0
    assert kest <= base / Decimal("4") + Decimal("0.01")


@quick
@given(gross=money, pct=st.decimals(min_value=Decimal("0"), max_value=Decimal("0.6"), places=2))
def test_teilfreistellung_never_taxes_more_than_the_gain(gross, pct):
    taxable = apply_teilfreistellung(gross, pct)
    assert 0 <= taxable <= gross


@quick
@given(a=money, b=money, year=st.sampled_from([2024, 2025, 2026]))
def test_income_tax_is_monotone_and_below_income(a, b, year):
    lo, hi = sorted((a, b))
    assert 0 <= income_tax(lo, year) <= income_tax(hi, year)
    assert income_tax(hi, year) <= hi


@quick
@given(capital=money, other=money, year=st.sampled_from([2025, 2026]), spouse=st.booleans())
def test_guenstigerpruefung_picks_the_cheaper_side(capital, other, year, spouse):
    g = guenstigerpruefung(year, capital_income_eur=capital, other_income_eur=other, spouse=spouse)
    assert g.flat_tax_eur >= 0 and g.tariff_tax_eur >= 0
    if g.use_tariff:
        assert g.tariff_tax_eur < g.flat_tax_eur


@quick
@given(returns=daily_returns)
def test_drawdown_is_within_minus_one_and_zero(returns):
    dd = max_drawdown(returns)
    assert -1.0 <= dd["max_drawdown"] <= 0.0
    assert 0 <= dd["max_drawdown_duration"] <= len(returns)


@quick
@given(returns=st.lists(st.floats(min_value=-0.5, max_value=0.5, allow_nan=False), min_size=20, max_size=300))
def test_cvar_is_at_least_as_bad_as_var(returns):
    assert historical_cvar(returns, 0.95) >= historical_var(returns, 0.95) - 1e-12


@quick
@given(
    current=st.lists(st.floats(min_value=0, max_value=1e6), min_size=2, max_size=6),
    shares=st.lists(st.integers(min_value=1, max_value=100), min_size=2, max_size=6),
    contribution=st.floats(min_value=1, max_value=1e5),
)
def test_contribution_is_fully_allocated_and_nothing_is_sold(current, shares, contribution):
    n = min(len(current), len(shares))
    keys = [f"L{i}" for i in range(n)]
    targets = {k: 100.0 * shares[i] / sum(shares[:n]) for i, k in enumerate(keys)}
    out = allocate_contribution({k: current[i] for i, k in enumerate(keys)}, targets, contribution)
    assert all(v >= -1e-9 for v in out.values())
    assert abs(sum(out.values()) - contribution) <= 1e-6 * max(1.0, contribution)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 10_000), n=st.integers(2, 4))
def test_candidate_weights_are_long_only_and_sum_to_one(seed, n):
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        rng.normal(0.0003, 0.01, size=(250, n)), columns=[f"A{i}" for i in range(n)],
        index=pd.date_range("2024-01-01", periods=250, freq="B"),
    )
    out = allocation_candidates(frame, {})
    for method in out["methods"].values():
        w = np.array(list(method["weights"].values()))
        assert (w >= -1e-9).all()
        assert abs(w.sum() - 1.0) < 1e-6
