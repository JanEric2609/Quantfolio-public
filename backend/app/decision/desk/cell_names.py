"""Plain names for the desk's scored cells, for messages and the weekly digest (a leaf module, no imports)."""
from __future__ import annotations

CELL_NAMES: dict[str, str] = {
    "ta_trend:cfd": "EMA cross",
    "ta_band:cfd": "Bollinger fade",
    "ta_rsi2:cfd": "RSI(2) dip in an uptrend",
    "hype_fade:cfd": "hype fade",
    "news_drift:cfd": "news drift",
    "owner_call:cfd": "your /idea call",
    "owner_call:knockout": "your /idea call (knock-out)",
    "owner_call:warrant": "your /idea call (warrant)",
    "index_momentum:cfd": "index rule (CFD)",
    "index_momentum:knockout": "index rule (knock-out)",
    "index_momentum:warrant": "index rule (warrant)",
}


def cell_name(cell: str) -> str:
    """The plain name of a cell id, or the id itself for an unknown one."""
    return CELL_NAMES.get(cell, cell)
