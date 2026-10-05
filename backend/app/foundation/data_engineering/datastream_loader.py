"""Loader for a manually-exported Refinitiv/LSEG Datastream point-in-time extract.

ADR 0015 Phase 3 calls for a "one-time Datastream PIT extract" of the STOXX
Europe 600 universe, but the owner's Datastream access (an institutional
licence that expires) is not yet set up. Per that decision, this module ships as a
ready, tested ingestion path -- not a live pull. Whenever access exists, the
owner exports a CSV (Datastream Excel Add-in or the WRDS DSWS web interface
both produce this shape) and hands it to ``load_datastream_extract``.

Expected CSV columns (case-insensitive): ``Code`` (RIC/Datastream code ->
``symbol``), ``ISIN`` (optional), ``Date``, ``Open``, ``High``, ``Low``,
``Close``, ``Volume`` (optional), ``Currency`` (optional, defaults to
``"EUR"`` -- the STOXX Europe 600 is a Eurozone-plus universe).

Native Datastream mnemonics (``PO``/``PH``/``PL``/``P``/``VO``, and their
unpadded ``#T`` forms) and Compustat Global ``g_secd`` column names
(``prcod``/``prchd``/``prcld``/``prccd``, ``cshtrd``, ``curcdd``,
``datadate``) are accepted as aliases, so an extract can be loaded without
renaming columns by hand first. See ``_TARGET_SOURCES`` for the precedence
rules and the caveats on each dialect.

If the extract carries a Compustat-style ``ajexdi`` adjustment factor, prices
are divided by it and volume multiplied by it before validation -- see
``_apply_split_adjustment``. Currency is stored as quoted and never converted.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.panel_schema import PanelSchemaError, validate_panel_frame
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

# Candidate source headers for each panel column, highest precedence first.
#
# Three export dialects land here, so most targets have several spellings:
#
# * Plain headers -- ``Open``/``High``/``Low``/``Close``/``Volume``. What a
#   Datastream request with "Display Custom Header" set produces, and what any
#   hand-tidied sheet looks like.
# * Datastream mnemonics -- ``PO``/``PH``/``PL``/``P`` for open/high/low/close
#   and ``VO`` for volume. The ``#T`` variants outrank the bare ones: per EDSC's
#   "Stock prices in Refinitiv Datastream" guide, a bare ``P`` pads the last
#   known value forward once a company delists or merges, which fabricates flat
#   price history for precisely the dead names a point-in-time panel exists to
#   capture. ``P#T`` leaves the gap.
# * Compustat Global ``g_secd`` -- ``prcod``/``prchd``/``prcld``/``prccd``,
#   ``cshtrd``, ``curcdd``, ``datadate``. Note these are UNADJUSTED prices in
#   local currency: divide by ``ajexdi`` for splits before loading, and mind
#   that UK lines arrive in GBp (pence). This loader neither adjusts nor
#   converts -- it stores what it is given.
#
# Resolution is keyed by target rather than by source so an extract carrying two
# spellings of one column (``Close`` and ``P``, or ``gvkey`` and ``tic``)
# collapses to a single column. Mapping source->target instead let both win the
# same name, and the duplicate then crashed DataFrame construction below with an
# opaque "Data must be 1-dimensional" -- outside ``LoadResult``, so the caller
# got a traceback rather than the documented structured failure.
_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "code", "ric", "tic", "gvkey"),
    "isin": ("isin",),
    "as_of_date": ("as_of_date", "date", "datadate"),
    "open": ("open", "prcod", "po#t", "po"),
    "high": ("high", "prchd", "ph#t", "ph"),
    "low": ("low", "prcld", "pl#t", "pl"),
    "close": ("close", "prccd", "p#t", "p", "pi"),
    "volume": ("volume", "cshtrd", "vo#t", "vo"),
    "currency": ("currency", "curcdd", "ccy"),
    # Not a panel column -- consumed and dropped by the split adjustment below.
    "adjustment_factor": ("ajexdi", "adjustment_factor", "adj_factor"),
}

_REQUIRED_TARGET_COLUMNS = ("symbol", "as_of_date", "open", "high", "low", "close")


@dataclass
class LoadResult:
    ok: bool
    rows_loaded: int = 0
    symbols: list[str] = field(default_factory=list)
    error: str | None = None
    # True when an ``ajexdi``-style column was present and applied. False means
    # the prices went in exactly as exported -- correct for Datastream's ``P``
    # (already adjusted for capital changes), wrong for raw Compustat
    # ``prccd`` (not adjusted at all).
    split_adjusted: bool = False


def _read_extract(path: Path) -> pd.DataFrame:
    """Read an extract CSV, keeping only columns the alias map can use.

    A universe-wide Compustat Global year is ~18.5M rows and the export
    carries columns this panel has no use for (``iid``, ``conm``, ``exchg``
    and friends). Reading those too costs gigabytes of Python strings for
    nothing, so the header is inspected first and ``usecols`` restricts the
    real read. The repeated string columns are held as categories, which is
    where most of the remaining saving comes from: 74,000 distinct symbols
    across 18.5M rows is a lot of duplicated text.

    Falls back to reading everything when no column is recognised, so the
    "missing required column" error downstream describes the actual file
    rather than an empty frame.
    """
    header = pd.read_csv(path, nrows=0)
    known = {candidate for candidates in _TARGET_SOURCES.values() for candidate in candidates}
    usecols: list[str] = [str(c) for c in header.columns if str(c).strip().lower() in known]
    if not usecols:
        return pd.read_csv(path, low_memory=False)

    # Only the columns that repeat heavily; prices and volume stay numeric.
    categorical_targets = ("symbol", "isin", "currency")
    repeated = {
        candidate
        for target in categorical_targets
        for candidate in _TARGET_SOURCES[target]
    }
    dtypes: dict[str, str] = {c: "category" for c in usecols if c.strip().lower() in repeated}
    # pandas-stubs' UsecolsArgType rejects a plain list[str], so no read_csv
    # overload matches once usecols is passed at all -- a stub limitation, not
    # a runtime one (verified against this pandas version by the scale test in
    # tests/test_data_engineering_datastream_loader.py).
    return cast(
        pd.DataFrame,
        pd.read_csv(path, usecols=usecols, dtype=dtypes, low_memory=False),  # type: ignore[call-overload]
    )


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename recognised export headers to panel column names.

    For each panel column the first candidate header present wins; the losing
    spellings keep their original names and are simply ignored downstream, so an
    extract carrying several spellings of one column can never produce a
    duplicate.
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


class _AdjustmentError(ValueError):
    """Raised when an adjustment-factor column is present but unusable."""


def _apply_split_adjustment(df: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """Divide prices (and multiply volume) by a Compustat-style split factor.

    Compustat Global ``g_secd`` reports ``prccd`` unadjusted, alongside the
    cumulative adjustment factor ``ajexdi``; the vendor-documented conversion
    is ``price / ajexdi`` and ``shares * ajexdi``. Without it a 3-for-1 split
    reads as a -67% day, which corrupts momentum -- the one factor that is
    otherwise immune to how the extract was denominated.

    Datastream's default ``P`` series is already adjusted for capital changes
    and carries no such column, so nothing happens for those extracts.

    A missing or non-positive factor fails the whole load rather than leaving
    that row unadjusted: a panel where *some* rows are split-adjusted is the
    silent-corruption case this exists to prevent, and the fix belongs in the
    extract query (add ``and ajexdi > 0``), where it is one clause.
    """
    if "adjustment_factor" not in df.columns:
        return df, False

    out = df.copy()
    factor = cast(pd.Series, pd.to_numeric(out["adjustment_factor"], errors="coerce"))
    bad = factor.isna() | (factor <= 0)
    if bool(bad.any()):
        offenders = sorted({str(v) for v in out.loc[bad, "symbol"]})
        raise _AdjustmentError(
            f"{int(bad.sum())} row(s) have a missing or non-positive adjustment factor "
            f"(symbols: {offenders[:10]}); re-export with a positive factor on every row, "
            "or drop the adjustment-factor column to load prices as-is"
        )

    for col in ("open", "high", "low", "close"):
        out[col] = cast(pd.Series, pd.to_numeric(out[col], errors="coerce")) / factor
    if "volume" in out.columns:
        out["volume"] = cast(pd.Series, pd.to_numeric(out["volume"], errors="coerce")) * factor
    return out.drop(columns=["adjustment_factor"]), True


def load_datastream_extract(file_path: Path | str, dry_run: bool = True) -> LoadResult:
    """Parse, validate, and merge a Datastream CSV export into the PIT panel.

    Fails loud (returns ``LoadResult(ok=False, error=...)``) on a malformed
    file rather than silently importing a partial result -- the ADR 0014
    lesson about bare excepts swallowing schema breaks applies here too.
    """
    path = Path(file_path)
    try:
        raw = _read_extract(path)
    except Exception as exc:
        return LoadResult(ok=False, error=f"could not read {path}: {exc}")

    normalized = _normalize_columns(raw)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if missing:
        return LoadResult(ok=False, error=f"extract missing required column(s) after alias mapping: {missing}")

    try:
        normalized, split_adjusted = _apply_split_adjustment(normalized)
    except _AdjustmentError as exc:
        return LoadResult(ok=False, error=str(exc))

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "symbol": normalized["symbol"].astype(str),
        "isin": normalized["isin"] if "isin" in normalized.columns else None,
        "as_of_date": pd.to_datetime(normalized["as_of_date"], errors="coerce"),
        "open": normalized["open"],
        "high": normalized["high"],
        "low": normalized["low"],
        "close": normalized["close"],
        "volume": normalized["volume"] if "volume" in normalized.columns else None,
        "currency": normalized["currency"] if "currency" in normalized.columns else "EUR",
        "source": "datastream_pit",
        "ingested_at": now,
    })

    if bool(frame["as_of_date"].isna().any()):
        return LoadResult(ok=False, error="one or more rows have an unparseable Date value")

    try:
        validated = validate_panel_frame(frame)
    except PanelSchemaError as exc:
        return LoadResult(ok=False, error=str(exc))

    # One bar per symbol per day, or the panel silently keeps an arbitrary one.
    # The usual cause is a Compustat Global extract carrying every issue
    # (``iid``) of a company rather than the primary one: same ``tic``, same
    # ``datadate``, several rows.
    dupes = validated.duplicated(subset=["symbol", "as_of_date"], keep=False)
    if bool(dupes.any()):
        offenders = sorted({str(v) for v in validated.loc[dupes, "symbol"]})
        return LoadResult(
            ok=False,
            error=(
                f"{int(dupes.sum())} row(s) share a (symbol, date) with another row "
                f"(symbols: {offenders[:10]}); the extract has more than one series per symbol per day "
                "-- restrict it to a single issue/line before loading"
            ),
        )

    symbols = sorted(cast(list, validated["symbol"].unique().tolist()))
    if not dry_run:
        panel_dir = get_panel_dir() / "datastream_pit"
        panel_dir.mkdir(parents=True, exist_ok=True)
        # groupby, not a boolean filter per symbol. ``validated[validated["symbol"]
        # == s]`` rescans every row once per symbol, so the cost is O(rows x
        # symbols): measured at 0.77s for 250 symbols, 45s for 2,000, and
        # quadratic in between -- which extrapolates to ~17 hours for one year
        # of Compustat Global (~74,000 securities), for byte-identical output.
        # groupby partitions in a single pass. One file per symbol is kept
        # deliberately: re-loading a corrected extract must overwrite that
        # symbol's partition by name, which a bundled file would not do.
        for symbol, symbol_frame in validated.groupby("symbol", sort=False):
            safe_name = str(symbol).replace("/", "_")
            cast(pd.DataFrame, symbol_frame).to_parquet(panel_dir / f"{safe_name}.parquet", index=False)

    return LoadResult(ok=True, rows_loaded=len(validated), symbols=symbols, split_adjusted=split_adjusted)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Load an extract CSV into the PIT Parquet panel.")
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_datastream_extract(args.csv_path, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info(
        "%s: %d rows, %d symbols, split-adjusted=%s%s",
        mode,
        result.rows_loaded,
        len(result.symbols),
        result.split_adjusted,
        suffix,
    )
    logger.info("  symbols: %s%s", result.symbols[:20], " ..." if len(result.symbols) > 20 else "")


if __name__ == "__main__":
    _main()
