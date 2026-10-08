"""The pre-registered satellite (report Phase 3, step 3), fixed before any result is seen.

The satellite is the 0-10 % stock-picking sleeve of the report's target
shape. It is reviewed once a quarter, and a holding is kept while its score
stays in the top tenth of the eligible stocks. It is sold only when the model
has clearly gone off it, never trimmed.

How many stocks it holds depends on the broker's order fee, so each broker is
its own pre-registered variant:

- DKB charges EUR 10 per order, so lumps of at least EUR 1,000: three stocks.
- Scalable Capital charges about EUR 1 per order, so the same money can hold
  ten stocks of about EUR 360 each.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.lab.pooled_model.spec import BASELINE, MODELS

__all__ = ["BASELINE", "MODELS"]

SATELLITE_EUR = 3_600.0


@dataclass(frozen=True)
class Broker:
    name: str
    positions: int
    order_fee_eur: float

    @property
    def position_eur(self) -> float:
        return SATELLITE_EUR / self.positions

    @property
    def commission_bps(self) -> float:
        return self.order_fee_eur / self.position_eur * 10_000.0


BROKERS = (
    # EUR 10 per order up to EUR 5,000 (decision/advisor/costs.py; not
    # importable from the lab layer, so restated here): about 83 bps each way.
    Broker("dkb", positions=3, order_fee_eur=10.0),
    # The per-order fee of Scalable's free plan on EIX: about 28 bps each
    # way. A flat-fee plan costs a fixed amount per month instead.
    Broker("scalable", positions=10, order_fee_eur=0.99),
)
# Half-spread paid per trade on a German venue (Xetra/Tradegate/gettex) for a
# mega cap, foreign ones included.
SPREAD_BPS = 10.0

# Stocks a German retail investor can buy with a tight spread.
ELIGIBLE_SIZE_GROUPS = ("mega",)
REVIEW_MONTHS = (3, 6, 9, 12)
HOLD_WHILE_TOP = 0.10

# The core it competes with: an MSCI World ETF, taxed only when sold.
CORE_TER = 0.0020
# Tax: 25 % Kapitalertragsteuer plus 5.5 % Soli on it, no church tax, and no
# Sparer-Pauschbetrag (the core's Vorabpauschale uses it). Single stocks get
# no Teilfreistellung; an equity ETF gets 30 % (Sec. 20 InvStG).
FUND_PARTIAL_EXEMPTION = 0.30
CORE_DEFERRAL_YEARS = 20

NW_LAGS = 6
RECENT_YEARS = 10
# The horizon for "chance the satellite beats the core".
HORIZON_YEARS = 10
# 25 % KESt plus 5.5 % Soli on it (factor_premia states the same rate).
STOCK_TAX_RATE = 0.26375
