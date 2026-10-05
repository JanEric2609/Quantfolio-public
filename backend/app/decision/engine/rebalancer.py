"""Drift-based Rebalancer for paper portfolio.

Computes current allocation weights, detects drift from target weights,
and generates trade proposals to close gaps.  Threshold-based — only
rebalances when drift exceeds the configured threshold.
"""

from __future__ import annotations

from app.decision.engine.state import PortfolioState
from app.decision.engine.types import (
    Holding,
    PortfolioConfig,
    TradeProposal,
)


class Rebalancer:
    """Generates trade proposals to correct portfolio drift.

    Usage:
        rebalancer = Rebalancer(state.config)
        trades, drift_report = rebalancer(
            state, target_weights, run_id="run_001",
        )
    """

    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_current_weights(self, state: PortfolioState) -> dict[str, float]:
        """Return ``{asset_id → weight}`` as fraction of NAV.

        Weights sum to ≤ 1.0 with the remainder implicitly in cash.
        Returns an empty dict when NAV is zero (empty portfolio).
        """
        nav = self._nav(state)
        if nav <= 0:
            return {}
        weights: dict[str, float] = {}
        for h in state.holdings.values():
            weights[h.asset_id] = (h.quantity * h.avg_price) / nav
        return weights

    def detect_drift(
        self,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
    ) -> dict[str, float]:
        """Return ``{asset_id → drift}`` for assets exceeding threshold.

        *drift = current_weight − target_weight* (positive = overweight).
        Only includes assets where ``|drift| > config.rebalance_threshold_pct``.
        Assets present in either dict are checked.
        """
        threshold = self.config.rebalance_threshold_pct
        drift: dict[str, float] = {}
        all_ids = set(current_weights) | set(target_weights)
        for asset_id in all_ids:
            cur = current_weights.get(asset_id, 0.0)
            tgt = target_weights.get(asset_id, 0.0)
            d = cur - tgt
            if abs(d) > threshold:
                drift[asset_id] = d
        return drift

    def generate_trades(
        self,
        state: PortfolioState,
        target_weights: dict[str, float],
        run_id: str,
        prices: dict[str, float] | None = None,
    ) -> list[TradeProposal]:
        """Generate trades to close drift gaps.

        * Sells overweight positions first to fund purchases.
        * Skips trades below ``config.min_trade_value``.
        * Skips buy proposals for new assets when the unit price is
          unavailable (no price-fetching inside this class).

        Args:
            prices: Optional market prices keyed by ``asset_id``. When
                provided, buy proposals use the current market price instead
                of the holding's average cost basis.
        """
        current = self.compute_current_weights(state)
        drift = self.detect_drift(current, target_weights)
        if not drift:
            return []

        nav = self._nav(state)
        proposals: list[TradeProposal] = []
        seq = 0

        # --- Sell over-weight positions first ---
        sell_proceeds = 0.0
        for asset_id in sorted(drift):
            d = drift[asset_id]
            if d <= 0:
                continue
            holding = self._find_holding(state, asset_id)
            if holding is None or holding.quantity <= 0:
                continue
            target_notional = target_weights.get(asset_id, 0.0) * nav
            price = prices.get(asset_id) if prices is not None else holding.avg_price
            if price is None or price <= 0:
                price = holding.avg_price
            current_notional = holding.quantity * price
            excess_notional = current_notional - target_notional
            if excess_notional < self.config.min_trade_value:
                continue
            sell_qty = excess_notional / price
            sell_qty = min(sell_qty, holding.quantity)
            if sell_qty * price < self.config.min_trade_value:
                continue
            seq += 1
            proposals.append(TradeProposal(
                asset_id=asset_id,
                symbol=asset_id,
                action="sell",
                quantity=round(sell_qty, 6),
                rationale=(
                    f"Rebalance: {asset_id} drifted "
                    f"{d * 100:.2f}% from target"
                ),
                agent_source="rebalancer",
                confidence=0.9,
                idempotency_key=f"{run_id}:{seq}",
            ))
            sell_proceeds += sell_qty * price

        # --- Buy under-weight positions ---
        available_cash = self._total_cash(state) + sell_proceeds
        for asset_id in sorted(drift):
            d = drift[asset_id]
            if d >= 0:
                continue
            target_notional = target_weights.get(asset_id, 0.0) * nav
            holding = self._find_holding(state, asset_id)
            current_notional = (
                current.get(asset_id, 0.0) * nav
            )
            deficit_notional = target_notional - current_notional
            if deficit_notional < self.config.min_trade_value:
                continue
            buy_notional = min(deficit_notional, available_cash)
            if buy_notional < self.config.min_trade_value:
                continue
            price = prices.get(asset_id) if prices is not None else None
            if price is None and holding is not None and holding.avg_price > 0:
                price = holding.avg_price
            if price is None or price <= 0:
                continue
            buy_qty = buy_notional / price
            seq += 1
            proposals.append(TradeProposal(
                asset_id=asset_id,
                symbol=asset_id,
                action="buy",
                quantity=round(buy_qty, 6),
                rationale=(
                    f"Rebalance: {asset_id} drifted "
                    f"{d * 100:.2f}% from target"
                ),
                agent_source="rebalancer",
                confidence=0.9,
                idempotency_key=f"{run_id}:{seq}",
            ))
            available_cash -= buy_notional

        return proposals

    def __call__(
        self,
        state: PortfolioState,
        target_weights: dict[str, float],
        run_id: str,
        prices: dict[str, float] | None = None,
    ) -> tuple[list[TradeProposal], dict[str, float]]:
        """Run the full rebalance in one call.

        Returns ``(trades, drift_report)`` where *drift_report* maps
        ``asset_id → drift_amount`` for every drifted asset.
        """
        current = self.compute_current_weights(state)
        drift = self.detect_drift(current, target_weights)
        trades = self.generate_trades(state, target_weights, run_id, prices=prices)
        return trades, drift

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _nav(state: PortfolioState) -> float:
        holdings_value = sum(
            h.quantity * h.avg_price for h in state.holdings.values()
        )
        # Only EUR cash — multi-currency assets are valued in their own
        # currency (treated as EUR for NAV). Full FX conversion is a
        # future enhancement.
        cash_value = sum(
            c.amount for c in state.cash.values() if c.currency == "EUR"
        )
        return holdings_value + cash_value

    @staticmethod
    def _total_cash(state: PortfolioState) -> float:
        # Only EUR cash (primary portfolio currency). Multi-currency
        # balances are not fungible without FX rates.
        return sum(
            c.amount for c in state.cash.values() if c.currency == "EUR"
        )

    @staticmethod
    def _find_holding(
        state: PortfolioState, asset_id: str
    ) -> Holding | None:
        for h in state.holdings.values():
            if h.asset_id == asset_id:
                return h
        return None
