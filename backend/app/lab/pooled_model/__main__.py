"""CLI: ``python -m app.lab.pooled_model [--region world|europe] [--dry-run]``.

Fits the pre-registered models under walk-forward CV and prints one line per
model plus the value + momentum baseline. Without ``--dry-run`` each model is
recorded on the trial ledger and the results are written next to the panel.
"""
from __future__ import annotations

import argparse
import logging

from app.foundation.core.db import SessionLocal
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.lab.factor_premia.strategies import REGIONS
from app.lab.pooled_model.study import run_model_study


def _pct(value: float | None) -> str:
    return "     —" if value is None else f"{value * 100:+6.2f}%"


def _num(value: float | None, fmt: str = "6.2f") -> str:
    return "     —" if value is None else format(value, fmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit and grade the pooled JKP model out of sample.")
    parser.add_argument("--region", default=TILT_EVIDENCE_REGION, choices=sorted(REGIONS))
    parser.add_argument("--dry-run", action="store_true", help="print the results without storing them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with SessionLocal() as db:
        study = run_model_study(db, region=args.region, persist=not args.dry_run)
    if study is None:
        raise SystemExit(f"No JKP rows for region {args.region}; is the characteristics panel loaded?")

    print(
        f"{'model':<16}{'months':>7}{'IC':>8}{'IC t':>7}{'L/S p.a.':>10}{'t':>7}{'Sharpe':>8}"
        f"{'L/O p.a.':>10}{'vs V+M':>9}{'t':>7}{'last 10y L/S':>14}"
    )
    for c in study.cards:
        print(
            f"{c.model:<16}{c.months:>7}{_num(c.ic_mean, '8.4f')}{_num(c.ic_t)} {_pct(c.long_short_annual):>9}"
            f"{_num(c.long_short_t)}{_num(c.long_short_sharpe, '8.2f')}{_pct(c.long_only_annual):>10}"
            f"{_pct(c.vs_baseline_annual):>9}{_num(c.vs_baseline_t)}{_pct(c.recent.get('long_short_annual')):>14}"
        )
    print(f"\nout of sample {study.cards[0].first_month} to {study.cards[0].last_month}, {len(study.folds)} folds")
    for f in study.folds:
        print(
            f"  fold {f['fold']:>2}: train {f['train_months']:>3} months, test {f['test_first']} to {f['test_last']},"
            f" ridge lambda {f['ridge_lambda']:g}, trees {f['gbm_trees']}"
        )
    print(f"\nstored: {study.artifact}" if study.artifact else "\ndry run: nothing stored")


if __name__ == "__main__":
    main()
