"""Score a data provider's fundamentals against the hand-keyed ten-issuer truth set (WP15.4, decision D7).

    python scripts/score_truth_set.py --truth ../docs/research/truth-set/truth_set.csv --live yfinance
    python scripts/score_truth_set.py --truth truth.csv --provider-json dump.json   # offline: {ticker: {field: value}}

A field matches when the provider is within --tol (default 2 %) of the keyed figure.
Period end, period length and currency must match exactly. Output: per-field match rate and a
list of misses. Read-only: it calls a provider's public fundamentals method and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

NUMERIC = ("ebitda", "eps", "total_debt", "total_cash")
EXACT = (("currency", "currency"), ("period_end", "latest_period_end"), ("period_months", "period_months"))


def load_truth(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(line for line in fh if not line.lstrip().startswith("#"))]
    return [r for r in rows if r.get("ticker")]


def _num(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def score(truth: list[dict[str, str]], provided: dict[str, dict[str, Any]], tol: float) -> dict[str, Any]:
    """Return {"fields": {name: [hits, keyed]}, "misses": [...]}. Unkeyed cells are skipped."""
    fields: dict[str, list[int]] = {}
    misses: list[str] = []

    def tally(name: str, ok: bool, ticker: str, keyed: Any, got: Any) -> None:
        hit_keyed = fields.setdefault(name, [0, 0])
        hit_keyed[1] += 1
        if ok:
            hit_keyed[0] += 1
        else:
            misses.append(f"{ticker} {name}: keyed {keyed!r}, provider {got!r}")

    for row in truth:
        ticker = row["ticker"]
        got_row = provided.get(ticker) or {}
        for name in NUMERIC:
            keyed = _num(row.get(name))
            if keyed is None:
                continue
            got = _num(got_row.get(name))
            ok = got is not None and abs(got - keyed) <= tol * abs(keyed)
            tally(name, ok, ticker, keyed, got)
        for truth_key, prov_key in EXACT:
            keyed_text = (row.get(truth_key) or "").strip()
            if not keyed_text:
                continue
            got_text = str(got_row.get(prov_key) if got_row.get(prov_key) is not None else "").strip()
            tally(truth_key, got_text == keyed_text, ticker, keyed_text, got_text or None)
    return {"fields": fields, "misses": misses}


def _live(provider: str, tickers: list[str]) -> dict[str, dict[str, Any]]:
    from app.foundation.core.db import SessionLocal  # lazy: needs the app environment
    from app.foundation.providers.registry import build_provider_registry

    with SessionLocal() as db:
        registry = build_provider_registry(db)
    by_name = {p.name: p for p in registry.providers}
    if provider not in by_name:
        sys.exit(f"unknown provider {provider!r}; available: {sorted(by_name)}")
    out: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        result = by_name[provider].get_fundamentals(ticker)
        out[ticker] = (result.get("data") if isinstance(result, dict) else None) or {}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth", type=Path, required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--provider-json", type=Path)
    src.add_argument("--live", metavar="PROVIDER")
    ap.add_argument("--tol", type=float, default=0.02)
    args = ap.parse_args()

    truth = load_truth(args.truth)
    if args.provider_json:
        provided = json.loads(args.provider_json.read_text())
    else:
        provided = _live(args.live, [r["ticker"] for r in truth])
    result = score(truth, provided, args.tol)
    print(f"{len(truth)} issuers, tolerance {args.tol:.0%}")
    for name, (hit, keyed) in sorted(result["fields"].items()):
        print(f"  {name:14s} {hit}/{keyed}  ({hit / keyed:.0%})")
    for miss in result["misses"]:
        print("  MISS", miss)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
