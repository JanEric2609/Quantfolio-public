"""Canonical schema for the CRSP ticker-history (security names) PIT panel.

One row per (``permno``, name window): which ticker a CRSP security traded
under during ``[namedt, nameenddt]``. This is the only place a *ticker* enters
the WRDS side of the data spine -- every other panel is keyed by ``gvkey``,
``permno``, ISIN or CIK -- so it is what lets an app symbol like ``AAPL``
reach its WRDS factor characteristics (ticker -> permno here, permno ->
gvkey via the CCM link history). A null ``nameenddt`` means the name is
still current.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

CRSP_NAMES_COLUMNS: tuple[str, ...] = (
    "permno",
    "ticker",
    "trading_symbol",
    "namedt",
    "nameenddt",
    "source",
    "ingested_at",
)

CRSP_NAMES_DTYPES: dict[str, str] = {
    "permno": "Int64",
    "ticker": "string",
    "trading_symbol": "string",
    "namedt": "datetime64[ns]",
    "nameenddt": "datetime64[ns]",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

CRSP_NAMES_SOURCES: tuple[str, ...] = ("crsp_security_names",)


class CrspNamesSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the CRSP names schema."""


def validate_crsp_names_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Same fail-loud contract as ``validate_ccm_link_frame``: ``permno`` and
    ``namedt`` are required non-null; ``nameenddt`` null means still current.
    """
    missing = [c for c in CRSP_NAMES_COLUMNS if c not in df.columns]
    if missing:
        raise CrspNamesSchemaError(f"CRSP names frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(CRSP_NAMES_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(CRSP_NAMES_SOURCES))
    if unknown:
        raise CrspNamesSchemaError(f"unknown source value(s): {unknown}; expected one of {CRSP_NAMES_SOURCES}")

    try:
        for col, dtype in CRSP_NAMES_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise CrspNamesSchemaError(f"CRSP names frame failed dtype coercion: {exc}") from exc

    if bool(out["permno"].isna().any()):
        raise CrspNamesSchemaError("one or more rows have a null permno")
    if bool(out["namedt"].isna().any()):
        raise CrspNamesSchemaError("one or more rows have a null namedt")
    if bool((out["nameenddt"] < out["namedt"]).fillna(False).any()):
        raise CrspNamesSchemaError("one or more rows have nameenddt before namedt")

    return out
