"""Back-fill the shadow ledger from past Discover runs (ADR 0018 §5, exploratory).

Rebuilds ``discover_candidate_snapshot`` rows for every completed run that has
none, from its ``DiscoverCandidate`` rows, flagged ``backfilled = true`` with
cohort ``legacy_unstamped``, then resolves their forward returns. These rows
are shown as exploratory on the trust page and never enter an e-value.
Idempotent: runs that already have snapshots are skipped, and outcomes are
written once.

    cd backend && .venv/bin/python scripts/backfill_shadow_ledger.py [--user USER_ID] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.decision.discover import backfill_snapshots  # noqa: E402
from app.decision.verification.candidate_outcomes import resolve_candidate_outcomes  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import DiscoverCandidateSnapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", help="Only this user's runs")
    parser.add_argument("--dry-run", action="store_true", help="Build and count, then roll back")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        counts = backfill_snapshots(db, user_id=args.user)
        print(f"snapshots: {counts['rows']} rows from {counts['runs']} runs")
        earliest = (
            db.query(DiscoverCandidateSnapshot.issue_date)
            .filter(DiscoverCandidateSnapshot.backfilled.is_(True))
            .order_by(DiscoverCandidateSnapshot.issue_date)
            .first()
        )
        if earliest is not None:
            days = (datetime.now(UTC).date() - earliest[0]).days + 1
            written = resolve_candidate_outcomes(db, user_id=args.user, refresh=True, lookback_days=days)
            print(f"outcomes: {written} rows")
        if args.dry_run:
            db.rollback()
            print("dry run: rolled back")
        else:
            db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
