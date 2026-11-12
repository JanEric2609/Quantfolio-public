"""First tests for modules that had none: the Monte Carlo engine and the Discover config review (WP12.10)."""
from __future__ import annotations

import math
import warnings

import numpy as np

from app.decision.discover import config_review as cr
from app.foundation.quant_mc.engine import McSpec, MonteCarloEngine


def _bs_call(s0: float, k: float, r: float, sigma: float, t: float) -> float:
    d1 = (math.log(s0 / k) + (r + sigma**2 / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    return s0 * cdf(d1) - k * math.exp(-r * t) * cdf(d2)


def _spec(**kw) -> McSpec:
    base = dict(
        model="gbm_euler", model_params={"S0": 100.0, "mu": 0.05, "r": 0.05, "sigma": 0.2},
        horizon_steps=50, T=1.0, n_paths=40000, payoff="european_call",
        payoff_params={"strike": 100.0}, seed=7, compute_greeks=False,
    )
    base.update(kw)
    return McSpec(**base)


def test_mc_european_call_matches_black_scholes_forward_value():
    """The engine's ``estimate`` is the mean payoff at maturity, NOT discounted (only its Greeks discount).

    Pinned as found: Black-Scholes price x exp(rT) is the expected payoff. A caller that wants
    a price must discount the estimate itself; changing the engine is a separate decision.
    """
    result, _ = MonteCarloEngine().run(_spec())
    forward_value = _bs_call(100, 100, 0.05, 0.2, 1.0) * math.exp(0.05)
    assert result.stderr > 0
    assert abs(result.estimate - forward_value) < 4 * result.stderr + 0.1  # Euler bias is small at 50 steps


def test_mc_same_seed_same_answer_different_seed_differs():
    a, _ = MonteCarloEngine().run(_spec(n_paths=2000))
    b, _ = MonteCarloEngine().run(_spec(n_paths=2000))
    c, _ = MonteCarloEngine().run(_spec(n_paths=2000, seed=8))
    assert a.estimate == b.estimate
    assert a.estimate != c.estimate


def test_mc_deeper_in_the_money_call_is_worth_more():
    low, _ = MonteCarloEngine().run(_spec(payoff_params={"strike": 120.0}))
    high, _ = MonteCarloEngine().run(_spec(payoff_params={"strike": 80.0}))
    assert high.estimate > low.estimate > 0


def test_mc_spec_round_trips_to_json():
    assert '"n_paths": 40000' in _spec().to_json()


def test_review_helpers_on_known_values():
    assert cr._num(None, 3.0) == 3.0 and cr._num("x", 3.0) == 3.0 and cr._num("2.5", 0) == 2.5
    assert cr._rank_ic([1.0], [1.0]) is None
    assert cr._rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert cr._rank_ic([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert cr._rank_ic([1, 1, 1], [1, 2, 3]) is None  # constant input: no correlation, not NaN
    assert cr._brier([], []) is None
    assert cr._brier([1.0, 0.0], [1, 0]) == 0.0
    assert cr._brier([0.5, 0.5], [1, 0]) == 0.25


def test_review_hit_rate_and_calibration():
    conv = [0.1, 0.2, 0.3, 0.9]
    assert cr._top_quartile_hit_rate(conv, [-1, -1, -1, 2]) == 1.0
    assert cr._top_quartile_hit_rate([], []) == 0.0
    assert cr._expected_calibration_error([], []) == 0.0
    # confidence 0.9 every time, right 9 times in 10 → perfectly calibrated
    assert cr._expected_calibration_error([0.9] * 10, [1] * 9 + [-1]) < 1e-9
    # confident and always wrong → error close to the confidence
    assert abs(cr._expected_calibration_error([0.95] * 10, [-1] * 10) - 0.95) < 1e-9


def test_review_sharpe_is_a_t_statistic_and_zero_for_tiny_samples():
    assert cr._sharpe_ratio([0.1]) == 0.0
    r = [0.02, 0.01, 0.03, -0.01, 0.02, 0.01]
    expected = np.mean(r) / np.std(r, ddof=1) * math.sqrt(len(r))
    assert abs(cr._sharpe_ratio(r) - expected) < 1e-9


# --- llm_research: the parsers that read model output must survive junk -------------------------
from decimal import Decimal  # noqa: E402

from app.foundation import broker_sync  # noqa: E402
from app.foundation import llm_research as lr  # noqa: E402


def test_factor_proposals_skip_junk_duplicates_and_clamp_confidence():
    text = 'Here you go: [{"name": "Quality Minus Junk", "confidence": 7}, {"name": "known"}, "x", {"name": ""}]'
    out = lr._parse_factor_proposals(text, existing={"known"})
    assert [p.name for p in out] == ["quality_minus_junk"]
    assert out[0].confidence == 1.0
    assert lr._parse_factor_proposals("no json here", set()) == []
    assert lr._parse_factor_proposals("[not json]", set()) == []
    assert lr._parse_factor_proposals('{"a": 1}', set()) == []


def test_market_analysis_falls_back_to_the_context_when_the_answer_is_not_json():
    ctx = lr.ResearchContext(regime_label="bear")
    broken = lr._parse_market_analysis("the market is nervous " * 50, ctx)
    assert broken.regime_assessment == "bear" and len(broken.summary) == 500
    good = lr._parse_market_analysis('{"summary": "calm", "confidence": "x"}', ctx)
    assert good.summary == "calm" and good.confidence == 0.5


def test_regime_transition_defaults_to_low_plausibility_on_junk():
    bad = lr._parse_regime_transition("???", "bull", "bear")
    assert bad.plausibility == "low" and (bad.from_regime, bad.to_regime) == ("bull", "bear")
    ok = lr._parse_regime_transition('{"plausibility": "high", "implications": "x"}', "bull", "bear")
    assert ok.plausibility == "high"


def test_broker_sync_value_coercion_never_raises():
    assert broker_sync._decimal_or_none("12.50") == Decimal("12.50")
    assert broker_sync._decimal_or_none(None) is None and broker_sync._decimal_or_none("") is None
    assert broker_sync._decimal_or_none("NaN") is None or broker_sync._decimal_or_none("NaN").is_nan()
