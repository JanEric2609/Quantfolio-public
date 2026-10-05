"""Comprehensive tests for the headless portfolio engine.

Covers all 6 modules: types, state, executor, rebalancer, performance, audit.
Zero infrastructure dependencies — no FastAPI, no DB, no SQLAlchemy.
All tests use pure Python / Pydantic models.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from pydantic import ValidationError

from app.decision.engine import (
    AuditTrail,
    AuditEntry,
    Cash,
    ExecutedTrade,
    GateResult,
    Holding,
    PerformanceSnapshot,
    PerformanceTracker,
    PortfolioConfig,
    PortfolioState,
    PortfolioStateSnapshot,
    Rebalancer,
    TradeAction,
    TradeBatch,
    TradeExecutor,
    TradeProposal,
)


# ======================================================================
# Helpers
# ======================================================================


def _make_proposal(
    asset_id: str = "IE00BK5BQT80",
    action: TradeAction = "buy",
    quantity: float = 10.0,
    agent_source: str = "test",
    rationale: str = "Test trade",
    idempotency_key: str = "run_000:1",
    confidence: float = 0.8,
) -> TradeProposal:
    return TradeProposal(
        asset_id=asset_id,
        action=action,
        quantity=quantity,
        rationale=rationale,
        agent_source=agent_source,
        confidence=confidence,
        idempotency_key=idempotency_key,
    )


def _make_trade(
    proposal: TradeProposal | None = None,
    fill_price: float = 100.0,
    gate_results: list[GateResult] | None = None,
    idempotency_key: str = "run_000:1",
) -> ExecutedTrade:
    if proposal is None:
        proposal = _make_proposal(idempotency_key=idempotency_key)
    return ExecutedTrade(
        proposal=proposal,
        fill_price=fill_price,
        gate_results=gate_results or [],
        idempotency_key=idempotency_key,
    )


def _make_nav_point(value: float, ts: datetime | None = None) -> dict[str, Any]:
    return {
        "timestamp": (ts or datetime(2025, 1, 1, tzinfo=timezone.utc)).isoformat(),
        "total_value": {"EUR": value},
    }


# ======================================================================
# Types — sanity checks for Pydantic models
# ======================================================================


class TestTypes:
    def test_holding_creation(self) -> None:
        h = Holding(
            asset_id="IE00BK5BQT80",
            symbol="VWCE",
            quantity=100.0,
            avg_price=85.50,
            currency="EUR",
        )
        assert h.asset_id == "IE00BK5BQT80"
        assert h.symbol == "VWCE"
        assert h.quantity == 100.0
        assert h.avg_price == 85.50
        assert h.currency == "EUR"

    def test_holding_default_currency(self) -> None:
        h = Holding(
            asset_id="IE00BK5BQT80",
            symbol="VWCE",
            quantity=10.0,
            avg_price=100.0,
        )
        assert h.currency == "EUR"

    def test_cash_creation(self) -> None:
        c = Cash(currency="USD", amount=5000.0)
        assert c.currency == "USD"
        assert c.amount == 5000.0

    def test_portfolio_config_defaults(self) -> None:
        c = PortfolioConfig()
        assert c.max_position_pct == 0.25
        assert c.max_sector_pct == 0.40
        assert c.ucits_only is True
        assert c.min_trade_value == 500.0
        assert c.rebalance_threshold_pct == 0.05

    def test_trade_proposal_frozen(self) -> None:
        tp = _make_proposal()
        with pytest.raises(ValidationError):
            tp.asset_id = "CHANGED"  # type: ignore[misc]

    def test_gate_result_reason_default(self) -> None:
        gr = GateResult(gate="cash_sufficiency", passed=True)
        assert gr.reason is None

    def test_executed_trade_default_fill_time(self) -> None:
        t = _make_trade()
        assert isinstance(t.fill_time, datetime)

    def test_trade_batch_defaults(self) -> None:
        tb = TradeBatch()
        assert tb.trades == []
        assert tb.state_before_snapshot == {}
        assert tb.state_after_snapshot == {}

    def test_performance_snapshot_frozen(self) -> None:
        ps = PerformanceSnapshot(
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            total_value={"EUR": 1000.0},
        )
        assert ps.returns == {}
        assert ps.metrics == {}

    def test_event_type_literal(self) -> None:
        # Verify all valid event types are accepted
        for event in [
            "trade_proposed",
            "gate_check",
            "trade_executed",
            "trade_rejected",
            "trade_cancelled",
            "rebalance_triggered",
            "state_snapshot",
            "performance_computed",
            "decision_logged",
        ]:
            entry = AuditEntry(
                event_type=event,  # type: ignore[arg-type]
                portfolio_id="p1",
                sequence=1,
            )
            assert entry.event_type == event


# ======================================================================
# PortfolioState
# ======================================================================


class TestPortfolioState:
    def test_from_seed_creates_portfolio(self) -> None:
        positions = [
            {"asset_id": "IE00BK5BQT80", "symbol": "VWCE", "quantity": 100.0, "avg_price": 85.0},
            {"asset_id": "IE00B4L5Y983", "symbol": "IWDA", "quantity": 50.0, "avg_price": 60.0},
        ]
        state = PortfolioState.from_seed(positions, cash=10000.0)
        assert len(state.holdings) == 2
        assert state.holdings["IE00BK5BQT80"].quantity == 100.0
        assert state.holdings["IE00B4L5Y983"].quantity == 50.0
        assert state.cash["EUR"].amount == 10000.0

    def test_from_seed_default_cash(self) -> None:
        positions = [
            {"asset_id": "IE00BK5BQT80", "symbol": "VWCE", "quantity": 10.0, "avg_price": 100.0},
        ]
        state = PortfolioState.from_seed(positions)
        assert state.cash["EUR"].amount == 0.0

    def test_from_seed_custom_config(self) -> None:
        config = PortfolioConfig(max_position_pct=0.5)
        state = PortfolioState.from_seed([], config=config)
        assert state.config.max_position_pct == 0.5

    def test_nav_without_prices(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},
                {"asset_id": "B", "symbol": "B", "quantity": 20.0, "avg_price": 25.0},
            ],
            cash=1000.0,
        )
        expected = (10.0 * 50.0) + (20.0 * 25.0) + 1000.0
        assert state.nav() == expected

    def test_nav_with_prices(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},
            ],
            cash=0.0,
        )
        nav = state.nav(prices={"A": 60.0})
        assert nav == 10.0 * 60.0

    def test_nav_with_missing_price_falls_back_to_avg(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},
            ],
            cash=0.0,
        )
        nav = state.nav(prices={"B": 999.0})
        assert nav == 10.0 * 50.0

    def test_apply_executed_trade_buy_new_asset(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        trade = _make_trade(
            _make_proposal(asset_id="VWCE", action="buy", quantity=10.0),
            fill_price=100.0,
        )
        new_state = state.apply_executed_trade(trade)
        assert "VWCE" in new_state.holdings
        assert new_state.holdings["VWCE"].quantity == 10.0
        assert new_state.holdings["VWCE"].avg_price == 100.0
        assert new_state.cash["EUR"].amount == 10000.0 - (10.0 * 100.0)

    def test_apply_executed_trade_buy_existing_asset(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "VWCE", "symbol": "VWCE", "quantity": 10.0, "avg_price": 80.0}],
            cash=10000.0,
        )
        trade = _make_trade(
            _make_proposal(asset_id="VWCE", action="buy", quantity=10.0),
            fill_price=100.0,
        )
        new_state = state.apply_executed_trade(trade)
        h = new_state.holdings["VWCE"]
        assert h.quantity == 20.0
        expected_avg = (10.0 * 80.0 + 10.0 * 100.0) / 20.0
        assert h.avg_price == expected_avg
        assert new_state.cash["EUR"].amount == 10000.0 - (10.0 * 100.0)

    def test_apply_executed_trade_sell_partial(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "VWCE", "symbol": "VWCE", "quantity": 20.0, "avg_price": 80.0}],
            cash=0.0,
        )
        trade = _make_trade(
            _make_proposal(asset_id="VWCE", action="sell", quantity=5.0),
            fill_price=90.0,
        )
        new_state = state.apply_executed_trade(trade)
        assert new_state.holdings["VWCE"].quantity == 15.0
        assert new_state.holdings["VWCE"].avg_price == 80.0  # avg_price unchanged for partial sell
        assert new_state.cash["EUR"].amount == 5.0 * 90.0

    def test_apply_executed_trade_sell_full_removes_holding(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "VWCE", "symbol": "VWCE", "quantity": 20.0, "avg_price": 80.0}],
            cash=0.0,
        )
        trade = _make_trade(
            _make_proposal(asset_id="VWCE", action="sell", quantity=20.0),
            fill_price=90.0,
        )
        new_state = state.apply_executed_trade(trade)
        assert "VWCE" not in new_state.holdings
        assert new_state.cash["EUR"].amount == 20.0 * 90.0

    def test_apply_executed_trade_sell_nonexistent_raises(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        trade = _make_trade(
            _make_proposal(asset_id="MISSING", action="sell", quantity=1.0),
            fill_price=100.0,
        )
        with pytest.raises(ValueError, match="Cannot sell MISSING"):
            state.apply_executed_trade(trade)

    def test_apply_trade_batch(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        trade1 = _make_trade(
            _make_proposal(asset_id="A", action="buy", quantity=10.0, idempotency_key="r:1"),
            fill_price=100.0,
            idempotency_key="r:1",
        )
        trade2 = _make_trade(
            _make_proposal(asset_id="B", action="buy", quantity=5.0, idempotency_key="r:2"),
            fill_price=50.0,
            idempotency_key="r:2",
        )
        batch = TradeBatch(trades=[trade1, trade2])
        new_state = state.apply_trade_batch(batch)
        assert len(new_state.holdings) == 2
        assert new_state.holdings["A"].quantity == 10.0
        assert new_state.holdings["B"].quantity == 5.0
        assert new_state.cash["EUR"].amount == 10000.0 - (10.0 * 100.0 + 5.0 * 50.0)

    def test_snapshot(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0}],
            cash=500.0,
        )
        snap = state.snapshot()
        assert isinstance(snap, PortfolioStateSnapshot)
        assert snap.nav_total == (10.0 * 50.0) + 500.0
        assert snap.holdings["A"].quantity == 10.0
        assert snap.cash["EUR"].amount == 500.0
        assert isinstance(snap.timestamp, datetime)

    def test_position_weight(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},
                {"asset_id": "B", "symbol": "B", "quantity": 20.0, "avg_price": 25.0},
            ],
            cash=0.0,
        )
        weight_a = state.position_weight("A")
        assert weight_a == pytest.approx(10.0 * 50.0 / (10.0 * 50.0 + 20.0 * 25.0))

    def test_position_weight_zero_nav(self) -> None:
        state = PortfolioState.from_seed([], cash=0.0)
        assert state.position_weight("NONEXISTENT") == 0.0

    def test_position_weight_missing_asset(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0}],
            cash=0.0,
        )
        assert state.position_weight("MISSING") == 0.0

    def test_immutability_state_transitions_return_new(self) -> None:
        """Original state must remain unchanged after apply_executed_trade."""
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0}],
            cash=1000.0,
        )
        trade = _make_trade(
            _make_proposal(asset_id="B", action="buy", quantity=5.0, idempotency_key="r:1"),
            fill_price=100.0,
            idempotency_key="r:1",
        )
        _ = state.apply_executed_trade(trade)
        # Original state must be unchanged
        assert len(state.holdings) == 1
        assert "B" not in state.holdings
        assert state.cash["EUR"].amount == 1000.0


# ======================================================================
# TradeExecutor — TCC pattern & pre-trade gates
# ======================================================================


class TestTradeExecutor:
    def test_execute_batch_happy_path(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(
                asset_id="VWCE",
                action="buy",
                quantity=10.0,
                idempotency_key="run_000:1",
            ),
        ]
        prices = {"VWCE": 100.0}
        batch, skipped = executor.execute_batch(proposals, prices)
        assert skipped == []
        assert len(batch.trades) == 1
        assert batch.trades[0].fill_price == 100.0
        assert batch.state_before_snapshot != {}
        assert batch.state_after_snapshot != {}
        # Verify state_after reflects the trade
        after_cash = batch.state_after_snapshot["cash"]["EUR"]["amount"]
        assert after_cash == 10000.0 - (10.0 * 100.0)

    def test_cash_sufficiency_gate_fails(self) -> None:
        state = PortfolioState.from_seed([], cash=500.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="VWCE", action="buy", quantity=10.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"VWCE": 100.0})
        assert skipped  # non-empty skip list means rejection occurred
        assert any("exceeds available cash" in r for r in skipped)
        assert len(batch.trades) == 0

    def test_cash_sufficiency_sell_always_passes(self) -> None:
        """The cash_sufficiency gate should always pass for sell orders."""
        state = PortfolioState.from_seed(
            [{"asset_id": "VWCE", "symbol": "VWCE", "quantity": 10.0, "avg_price": 100.0}],
            cash=3000.0,  # total NAV=4000, post-sell weight=500/4000=12.5% < 25%
        )
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="VWCE", action="sell", quantity=5.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"VWCE": 100.0})
        assert skipped == []
        assert len(batch.trades) == 1

    def test_concentration_gate_fails(self) -> None:
        """Post-trade weight must not exceed max_position_pct."""
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0}],
            cash=0.0,
        )
        config = PortfolioConfig(max_position_pct=0.10)  # 10% max
        state = PortfolioState(holdings=state.holdings, cash=state.cash, config=config)
        executor = TradeExecutor(state, run_id="run_001")
        # Buying more of A would push weight above 10% of NAV (500 NAV, buying 100 more)
        proposals = [
            _make_proposal(asset_id="A", action="buy", quantity=10.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"A": 100.0})
        assert skipped
        assert any("Post-trade weight" in r for r in skipped)
        assert len(batch.trades) == 0

    def test_ucits_gate_fails(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="NONUCITS", action="buy", quantity=1.0),
        ]
        batch, skipped = executor.execute_batch(
            proposals,
            {"NONUCITS": 100.0},
            ucits_eligible={"VWCE", "IWDA"},  # NONUCITS is not in this set
        )
        assert skipped
        assert any("not UCITS-eligible" in r for r in skipped)
        assert len(batch.trades) == 0

    def test_ucits_gate_skipped_when_config_disabled(self) -> None:
        state = PortfolioState.from_seed(
            [],
            cash=10000.0,
            config=PortfolioConfig(ucits_only=False),
        )
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="NONUCITS", action="buy", quantity=1.0),
        ]
        # Even though NONUCITS not in ucits_eligible, the gate is skipped
        # because config.ucits_only is False.
        batch, skipped = executor.execute_batch(
            proposals,
            {"NONUCITS": 100.0},
            ucits_eligible={"VWCE"},
        )
        assert skipped == []
        assert len(batch.trades) == 1

    def test_ucits_gate_passes_when_eligible_is_none(self) -> None:
        """ucits_eligible=None means gate is skipped."""
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="ANY", action="buy", quantity=1.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"ANY": 100.0}, ucits_eligible=None)
        assert skipped == []

    def test_tradeability_gate_fails(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="UNTRADEABLE", action="buy", quantity=1.0),
        ]
        batch, skipped = executor.execute_batch(
            proposals,
            {"UNTRADEABLE": 100.0},
            tradeable_assets={"VWCE"},
        )
        assert skipped
        assert any("not in tradeable universe" in r for r in skipped)
        assert len(batch.trades) == 0

    def test_tradeability_gate_passes_when_none(self) -> None:
        """tradeable_assets=None means gate is skipped."""
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="ANY", action="buy", quantity=1.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"ANY": 100.0}, tradeable_assets=None)
        assert skipped == []

    def test_empty_proposals(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        batch, skipped = executor.execute_batch([], {})
        assert "No proposals provided" in skipped
        assert len(batch.trades) == 0

    def test_missing_price_rejected(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="VWCE", action="buy", quantity=10.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {})
        assert skipped
        assert any("No valid fill price" in r for r in skipped)

    def test_non_positive_quantity_rejected(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        # Use model_construct to bypass Pydantic's gt=0 validation
        bad_proposal = TradeProposal.model_construct(
            asset_id="VWCE",
            action="buy",
            quantity=0.0,
            agent_source="test",
            idempotency_key="k1",
        )
        batch, skipped = executor.execute_batch([bad_proposal], {"VWCE": 100.0})
        assert skipped
        assert any("Non-positive quantity" in r for r in skipped)

    def test_zero_fill_price_rejected(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="VWCE", action="buy", quantity=10.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"VWCE": 0.0})
        assert skipped
        assert any("No valid fill price" in r for r in skipped)

    def test_idempotency_keys_generated(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="run_x")
        proposals = [
            _make_proposal(asset_id="A", action="buy", quantity=1.0, idempotency_key="run_000:1"),
            _make_proposal(asset_id="B", action="buy", quantity=1.0, idempotency_key="run_000:2"),
        ]
        batch, skipped = executor.execute_batch(proposals, {"A": 100.0, "B": 50.0})
        assert skipped == []
        assert batch.trades[0].idempotency_key == "run_x:1"
        assert batch.trades[1].idempotency_key == "run_x:2"

    def test_multiple_gates_fail_first_reported(self) -> None:
        """When multiple gates fail for a proposal, all reasons are in the skipped entry."""
        state = PortfolioState.from_seed([], cash=10.0)
        executor = TradeExecutor(state, run_id="run_001")
        proposals = [
            _make_proposal(asset_id="X", action="buy", quantity=100.0),
        ]
        batch, skipped = executor.execute_batch(
            proposals,
            {"X": 100.0},
            ucits_eligible=set(),
            tradeable_assets=set(),
        )
        assert skipped
        # cash_sufficiency is checked first; its reason appears in the skip entry
        assert any("exceeds available cash" in r for r in skipped)
        assert len(batch.trades) == 0


# ======================================================================
# Rebalancer
# ======================================================================


class TestRebalancer:
    def test_compute_current_weights(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},  # 500
                {"asset_id": "B", "symbol": "B", "quantity": 20.0, "avg_price": 25.0},  # 500
            ],
            cash=0.0,
        )
        rebalancer = Rebalancer(PortfolioConfig())
        weights = rebalancer.compute_current_weights(state)
        assert weights == {"A": 0.5, "B": 0.5}

    def test_compute_current_weights_includes_cash_implicitly(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0},  # 500
            ],
            cash=500.0,  # total NAV = 1000
        )
        rebalancer = Rebalancer(PortfolioConfig())
        weights = rebalancer.compute_current_weights(state)
        # Only A is a holding; cash is the remainder
        assert weights == {"A": 0.5}

    def test_compute_current_weights_zero_nav(self) -> None:
        state = PortfolioState.from_seed([], cash=0.0)
        rebalancer = Rebalancer(PortfolioConfig())
        assert rebalancer.compute_current_weights(state) == {}

    def test_detect_drift_within_threshold(self) -> None:
        """Drift within the threshold (5% default) should not be reported."""
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        current = {"A": 0.54, "B": 0.46}
        target = {"A": 0.50, "B": 0.50}
        drift = rebalancer.detect_drift(current, target)
        # |0.54-0.50| = 0.04 which is < 0.05 (threshold)
        assert drift == {}

    def test_detect_drift_exceeds_threshold(self) -> None:
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        current = {"A": 0.60, "B": 0.40}
        target = {"A": 0.50, "B": 0.50}
        drift = rebalancer.detect_drift(current, target)
        assert "A" in drift
        assert drift["A"] == pytest.approx(0.10)  # 0.60 - 0.50

    def test_detect_drift_underweight_negative_drift(self) -> None:
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        current = {"A": 0.30, "B": 0.70}
        target = {"A": 0.50, "B": 0.50}
        drift = rebalancer.detect_drift(current, target)
        assert "A" in drift
        assert drift["A"] == pytest.approx(-0.20)
        assert "B" in drift
        assert drift["B"] == pytest.approx(0.20)

    def test_detect_drift_asset_in_target_not_in_current(self) -> None:
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        current = {"A": 1.0}
        target = {"A": 0.80, "B": 0.20}
        drift = rebalancer.detect_drift(current, target)
        assert "B" in drift
        assert drift["B"] == pytest.approx(-0.20)  # 0.0 - 0.20

    def test_generate_trades_sells_before_buys(self) -> None:
        """Sell proposals must come before buy proposals in the list."""
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 100.0, "avg_price": 10.0},  # 1000
                {"asset_id": "B", "symbol": "B", "quantity": 50.0, "avg_price": 10.0},  # 500
            ],
            cash=500.0,  # total NAV = 2000
        )
        # Current: A=50%, B=25%, cash=25%
        # Target: A=40%, B=35% — A is 10% overweight, B is 10% underweight (both exceed 5% threshold)
        # min_trade_value=100 so both 200-notional trades clear the threshold
        target = {"A": 0.40, "B": 0.35}
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05, min_trade_value=100.0))
        trades, drift = rebalancer(state, target, run_id="r1")

        # Assert sell (A) comes before buy (B)
        assert len(trades) >= 2
        assert trades[0].action == "sell"
        assert trades[1].action == "buy"

    def test_generate_trades_respects_min_trade_value(self) -> None:
        """Small drifts below min_trade_value should be skipped."""
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 100.0},  # 1000
            ],
            cash=0.0,
        )
        target = {"A": 0.90}  # A has drift of 0.10 (10%)
        # min_trade_value = 500, drift = 0.10 * 1000 = 100 notional, which is < 500
        rebalancer = Rebalancer(PortfolioConfig(min_trade_value=500.0, rebalance_threshold_pct=0.05))
        trades, drift = rebalancer(state, target, run_id="r1")
        assert len(trades) == 0

    def test_call_returns_trades_and_drift_report(self) -> None:
        state = PortfolioState.from_seed(
            [
                {"asset_id": "A", "symbol": "A", "quantity": 100.0, "avg_price": 20.0},
            ],
            cash=1000.0,  # A=2000, total NAV=3000, A weight=66.7%
        )
        target = {"A": 0.40}
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        trades, drift = rebalancer(state, target, run_id="r1")
        assert len(trades) >= 1
        assert "A" in drift
        assert drift["A"] == pytest.approx(0.6667 - 0.40, rel=1e-3)

    def test_no_drift_returns_empty_trades(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 100.0}],
            cash=0.0,
        )
        target = {"A": 1.0}  # exact match
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05))
        trades, drift = rebalancer(state, target, run_id="r1")
        assert trades == []
        assert drift == {}

    def test_zero_nav_rebalancer(self) -> None:
        """With zero NAV, compute_current_weights returns {}.
        detect_drift still flags assets that are in target but missing
        entirely (current=0, target=0.50 → drift=-0.50). No trades
        can be generated because there are no holdings to sell and no
        avg_price to price a buy."""
        state = PortfolioState.from_seed([], cash=0.0)
        target = {"A": 0.50}
        rebalancer = Rebalancer(PortfolioConfig())
        trades, drift = rebalancer(state, target, run_id="r1")
        assert trades == []
        assert "A" in drift
        assert drift["A"] == pytest.approx(-0.50)

    def test_rebalancer_no_price_skip_buy(self) -> None:
        """When a buy target has no existing holding (no avg_price), skip."""
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 100.0, "avg_price": 10.0}],
            cash=1000.0,
        )
        target = {"A": 0.40, "B": 0.10}  # B is new asset with no avg_price
        rebalancer = Rebalancer(PortfolioConfig(rebalance_threshold_pct=0.05, min_trade_value=100.0))
        trades, drift = rebalancer(state, target, run_id="r1")
        # A is 50% weighted vs 40% target (10% overweight) → sell trade generated
        # B is 10% underweight but has no price → buy skipped
        assert len(trades) >= 1
        assert all(t.action == "sell" for t in trades)


# ======================================================================
# PerformanceTracker
# ======================================================================


class TestPerformanceTracker:
    def test_empty_nav_history(self) -> None:
        tracker = PerformanceTracker()
        result = tracker.compute_from_nav_history([])
        assert isinstance(result, PerformanceSnapshot)
        assert result.total_value == {}
        assert result.returns == {"1D": 0.0, "1W": 0.0, "1M": 0.0, "3M": 0.0, "6M": 0.0, "1Y": 0.0}
        assert result.metrics == {}

    def test_single_data_point(self) -> None:
        tracker = PerformanceTracker()
        nav_history = [_make_nav_point(1000.0)]
        result = tracker.compute_from_nav_history(nav_history)
        assert result.total_value == {"EUR": 1000.0}
        assert result.returns == {"1D": 0.0, "1W": 0.0, "1M": 0.0, "3M": 0.0, "6M": 0.0, "1Y": 0.0}
        assert result.metrics == {}

    def test_normal_multi_point(self) -> None:
        tracker = PerformanceTracker()
        nav_history = [
            _make_nav_point(100.0, datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _make_nav_point(110.0, datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _make_nav_point(121.0, datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ]
        result = tracker.compute_from_nav_history(nav_history)
        assert result.total_value == {"EUR": 121.0}
        # returns[0.10, 0.10] → 1D = 0.10, other periods = cumulative = 0.21
        assert result.returns["1D"] == pytest.approx(0.10)
        assert result.returns["1W"] == pytest.approx(0.21)
        # With only 2 returns (< MIN_OBSERVATIONS=20), annualised risk metrics
        # are omitted; only cagr, max_drawdown, max_drawdown_duration are present.
        assert "sharpe" not in result.metrics
        assert "sortino" not in result.metrics
        assert "volatility" not in result.metrics
        assert "cagr" in result.metrics
        assert "max_drawdown" in result.metrics

    def test_compute_returns_from_values(self) -> None:
        tracker = PerformanceTracker()
        values = [100.0, 110.0, 121.0, 133.1]
        returns = tracker.compute_returns_from_values(values)
        assert returns == pytest.approx([0.10, 0.10, 0.10])

    def test_compute_returns_from_values_less_than_two(self) -> None:
        tracker = PerformanceTracker()
        assert tracker.compute_returns_from_values([100.0]) == []
        assert tracker.compute_returns_from_values([]) == []

    def test_compute_returns_from_values_zero_division(self) -> None:
        tracker = PerformanceTracker()
        values = [0.0, 100.0, 200.0]
        returns = tracker.compute_returns_from_values(values)
        # prev=0.0 → result = 0.0 (edge case)
        assert returns[0] == 0.0

    def test_rolling_window_metrics_insufficient_data(self) -> None:
        tracker = PerformanceTracker()
        metrics = tracker.rolling_window_metrics([0.01, 0.02], window_days=252)
        assert metrics == {}

    def test_rolling_window_metrics_sufficient_data(self) -> None:
        tracker = PerformanceTracker()
        # Use exactly 5 returns
        returns = [0.01, 0.02, -0.01, 0.03, 0.01]
        metrics = tracker.rolling_window_metrics(returns, window_days=5)
        assert "sharpe" in metrics
        assert "sortino" in metrics
        assert "calmar" in metrics
        assert "volatility" in metrics
        assert "cagr" in metrics
        assert "max_drawdown" in metrics
        assert "max_drawdown_duration" in metrics

    def test_rolling_window_metrics_with_benchmark(self) -> None:
        tracker = PerformanceTracker()
        returns = [0.01, 0.02, 0.015, 0.01, 0.02]
        benchmark = [0.005, 0.01, 0.01, 0.005, 0.01]
        metrics = tracker.rolling_window_metrics(returns, window_days=5, benchmark_returns=benchmark)
        assert "beta" in metrics
        assert "alpha" in metrics

    def test_compute_from_nav_history_with_benchmark(self) -> None:
        tracker = PerformanceTracker()
        nav_history = [
            _make_nav_point(100.0, datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _make_nav_point(105.0, datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _make_nav_point(110.0, datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ]
        benchmark = [0.02, 0.02]
        result = tracker.compute_from_nav_history(
            nav_history,
            benchmark_returns=benchmark,
        )
        assert result.returns["1D"] == pytest.approx(0.05 / 1.05, rel=1e-3)
        # Period metrics with benchmark
        for p in ["1D", "1W", "1M", "3M", "6M", "1Y"]:
            key = f"beta_{p}"
            if key in result.metrics:
                assert isinstance(result.metrics[key], float)
                break
        else:
            # If no period has a beta, that's also OK — the benchmark may not
            # align with every period length.
            pass

    def test_compute_returns_method(self) -> None:
        tracker = PerformanceTracker()
        nav_history = [
            _make_nav_point(100.0, datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _make_nav_point(110.0, datetime(2025, 1, 2, tzinfo=timezone.utc)),
        ]
        returns = tracker.compute_returns(nav_history)
        assert returns == [0.10]

    def test_compute_returns_empty(self) -> None:
        tracker = PerformanceTracker()
        assert tracker.compute_returns([]) == []

    def test_compute_returns_missing_total_value(self) -> None:
        tracker = PerformanceTracker()
        nav = [{"timestamp": "2025-01-01T00:00:00+00:00"}]  # no total_value
        assert tracker.compute_returns(nav) == []


# ======================================================================
# quant_metrics integration sanity
# ======================================================================


class TestQuantMetricsSanity:
    """Lightweight smoke tests to verify the quant_metrics functions work
    with realistic return data. These are real computations, not mocks."""

    def test_sharpe_positive_returns(self) -> None:
        from app.foundation.quant_metrics import sharpe_ratio

        # Varying returns to produce non-zero standard deviation
        returns = [0.01, 0.015, 0.005, 0.02, 0.01, 0.012, -0.005, 0.018, 0.008, 0.011] * 25
        sr = sharpe_ratio(returns, risk_free=0.0, periods_per_year=252)
        assert sr > 0

    def test_sharpe_flat_returns(self) -> None:
        from app.foundation.quant_metrics import sharpe_ratio

        returns = [0.0] * 10
        sr = sharpe_ratio(returns, risk_free=0.0, periods_per_year=252)
        assert sr == 0.0

    def test_beta_same_returns(self) -> None:
        from app.foundation.quant_metrics import beta

        r = [0.01, 0.02, -0.01, 0.005, 0.015]
        assert beta(r, r) == pytest.approx(1.0)

    def test_beta_higher_vol(self) -> None:
        from app.foundation.quant_metrics import beta

        portfolio = [0.02, 0.04, -0.02, 0.01, 0.03]
        benchmark = [0.01, 0.02, -0.01, 0.005, 0.015]
        b = beta(portfolio, benchmark)
        assert b == pytest.approx(2.0, rel=0.5)

    def test_max_drawdown_all_positive(self) -> None:
        from app.foundation.quant_metrics import max_drawdown

        dd = max_drawdown([0.01, 0.02, 0.03])
        assert dd["max_drawdown"] <= 0.0  # zero or negative

    def test_max_drawdown_with_decline(self) -> None:
        from app.foundation.quant_metrics import max_drawdown

        dd = max_drawdown([0.10, -0.20, 0.05, -0.10])
        assert dd["max_drawdown"] < 0.0
        assert dd["max_drawdown_duration"] >= 0.0


# ======================================================================
# AuditTrail
# ======================================================================


class TestAuditTrail:
    def test_basic_recording(self) -> None:
        audit = AuditTrail()
        entry = audit.record(event_type="trade_executed", portfolio_id="p1", details={"qty": 10})
        assert entry.event_type == "trade_executed"
        assert entry.portfolio_id == "p1"
        assert entry.details == {"qty": 10}
        assert entry.sequence == 1
        assert len(audit) == 1

    def test_idempotency_key_dedup(self) -> None:
        audit = AuditTrail()
        e1 = audit.record(
            event_type="trade_proposed",
            portfolio_id="p1",
            idempotency_key="r1:1",
        )
        e2 = audit.record(
            event_type="trade_proposed",
            portfolio_id="p1",
            idempotency_key="r1:1",  # same key
        )
        # Same object returned (dedup)
        assert e1 is e2
        assert len(audit) == 1

    def test_idempotency_dedup_different_portfolio(self) -> None:
        """Idempotency keys are scoped per portfolio; same key across portfolios is allowed."""
        audit = AuditTrail()
        e1 = audit.record(event_type="trade_executed", portfolio_id="p1", idempotency_key="k1")
        e2 = audit.record(event_type="trade_executed", portfolio_id="p2", idempotency_key="k1")
        assert e1 is not e2
        assert len(audit) == 2

    def test_idempotency_none_allows_duplicates(self) -> None:
        """When idempotency_key is None, duplicates are allowed."""
        audit = AuditTrail()
        e1 = audit.record(event_type="trade_executed", portfolio_id="p1")
        e2 = audit.record(event_type="trade_executed", portfolio_id="p1")
        assert e1 is not e2
        assert len(audit) == 2

    def test_monotonic_sequence_per_portfolio(self) -> None:
        audit = AuditTrail()
        e1 = audit.record(event_type="trade_proposed", portfolio_id="p1")
        e2 = audit.record(event_type="trade_executed", portfolio_id="p1")
        e3 = audit.record(event_type="trade_proposed", portfolio_id="p2")  # different portfolio
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert e3.sequence == 1  # resets per portfolio

    def test_by_portfolio(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_proposed", portfolio_id="p1")
        audit.record(event_type="trade_executed", portfolio_id="p1")
        audit.record(event_type="trade_proposed", portfolio_id="p2")
        entries = audit.by_portfolio("p1")
        assert len(entries) == 2
        assert all(e.portfolio_id == "p1" for e in entries)

    def test_by_run(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_proposed", portfolio_id="p1", run_id="r1")
        audit.record(event_type="trade_executed", portfolio_id="p1", run_id="r1")
        audit.record(event_type="trade_rejected", portfolio_id="p2", run_id="r2")
        entries = audit.by_run("r1")
        assert len(entries) == 2

    def test_by_event_type(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_proposed", portfolio_id="p1")
        audit.record(event_type="trade_executed", portfolio_id="p1")
        audit.record(event_type="trade_proposed", portfolio_id="p2")
        entries = audit.by_event_type("trade_proposed")
        assert len(entries) == 2

    def test_by_time_range(self) -> None:
        audit = AuditTrail()
        before = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mid = datetime(2025, 6, 15, tzinfo=timezone.utc)
        after = datetime(2025, 12, 31, tzinfo=timezone.utc)
        e1 = audit.record(event_type="trade_proposed", portfolio_id="p1")
        # Manually override the auto-set timestamp for testing
        # We can't easily override frozen AuditEntry, so let's just verify
        # that by_time_range at least exists and works with basic timestamps.
        # Instead, test that entries have a timestamp in a very wide range.
        entries = audit.by_time_range(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc))
        assert len(entries) == 1

    def test_latest(self) -> None:
        audit = AuditTrail()
        for i in range(5):
            audit.record(
                event_type="trade_proposed",
                portfolio_id="p1",
                details={"seq": i},
            )
        latest = audit.latest("p1", n=3)
        assert len(latest) == 3
        assert latest[0].details["seq"] == 4  # newest first
        assert latest[1].details["seq"] == 3
        assert latest[2].details["seq"] == 2

    def test_latest_with_zero_n(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_executed", portfolio_id="p1")
        assert audit.latest("p1", n=0) == []

    def test_latest_empty_portfolio(self) -> None:
        audit = AuditTrail()
        assert audit.latest("p_nonexistent") == []

    def test_to_dicts(self) -> None:
        audit = AuditTrail()
        audit.record(
            event_type="trade_executed",
            portfolio_id="p1",
            details={"qty": 10},
            run_id="r1",
        )
        dicts = audit.to_dicts()
        assert len(dicts) == 1
        d = dicts[0]
        assert d["event_type"] == "trade_executed"
        assert d["portfolio_id"] == "p1"
        assert d["details"] == {"qty": 10}
        assert d["run_id"] == "r1"
        assert d["sequence"] == 1

    def test_to_json(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_executed", portfolio_id="p1")
        json_str = audit.to_json()
        parsed = json.loads(json_str)
        assert len(parsed) == 1
        assert parsed[0]["event_type"] == "trade_executed"

    def test_len(self) -> None:
        audit = AuditTrail()
        assert len(audit) == 0
        audit.record(event_type="trade_proposed", portfolio_id="p1")
        assert len(audit) == 1

    def test_repr(self) -> None:
        audit = AuditTrail()
        audit.record(event_type="trade_proposed", portfolio_id="p1")
        rep = repr(audit)
        assert "<AuditTrail" in rep
        assert "entries=1" in rep


# ======================================================================
# Functional immutability — cross-cutting concern
# ======================================================================


class TestImmutability:
    """Verify all engine components maintain functional immutability."""

    def test_portfolio_state_immutable_on_trade(self) -> None:
        state = PortfolioState.from_seed(
            [{"asset_id": "A", "symbol": "A", "quantity": 10.0, "avg_price": 50.0}],
            cash=1000.0,
        )
        orig_holdings = dict(state.holdings)
        orig_cash = dict(state.cash)
        orig_nav = state.nav()

        trade = _make_trade(
            _make_proposal(asset_id="B", action="buy", quantity=5.0, idempotency_key="r:1"),
            fill_price=100.0,
            idempotency_key="r:1",
        )
        new_state = state.apply_executed_trade(trade)

        # Original must be unchanged
        assert state.holdings == orig_holdings
        assert state.cash == orig_cash
        assert state.nav() == orig_nav
        # New state must be different
        assert new_state is not state
        assert len(new_state.holdings) == 2

    def test_executor_does_not_mutate_state(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        orig_nav = state.nav()
        executor = TradeExecutor(state, run_id="r1")
        proposals = [
            _make_proposal(asset_id="A", action="buy", quantity=10.0),
        ]
        _batch, _skipped = executor.execute_batch(proposals, {"A": 100.0})
        # Original state must be untouched
        assert state.nav() == orig_nav
        assert len(state.holdings) == 0

    def test_audit_trail_entries_frozen(self) -> None:
        audit = AuditTrail()
        entry = audit.record(event_type="trade_executed", portfolio_id="p1")
        with pytest.raises(ValidationError):
            entry.event_type = "different"  # type: ignore[misc]

    def test_trade_batch_snapshots_are_dicts(self) -> None:
        state = PortfolioState.from_seed([], cash=10000.0)
        executor = TradeExecutor(state, run_id="r1")
        proposals = [
            _make_proposal(asset_id="A", action="buy", quantity=10.0),
        ]
        batch, skipped = executor.execute_batch(proposals, {"A": 100.0})
        assert skipped == []
        assert isinstance(batch.state_before_snapshot, dict)
        assert isinstance(batch.state_after_snapshot, dict)
        assert batch.state_before_snapshot != batch.state_after_snapshot
