"""CLI: ``python -m app.lab.satellite [--region world|europe] [--dry-run]``.

Simulates the pre-registered satellite on each score ``app.lab.pooled_model``
wrote, and prints one line per score. Without ``--dry-run`` each model is
recorded on the trial ledger and the results are written next to the panel.
"""
from __future__ import annotations

import argparse
import logging

from app.foundation.core.db import SessionLocal
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.lab.factor_premia.strategies import REGIONS
from app.lab.satellite.spec import BROKERS, HORIZON_YEARS
from app.lab.satellite.study import run_satellite_study


def _pct(value: float | None) -> str:
    return "     —" if value is None else f"{value * 100:+6.2f}%"


def _num(value: float | None, fmt: str = "6.2f") -> str:
    return "     —" if value is None else format(value, fmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate the stock-picking satellite net of costs and tax.")
    parser.add_argument("--region", default=TILT_EVIDENCE_REGION, choices=sorted(REGIONS))
    parser.add_argument("--dry-run", action="store_true", help="print the results without storing them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with SessionLocal() as db:
        study = run_satellite_study(db, region=args.region, persist=not args.dry_run)
    if study is None:
        raise SystemExit(f"No scores for region {args.region}; run `python -m app.lab.pooled_model` first.")

    print(
        f"{'broker':<10}{'score':<16}{'trades/y':>9}{'held m':>7}{'gross':>9}{'net':>9}{'t':>7}{'TE':>8}"
        f"{'tax sat':>9}{'tax core':>9}{'after tax':>10}{'t':>7}{'vs V+M':>9}{'t':>7}"
        f"{f'P(win {HORIZON_YEARS}y)':>12}{'net 10y':>9}"
    )
    for c in study.cards:
        print(
            f"{c.broker:<10}{c.model:<16}{c.trades_per_year:>9.1f}{_num(c.holding_months, '7.1f')}{_pct(c.gross_excess_annual):>9}"
            f"{_pct(c.net_excess_annual):>9}{_num(c.net_excess_t, '7.2f')}{_pct(c.tracking_error):>8}"
            f"{_pct(c.satellite_tax_annual):>9}{_pct(c.core_tax_annual):>9}{_pct(c.after_tax_excess_annual):>10}"
            f"{_num(c.after_tax_excess_t, '7.2f')}{_pct(c.vs_baseline_annual):>9}{_num(c.vs_baseline_t, '7.2f')}"
            f"{_num(c.p_beat_core, '12.2f')}{_pct(c.recent.get('net_excess_annual')):>9}"
        )
    first = study.cards[0]
    print(f"\nexcess over the core, p.a.; {first.first_month} to {first.last_month}")
    for b in BROKERS:
        print(f"  {b.name}: {b.positions} stocks of EUR {b.position_eur:,.0f}, EUR {b.order_fee_eur:g} per order"
              f" = {b.commission_bps:.0f} bps each way")
    print(f"\nstored: {study.artifact}" if study.artifact else "\ndry run: nothing stored")


if __name__ == "__main__":
    main()
