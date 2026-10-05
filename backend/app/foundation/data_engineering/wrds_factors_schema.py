"""Canonical schema for WRDS-sourced factor data -- two distinct shapes.

**Open question, deliberately left open.** Unlike the other data_engineering
tables, "WRDS Factors" is not one settled product name -- depending on the
subscription it can mean either of two structurally incompatible things
(see the research this schema was built from,
``wrds_research_factors.md``):

1. **Portfolio-level factor RETURNS** -- WRDS's redistribution of the
   Fama-French time series (one row per date, columns ``mkt_rf``/``smb``/
   ``hml``/``rmw``/``cma``/``rf``, values in PERCENT). This app already
   fetches this exact series live from Ken French's own site in
   ``quant_factors.py`` (cached to ``QUANT_FACTORS_FF_CACHE_DIR``), so
   ingesting it again here would just be a second copy of data already
   covered -- confirm against the actual WRDS product before treating this
   candidate as the reason to use this loader at all.
2. **Firm-level factor CHARACTERISTICS** -- a JKP-style ("Is There a
   Replication Crisis in Finance?", Jensen/Kelly/Pedersen 2023) firm-month
   panel of dozens of characteristics (``mom_12_1``, ``be_me``, ``gp_at``,
   ...) keyed by ``gvkey``/``permno``/``eom``, values in DECIMAL. This is
   the more likely reason to actually want a *new* WRDS ingestion path here,
   since nothing else in the app provides firm-level characteristics at this
   breadth.

Both schemas are kept side by side rather than guessing one. Once real WRDS
data is in hand, confirm which product it actually is (check the WRDS
product/table name) and use the matching ``validate_*`` function; a caller
uncertain which shape an extract is can call
:func:`detect_wrds_factor_shape` first.
"""
from __future__ import annotations

from typing import Literal, cast

import pandas as pd

# ---------------------------------------------------------------------------
# Candidate 1: portfolio-level factor return time series (Fama-French shape)
# ---------------------------------------------------------------------------

FACTOR_RETURNS_COLUMNS: tuple[str, ...] = (
    "date",
    "mkt_rf",
    "smb",
    "hml",
    "rmw",
    "cma",
    "umd",
    "rf",
    "source",
    "ingested_at",
)

# Percent, not decimal -- Ken French's own convention (1.23 means 1.23%).
# Carried through unconverted, exactly like security_identifiers_loader
# stores currency as quoted: converting units is a caller's job, done once,
# not silently baked into the loader.
FACTOR_RETURNS_DTYPES: dict[str, str] = {
    "date": "datetime64[ns]",
    "mkt_rf": "float64",
    "smb": "float64",
    "hml": "float64",
    "rmw": "float64",
    "cma": "float64",
    "umd": "float64",
    "rf": "float64",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

FACTOR_RETURNS_SOURCES: tuple[str, ...] = ("wrds_factor_returns",)


class FactorReturnsSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the factor-returns schema."""


def validate_factor_returns_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Only ``date`` and ``mkt_rf`` are required non-null -- ``rmw``/``cma``/
    ``umd`` are legitimately absent from a 3-factor-only extract.
    """
    missing = [c for c in FACTOR_RETURNS_COLUMNS if c not in df.columns]
    if missing:
        raise FactorReturnsSchemaError(f"factor returns frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(FACTOR_RETURNS_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(FACTOR_RETURNS_SOURCES))
    if unknown:
        raise FactorReturnsSchemaError(
            f"unknown source value(s): {unknown}; expected one of {FACTOR_RETURNS_SOURCES}"
        )

    try:
        for col, dtype in FACTOR_RETURNS_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise FactorReturnsSchemaError(f"factor returns frame failed dtype coercion: {exc}") from exc

    if bool(out["date"].isna().any()):
        raise FactorReturnsSchemaError("one or more rows have a null date")
    if bool(out["mkt_rf"].isna().any()):
        raise FactorReturnsSchemaError("one or more rows have a null mkt_rf")

    return out


# ---------------------------------------------------------------------------
# Candidate 2: firm-level factor characteristics panel (JKP shape)
# ---------------------------------------------------------------------------

# The characteristics this app loads from JKP's ~153, grouped by JKP's own 13
# themes (bkelly-lab/ReplicationCrisis, ``GlobalFactors/Cluster Labels.csv``).
# Two to five per theme, chosen for coverage outside the US and for being the
# theme's best-known member, so a cross-sectional model sees every theme
# without an extract of all 153 columns (about 4x the size). Names are JKP's
# own, except ``mom_12_1``, which JKP calls ``ret_12_1`` (see the loader's
# alias map). Adding a name here is additive: older partitions without it
# read back as null.
JKP_CHARACTERISTICS_BY_THEME: dict[str, tuple[str, ...]] = {
    "accruals": ("oaccruals_at", "taccruals_at"),
    "debt_issuance": ("noa_at", "debt_gr3"),
    "investment": ("at_gr1", "sale_gr1", "capx_gr1", "noa_gr1a", "ppeinv_gr1a"),
    "low_leverage": ("at_be", "cash_at", "netdebt_me"),
    "low_risk": ("beta_60m", "betabab_1260d", "ivol_capm_252d", "rvol_21d"),
    "momentum": ("mom_12_1", "ret_6_1", "resff3_12_1", "prc_highprc_252d"),
    "profit_growth": ("niq_su", "saleq_su", "niq_at_chg1"),
    "profitability": ("ope_be", "ni_be", "ocf_at"),
    "quality": ("gp_at", "op_at", "cop_at", "qmj"),
    "seasonality": ("seas_2_5an", "dbnetis_at"),
    "short_term_reversal": ("ret_1_0", "rmax5_rvol_21d"),
    "size": ("dolvol_126d", "ami_126d"),
    "value": ("be_me", "bev_mev", "ni_me", "ocf_me", "eqnpo_me"),
}
JKP_CHARACTERISTICS: tuple[str, ...] = tuple(
    c for names in JKP_CHARACTERISTICS_BY_THEME.values() for c in names
)
# The original four, kept first in column order.
_CORE_CHARACTERISTICS: tuple[str, ...] = ("mom_12_1", "be_me", "gp_at", "at_gr1")

FACTOR_CHARACTERISTICS_COLUMNS: tuple[str, ...] = (
    "gvkey",
    "permno",
    "eom",
    "excntry",
    "size_grp",
    "me",
    *_CORE_CHARACTERISTICS,
    "ret_exc_lead1m",
    *(c for c in JKP_CHARACTERISTICS if c not in _CORE_CHARACTERISTICS),
    # GICS industry code, for industry-relative ranks.
    "gics",
    "source",
    "ingested_at",
)

# Decimal, not percent -- JKP's own convention (0.025 means 2.5%). The
# opposite convention from candidate 1 above; this mismatch is the single
# most-cited gotcha across every source consulted for this schema.
FACTOR_CHARACTERISTICS_DTYPES: dict[str, str] = {
    "gvkey": "string",
    "permno": "Int64",
    "eom": "datetime64[ns]",
    "excntry": "string",
    "size_grp": "string",
    "me": "float64",
    **{c: "float64" for c in JKP_CHARACTERISTICS},
    "ret_exc_lead1m": "float64",
    "gics": "Int64",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

FACTOR_CHARACTERISTICS_SOURCES: tuple[str, ...] = ("wrds_factor_characteristics",)


class FactorCharacteristicsSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the factor-characteristics schema."""


def validate_factor_characteristics_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Only ``gvkey`` and ``eom`` are required non-null -- every characteristic
    column is legitimately absent for a firm-month JKP has not computed it
    for (e.g. insufficient return history for a momentum window).
    """
    missing = [c for c in FACTOR_CHARACTERISTICS_COLUMNS if c not in df.columns]
    if missing:
        raise FactorCharacteristicsSchemaError(f"factor characteristics frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(FACTOR_CHARACTERISTICS_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(FACTOR_CHARACTERISTICS_SOURCES))
    if unknown:
        raise FactorCharacteristicsSchemaError(
            f"unknown source value(s): {unknown}; expected one of {FACTOR_CHARACTERISTICS_SOURCES}"
        )

    try:
        for col, dtype in FACTOR_CHARACTERISTICS_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise FactorCharacteristicsSchemaError(
            f"factor characteristics frame failed dtype coercion: {exc}"
        ) from exc

    if bool(out["gvkey"].isna().any()):
        raise FactorCharacteristicsSchemaError("one or more rows have a null gvkey")
    if bool(out["eom"].isna().any()):
        raise FactorCharacteristicsSchemaError("one or more rows have a null eom")

    return out


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------

WrdsFactorShape = Literal["factor_returns", "factor_characteristics"]


def detect_wrds_factor_shape(columns: pd.Index | list[str]) -> WrdsFactorShape | None:
    """Guess which of the two candidate shapes a raw extract's header matches.

    Returns ``None`` when neither shape's identifying columns are present,
    so a caller can fail loud instead of silently guessing wrong. A
    portfolio return file is identified by having a date column plus at
    least one of the factor-return columns and no firm identifier; a
    characteristics panel is identified by carrying a firm identifier
    (``gvkey`` or ``permno``) alongside a date.
    """
    lowered = {str(c).strip().lower() for c in columns}
    has_firm_id = bool({"gvkey", "permno"} & lowered)
    has_date = bool({"date", "eom"} & lowered)
    has_factor_return = bool({"mkt_rf", "mktrf", "smb", "hml"} & lowered)

    if has_firm_id and has_date:
        return "factor_characteristics"
    if has_date and has_factor_return and not has_firm_id:
        return "factor_returns"
    return None
