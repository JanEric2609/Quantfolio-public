"""CLI: ``python -m app.lab.factor_premia [--region world|europe] [--dry-run]``.

Runs the study against the JKP panel and prints one line per strategy. Without
``--dry-run`` the cards are stored. Only ``world`` cards (the default) gate the
monthly plan's tilt; ``europe`` is an informational second test.
"""
from __future__ import annotations

import argparse
import logging

from app.foundation.core.db import SessionLocal
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.lab.factor_premia.strategies import REGIONS
from app.lab.factor_premia.study import run_study


def _pct(value: float | None) -> str:
    return "     —" if value is None else f"{value * 100:+6.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade the pre-registered factor premia on the JKP panel.")
    parser.add_argument("--region", default=TILT_EVIDENCE_REGION, choices=sorted(REGIONS))
    parser.add_argument("--dry-run", action="store_true", help="print the cards without storing them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with SessionLocal() as db:
        cards = run_study(db, region=args.region, persist=not args.dry_run)
    if not cards:
        raise SystemExit(f"No JKP rows for region {args.region}; is the characteristics panel loaded?")

    print(f"{'strategy':<18}{'months':>7}{'L/S p.a.':>10}{'t':>7}{'post-pub':>10}{'net p.a.':>10}  verdict")
    for c in cards:
        t = "     —" if c.long_short_t is None else f"{c.long_short_t:6.2f}"
        verdict = "PASS" if c.passed else "fail: " + ", ".join(k["name"] for k in c.checks if not k["passed"])
        print(
            f"{c.label:<18}{c.months:>7}{_pct(c.long_short_annual):>10} {t}"
            f"{_pct(c.post_publication_annual):>10}{_pct(c.net_expected_annual):>10}  {verdict}"
        )
    print("\nstored" if not args.dry_run else "\ndry run: nothing stored")


if __name__ == "__main__":
    main()
