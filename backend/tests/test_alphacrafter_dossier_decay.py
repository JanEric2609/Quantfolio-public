"""Tests for AlphaCrafter ic_decay.py and dossier.py (Phase 4)."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta

from conftest import _memory_db
from sqlalchemy import select

from app.foundation.models.entities import AlphaSignal, FactorsLibrary, RecommendationDossier
from app.lab.alphacrafter.dossier import build_dossier, compute_conviction, compute_verdict
from app.lab.alphacrafter.ic_decay import (
    DecayAnalysis,
    _decay_rate,
    analyze_decay,
    auto_retire_factors,
)

# ---------------------------------------------------------------------------
# DB helper (SQLite in-memory, same pattern as test_alphacrafter_miner.py)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_factor(db, *, name="test_factor", ic_series=None, retired_at=None):
    """Create and persist a FactorsLibrary row, return it."""
    ic_summary = {"ic_series": ic_series or [], "decay_halflife_days": None}
    factor = FactorsLibrary(
        name=name,
        formula_json=json.dumps({"dsl": "close"}),
        source="test",
        ic_summary_json=json.dumps(ic_summary),
        retired_at=retired_at,
    )
    db.add(factor)
    db.commit()
    db.refresh(factor)
    return factor


def _add_alpha_signals(db, factor_id, ic_values):
    """Seed AlphaSignal rows with ic_window_value for a factor."""
    base_ts = datetime(2025, 1, 1, tzinfo=UTC)
    for i, ic_val in enumerate(ic_values):
        db.add(
            AlphaSignal(
                symbol="TSLA",
                factor_id=factor_id,
                ts=base_ts + timedelta(days=i),
                value=0.0,
                ic_window_value=float(ic_val),
            )
        )
    db.commit()


# ===========================================================================
# ic_decay tests
# ===========================================================================


class TestAnalyzeDecayMissingFactor:
    """analyze_decay for a non-existent factor id returns safe defaults."""

    def test_non_existent_factor_returns_safe_defaults(self):
        db = _memory_db()
        result = asyncio.run(analyze_decay(db, "00000000-0000-0000-0000-000000000000"))
        assert isinstance(result, DecayAnalysis)
        assert result.recommend_retire is False
        assert math.isinf(result.half_life_days)
        assert result.n_obs == 0


class TestLibraryFallback:
    """IC series from FactorsLibrary.ic_summary_json is used when no AlphaSignals exist."""

    def test_library_decayed_series_recommends_retire(self):
        db = _memory_db()
        # Last 3 values (tau=3) all below min_ic=0.02 → should recommend retire.
        ic_series = [0.15, 0.12, 0.10, 0.005, 0.003, 0.001]
        factor = _make_factor(db, name="decayed_lib", ic_series=ic_series)
        result = asyncio.run(analyze_decay(db, factor.id, min_ic=0.02, tau=3))
        assert result.recommend_retire is True
        assert result.n_obs == len(ic_series)

    def test_library_healthy_series_does_not_recommend_retire(self):
        db = _memory_db()
        # Recent values all well above min_ic.
        ic_series = [0.08, 0.10, 0.12, 0.09, 0.11, 0.13]
        factor = _make_factor(db, name="healthy_lib", ic_series=ic_series)
        result = asyncio.run(analyze_decay(db, factor.id, min_ic=0.02, tau=3))
        assert result.recommend_retire is False
        assert result.n_obs == len(ic_series)

    def test_library_series_shorter_than_tau_does_not_retire(self):
        db = _memory_db()
        # Only 2 observations, tau=3 → can't satisfy the "≥ tau" requirement.
        ic_series = [0.001, 0.001]
        factor = _make_factor(db, name="short_series", ic_series=ic_series)
        result = asyncio.run(analyze_decay(db, factor.id, min_ic=0.02, tau=3))
        assert result.recommend_retire is False


class TestAlphaSignalPrecedence:
    """AlphaSignal.ic_window_value beats the library ic_series."""

    def test_dead_alpha_signals_override_healthy_library(self):
        db = _memory_db()
        # Library ic_series is healthy — factor should NOT be retired based on library.
        healthy_library = [0.15, 0.18, 0.20, 0.16, 0.19, 0.21]
        factor = _make_factor(db, name="alive_library_dead_signals", ic_series=healthy_library)

        # AlphaSignal rows are all ~0 (dead signal for tau=3 consecutive periods).
        dead_signals = [0.0, 0.0, 0.0]
        _add_alpha_signals(db, factor.id, dead_signals)

        result = asyncio.run(analyze_decay(db, factor.id, min_ic=0.02, tau=3))
        # AlphaSignal data must have been preferred; dead signals dominate.
        assert result.recommend_retire is True, (
            "Expected AlphaSignal rows to override healthy library series "
            f"but got recommend_retire=False (n_obs={result.n_obs}, ic_current={result.ic_current})"
        )
        assert result.n_obs == len(dead_signals)


class TestAutoRetireFactors:
    """auto_retire_factors retires only decayed active factors."""

    def test_retires_decayed_factor_and_leaves_healthy_alone(self):
        db = _memory_db()

        # Decayed: last 3 IC values well below threshold.
        decayed = _make_factor(
            db, name="decayed_active", ic_series=[0.15, 0.12, 0.005, 0.003, 0.001]
        )
        # Healthy: recent values clearly above threshold.
        healthy = _make_factor(
            db, name="healthy_active", ic_series=[0.08, 0.09, 0.11, 0.12, 0.10, 0.13]
        )

        retired = asyncio.run(auto_retire_factors(db, min_ic_threshold=0.02, tau=3))

        assert decayed.id in retired, f"Decayed factor {decayed.id} should be in retired ids"
        assert healthy.id not in retired, "Healthy factor should NOT have been retired"

        # DB state: decayed factor has retired_at set.
        db.expire_all()
        decayed_row = db.get(FactorsLibrary, decayed.id)
        healthy_row = db.get(FactorsLibrary, healthy.id)

        assert decayed_row.retired_at is not None, "retired_at must be set for decayed factor"
        assert healthy_row.retired_at is None, "retired_at must remain None for healthy factor"

    def test_already_retired_factor_is_skipped(self):
        db = _memory_db()
        # Already retired — auto_retire_factors only considers active factors.
        already_retired = _make_factor(
            db,
            name="already_retired",
            ic_series=[0.001, 0.001, 0.001],
            retired_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        retired = asyncio.run(auto_retire_factors(db, min_ic_threshold=0.02, tau=3))
        assert already_retired.id not in retired


class TestDecayRate:
    """_decay_rate helper returns correct fractional drop."""

    def test_monotone_decaying_series_returns_positive(self):
        # |IC| steadily decreases from first half to second half.
        series = [0.20, 0.18, 0.10, 0.05]
        rate = _decay_rate(series)
        assert rate > 0, f"Expected positive decay rate, got {rate}"

    def test_flat_series_returns_near_zero(self):
        series = [0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
        rate = _decay_rate(series)
        assert abs(rate) < 1e-9, f"Expected ~0 decay rate for flat series, got {rate}"

    def test_three_point_series_returns_zero(self):
        # < 4 observations → always 0.
        series = [0.20, 0.10, 0.05]
        rate = _decay_rate(series)
        assert rate == 0.0, f"Expected 0.0 for 3-point series, got {rate}"

    def test_improving_series_returns_negative(self):
        # |IC| increases from first to second half → negative (not decaying).
        series = [0.05, 0.06, 0.15, 0.18]
        rate = _decay_rate(series)
        assert rate < 0, f"Expected negative rate for improving series, got {rate}"

    def test_empty_series_returns_zero(self):
        assert _decay_rate([]) == 0.0

    def test_zero_early_mean_returns_zero(self):
        # All-zero first half → early == 0 → avoids division.
        series = [0.0, 0.0, 0.10, 0.10]
        rate = _decay_rate(series)
        assert rate == 0.0


# ===========================================================================
# dossier tests
# ===========================================================================


class TestComputeConviction:
    """compute_conviction formula and boundary conditions."""

    def test_monotonic_in_sharpe(self):
        sharpes = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]
        convictions = [compute_conviction(sharpe=s, mean_ic=0.05) for s in sharpes]
        for a, b in zip(convictions, convictions[1:]):
            assert b >= a, f"Conviction should not decrease as Sharpe increases: {convictions}"

    def test_monotonic_in_mean_ic(self):
        ics = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15]
        convictions = [compute_conviction(sharpe=1.0, mean_ic=ic) for ic in ics]
        for a, b in zip(convictions, convictions[1:]):
            assert b >= a, f"Conviction should not decrease as |mean_ic| increases: {convictions}"

    def test_crisis_strictly_lowers_conviction(self):
        base = compute_conviction(sharpe=2.0, mean_ic=0.08)
        crisis = compute_conviction(sharpe=2.0, mean_ic=0.08, crisis=True)
        assert crisis < base, f"Crisis should lower conviction: base={base}, crisis={crisis}"

    def test_always_within_bounds(self):
        # Cases that do NOT saturate the clamp — all well below the 0.95 cap.
        safe_cases = [
            dict(sharpe=0.0, mean_ic=0.0),
            dict(sharpe=-5.0, mean_ic=-0.5),
            dict(sharpe=1.5, mean_ic=0.05, crisis=True),
            dict(sharpe=0.0, mean_ic=0.0, crisis=True, regime_score=0.0),
        ]
        for kwargs in safe_cases:
            c = compute_conviction(**kwargs)
            assert 0.0 <= c <= 0.95, f"Conviction out of [0, 0.95]: {c} for kwargs={kwargs}"

    def test_always_at_least_zero(self):
        # Regardless of inputs, result is never negative.
        for kwargs in [
            dict(sharpe=-100.0, mean_ic=-1.0),
            dict(sharpe=-100.0, mean_ic=-1.0, crisis=True, regime_score=-5.0),
        ]:
            c = compute_conviction(**kwargs)
            assert c >= 0.0, f"Conviction went negative: {c}"

    # --- Conviction is capped at 0.95 even for saturated inputs (the spec cap). ---

    def test_max_inputs_capped_at_0_95(self):
        """Saturated inputs are clamped to the 0.95 conviction cap, never 1.0."""
        c = compute_conviction(sharpe=10.0, mean_ic=1.0)
        assert c == 0.95, f"Expected 0.95 cap, got {c}"

    def test_regime_score_changes_blend(self):
        without = compute_conviction(sharpe=1.0, mean_ic=0.05)
        with_regime = compute_conviction(sharpe=1.0, mean_ic=0.05, regime_score=0.10)
        # Different blend → different value (they won't be equal in general).
        # High regime score pushes conviction up vs. no regime score (for these params,
        # regime score 0.10/0.15 ≈ 0.667, which exceeds the 0.4*IC contribution).
        assert without != with_regime, (
            "Passing regime_score should change conviction (different blend weights)"
        )

    def test_full_inputs_with_regime_capped_at_0_95(self):
        """Sharpe=3, IC=0.10, regime=0.15 all at normalised max → clamped to 0.95."""
        c = compute_conviction(sharpe=3.0, mean_ic=0.10, regime_score=0.15)
        assert c == 0.95, f"Expected 0.95 cap, got {c}"

    def test_zero_inputs_give_zero(self):
        c = compute_conviction(sharpe=0.0, mean_ic=0.0)
        assert c == 0.0

    def test_crisis_formula_exact(self):
        # Without crisis: base = 0.6*(2/3) + 0.4*(0.05/0.10) = 0.4 + 0.2 = 0.6
        # With crisis: 0.6 * 0.7 = 0.42
        base = compute_conviction(sharpe=2.0, mean_ic=0.05)
        crisis = compute_conviction(sharpe=2.0, mean_ic=0.05, crisis=True)
        assert abs(base - 0.6) < 1e-6, f"Expected base=0.6, got {base}"
        assert abs(crisis - 0.42) < 1e-6, f"Expected crisis=0.42, got {crisis}"


class TestBuildDossier:
    """build_dossier persists correctly and derives conviction from config."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_persists_exactly_one_row(self):
        db = _memory_db()
        self._run(
            build_dossier(
                db,
                symbol="AAPL",
                miner_factors=["momentum_12_1", "value_bp"],
                screener_regime="bull",
                trader_config={"sharpe_ratio": 1.5, "max_drawdown": 0.15, "rebalance_freq": "W"},
                mean_ic=0.07,
            )
        )
        rows = db.execute(select(RecommendationDossier)).scalars().all()
        assert len(rows) == 1

    def test_dossier_json_has_required_keys(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="MSFT",
                miner_factors=["value_ep"],
                screener_regime="neutral",
                trader_config={"sharpe_ratio": 1.0, "max_drawdown": 0.10},
                mean_ic=0.05,
            )
        )
        content = json.loads(dossier.dossier_json)
        for key in ("conviction", "size", "rationale", "risk_notes", "miner_factors"):
            assert key in content, f"Missing key '{key}' in dossier_json"

    def test_conviction_matches_compute_conviction(self):
        db = _memory_db()
        sharpe = 1.8
        mean_ic = 0.06
        dossier = self._run(
            build_dossier(
                db,
                symbol="GOOGL",
                miner_factors=["quality_roe"],
                screener_regime="bull",
                trader_config={"sharpe_ratio": sharpe, "max_drawdown": 0.12},
                mean_ic=mean_ic,
            )
        )
        expected_conviction = compute_conviction(sharpe=sharpe, mean_ic=mean_ic)
        assert abs(dossier.conviction - expected_conviction) < 1e-6

    def test_size_scales_with_conviction(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="TSLA",
                miner_factors=["momentum_12_1"],
                screener_regime=None,
                trader_config={"sharpe_ratio": 1.5, "max_drawdown": 0.20},
                mean_ic=0.08,
            )
        )
        content = json.loads(dossier.dossier_json)
        expected_size_pct = round(dossier.conviction * 10.0, 1)
        assert content["size"] == f"{expected_size_pct:.1f}%", (
            f"Expected size '{expected_size_pct:.1f}%', got '{content['size']}'"
        )

    def test_rationale_contains_factor_names_and_regime(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="NVDA",
                miner_factors=["momentum_12_1", "quality_roe"],
                screener_regime="bull_expansion",
                trader_config={"sharpe_ratio": 2.0, "max_drawdown": 0.10},
                mean_ic=0.09,
            )
        )
        content = json.loads(dossier.dossier_json)
        rationale = content["rationale"]
        assert "momentum_12_1" in rationale, f"Factor name missing from rationale: {rationale}"
        assert "quality_roe" in rationale, f"Factor name missing from rationale: {rationale}"
        assert "bull_expansion" in rationale, f"Regime missing from rationale: {rationale}"

    def test_risk_notes_contains_sharpe(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="AMZN",
                miner_factors=["value_bp"],
                screener_regime="neutral",
                trader_config={"sharpe_ratio": 1.23, "max_drawdown": 0.18},
                mean_ic=0.04,
            )
        )
        content = json.loads(dossier.dossier_json)
        risk_notes = content["risk_notes"]
        assert "1.23" in risk_notes, f"Sharpe not in risk_notes: {risk_notes}"

    def test_crisis_risk_notes_contain_crisis_note(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="META",
                miner_factors=["momentum_12_1"],
                screener_regime="bear",
                trader_config={"sharpe_ratio": 0.8, "max_drawdown": 0.30},
                mean_ic=0.03,
                crisis=True,
            )
        )
        content = json.loads(dossier.dossier_json)
        risk_notes = content["risk_notes"]
        assert "crisis" in risk_notes.lower(), (
            f"Expected crisis note in risk_notes, got: {risk_notes}"
        )

    def test_explicit_conviction_and_size_pct_used_verbatim(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="SPY",
                miner_factors=["momentum_12_1"],
                screener_regime=None,
                trader_config={"sharpe_ratio": 2.5, "max_drawdown": 0.08},
                mean_ic=0.09,
                conviction=0.42,
                size_pct=7.5,
            )
        )
        assert abs(dossier.conviction - 0.42) < 1e-9, (
            f"Expected conviction=0.42, got {dossier.conviction}"
        )
        content = json.loads(dossier.dossier_json)
        assert content["conviction"] == 0.42
        assert content["size"] == "7.5%", f"Expected '7.5%', got '{content['size']}'"

    def test_legacy_sharpe_key_works(self):
        """trader_config with 'sharpe' (legacy) instead of 'sharpe_ratio' should work."""
        db = _memory_db()
        sharpe = 1.5
        dossier = self._run(
            build_dossier(
                db,
                symbol="IWM",
                miner_factors=["value_bp"],
                screener_regime=None,
                trader_config={"sharpe": sharpe, "max_dd": 0.15},
                mean_ic=0.06,
            )
        )
        expected = compute_conviction(sharpe=sharpe, mean_ic=0.06)
        assert abs(dossier.conviction - expected) < 1e-6

    def test_no_screener_regime_omits_regime_from_rationale(self):
        db = _memory_db()
        dossier = self._run(
            build_dossier(
                db,
                symbol="QQQ",
                miner_factors=["momentum_12_1"],
                screener_regime=None,
                trader_config={"sharpe_ratio": 1.0, "max_drawdown": 0.12},
                mean_ic=0.05,
            )
        )
        content = json.loads(dossier.dossier_json)
        # No "Regime:" suffix should appear.
        assert "Regime:" not in content["rationale"]


class TestComputeVerdict:
    """compute_verdict maps conviction thresholds to buy/hold/review."""

    def test_buy_at_and_above_0_6(self):
        assert compute_verdict(0.6) == "buy"
        assert compute_verdict(0.75) == "buy"
        assert compute_verdict(0.95) == "buy"

    def test_hold_between_0_3_and_0_6(self):
        assert compute_verdict(0.3) == "hold"
        assert compute_verdict(0.45) == "hold"
        assert compute_verdict(0.59) == "hold"

    def test_review_below_0_3(self):
        assert compute_verdict(0.0) == "review"
        assert compute_verdict(0.29) == "review"
        assert compute_verdict(-0.1) == "review"

    def test_verdict_in_built_dossier_json(self):
        """Dossiers produced by build_dossier contain a valid verdict key."""
        db = _memory_db()
        dossier = asyncio.run(
            build_dossier(
                db,
                symbol="TEST",
                miner_factors=["mom"],
                screener_regime="bull",
                trader_config={"sharpe_ratio": 2.0, "max_drawdown": 0.10},
                mean_ic=0.08,
                regime_score=0.12,
            )
        )
        content = json.loads(dossier.dossier_json)
        assert "verdict" in content
        assert content["verdict"] in ("buy", "hold", "review")
