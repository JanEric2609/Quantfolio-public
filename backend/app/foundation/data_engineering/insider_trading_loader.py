"""Loader for SEC EDGAR's quarterly bulk "Insider Transactions" data set.

Companion to the other ``data_engineering`` loaders in point-in-time
discipline but a genuinely different *input* shape: SEC does not publish a
single flat CSV. Each quarterly download
(https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
is a zip of several tab-separated files extracted from Form 3/4/5 XML,
joined here on ``ACCESSION_NUMBER``:

- ``SUBMISSION.tsv`` -- one row per filing: ``FILING_DATE``, ``ISSUERCIK``,
  ``ISSUERNAME``, ``ISSUERTRADINGSYMBOL``, ``DOCUMENT_TYPE``, and (sets
  from 2023 on) ``AFF10B5ONE``, the Rule 10b5-1 plan checkbox.
- ``REPORTINGOWNER.tsv`` -- one row per (filing, insider): ``RPTOWNERCIK``,
  ``RPTOWNERNAME``, ``RPTOWNER_RELATIONSHIP`` (e.g. ``"Director,Officer"``).
  A filing legitimately has more than one reporting owner (joint filers --
  spouses, trusts) -- verified against a real 2024 Q1 extract, where roughly
  1 in 14 filings has 2+ owners, up to 10 on one filing. Joining on
  ``ACCESSION_NUMBER`` correctly fans a transaction out to one row per
  co-filer rather than picking one arbitrarily.
- ``NONDERIV_TRANS.tsv`` -- one row per non-derivative transaction line:
  ``TRANS_DATE``, ``TRANS_CODE``, ``TRANS_SHARES``, ``TRANS_PRICEPERSHARE``,
  ``TRANS_ACQUIRED_DISP_CD``, ``SHRS_OWND_FOLWNG_TRANS``,
  ``DIRECT_INDIRECT_OWNERSHIP``. This is the file equivalent to WRDS
  Insiders "Table 1"; the sibling ``DERIV_TRANS.tsv`` (Table 2, options/
  derivatives) is out of scope for this loader.

Both ``TRANS_DATE`` and ``FILING_DATE`` are SEC's ``DD-MON-YYYY`` format
(e.g. ``"15-NOV-2022"``), not ISO -- parsed explicitly rather than relying on
pandas' format inference, which is unreliable on that layout.

**No cusip or cleanse_code in this source.** Those two
:mod:`insider_trading_schema` columns come from WRDS's own derived fields;
SEC's raw filings carry neither (a Form 4's ``SECURITY_TITLE`` is free text
like ``"Common Stock"``, not a CUSIP), so both are always null when loaded
from this pipeline.

**Not chunked.** A full quarter is ~50-100k transactions (~10-15MB of TSV,
verified against real 2024 Q1 data) -- small enough to read in one shot, unlike
the WRDS Factors extract's 2GB CSV that made per-chunk reading necessary
there. One call to :func:`load_insider_trading_extract` processes one
quarter; loading history means calling it once per quarterly zip.
"""
from __future__ import annotations

import argparse
import logging
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.insider_trading_schema import (
    INSIDER_TRADING_COLUMNS,
    OPEN_MARKET_TRANSACTION_CODES,
    InsiderTradingSchemaError,
    validate_insider_trading_frame,
)
from app.foundation.data_engineering._parquet_combine import read_parquet_partitions
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

_SEC_DATE_FORMAT = "%d-%b-%Y"

_REQUIRED_FILES = ("SUBMISSION.tsv", "REPORTINGOWNER.tsv", "NONDERIV_TRANS.tsv")

#: SUBMISSION's Rule 10b5-1 plan checkbox; SEC added it to the 2023-2025
#: sets in July 2025, and older sets (and the test fixture) lack it.
_AFF10B5ONE = "AFF10B5ONE"

# issuer_cik (not ticker) disambiguates the company: verified against a real
# 2024 Q1 extract where two distinct legal entities (Liberty Media Corp and
# Liberty Media LLC, mid corporate restructuring) both traded under ticker
# "LSXMA" in the same quarter -- a ticker-keyed dedup silently merged their
# insiders' trades. price_per_share is included too: an institutional
# insider trading identical round-lot share counts across several portfolio
# companies on the same day is a real, observed pattern, not a duplicate.
_DEDUP_KEY = ["cik", "issuer_cik", "transaction_date", "transaction_code", "shares", "price_per_share"]


@dataclass
class InsiderTradingLoadResult:
    ok: bool
    rows_loaded: int = 0
    ciks: list[str] = field(default_factory=list)
    error: str | None = None
    duplicate_rows_dropped: int = 0
    malformed_date_rows_dropped: int = 0
    filing_precedes_transaction_rows_dropped: int = 0
    blank_code_rows_dropped: int = 0


def _resolve_source_dir(source: Path, tmp_dir: Path) -> Path | str:
    """Return a directory containing the three required TSVs, extracting a zip if needed."""
    if source.is_dir():
        return source
    if source.suffix.lower() != ".zip":
        return f"{source} is neither a directory nor a .zip file"
    try:
        with zipfile.ZipFile(source) as zf:
            zf.extractall(tmp_dir)
    except (OSError, zipfile.BadZipFile) as exc:
        return f"could not extract {source}: {exc}"
    return tmp_dir


def _read_tsv(path: Path, usecols: list[str]) -> pd.DataFrame | str:
    try:
        # pandas-stubs' UsecolsArgType overload rejects a plain list[str].
        return pd.read_csv(path, sep="\t", usecols=usecols, dtype=str, keep_default_na=False, low_memory=False)  # type: ignore
    except pd.errors.EmptyDataError:
        return f"{path.name} is empty"
    except (OSError, ValueError) as exc:
        return f"could not read {path.name}: {exc}"


def _tsv_header(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.readline().rstrip("\r\n").split("\t")
    except OSError:
        return []


def plan_flag(values: pd.Series) -> pd.Series:
    """``AFF10B5ONE`` / ``<aff10b5One>`` as a nullable boolean.

    SEC ships ``1``/``0`` and ``true``/``false`` (2026 Q2: 35k "0", 10k
    "false", 4k "1", 1k "true"); blank (Form 3, filings before April 2023)
    is unknown, not "no plan".
    """
    text = values.astype(str).str.strip().str.lower()
    flag = pd.Series(pd.NA, index=values.index, dtype="boolean")
    flag[text.isin(("1", "true"))] = True
    flag[text.isin(("0", "false"))] = False
    return flag


def _parse_sec_date(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format=_SEC_DATE_FORMAT, errors="coerce")
    blank = series.astype(str).str.strip().eq("")
    return cast(pd.Series, parsed.mask(blank, other=pd.NaT))


def _load_and_join(source_dir: Path) -> pd.DataFrame | str:
    missing_files = [name for name in _REQUIRED_FILES if not (source_dir / name).exists()]
    if missing_files:
        return f"missing required file(s): {missing_files}"

    submission_columns = ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL"]
    if _AFF10B5ONE in _tsv_header(source_dir / "SUBMISSION.tsv"):
        submission_columns.append(_AFF10B5ONE)
    submission = _read_tsv(source_dir / "SUBMISSION.tsv", submission_columns)
    if isinstance(submission, str):
        return submission

    owner = _read_tsv(
        source_dir / "REPORTINGOWNER.tsv",
        ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP"],
    )
    if isinstance(owner, str):
        return owner

    trans = _read_tsv(
        source_dir / "NONDERIV_TRANS.tsv",
        [
            "ACCESSION_NUMBER",
            "TRANS_DATE",
            "TRANS_CODE",
            "TRANS_SHARES",
            "TRANS_PRICEPERSHARE",
            "TRANS_ACQUIRED_DISP_CD",
            "SHRS_OWND_FOLWNG_TRANS",
            "DIRECT_INDIRECT_OWNERSHIP",
        ],
    )
    if isinstance(trans, str):
        return trans

    for name, frame in (("SUBMISSION.tsv", submission), ("REPORTINGOWNER.tsv", owner), ("NONDERIV_TRANS.tsv", trans)):
        if bool((frame["ACCESSION_NUMBER"] == "").any()):
            return f"{name} has one or more rows with a blank ACCESSION_NUMBER"

    joined = trans.merge(submission, on="ACCESSION_NUMBER", how="left", validate="many_to_one")
    missing_submission = int(joined["ISSUERCIK"].isna().sum())
    if missing_submission:
        return (
            f"{missing_submission} transaction row(s) reference an ACCESSION_NUMBER "
            "not present in SUBMISSION.tsv -- inconsistent extract"
        )

    joined = joined.merge(owner, on="ACCESSION_NUMBER", how="left", validate="many_to_many")
    missing_owner = int(joined["RPTOWNERCIK"].isna().sum())
    if missing_owner:
        return (
            f"{missing_owner} transaction row(s) reference an ACCESSION_NUMBER not present in "
            "REPORTINGOWNER.tsv -- inconsistent extract"
        )

    return joined


def _process(joined: pd.DataFrame) -> tuple[pd.DataFrame, int, int, int, int] | str:
    transaction_date = _parse_sec_date(cast(pd.Series, joined["TRANS_DATE"]))
    filing_date = _parse_sec_date(cast(pd.Series, joined["FILING_DATE"]))

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "cik": joined["RPTOWNERCIK"].astype(str).str.strip(),
        "issuer_cik": joined["ISSUERCIK"].astype(str).str.strip(),
        "company_name": joined["ISSUERNAME"].astype(str).str.strip(),
        "ticker": joined["ISSUERTRADINGSYMBOL"].astype(str).str.strip().str.upper(),
        "cusip": None,
        "insider_name": joined["RPTOWNERNAME"].astype(str).str.strip(),
        "insider_role": joined["RPTOWNER_RELATIONSHIP"].astype(str).str.strip().str.upper(),
        "transaction_date": transaction_date,
        "filing_date": filing_date,
        "transaction_code": joined["TRANS_CODE"].astype(str).str.strip().str.upper(),
        "acquired_disposed": joined["TRANS_ACQUIRED_DISP_CD"].astype(str).str.strip().str.upper(),
        "shares": pd.to_numeric(joined["TRANS_SHARES"], errors="coerce"),
        "price_per_share": pd.to_numeric(joined["TRANS_PRICEPERSHARE"], errors="coerce"),
        "shares_held_after": pd.to_numeric(joined["SHRS_OWND_FOLWNG_TRANS"], errors="coerce"),
        "ownership_type": joined["DIRECT_INDIRECT_OWNERSHIP"].astype(str).str.strip().str.upper(),
        "cleanse_code": None,
        "form_type": joined["DOCUMENT_TYPE"].astype(str).str.strip(),
        "source": "sec_edgar_form4_nonderivative",
        "ingested_at": now,
        "plan_10b5_1": (
            plan_flag(cast(pd.Series, joined[_AFF10B5ONE]))
            if _AFF10B5ONE in joined.columns
            else pd.Series(pd.NA, index=joined.index, dtype="boolean")
        ),
    })

    # SEC's own bulk data is "presented without change from the 'as-filed'
    # submissions" -- individual filers hand-type TRANS_DATE into the Form 4
    # XML (unlike FILING_DATE, which SEC stamps itself on receipt), and a
    # real 2024 Q1 extract contains 7 rows with a mistyped 4-digit year
    # ("0024" instead of "2024"). Guessing the intended date would be
    # fabricating filer intent; dropping the row is the same "genuinely
    # unusable, drop and count rather than fail the whole extract" choice
    # made for WRDS Factors' blank gvkeys.
    before = len(frame)
    frame = frame[frame["transaction_date"].notna() & frame["filing_date"].notna()]
    malformed_date_rows_dropped = before - len(frame)

    # A real 2024 Q1 extract also contains 14 rows where TRANS_DATE parses
    # fine but lands *after* FILING_DATE -- e.g. "15-FEB-2026" filed
    # "25-MAR-2024", or a day/month transposition landing 16 days past its
    # own filing. Both dates are individually well-formed, so this can't be
    # caught by the parse step above; it's the same "hand-typed filer
    # mistake" root cause as the year typos, just shaped differently. SEC
    # ships it "as-filed... without change", so the row is untrustworthy
    # rather than fixable -- dropped here (before validate) rather than
    # failing the whole quarter, same reasoning as the block above.
    before = len(frame)
    frame = frame[frame["filing_date"] >= frame["transaction_date"]]
    filing_precedes_transaction_rows_dropped = before - len(frame)

    # The 2026 Q2 set has 2 of its 78,328 transaction lines with no
    # transaction code at all, which failed the whole quarter. A line without
    # a code says nothing about the insider's decision: dropped and counted.
    # A blank optional code is missing, not a new code value.
    before = len(frame)
    frame = cast(pd.DataFrame, frame[frame["transaction_code"] != ""]).copy()
    blank_code_rows_dropped = before - len(frame)
    for column in ("acquired_disposed", "ownership_type"):
        frame[column] = frame[column].replace("", None)

    before = len(frame)
    frame = cast(pd.DataFrame, frame).drop_duplicates(subset=_DEDUP_KEY)
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_insider_trading_frame(frame)
    except InsiderTradingSchemaError as exc:
        return str(exc)

    return (
        validated,
        duplicate_rows_dropped,
        malformed_date_rows_dropped,
        filing_precedes_transaction_rows_dropped,
        blank_code_rows_dropped,
    )


def load_insider_trading_extract(source: Path | str, dry_run: bool = True) -> InsiderTradingLoadResult:
    """Parse, validate and write one quarter of SEC EDGAR insider transactions.

    Args:
        source: either the quarterly ``.zip`` as downloaded from SEC, or a
            directory it has already been extracted into.
        dry_run: when True (default) validate and report, writing nothing.

    Returns an :class:`InsiderTradingLoadResult`; a malformed or incomplete
    extract comes back as ``ok=False`` with an explanatory ``error`` rather
    than a traceback.
    """
    path = Path(source)
    out_dir = get_panel_dir() / "insider_trading_pit"
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"

    with tempfile.TemporaryDirectory(prefix="sec_insiders_") as tmp:
        resolved = _resolve_source_dir(path, Path(tmp))
        if isinstance(resolved, str):
            return InsiderTradingLoadResult(ok=False, error=resolved)

        joined = _load_and_join(resolved)
        if isinstance(joined, str):
            return InsiderTradingLoadResult(ok=False, error=joined)

        processed = _process(joined)
        if isinstance(processed, str):
            return InsiderTradingLoadResult(ok=False, error=processed)
        (
            validated,
            duplicate_rows_dropped,
            malformed_date_rows_dropped,
            filing_precedes_transaction_rows_dropped,
            blank_code_rows_dropped,
        ) = processed

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        validated.to_parquet(out_dir / f"{safe_stem}.parquet", index=False)

    return InsiderTradingLoadResult(
        ok=True,
        rows_loaded=len(validated),
        ciks=sorted(cast(list, validated["cik"].unique().tolist())),
        duplicate_rows_dropped=duplicate_rows_dropped,
        malformed_date_rows_dropped=malformed_date_rows_dropped,
        filing_precedes_transaction_rows_dropped=filing_precedes_transaction_rows_dropped,
        blank_code_rows_dropped=blank_code_rows_dropped,
    )


def read_insider_trading(
    as_of: pd.Timestamp | str | None = None,
    open_market_only: bool = False,
) -> pd.DataFrame:
    """Read every loaded insider-trading partition back as one frame.

    Args:
        as_of: when given, keep only rows whose ``filing_date`` is on or
            before this date -- i.e. what was actually public then. Leaving
            it ``None`` returns the whole panel, which is correct for
            inspection and wrong for backtesting (see module docstring).
        open_market_only: when True, keep only
            :data:`insider_trading_schema.OPEN_MARKET_TRANSACTION_CODES`
            (P/S). Off by default so a caller inspecting the raw filings
            sees everything the source shipped.
    """
    out_dir = get_panel_dir() / "insider_trading_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(INSIDER_TRADING_COLUMNS))

    combined = read_parquet_partitions(paths)
    combined = combined.sort_values("ingested_at")
    combined = cast(pd.DataFrame, combined.drop_duplicates(subset=_DEDUP_KEY, keep="last"))

    if open_market_only:
        combined = cast(pd.DataFrame, combined[combined["transaction_code"].isin(OPEN_MARKET_TRANSACTION_CODES)])

    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        combined = cast(pd.DataFrame, combined[combined["filing_date"] <= cutoff])

    return combined.sort_values(["cik", "filing_date"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load one quarter of SEC EDGAR insider-transaction data into the PIT Parquet panel."
    )
    parser.add_argument("source", help="path to the quarterly .zip, or a directory it was extracted into")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_insider_trading_extract(args.source, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info("%s: %d rows, %d insiders%s", mode, result.rows_loaded, len(result.ciks), suffix)
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)
    if result.malformed_date_rows_dropped:
        logger.info("  rows with an unparseable transaction/filing date dropped: %d", result.malformed_date_rows_dropped)
    if result.filing_precedes_transaction_rows_dropped:
        logger.info(
            "  rows where filing_date precedes transaction_date dropped: %d",
            result.filing_precedes_transaction_rows_dropped,
        )
    if result.blank_code_rows_dropped:
        logger.info("  rows with no transaction code dropped: %d", result.blank_code_rows_dropped)


if __name__ == "__main__":
    _main()
