"""Canonical schema for the point-in-time gvkey<->ISIN identifier crosswalk.

Sibling of ``fundamentals_schema`` and ``panel_schema``. Compustat Global's own
identifiers drift over time -- a company can be reissued a new ISIN, gain or
drop share classes, or carry unlisted securities with no ISIN at all -- so a
single static gvkey->ISIN table would silently misjoin historical price rows
to the wrong identifier. This schema instead keeps one row per
(``gvkey``, ``isin``) pair *as observed on a given day* (``as_of_date``), so a
consumer can ask "what ISIN(s) did this gvkey carry on date X" and get the
answer that was actually true then.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

SECURITY_IDENTIFIERS_COLUMNS: tuple[str, ...] = (
    "gvkey",
    "isin",
    "fic",
    "as_of_date",
    "source",
    "ingested_at",
)

# isin/fic are nullable: Compustat carries unlisted or delisted securities with
# a blank ISIN, and that blankness is itself information, not a reason to drop
# the row -- it still anchors the gvkey's identifier history on that date.
SECURITY_IDENTIFIERS_DTYPES: dict[str, str] = {
    "gvkey": "string",
    "isin": "string",
    "fic": "string",
    "as_of_date": "datetime64[ns]",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

SECURITY_IDENTIFIERS_SOURCES: tuple[str, ...] = ("compustat_global_security",)


class SecurityIdentifiersSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the security-identifiers schema."""


def validate_security_identifiers_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Same fail-loud contract as ``validate_fundamentals_frame``: a partial
    import that looks successful is worse than a refused one.
    """
    missing = [c for c in SECURITY_IDENTIFIERS_COLUMNS if c not in df.columns]
    if missing:
        raise SecurityIdentifiersSchemaError(f"security identifiers frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(SECURITY_IDENTIFIERS_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(SECURITY_IDENTIFIERS_SOURCES))
    if unknown:
        raise SecurityIdentifiersSchemaError(
            f"unknown source value(s): {unknown}; expected one of {SECURITY_IDENTIFIERS_SOURCES}"
        )

    try:
        for col, dtype in SECURITY_IDENTIFIERS_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise SecurityIdentifiersSchemaError(
            f"security identifiers frame failed dtype coercion: {exc}"
        ) from exc

    if bool(out["as_of_date"].isna().any()):
        raise SecurityIdentifiersSchemaError("one or more rows have a null as_of_date")

    return out
