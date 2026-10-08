"""Order and savings-plan fees of the two brokers the owner uses.

Price lists, checked 2026-10-06 (de.scalable.capital/trading-gebuehren,
Preis- und Leistungsverzeichnis from 2026-09-01; dkb.de/privatkunden/broker):

* Scalable Capital, FREE Broker: 0.99 EUR per order on its own venue EIX
  (European Investor Exchange); *buys* of at least 250 EUR in ETFs of the
  "Prime" partners (iShares, Vanguard, Xtrackers, Amundi) are free there,
  sells always pay. Since 2026-09-01 gettex and Xetra cost a flat 1.99 EUR
  per order. Every fee here assumes EIX, the cheapest venue that lists the
  owner's instruments. Savings plans are free.
* DKB: an order costs 10 EUR up to 5,000 EUR, 15 EUR up to 20,000 EUR and
  30 EUR above (Tradegate/gettex/Quotrix); a savings plan 1.50 EUR per
  execution.

There is no issuer field anywhere in the data, so a Prime ETF is recognised
by the fund name every broker and price source carries ("iShares Core MSCI
World", "Vanguard FTSE All-World", ...).
"""
from __future__ import annotations

import re

SCALABLE_ORDER_FEE_EUR = 0.99
SCALABLE_PRIME_MIN_ORDER_EUR = 250.0
SCALABLE_OTHER_VENUE_FEE_EUR = 1.99  # gettex / Xetra since 2026-09-01; not used, EIX is assumed
SAVINGS_PLAN_FEE_EUR = {"dkb": 1.5, "scalable": 0.0}
DKB_ORDER_TIERS_EUR: tuple[tuple[float, float], ...] = ((5_000.0, 10.0), (20_000.0, 15.0))
DKB_ORDER_ABOVE_EUR = 30.0

_PRIME_ISSUER = re.compile(r"\b(ishares|ishsiii?|vanguard|xtrackers|amundi)\b", re.IGNORECASE)


def is_prime_etf(name: str | None) -> bool:
    """True for an ETF of one of Scalable's Prime partners, judged by its name."""
    return bool(name) and _PRIME_ISSUER.search(str(name)) is not None


def scalable_order_fee(amount: float, name: str | None = None, side: str = "buy") -> float:
    """One Scalable order on EIX: a buy of a Prime ETF from 250 EUR is free, everything else 0.99 EUR."""
    if amount <= 0:
        return 0.0
    if side == "buy" and amount >= SCALABLE_PRIME_MIN_ORDER_EUR and is_prime_etf(name):
        return 0.0
    return SCALABLE_ORDER_FEE_EUR


def dkb_order_fee(amount: float) -> float:
    """One DKB order on its default venue."""
    if amount <= 0:
        return 0.0
    for upper, fee in DKB_ORDER_TIERS_EUR:
        if amount <= upper:
            return fee
    return DKB_ORDER_ABOVE_EUR


def order_fee_eur(broker: str, amount: float, name: str | None = None, side: str = "buy") -> float:
    """One order's fee at *broker* (``"scalable"`` or ``"dkb"``); *side* is ``"buy"`` or ``"sell"``."""
    return scalable_order_fee(amount, name, side) if broker == "scalable" else dkb_order_fee(amount)
