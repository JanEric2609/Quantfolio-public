"""German (DE) tax jurisdiction implementations.

All amounts in EUR. All outputs are estimates; broker statements remain the
source of truth.
"""

from .allowances import apply_sparer_pauschbetrag, soli_amount, kirchensteuer_amount
from .fifo import fifo_consume, FifoSale
from .teilfreistellung import teilfreistellung_pct_for_fund_class, apply_teilfreistellung
from .vorabpauschale import vorabpauschale_estimate
from .foreign_wht import cap_foreign_wht
from .buckets import classify_loss_bucket
from .gain_harvest import Holding, OpenLot, plan_gain_harvest, tax_free_room

__all__ = [
    "apply_sparer_pauschbetrag",
    "soli_amount",
    "kirchensteuer_amount",
    "fifo_consume",
    "FifoSale",
    "teilfreistellung_pct_for_fund_class",
    "apply_teilfreistellung",
    "vorabpauschale_estimate",
    "cap_foreign_wht",
    "classify_loss_bucket",
    "Holding",
    "OpenLot",
    "plan_gain_harvest",
    "tax_free_room",
]
