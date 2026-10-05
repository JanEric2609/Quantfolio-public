"""Tax calculation context (bounded): jurisdiction tax estimators.

Pure calculation functions for multiple jurisdictions (DE, NL, etc.); the
German (DE) implementations are the main exports, maintained via
backward-compatibility shims over ``jurisdictions.de``. All amounts in EUR.
All outputs are estimates; broker statements remain the source of truth.
See ``docs/tax-cockpit.md`` for scope and limits.

This ``__init__`` is the context facade: it re-exports the callables and
constants other contexts consume today. Import the package root, not
internal submodules.
"""

from .allowances import (
    KEST_RATE,
    SPARER_PAUSCHBETRAG_SINGLE,
    SPARER_PAUSCHBETRAG_SPOUSE,
    apply_sparer_pauschbetrag,
    kirchensteuer_amount,
    reduced_kest_amount,
    soli_amount,
)
from .buckets import classify_loss_bucket
from .fifo import FifoSale, fifo_consume
from .foreign_wht import cap_foreign_wht
from .jurisdictions.de.gain_harvest import (
    FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR,
    FUTURE_TAX_RATE,
    GRUNDFREIBETRAG_BY_YEAR,
    HarvestStep,
    Holding,
    OpenLot,
    Room,
    plan_gain_harvest,
    tax_free_room,
)
from .jurisdictions.nl import calculate_box3, classify_wealth_by_bucket
from .teilfreistellung import apply_teilfreistellung, teilfreistellung_pct_for_fund_class
from .vorabpauschale import BASISZINS_BY_YEAR, vorabpauschale_estimate

__all__ = [
    "BASISZINS_BY_YEAR",
    "FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR",
    "FUTURE_TAX_RATE",
    "GRUNDFREIBETRAG_BY_YEAR",
    "HarvestStep",
    "Holding",
    "OpenLot",
    "Room",
    "FifoSale",
    "KEST_RATE",
    "SPARER_PAUSCHBETRAG_SINGLE",
    "SPARER_PAUSCHBETRAG_SPOUSE",
    "apply_sparer_pauschbetrag",
    "apply_teilfreistellung",
    "calculate_box3",
    "cap_foreign_wht",
    "classify_loss_bucket",
    "classify_wealth_by_bucket",
    "fifo_consume",
    "kirchensteuer_amount",
    "plan_gain_harvest",
    "reduced_kest_amount",
    "soli_amount",
    "tax_free_room",
    "teilfreistellung_pct_for_fund_class",
    "vorabpauschale_estimate",
]
