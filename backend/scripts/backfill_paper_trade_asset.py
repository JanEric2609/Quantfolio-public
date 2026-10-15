"""One-time backfill: what each paper trade traded, and its check re-costed (plan 2026-10-14, M2).

Before migration 0138 a paper trade did not record its ISIN or asset type, and
the trade check looked the type up by ticker alone, so an ETF missing from the
static table was costed as a stock (10 bp instead of 5 bp half-spread, no 30 %
Teilfreistellung on a sale). This fills ``paper_trades.isin``/``asset_type``
where empty (from the trade's holding, else the same evidence ladder that types
a new holding) and re-costs every ``paper_trade_checks`` row whose spread or tax
changes. The stored edge is kept; the verdict and, once marked, the
contribution follow the new cost.

Dry run by default: prints a before/after table. Pass --apply to write. Run
against prod only with the owner's OK.

Usage (run from backend/):
    .venv/bin/python scripts/backfill_paper_trade_asset.py            # dry run
    .venv/bin/python scripts/backfill_paper_trade_asset.py --apply    # writes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision import trade_check  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import PaperPortfolio, PaperTrade, PaperTradeCheck  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        filled = 0
        for trade in db.query(PaperTrade).filter(PaperTrade.asset_type.is_(None)).all():
            asset_type = trade_check.traded_asset_type(db, trade)
            isin = trade.isin or (trade.holding.isin if trade.holding is not None else None)
            print(f"trade {trade.ticker:<10} {trade.side:<4} {trade.date:%Y-%m-%d}: asset_type -> {asset_type}")
            trade.asset_type, trade.isin = asset_type, isin
            filled += 1

        recosted = 0
        nv: dict[tuple[str, int], bool] = {}
        for check in db.query(PaperTradeCheck).order_by(PaperTradeCheck.traded_on).all():
            trade = db.get(PaperTrade, check.trade_id)
            portfolio = db.get(PaperPortfolio, check.portfolio_id)
            if trade is None or portfolio is None:
                continue
            key = (portfolio.user_id, check.traded_on.year)
            if key not in nv:
                nv[key] = trade_check._nv_valid(db, *key)
            before = (check.spread_eur, check.tax_eur, check.verdict, check.contribution_eur)
            if trade_check.recost(db, check, trade, nv_valid=nv[key]):
                recosted += 1
                after = (check.spread_eur, check.tax_eur, check.verdict, check.contribution_eur)
                print(
                    f"check {check.ticker:<10} {check.side:<4} {check.traded_on}: "
                    f"spread {before[0]:.2f} -> {after[0]:.2f}, tax {before[1]:.2f} -> {after[1]:.2f}, "
                    f"verdict {before[2]} -> {after[2]}, contribution {before[3]} -> {after[3]}"
                )

        if args.apply:
            db.commit()
            print(f"\nApplied: {filled} trade(s) typed, {recosted} check(s) re-costed.")
        else:
            db.rollback()
            print(f"\nDry run: {filled} trade(s) would be typed, {recosted} check(s) re-costed. Pass --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
