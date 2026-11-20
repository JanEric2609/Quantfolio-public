"""One-time re-check of series excluded as ``split_unadjusted`` (plan 2026-10-14, M1).

Before the fix, any move that looked like a split ratio (a -50 % crash reads as
2:1) marked the series ``split_unadjusted`` for good, and Discover skipped the
symbol. A split signature now counts as a split only when the provider lists a
split near that day at that ratio (or the move is beyond what a market makes).

Dry run (default): runs the guard over each excluded series as stored, reads
the provider's split events and prints the verdict. ``--apply`` re-pulls each
series through ``DataIngester.repull_series``, which applies the same verdict
and clears the exclusion of a real move. Writing to prod needs the owner's OK.

Usage (run from backend/):
    .venv/bin/python scripts/recheck_split_unadjusted.py            # dry run
    .venv/bin/python scripts/recheck_split_unadjusted.py --apply    # re-pulls
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.data_backbone.bar_guard import guard_bars, guard_class_for  # noqa: E402
from app.foundation.data_backbone.ingest import DataIngester  # noqa: E402
from app.foundation.models.entities import BarSeriesMeta  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Re-pull the series (default: dry run).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ingester = DataIngester(db)
        pins = db.query(BarSeriesMeta).filter(BarSeriesMeta.quarantine_reason == "split_unadjusted").all()
        for pin in pins:
            rows = db.execute(
                text("SELECT ts, close FROM bar_prices WHERE symbol = :s ORDER BY ts"), {"s": pin.symbol},
            ).all()
            frame = pd.DataFrame(rows, columns=pd.Index(["ts", "close"]))
            guarded = guard_bars(
                frame, None, symbol=pin.symbol, guard_class=guard_class_for(db, pin.symbol),
                pinned_provider=pin.provider, allow_split=True, check_calendar=False,
            )
            verdict = ingester.split_verdict(pin.symbol, guarded)
            days = ", ".join(f"{d.date()} x{r:.4g}" for d, r in guarded.split_days) or "no signature left"
            print(f"{pin.symbol:<10} {pin.provider:<10} {verdict:<10} {days}")
            if args.apply:
                result = ingester.repull_series(pin.symbol, provider=pin.provider)
                print(f"           re-pulled: {result.get('split_verdict')} ({result.get('message') or 'ok'})")
        if not args.apply:
            print(f"\nDry run: {len(pins)} excluded series checked. Pass --apply to re-pull them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
