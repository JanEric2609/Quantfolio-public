"""Calibrate the trust tests on simulated series (plan 2026-10-14, WP-D2).

Builds each test from ``decision.verification.daily_tests``' own constants and
writes ``docs/research/calibration/calibration-<date>.json``:

* the false-alarm rate over 20 years at the null's boundary, per test and noise model;
* the 10/50/90 % years to a verdict on an edge grid (iid normal and t(4)-GARCH);
* the minimum detectable edge in 5 and 10 years (median verdict within the horizon).

Not a trial: no return claim, nothing in the trial ledger.

Usage (from backend/):
    .venv/bin/python scripts/run_calibration.py [--paths 2000] [--out ../docs/research/calibration]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision.verification import daily_tests as dt  # noqa: E402
from app.lab.calibration import MODELS, TestSpec, minimum_detectable_edge, time_to_verdict, type_i_error  # noqa: E402

#: A mandate sleeve against MSCI World: about 0.8 % a day (a 78 %-cash sleeve on 1 % equity noise).
MANDATE_SD_DAILY = 0.008
EDGE_GRID_21D = (0.0025, 0.005, 0.01, 0.02)
MDE_GRID_21D = (0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03)


def specs() -> list[tuple[TestSpec, float]]:
    """(test, its reference daily noise) for every family the Trust page runs."""
    m_clip = dt.CLIP_SIGMAS * MANDATE_SD_DAILY
    return [
        (TestSpec("F1 skill", dt.CLIP, dt.PRIOR_VAR, dt.BET_CAP, dt.THRESHOLD, "skill"), dt.SIGMA_REF),
        (TestSpec("F1 harm", dt.CLIP, dt.PRIOR_VAR, dt.BET_CAP, dt.THRESHOLD, "harm"), dt.SIGMA_REF),
        (TestSpec("S skill", dt.SELECTION_CLIP, dt.PRIOR_VAR, dt.BET_CAP, dt.THRESHOLD, "skill"),
         dt.SELECTION_SIGMA_REF),
        (TestSpec("S harm", dt.SELECTION_CLIP, dt.PRIOR_VAR, dt.BET_CAP, dt.THRESHOLD, "harm"),
         dt.SELECTION_SIGMA_REF),
        (TestSpec("U futility", dt.SELECTION_CLIP, dt.PRIOR_VAR, dt.BET_CAP, dt.FUTILITY_THRESHOLD, "futility",
                  shift=dt.FUTILITY_DELTA_21D / dt.HORIZON_DAYS), dt.SELECTION_SIGMA_REF),
        (TestSpec("M futility", m_clip, dt.PRIOR_VAR, dt.BET_CAP, dt.THRESHOLD, "futility",
                  shift=dt.MANDATE_DELTA_21D / dt.HORIZON_DAYS), MANDATE_SD_DAILY),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=int, default=2000)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "research" / "calibration"))
    args = parser.parse_args()
    out: dict = {"date": date.today().isoformat(), "paths": args.paths, "tests": {}}
    for spec, sd in specs():
        row: dict = {"spec": spec.__dict__, "sd_daily": sd, "sd_21d": sd * math.sqrt(dt.HORIZON_DAYS)}
        row["type_i_20y"] = {m: type_i_error(spec, m, sd, years=20, n_paths=args.paths, seed=11) for m in MODELS}
        row["time_to_verdict"] = {
            m: {str(e): time_to_verdict(spec, m, e, sd, years=60, n_paths=args.paths // 2, seed=12)
                for e in EDGE_GRID_21D}
            for m in ("iid_normal", "t4_garch")
        }
        row["min_detectable_edge_21d"] = {
            # The table's seed, so the two agree where an edge's median verdict sits near the horizon.
            str(y): minimum_detectable_edge(spec, "iid_normal", sd, y, MDE_GRID_21D, n_paths=args.paths // 2, seed=12)
            for y in (5, 10)
        }
        out["tests"][spec.name] = row
        worst = max(row["type_i_20y"].values())
        print(f"{spec.name:<11} type-I max {worst:.3f}  MDE 5y {row['min_detectable_edge_21d']['5']}  "
              f"10y {row['min_detectable_edge_21d']['10']}", flush=True)
    path = Path(args.out) / f"calibration-{out['date']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(path)


if __name__ == "__main__":
    main()
