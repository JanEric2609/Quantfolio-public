"""Tests for the composite scoring module (advisor-loop PR1 semantics).

The deterministic quant signals (momentum, risk, benchmark, portfolio) are
load-bearing; the AlphaCrafter ic_icir signal (the candidate's exposure to
validated library factors) is advisory and drops out of the weighting until
such factors exist and the exposure was measured in a real cross-section.
"""
import math

import pytest

from app.decision.discover.composite import (
    MIN_IC_PANEL_SIZE,
    THRESHOLD_IC_OBS,
    WEIGHTS,
    compute_weighted_composite,
    derive_signals_from_scores,
)


class TestWeights:
    def test_weights_sum_to_the_old_total_less_the_retired_regime_weight(self):
        """The regime signal's 0.05 was dropped, not handed on: renormalising
        keeps every other signal's relative weight unchanged."""
        assert sum(WEIGHTS.values()) == pytest.approx(0.95)
        assert "regime" not in WEIGHTS

    def test_deterministic_signals_carry_majority_weight(self):
        deterministic = (
            WEIGHTS["momentum"] + WEIGHTS["risk"] + WEIGHTS["benchmark"]
            + WEIGHTS["portfolio"] + WEIGHTS["fundamentals"]
        )
        assert deterministic > 0.5
        assert WEIGHTS["ic_icir"] < 0.2


class TestComputeWeightedComposite:
    def test_ic_signal_participates_with_a_validated_exposure(self):
        """An exposure_score from at least one validated factor over a real
        cross-section contributes ic_icir's (normalised) weight."""
        signals = {
            "ic_icir": 1.0,
            "analyst": 0.0,
            "sentiment": 0.0,
            "portfolio": 0.0,
            "fundamentals": 0.0,
        }
        ic_data = {"exposure_score": 0.8, "n_factors": 1, "panel_size": MIN_IC_PANEL_SIZE}
        result = compute_weighted_composite(signals, ic_data)
        present_total = sum(WEIGHTS[k] for k in signals)
        assert result == pytest.approx(WEIGHTS["ic_icir"] / present_total)

    def test_advisory_signals_dropped_without_validated_factors(self):
        """No library factor (n_factors 0, exposure None): a perfect score
        cannot lift the composite when there is no evidence behind it."""
        signals = {"ic_icir": 1.0, "momentum": 0.4, "risk": 0.6}
        empty = compute_weighted_composite(
            signals, {"exposure_score": None, "n_factors": 0, "panel_size": 150}
        )
        expected = (0.4 * WEIGHTS["momentum"] + 0.6 * WEIGHTS["risk"]) / (
            WEIGHTS["momentum"] + WEIGHTS["risk"]
        )
        assert empty == pytest.approx(expected)

    def test_advisory_only_signals_score_zero_without_exposure(self):
        assert compute_weighted_composite({"ic_icir": 1.0}, {"n_factors": 0, "panel_size": 150}) == 0.0

    def test_exposure_from_a_small_cross_section_is_not_evidence(self):
        """A z-score within fewer than MIN_IC_PANEL_SIZE names is noise."""
        signals = {"ic_icir": 1.0, "momentum": 0.4}
        small = {"exposure_score": 0.9, "n_factors": 2, "panel_size": MIN_IC_PANEL_SIZE - 1}
        assert compute_weighted_composite(signals, small) == pytest.approx(0.4)

    def test_breakdowns_from_the_retired_panel_ic_never_qualify(self):
        """Before 2026-09-28 the stage measured IC across the candidate and
        three benchmark ETFs and stored n_obs/panel_size, no exposure_score.
        However many observations, those stay out of the weighting."""
        signals = {"ic_icir": 1.0, "momentum": 0.4}
        for legacy in ({"n_obs": 500, "panel_size": 4}, {"n_obs": 500}, {"n_obs": THRESHOLD_IC_OBS, "panel_size": 50}):
            assert compute_weighted_composite(signals, legacy) == pytest.approx(0.4)

    def test_exposure_score_is_the_ic_signal(self):
        """derive_signals_from_scores takes exposure_score as ic_icir as-is;
        without it, the retired IC/ICIR formula (for replays)."""
        scores = {"alpha_miner": {"exposure_score": 0.73, "ic": 0.5, "icir": 5.0}}
        assert derive_signals_from_scores(scores)["ic_icir"] == pytest.approx(0.73)
        legacy = {"alpha_miner": {"ic": 0.02, "icir": 0.4}}
        assert derive_signals_from_scores(legacy)["ic_icir"] == pytest.approx(0.5 + 0.06 + 0.1)

    def test_a_stored_regime_weight_and_signal_are_ignored(self):
        """Prod's DiscoveryConfig row still weights "regime"; the signal was
        the same ~1.0 for every candidate, so it must not count."""
        with_regime = compute_weighted_composite(
            {"regime": 1.0, "momentum": 0.4, "risk": 0.6}, weights={"regime": 0.05}
        )
        without = compute_weighted_composite({"momentum": 0.4, "risk": 0.6})
        assert with_regime == pytest.approx(without)

    def test_none_ic_data_drops_advisory_signals(self):
        signals = {"ic_icir": 1.0, "momentum": 0.8}
        result = compute_weighted_composite(signals, ic_data=None)
        assert result == pytest.approx(0.8)

    def test_weighted_composite_empty_signals(self):
        assert compute_weighted_composite({}) == 0.0

    def test_quant_moves_the_ranking(self):
        """Two candidates identical except momentum/risk: better quant profile
        must rank higher (acceptance check for B2)."""
        base = {"analyst": 0.5, "sentiment": 0.5, "portfolio": 0.5, "fundamentals": 0.5}
        strong = compute_weighted_composite({**base, "momentum": 0.9, "risk": 0.8, "benchmark": 0.7})
        weak = compute_weighted_composite({**base, "momentum": 0.3, "risk": 0.3, "benchmark": 0.4})
        assert strong > weak

    def test_stored_config_missing_new_keys_does_not_zero_them(self):
        """Configs persisted before momentum/risk/benchmark existed are merged
        over defaults, so the new deterministic signals still carry weight."""
        old_stored = {
            "ic_icir": 0.50,
            "regime": 0.15,
            "analyst": 0.10,
            "sentiment": 0.10,
            "portfolio": 0.10,
            "fundamentals": 0.05,
        }
        signals = {"momentum": 1.0, "risk": 1.0, "benchmark": 1.0, "portfolio": 0.0,
                   "analyst": 0.0, "sentiment": 0.0, "fundamentals": 0.0}
        result = compute_weighted_composite(signals, ic_data=None, weights=old_stored)
        assert result > 0.4  # deterministic trio dominates despite old config


class TestDeriveSignalsFromScores:
    """Regression coverage for the XEON.DE prod incident (2026-08-20): a
    money-market ETF's near-zero volatility produced a degenerate Sharpe
    delta that saturated benchmark_score to 1.0 despite the candidate's own
    "trails benchmark by 15.6%/yr" concern, and flat neutral-0.5 defaults for
    fundamentals/sentiment/analyst (structurally inapplicable — no P/E, no
    analyst coverage) still counted fully in the composite.
    """

    # Real scores_json captured from prod discover_candidates for XEON.DE.
    XEON_SCORES = {
        "alpha_miner": {"ic": 0.0, "icir": 0.0, "concerns": ["alphacrafter_miner_insufficient_panel"]},
        "alpha_screener": {"regime_affinity": 1.0, "correlation_score": 0.5, "regime_label": "sideways"},
        "sentiment_fundamentals": {
            "sentiment_score": 0.5, "fundamentals_score": 0.5, "analyst_estimate_score": 0.5,
            "pe": None, "pb": None, "roe": None, "de_ratio": None,
            "market_cap": None, "revenue_growth": None, "social_mentions": 0,
        },
        "momentum_quality": {"momentum_12_1m": 0.0185, "volatility_6m": 0.0017, "max_drawdown": -0.0003},
        "backtest_vs_benchmark": {
            "excess_return_annual": -0.1558, "candidate_return_annual": 0.0301,
            "sharpe_delta": 11.1444, "candidate_sharpe": 12.29, "benchmark_sharpe": 1.1457,
        },
        "verification_gate": {"volatility": 0.0021, "max_drawdown": 0.0003, "sharpe": 9.6742},
        "quant_signals": {"instrument_type": "money_market", "var_95_daily": 0.00014, "cvar_95_daily": 0.00018},
        "portfolio_fit": {"correlation": 0.0878, "overlap": 0.0, "fit_score": 0.6737},
    }

    def test_benchmark_score_does_not_saturate_on_degenerate_sharpe_delta(self):
        signals = derive_signals_from_scores(self.XEON_SCORES)
        # Old formula (0.5 + excess + 0.25*sharpe_delta) saturated to 1.0
        # despite excess_return_annual=-15.6%/yr; must stay well below max
        # and reflect the negative excess return as the dominant signal.
        assert signals["benchmark"] < 0.6
        assert signals["benchmark"] == pytest.approx(0.4442, abs=1e-3)

    def test_fundamentals_analyst_sentiment_excluded_when_structurally_null(self):
        signals = derive_signals_from_scores(self.XEON_SCORES)
        assert "fundamentals" not in signals
        assert "analyst" not in signals
        assert "sentiment" not in signals
        # Deterministic + advisory signals that ARE meaningful stay present.
        assert "momentum" in signals
        assert "risk" in signals
        assert "portfolio" in signals

    def test_money_market_momentum_forced_neutral_not_raw(self):
        """F9 wiring: instrument_type=money_market must neutralise momentum
        to 0.5 regardless of the raw 12-1m reading, not just pass it
        through. A cash-equivalent's near-zero momentum reflects the policy
        rate drifting, not tradeable evidence."""
        scores = dict(self.XEON_SCORES)
        # A deliberately large raw momentum reading — must still be ignored.
        scores["momentum_quality"] = {**scores["momentum_quality"], "momentum_12_1m": 0.35}
        signals = derive_signals_from_scores(scores)
        assert signals["momentum"] == 0.5

    def test_non_money_market_momentum_uses_raw_reading(self):
        """Sanity check the neutralisation is instrument_type-gated, not global.
        Without a cross-sectional rank the raw reading goes through the tanh
        squash (ADR 0017)."""
        scores = dict(self.XEON_SCORES)
        scores["quant_signals"] = {**scores["quant_signals"], "instrument_type": "equity"}
        scores["momentum_quality"] = {**scores["momentum_quality"], "momentum_12_1m": 0.35}
        signals = derive_signals_from_scores(scores)
        assert signals["momentum"] == pytest.approx(0.5 + 0.5 * math.tanh(0.35 / 0.5))

    def test_momentum_rank_takes_precedence_over_raw_reading(self):
        scores = dict(self.XEON_SCORES)
        scores["quant_signals"] = {**scores["quant_signals"], "instrument_type": "equity"}
        scores["momentum_quality"] = {
            **scores["momentum_quality"], "momentum_12_1m": 0.61, "momentum_rank": 0.62,
        }
        assert derive_signals_from_scores(scores)["momentum"] == pytest.approx(0.62)

    def test_momentum_squash_no_longer_saturates_at_fifty_percent(self):
        """BBVA.MC audit: the old 0.5 + m pinned every name up >= 50% at 1.0."""
        base = dict(self.XEON_SCORES)
        base["quant_signals"] = {**base["quant_signals"], "instrument_type": "equity"}

        def momentum(m: float) -> float:
            scores = {**base, "momentum_quality": {**base["momentum_quality"], "momentum_12_1m": m}}
            return derive_signals_from_scores(scores)["momentum"]

        assert momentum(0.5) < momentum(0.6) < momentum(1.0) < 1.0

    def test_composite_meaningfully_lower_than_prod_incident_value(self):
        signals = derive_signals_from_scores(self.XEON_SCORES)
        composite = compute_weighted_composite(signals, ic_data={"n_obs": 0})
        # Prod incident composite was 0.7008 with the buggy formula.
        assert composite < 0.68

    def test_fundamentals_included_when_real_data_present(self):
        scores = dict(self.XEON_SCORES)
        scores["sentiment_fundamentals"] = {
            "sentiment_score": 0.6, "fundamentals_score": 0.7, "analyst_estimate_score": 0.6,
            "pe": 18.0, "pb": 2.1, "roe": 0.12, "de_ratio": 90.0,
            "market_cap": 3e9, "revenue_growth": 0.05, "social_mentions": 5,
        }
        signals = derive_signals_from_scores(scores)
        assert signals["fundamentals"] == pytest.approx(0.7)
        assert signals["analyst"] == pytest.approx(0.6)
        assert signals["sentiment"] == pytest.approx(0.6)

    def test_sharpe_nudge_bounded_for_normal_equity_delta(self):
        """A typical equity sharpe_delta (e.g. +0.5) still nudges the score
        sensibly — the fix bounds the pathological case, not normal signal."""
        scores = {
            "backtest_vs_benchmark": {"excess_return_annual": 0.05, "sharpe_delta": 0.5},
        }
        signals = derive_signals_from_scores(scores)
        assert signals["benchmark"] == pytest.approx(0.5 + 0.05 + 0.02 * 0.5)
