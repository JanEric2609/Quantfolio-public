"""Builds/refreshes the point-in-time Parquet panel from internal sources.

Reads ``bar_prices`` via ``BarStore`` (the same read API ``market.history()``
and the rest of the app use -- no parallel raw-SQL path), tags rows
``source="internal"``, and writes them into the shared PIT panel directory
(``paths.get_panel_dir()``) alongside whatever ``datastream_loader.py`` has
separately merged in. Idempotent: each run overwrites a symbol's partition
file with a fresh read, so re-running is always safe.

Not part of any migration or scheduled job -- run manually:
``python -m app.foundation.data_engineering.panel_export --apply``, mirroring
``backend/scripts/backfill_bar_prices.py``'s dry-run-by-default convention.
"""
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_backbone import BarStore
from app.foundation.data_engineering.panel_schema import validate_panel_frame
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)


def _bars_to_panel_frame(symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
    now = datetime.now(UTC).replace(tzinfo=None)
    return pd.DataFrame({
        "symbol": symbol,
        "isin": pd.Series([None] * len(bars), dtype="object"),
        "as_of_date": bars["ts"],
        "open": bars["open"],
        "high": bars["high"],
        "low": bars["low"],
        "close": bars["close"],
        "volume": bars["volume"],
        "currency": bars["currency"],
        "source": "internal",
        "ingested_at": now,
    })


def export_panel(db: Session, symbols: list[str] | None = None, dry_run: bool = True) -> dict[str, int]:
    """Export ``bar_prices`` coverage into the PIT Parquet panel.

    Returns ``{symbol: row_count}`` for rows written (or that would be
    written, under ``dry_run``). Each symbol becomes its own partition file
    (``<panel_dir>/internal/<symbol>.parquet``) so re-exporting one symbol
    never touches another's file.
    """
    store = BarStore(db)
    targets = symbols if symbols is not None else store.list_symbols_with_data()
    panel_dir = get_panel_dir() / "internal"

    counts: dict[str, int] = {}
    for symbol in targets:
        bars = store.get_bars(symbol)
        if bars is None or bars.empty:
            counts[symbol] = 0
            continue
        frame = validate_panel_frame(_bars_to_panel_frame(symbol, bars))
        counts[symbol] = len(frame)
        if not dry_run:
            panel_dir.mkdir(parents=True, exist_ok=True)
            safe_name = symbol.replace("/", "_")
            frame.to_parquet(panel_dir / f"{safe_name}.parquet", index=False)

    return counts


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write files (default: dry-run, prints row counts only)")
    parser.add_argument(
        "--symbol", action="append", help="limit to specific symbol(s); default: all symbols with bar_prices coverage"
    )
    args = parser.parse_args()

    from app.foundation.core.db import SessionLocal

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        counts = export_panel(db, symbols=args.symbol, dry_run=not args.apply)

    total = sum(counts.values())
    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info("%s: %d symbols, %d total rows%s", mode, len(counts), total, suffix)
    for symbol, count in sorted(counts.items()):
        logger.info("  %s: %d rows", symbol, count)


if __name__ == "__main__":
    _main()
