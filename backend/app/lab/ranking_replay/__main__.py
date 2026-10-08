"""CLI: open the locked 2019–2025 exam once (ADR 0019 §4).

Reads per-ingredient cross-sections from ``<input_dir>/<ingredient>.parquet``
(``period``, ``entity``, ``score``, ``forward``) and writes the verdict to
stdout. With ``--dry-run`` nothing is recorded on the trial ledger.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from app.foundation.core.db import SessionLocal
from app.lab.ranking_replay.replay import (
    EXAM_END_YEAR,
    EXAM_START_YEAR,
    NON_AI_INGREDIENTS,
    run_replay,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--years", default=f"{EXAM_START_YEAR}-{EXAM_END_YEAR}")
    parser.add_argument("--spec-version", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start, end = (int(y) for y in args.years.split("-"))
    frames = {}
    for name in (*NON_AI_INGREDIENTS, "composite"):
        path = args.input_dir / f"{name}.parquet"
        if path.exists():
            frames[name] = pd.read_parquet(path)
    if not frames:
        raise SystemExit(f"no ingredient panels in {args.input_dir}")

    db = SessionLocal()
    try:
        report = run_replay(
            db, frames, years=(start, end),
            spec_version=args.spec_version, persist=not args.dry_run,
        )
    finally:
        db.close()
    print(json.dumps(report, indent=2, default=str))
    logger.info("ranking_replay: verdict %s for %s", report["verdict"], args.years)


if __name__ == "__main__":
    main()
