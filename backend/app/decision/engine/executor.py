"""TCC TradeExecutor with deterministic pre-trade gates.

TCC pattern: Try (validate) → Cancel (rollback) → Confirm (execute).
Per-proposal skip-and-continue: an un-priceable or gate-failed proposal
is skipped with a recorded reason; the remaining proposals still execute.
"""

from __future__ import annotations

from app.decision.engine.state import PortfolioState
from app.decision.engine.types import (
    TradeProposal,
    GateResult,
    ExecutedTrade,
    TradeBatch,
)


class TradeExecutor:
    """TCC (Try-Cancel-Confirm) Trade Executor.

    Validates trade proposals through 4 deterministic pre-trade gates
    and executes them atomically. Pure functions on PortfolioState — no
    side effects, no DB, no network.

    Gates (evaluated in order; all must pass):
      1. cash_sufficiency  — buy orders require sufficient cash
      2. ucits_compliance  — if config.ucits_only, asset must be eligible
      3. tradeability      — asset must be in tradeable universe
      4. concentration     — post-trade weight ≤ config.max_position_pct
    """

    def __init__(self, state: PortfolioState, run_id: str) -> None:
        self._state = state
        self._run_id = run_id
        self._seq = 0

    # ------------------------------------------------------------------
    # Idempotency keys
    # ------------------------------------------------------------------

    def _next_idempotency_key(self) -> str:
        self._seq += 1
        return f"{self._run_id}:{self._seq}"

    # ------------------------------------------------------------------
    # Pre-trade gates
    # ------------------------------------------------------------------

    def _check_cash_sufficiency(
        self,
        proposal: TradeProposal,
        fill_price: float,
        state: PortfolioState | None = None,
    ) -> GateResult:
        if proposal.action != "buy":
            return GateResult(gate="cash_sufficiency", passed=True)

        state = state or self._state
        cost = proposal.quantity * fill_price
        currency = getattr(proposal, "currency", None)
        if currency is not None and currency in state.cash:
            available_cash = state.cash[currency].amount
        else:
            base_cash = state.cash.get("EUR")
            available_cash = base_cash.amount if base_cash else 0.0

        if cost > available_cash:
            return GateResult(
                gate="cash_sufficiency",
                passed=False,
                reason=(
                    f"Buy {proposal.asset_id} {proposal.quantity}×{fill_price:.2f} = "
                    f"{cost:.2f} exceeds available cash {available_cash:.2f}"
                ),
            )
        return GateResult(gate="cash_sufficiency", passed=True)

    def _check_ucits(
        self,
        proposal: TradeProposal,
        ucits_eligible: set[str] | None,
    ) -> GateResult:
        if not self._state.config.ucits_only:
            return GateResult(gate="ucits_compliance", passed=True)
        if ucits_eligible is not None and proposal.asset_id not in ucits_eligible:
            return GateResult(
                gate="ucits_compliance",
                passed=False,
                reason=f"Asset {proposal.asset_id} is not UCITS-eligible",
            )
        return GateResult(gate="ucits_compliance", passed=True)

    def _check_tradeability(
        self,
        proposal: TradeProposal,
        tradeable_assets: set[str] | None,
    ) -> GateResult:
        if tradeable_assets is not None and proposal.asset_id not in tradeable_assets:
            return GateResult(
                gate="tradeability",
                passed=False,
                reason=f"Asset {proposal.asset_id} is not in tradeable universe",
            )
        return GateResult(gate="tradeability", passed=True)

    def _check_concentration(
        self,
        proposal: TradeProposal,
        fill_price: float,
        prices: dict[str, float],
        state: PortfolioState | None = None,
    ) -> GateResult:
        state = state or self._state
        holdings_value = sum(
            h.quantity * prices.get(h.asset_id, h.avg_price)
            for h in state.holdings.values()
        )
        total_cash = sum(c.amount for c in state.cash.values())
        nav = holdings_value + total_cash

        if nav <= 0:
            return GateResult(gate="concentration", passed=True)

        current_pos_value = sum(
            h.quantity * prices.get(h.asset_id, h.avg_price)
            for h in state.holdings.values()
            if h.asset_id == proposal.asset_id
        )

        post_value = (
            current_pos_value + proposal.quantity * fill_price
            if proposal.action == "buy"
            else max(0.0, current_pos_value - proposal.quantity * fill_price)
        )
        post_pct = post_value / nav
        max_pct = self._state.config.max_position_pct

        if post_pct > max_pct:
            return GateResult(
                gate="concentration",
                passed=False,
                reason=(
                    f"Post-trade weight {post_pct:.2%} exceeds "
                    f"max {max_pct:.2%} for {proposal.asset_id}"
                ),
            )
        return GateResult(gate="concentration", passed=True)

    # ------------------------------------------------------------------
    # Public API — TCC pattern
    # ------------------------------------------------------------------

    def execute_batch(
        self,
        proposals: list[TradeProposal],
        prices: dict[str, float],
        ucits_eligible: set[str] | None = None,
        tradeable_assets: set[str] | None = None,
    ) -> tuple[TradeBatch, list[str]]:
        """TCC: Try → Cancel → Confirm — per-proposal skip-and-continue.

        Args:
            proposals: Trade proposals to validate and execute.
            prices: Current market prices for all relevant assets
                (asset_id → price). Used as fill price for traded
                assets and for NAV computation.
            ucits_eligible: Set of UCITS-eligible asset IDs.
                ``None`` skips the UCITS gate entirely
                (still gated by ``config.ucits_only``).
            tradeable_assets: Set of tradeable asset IDs.
                ``None`` skips the tradeability gate.

        Returns:
            ``(TradeBatch, skipped)`` where ``skipped`` is a list of
            human-readable skip reasons (empty when all proposals executed).
            A single bad proposal no longer cancels the entire batch —
            valid proposals still execute and the skipped list records why
            each proposal was not filled.
        """
        if not proposals:
            return (
                TradeBatch(state_before_snapshot=self._state.model_dump()),
                ["No proposals provided"],
            )

        skipped: list[str] = []
        executed_trades: list[ExecutedTrade] = []
        working_state = self._state

        for proposal in proposals:
            # ── quantity guard ──
            if proposal.quantity <= 0:
                skipped.append(
                    f"Non-positive quantity {proposal.quantity} for {proposal.asset_id}"
                )
                continue

            # ── price resolution: market price, then limit_price fallback ──
            fill_price = prices.get(proposal.asset_id)
            if (fill_price is None or fill_price <= 0) and proposal.limit_price and proposal.limit_price > 0:
                fill_price = proposal.limit_price
            if fill_price is None or fill_price <= 0:
                skipped.append(f"No valid fill price for {proposal.asset_id}")
                continue

            # ── pre-trade gates ──
            gates = [
                self._check_cash_sufficiency(proposal, fill_price, working_state),
                self._check_ucits(proposal, ucits_eligible),
                self._check_tradeability(proposal, tradeable_assets),
                self._check_concentration(proposal, fill_price, prices, working_state),
            ]

            failed = [g for g in gates if not g.passed]
            if failed:
                reason = "; ".join(f.reason or f"{f.gate} failed" for f in failed)
                skipped.append(f"Trade {proposal.asset_id} rejected: {reason}")
                continue

            key = self._next_idempotency_key()
            executed = ExecutedTrade(
                proposal=proposal,
                fill_price=fill_price,
                gate_results=gates,
                idempotency_key=key,
            )
            executed_trades.append(executed)
            working_state = working_state.apply_executed_trade(executed)

        # ── CONFIRM phase: replay successful trades on canonical state ──

        current_state = self._state
        for executed in executed_trades:
            current_state = current_state.apply_executed_trade(executed)

        batch = TradeBatch(
            trades=executed_trades,
            state_before_snapshot=self._state.model_dump(),
            state_after_snapshot=current_state.model_dump() if executed_trades else {},
        )
        return batch, skipped
