"""Risk-based CFD size for the event desk (ADR 0028): the stop loses ``risk_per_trade`` EUR, costs included."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from .cfd_instruments import CfdInstrument
from .event_limits import EVENT_LIMITS

FX_FEE = Decimal("0.005")


@dataclass(frozen=True)
class Sizing:
    quantity: Decimal
    exposure_eur: Decimal
    margin_eur: Decimal
    leverage: Decimal
    loss_at_stop_eur: Decimal
    stop_level: Decimal
    overnight_eur_per_night: Decimal


def size(inst: CfdInstrument, ref_price: Decimal, stop_distance: Decimal, eur_per_unit: Decimal,
         now: datetime, direction: str = "long", open_margin_eur: Decimal = Decimal(0)) -> Sizing | None:
    """quantity = risk after costs / (stop distance x EUR per unit), rounded DOWN to the step.

    Costs per unit at the stop: the spread, the 0.5 % FX fee on the loss (USD) and one night of interest.
    ``None`` when the quantity rounds to 0 or the margin would break ``max_margin_share`` of the pot."""
    if ref_price <= 0 or stop_distance <= 0 or eur_per_unit <= 0:
        return None
    price_eur = ref_price * eur_per_unit
    stop_loss = stop_distance * eur_per_unit
    spread = inst.spread_bp / Decimal(10000) * price_eur
    fx = FX_FEE * stop_loss if inst.usd else Decimal(0)
    night = inst.overnight_rate(direction) * price_eur
    per_unit = stop_loss + spread + fx + night
    qty = (EVENT_LIMITS.risk_per_trade / per_unit).quantize(inst.quantity_step, rounding=ROUND_DOWN)
    if qty <= 0:
        return None
    exposure = qty * price_eur
    margin = exposure * inst.margin_rate
    if open_margin_eur + margin > EVENT_LIMITS.pot * EVENT_LIMITS.max_margin_share:
        return None
    stop = ref_price - stop_distance if direction == "long" else ref_price + stop_distance
    return Sizing(
        quantity=qty, exposure_eur=exposure.quantize(Decimal("0.01")), margin_eur=margin.quantize(Decimal("0.01")),
        leverage=(Decimal(1) / inst.margin_rate).quantize(Decimal("0.1")),
        loss_at_stop_eur=(qty * per_unit).quantize(Decimal("0.01")), stop_level=stop,
        overnight_eur_per_night=(qty * night).quantize(Decimal("0.0001")))
