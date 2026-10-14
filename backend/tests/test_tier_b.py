"""Tier B harness on synthetic data (ADR 0020, plan 2026-10-12 WP12). Never touches the network."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import _memory_db

from app.foundation.models.entities import TrialLedgerEntry
from app.foundation.quant_metrics import effective_n_trials
from app.lab import tier_b


def _months(n, start="1990-01"):
    return pd.period_range(start, periods=n, freq="M")


def test_signal_uses_last_month_only():
    idx = _months(14)
    eq = pd.Series([0.01] * 12 + [-0.30, 0.05], index=idx)
    res = tier_b.trend_overlay(eq, pd.Series(0.002, index=idx), window=10, switch_cost=0.0, gain_tax_rate=0.0)
    # The crash month is still held: the signal only reacts the month after.
    assert res.loc[idx[12], "in_equity"] == 1.0
    assert res.loc[idx[12], "gross"] == pytest.approx(-0.30)
    assert res.loc[idx[13], "in_equity"] == 0.0
    assert res.loc[idx[13], "gross"] == pytest.approx(0.002)


def test_exit_pays_switch_cost_and_tax_on_the_gain():
    idx = _months(14)
    eq = pd.Series([0.01] * 12 + [-0.30, 0.05], index=idx)
    cash = pd.Series(0.0, index=idx)
    no_tax = tier_b.trend_overlay(eq, cash, window=10, switch_cost=0.001, gain_tax_rate=0.0)
    taxed = tier_b.trend_overlay(eq, cash, window=10, switch_cost=0.001, gain_tax_rate=tier_b.GAIN_TAX_RATE)
    exit_month = idx[13]
    assert no_tax.loc[exit_month, "net"] == pytest.approx(-0.001)
    # The leg fell below its entry value, so no gain and no tax.
    assert taxed.loc[exit_month, "net"] == pytest.approx(-0.001)


def test_pass_bar_requires_both_oos_regions():
    rng = np.random.default_rng(1)
    strong = pd.Series(0.01 + rng.normal(0, 0.01, 400), index=_months(400))
    weak = pd.Series(rng.normal(0, 0.03, 400), index=_months(400))
    ok = tier_b.evaluate("B3_size", strong, {"us": strong, "europe": strong}, publication=None)
    assert ok.passed
    fail = tier_b.evaluate("B3_size", strong, {"us": strong, "europe": weak}, publication=None)
    assert not fail.passed and any("europe" in r for r in fail.reasons)
    assert not tier_b.evaluate("B3_size", strong, {"us": strong}).passed


def test_b1_needs_a_drawdown_cut_and_a_small_cagr_shortfall():
    s = pd.Series(0.01, index=_months(200)) + pd.Series(np.random.default_rng(2).normal(0, 0.005, 200),
                                                        index=_months(200))
    v = tier_b.evaluate("B1_trend_10m", s, {"us": s, "europe": s}, publication=None,
                        b1={"mdd": -0.40, "mdd_bh": -0.50, "cagr": 0.07, "cagr_bh": 0.075})
    assert not v.passed and any("drawdown cut" in r for r in v.reasons)


def test_trials_are_registered_once_and_b1_variants_share_a_family():
    db = _memory_db()
    tier_b.register_trials(db)
    tier_b.register_trials(db)
    assert db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "tier_b").count() == 6
    assert effective_n_trials(db) == 5


def test_rescaling_a_whole_series_leaves_t_unchanged_so_decay_hits_pre_publication_months_only():
    rng = np.random.default_rng(3)
    s = pd.Series(0.004 + rng.normal(0, 0.02, 360), index=_months(360))  # 1990-2019
    assert tier_b.nw_t(s * 0.42) == pytest.approx(tier_b.nw_t(s))
    decayed = tier_b.decay_before_publication(s, 2007)
    assert decayed[: "2006-12"].to_numpy() == pytest.approx(s[: "2006-12"].to_numpy() * (1 - tier_b.PUBLICATION_DECAY))
    assert decayed["2007-01":].to_numpy() == pytest.approx(s["2007-01":].to_numpy())
    assert tier_b.nw_t(decayed) < tier_b.nw_t(s)
    v_decayed = tier_b.evaluate("B1_trend_10m", s, {"us": s, "europe": s}, publication=2007)
    v_plain = tier_b.evaluate("B1_trend_10m", s, {"us": s, "europe": s}, publication=None)
    assert v_decayed.stats["t_us"] < v_plain.stats["t_us"]


def test_publication_year_per_family():
    assert tier_b.publication_year("B1_trend_200d") == 2007
    assert tier_b.publication_year("B3_size") == 1981
    assert set(tier_b.PUBLICATION_YEAR) == {t.split("_")[0] for t in tier_b.REGISTERED_TRIALS}


def test_tilt_pays_implementation_cost_then_tax():
    idx = _months(12)
    gross = pd.Series(0.01, index=idx)
    nv = tier_b.tilt_net(gross, taxed=False)
    taxed = tier_b.tilt_net(gross, taxed=True)
    assert nv.iloc[0] == pytest.approx(0.01 - 0.004 / 12)
    assert taxed.iloc[0] == pytest.approx((0.01 - 0.004 / 12) * (1 - 0.26375 * 0.7))
