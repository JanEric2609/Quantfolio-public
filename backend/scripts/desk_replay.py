"""Replay the desk's index rule on SPY and QQQ 30-minute bars since 2016 (research only, read only).

    python scripts/desk_replay.py                  # fetch with the stored Alpaca key (SIP history is free
                                                   # for bars older than 15 minutes), print the report
    python scripts/desk_replay.py --dump bars.json # also save the bars
    python scripts/desk_replay.py --bars bars.json # replay saved bars, no network
    python scripts/desk_replay.py --minutes m.json # the pre-registered exit variants (take-profit, trailing)
                                                   # on saved 1-minute bars {sym: [[iso, high, low, close]]}

Writes nothing to the database. See ``app/lab/desk_replay.py`` for the cost models.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lab.desk_replay import (  # noqa: E402
    Costs,
    days_from_bars,
    evaluate,
    evaluate_exits,
    evaluate_predictors,
    minute_days,
)

SYMBOLS = ("SPY", "QQQ")
START = datetime(2016, 1, 1, tzinfo=UTC)
SIP_DELAY = timedelta(minutes=20)


def fetch() -> dict[str, list[dict]]:
    from app.foundation.core.db import SessionLocal
    from app.foundation.providers.alpaca_provider import AlpacaProvider
    from app.foundation.settings import get_secret

    db = SessionLocal()
    try:
        key, meta = get_secret(db, "alpaca") or (None, None)
    finally:
        db.close()
    provider = AlpacaProvider(key, api_secret=(meta or {}).get("api_secret"))
    # The free plan serves SIP history only when it ends 15+ minutes ago; an `end` of "now" is a 403
    # ("subscription does not permit querying recent SIP data"). Stop 20 minutes back, and fall back to
    # the IEX feed (thinner volume, same prices) when SIP history is refused outright.
    now = datetime.now(UTC) - SIP_DELAY
    out: dict[str, list[dict]] = {}
    for sym in SYMBOLS:
        rows: list[dict] = []
        for feed in ("sip", "iex"):
            try:
                rows = []
                for year in range(START.year, now.year + 1):
                    rows += provider.get_intraday_bars(sym, "30Min", datetime(year, 1, 1, tzinfo=UTC),
                                                       min(datetime(year + 1, 1, 1, tzinfo=UTC), now), feed=feed)
                print(f"{sym}: {len(rows)} bars on the {feed} feed", file=sys.stderr)
                break
            except ValueError as exc:
                print(f"{sym}: {feed} feed refused ({exc})", file=sys.stderr)
                if feed == "iex":
                    raise
        out[sym] = [{"t": r["t"].isoformat(), "c": str(r["c"])} for r in rows]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars")
    ap.add_argument("--dump")
    ap.add_argument("--minutes")
    ap.add_argument("--predictors", action="store_true",
                    help="r1 (registered) against the rest-of-day return, split by trailing volatility terciles")
    args = ap.parse_args()
    if args.minutes:
        raw_m = json.loads(Path(args.minutes).read_text())
        exits = {sym: evaluate_exits(minute_days([(datetime.fromisoformat(t), h, lo, c) for t, h, lo, c in rows]),
                                     Costs()) for sym, rows in raw_m.items()}
        print(json.dumps(exits, indent=1, default=str))
        return
    raw = json.loads(Path(args.bars).read_text()) if args.bars else fetch()
    if args.predictors:
        out = {sym: evaluate_predictors(days_from_bars([{"t": datetime.fromisoformat(r["t"]), "c": float(r["c"])}
                                                        for r in rows]), Costs()) for sym, rows in raw.items()}
        print(json.dumps(out, indent=1, default=str))
        return
    if args.dump:
        Path(args.dump).write_text(json.dumps(raw))
    report: dict[str, dict] = {}
    for sym, rows in raw.items():
        bars = [{"t": datetime.fromisoformat(r["t"]), "c": float(r["c"])} for r in rows]
        days = days_from_bars(bars)
        report[sym] = {
            "desk_days": evaluate(days, Costs()),
            "all_days": evaluate(days, Costs(), desk_days=False),
            "ko_spread_0.2pct": evaluate(days, Costs(ko_spread=0.002)),
            "cfd_spread_1bp": evaluate(days, Costs(cfd_spread_bp=1.0)),
        }
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
