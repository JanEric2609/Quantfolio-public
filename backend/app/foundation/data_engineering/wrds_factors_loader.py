"""Loader for a manually-exported WRDS "Factors" extract -- shape TBD.

See :mod:`wrds_factors_schema`'s module docstring for the full explanation:
"WRDS Factors" is ambiguous between a portfolio-level factor-RETURN time
series (Fama-French shape, values in percent) and a firm-level factor-
CHARACTERISTICS panel (JKP shape, values in decimal). This loader does not
guess which one a caller wants -- it inspects the extract's own header via
:func:`wrds_factors_schema.detect_wrds_factor_shape` and routes to whichever
matches, refusing the load with a clear error if neither pattern is found.

**Confirm the real product name once WRDS access is in hand.** If it turns
out to be the plain Fama-French return series, check first whether
``quant_factors.py``'s existing live Ken French fetch already covers the
need before ingesting a second copy of the same numbers through this path.

**Chunked by construction** for the characteristics shape, which JKP's own
documentation puts at 5-15GB for a full multi-decade, multi-country extract
-- the same OOM risk :mod:`security_identifiers_loader` already hit once.
The returns shape is always small (a handful of columns, decades of monthly
rows) and is loaded as one pass; splitting it into chunks would add
complexity for a file that is never going to be large enough to need it.

**Not yet exercised against real data.** This module ships ready for
whenever a WRDS Factors extract is available; see ``CONTEXT.md``'s
Owner-wave notes.
"""
from __future__ import annotations

import argparse
import gc
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering._parquet_combine import read_parquet_partitions
from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.data_engineering.wrds_factors_schema import (
    FACTOR_CHARACTERISTICS_COLUMNS,
    FACTOR_RETURNS_COLUMNS,
    JKP_CHARACTERISTICS,
    FactorCharacteristicsSchemaError,
    FactorReturnsSchemaError,
    WrdsFactorShape,
    detect_wrds_factor_shape,
    validate_factor_characteristics_frame,
    validate_factor_returns_frame,
)

logger = logging.getLogger(__name__)

_CHUNK_ROWS = 20_000

_RETURNS_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "mkt_rf": ("mkt_rf", "mktrf", "mkt-rf"),
    "smb": ("smb",),
    "hml": ("hml",),
    "rmw": ("rmw",),
    "cma": ("cma",),
    "umd": ("umd", "mom"),
    "rf": ("rf",),
}
_RETURNS_REQUIRED_COLUMNS = ("date", "mkt_rf")

_CHARACTERISTICS_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "gvkey": ("gvkey",),
    "permno": ("permno", "id"),
    "eom": ("eom", "date"),
    "excntry": ("excntry", "country"),
    "size_grp": ("size_grp",),
    "me": ("me",),
    "ret_exc_lead1m": ("ret_exc_lead1m",),
    "gics": ("gics",),
    # Every other characteristic keeps JKP's own name.
    **{c: (c,) for c in JKP_CHARACTERISTICS},
    "mom_12_1": ("mom_12_1", "ret_12_1"),  # JKP names it ret_12_1
}
_CHARACTERISTICS_REQUIRED_COLUMNS = ("gvkey", "eom")
_CHARACTERISTICS_NUMERIC_COLUMNS = ("me", *JKP_CHARACTERISTICS, "ret_exc_lead1m", "gics")


@dataclass
class WrdsFactorsLoadResult:
    ok: bool
    shape: WrdsFactorShape | None = None
    rows_loaded: int = 0
    identifiers: list[str] = field(default_factory=list)
    error: str | None = None
    duplicate_rows_dropped: int = 0
    unmatched_gvkey_rows_dropped: int = 0
    chunks_processed: int = 0
    # Characteristics the app asks for that the extract's header lacks; they
    # load as null. Non-empty means the WRDS query left some out.
    missing_characteristics: list[str] = field(default_factory=list)


def _normalize_columns(df: pd.DataFrame, target_sources: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    by_key: dict[str, object] = {}
    for col in df.columns:
        by_key.setdefault(str(col).strip().lower(), col)

    rename: dict[object, str] = {}
    claimed: set[object] = set()
    for target, candidates in target_sources.items():
        for candidate in candidates:
            col = by_key.get(candidate)
            if col is not None and col not in claimed:
                rename[col] = target
                claimed.add(col)
                break
    return df.rename(columns=rename)


def _load_factor_returns(path: Path, raw: pd.DataFrame, dry_run: bool) -> WrdsFactorsLoadResult:
    normalized = _normalize_columns(raw, _RETURNS_TARGET_SOURCES)
    missing = [c for c in _RETURNS_REQUIRED_COLUMNS if c not in normalized.columns]
    if missing:
        return WrdsFactorsLoadResult(
            ok=False, error=f"extract missing required column(s) after alias mapping: {missing}"
        )

    date = cast(pd.Series, pd.to_datetime(normalized["date"], errors="coerce"))
    if bool(date.isna().any()):
        return WrdsFactorsLoadResult(
            ok=False, error="one or more rows have an unparseable date value; re-download with YYYY-MM-DD dates"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({"date": date, "source": "wrds_factor_returns", "ingested_at": now})
    for col in ("mkt_rf", "smb", "hml", "rmw", "cma", "umd", "rf"):
        frame[col] = pd.to_numeric(normalized[col], errors="coerce") if col in normalized.columns else float("nan")

    before = len(frame)
    frame = frame.drop_duplicates(subset=["date"])
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_factor_returns_frame(frame)
    except FactorReturnsSchemaError as exc:
        return WrdsFactorsLoadResult(ok=False, error=str(exc))

    if not dry_run:
        out_dir = get_panel_dir() / "wrds_factor_returns_pit"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"
        validated.to_parquet(out_dir / f"{safe_stem}.parquet", index=False)

    return WrdsFactorsLoadResult(
        ok=True,
        shape="factor_returns",
        rows_loaded=len(validated),
        # No natural per-row identifier for a portfolio-level time series
        # (unlike gvkeys/tickers elsewhere) -- left empty rather than
        # repurposing a date list as a stand-in.
        identifiers=[],
        duplicate_rows_dropped=duplicate_rows_dropped,
    )


def _iter_raw_chunks(path: Path, known_headers: set[str]) -> Iterator[pd.DataFrame]:
    header = pd.read_csv(path, nrows=0)
    usecols = [str(c) for c in header.columns if str(c).strip().lower() in known_headers]
    if not usecols:
        yield pd.read_csv(path, low_memory=False)
        return
    reader = cast(
        Iterator[pd.DataFrame],
        pd.read_csv(path, usecols=usecols, low_memory=False, chunksize=_CHUNK_ROWS),  # type: ignore[call-overload]
    )
    yield from reader


def _process_characteristics_chunk(raw_chunk: pd.DataFrame) -> tuple[pd.DataFrame, int, int] | str:
    normalized = _normalize_columns(raw_chunk, _CHARACTERISTICS_TARGET_SOURCES)
    missing = [c for c in _CHARACTERISTICS_REQUIRED_COLUMNS if c not in normalized.columns]
    if missing:
        return f"extract missing required column(s) after alias mapping: {missing}"

    eom = cast(pd.Series, pd.to_datetime(normalized["eom"], errors="coerce"))
    if bool(eom.isna().any()):
        return "one or more rows have an unparseable eom/date value; re-download with YYYY-MM-DD dates"

    now = datetime.now(UTC).replace(tzinfo=None)
    # A "search the entire database" extract legitimately contains CRSP-only
    # securities with no Compustat match: gvkey comes through blank for those
    # rows. Whenever a chunk contains even one blank gvkey, pandas parses that
    # whole chunk's gvkey column as float64 (not object) -- so every OTHER
    # gvkey in the chunk becomes "1157.0" rather than "001157" once
    # stringified, and zfill is a no-op on a string already 6+ chars long.
    # Routing through pd.to_numeric first (not str(...)) is immune to
    # whichever dtype pandas happened to infer -- int64, float64, or
    # object -- and drops the spurious ".0" before zfill ever runs. A blank
    # cell becomes real NaN here and is masked back to NA after, so it is
    # rejected downstream like any other required-column gap instead of
    # being ingested under a fabricated identifier.
    gvkey_numeric = cast(pd.Series, pd.to_numeric(normalized["gvkey"], errors="coerce"))
    gvkey_missing = gvkey_numeric.isna()
    gvkey_clean = gvkey_numeric.astype("Int64").astype(str).mask(gvkey_missing, other=pd.NA)
    frame = pd.DataFrame({
        "gvkey": gvkey_clean.str.zfill(6),
        "permno": pd.to_numeric(normalized["permno"], errors="coerce") if "permno" in normalized.columns else float("nan"),
        "eom": eom,
        "excntry": (
            normalized["excntry"].astype(str).str.strip().str.upper()
            if "excntry" in normalized.columns
            else None
        ),
        "size_grp": (
            normalized["size_grp"].astype(str).str.strip().str.lower()
            if "size_grp" in normalized.columns
            else None
        ),
        "source": "wrds_factor_characteristics",
        "ingested_at": now,
    })
    # Built as one frame: inserting ~45 columns one at a time fragments it.
    numeric = pd.DataFrame(
        {
            col: pd.to_numeric(normalized[col], errors="coerce") if col in normalized.columns else float("nan")
            for col in _CHARACTERISTICS_NUMERIC_COLUMNS
        },
        index=frame.index,
    )
    frame = pd.concat([frame, numeric], axis=1)

    # A CRSP-only security with no Compustat match is a normal, expected
    # outcome of an unrestricted "entire database" search, not a malformed
    # extract -- this table exists to be gvkey-keyed, so such rows are
    # dropped here rather than raising through validate_factor_characteristics_frame
    # and failing the whole chunk over a handful of unmatched securities.
    before = len(frame)
    frame = frame[frame["gvkey"].notna()]
    unmatched_gvkey_dropped = before - len(frame)

    before = len(frame)
    frame = cast(pd.DataFrame, frame).drop_duplicates(subset=["gvkey", "eom"])
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_factor_characteristics_frame(frame)
    except FactorCharacteristicsSchemaError as exc:
        return str(exc)

    return validated, duplicate_rows_dropped, unmatched_gvkey_dropped


def _missing_characteristics(header: pd.Index) -> list[str]:
    present = {str(c).strip().lower() for c in header}
    return [
        target
        for target in (*JKP_CHARACTERISTICS, "ret_exc_lead1m", "gics")
        if not present & set(_CHARACTERISTICS_TARGET_SOURCES[target])
    ]


def _load_factor_characteristics(path: Path, header: pd.Index, dry_run: bool) -> WrdsFactorsLoadResult:
    known_headers = {c for cands in _CHARACTERISTICS_TARGET_SOURCES.values() for c in cands}
    out_dir = get_panel_dir() / "wrds_factor_characteristics_pit"
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"

    total_rows = 0
    gvkeys_seen: set[str] = set()
    duplicate_rows_dropped = 0
    unmatched_gvkey_rows_dropped = 0
    chunk_index = 0

    try:
        for raw_chunk in _iter_raw_chunks(path, known_headers):
            processed = _process_characteristics_chunk(raw_chunk)
            if isinstance(processed, str):
                return WrdsFactorsLoadResult(ok=False, error=processed)
            validated, chunk_dupes, chunk_unmatched = processed

            total_rows += len(validated)
            gvkeys_seen.update(cast(list, validated["gvkey"].unique().tolist()))
            duplicate_rows_dropped += chunk_dupes
            unmatched_gvkey_rows_dropped += chunk_unmatched

            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                validated.to_parquet(out_dir / f"{safe_stem}.{chunk_index:04d}.parquet", index=False)
            chunk_index += 1
            del raw_chunk, validated
            if chunk_index % 50 == 0:
                gc.collect()
    except Exception as exc:
        return WrdsFactorsLoadResult(ok=False, error=f"could not read {path}: {exc}")

    return WrdsFactorsLoadResult(
        ok=True,
        shape="factor_characteristics",
        rows_loaded=total_rows,
        identifiers=sorted(gvkeys_seen),
        duplicate_rows_dropped=duplicate_rows_dropped,
        unmatched_gvkey_rows_dropped=unmatched_gvkey_rows_dropped,
        chunks_processed=chunk_index,
        missing_characteristics=_missing_characteristics(header),
    )


def load_wrds_factors_extract(file_path: Path | str, dry_run: bool = True) -> WrdsFactorsLoadResult:
    """Parse, validate and write a WRDS Factors CSV -- shape auto-detected.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.

    Inspects the header to decide whether this is a portfolio-level
    factor-return time series or a firm-level factor-characteristics panel
    (see module docstring) and dispatches accordingly. Returns ``ok=False``
    with an explanatory error -- rather than guessing -- when the header
    matches neither shape.
    """
    path = Path(file_path)
    try:
        header = pd.read_csv(path, nrows=0)
    except Exception as exc:
        return WrdsFactorsLoadResult(ok=False, error=f"could not read {path}: {exc}")

    shape = detect_wrds_factor_shape(header.columns)
    if shape is None:
        return WrdsFactorsLoadResult(
            ok=False,
            error=(
                "could not determine whether this is a factor-return time series or a firm-"
                "characteristics panel; expected either a date column plus a return column "
                "(mkt_rf/smb/hml) with no firm identifier, or a firm identifier (gvkey/permno) "
                "plus a date/eom column"
            ),
        )

    if shape == "factor_returns":
        try:
            raw = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            return WrdsFactorsLoadResult(ok=False, error=f"could not read {path}: {exc}")
        return _load_factor_returns(path, raw, dry_run)

    return _load_factor_characteristics(path, header.columns, dry_run)


def read_wrds_factor_returns(as_of: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """Read every loaded factor-returns partition back as one frame.

    Args:
        as_of: when given, keep only rows on or before this date.
    """
    out_dir = get_panel_dir() / "wrds_factor_returns_pit"
    parts = [pd.read_parquet(p) for p in sorted(out_dir.glob("*.parquet"))] if out_dir.exists() else []
    if not parts:
        return pd.DataFrame(columns=pd.Index(FACTOR_RETURNS_COLUMNS))

    combined = cast(pd.DataFrame, pd.concat(parts, ignore_index=True))
    combined = combined.sort_values("ingested_at")
    combined = cast(pd.DataFrame, combined.drop_duplicates(subset=["date"], keep="last"))
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        combined = cast(pd.DataFrame, combined[combined["date"] <= cutoff])
    return combined.sort_values("date").reset_index(drop=True)


def read_wrds_factor_characteristics(as_of: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """Read every loaded factor-characteristics partition back as one frame.

    Args:
        as_of: when given, keep only rows whose ``eom`` is on or before this
            date. **Lag convention confirmed 2026-09-22** against JKP's own
            Global Factor Data documentation (jkpfactors.com): ``eom`` *is*
            the availability date -- "the eom column shows the end of
            month, where the data is valid ... it shows the information
            available by the end of a given month." The standard
            reporting-lag convention (6-18 months for accounting-derived
            characteristics, skip-most-recent-month for ``mom_12_1``) is
            already baked in by JKP before a row is assigned its ``eom``;
            callers do **not** need to apply any further lag on top of it
            (doing so would double-lag the data). This makes
            ``pit_wrds_factor_characteristics_for_symbol``'s direct
            ``merge_asof(..., direction="backward")`` against raw ``eom``
            correct as written -- no ``fundamentals_loader``-style
            ``available_from`` padding needed here, unlike that module's
            genuinely-separate reporting-lag source. Unrelated to this: a
            characteristic at ``eom=T`` is contemporaneous with that same
            month's own return, so pairing it with a *subsequent* month's
            return for prediction (not this loader's concern -- see
            ``FACTOR_CHARACTERISTIC_VALUE_COLUMNS``'s exclusion of
            ``ret_exc_lead1m`` in ``pit_panel_joins.py``) is the caller's
            responsibility, not a PIT-join safety issue.
    """
    out_dir = get_panel_dir() / "wrds_factor_characteristics_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(FACTOR_CHARACTERISTICS_COLUMNS))

    combined = read_parquet_partitions(paths)
    combined = combined.sort_values("ingested_at")
    combined = cast(pd.DataFrame, combined.drop_duplicates(subset=["gvkey", "eom"], keep="last"))
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        combined = cast(pd.DataFrame, combined[combined["eom"] <= cutoff])
    return combined.sort_values(["gvkey", "eom"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a WRDS Factors CSV (shape auto-detected) into the PIT Parquet panel."
    )
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_wrds_factors_extract(args.csv_path, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info("%s: shape=%s, %d rows%s", mode, result.shape, result.rows_loaded, suffix)
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)
    if result.unmatched_gvkey_rows_dropped:
        logger.info("  rows with no gvkey (unmatched to Compustat) dropped: %d", result.unmatched_gvkey_rows_dropped)
    if result.missing_characteristics:
        logger.warning(
            "  %d characteristic(s) not in the extract, loaded as null: %s",
            len(result.missing_characteristics),
            ", ".join(result.missing_characteristics),
        )


if __name__ == "__main__":
    _main()
