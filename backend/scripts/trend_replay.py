"""Slow trend (100-day breakout, EMA50 or 5 ATR state exit, no clock) on indices, commodities and FX (research only).

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/trend_replay.py --since 2000 --draws 200 --write
    python scripts/trend_replay.py --since 2000 --cost-mult 2

Rule: ``app/lab/algo_lab/trend_cell.py``; test and costs: ``app/lab/trend_replay.py``. Commodity and FX costs are
assumptions (``--cost-mult`` scales them); commodity bars are unadjusted continuous futures. Reads bars through
``market.history`` (a cache miss may store price bars); writes no database result, sends nothing, calls no broker.
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
from app.lab import commodity_fx_replay as cf  # noqa: E402
from app.lab import trend_replay as tr  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "trend-replay-2026-11.json"
MIN_BARS = 400


def _load(db, ticker: str, since: int) -> list[technical.Bar]:  # type: ignore[no-untyped-def]
    return [x for x in technical.bars_from_rows(history(db, ticker, days=11000)) if x.date.year >= since]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=2000)
    ap.add_argument("--draws", type=int, default=100)
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    families: dict[str, tuple[dict, dict]] = {}
    try:
        idx_bars, idx_inst = {}, {}
        for i in ci.INDICES:
            b = _load(db, i.level_ticker, args.since)
            if len(b) > MIN_BARS:
                idx_bars[i.key] = b
                m = args.cost_mult
                idx_inst[i.key] = {"spread_bp": float(i.spread_bp) * m, "usd": i.currency == "USD",
                                   "overnight_long": float(i.overnight_long) * m, "overnight_short": float(i.overnight_short) * m}
        if idx_bars:
            families["indices"] = (idx_bars, idx_inst)
        for fam, table in cf.FAMILIES.items():
            table = cf.stress(table, args.cost_mult)
            bars, insts = {}, {}
            for key, spec in table.items():
                try:
                    b = _load(db, spec["ticker"], args.since)
                except Exception as exc:  # noqa: BLE001 - one missing ticker must not stop the others
                    print(f"no {spec['ticker']}: {type(exc).__name__}", file=sys.stderr)
                    continue
                if len(b) > MIN_BARS:
                    bars[key] = b
                    insts[key] = {f: spec[f] for f in ("spread_bp", "usd", "overnight_long", "overnight_short")}
            if bars:
                families[fam] = (bars, insts)
    finally:
        db.close()
    if not families:
        sys.exit("no history loaded; check the price provider and --since")
    res = tr.run(families, draws=args.draws)
    res.update({"since": args.since, "cost_mult": args.cost_mult,
                "instruments": {f: sorted(v[0]) for f, v in families.items()},
                "note": "Costs of commodities and FX are assumptions; commodity bars have roll splices (a commodity pass is provisional); "
                        "required control = same breakout days with random sides; p-values are quarter-block bootstrap."})
    for name, c in res["cells"].items():
        p, sp = c["pooled"], c["side_placebo"]
        print(f"{name:24} n={p['n']:4} mean={p['mean_eur']} t={p['t']} hold={p['mean_hold_days']} "
              f"side_placebo_p={sp and sp['p_value']} holm={c['holm_p']:.3f} verdict={c['verdict']}")
    print(json.dumps(res, indent=1, default=str))
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(res, indent=1, default=str))
        print("written", OUT)


if __name__ == "__main__":
    main()
