"""Print the rank IC of Discover's shadow signals (plan 2026-11-08, WP3).

    cd backend && .venv/bin/python scripts/shadow_signal_ics.py [--horizon 21]

Read-only. One line per signal for each user with snapshots: runs measured, mean
per-run Spearman IC against the forward EUR return, its Newey-West t (lag 5 runs),
ICIR and the share of runs with a positive IC. Descriptive, never a verdict.
"""
from __future__ import annotations

import argparse

from app.decision.verification.ranking_metrics import shadow_signal_ics
from app.foundation.core.db import SessionLocal
from app.foundation.models.entities import DiscoverCandidateSnapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", type=int, default=21)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        users = [u for (u,) in db.query(DiscoverCandidateSnapshot.user_id).distinct().all()]
        for user_id in users:
            print(f"user {user_id}, {args.horizon} trading days")
            for name, row in sorted(shadow_signal_ics(db, user_id, horizon=args.horizon).items()):
                fmt = lambda v: "n/a" if v is None else f"{v:+.3f}"  # noqa: E731
                print(
                    f"  {name:30s} runs {row['n_runs']:3d}  mean IC {fmt(row['mean'])}  t {fmt(row['t_nw'])}  "
                    f"ICIR {fmt(row['icir'])}  positive {fmt(row['share_positive'])}"
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
