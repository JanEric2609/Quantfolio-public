"""Strong-day ride on commodity and FX daily bars with state exits, no time stop (research only, read only).

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/commodity_fx_replay.py                          # both families, writes nothing
    python scripts/commodity_fx_replay.py --since 2000 --draws 200 --cost-mult 2 --write

Costs are assumptions until read off Trading 212's Instrument details pages; ``--cost-mult`` scales them. See
``app/lab/commodity_fx_replay.py``. Reads bars through ``market.history`` (a cache miss may store price bars, as every
chart does); no database results, no message, no broker call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.foundation import technical  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.market import history  # noqa: E402
from app.lab import commodity_fx_replay as cf  # noqa: E402
from app.lab import shock_ride_replay as sr  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "commodity-fx-replay-2026-11.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=2005)
    ap.add_argument("--draws", type=int, default=100)
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    result: dict[str, object] = {"since": args.since, "cost_mult": args.cost_mult, "families": {}}
    try:
        for fam, table in cf.FAMILIES.items():
            table = cf.stress(table, args.cost_mult)
            bars, vols = {}, {}
            for key, spec in table.items():
                try:
                    b = [x for x in technical.bars_from_rows(history(db, spec["ticker"], days=11000)) if x.date.year >= args.since]
                except Exception as exc:  # noqa: BLE001 - one missing ticker must not stop the others
                    print(f"no {spec['ticker']}: {type(exc).__name__}", file=sys.stderr)
                    continue
                if len(b) > sr.WARMUP + 50:
                    bars[key] = b
                if spec["vol"] and key in bars:
                    try:
                        v = technical.bars_from_rows(history(db, spec["vol"], days=11000))
                        if v:
                            vols[key] = {x.date: x.close for x in v}
                    except Exception as exc:  # noqa: BLE001 - optional input
                        print(f"no {spec['vol']}: {type(exc).__name__}", file=sys.stderr)
            if not bars:
                print(f"{fam}: no history loaded", file=sys.stderr)
                continue
            res = cf.run_family(table, bars, vols, draws=args.draws)
            res["instruments"] = sorted(bars)
            result["families"][fam] = res  # type: ignore[index]
            for name, c in res["cells"].items():
                p = c["pooled"]
                sp = c["side_placebo"]
                print(f"{fam:12} {name:26} n={p['n']:5} mean={p['mean_eur']} t={p['t']} side_placebo_p={sp and sp['p_value']} "
                      f"holm={c['holm_p']:.3f} verdict={c['verdict']}")
    finally:
        db.close()
    if not result["families"]:
        sys.exit("no history loaded; check the price provider and --since")
    print(json.dumps(result, indent=1, default=str))
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, indent=1, default=str))
        print("written", OUT)


if __name__ == "__main__":
    main()
