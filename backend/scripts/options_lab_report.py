"""Print the Quantlab's options study (ADR 0026).

    DATABASE_URL=... .venv/bin/python scripts/options_lab_report.py [--margin]

The protective-put replay fetches S&P 500 total-return-free closes and VIX from
Yahoo (research only, nothing is stored); ``--margin`` adds the issuer-margin
summary from the stored warrant quotes.
"""
from __future__ import annotations

import argparse
import json

import yfinance as yf

from app.lab.options_lab import protective_put_backtest, warrant_margin_from_db


def _closes(symbol: str):
    frame = yf.Ticker(symbol).history(period="max", auto_adjust=True)["Close"].dropna()
    frame.index = frame.index.tz_localize(None).normalize()
    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--margin", action="store_true")
    args = parser.parse_args()
    spx, vix = _closes("^GSPC"), _closes("^VIX") / 100.0
    for skew in (0.0, 0.03):
        for otm in (0.0, 0.05, 0.10):
            r = protective_put_backtest(spx, vix, otm=otm, skew=skew, rate=0.025)
            print(json.dumps({k: r[k] for k in ("first_day", "last_day", "otm", "skew", "premium_per_year",
                                                "matched_equity_weight")}))
            for leg in ("index", "protected", "divested"):
                s = r[leg]
                print(f"  {leg:9} return {s['annual_return']*100:5.2f} %  vol {s['annual_volatility']*100:5.2f} %  "
                      f"Sharpe {s['sharpe']:.2f}  max fall {s['max_drawdown']*100:6.1f} %  worst month {s['worst_period']*100:6.1f} %")
    if args.margin:
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as db:
            print(json.dumps(warrant_margin_from_db(db), indent=1, default=str))


if __name__ == "__main__":
    main()
