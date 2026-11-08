"""The outcome-blind noise study of test designs (plan 2026-11-08)."""
import numpy as np
import pandas as pd

from app.lab.calibration.noise_study import centred_rank_weights, efficient_weights, run_study


def _market(n_days=700, n_stocks=120, seed=3):
    """One market factor, five sectors with their own factor, betas 0.5-1.5, vols 1-3 % a day."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2023-01-02", periods=n_days)
    m = rng.normal(0.0003, 0.01, n_days)
    sectors = {f"S{i}": f"sec{i % 5}" for i in range(n_stocks)}
    sec_f = rng.normal(0, 0.006, (n_days, 5))
    beta = rng.uniform(0.5, 1.5, n_stocks)
    idio = rng.uniform(0.01, 0.03, n_stocks)
    r = m[:, None] * beta + sec_f[:, [i % 5 for i in range(n_stocks)]] + rng.normal(0, 1, (n_days, n_stocks)) * idio
    closes = pd.DataFrame(100 * np.cumprod(1 + r, axis=0), index=days, columns=list(sectors))
    core = pd.Series(100 * np.cumprod(1 + m), index=days)
    return closes, core, sectors


def test_efficient_weights_keep_the_exposure_and_are_self_financing():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(300, 40))
    cov = np.cov(a, rowvar=False) + np.eye(40) * 0.1
    w_rank = centred_rank_weights(rng.normal(size=40))
    w = efficient_weights(w_rank, cov, float(w_rank @ w_rank))
    assert abs(w.sum()) < 1e-9
    assert abs(w @ w_rank - w_rank @ w_rank) < 1e-9
    # Minimum variance at that exposure: never above the rank portfolio's own.
    assert w @ cov @ w <= w_rank @ cov @ w_rank + 1e-12


def test_centred_rank_weights_match_the_ledger():
    w = centred_rank_weights(np.array([3.0, 1.0, 2.0, 4.0]))
    assert np.isclose(w[w > 0].sum(), 1.0) and np.isclose(w[w < 0].sum(), -1.0)
    assert w.argmax() == 3 and w.argmin() == 1


def test_the_study_finds_the_noise_a_factor_model_implies():
    closes, core, sectors = _market()
    res = run_study(closes, core, sectors, seeds=(0, 1))
    s = res.summary()
    assert res.n_issues > 50 and res.mean_universe == 120
    # Hedging the beta difference removes market noise the sector controls leave in.
    assert s["sel_sector_beta"]["sd_daily"] < s["sel_sector"]["sd_daily"]
    # Precision weighting cannot be noisier than equal weighting by much, and usually helps.
    assert s["sel_sector_ivw"]["sd_daily"] < s["sel_sector"]["sd_daily"] * 1.02
    # Covariance-aware ranking weights are less noisy at the same exposure.
    assert s["rank_ef"]["times_faster"] > 1.0
    # A smaller universe is a noisier ranking test.
    assert s["rank_n100"]["sd_daily"] >= s["rank"]["sd_daily"] * 0.98
