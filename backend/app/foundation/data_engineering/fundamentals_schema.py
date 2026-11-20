"""Canonical schema for the point-in-time fundamentals panel.

Sibling of :mod:`panel_schema`, which covers daily OHLCV bars. Fundamentals
need their own shape for one structural reason: a price row is *knowable* on
its ``as_of_date``, and an accounting row is not. A fiscal year ending
2024-12-31 is not public knowledge until the annual report is filed months
later, so every row here carries two dates -- ``datadate`` (when the period
ended) and ``available_from`` (the earliest date a backtest may look at it).

Anything ranking or scoring companies must filter on ``available_from``, never
on ``datadate``. See ``fundamentals_loader`` for how the second date is
derived and why the default lag is what it is.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

FUNDAMENTALS_COLUMNS: tuple[str, ...] = (
    "gvkey",
    "datadate",
    "available_from",
    "fyear",
    "currency",
    "total_assets",
    "common_equity",
    "net_income",
    "revenue",
    "shares_outstanding",
    "eps",
    "long_term_debt",
    "source",
    "ingested_at",
)

# Every financial figure is nullable: Compustat leaves a field blank when the
# filing does not report it, and a blank is information ("not disclosed"),
# not a reason to reject the row. Hence float64 throughout rather than int.
FUNDAMENTALS_DTYPES: dict[str, str] = {
    "gvkey": "string",
    "datadate": "datetime64[ns]",
    "available_from": "datetime64[ns]",
    "fyear": "float64",
    "currency": "string",
    "total_assets": "float64",
    "common_equity": "float64",
    "net_income": "float64",
    "revenue": "float64",
    "shares_outstanding": "float64",
    "eps": "float64",
    "long_term_debt": "float64",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

# Mirrors panel_schema.SOURCE_PRECEDENCE: a vendor extract outranks anything
# derived, so a later provider-sourced snapshot can never overwrite a
# point-in-time Compustat row.
FUNDAMENTALS_SOURCES: tuple[str, ...] = ("compustat_global_funda",)


class FundamentalsSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the fundamentals schema."""


def validate_fundamentals_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Fails loud on a missing column, an unknown ``source``, or a dtype that
    cannot be coerced -- the same contract ``validate_panel_frame`` offers,
    for the same ADR 0014 reason: a partial import that looks successful is
    worse than a refused one.
    """
    missing = [c for c in FUNDAMENTALS_COLUMNS if c not in df.columns]
    if missing:
        raise FundamentalsSchemaError(f"fundamentals frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(FUNDAMENTALS_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(FUNDAMENTALS_SOURCES))
    if unknown:
        raise FundamentalsSchemaError(f"unknown source value(s): {unknown}; expected one of {FUNDAMENTALS_SOURCES}")

    try:
        for col, dtype in FUNDAMENTALS_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise FundamentalsSchemaError(f"fundamentals frame failed dtype coercion: {exc}") from exc

    # available_from is the whole point of this schema; a row that lost it to
    # an unparseable datadate would silently become invisible to every
    # correctly-written consumer instead of failing here.
    if bool(out["available_from"].isna().any()):
        raise FundamentalsSchemaError("one or more rows have a null available_from")

    return out
