"""Replay the event desk's technical rules on 2016-2026 daily bars (research only; ADR 0028).

    python scripts/signal_replay.py            # fetch bars through market.history, print the table
    python scripts/signal_replay.py --write    # also write docs/research/signal-replay-2026-11.json,
                                               # the app_settings key desk_signal_replay_json and the trials

The replay counts as research, not the pass test. All six cells are scored live whatever it says.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.decision.desk import cfd_instruments as ci  # noqa: E402
from app.decision.desk import prereg_events  # noqa: E402
from app.foundation import technical  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.market import history  # noqa: E402
from app.foundation.settings import upsert_public_settings  # noqa: E402
from app.lab import signal_replay  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "signal-replay-2026-11.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        bars, insts = {}, {}
        for i in ci.UNIVERSE:
            rows = history(db, i.level_ticker, days=4200)
            b = technical.bars_from_rows(rows)
            if len(b) > signal_replay.WARMUP + 50:
                bars[i.key] = [x for x in b if x.date.year >= 2015]
                insts[i.key] = {"spread_bp": float(i.spread_bp), "usd": i.usd, "kind": i.kind,
                                "overnight_long": float(i.overnight_long), "overnight_short": float(i.overnight_short)}
        base = signal_replay.run(bars, insts)
        stress = signal_replay.run(bars, insts, stock_spread_bp=15.0)
        result = {"instruments": sorted(bars), "cells": base, "stress_stock_spread_15bp": stress,
                  "note": "EUR/USD taken as 1; research only, not the pass test."}
        for cell, r in base.items():
            p = r["pooled"]
            print(f"{cell:14} n={p['n']:5} mean={p['mean_eur']} t={p['t']} hit={p['hit_rate']} "
                  f"hold={p['mean_hold_days']} maxdd={p['max_drawdown_eur']}")
        if args.write:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(result, indent=1, default=str))
            summary = {c: {k: v for k, v in r["pooled"].items() if k != "per_year"} for c, r in base.items()}
            upsert_public_settings(db, {"desk_signal_replay_json": json.dumps(summary, default=str)})
            signal_replay.record_trials(db, prereg_events.EVENT_HASH)
            db.commit()
            print("written", OUT)
    finally:
        db.close()


if __name__ == "__main__":
    main()
