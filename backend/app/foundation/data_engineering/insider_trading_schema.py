"""Canonical schema for the insider-trading (non-derivative transactions) panel.

Covers non-derivative stock transactions from SEC Forms 3/4/5, standardised --
equivalent in content to Table 1 of the WRDS Insiders Filing Data feed. This
is the source for the classic "insider trading signal" literature
(Cohen/Malloy/Pomorski and descendants).

Two source pipelines feed this same schema: SEC's own free bulk quarterly
"Insider Transactions" data sets (``sec_edgar_form4_nonderivative`` --
:mod:`insider_trading_loader`'s primary path, since not every WRDS
subscription includes the Insiders add-on) and a WRDS Insiders Table 1
export (``wrds_insiders_table1``), kept as an allowed source in case that
access becomes available later. Both normalise to the identical column set
below, so callers never need to know which pipeline populated a given row.

**Point-in-time anchor is ``filing_date``, never ``transaction_date``.** An
insider executes a trade on ``transaction_date`` but SEC rules only require
the Form 4 to be filed within 2 business days after -- a market participant
could not have known about the trade until it was filed. Using
``transaction_date`` as a signal date is look-ahead bias, empirically
inflating abnormal returns by 50-200bp (see the research this schema was
built from). Every point-in-time read in :mod:`insider_trading_loader`
therefore filters on ``filing_date``, mirroring
``fundamentals_schema.available_from``.

**Not every transaction code is a real signal.** The SEC defines 20
transaction codes; only ``P`` (open-market purchase) and ``S`` (open-market
sale) reflect the insider's own discretionary decision. Everything else --
option exercises, RSU vesting, tax withholding, gifts, inheritance -- is
company-driven or tax-automatic and, per Cohen/Malloy/Pomorski, roughly half
of even P/S trades are themselves "routine" (10b5-1 plans) and carry no
predictive power. This schema stores every code unfiltered (a caller
inspecting raw filings is legitimate); :data:`OPEN_MARKET_TRANSACTION_CODES`
is exported so any future signal-construction code applies the standard
filter explicitly rather than baking an opinion into the loader.

**``plan_10b5_1``** is the Form 4 checkbox SEC added in April 2023 (XML
``<aff10b5One>``, the quarterly sets' ``SUBMISSION.AFF10B5ONE`` since SEC's
July 2025 update): a transaction in the filing was made under a Rule
10b5-1(c) trading plan. It is one flag per filing, so every transaction of
a ticked filing carries it; the structured data does not say which line
the plan covered. Null where the filing predates the checkbox or the
source does not carry it (WRDS, partitions written before 2026-09-30).
"""
from __future__ import annotations

from typing import cast

import pandas as pd

INSIDER_TRADING_COLUMNS: tuple[str, ...] = (
    "cik",
    "issuer_cik",
    "company_name",
    "ticker",
    "cusip",
    "insider_name",
    "insider_role",
    "transaction_date",
    "filing_date",
    "transaction_code",
    "acquired_disposed",
    "shares",
    "price_per_share",
    "shares_held_after",
    "ownership_type",
    "cleanse_code",
    "form_type",
    "source",
    "ingested_at",
    "plan_10b5_1",
)

INSIDER_TRADING_DTYPES: dict[str, str] = {
    "cik": "string",
    "issuer_cik": "string",
    "company_name": "string",
    "ticker": "string",
    "cusip": "string",
    "insider_name": "string",
    "insider_role": "string",
    "transaction_date": "datetime64[ns]",
    "filing_date": "datetime64[ns]",
    "transaction_code": "string",
    "acquired_disposed": "string",
    "shares": "float64",
    "price_per_share": "float64",
    "shares_held_after": "float64",
    "ownership_type": "string",
    "cleanse_code": "string",
    "form_type": "string",
    "source": "string",
    "ingested_at": "datetime64[ns]",
    "plan_10b5_1": "boolean",
}

INSIDER_TRADING_SOURCES: tuple[str, ...] = ("wrds_insiders_table1", "sec_edgar_form4_nonderivative")

# The complete SEC Section 16 transaction-code enumeration (Form 4/5
# instructions). Kept as one allow-list covering every code actually seen in
# the wild, not just the P/S subset a signal cares about -- see
# OPEN_MARKET_TRANSACTION_CODES below for that narrower, opt-in filter.
TRANSACTION_CODE_VALUES: tuple[str, ...] = (
    "P", "S", "V",  # open market + voluntary-early-report flag
    "A", "D", "F", "I", "M",  # Rule 16b-3 exempt / compensation-driven
    "C", "E", "H", "O", "X",  # derivative transactions (mainly Table 2)
    "G", "L", "W", "Z",  # exempt / small / inheritance / rare
    "J", "K", "U",  # catch-all / equity swap / tender in M&A
)

# The only two codes reflecting the insider's own open-market decision.
# Every source consulted (Cohen/Malloy/Pomorski and descendants) treats
# everything else as non-signal-bearing; even P/S needs further "routine vs.
# opportunistic" filtering the raw table cannot provide on its own.
OPEN_MARKET_TRANSACTION_CODES: tuple[str, ...] = ("P", "S")

ACQUIRED_DISPOSED_VALUES: tuple[str, ...] = ("A", "D")
OWNERSHIP_TYPE_VALUES: tuple[str, ...] = ("D", "I")


class InsiderTradingSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the insider-trading schema."""


def validate_insider_trading_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Same fail-loud contract as the other data_engineering schemas.
    ``filing_date`` is the required non-null column -- it is the point-in-time
    anchor this table exists to provide; ``transaction_date`` is also
    required (a filing with no trade date is not a usable row) but is
    checked in the loader alongside the filing-precedes-transaction
    ordering check, not here.

    ``issuer_cik`` is also required non-null -- unlike ``ticker`` and
    ``company_name``, it is the only issuer identifier guaranteed unique.
    Verified against a real 2024 Q1 SEC extract: Liberty Media Corp
    (CIK 0001560385) and Liberty Media LLC (CIK 0001082114) -- two distinct
    legal entities mid corporate restructuring -- both traded under ticker
    "LSXMA" in the same quarter. A dedup key or join keyed on ticker alone
    would silently merge their insiders' trades.
    """
    if "plan_10b5_1" not in df.columns:
        # Optional: sources without the checkbox leave it unknown.
        df = df.assign(plan_10b5_1=pd.NA)
    missing = [c for c in INSIDER_TRADING_COLUMNS if c not in df.columns]
    if missing:
        raise InsiderTradingSchemaError(f"insider trading frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(INSIDER_TRADING_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(INSIDER_TRADING_SOURCES))
    if unknown:
        raise InsiderTradingSchemaError(
            f"unknown source value(s): {unknown}; expected one of {INSIDER_TRADING_SOURCES}"
        )

    unknown_code = sorted(set(out["transaction_code"].dropna().unique()) - set(TRANSACTION_CODE_VALUES))
    if unknown_code:
        raise InsiderTradingSchemaError(
            f"unknown transaction_code value(s): {unknown_code}; expected one of {TRANSACTION_CODE_VALUES}"
        )

    unknown_ad = sorted(set(out["acquired_disposed"].dropna().unique()) - set(ACQUIRED_DISPOSED_VALUES))
    if unknown_ad:
        raise InsiderTradingSchemaError(
            f"unknown acquired_disposed value(s): {unknown_ad}; expected one of {ACQUIRED_DISPOSED_VALUES}"
        )

    unknown_ownership = sorted(set(out["ownership_type"].dropna().unique()) - set(OWNERSHIP_TYPE_VALUES))
    if unknown_ownership:
        raise InsiderTradingSchemaError(
            f"unknown ownership_type value(s): {unknown_ownership}; expected one of {OWNERSHIP_TYPE_VALUES}"
        )

    try:
        for col, dtype in INSIDER_TRADING_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise InsiderTradingSchemaError(f"insider trading frame failed dtype coercion: {exc}") from exc

    if bool(out["issuer_cik"].isna().any()):
        raise InsiderTradingSchemaError("one or more rows have a null issuer_cik")
    if bool(out["filing_date"].isna().any()):
        raise InsiderTradingSchemaError("one or more rows have a null filing_date")
    if bool(out["transaction_date"].isna().any()):
        raise InsiderTradingSchemaError("one or more rows have a null transaction_date")
    if bool((out["filing_date"] < out["transaction_date"]).any()):
        raise InsiderTradingSchemaError(
            "one or more rows have filing_date before transaction_date -- a filing cannot "
            "precede the trade it reports"
        )

    return out
