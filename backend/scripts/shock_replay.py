"""Replay confirmed shock days on daily index bars (research only, read only).

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/shock_replay.py --deep        # ask the providers for about 11 years (stored bars cover about two)
    python scripts/shock_replay.py               # stored bars only

Prints the coverage per index (first bar, bar count) to stderr and a JSON table to stdout. Stores nothing and writes no
replay results to the database (a price read may fill the price cache, as every chart does). The rule and its constants
are frozen in ``app/lab/shock_replay.py``; a positive result would justify a new pre-registration, never a live signal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.decision.desk import cfd_instruments as ci  # noqa: E402
from app.foundation import technical  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.market import history  # noqa: E402
from app.lab import shock_replay  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="fetch about 11 years from the providers (stores nothing)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        bars, insts = {}, {}
        for i in ci.INDICES:
            b = technical.bars_from_rows(history(db, i.level_ticker, days=4200, allow_live=not args.deep))
            if args.deep:
                from app.foundation.providers.registry import build_provider_registry

                got = build_provider_registry(db).get_history(i.level_ticker, days=4200)
                deep = technical.bars_from_rows(got.get("data") or []) if got.get("ok") else []
                print(f"{i.key}: stored {len(b)} bars from {b[0].date if b else '-'}; providers {len(deep)} bars "
                      f"from {deep[0].date if deep else '-'}", file=sys.stderr)
                if len(deep) > len(b):
                    b = deep
            if len(b) > shock_replay.SIGMA_N + 50:
                bars[i.key] = b
                insts[i.key] = {"spread_bp": float(i.spread_bp), "usd": i.currency == "USD",
                                "overnight_long": float(i.overnight_long), "overnight_short": float(i.overnight_short)}
    finally:
        db.close()
    result = shock_replay.run(bars, insts)
    for cell, r in result.items():
        print(f"{cell:18} events={r['events']:4} per_year={r['events_per_year']} mean_net_bp={r['mean_net_bp']} "
              f"t={r['t_days']} p_holm={r['p_holm']:.3f}", file=sys.stderr)
    print(json.dumps({"instruments": sorted(bars), "cells": result,
                      "note": "net bp of notional per trade, one observation per entry date; no news or sentiment (no history)"},
                     indent=1, default=str))


if __name__ == "__main__":
    main()
