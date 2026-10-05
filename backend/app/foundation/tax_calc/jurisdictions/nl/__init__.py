"""Dutch (NL) tax jurisdiction implementation.

Provides NL Box 3 (wealth tax / vermogensrendementsheffing) calculations and support
for dual residency scenarios during moves to/from the Netherlands.

All amounts in EUR. All outputs are estimates; official tax returns remain the source of truth.
"""

from .allowances import get_heffingvrij_vermogen, prorate_heffingvrij_vermogen
from .box3 import (
    Box3Calculation,
    Box3WealthBucket,
    calculate_box3,
    classify_wealth_by_bucket,
    convert_foreign_holding_to_eur,
    prorate_box3_dual_residency,
)
from .fictitious_return import (
    calculate_box3_tax,
    calculate_box3_taxable_wealth,
    get_bucket_rates,
    get_fictitious_return_rate,
)

__all__ = [
    "Box3Calculation",
    "Box3WealthBucket",
    "calculate_box3",
    "calculate_box3_tax",
    "calculate_box3_taxable_wealth",
    "classify_wealth_by_bucket",
    "convert_foreign_holding_to_eur",
    "get_bucket_rates",
    "get_fictitious_return_rate",
    "get_heffingvrij_vermogen",
    "prorate_box3_dual_residency",
    "prorate_heffingvrij_vermogen",
]
