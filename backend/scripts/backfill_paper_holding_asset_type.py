"""One-time backfill: re-classify PaperHolding.name/asset_type (F9, bypass #3).

Existing paper_holdings rows were written with ``name`` mangled to the bare
ticker (e.g. "XEON.DE") instead of the real instrument name, which starved
classify_instrument's name-based money-market detection and left rows like
XEON.DE stuck with asset_type='stock' when they're actually money-market
funds. This does NOT touch the schema — same columns, corrected values.

Dry-run by default: prints what would change without writing. Pass --apply
to actually commit the updates. Run manually against prod only once
approved — this is not part of any migration or scheduled job.

Usage (run from backend/):
    .venv/bin/python scripts/backfill_paper_holding_asset_type.py            # dry run
    .venv/bin/python scripts/backfill_paper_holding_asset_type.py --apply    # writes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import PaperHolding  # noqa: E402
from app.decision.paper_portfolio import (  # noqa: E402
    resolve_instrument_name,
    resolve_paper_asset_type,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        holdings = db.query(PaperHolding).all()
        changed = 0
        for h in holdings:
            if not h.ticker:
                continue
            new_name = resolve_instrument_name(db, h.ticker, h.isin)
            new_asset_type = resolve_paper_asset_type(db, h.ticker, h.isin)
            if new_name == h.name and new_asset_type == h.asset_type:
                continue
            changed += 1
            print(
                f"{h.ticker}: name {h.name!r} -> {new_name!r}, "
                f"asset_type {h.asset_type!r} -> {new_asset_type!r}"
            )
            if args.apply:
                h.name = new_name
                h.asset_type = new_asset_type

        if args.apply:
            db.commit()
            print(f"\nApplied: {changed} of {len(holdings)} row(s) updated.")
        else:
            print(f"\nDry run: {changed} of {len(holdings)} row(s) would change. Re-run with --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
