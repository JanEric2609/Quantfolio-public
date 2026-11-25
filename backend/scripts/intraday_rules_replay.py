"""Replay the owner's EMA 9/26 cross with ATR stops, targets, trailing exit and a volatility filter on 5-minute bars
(research only, read only). Run on prod with the stored Alpaca key, or on the pve host with saved bars.

    set -a; . /opt/quantfolio/.env; set +a
    python scripts/intraday_rules_replay.py --dump /tmp/bars5.json     # fetch SPY and QQQ 5-minute SIP bars since 2019
    python scripts/intraday_rules_replay.py --bars /tmp/bars5.json     # replay saved bars, no network

Prints a JSON table; writes no results to the database (the Alpaca read stores nothing). Rules and constants: ``app/lab/intraday_rules_replay.py``.
Cells are processed in parallel by symbol on the host's CPU threads.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lab import intraday_rules_replay as rep  # noqa: E402

SYMBOLS = ("SPY", "QQQ")
START_YEAR = 2019
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
    now = datetime.now(UTC) - SIP_DELAY
    out: dict[str, list[dict]] = {}
    for sym in SYMBOLS:
        rows: list[dict] = []
        for year in range(START_YEAR, now.year + 1):
            for q in range(4):  # quarters keep each call under the provider's page budget
                a = datetime(year, 1 + 3 * q, 1, tzinfo=UTC)
                b = datetime(year + (q == 3), 1 + 3 * (q + 1) % 12 if q < 3 else 1, 1, tzinfo=UTC)
                if a >= now:
                    break
                rows += provider.get_intraday_bars(sym, "5Min", a, min(b, now), feed="sip")
        out[sym] = [{"t": r["t"].isoformat(), "o": str(r["o"]), "h": str(r["h"]), "l": str(r["l"]), "c": str(r["c"])} for r in rows]
    return out


def _one(item: tuple[str, list[dict]]) -> tuple[str, dict]:
    sym, rows = item
    parsed = [{"t": datetime.fromisoformat(r["t"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]} for r in rows]
    return sym, rep.run({sym: parsed})[sym]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars")
    ap.add_argument("--dump")
    args = ap.parse_args()
    raw = json.loads(Path(args.bars).read_text()) if args.bars else fetch()
    if args.dump:
        Path(args.dump).write_text(json.dumps(raw))
    with ProcessPoolExecutor(max_workers=min(len(raw), 8)) as pool:
        out = dict(pool.map(_one, raw.items()))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
