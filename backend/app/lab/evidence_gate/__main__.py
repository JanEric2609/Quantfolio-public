"""CLI: ``python -m app.lab.evidence_gate [--region world|europe] [--dry-run]``.

Grades what ``app.lab.pooled_model`` and ``app.lab.satellite`` stored and
prints one block per family. Without ``--dry-run`` the result is written next
to the panel. It records no trials.
"""
from __future__ import annotations

import argparse
import logging

from app.foundation.core.db import SessionLocal
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.lab.evidence_gate.gate import run_evidence_gate
from app.lab.evidence_gate.spec import DSR_PASS, PBO_MAX
from app.lab.factor_premia.strategies import REGIONS


def _pct(value: float | None) -> str:
    return "     —" if value is None else f"{value * 100:+6.2f}%"


def _num(value: float | None, fmt: str) -> str:
    return "—" if value is None else format(value, fmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deflated Sharpe and PBO over the pre-registered trials.")
    parser.add_argument("--region", default=TILT_EVIDENCE_REGION, choices=sorted(REGIONS))
    parser.add_argument("--dry-run", action="store_true", help="print the result without storing it")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with SessionLocal() as db:
        result = run_evidence_gate(db, region=args.region, persist=not args.dry_run)
    if result is None:
        raise SystemExit(
            f"No results for region {args.region}; run `python -m app.lab.pooled_model` "
            "and `python -m app.lab.satellite` first."
        )

    print(f"trials on the ledger: {result.n_trials}  (pass: DSR >= {DSR_PASS}, PBO <= {PBO_MAX:.0%})")
    for f in result.families:
        pbo = "not measurable" if f.pbo is None else f"{f.pbo:.0%}"
        print(f"\n{f.name}{' (gates money)' if f.gates_money else ''}: {f.question}")
        print(f"  PBO {pbo} over {f.common_months} common months")
        print(f"  {'candidate':<36}{'months':>7}{'mean p.a.':>11}{'Sharpe':>8}{'t':>7}{'DSR':>7}  pass")
        for c in f.candidates:
            label = c.name + ("" if c.mined else " (baseline)")
            print(
                f"  {label:<36}{c.months:>7}{_pct(c.mean_annual):>11}{_num(c.sharpe_annual, '8.2f'):>8}"
                f"{_num(c.t, '7.2f'):>7}{c.dsr:>7.2f}  {'yes' if c.passes_dsr else 'no'}"
            )
    print(f"\nsatellite: {'UNLOCKED' if result.satellite_unlocked else 'locked'}. {result.reason}")
    print(f"stored: {result.artifact}" if result.artifact else "dry run: nothing stored")


if __name__ == "__main__":
    main()
