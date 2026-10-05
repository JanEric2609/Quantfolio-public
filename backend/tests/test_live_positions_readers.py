"""Synced positions are read through ``app.foundation.live_positions``.

Only the broker syncs, their repair tools and the ticker resolver touch the
position tables directly; everything else reads ``live_positions`` /
``combined_positions`` and so sees DKB and Scalable alike. A module that reads
``DkbPosition`` itself sees DKB only, which is how most of the app came to
ignore the Scalable depot. The lists below may only shrink: a new direct
reader fails here, and so does an entry whose file no longer needs it.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Writers and DKB-only tools: the DKB sync, the snapshot/holdings mirror it
# runs, the DKB endpoints, the admin repair job, the ISIN->ticker resolver
# (writes tickers onto both tables) and live_positions itself.
DKB_POSITION_ALLOWED = {
    "foundation/dkb/service.py",
    "foundation/live_positions.py",
    "foundation/portfolio/isin_resolver.py",
    "foundation/portfolio_service.py",
    "interface/api/admin_repair.py",
    "interface/api/dkb.py",
}

BROKER_POSITION_ALLOWED = {
    "foundation/broker_status.py",
    "foundation/live_positions.py",
    "foundation/portfolio/isin_resolver.py",
    "foundation/scalable/service.py",
}


def _files_naming(model: str) -> set[str]:
    pattern = re.compile(rf"\b{model}\b")
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("foundation/models/"):
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(rel)
    return found


def test_only_known_modules_read_dkb_positions_directly():
    found = _files_naming("DkbPosition")
    assert found - DKB_POSITION_ALLOWED == set(), (
        "Read synced positions through app.foundation.live_positions, not DkbPosition "
        "(it misses Scalable)"
    )
    assert DKB_POSITION_ALLOWED - found == set(), "No longer reads DkbPosition: drop it from the list"


def test_only_known_modules_read_broker_positions_directly():
    found = _files_naming("BrokerPosition")
    assert found - BROKER_POSITION_ALLOWED == set(), (
        "Read synced positions through app.foundation.live_positions, not BrokerPosition"
    )
    assert BROKER_POSITION_ALLOWED - found == set(), "No longer reads BrokerPosition: drop it from the list"
