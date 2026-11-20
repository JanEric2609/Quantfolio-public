"""One-time backfill: populate bar_prices from the healthy PriceCache table.

ADR 0014. bar_prices/factor_loadings_daily/regime_snapshots/
provider_health_history never accepted a write in production (see
alembic/versions/0106_restore_hypertable_writes.py for the schema fix) —
bar_prices had 0 rows while price_cache held ~458k rows across ~315 tickers.
This script is the one-time data catch-up: it does NOT touch the schema, only
copies price_cache rows into bar_prices via the same
``ON CONFLICT (symbol, ts) DO UPDATE`` upsert DataIngester.ingest_bar_prices
uses, so it is idempotent and safe to re-run.

Run AFTER 0106 has been applied (``alembic upgrade head``) — bar_prices must
already have its id default and unique index, or every insert here will fail
exactly as production ingestion did.

Dry-run by default: prints row counts without writing. Pass --apply to
actually commit. Run manually against prod only once approved — this is not
part of any migration or scheduled job.

Usage (run from backend/):
    .venv/bin/python scripts/backfill_bar_prices.py            # dry run
    .venv/bin/python scripts/backfill_bar_prices.py --apply    # writes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import PriceCache  # noqa: E402

BATCH_SIZE = 500

_UPSERT_SQL = text("""
    INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
    VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
    ON CONFLICT (symbol, ts) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        currency = EXCLUDED.currency,
        provider = EXCLUDED.provider
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        total = db.query(PriceCache).count()
        print(f"price_cache has {total} rows to backfill into bar_prices")
        if not args.apply:
            print("Dry run — pass --apply to write.")
            return

        written = 0
        offset = 0
        while True:
            rows = (
                db.query(PriceCache)
                .order_by(PriceCache.id)
                .offset(offset)
                .limit(BATCH_SIZE)
                .all()
            )
            if not rows:
                break
            params = [
                {
                    "symbol": row.ticker,
                    "ts": row.date,
                    "open": float(row.open) if row.open is not None else float(row.close),
                    "high": float(row.high) if row.high is not None else float(row.close),
                    "low": float(row.low) if row.low is not None else float(row.close),
                    "close": float(row.close),
                    "volume": int(row.volume) if row.volume is not None else 0,
                    "currency": row.currency or "EUR",
                    "provider": row.source or "price_cache_backfill",
                }
                for row in rows
            ]
            db.execute(_UPSERT_SQL, params)
            db.commit()
            written += len(rows)
            offset += BATCH_SIZE
            print(f"  ... {written}/{total}")

        print(f"Backfilled {written} rows into bar_prices.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
