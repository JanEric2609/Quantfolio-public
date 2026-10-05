"""Canonical schema for the IBES analyst-consensus estimates panel.

Covers WRDS's IBES Summary Statistics file (``ibes.statsum_epsus``), the
table almost every "earnings surprise" / "analyst revision" factor is built
from -- a monthly, per-security, per-forecast-period consensus snapshot
(median/mean/std/count of analyst estimates), not individual analyst-level
detail.

**Point-in-time anchor.** ``statpers`` is the date IBES computed that
consensus snapshot, and is the column every point-in-time read must filter
on -- exactly like ``fundamentals_schema.available_from`` or
``security_identifiers_schema.as_of_date``. It is easy to confuse with
``fpedats`` (the fiscal period the *forecast* is about) or ``anndats_act``
(when the actual was announced); neither of those is "when this consensus
was knowable".

**The ticker trap.** ``ticker`` here is IBES's own internal identifier, not
an exchange ticker and not directly joinable to this app's ISIN- or
gvkey-keyed panels -- that requires a separate WRDS ICLINK (IBES ticker ->
CRSP permno) + CCM (permno -> gvkey) chain, which this schema does not
attempt to resolve. ``oftic`` (official/exchange ticker) and ``cusip`` are
carried through as-given for a caller to join on if they have that chain
available; treat ``ticker`` itself as an opaque IBES key only.

**Street vs. GAAP.** ``actual`` is IBES's own "street" (analyst-basis)
earnings figure, not Compustat's GAAP EPS -- the two differ by construction
(analysts exclude items they consider non-recurring) and must never be
mixed in one earnings-surprise calculation. This module stores IBES's own
actual only; reconciling it with :mod:`fundamentals_schema`'s ``eps`` is a
caller's job, not this loader's.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

IBES_ESTIMATES_COLUMNS: tuple[str, ...] = (
    "ticker",
    "cusip",
    "oftic",
    "statpers",
    "fpedats",
    "fpi",
    "measure",
    "numest",
    "medest",
    "meanest",
    "stdev",
    "highest",
    "lowest",
    "actual",
    "anndats_act",
    "curr",
    "usfirm",
    "source",
    "ingested_at",
)

# Estimates are nullable throughout: a security-period with no analyst
# coverage yet (numest == 0) legitimately carries no medest/meanest/stdev,
# and actual/anndats_act are null until the period's earnings are announced.
IBES_ESTIMATES_DTYPES: dict[str, str] = {
    "ticker": "string",
    "cusip": "string",
    "oftic": "string",
    "statpers": "datetime64[ns]",
    "fpedats": "datetime64[ns]",
    "fpi": "string",
    "measure": "string",
    "numest": "Int64",
    "medest": "float64",
    "meanest": "float64",
    "stdev": "float64",
    "highest": "float64",
    "lowest": "float64",
    "actual": "float64",
    "anndats_act": "datetime64[ns]",
    "curr": "string",
    "usfirm": "Int64",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

IBES_ESTIMATES_SOURCES: tuple[str, ...] = ("ibes_summary_epsus",)

# Forecast-period indicator: which fiscal period a row's estimate concerns.
# '1'/'2' (next/next-plus-one fiscal year) are what "earnings surprise" and
# "analyst revision" factor research almost always filters to; quarterly and
# long-term-growth rows are kept in the panel but need explicit filtering by
# a caller, exactly like fundamentals_loader refuses to pick indfmt for you.
IBES_FPI_VALUES: tuple[str, ...] = ("0", "1", "2", "4", "6", "Q1", "Q2", "Q3", "Q4", "S")

# The measure most factor research uses is EPS; REV/EBITDA/etc. are carried
# through for a caller that wants them, not filtered here.
IBES_MEASURE_VALUES: tuple[str, ...] = ("EPS", "REV", "EBITDA", "EBT", "CF")


class IbesEstimatesSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the IBES estimates schema."""


def validate_ibes_estimates_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Same fail-loud contract as the other data_engineering schemas. Only
    ``statpers`` is required non-null -- it is the point-in-time anchor this
    whole table exists to provide; ``actual``/``anndats_act`` are legitimately
    null before the fiscal period's earnings are announced.
    """
    missing = [c for c in IBES_ESTIMATES_COLUMNS if c not in df.columns]
    if missing:
        raise IbesEstimatesSchemaError(f"IBES estimates frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(IBES_ESTIMATES_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(IBES_ESTIMATES_SOURCES))
    if unknown:
        raise IbesEstimatesSchemaError(
            f"unknown source value(s): {unknown}; expected one of {IBES_ESTIMATES_SOURCES}"
        )

    unknown_fpi = sorted(set(out["fpi"].dropna().unique()) - set(IBES_FPI_VALUES))
    if unknown_fpi:
        raise IbesEstimatesSchemaError(f"unknown fpi value(s): {unknown_fpi}; expected one of {IBES_FPI_VALUES}")

    try:
        for col, dtype in IBES_ESTIMATES_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise IbesEstimatesSchemaError(f"IBES estimates frame failed dtype coercion: {exc}") from exc

    if bool(out["statpers"].isna().any()):
        raise IbesEstimatesSchemaError("one or more rows have a null statpers")

    return out
