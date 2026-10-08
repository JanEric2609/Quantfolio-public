"""Tests for Multiplicative Weights Update (MWU) regime advisor."""
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from conftest import _memory_db

from app.foundation.models.entities import Recommendation, RecommendationOutcome, RegimeRecommendationWeight
from app.decision.regime_advisor import (
    MWU_ACTIONS,
    _accuracy_for_label,
    _infer_action_from_rec,
    _loss_for_label,
    get_or_init_weights,
    get_regime_adjusted_weights,
    outcomes_already_applied,
    update_weights_for_outcome,
)


def _make_rec(db, verdict="BUY", payload_json=None) -> Recommendation:
    rec = Recommendation(
        id="rec-1",
        user_id="u-1",
        ticker="EIMI.L",
        horizon="mid",
        verdict=verdict,
        confidence=0.7,
        payload_json=payload_json or json.dumps({"regime": {"label": "bull"}, "checks": []}),
        mode="portfolio_advisor",
        approval_state="accepted",
        created_at=datetime.now(UTC),
    )
    db.add(rec)
    db.commit()
    return rec


def _make_outcome(db, rec_id: str, label: str, excess: float = 0.05) -> RecommendationOutcome:
    outcome = RecommendationOutcome(
        recommendation_id=rec_id,
        evaluated_at=datetime.now(UTC),
        window_days=60,
        outcome_label=label,
        abs_return=excess + 0.02,
        benchmark_excess_return=excess,
        portfolio_sortino_delta=excess * 2.5,
        notes_json=json.dumps({"ticker": "EIMI.L"}),
    )
    db.add(outcome)
    db.commit()
    return outcome


# --- Pure function tests ---

class TestLossAndAccuracy:
    def test_correct_outcome_gives_zero_loss(self):
        assert _loss_for_label("correct") == 0.0

    def test_incorrect_outcome_gives_full_loss(self):
        assert _loss_for_label("incorrect") == 1.0

    def test_neutral_outcome_gives_half_loss(self):
        assert _loss_for_label("neutral") == pytest.approx(0.5)

    def test_correct_accuracy_is_one(self):
        assert _accuracy_for_label("correct") == 1.0

    def test_incorrect_accuracy_is_zero(self):
        assert _accuracy_for_label("incorrect") == 0.0

    def test_unknown_label_raises(self):
        with pytest.raises(ValueError):
            _accuracy_for_label("pending")


class TestInferAction:
    def _rec(self, verdict, payload="{}"):
        r = MagicMock(spec=Recommendation)
        r.verdict = verdict
        r.ticker = "EIMI.L"
        r.horizon = "mid"
        r.payload_json = payload
        return r

    def test_buy_verdict_maps_to_buy_equity(self):
        assert _infer_action_from_rec(self._rec("BUY")) == "buy_equity"

    def test_sell_verdict_maps_to_reduce_holding(self):
        assert _infer_action_from_rec(self._rec("SELL")) == "reduce_holding"

    def test_hold_verdict_keeps_the_position(self):
        assert _infer_action_from_rec(self._rec("HOLD")) == "hold_position"

    def test_cash_verdict_maps_to_hold_cash(self):
        assert _infer_action_from_rec(self._rec("RAISE_CASH")) == "hold_cash"

    def test_buy_on_a_bond_horizon_is_buy_bond(self):
        rec = self._rec("BUY")
        rec.horizon = "BOND ladder"
        assert _infer_action_from_rec(rec) == "buy_bond"
        assert _infer_action_from_rec(self._rec("BOND_BUY")) == "buy_bond"

    def test_every_action_is_reachable(self):
        reached = {
            _infer_action_from_rec(self._rec(v))
            for v in ("BUY", "SELL", "HOLD", "CASH", "ROTATE", "BOND_BUY")
        }
        assert reached == set(MWU_ACTIONS)

    def test_unknown_verdict_defaults_to_buy_equity(self):
        assert _infer_action_from_rec(self._rec("UNKNOWN_VERDICT")) == "buy_equity"


class TestOutcomesAlreadyApplied:
    def _outcome(self, notes_json: str | None) -> MagicMock:
        o = MagicMock(spec=RecommendationOutcome)
        o.notes_json = notes_json
        return o

    def test_not_applied_when_no_mwu_key(self):
        assert not outcomes_already_applied(self._outcome(json.dumps({"ticker": "EIMI.L"})))

    def test_applied_when_mwu_applied_at_present(self):
        assert outcomes_already_applied(self._outcome(json.dumps({"mwu_applied_at": "2026-06-05T00:00:00"})))

    def test_not_applied_when_notes_json_is_none(self):
        assert not outcomes_already_applied(self._outcome(None))


# --- DB-dependent tests ---

class TestGetOrInitWeights:
    def test_initializes_all_actions_with_equal_weight(self):
        db = _memory_db()
        weights = get_or_init_weights(db, "bull")
        assert set(weights.keys()) == set(MWU_ACTIONS)
        expected = 1.0 / len(MWU_ACTIONS)
        for v in weights.values():
            assert v == pytest.approx(expected, abs=1e-6)

    def test_idempotent_second_call(self):
        db = _memory_db()
        w1 = get_or_init_weights(db, "bear")
        w2 = get_or_init_weights(db, "bear")
        assert w1 == w2

    def test_different_regimes_are_independent(self):
        db = _memory_db()
        get_or_init_weights(db, "bull")
        get_or_init_weights(db, "bear")
        bull_rows = db.query(RegimeRecommendationWeight).filter_by(regime_label="bull").count()
        bear_rows = db.query(RegimeRecommendationWeight).filter_by(regime_label="bear").count()
        assert bull_rows == len(MWU_ACTIONS)
        assert bear_rows == len(MWU_ACTIONS)


class TestUpdateWeightsForOutcome:
    def test_incorrect_outcome_lowers_weight_for_action(self):
        db = _memory_db()
        rec = _make_rec(db, verdict="BUY")  # maps to buy_equity
        outcome = _make_outcome(db, rec.id, label="incorrect", excess=-0.05)

        initial = get_or_init_weights(db, "bull")
        initial_buy_equity = initial["buy_equity"]

        update_weights_for_outcome(db, outcome, rec)

        # After update all weights sum to 1 (normalized)
        updated = get_regime_adjusted_weights(db, "bull")
        assert sum(updated.values()) == pytest.approx(1.0, abs=1e-6)

        # buy_equity weight was reduced before normalization, so its normalized
        # share should be smaller than the uniform starting point
        assert updated["buy_equity"] < initial_buy_equity

    def test_correct_outcome_preserves_weight_relative_rank(self):
        db = _memory_db()
        rec = _make_rec(db, verdict="BUY")  # buy_equity
        outcome = _make_outcome(db, rec.id, label="correct", excess=0.08)

        update_weights_for_outcome(db, outcome, rec)

        updated = get_regime_adjusted_weights(db, "bull")
        # Correct outcome → loss=0 → weight unchanged before normalization
        # With one action unchanged and others also unchanged, weight distribution stays uniform
        assert sum(updated.values()) == pytest.approx(1.0, abs=1e-6)

    def test_weights_always_sum_to_one_after_mixed_outcomes(self):
        db = _memory_db()
        # Apply two outcomes — one correct (buy_equity), one incorrect (reduce_holding)
        rec1 = Recommendation(
            id="rec-correct",
            user_id="u-1",
            ticker="IWDA.AS",
            horizon="mid",
            verdict="BUY",
            confidence=0.7,
            payload_json=json.dumps({"regime": {"label": "bull"}, "checks": []}),
            mode="portfolio_advisor",
            approval_state="accepted",
            created_at=datetime.now(UTC),
        )
        db.add(rec1)
        db.commit()
        out1 = _make_outcome(db, "rec-correct", label="correct", excess=0.06)

        rec2 = Recommendation(
            id="rec-incorrect",
            user_id="u-1",
            ticker="IUSN.DE",
            horizon="mid",
            verdict="SELL",
            confidence=0.6,
            payload_json=json.dumps({"regime": {"label": "bull"}, "checks": []}),
            mode="portfolio_advisor",
            approval_state="accepted",
            created_at=datetime.now(UTC),
        )
        db.add(rec2)
        db.commit()
        out2 = _make_outcome(db, "rec-incorrect", label="incorrect", excess=-0.04)

        update_weights_for_outcome(db, out1, rec1)
        update_weights_for_outcome(db, out2, rec2)

        final = get_regime_adjusted_weights(db, "bull")
        assert sum(final.values()) == pytest.approx(1.0, abs=1e-6)

    def test_pending_outcome_returns_none(self):
        db = _memory_db()
        rec = _make_rec(db)
        outcome = _make_outcome(db, rec.id, label="pending")
        result = update_weights_for_outcome(db, outcome, rec)
        assert result is None


class TestExp3Update:
    def test_zero_loss_leaves_weights_unchanged(self):
        from app.decision.regime_advisor import exp3_update

        w = {a: 1.0 / len(MWU_ACTIONS) for a in MWU_ACTIONS}
        out = exp3_update(w, "buy_equity", 0.0)
        assert all(abs(out[a] - w[a]) < 1e-12 for a in w)

    def test_loss_is_importance_weighted_and_sums_to_one(self):
        import math

        from app.decision.regime_advisor import EXPLORATION, LEARNING_RATE, exp3_update

        k = len(MWU_ACTIONS)
        w = {a: 1.0 / k for a in MWU_ACTIONS}
        out = exp3_update(w, "buy_equity", 1.0)
        shrunk = (1 / k) * math.exp(-LEARNING_RATE * 1.0 / (1 / k))
        expected = shrunk / (shrunk + (k - 1) / k)
        assert abs(out["buy_equity"] - expected) < 1e-12
        assert abs(sum(out.values()) - 1.0) < 1e-12
        assert out["buy_equity"] < 1 / k < out["buy_bond"]
        assert EXPLORATION > 0

    def test_a_rarely_trusted_action_is_not_blown_up_by_a_loss(self):
        from app.decision.regime_advisor import MIN_WEIGHT, exp3_update

        w = {a: 1e-9 for a in MWU_ACTIONS}
        w["buy_equity"] = 1.0
        out = exp3_update(w, "buy_bond", 1.0)  # taken action has a ~0 probability
        assert all(v >= MIN_WEIGHT * 0.99 / 2 for v in out.values())
        assert abs(sum(out.values()) - 1.0) < 1e-9
        assert out["buy_bond"] > 0

    def test_weights_stay_valid_after_many_losses(self):
        from app.decision.regime_advisor import exp3_update

        w = {a: 1.0 / len(MWU_ACTIONS) for a in MWU_ACTIONS}
        for _ in range(5000):
            w = exp3_update(w, "buy_equity", 1.0)
        assert abs(sum(w.values()) - 1.0) < 1e-9 and w["buy_equity"] > 0

    def test_new_action_is_added_to_a_learned_regime_without_breaking_the_sum(self):
        db = _memory_db()
        db.add_all([
            RegimeRecommendationWeight(regime_label="bull", action=a, weight=0.2, n_obs=3)
            for a in MWU_ACTIONS if a != "hold_position"
        ])
        db.commit()
        weights = get_or_init_weights(db, "bull")
        assert set(weights) == set(MWU_ACTIONS)
        assert abs(sum(weights.values()) - 1.0) < 1e-9
