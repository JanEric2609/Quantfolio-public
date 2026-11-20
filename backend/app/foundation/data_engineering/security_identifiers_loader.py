"""Loader for a manually-exported Compustat Global security-identifiers extract.

Companion to :mod:`fundamentals_loader` and :mod:`datastream_loader`. Those two
key their panels differently -- fundamentals by ``gvkey``, prices by whatever
symbol the export used (often an ISIN) -- and reconciling the two requires
knowing which ISIN(s) a given ``gvkey`` actually carried on a given day.
Compustat's own identifier history changes over time (reissued ISINs, added or
delisted share classes), so this loader keeps the full daily history rather
than collapsing it to one row per company: collapsing away the ``as_of_date``
would silently misjoin a 2019 price row to a 2024 ISIN.

Expected CSV columns (case-insensitive): ``gvkey``, ``isin`` (nullable --
unlisted/delisted securities carry no ISIN and that blankness is itself
information), ``fic`` (Compustat's ISO-alpha-3 country-of-incorporation code),
``datadate`` (the day this identifier mapping was observed to hold).

**Chunked by construction.** A universe-wide daily extract is tens of millions
of rows (one calendar year, all gvkeys, was ~18.5M rows / ~600MB in practice).
Reading it whole and building the normalised frame in memory pushed a 16GB
dev machine into ``numpy._core._exceptions._ArrayMemoryError`` -- the
categorical read savings in ``_read_extract`` don't survive the ``.astype(str)``
normalisation step, which materialises a full Python-object array again. So
the extract is read and processed ``_CHUNK_ROWS`` rows at a time, each chunk
validated and written as its own parquet part; ``read_security_identifiers``
already globs every part in the directory, so this needs no change on the
read side. The one thing this gives up: an exact-duplicate row split across a
chunk boundary is not deduplicated at load time (only within a chunk) --
``read_security_identifiers`` still catches it, since it deduplicates the
combined frame at read time.
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
import pyarrow as pa
import pyarrow.dataset as pa_dataset

from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.data_engineering.security_identifiers_schema import (
    SECURITY_IDENTIFIERS_COLUMNS,
    SecurityIdentifiersSchemaError,
    validate_security_identifiers_frame,
)

logger = logging.getLogger(__name__)

_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "gvkey": ("gvkey",),
    "isin": ("isin",),
    "fic": ("fic", "country", "iso_country"),
    "as_of_date": ("as_of_date", "datadate", "date"),
}

_REQUIRED_TARGET_COLUMNS = ("gvkey", "isin", "as_of_date")

# Rows per processing chunk. Small test fixtures always fit in one chunk, so
# this is invisible to anything but a real universe-wide extract. Kept modest
# rather than tuned for throughput: the loader needs to run on ordinary
# desktop-class memory, not just a generously provisioned server.
_CHUNK_ROWS = 20_000


def _string_types_mapper(pa_type: pa.DataType) -> pd.api.extensions.ExtensionDtype | None:
    """Route Arrow string columns straight to pandas' Arrow-backed string dtype.

    Passed to ``pyarrow.Table.to_pandas`` so string columns keep Arrow's
    compact buffer representation (one small offset entry per value, plus the
    shared character buffer) instead of pandas' nullable ``"string"``/
    ``object`` dtype, which boxes every value as its own Python string object
    -- ~50-80 bytes of pure per-value overhead regardless of length. Across 4
    string columns x ~16.7M rows that overhead alone was several GB, before
    even counting the dedup/sort copy that follows. ``isin``/``fic``/
    ``source`` keep this dtype (nothing joins on them); ``as_of_date``/
    ``ingested_at`` are cast back to plain ``datetime64[ns]`` and ``gvkey``
    back to plain ``"string"`` immediately afterwards, since those are the
    columns another loader's merge actually keys on --
    ``pit_fundamentals.py`` passes ``gvkey`` as ``pd.merge_asof``'s ``by=``
    against ``fundamentals_schema``'s own ``"gvkey": "string"`` column, which
    requires an exact dtype match.
    """
    if pa.types.is_string(pa_type) or pa.types.is_large_string(pa_type):
        return pd.ArrowDtype(pa_type)
    return None


@dataclass
class SecurityIdentifiersLoadResult:
    ok: bool
    rows_loaded: int = 0
    gvkeys: list[str] = field(default_factory=list)
    error: str | None = None
    # Exact duplicate (gvkey, isin, fic, as_of_date) rows collapsed on load --
    # reported rather than silently absorbed, since a source export that is
    # mostly duplicates is probably the wrong extract. Counted within a chunk
    # only; see the module docstring for the cross-chunk-boundary caveat.
    duplicate_rows_dropped: int = 0
    chunks_processed: int = 0


def _iter_raw_chunks(path: Path) -> Iterator[pd.DataFrame]:
    """Yield the extract in ``_CHUNK_ROWS``-row pieces, columns/dtypes restricted.

    Mirrors ``datastream_loader._read_extract``'s column/category restriction,
    plus ``chunksize`` so a 18M-row extract never exists as one frame.
    """
    header = pd.read_csv(path, nrows=0)
    known = {candidate for candidates in _TARGET_SOURCES.values() for candidate in candidates}
    usecols = [str(c) for c in header.columns if str(c).strip().lower() in known]
    if not usecols:
        yield pd.read_csv(path, low_memory=False)
        return

    dtype = {col: "category" for col in usecols if col.strip().lower() != "datadate"}
    # pandas-stubs' UsecolsArgType rejects a plain list[str], so no read_csv
    # overload matches once usecols is passed at all -- a stub limitation, not
    # a runtime one (same workaround as datastream_loader._read_extract).
    reader = cast(
        Iterator[pd.DataFrame],
        pd.read_csv(path, usecols=usecols, dtype=dtype, low_memory=False, chunksize=_CHUNK_ROWS),  # type: ignore[call-overload]
    )
    yield from reader


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
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


def _process_chunk(
    raw_chunk: pd.DataFrame,
) -> tuple[pd.DataFrame, int] | str:
    """Normalise, validate and dedupe one chunk. Returns an error string on failure."""
    normalized = _normalize_columns(raw_chunk)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if missing:
        return f"extract missing required column(s) after alias mapping: {missing}"

    as_of_date = cast(pd.Series, pd.to_datetime(normalized["as_of_date"], errors="coerce"))
    if bool(as_of_date.isna().any()):
        return "one or more rows have an unparseable date value; re-download with YYYY-MM-DD dates"

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "gvkey": normalized["gvkey"].astype(str).str.strip().str.zfill(6),
        "isin": normalized["isin"].astype(str).str.strip().str.upper().replace({"": None, "NAN": None}),
        "fic": (
            normalized["fic"].astype(str).str.strip().str.upper().replace({"": None, "NAN": None})
            if "fic" in normalized.columns
            else None
        ),
        "as_of_date": as_of_date,
        "source": "compustat_global_security",
        "ingested_at": now,
    })

    before = len(frame)
    frame = frame.drop_duplicates(subset=["gvkey", "isin", "fic", "as_of_date"])
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_security_identifiers_frame(frame)
    except SecurityIdentifiersSchemaError as exc:
        return str(exc)

    return validated, duplicate_rows_dropped


def load_security_identifiers_extract(
    file_path: Path | str,
    dry_run: bool = True,
) -> SecurityIdentifiersLoadResult:
    """Parse, validate and write a Compustat Global security-identifiers CSV.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.

    Processes and writes the extract one chunk at a time (see module
    docstring), so memory use stays flat regardless of file size. Returns a
    :class:`SecurityIdentifiersLoadResult`; a malformed extract comes back as
    ``ok=False`` with an explanatory ``error`` rather than a traceback.
    """
    path = Path(file_path)
    out_dir = get_panel_dir() / "security_identifiers_pit"
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"

    total_rows = 0
    gvkeys_seen: set[str] = set()
    duplicate_rows_dropped = 0
    chunk_index = 0

    try:
        for raw_chunk in _iter_raw_chunks(path):
            processed = _process_chunk(raw_chunk)
            if isinstance(processed, str):
                return SecurityIdentifiersLoadResult(ok=False, error=processed)
            validated, chunk_dupes = processed

            total_rows += len(validated)
            gvkeys_seen.update(cast(list, validated["gvkey"].unique().tolist()))
            duplicate_rows_dropped += chunk_dupes

            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                validated.to_parquet(out_dir / f"{safe_stem}.{chunk_index:04d}.parquet", index=False)
            chunk_index += 1
            del raw_chunk, validated
            if chunk_index % 50 == 0:
                # A universe-wide extract is thousands of chunks; letting
                # per-chunk garbage (categoricals, intermediate frames) pile
                # up before the collector notices is what pushed this over a
                # tight memory budget in practice. Periodic, not per-chunk --
                # gc.collect() itself isn't free.
                gc.collect()
    except Exception as exc:
        return SecurityIdentifiersLoadResult(ok=False, error=f"could not read {path}: {exc}")

    return SecurityIdentifiersLoadResult(
        ok=True,
        rows_loaded=total_rows,
        gvkeys=sorted(gvkeys_seen),
        duplicate_rows_dropped=duplicate_rows_dropped,
        chunks_processed=chunk_index,
    )


def read_security_identifiers(as_of: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """Read the security-identifiers panel, as of a date or in full.

    Args:
        as_of: when given, keep only the latest known (gvkey, isin) rows whose
            ``as_of_date`` is on or before this date -- i.e. the identifier
            mapping that was actually in force then. Leaving it ``None``
            returns the full daily history, which is correct for inspection
            and wrong for joining against a point-in-time price or
            fundamentals row.

    A universe-wide extract loads as one row per (gvkey, isin) *per day*
    (~16.7M rows in practice for one calendar year). Building a Python list of
    one :class:`pandas.DataFrame` per partition and then calling
    ``pd.concat`` -- the naive approach -- holds every partition's frame
    resident at once *and* needs a further ~2x the combined size at the
    moment of concatenation (a well-documented, unfixed ``pd.concat``
    behaviour: the result is built as one new contiguous block before the
    inputs can be freed). Combined with this schema's four nullable
    ``"string"`` columns -- pandas' most memory-expensive string
    representation, ~40-50 bytes of pure per-value overhead on top of the
    text itself -- that pattern was enough to exhaust memory on an ordinary
    16GB machine, and even a container bumped to 8GB. Instead, every
    partition is merged in Arrow's columnar form via
    ``pyarrow.dataset``, which reads and concatenates the underlying
    ``ChunkedArray``s without ever materialising 928 separate pandas frames,
    and the whole panel is converted to pandas exactly once, at the end. With
    ``as_of`` given, partitions are instead reduced one at a time: each is
    read, cut to rows on or before the cutoff, and folded into a running
    "latest state per (gvkey, isin)" accumulator bounded by the number of
    distinct identifiers (tens of thousands), not the number of dated rows
    (tens of millions). Partition filenames sort in the extract's original
    row order, which for a Compustat daily extract is chronological, so a
    partition entirely after the cutoff is skipped without being read at all.
    """
    out_dir = get_panel_dir() / "security_identifiers_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(SECURITY_IDENTIFIERS_COLUMNS))

    if as_of is None:
        table = pa_dataset.dataset([str(p) for p in paths], format="parquet").to_table()
        combined = table.to_pandas(types_mapper=_string_types_mapper)
        del table
        # Cheap relative to the string columns above (8 bytes/value, no
        # per-value Python object) -- only these two need normalising, since
        # a parquet part may have been written at a different timestamp
        # resolution than pandas' default 'ns'.
        combined["as_of_date"] = combined["as_of_date"].astype("datetime64[ns]")
        combined["ingested_at"] = combined["ingested_at"].astype("datetime64[ns]")
        # gvkey is the one string column another loader joins against:
        # pit_fundamentals.py's second merge_asof passes it as `by=`, which
        # requires an exact dtype match against fundamentals_schema's own
        # "gvkey": "string" column. Cast back so that join keeps working --
        # a single-column cost, not the whole frame.
        combined["gvkey"] = combined["gvkey"].astype("string")
        # Confirmed single-valued by the schema (SECURITY_IDENTIFIERS_SOURCES
        # has exactly one entry) -- a near-free memory win, safe to apply here
        # since nothing joins or filters on it downstream.
        combined["source"] = combined["source"].astype("category")
        combined = combined.drop_duplicates(subset=["gvkey", "isin", "fic", "as_of_date"])
        return combined.sort_values(["gvkey", "as_of_date"]).reset_index(drop=True)

    cutoff = pd.Timestamp(as_of)
    accumulator: pd.DataFrame | None = None
    for p in paths:
        part_min = cast(pd.Timestamp, pd.read_parquet(p, columns=["as_of_date"])["as_of_date"].min())
        if bool(pd.isna(part_min)) or part_min > cutoff:
            continue
        part = pd.read_parquet(p)
        part = cast(pd.DataFrame, part[part["as_of_date"] <= cutoff])
        if part.empty:
            continue
        merged = pd.concat([accumulator, part], ignore_index=True) if accumulator is not None else part
        merged = merged.sort_values("as_of_date")
        accumulator = cast(
            pd.DataFrame, merged.drop_duplicates(subset=["gvkey", "isin"], keep="last")
        )
    if accumulator is None:
        return pd.DataFrame(columns=pd.Index(SECURITY_IDENTIFIERS_COLUMNS))
    return accumulator.sort_values(["gvkey", "as_of_date"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a Compustat Global security-identifiers CSV into the PIT Parquet panel."
    )
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_security_identifiers_extract(args.csv_path, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info(
        "%s: %d rows, %d companies, %d chunk(s)%s",
        mode,
        result.rows_loaded,
        len(result.gvkeys),
        result.chunks_processed,
        suffix,
    )
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)


if __name__ == "__main__":
    _main()
