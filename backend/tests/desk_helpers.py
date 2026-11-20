"""Shared fixtures for the desk tests: a fake ``sc`` runner and fixed times. Fake IDs only.

Each test file defines its own ``_memory_db()`` (AGENTS.md); nothing database-side is shared here."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.foundation.scalable.cli import ScalableCli, ScalableCliConfig

BERLIN = ZoneInfo("Europe/Berlin")
DAX = "DE0008469008"
DAX_ETF = "DE0005933931"
# Wednesday, Xetra open, inside the trading window.
NOW = datetime(2026, 11, 18, 11, 0, tzinfo=BERLIN)


def make_user(db, role="admin", name="owner"):
    from app.foundation.models.entities import User

    user = User(username=name, password_hash="x", role=role)
    db.add(user)
    db.commit()
    return user


def quote_result(isin, bid, ask, now=NOW, age=10, outdated=False):
    ts = (now - timedelta(seconds=age)).astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    return {"isin": isin, "name": "Fake", "security_type": "DERIVATIVE", "quote_bid_price": bid,
            "quote_ask_price": ask, "quote_mid_price": float((Decimal(str(bid)) + Decimal(str(ask))) / 2),
            "quote_currency": "EUR", "quote_is_outdated": outdated, "quote_timestamp_utc": ts}


def ko_item(isin, lev=4.0, distance=0.25, issuer="VONTOBEL", strategy="long"):
    return {"isin": isin, "issuer": issuer, "strategy": strategy, "product_subcategory": "TURBO", "leverage": lev,
            "knockout_barrier": {"kind": "x", "value": 1}, "distance_to_knockout": distance, "strike": {"value": 1},
            "expiry_is_open_end": True, "expiry_date": None, "underlying_isin": DAX}


def daily_points(n=40, start=Decimal(100), step=Decimal("1.0"), now=NOW):
    """n daily points ending yesterday with alternating +/- moves (sigma about 1 %)."""
    pts, px = [], start
    for i in range(n):
        px = px + (step if i % 2 else -step)
        ts = (now - timedelta(days=n - i)).astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
        pts.append({"mid_price": float(px), "timestamp_utc": ts})
    return pts


class FakeSc:
    """Routes argv to a handler returning the ``result``/data dict, and records the calls."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        args = [a for a in argv[1:] if a != "--json"]
        self.calls.append(args)
        data = self.handler(args)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "command": "x", "data": data}).encode(), b"")

    def cli(self):
        return ScalableCli(ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="", max_attempts=1,
                                             trusted_wrapper_only=False), runner=self)


def flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def standard_handler(quotes: dict, listings: dict | None = None, chart_pts=None, now=NOW):
    """quotes: isin -> (bid, ask). listings: (type) -> items."""
    def handle(args):
        if args[:2] == ["broker", "quote"]:
            isin = flag(args, "--isin")
            if isin in quotes:
                bid, ask = quotes[isin]
                return {"result": quote_result(isin, bid, ask, now=now)}
            return {"result": {"isin": isin, "quote_bid_price": None, "quote_ask_price": None}}
        if args[:3] == ["broker", "derivatives", "search"]:
            items = (listings or {}).get(flag(args, "--type"), [])
            return {"result": {"total_available": len(items), "count": len(items), "items": items}}
        if args[:2] == ["broker", "chart"]:
            return {"data_points": chart_pts if chart_pts is not None else daily_points()}
        if args[:2] == ["broker", "security-news"]:
            return {"result": {"items": []}}
        raise AssertionError(f"unexpected sc call {args}")
    return handle
