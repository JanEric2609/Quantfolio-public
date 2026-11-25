"""Algo lab prototype (ADR 0029): promotion ledger and gate maths. Pure, no DB."""
import math

import numpy as np
import pytest

from app.lab.algo_lab import (
    LedgerError,
    PromotionLedger,
    breakeven_cost_bps,
    clipped_score,
    holm_alpha,
    minimum_detectable_sharpe,
    net_sharpe,
    oos_replication_ratio,
    per_order_cost_eur,
    prob_drawdown_breach,
    years_to_detect,
)


def test_clipped_score_bounds_and_scale():
    assert clipped_score(0.5, 0.01) == 1.0
    assert clipped_score(-0.5, 0.01) == -1.0
    assert clipped_score(0.005, 0.01) == pytest.approx(0.5)
    with pytest.raises(LedgerError):
        clipped_score(0.1, 0.0)


def test_budget_is_enforced_and_restart_costs_new_mass():
    led = PromotionLedger(alpha=0.05)
    led.register("a", 0.5, 0.01)
    led.register("b", 0.5, 0.01)
    assert led.remaining == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(LedgerError):
        led.register("c", 0.01, 0.01)
    with pytest.raises(LedgerError):
        led.register("a", 0.1, 0.01)  # no free restart under the same hash


def test_threshold_is_inverse_alpha_mass():
    led = PromotionLedger(alpha=0.05)
    led.register("a", 0.25, 0.01)
    assert led.threshold("a") == pytest.approx(80.0)


def test_observe_is_idempotent_per_event_key():
    led = PromotionLedger()
    led.register("a", 1.0, 0.01)
    first = led.observe("a", "2026-01-02", 0.004)
    again = led.observe("a", "2026-01-02", 0.004)
    assert first == again
    assert led.n_observations("a") == 1


def test_null_evidence_does_not_grow_and_strong_edge_crosses_next_day():
    led = PromotionLedger(alpha=0.05)
    led.register("noise", 0.5, 0.01)
    led.register("edge", 0.5, 0.01)
    rng = np.random.default_rng(1)
    for i, r in enumerate(rng.choice([-0.01, 0.01], size=2000)):
        led.observe("noise", f"d{i}", float(r))
    assert not led.crossed("noise")
    for i in range(2000):
        led.observe("edge", f"d{i}", 0.01 if i % 4 else -0.01)  # 50 % mean score
    assert led.crossed("edge")
    idx = led.first_executable_index("edge")
    assert idx is not None and idx > 1
    assert led.to_dict()["spent"] == pytest.approx(1.0)


def test_false_promotion_rate_under_null_is_below_alpha():
    alpha = 0.05
    rng = np.random.default_rng(7)
    hits = 0
    runs = 300
    for _ in range(runs):
        led = PromotionLedger(alpha=alpha)
        led.register("h", 1.0, 0.01)
        for i, r in enumerate(rng.choice([-0.01, 0.01], size=500)):
            led.observe("h", str(i), float(r))
        hits += led.crossed("h")
    assert hits / runs <= alpha


def test_years_to_detect_and_mdes_are_inverse():
    t = years_to_detect(0.5)
    assert t == pytest.approx(24.7, abs=0.2)
    assert minimum_detectable_sharpe(t) == pytest.approx(0.5, rel=1e-6)
    assert years_to_detect(0.5, trials=4) > t
    assert math.isinf(years_to_detect(0.0))
    assert holm_alpha(0.05, 4) == pytest.approx(0.0125)


def test_replication_ratio_properties():
    short = oos_replication_ratio(0.5, 5)
    long_ = oos_replication_ratio(0.5, 40)
    strong = oos_replication_ratio(1.5, 5)
    assert 0 < short < long_ < 1
    assert strong > short


def test_breakeven_and_net_sharpe_agree():
    be = breakeven_cost_bps(0.4, 0.10, 12.0)
    assert be == pytest.approx(33.3, abs=0.1)
    assert net_sharpe(0.4, 0.10, 12.0, be) == pytest.approx(0.0, abs=1e-9)
    assert net_sharpe(0.4, 0.10, 12.0, 0.0) == pytest.approx(0.4)


def test_per_order_cost_floor_and_cap():
    assert per_order_cost_eur(250, 0.0005, 1.25) == pytest.approx(1.25)
    assert per_order_cost_eur(100_000, 0.0005, 1.25, 29.0) == pytest.approx(29.0)
    assert per_order_cost_eur(100_000, 0.0005, 1.25) == pytest.approx(50.0)


def test_drawdown_breach_probability_rises_with_vol_and_horizon():
    low = prob_drawdown_breach(0.0, 0.10, 3, 0.20, n_paths=4000)
    high = prob_drawdown_breach(0.0, 0.25, 3, 0.20, n_paths=4000)
    assert high > low
    assert prob_drawdown_breach(0.0, 0.25, 3, 0.20, n_paths=4000) == prob_drawdown_breach(
        0.0, 0.25, 3, 0.20, n_paths=4000
    )


def test_clipped_null_is_not_the_return_null():
    """Zero-mean but negatively skewed returns have a positive clipped mean when scale is small.

    The ledger's guarantee is for the clipped score (Benhamou et al. 2611.03384, Sec. 2).
    With a scale that clips the rare loss the false-promotion rate is far above alpha; with
    a scale that does not clip, it is not. ``clipped_fraction`` makes this visible.
    """
    rng = np.random.default_rng(3)
    gain, loss, p_loss = 0.002, -0.038, 0.05  # mean exactly zero
    assert (1 - p_loss) * gain + p_loss * loss == pytest.approx(0.0)
    draws = np.where(rng.random((40, 2520)) < p_loss, loss, gain)
    rates = {}
    for scale in (0.01, 0.05):
        crossed = 0
        for i in range(40):
            led = PromotionLedger()
            led.register("h", 1 / 3, scale)
            for t, r in enumerate(draws[i]):
                led.observe("h", str(t), float(r))
            crossed += led.crossed("h")
            frac = led.to_dict()["candidates"]["h"]["clipped_fraction"]
        rates[scale] = (crossed / 40, frac)
    assert rates[0.01][0] > 0.5 and rates[0.01][1] > 0.04
    assert rates[0.05][0] == 0.0 and rates[0.05][1] == 0.0


def test_years_to_detect_alt_se_is_frequency_matched():
    """Annual Sharpe with years of daily data: the inflation is tiny, and it grows with Sharpe."""
    assert years_to_detect(0.5, alt_se=True) > years_to_detect(0.5)
    ratio_low = years_to_detect(0.5, alt_se=True) / years_to_detect(0.5)
    ratio_high = years_to_detect(1.5, alt_se=True) / years_to_detect(1.5)
    assert ratio_high > ratio_low > 1.0
    assert ratio_high < 1.02
    assert years_to_detect(1.5, alt_se=True) == pytest.approx(2.8, abs=0.1)


def test_non_finite_return_is_rejected_and_adds_no_evidence():
    led = PromotionLedger()
    led.register("h", 0.5, 0.01)
    before = led.observe("h", "d0", 0.0)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(LedgerError):
            led.observe("h", "bad", bad)
    assert led.to_dict()["candidates"]["h"]["n_obs"] == 1
    assert led.observe("h", "d1", 0.0) == pytest.approx(before)
