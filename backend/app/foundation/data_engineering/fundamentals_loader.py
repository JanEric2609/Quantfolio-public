"""Loader for a manually-exported Compustat Global Fundamentals Annual extract.

Companion to :mod:`datastream_loader`, which handles daily prices. This one
handles ``g_funda`` -- book equity, earnings, assets, revenue -- and exists to
close a specific, documented hole.

**The hole.** ``app.lab.alphacrafter.panel`` admits ``pe_ratio``, ``market_cap``
and ``roe`` into the factor-search vocabulary (``factor_dsl``), but sources
them from the providers registry, which exposes only a *latest* snapshot. That
snapshot is broadcast backwards across the whole date index, so a historical IC
computed on a fundamental factor scores 2011 rankings using 2026 accounting
data. The module says so in its own docstring and calls the result "indicative
only". Point-in-time fundamentals are what turn that into a real number.

**So this loader must not reintroduce the same bug.** Compustat's ``datadate``
is the fiscal period *end*, not the date the figures became public -- an
annual report lands months after the year it describes. Every row therefore
gets an ``available_from`` date (``datadate`` + a publication lag, default six
months, the Fama-French convention of matching fiscal years ending in calendar
year t-1 to returns from July of year t). Consumers filter on
``available_from``. Filtering on ``datadate`` reproduces exactly the lookahead
this module was written to remove.

**Currency trap.** ``curcd`` (the reporting currency of the fundamentals) is
not necessarily ``curcdd`` (the quotation currency of the price line). A firm
can report in USD and trade in EUR. Book-to-market divides one by the other,
so a caller combining this panel with the price panel MUST reconcile the two
currencies first. Like the price loader, this module stores what it is given
and converts nothing.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.fundamentals_schema import (
    FUNDAMENTALS_COLUMNS,
    FundamentalsSchemaError,
    validate_fundamentals_frame,
)
from app.foundation.data_engineering._parquet_combine import read_parquet_partitions
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

# Fama-French match annual accounting data for fiscal years ending in calendar
# year t-1 to returns from July of year t -- a minimum gap of six months, which
# is comfortably longer than a normal European filing deadline. Shorten it only
# with a reason; every month cut is a month of assumed clairvoyance.
DEFAULT_PUBLICATION_LAG_MONTHS = 6

# Candidate source headers per target column, highest precedence first.
# Annual (``g_funda``) mnemonics lead; the quarterly (``g_fundq``) spellings
# follow so a quarterly extract loads without renaming, and plain-English
# headers last for hand-tidied sheets.
_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "gvkey": ("gvkey",),
    "datadate": ("datadate", "date", "as_of_date"),
    "fyear": ("fyear", "fyearq", "fiscal_year"),
    "currency": ("curcd", "curcdq", "currency"),
    "total_assets": ("at", "atq", "total_assets"),
    "common_equity": ("ceq", "ceqq", "common_equity"),
    "net_income": ("ni", "niq", "net_income"),
    "revenue": ("revt", "revtq", "sale", "revenue"),
    "shares_outstanding": ("csho", "cshoq", "shares_outstanding"),
    "eps": ("epspx", "epspxq", "eps"),
    "long_term_debt": ("dltt", "dlttq", "long_term_debt"),
}

_REQUIRED_TARGET_COLUMNS = ("gvkey", "datadate")

# Financial figures -- at least one must be present, or the extract is a list
# of company-years with no accounting content and nothing to compute from.
_VALUE_COLUMNS = (
    "total_assets",
    "common_equity",
    "net_income",
    "revenue",
    "shares_outstanding",
    "eps",
    "long_term_debt",
)

# Compustat ships several presentations of the same company-year, discriminated
# by four flag columns. Three have an unambiguous right answer for a
# point-in-time equity panel and are filtered automatically when present:
#
#   datafmt = STD   standardised presentation (not restated summaries)
#   consol  = C     consolidated (not parent-only)
#   popsrc  = I     international population -- the Global file's own value
#
# ``indfmt`` is deliberately NOT in this list. It separates INDL (industrial)
# from FS (financial-services) presentation, and filtering to INDL -- the
# reflex borrowed from Compustat North America recipes -- silently deletes
# every bank and insurer from the universe. That is a sector-sized hole in a
# European panel, so the choice is left to the caller and surfaces as a
# duplicate error below rather than as a quiet deletion.
_STANDARD_FLAGS: dict[str, str] = {"datafmt": "STD", "consol": "C", "popsrc": "I"}


@dataclass
class FundamentalsLoadResult:
    ok: bool
    rows_loaded: int = 0
    gvkeys: list[str] = field(default_factory=list)
    error: str | None = None
    # Rows dropped by the _STANDARD_FLAGS filter, per flag. Reported rather
    # than logged away: a filter that removes most of the file is a wrong
    # extract, and the caller should see that in the result.
    dropped_by_flag: dict[str, int] = field(default_factory=dict)
    publication_lag_months: int = DEFAULT_PUBLICATION_LAG_MONTHS


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename recognised Compustat headers to schema column names.

    Resolution is keyed by target, first candidate wins, and a source column
    can be claimed only once -- so an extract carrying both ``revt`` and
    ``sale`` collapses to one ``revenue`` rather than producing a duplicate
    column name that would crash DataFrame construction downstream.
    """
    by_key: dict[str, object] = {}
    for col in df.columns:
        by_key.setdefault(str(col).strip().lower(), col)

    rename: dict[object, str] = {}
    claimed: set[object] = set()
    for target, candidates in _TARGET_SOURCES.items():
        for candidate in candidates:
            col = by_key.get(candidate)
            if col is not None and col not in claimed:
                rename[col] = target
                claimed.add(col)
                break
    return df.rename(columns=rename)


def _apply_standard_flags(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Keep only the standardised/consolidated/international presentation.

    Each flag is filtered only if the extract actually carries that column, so
    a pre-filtered WRDS query (the flags unticked) passes through untouched.
    """
    out = df
    dropped: dict[str, int] = {}
    for flag, keep in _STANDARD_FLAGS.items():
        if flag not in out.columns:
            continue
        mask = out[flag].astype("string").str.strip().str.upper() == keep
        removed = int((~mask).sum())
        if removed:
            dropped[flag] = removed
        out = cast(pd.DataFrame, out[mask])
    return out, dropped


def load_fundamentals_extract(
    file_path: Path | str,
    dry_run: bool = True,
    publication_lag_months: int = DEFAULT_PUBLICATION_LAG_MONTHS,
) -> FundamentalsLoadResult:
    """Parse, validate and write a Compustat Global fundamentals CSV.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.
        publication_lag_months: months added to ``datadate`` to obtain
            ``available_from``. See the module docstring before lowering it.

    Returns a :class:`FundamentalsLoadResult`; a malformed extract comes back
    as ``ok=False`` with an explanatory ``error`` rather than a traceback.
    """
    if publication_lag_months < 0:
        return FundamentalsLoadResult(
            ok=False, error=f"publication_lag_months must be >= 0, got {publication_lag_months}"
        )

    path = Path(file_path)
    try:
        raw = pd.read_csv(path, low_memory=False)
    except Exception as exc:
        return FundamentalsLoadResult(ok=False, error=f"could not read {path}: {exc}")

    normalized = _normalize_columns(raw)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if missing:
        return FundamentalsLoadResult(
            ok=False, error=f"extract missing required column(s) after alias mapping: {missing}"
        )

    present_values = [c for c in _VALUE_COLUMNS if c in normalized.columns]
    if not present_values:
        return FundamentalsLoadResult(
            ok=False,
            error=(
                "extract carries no financial figures after alias mapping (looked for "
                f"{list(_VALUE_COLUMNS)}); re-export with at least one, e.g. ceq (book equity)"
            ),
        )

    normalized, dropped_by_flag = _apply_standard_flags(normalized)
    if normalized.empty:
        return FundamentalsLoadResult(
            ok=False,
            error=(
                f"no rows left after filtering to {_STANDARD_FLAGS} (dropped: {dropped_by_flag}); "
                "the extract is probably not a Compustat Global annual file"
            ),
            dropped_by_flag=dropped_by_flag,
        )

    datadate = cast(pd.Series, pd.to_datetime(normalized["datadate"], errors="coerce"))
    if bool(datadate.isna().any()):
        return FundamentalsLoadResult(
            ok=False,
            error="one or more rows have an unparseable datadate value; re-download with YYYY-MM-DD dates",
            dropped_by_flag=dropped_by_flag,
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "gvkey": normalized["gvkey"].astype(str).str.strip().str.zfill(6),
        "datadate": datadate,
        "available_from": datadate + pd.DateOffset(months=publication_lag_months),
        # NaN/None rather than pd.NA for an absent column: pd.NA cannot be cast
        # to float64 or string, so a partial extract would die in dtype
        # coercion with a message about NAType instead of loading its columns.
        "fyear": normalized["fyear"] if "fyear" in normalized.columns else float("nan"),
        "currency": normalized["currency"] if "currency" in normalized.columns else None,
        "source": "compustat_global_funda",
        "ingested_at": now,
    })
    for col in _VALUE_COLUMNS:
        frame[col] = (
            pd.to_numeric(normalized[col], errors="coerce")
            if col in normalized.columns
            else float("nan")
        )

    try:
        validated = validate_fundamentals_frame(frame)
    except FundamentalsSchemaError as exc:
        return FundamentalsLoadResult(ok=False, error=str(exc), dropped_by_flag=dropped_by_flag)

    # One accounting row per company per period. The usual cause of a clash is
    # an extract carrying both INDL and FS presentations of the same firm --
    # see the _STANDARD_FLAGS comment for why this module refuses to pick for
    # you rather than dropping the financial sector on your behalf.
    dupes = validated.duplicated(subset=["gvkey", "datadate"], keep=False)
    if bool(dupes.any()):
        offenders = sorted({str(v) for v in validated.loc[dupes, "gvkey"]})
        return FundamentalsLoadResult(
            ok=False,
            error=(
                f"{int(dupes.sum())} row(s) share a (gvkey, datadate) with another row "
                f"(gvkeys: {offenders[:10]}); the extract holds more than one presentation per "
                "company-year -- most often both indfmt=INDL and indfmt=FS. Choose one in the "
                "WRDS query, remembering that INDL alone excludes banks and insurers"
            ),
            dropped_by_flag=dropped_by_flag,
        )

    gvkeys = sorted(cast(list, validated["gvkey"].unique().tolist()))
    if not dry_run:
        out_dir = get_panel_dir() / "fundamentals_pit"
        out_dir.mkdir(parents=True, exist_ok=True)
        # One Parquet per source CSV, not per company: fundamentals are a few
        # rows per firm-year, and a file per gvkey would scatter a European
        # universe across thousands of tiny files. Year-at-a-time extracts
        # therefore land side by side and are reassembled on read.
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"
        validated.to_parquet(out_dir / f"{safe_stem}.parquet", index=False)

    return FundamentalsLoadResult(
        ok=True,
        rows_loaded=len(validated),
        gvkeys=gvkeys,
        dropped_by_flag=dropped_by_flag,
        publication_lag_months=publication_lag_months,
    )


def read_fundamentals(as_of: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """Read every loaded fundamentals partition back as one frame.

    Args:
        as_of: when given, keep only rows whose ``available_from`` is on or
            before this date -- i.e. what was actually knowable then. Leaving
            it ``None`` returns the whole panel, which is correct for
            inspection and wrong for backtesting.

    Later extracts win on a repeated ``(gvkey, datadate)``, so re-loading a
    corrected export supersedes the earlier one without a manual delete.
    """
    out_dir = get_panel_dir() / "fundamentals_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(FUNDAMENTALS_COLUMNS))

    combined = read_parquet_partitions(paths)
    combined = combined.sort_values("ingested_at")
    combined = cast(
        pd.DataFrame, combined.drop_duplicates(subset=["gvkey", "datadate"], keep="last")
    )
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        combined = cast(pd.DataFrame, combined[combined["available_from"] <= cutoff])
    return combined.sort_values(["gvkey", "datadate"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a Compustat Global fundamentals CSV into the PIT Parquet panel."
    )
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    parser.add_argument(
        "--lag-months",
        type=int,
        default=DEFAULT_PUBLICATION_LAG_MONTHS,
        help=(
            "months from fiscal period end to assumed public availability "
            f"(default {DEFAULT_PUBLICATION_LAG_MONTHS}); lowering it assumes earlier knowledge"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_fundamentals_extract(
        args.csv_path, dry_run=not args.apply, publication_lag_months=args.lag_months
    )

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info(
        "%s: %d rows, %d companies, publication lag=%d months%s",
        mode,
        result.rows_loaded,
        len(result.gvkeys),
        result.publication_lag_months,
        suffix,
    )
    if result.dropped_by_flag:
        logger.info("  dropped by presentation flags: %s", result.dropped_by_flag)


if __name__ == "__main__":
    _main()
