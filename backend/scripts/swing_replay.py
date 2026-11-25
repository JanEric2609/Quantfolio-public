"""Replay the proposed swing_trend cell on 2015-2026 daily index bars (research only, read only).

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/swing_replay.py              # prints the table and a JSON blob; writes no results
    python scripts/swing_replay.py --write      # also saves docs/research/swing-replay-2026-11.json (a file, not the database)

Reads daily bars through ``market.history`` (cache or provider; on a cache miss that call may store price bars in the
price cache, as every chart does). It writes no replay results to the database, sends no message and calls no broker. The rule and its constants are frozen in ``app/lab/swing_replay.py``. A positive
result would justify a new pre-registration, never a live signal.
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
from app.lab import swing_replay  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "swing-replay-2026-11.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        bars, insts = {}, {}
        for i in ci.INDICES:
            b = technical.bars_from_rows(history(db, i.level_ticker, days=4200))
            if len(b) > swing_replay.WARMUP + 50:
                bars[i.key] = [x for x in b if x.date.year >= 2015]
                insts[i.key] = {"spread_bp": float(i.spread_bp), "usd": i.currency == "USD",
                                "overnight_long": float(i.overnight_long), "overnight_short": float(i.overnight_short)}
    finally:
        db.close()
    result = swing_replay.run(bars, insts)
    out = {"instruments": sorted(bars), "cells": result,
           "note": "EUR/USD taken as 1; research only; flipped_control trades the opposite side on the same days."}
    for name, r in result.items():
        p = r["pooled"]
        print(f"{name:16} n={p['n']:5} mean={p['mean_eur']} t={p['t']} hit={p['hit_rate']} "
              f"hold={p['mean_hold_days']} maxdd={p['max_drawdown_eur']} gaps={r['gaps']}")
    print(json.dumps(out, indent=1, default=str))
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=1, default=str))
        print("written", OUT)


if __name__ == "__main__":
    main()
