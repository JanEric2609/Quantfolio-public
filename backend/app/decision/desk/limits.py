"""The desk's limits: code constants, never settings (ADR 0027).

Nothing in the UI or API can raise one. Changing a value means a commit plus
an ADR amendment, and ``tests/test_desk_limits.py`` pins every value. They are
also part of the pre-registered spec (``prereg.py``), so a change moves its hash.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Limits:
    # Money (EUR). Live; paper records what live would have done.
    pot: Decimal = Decimal("250")
    daily_loss: Decimal = Decimal("15")
    max_loss_per_trade: Decimal = Decimal("5")  # at the stop, both fees included
    fee_per_order: Decimal = Decimal("0.99")
    # Positions and timing.
    max_open_live: int = 1
    max_entries_per_day: int = 2
    max_holding_trading_days: int = 2
    cutoff_hour: int = 21  # Europe/Berlin, on the last trading day before a weekend or holiday
    cutoff_minute: int = 30
    window_start_hour: int = 8
    window_start_minute: int = 5
    window_end_hour: int = 21
    window_end_minute: int = 45  # US last half hour starts 21:30 Berlin; issuers quote until 22:00
    # Product filters.
    warrant_min_days_to_expiry: int = 21
    knockout_max_leverage: Decimal = Decimal("5")
    knockout_min_barrier_sigmas: Decimal = Decimal("3")
    warrant_min_abs_delta: Decimal = Decimal("0.3")
    warrant_max_abs_delta: Decimal = Decimal("0.7")
    spread_cap_liquid: Decimal = Decimal("0.01")  # fraction of mid
    spread_cap_other: Decimal = Decimal("0.025")
    # Quotes and taps.
    max_quote_age_s: int = 60
    tap_window_s: int = 180
    reask_move: Decimal = Decimal("0.01")
    quote_lost_s: int = 600  # no fresh quote for 10 minutes -> close at the last bid
    # Stop construction (sizing.py).
    stop_min_fraction: Decimal = Decimal("0.03")
    stop_max_fraction: Decimal = Decimal("0.30")
    stop_sigmas: Decimal = Decimal("1")

    def as_json(self) -> dict[str, str | int]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


LIMITS = Limits()
