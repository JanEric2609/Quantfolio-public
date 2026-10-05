"""PortfolioState — immutable portfolio state management.

All state transitions return NEW objects (functional immutability).
Zero FastAPI / DB / SQLAlchemy dependencies.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.decision.engine.types import (
    Cash,
    ExecutedTrade,
    Holding,
    PortfolioConfig,
    PortfolioStateSnapshot,
    TradeBatch,
)


class PortfolioState(BaseModel):
    """Immutable representation of a portfolio's current state.

    Holds positions (asset_id → Holding), cash balances (currency → Cash),
    and a PortfolioConfig. Every mutation-like method returns a new instance.
    """

    model_config = ConfigDict(frozen=False)  # mutable for in-place build, transitions return new

    holdings: dict[str, Holding] = Field(default_factory=dict)
    cash: dict[str, Cash] = Field(default_factory=lambda: {"EUR": Cash(currency="EUR", amount=0.0)})
    config: PortfolioConfig = Field(default_factory=PortfolioConfig)

    # ------------------------------------------------------------------
    # factory
    # ------------------------------------------------------------------

    @classmethod
    def from_seed(
        cls,
        positions: list[dict],
        cash: float = 0.0,
        config: PortfolioConfig | None = None,
    ) -> PortfolioState:
        """Create a PortfolioState from a list of position dicts and a cash balance.

        Each position dict must have at minimum:
          asset_id, symbol, quantity, avg_price
        Optional: currency (defaults to "EUR").
        """
        holdings: dict[str, Holding] = {}
        for p in positions:
            holdings[p["asset_id"]] = Holding(
                asset_id=p["asset_id"],
                symbol=p["symbol"],
                quantity=p["quantity"],
                avg_price=p["avg_price"],
                currency=p.get("currency", "EUR"),
            )
        return cls(
            holdings=holdings,
            cash={"EUR": Cash(currency="EUR", amount=cash)},
            config=config or PortfolioConfig(),
        )

    # ------------------------------------------------------------------
    # value computations
    # ------------------------------------------------------------------

    def nav(
        self,
        currency: str = "EUR",
        prices: dict[str, float] | None = None,
    ) -> float:
        """Total portfolio value in *currency*.

        Uses provided market prices or falls back to avg_price per holding.
        """
        total = 0.0
        if currency in self.cash:
            total += self.cash[currency].amount
        for asset_id, holding in self.holdings.items():
            price = prices.get(asset_id, holding.avg_price) if prices else holding.avg_price
            total += holding.quantity * price
        return total

    def position_weight(self, asset_id: str, prices: dict[str, float] | None = None) -> float:
        """Weight of a single position as a fraction of total NAV."""
        if asset_id not in self.holdings:
            return 0.0
        total_nav = self.nav(prices=prices)
        if total_nav == 0.0:
            return 0.0
        holding = self.holdings[asset_id]
        price = prices.get(asset_id, holding.avg_price) if prices else holding.avg_price
        return (holding.quantity * price) / total_nav

    # ------------------------------------------------------------------
    # state transitions (all return new PortfolioState)
    # ------------------------------------------------------------------

    def apply_executed_trade(self, trade: ExecutedTrade) -> PortfolioState:
        """Return a NEW PortfolioState after executing a single trade."""
        prop = trade.proposal
        aid = prop.asset_id

        new_holdings = dict(self.holdings)
        new_cash: dict[str, Cash] = {k: v.model_copy() for k, v in self.cash.items()}

        if prop.action == "buy":
            cost = prop.quantity * trade.fill_price

            if aid in new_holdings:
                h = new_holdings[aid]
                total_qty = h.quantity + prop.quantity
                total_cost = (h.quantity * h.avg_price) + cost
                new_holdings[aid] = Holding(
                    asset_id=h.asset_id,
                    symbol=h.symbol,
                    quantity=total_qty,
                    avg_price=total_cost / total_qty,
                    currency=h.currency,
                )
                currency = h.currency
            else:
                currency = prop.currency
                new_holdings[aid] = Holding(
                    asset_id=aid,
                    symbol=prop.symbol,
                    quantity=prop.quantity,
                    avg_price=trade.fill_price,
                    currency=currency,
                )

            if currency in new_cash:
                existing = new_cash[currency]
                new_cash[currency] = Cash(currency=currency, amount=existing.amount - cost)
            else:
                new_cash[currency] = Cash(currency=currency, amount=-cost)
        else:  # sell
            if aid not in new_holdings:
                raise ValueError(f"Cannot sell {aid}: not in holdings")
            h = new_holdings[aid]
            if prop.quantity > h.quantity:
                raise ValueError(
                    f"Cannot sell {aid}: quantity {prop.quantity} exceeds "
                    f"holding {h.quantity}"
                )
            remaining = h.quantity - prop.quantity
            if remaining <= 0.0:
                del new_holdings[aid]
            else:
                new_holdings[aid] = Holding(
                    asset_id=h.asset_id,
                    symbol=h.symbol,
                    quantity=remaining,
                    avg_price=h.avg_price,
                    currency=h.currency,
                )
            proceeds = prop.quantity * trade.fill_price
            currency = h.currency
            if currency in new_cash:
                existing = new_cash[currency]
                new_cash[currency] = Cash(currency=currency, amount=existing.amount + proceeds)
            else:
                new_cash[currency] = Cash(currency=currency, amount=proceeds)

        return PortfolioState(holdings=new_holdings, cash=new_cash, config=self.config)

    def apply_trade_batch(self, batch: TradeBatch) -> PortfolioState:
        """Return a NEW PortfolioState after executing all trades in a batch."""
        state = self
        for trade in batch.trades:
            state = state.apply_executed_trade(trade)
        return state

    def snapshot(self) -> PortfolioStateSnapshot:
        """Create an immutable point-in-time snapshot of the current state."""
        return PortfolioStateSnapshot(
            timestamp=datetime.now(timezone.utc),
            holdings=deepcopy(self.holdings),
            cash=deepcopy(self.cash),
            config=self.config,
            nav_total=self.nav(),
        )

    def __repr__(self) -> str:
        return (
            f"PortfolioState(holdings={len(self.holdings)}, "
            f"cash={list(self.cash)}, nav={self.nav():.2f})"
        )
