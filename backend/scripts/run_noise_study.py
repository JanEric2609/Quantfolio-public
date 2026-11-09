"""Measure the noise of selection and ranking test designs on stored prices (plan 2026-11-08).

Outcome-blind: picks and scores are random (``lab.calibration.noise_study``),
so the figures never depend on how Discover's own picks did. Reads only:
the latest live Discover run's evaluable stocks and their sectors, and stored
EUR closes. Writes ``docs/research/calibration/noise-study-<date>.json`` and
prints each design's noise and the median years to a verdict it implies.

Usage (from backend/):
    .venv/bin/python scripts/run_noise_study.py [--days 1100] [--seeds 10] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision.verification import daily_tests as dt  # noqa: E402
from app.decision.verification.benchmark import passive_core_symbol  # noqa: E402
from app.foundation import eur_prices  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import DiscoverCandidateSnapshot  # noqa: E402
from app.lab.calibration import TestSpec, time_to_verdict  # noqa: E402
from app.lab.calibration.noise_study import RunUniverse, ex_ante_study, run_study  # noqa: E402

EDGES_21D = (0.005, 0.01)
#: The trust page's e-BH bar (e >= 40) and the BUY gate's 1 % (e >= 100).
THRESHOLDS = (40.0, 100.0)


def _runs(db) -> list[RunUniverse]:
    """Every live run's evaluable stocks as issued (composite, sector, pick flag); no outcomes."""
    rows = (
        db.query(DiscoverCandidateSnapshot)
        .filter(
            DiscoverCandidateSnapshot.backfilled.is_(False),
            DiscoverCandidateSnapshot.evaluable.is_(True),
            DiscoverCandidateSnapshot.instrument_group == "stock",
            DiscoverCandidateSnapshot.composite.isnot(None),
        )
        .all()
    )
    by_run: dict[str, list[DiscoverCandidateSnapshot]] = {}
    for r in rows:
        by_run.setdefault(r.run_id, []).append(r)
    return [
        RunUniverse(
            issue_day=pd.Timestamp(min(r.issue_date for r in group).isoformat()),
            sector={r.symbol.upper(): (r.sector or "") for r in group},
            composite={r.symbol.upper(): float(r.composite) for r in group if r.composite is not None},
            picked=frozenset(r.symbol.upper() for r in group if r.picked),
        )
        for group in by_run.values()
    ]


def _universe(db) -> tuple[str, dict[str, str]]:
    latest = (
        db.query(DiscoverCandidateSnapshot.run_id, DiscoverCandidateSnapshot.issue_date)
        .filter(DiscoverCandidateSnapshot.backfilled.is_(False))
        .order_by(DiscoverCandidateSnapshot.issue_date.desc())
        .first()
    )
    if latest is None:
        raise SystemExit("no live snapshot")
    rows = (
        db.query(DiscoverCandidateSnapshot)
        .filter(
            DiscoverCandidateSnapshot.run_id == latest[0],
            DiscoverCandidateSnapshot.evaluable.is_(True),
            DiscoverCandidateSnapshot.instrument_group == "stock",
        )
        .all()
    )
    return latest[0], {r.symbol.upper(): (r.sector or "") for r in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1100)
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--paths", type=int, default=600)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "research" / "calibration"))
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    db = SessionLocal()
    try:
        run_id, sectors = _universe(db)
        runs = _runs(db)
        bench = passive_core_symbol(db)
        series = {}
        wanted = {*sectors, *(s for r in runs for s in r.composite)}
        for sym in [*sorted(wanted), bench]:
            closes = eur_prices.eur_closes(db, sym, days=args.days, allow_live=False)
            if closes:
                series[sym] = pd.Series({pd.Timestamp(k): float(v) for k, v in closes.items()})
    finally:
        db.rollback()
        db.close()
    if bench not in series:
        raise SystemExit(f"no closes for the benchmark {bench}")
    core = series.pop(bench).sort_index()
    frame = pd.DataFrame(series).sort_index()
    frame = frame.reindex(core.index)  # the passive core's trading calendar, as the ledger uses
    universe = pd.DataFrame(frame[[s for s in sectors if s in frame.columns]])
    result = run_study(universe, core, sectors, seeds=tuple(range(args.seeds)))
    summary = result.summary()
    ex_ante = ex_ante_study(frame, core, runs)

    out = {
        "date": date.today().isoformat(),
        "run_id": run_id,
        "benchmark": bench,
        "n_symbols": frame.shape[1],
        "n_calendar_days": frame.shape[0],
        "n_issues": result.n_issues,
        "mean_universe": result.mean_universe,
        "seeds": args.seeds,
        "designs": {},
        "ex_ante": {},
    }
    for name, row in [*(("random:" + k, v) for k, v in summary.items()), *(("real:" + k, v) for k, v in ex_ante.items())]:
        sd = row["sd_daily"]
        clip = dt.CLIP_SIGMAS * sd
        ttv = {}
        for th in THRESHOLDS:
            spec = TestSpec(name, clip, dt.PRIOR_VAR, dt.BET_CAP, th, "skill")
            for edge in EDGES_21D:
                ttv[f"e{int(th)}_edge{edge}"] = time_to_verdict(spec, "iid_normal", edge, sd, years=40,
                                                                n_paths=args.paths, seed=12)["q50_years"]
        kind, key = name.split(":", 1)
        out["designs" if kind == "random" else "ex_ante"][key] = {**row, "q50_years": ttv}
        print(f"{name:<22} sd/21d {row['sd_21d'] * 100:5.2f} %  x{row['times_faster']:.2f} faster  "
              + "  ".join(f"{k}={v if v is None else round(v, 1)}" for k, v in ttv.items()), flush=True)
    path = Path(args.out) / f"noise-study-{out['date']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(path)
    print("sd per 21 days of the frozen designs used for sizing:",
          f"selection {dt.SELECTION_SIGMA_REF * math.sqrt(21):.4f}, ideas {dt.SIGMA_REF * math.sqrt(21):.4f}")


if __name__ == "__main__":
    main()
