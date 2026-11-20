"""Loader for a manually-exported WRDS CRSP ticker-history extract.

Companion to :mod:`ccm_link_loader`. The CCM link history maps ``gvkey`` <->
``permno``; this panel maps ``permno`` <-> ticker over time, which closes the
chain from an app symbol (``AAPL``) to a Compustat ``gvkey`` for US
securities that were never resolved through this app's own security master
(see ``pit_panel_joins._resolve_gvkey_as_of``).

Accepts either CRSP format's names table, exported from the WRDS web query
form as CSV:

- CIZ / "Version 2" ``crsp.stksecurityinfohist``: ``permno``, ``ticker``,
  ``tradingsymbol``, ``secinfostartdt``, ``secinfoenddt``.
- Legacy SIZ ``crsp.stocknames`` / ``crsp.dsenames``: ``permno``, ``ticker``,
  ``tsymbol``, ``namedt``, ``nameenddt`` (``nameendt`` in dsenames).

A row with neither a ticker nor a trading symbol carries no information for
this panel's one purpose and is dropped (counted, not an error). A blank or
``9999-12-31``-style far-future end date is kept as-is -- both mean "still
current" to the join.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.crsp_names_schema import (
    CRSP_NAMES_COLUMNS,
    CrspNamesSchemaError,
    validate_crsp_names_frame,
)
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "permno": ("permno", "lpermno"),
    "ticker": ("ticker", "htick"),
    "trading_symbol": ("tradingsymbol", "tsymbol", "htsymbol"),
    "namedt": ("namedt", "secinfostartdt", "begdt"),
    "nameenddt": ("nameenddt", "nameendt", "secinfoenddt", "enddt"),
}

_REQUIRED_TARGET_COLUMNS = ("permno", "namedt")

_DEDUP_KEY = ["permno", "namedt", "ticker", "trading_symbol"]


@dataclass
class CrspNamesLoadResult:
    ok: bool
    rows_loaded: int = 0
    permnos: int = 0
    error: str | None = None
    rows_without_ticker_dropped: int = 0
    duplicate_rows_dropped: int = 0


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


def _clean_symbol(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.strip().str.upper()
    return cleaned.mask(cleaned == "", pd.NA)


def load_crsp_names_extract(file_path: Path | str, dry_run: bool = True) -> CrspNamesLoadResult:
    """Parse, validate and write a WRDS CRSP names CSV export.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.
    """
    path = Path(file_path)
    try:
        raw = pd.read_csv(path, low_memory=False, dtype=str)
    except Exception as exc:
        return CrspNamesLoadResult(ok=False, error=f"could not read {path}: {exc}")

    normalized = _normalize_columns(raw)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if "ticker" not in normalized.columns and "trading_symbol" not in normalized.columns:
        missing.append("ticker or tradingsymbol/tsymbol")
    if missing:
        return CrspNamesLoadResult(
            ok=False, error=f"extract missing required column(s) after alias mapping: {missing}"
        )

    n = len(normalized)
    empty = pd.Series([pd.NA] * n, index=normalized.index, dtype="string")
    ticker = _clean_symbol(cast(pd.Series, normalized["ticker"])) if "ticker" in normalized.columns else empty
    trading_symbol = (
        _clean_symbol(cast(pd.Series, normalized["trading_symbol"])) if "trading_symbol" in normalized.columns else empty
    )

    namedt = cast(pd.Series, pd.to_datetime(normalized["namedt"], errors="coerce"))
    if bool(namedt.isna().any()):
        return CrspNamesLoadResult(
            ok=False, error="one or more rows have an unparseable start date; re-download with YYYY-MM-DD dates"
        )
    end_raw = normalized["nameenddt"] if "nameenddt" in normalized.columns else empty
    end_stripped = end_raw.astype("string").str.strip().fillna("")
    # Far-future sentinels (9999-12-31) overflow datetime64[ns]; they mean
    # "still current", exactly like a blank cell.
    open_ended = (end_stripped == "") | end_stripped.str.startswith("9999")
    nameenddt = cast(pd.Series, pd.to_datetime(end_raw.mask(open_ended, None), errors="coerce"))
    if bool((~open_ended & nameenddt.isna()).any()):
        return CrspNamesLoadResult(
            ok=False, error="one or more rows have an unparseable end date; leave blank for a current name"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "permno": pd.to_numeric(normalized["permno"], errors="coerce"),
        "ticker": ticker,
        "trading_symbol": trading_symbol,
        "namedt": namedt,
        "nameenddt": nameenddt,
        "source": "crsp_security_names",
        "ingested_at": now,
    })

    has_symbol = frame["ticker"].notna() | frame["trading_symbol"].notna()
    rows_without_ticker = int((~has_symbol).sum())
    frame = cast(pd.DataFrame, frame[has_symbol])

    before = len(frame)
    frame = frame.drop_duplicates(subset=_DEDUP_KEY)
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_crsp_names_frame(frame)
    except CrspNamesSchemaError as exc:
        return CrspNamesLoadResult(ok=False, error=str(exc))

    if not dry_run:
        out_dir = get_panel_dir() / "crsp_names_pit"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"
        validated.to_parquet(out_dir / f"{safe_stem}.parquet", index=False)

    return CrspNamesLoadResult(
        ok=True,
        rows_loaded=len(validated),
        permnos=int(validated["permno"].nunique()),
        rows_without_ticker_dropped=rows_without_ticker,
        duplicate_rows_dropped=duplicate_rows_dropped,
    )


def read_crsp_names() -> pd.DataFrame:
    """Every loaded CRSP names partition as one frame (later extract wins)."""
    out_dir = get_panel_dir() / "crsp_names_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(CRSP_NAMES_COLUMNS))
    combined = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    combined = combined.sort_values("ingested_at", kind="stable")
    combined = cast(pd.DataFrame, combined.drop_duplicates(subset=_DEDUP_KEY, keep="last"))
    return combined.sort_values(["permno", "namedt"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Load a WRDS CRSP ticker-history CSV into the PIT Parquet panel.")
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_crsp_names_extract(args.csv_path, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info("%s: %d rows, %d permnos%s", mode, result.rows_loaded, result.permnos, suffix)
    if result.rows_without_ticker_dropped:
        logger.info("  rows with no ticker dropped: %d", result.rows_without_ticker_dropped)
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)


if __name__ == "__main__":
    _main()
