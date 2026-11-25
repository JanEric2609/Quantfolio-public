"""Replay strong-day entries with state exits and no time stop on daily index bars (research only, read only).

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/shock_ride_replay.py                  # table + JSON on stdout, writes nothing
    python scripts/shock_ride_replay.py --since 1995 --draws 200 --write   # deeper history, saves docs/research/shock-ride-replay-2026-11.json

Reads daily bars through ``market.history`` (cache or provider; a cache miss may store price bars in the price cache, as
every chart does). SPX and NDX also get the implied trigger from the ^VIX / ^VXN closes when those load. It writes no
replay results to the database, sends no message and calls no broker. Rule, constants and the decision rule are frozen
in ``app/lab/shock_ride_replay.py``; a pass would justify a new pre-registration, never a live signal.
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
from app.lab import shock_ride_replay as sr  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "shock-ride-replay-2026-11.json"
VOL_INDEX = {"SPX": "^VIX", "NDX": "^VXN"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=2005)
    ap.add_argument("--draws", type=int, default=100)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    bars, insts, implied = {}, {}, {}
    try:
        for i in ci.INDICES:
            b = technical.bars_from_rows(history(db, i.level_ticker, days=11000))
            b = [x for x in b if x.date.year >= args.since]
            if len(b) > sr.WARMUP + 50:
                bars[i.key] = b
                insts[i.key] = {"spread_bp": float(i.spread_bp), "usd": i.currency == "USD",
                                "overnight_long": float(i.overnight_long), "overnight_short": float(i.overnight_short)}
            vt = VOL_INDEX.get(i.key)
            if vt and i.key in bars:
                try:
                    v = technical.bars_from_rows(history(db, vt, days=11000))
                    if v:
                        implied[i.key] = {x.date: x.close for x in v}
                except Exception as exc:  # noqa: BLE001 - optional input; the EWMA trigger still runs
                    print(f"no {vt}: {type(exc).__name__}", file=sys.stderr)
    finally:
        db.close()
    if not bars:
        sys.exit("no index history loaded; check the price provider and --since")
    res = sr.run(bars, insts, implied_by_key=implied or None, draws=args.draws)
    out = {"instruments": sorted(bars), "since": args.since, "from": str(min(b[0].date for b in bars.values())),
           "to": str(max(b[-1].date for b in bars.values())), **res,
           "note": "EUR/USD taken as 1; research only; required control = same strong days with random sides (side_placebo); the random-date placebo is descriptive; p-values are quarter-block bootstrap."}
    for name, c in res["cells"].items():
        p = c["pooled"]
        pl = c["placebo"]
        print(f"{name:26} n={p['n']:5} mean={p['mean_eur']} t={p['t']} hold={p['mean_hold_days']} "
              f"date_placebo_p={pl and pl['p_value']} side_placebo_p={c['side_placebo'] and c['side_placebo']['p_value']} holm={c['holm_p']:.3f} verdict={c['verdict']}")
    print(json.dumps(out, indent=1, default=str))
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=1, default=str))
        print("written", OUT)


if __name__ == "__main__":
    main()
