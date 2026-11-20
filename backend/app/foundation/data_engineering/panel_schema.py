"""Canonical schema for the point-in-time (PIT) Parquet panel.

ADR 0015 Phase 3: a Parquet-file panel, independent of but coexisting with
the live ``bar_prices``/``PriceCache`` Postgres tables, that Phase 4's lab
will read from. Two producers write into it under this shared schema so rows
from both can be queried together without a join:

- ``panel_export.py`` -- ``source="internal"``, read from ``bar_prices``.
- ``datastream_loader.py`` -- ``source="datastream_pit"``, from a manually
  exported Datastream extract (not yet run -- pending the owner's Datastream
  access, see docs/adr/0015-honest-measurement-rebuild.md).
"""
from __future__ import annotations

from typing import cast

import pandas as pd

PANEL_COLUMNS: tuple[str, ...] = (
    "symbol",
    "isin",
    "as_of_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "currency",
    "source",
    "ingested_at",
)

# pandas dtypes for each column, enforced before every Parquet write so
# incremental writes never fragment a column's type across files.
PANEL_DTYPES: dict[str, str] = {
    "symbol": "string",
    "isin": "string",
    "as_of_date": "datetime64[ns]",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    # float, not int: NaN represents "volume unknown" (some Datastream series omit it).
    "volume": "float64",
    "currency": "string",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

KNOWN_SOURCES: tuple[str, ...] = ("internal", "datastream_pit")

# Which producer wins when both wrote the same (symbol, as_of_date).
#
# Both partitions are read together by ``panel_read.read_panel``, and any
# symbol the app already tracks in ``bar_prices`` will collide with the same
# name in a Datastream/Compustat extract. ``datastream_pit`` wins: it is the
# point-in-time research extract the panel exists to hold, whereas
# ``internal`` is a projection of the live price cache, which is survivorship-
# and revision-contaminated by construction. Order is highest precedence
# first; every value must appear in ``KNOWN_SOURCES``.
SOURCE_PRECEDENCE: tuple[str, ...] = ("datastream_pit", "internal")


class PanelSchemaError(ValueError):
    """Raised when a DataFrame doesn't conform to the PIT panel schema."""


def validate_panel_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Validate and coerce *df* to the canonical panel schema.

    Returns a new DataFrame with columns in canonical order and dtypes
    coerced. Raises ``PanelSchemaError`` on a missing column, an unknown
    ``source`` value, or a dtype that can't be coerced -- fails loud rather
    than silently importing a partial/malformed row (the ADR 0014 lesson:
    a bare except swallowing a schema break hid a months-long outage).
    """
    missing = [c for c in PANEL_COLUMNS if c not in df.columns]
    if missing:
        raise PanelSchemaError(f"panel frame missing required columns: {missing}")

    out = cast(pd.DataFrame, df[list(PANEL_COLUMNS)]).copy()

    bad_sources = set(out["source"].dropna().unique().tolist()) - set(KNOWN_SOURCES)
    if bad_sources:
        raise PanelSchemaError(f"unknown panel source(s): {sorted(bad_sources)}; expected one of {KNOWN_SOURCES}")

    try:
        # Normalize both tz-naive and tz-aware inputs to UTC-naive first: astype
        # to a naive datetime64 dtype raises on tz-aware input (bar_prices'
        # `ts` frequently comes back tz-aware from pd.to_datetime), so a blanket
        # astype loop below would fail loud on the common case, not just the
        # genuinely malformed one.
        for col in ("as_of_date", "ingested_at"):
            out[col] = pd.to_datetime(out[col], utc=True).dt.tz_localize(None)
        for col, dtype in PANEL_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise PanelSchemaError(f"panel frame failed dtype coercion: {exc}") from exc

    return out
