"""One-time repair of stored bars after the 2026-10-12 audit (plan WP1d, D1/D2).

What it does, per symbol in ``bar_prices`` (or ``--symbols``):

1. runs the ingest guard (``data_backbone.bar_guard``) over the full stored
   history and prints what it would quarantine;
2. plans a full re-pull (``DataIngester.repull_series``) for every symbol
   whose history mixes providers (the 2021-10-13 Twelve Data -> yfinance
   splice on SPY/URTH/VGK, ``eod`` bars off the yfinance scale) or shows a
   split signature (P500.DE 100:1, ATO.PA reverse split) or a spike;
3. deletes bars dated on non-sessions of the listing's exchange (SPY
   2025-12-25);
4. the re-pull replaces ``eod`` bars and drops the symbol's ``price_cache``
   rows, which sit on the old scale.

Before any delete it lists compressed TimescaleDB chunks of ``bar_prices``:
a DELETE on a compressed chunk needs ``decompress_chunk`` first. With
``--apply`` it decompresses the chunks it lists.

Dry-run by default. Writing to prod needs the owner's explicit OK.

Usage (run from backend/):
    .venv/bin/python scripts/repair_bars_2026_10.py                      # dry run, every symbol
    .venv/bin/python scripts/repair_bars_2026_10.py --symbols SPY URTH   # dry run, some symbols
    .venv/bin/python scripts/repair_bars_2026_10.py --apply              # writes
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.data_backbone.bar_guard import guard_bars, guard_class_for, is_session  # noqa: E402

#: Known targets from the audit; scanned first so the report leads with them.
KNOWN_TARGETS = [
    "P500.DE", "I500.DE", "SGAS.DE", "8PSG.DE", "MWOE.DE", "PR1J.DE", "AVB", "ATO.PA",
    "SPY", "URTH", "VGK", "EWJ", "VWCE.DE", "EUNL.DE", "SXR8.DE",
]
#: Pairs whose yearly EUR returns should agree within 0.3 pp once repaired.
REFERENCE_PAIRS = [("URTH", "EUNL.DE"), ("SPY", "SXR8.DE")]
#: Year-end level = mean of this many last closes (see ``yearly_eur_returns``).
YEAR_END_DAYS = 5


def _history(db: Session, symbol: str) -> pd.DataFrame:
    rows = db.execute(
        text("SELECT ts, close, provider FROM bar_prices WHERE symbol = :s ORDER BY ts"), {"s": symbol},
    ).fetchall()
    return pd.DataFrame([tuple(r) for r in rows], columns=pd.Index(["ts", "close", "provider"]))


def scan_symbol(db: Session, symbol: str) -> dict[str, Any]:
    """What the repair would do for one symbol (no writes)."""
    hist = _history(db, symbol)
    if hist.empty:
        return {"symbol": symbol, "rows": 0}
    providers = sorted(set(hist["provider"].astype(str)))
    pinned = str(hist["provider"].iloc[-1])
    result = guard_bars(hist, None, symbol=symbol, guard_class=guard_class_for(db, symbol),
                        pinned_provider=pinned, allow_split=True, check_calendar=False)
    non_session = [pd.Timestamp(t).date().isoformat() for t in hist["ts"] if not is_session(symbol, pd.Timestamp(t))]
    spikes = [(q.ts.date().isoformat(), q.reason) for q in result.quarantined]
    needs_repull = len(providers) > 1 or result.split_suspected or bool(spikes)
    return {
        "symbol": symbol,
        "rows": len(hist),
        "providers": providers,
        "split_signature": result.split_suspected,
        "suspect_bars": spikes,
        "non_session": non_session,
        "repull": needs_repull,
        # The stored bars behind ``suspect_bars``, for when the re-pull fails.
        "_suspect": [replace(q, stored=True) for q in result.quarantined],
    }


def repair_symbol(db: Session, ingester: Any, plan: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one symbol's plan; returns the re-pull result, or None without a re-pull.

    A re-pull that fails or is refused (a delisted ticker returns a stub, see
    ``DataIngester._repull_too_short``) leaves the stored series, so its
    suspect bars are moved to ``bar_quarantine`` here instead: AVB's x2.8
    close on 2026-08-14 otherwise stays in ``bar_prices``.
    """
    symbol = plan["symbol"]
    for day in plan["non_session"]:
        db.execute(
            text("DELETE FROM bar_prices WHERE symbol = :s AND CAST(ts AS DATE) = :d"),
            {"s": symbol, "d": day},
        )
    db.commit()
    if not plan["repull"]:
        return None
    result = ingester.repull_series(symbol)
    if not result.get("success") and plan["_suspect"]:
        ingester._write_quarantine(symbol, plan["_suspect"])
        db.commit()
        result["quarantined"] = len(plan["_suspect"])
    return result


def compressed_chunks(db: Session) -> list[str]:
    try:
        rows = db.execute(text(
            "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'bar_prices' AND is_compressed"
        )).fetchall()
    except Exception:  # noqa: BLE001 - not TimescaleDB (sqlite, plain Postgres)
        db.rollback()
        return []
    return [r[0] for r in rows]


def _eur_closes(db: Session, symbol: str) -> dict[str, float]:
    from app.foundation.eur_prices import to_eur

    hist = _history(db, symbol)
    closes = {pd.Timestamp(t).date().isoformat(): float(c) for t, c in zip(hist["ts"], hist["close"])}
    eur, _ = to_eur(db, symbol, closes, days=365 * 6)
    return eur


def yearly_eur_returns(
    eur: dict[str, float], days: list[str], years: tuple[int, ...] = (2023, 2024, 2025),
) -> dict[int, float | None]:
    """Calendar-year returns of ``eur`` measured on ``days`` (the sessions both series of a pair traded)."""

    def _level(cutoff: str) -> float | None:
        # Mean of the last YEAR_END_DAYS closes up to the cutoff: a US listing
        # closes at 22:00 CET and Xetra at 17:30, so a single year-end close
        # differs by up to ~0.5 pp between two series of the same index.
        window = [eur[d] for d in days if d <= cutoff][-YEAR_END_DAYS:]
        return sum(window) / len(window) if len(window) == YEAR_END_DAYS else None

    out: dict[int, float | None] = {}
    for year in years:
        start, end = _level(f"{year - 1}-12-31"), _level(f"{year}-12-31")
        out[year] = (end / start - 1.0) if start and end else None
    return out


def reference_table(db: Session) -> list[str]:
    lines = ["| pair | year | a | b | gap (pp) |", "|---|---|---|---|---|"]
    for a, b in REFERENCE_PAIRS:
        ea, eb = _eur_closes(db, a), _eur_closes(db, b)
        # Common sessions only: Xetra is shut on 24/26/31 Dec while NYSE trades.
        common = sorted(set(ea) & set(eb))
        ra, rb = yearly_eur_returns(ea, common), yearly_eur_returns(eb, common)
        for year in ra:
            x, y = ra[year], rb.get(year)
            gap = f"{(x - y) * 100:+.2f}" if x is not None and y is not None else "n/a"
            fx = f"{x:.2%}" if x is not None else "n/a"
            fy = f"{y:.2%}" if y is not None else "n/a"
            lines.append(f"| {a} vs {b} | {year} | {fx} | {fy} | {gap} |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (needs the owner's OK on prod)")
    parser.add_argument("--symbols", nargs="*", help="limit to these symbols")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.symbols:
            symbols = args.symbols
        else:
            stored = [r[0] for r in db.execute(text("SELECT DISTINCT symbol FROM bar_prices"))]
            symbols = [s for s in KNOWN_TARGETS if s in stored] + sorted(s for s in stored if s not in KNOWN_TARGETS)

        print("## Before\n")
        print("\n".join(reference_table(db)))
        print()

        plans = [scan_symbol(db, s) for s in symbols]
        todo = [p for p in plans if p.get("repull") or p.get("non_session")]
        print(f"## Plan: {len(todo)} of {len(plans)} symbols need work\n")
        print("| symbol | rows | providers | split | suspect bars | non-session |")
        print("|---|---|---|---|---|---|")
        for p in todo:
            print(
                f"| {p['symbol']} | {p['rows']} | {', '.join(p['providers'])} | "
                f"{'yes' if p['split_signature'] else ''} | {len(p['suspect_bars'])} "
                f"{p['suspect_bars'][:3]} | {', '.join(p['non_session'][:5])} |"
            )

        chunks = compressed_chunks(db)
        print(f"\nCompressed bar_prices chunks: {len(chunks)}")
        if not args.apply:
            print("\nDry run: nothing written. Re-run with --apply after the owner's OK.")
            return 0

        for chunk in chunks:
            db.execute(text("SELECT decompress_chunk(:c, if_compressed => true)"), {"c": chunk})
        db.commit()

        from app.foundation.data_backbone.ingest import DataIngester

        ingester = DataIngester(db)
        results: list[dict[str, Any]] = []
        for p in todo:
            outcome = repair_symbol(db, ingester, p)
            if outcome is not None:
                results.append(outcome)
        print("\n## Re-pulls\n")
        print("| symbol | ok | provider | rows | quarantined | split still unadjusted |")
        print("|---|---|---|---|---|---|")
        for r in results:
            print(
                f"| {r.get('symbol')} | {r.get('success')} | {r.get('provider', '')} | "
                f"{r.get('rows_inserted', 0)} | {r.get('quarantined', 0)} | {r.get('split_unadjusted', '')} |"
            )
        print("\n## After\n")
        print("\n".join(reference_table(db)))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
