"""Analyse your own closed Trading 212 CFD trades from the app's history export (research, read only).

    python scripts/cfd_journal_report.py history.csv            # prints the report
    python scripts/cfd_journal_report.py history.csv --json     # machine-readable

Export: Trading 212 > a CFD position's History > download icon (CSV). Columns are matched by name, loosely
(open/close time, instrument, direction, quantity, open/close price, result); unmatched columns are ignored.
Reads one local file, writes nothing, and uses no database or network.

The export's Spread column is the spread per unit (it does not scale with Units); the report multiplies it by the
units for the round trip's cost. Check: Tech 100 comes out near 0.7 bp, the same as the live quote.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lab.trade_journal import JournalTrade, analyse  # noqa: E402

ALIASES = {
    "instrument": ("instrument", "name", "ticker", "symbol", "market"),
    "direction": ("direction", "side", "action", "type"),
    "opened": ("open time", "opened", "entered", "entry time", "time opened", "open date"),
    "closed": ("close time", "closed", "exit time", "time closed", "close date", "time"),
    "open_price": ("open price", "average price", "entry price", "price"),
    "close_price": ("close price", "exit price"),
    "quantity": ("quantity", "units", "no. of shares", "shares"),
    "result": ("result", "profit", "profit/loss", "p/l", "pnl", "total", "total result"),
}


def _pick(header: list[str], field: str) -> str | None:
    low = {h.lower().strip(): h for h in header}
    for a in ALIASES[field]:
        if a in low:
            return low[a]
    for h in header:  # loose containment
        if any(a in h.lower() for a in ALIASES[field]):
            return h
    return None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace("€", "").replace("$", "").replace(" ", "")
    if not v or v in ("-", "N/A"):
        return None
    if "," in v and "." not in v:
        v = v.replace(",", ".")
    v = v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _time(v: str | None) -> datetime | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v.strip()[:19], fmt)
        except ValueError:
            continue
    return None


def _norm(name: str) -> str:
    """'Total result (account currency)' -> 'totalresult'; 'Record Type' and 'RecordType' agree."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", name.lower()))


def _row(r: dict[str, str]) -> dict[str, str]:
    return {_norm(k): (v or "") for k, v in r.items() if k}


def _is_closed(rec: str) -> bool:
    return _norm(rec) == "closedposition"


def _load_t212_export(rows: list[dict[str, str]]) -> list[JournalTrade]:
    """The Trading 212 CFD export: 'Closed position' rows (``RecordType`` ``CLOSED_POSITION`` in the help-centre
    wording), joined to the executed closing order by Position ID for the exit type. Header names are matched without
    case, spaces, underscores and the bracketed currency note."""
    norm = [_row(r) for r in rows]
    exit_by_pos: dict[str, str] = {}
    for r in norm:
        if _norm(r.get("recordtype", "")) == "order" and _norm(r.get("intent", "")) == "close" \
                and _norm(r.get("status", "")) == "executed":
            exit_by_pos[r.get("positionid", "")] = r.get("ordertype", "")
    out = []
    for r in norm:
        if not _is_closed(r.get("recordtype", "")):
            continue
        res = _num(r.get("totalresult") or r.get("result"))
        closed = _time(r.get("dateclosed") or r.get("date"))
        if res is None or closed is None:
            continue
        # The Spread column is the spread per unit in account currency (it does not scale with Units): the cost
        # of the round trip is column x units; in index points it is column / exchange rate.
        units, avg, fx, sp = _num(r.get("units")), _num(r.get("averageprice")), _num(r.get("exchangerate")), _num(r.get("spread"))
        out.append(JournalTrade(
            instrument=(r.get("instrument") or "?").strip(),
            direction="short" if r.get("direction", "").lower().startswith("sell") else "long",
            opened=_time(r.get("dateopened")), closed=closed, result_eur=res,
            open_price=avg, close_price=_num(r.get("closeprice")), quantity=units,
            exit_type=exit_by_pos.get(r.get("positionid", "")) or None,
            spread_eur=sp * units if sp is not None and units else None,
            spread_bp=sp / fx / avg * 1e4 if sp is not None and fx and avg else None))
    return out


def load(path: Path) -> tuple[list[JournalTrade], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows and "recordtype" in {_norm(k) for k in rows[0] if k}:
        trades = _load_t212_export(rows)
        if not trades:
            raise SystemExit("Trading 212 export recognised (RecordType column) but it holds no closed positions with a "
                             f"result and close time. Headers seen: {', '.join(k for k in rows[0] if k)}")
        return trades, ["Trading 212 export: closed positions, exit type from the closing order"]
    header = list(rows[0]) if rows else []
    cols = {k: _pick(header, k) for k in ALIASES}
    notes = [f"{k} <- {v or 'NOT FOUND'}" for k, v in cols.items()]
    trades = []
    for r in rows:
        res = _num(r.get(cols["result"] or ""))
        closed = _time(r.get(cols["closed"] or ""))
        if res is None or closed is None:
            continue
        d = (r.get(cols["direction"] or "") or "").lower()
        direction = "short" if d.startswith(("sell", "short")) else "long"
        trades.append(JournalTrade(
            instrument=(r.get(cols["instrument"] or "") or "?").strip(), direction=direction,
            opened=_time(r.get(cols["opened"] or "")), closed=closed, result_eur=res,
            open_price=_num(r.get(cols["open_price"] or "")), close_price=_num(r.get(cols["close_price"] or "")),
            quantity=_num(r.get(cols["quantity"] or ""))))
    if not trades:
        raise SystemExit("No trades could be read. Column mapping: " + "; ".join(notes) +
                         ". Expected the Trading 212 CFD history export (a RecordType column) or columns for close time and result.")
    return trades, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--instrument", help="only trades whose instrument name contains this text, e.g. 'Tech 100'")
    args = ap.parse_args()
    trades, notes = load(Path(args.csv))
    if args.instrument:
        trades = [t for t in trades if args.instrument.lower() in t.instrument.lower()]
    report = analyse(trades)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
        return
    print("column mapping:", "; ".join(notes))
    print(f"{len(trades)} closed trades read")
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
