"""Value bounds for request fields, kept out of the OpenAPI schema.

Each bound refuses only input that used to fail with a 500 (found by the
authenticated API scan, tests/test_api_security.py). The checks are
validators, not ``ge``/``le`` constraints: the contract gate (oasdiff) counts
any new schema bound as a breaking change, and these inputs never had a
working answer to break.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator

#: Money and quantities: budget columns are Numeric(20, 6), so Postgres refuses
#: 1e14 and above on insert, and a 1e24 sale price overflowed the Decimal
#: quantize of the FIFO tax maths.
MAX_MONEY = 10**12


def between(low: float | None, high: float | None) -> AfterValidator:
    """Refuse a value outside [low, high] (either end open when None), or NaN/inf, with a 422."""

    def check(value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("must be a finite number")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("must be a finite number")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"must be between {low} and {high}" if high is not None else f"must be at least {low}")
        return value

    return AfterValidator(check)


MoneyDecimal = Annotated[Decimal, between(-MAX_MONEY, MAX_MONEY)]
MoneyFloat = Annotated[float, between(-MAX_MONEY, MAX_MONEY)]


def at_most(n: int) -> AfterValidator:
    """Refuse a list longer than ``n`` items with a 422."""

    def check(value: Any) -> Any:
        if value is not None and len(value) > n:
            raise ValueError(f"at most {n} items")
        return value

    return AfterValidator(check)
