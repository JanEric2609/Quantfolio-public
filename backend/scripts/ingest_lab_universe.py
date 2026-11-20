"""One-off ingestion: pull bar_prices for quant_lab's practical universe and
export the PIT Parquet panel.

ADR 0015 Phase 4. bar_prices coverage today is purely demand-driven by the
user's real DKB holdings; this script backfills the lab's 69-ticker
STOXX-proxy universe (``app.lab.quant_lab.universe.LAB_UNIVERSE``) so
the signal batch (``signal_batch.py``) has real cross-sectional data to
evaluate, then runs ``data_engineering.panel_export.export_panel`` to
publish it into the PIT panel. Uses the existing per-symbol
``DataIngester.ingest_bar_prices`` -- no new ingestion mechanism.

Not part of any migration or scheduled job. Dry-run by default (prints the
symbol list and count only); pass --apply to actually ingest + export.

Usage (run from backend/):
    .venv/bin/python scripts/ingest_lab_universe.py            # dry run
    .venv/bin/python scripts/ingest_lab_universe.py --apply    # writes
    .venv/bin/python scripts/ingest_lab_universe.py --apply --symbol ASML.AS --symbol OR.PA
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.data_backbone import DataIngester  # noqa: E402
from app.foundation.data_engineering.panel_export import export_panel  # noqa: E402
from app.lab.quant_lab.universe import LAB_UNIVERSE  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    parser.add_argument("--symbol", action="append", help="limit to specific symbol(s); default: LAB_UNIVERSE")
    parser.add_argument("--days", type=int, default=365 * 2, help="days of history to pull per symbol (default: 2 years)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    symbols = args.symbol if args.symbol else LAB_UNIVERSE
    print(f"{'APPLYING' if args.apply else 'DRY-RUN'}: {len(symbols)} symbols")
    if not args.apply:
        print("Dry run — pass --apply to ingest + export.")
        for s in symbols:
            print(f"  {s}")
        return

    db = SessionLocal()
    try:
        ingester = DataIngester(db)
        for symbol in symbols:
            try:
                result = ingester.ingest_bar_prices(symbol, days=args.days)
                logger.info("%s: %s", symbol, result)
            except Exception:
                logger.exception("Failed to ingest %s", symbol)

        counts = export_panel(db, symbols=symbols, dry_run=False)
        total = sum(counts.values())
        print(f"Exported {total} rows across {len(counts)} symbols into the PIT panel.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
