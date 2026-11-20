"""Start every paper portfolio again from one clean date (2026-10-04 decision).

Run once after the deploy that fixed FX, dividends, TWR and the benchmark
(migration 0128). Each portfolio's current run (holdings, trades, snapshots,
cash flows, metrics and advisor scorecards) is archived to
``paper_portfolio_archives`` by ``reset_portfolio`` and then deleted; nothing is
lost. The new run is seeded from the synced book at today's EUR prices, so its
return starts at 0 next to the same amount in MSCI World EUR.

Dry run by default. Usage (from backend/, against the real DATABASE_URL):
    .venv/bin/python scripts/reset_paper_portfolios.py
    .venv/bin/python scripts/reset_paper_portfolios.py --user <username> --yes
"""
from __future__ import annotations

import argparse
import sys

from app.decision.paper_portfolio import reset_portfolio
from app.foundation.core.db import SessionLocal
from app.foundation.models.entities import PaperPortfolio, User


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive every paper run and start again today at market value.")
    parser.add_argument("--user", help="Only this username (default: every user).")
    parser.add_argument("--reason", default="2026-10 engine fix: FX, dividends, TWR, benchmark")
    parser.add_argument("--yes", action="store_true", help="Actually reset; without it, only list.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = db.query(PaperPortfolio, User.username).join(User, User.id == PaperPortfolio.user_id)
        if args.user:
            query = query.filter(User.username == args.user)
        rows = query.order_by(User.username, PaperPortfolio.mandate).all()
        if not rows:
            print("No paper portfolios matched.")
            return 1
        for portfolio, username in rows:
            label = f"{username} · {portfolio.name} (mandate {portfolio.mandate}, {portfolio.id})"
            if not args.yes:
                print(f"would reset  {label}")
                continue
            result = reset_portfolio(db, portfolio.id, reason=args.reason)
            print(
                f"reset        {label}: archive {result['archive_id']}, "
                f"{result['holdings_count']} holdings, start EUR {result['baseline_value']:,.2f}, "
                f"archived {result['archived']}"
            )
        if not args.yes:
            print("Dry run. Pass --yes to archive and reset these.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
