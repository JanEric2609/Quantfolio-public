"""The event desk's limits: code constants, never settings (ADR 0028). Pinned by tests; part of the registration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EventLimits:
    pot: Decimal = Decimal("280")  # the CFD account, EUR
    risk_per_trade: Decimal = Decimal("5")  # EUR lost at the stop, costs included
    daily_real_loss_stop: Decimal = Decimal("15")
    monthly_real_loss_stop: Decimal = Decimal("60")
    max_open: int = 3  # event-desk paper positions open at once
    max_new_per_day: int = 4
    max_margin_share: Decimal = Decimal("0.5")  # margin of all open positions <= 50 % of the pot
    max_hold_trading_days: int = 10
    min_minutes_between_same_instrument: int = 24 * 60

    def as_json(self) -> dict[str, str | int]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


EVENT_LIMITS = EventLimits()
