"""Loader for a manually-exported WRDS IBES Summary Statistics extract.

Companion to :mod:`fundamentals_loader` -- both feed point-in-time factor
research -- but this one carries analyst consensus estimates
(``ibes.statsum_epsus``: median/mean/std/count of analyst EPS forecasts per
security per forecast period per month) rather than reported accounting
figures.

**Chunked by construction**, like :mod:`security_identifiers_loader`. A
full-history US extract at the annual forecast horizon alone runs to several
million rows (WRDS documentation puts it in the 3.5-4.2M range for FPI='1'
EPS estimates back to 1983; an "everything" extract across all measures and
forecast horizons runs into the tens of millions). Reading that whole file
with a single ``pd.read_csv`` risks the same
``numpy._core._exceptions._ArrayMemoryError`` :mod:`security_identifiers_loader`
already hit once with a much smaller extract, so the CSV is read and written
``_CHUNK_ROWS`` rows at a time here too.

**``read_ibes_estimates`` intentionally stays simple** (a plain
``pd.concat`` over partitions, mirroring ``fundamentals_loader``) rather than
pre-emptively adopting ``security_identifiers_loader``'s Arrow-native /
Arrow-dtype read path. That optimisation was earned the hard way against a
16.7M-row *real* extract; IBES's actual per-extract size here is not yet
known (it depends on which forecast horizons and measures are pulled from
WRDS), so building the more complex read path before there is a real extract
to verify it against would be guessing. If a full-history read of the real
data OOMs, apply the same fix documented in ``security_identifiers_loader``'s
module docstring rather than re-deriving it.

**The ticker trap and the street-vs-GAAP trap** are both documented in
:mod:`ibes_estimates_schema` -- read that module docstring before joining
this panel to anything else.

**fpi/measure are query filters, not output columns, in WRDS's own UI.**
The Summary Statistics web query tool's Step 2 has you *check* a Measure
(e.g. EPS) and an FPI (e.g. Fiscal Year 1) to scope the extract, but its
Step 3 variable picker never offers ``fpi``/``measure`` as selectable output
columns -- a real extract built that way simply has no such columns. Rather
than force every user to notice and hand-edit the CSV, :func:`load_ibes_estimates_extract`
accepts ``fpi``/``measure`` overrides (also `--fpi`/`--measure` on the CLI)
that stamp the constant value you filtered to onto every row when the
column is absent. Only fails if the column is missing *and* no override was
given.
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
from app.foundation.data_engineering.ibes_estimates_schema import (
    IBES_ESTIMATES_COLUMNS,
    IbesEstimatesSchemaError,
    validate_ibes_estimates_frame,
)
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

_CHUNK_ROWS = 20_000

# Candidate source headers per target column, highest precedence first.
# WRDS's own query tool exports these lowercase mnemonics unchanged; no
# alternate dialect is documented for this table the way Compustat has
# annual/quarterly mnemonic pairs, so each target has a single candidate.
_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "ticker": ("ticker",),
    "cusip": ("cusip",),
    "oftic": ("oftic",),
    "statpers": ("statpers",),
    "fpedats": ("fpedats",),
    "fpi": ("fpi",),
    "measure": ("measure",),
    "numest": ("numest",),
    "medest": ("medest",),
    "meanest": ("meanest",),
    "stdev": ("stdev",),
    "highest": ("highest",),
    "lowest": ("lowest",),
    "actual": ("actual",),
    "anndats_act": ("anndats_act",),
    "curr": ("curr",),
    "usfirm": ("usfirm",),
}

_REQUIRED_TARGET_COLUMNS = ("ticker", "statpers", "fpedats")

_NUMERIC_COLUMNS = ("numest", "medest", "meanest", "stdev", "highest", "lowest", "actual", "usfirm")


@dataclass
class IbesEstimatesLoadResult:
    ok: bool
    rows_loaded: int = 0
    tickers: list[str] = field(default_factory=list)
    error: str | None = None
    duplicate_rows_dropped: int = 0
    chunks_processed: int = 0


def _iter_raw_chunks(path: Path) -> Iterator[pd.DataFrame]:
    """Yield the extract in ``_CHUNK_ROWS``-row pieces, columns restricted.

    Mirrors ``security_identifiers_loader._iter_raw_chunks``.
    """
    header = pd.read_csv(path, nrows=0)
    known = {candidate for candidates in _TARGET_SOURCES.values() for candidate in candidates}
    usecols = [str(c) for c in header.columns if str(c).strip().lower() in known]
    if not usecols:
        yield pd.read_csv(path, low_memory=False)
        return

    reader = cast(
        Iterator[pd.DataFrame],
        pd.read_csv(path, usecols=usecols, low_memory=False, chunksize=_CHUNK_ROWS),  # type: ignore[call-overload]
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
    fpi_override: str | None = None,
    measure_override: str | None = None,
) -> tuple[pd.DataFrame, int] | str:
    """Normalise, validate and dedupe one chunk. Returns an error string on failure."""
    normalized = _normalize_columns(raw_chunk)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if missing:
        return f"extract missing required column(s) after alias mapping: {missing}"

    if "fpi" not in normalized.columns and fpi_override is None:
        return (
            "extract has no fpi column and no --fpi override was given; WRDS's Summary "
            "Statistics query tool treats FPI as a query filter, not an output column, so "
            "pass the value you filtered to in Step 2 (e.g. --fpi 1)"
        )
    if "measure" not in normalized.columns and measure_override is None:
        return (
            "extract has no measure column and no --measure override was given; WRDS's "
            "Summary Statistics query tool treats Measure as a query filter, not an output "
            "column, so pass the value you filtered to in Step 2 (e.g. --measure EPS)"
        )

    statpers = cast(pd.Series, pd.to_datetime(normalized["statpers"], errors="coerce"))
    if bool(statpers.isna().any()):
        return "one or more rows have an unparseable statpers value; re-download with YYYY-MM-DD dates"
    fpedats = cast(pd.Series, pd.to_datetime(normalized["fpedats"], errors="coerce"))

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "ticker": normalized["ticker"].astype(str).str.strip(),
        "cusip": (
            normalized["cusip"].astype(str).str.strip().replace({"": None, "NAN": None})
            if "cusip" in normalized.columns
            else None
        ),
        "oftic": (
            normalized["oftic"].astype(str).str.strip().replace({"": None, "NAN": None})
            if "oftic" in normalized.columns
            else None
        ),
        "statpers": statpers,
        "fpedats": fpedats,
        "fpi": (
            normalized["fpi"].astype(str).str.strip().str.upper()
            if "fpi" in normalized.columns
            else pd.Series([str(fpi_override).strip().upper()] * len(normalized), index=normalized.index)
        ),
        "measure": (
            normalized["measure"].astype(str).str.strip().str.upper()
            if "measure" in normalized.columns
            else pd.Series([str(measure_override).strip().upper()] * len(normalized), index=normalized.index)
        ),
        "anndats_act": (
            pd.to_datetime(normalized["anndats_act"], errors="coerce")
            if "anndats_act" in normalized.columns
            else pd.NaT
        ),
        "curr": (
            normalized["curr"].astype(str).str.strip().str.upper().replace({"": None, "NAN": None})
            if "curr" in normalized.columns
            else None
        ),
        "source": "ibes_summary_epsus",
        "ingested_at": now,
    })
    for col in _NUMERIC_COLUMNS:
        frame[col] = (
            pd.to_numeric(normalized[col], errors="coerce") if col in normalized.columns else float("nan")
        )

    before = len(frame)
    frame = frame.drop_duplicates(subset=["ticker", "statpers", "fpedats", "fpi", "measure"])
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_ibes_estimates_frame(frame)
    except IbesEstimatesSchemaError as exc:
        return str(exc)

    return validated, duplicate_rows_dropped


def load_ibes_estimates_extract(
    file_path: Path | str,
    dry_run: bool = True,
    fpi: str | None = None,
    measure: str | None = None,
) -> IbesEstimatesLoadResult:
    """Parse, validate and write a WRDS IBES Summary Statistics CSV.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.
        fpi: forecast-period indicator to stamp on every row when the
            extract has no ``fpi`` column. WRDS's Summary Statistics web
            query tool applies FPI as a query filter (checked in its Step 2
            form) rather than offering it as a selectable output column, so
            a real extract commonly needs this supplied out-of-band. Ignored
            if the extract does have an ``fpi`` column.
        measure: same idea as ``fpi``, for the ``measure`` column (e.g.
            ``"EPS"``).

    Processes the extract one chunk at a time (see module docstring), so
    memory use stays flat regardless of file size. Returns an
    :class:`IbesEstimatesLoadResult`; a malformed extract comes back as
    ``ok=False`` with an explanatory ``error`` rather than a traceback.
    """
    path = Path(file_path)
    out_dir = get_panel_dir() / "ibes_estimates_pit"
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"

    total_rows = 0
    tickers_seen: set[str] = set()
    duplicate_rows_dropped = 0
    chunk_index = 0

    try:
        for raw_chunk in _iter_raw_chunks(path):
            processed = _process_chunk(raw_chunk, fpi_override=fpi, measure_override=measure)
            if isinstance(processed, str):
                return IbesEstimatesLoadResult(ok=False, error=processed)
            validated, chunk_dupes = processed

            total_rows += len(validated)
            tickers_seen.update(cast(list, validated["ticker"].unique().tolist()))
            duplicate_rows_dropped += chunk_dupes

            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                validated.to_parquet(out_dir / f"{safe_stem}.{chunk_index:04d}.parquet", index=False)
            chunk_index += 1
            del raw_chunk, validated
            if chunk_index % 50 == 0:
                gc.collect()
    except Exception as exc:
        return IbesEstimatesLoadResult(ok=False, error=f"could not read {path}: {exc}")

    return IbesEstimatesLoadResult(
        ok=True,
        rows_loaded=total_rows,
        tickers=sorted(tickers_seen),
        duplicate_rows_dropped=duplicate_rows_dropped,
        chunks_processed=chunk_index,
    )


def read_ibes_estimates(
    as_of: pd.Timestamp | str | None = None,
    fpi: str | None = None,
    measure: str | None = None,
) -> pd.DataFrame:
    """Read every loaded IBES estimates partition back as one frame.

    Args:
        as_of: when given, keep only rows whose ``statpers`` is on or before
            this date -- i.e. the consensus snapshot that was actually
            knowable then. Leaving it ``None`` returns the whole panel.
        fpi: when given, keep only this forecast-period indicator (e.g.
            ``"1"`` for next-fiscal-year, the standard choice for annual
            earnings-surprise research). See
            :data:`ibes_estimates_schema.IBES_FPI_VALUES`.
        measure: when given, keep only this measure (e.g. ``"EPS"``).

    Later extracts win on a repeated (ticker, statpers, fpedats, fpi,
    measure), so re-loading a corrected export supersedes the earlier one.
    """
    out_dir = get_panel_dir() / "ibes_estimates_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(IBES_ESTIMATES_COLUMNS))

    combined = read_parquet_partitions(paths)
    combined = combined.sort_values("ingested_at")
    combined = cast(
        pd.DataFrame,
        combined.drop_duplicates(subset=["ticker", "statpers", "fpedats", "fpi", "measure"], keep="last"),
    )

    if fpi is not None:
        combined = cast(pd.DataFrame, combined[combined["fpi"] == fpi])
    if measure is not None:
        combined = cast(pd.DataFrame, combined[combined["measure"] == measure.upper()])
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        combined = cast(pd.DataFrame, combined[combined["statpers"] <= cutoff])

    return combined.sort_values(["ticker", "statpers"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a WRDS IBES Summary Statistics CSV into the PIT Parquet panel."
    )
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    parser.add_argument(
        "--fpi",
        default=None,
        help="forecast-period indicator to stamp on every row if the extract has no fpi column "
        "(WRDS applies it as a query filter, not an output column) -- e.g. --fpi 1",
    )
    parser.add_argument(
        "--measure",
        default=None,
        help="measure to stamp on every row if the extract has no measure column -- e.g. --measure EPS",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_ibes_estimates_extract(args.csv_path, dry_run=not args.apply, fpi=args.fpi, measure=args.measure)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info(
        "%s: %d rows, %d tickers, %d chunk(s)%s",
        mode,
        result.rows_loaded,
        len(result.tickers),
        result.chunks_processed,
        suffix,
    )
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)


if __name__ == "__main__":
    _main()
