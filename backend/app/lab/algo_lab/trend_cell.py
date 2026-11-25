"""The slow-trend paper cell as pure rules on a list of closes (pre-registered 2026-11-25, research only).

One rule, frozen before any result was read; changing a constant makes it a new trial:

* entry: the close is above the highest close of the previous ``BREAKOUT_N`` (100) days -> long, below the lowest ->
  short; filled at the next open, one position per instrument;
* exit, state-based, no clock: the close crosses back through the ``EMA_N`` (50) day EMA against the position, or a
  ``STOP_ATR`` (5) x ATR(``ATR_N``=20) stop taken at entry is hit (the replay also tests a pure trailing version);
* sizing by risk, costs and financing are charged by the caller (``lab/trend_replay``), never here.

Why this rule: it is the one family with decades of out-of-sample evidence in the report
(docs/research/2026-11-25-event-cfd-edge-research.md, section 3.11): slow, discrete (stop-based) trend systems on
commodities, bonds, currencies and indices. The evidence is a diversified portfolio of liquid futures; a handful of
CFDs pays several times the costs, so the prior for a net edge is modest and the replay is the test. Pure functions on
closes: no database, no broker, no order. Gates nothing.
"""
from __future__ import annotations

CELL_ID = "trend100_state"
BREAKOUT_N = 100
EMA_N = 50
ATR_N = 20
STOP_ATR = 5.0


def breakout_signals(closes: list[float], n: int = BREAKOUT_N) -> dict[int, str]:
    """Index of the breakout close -> side to enter at the next open. A flat or insufficient history gives no signal."""
    out: dict[int, str] = {}
    for i in range(n, len(closes) - 1):
        window = closes[i - n:i]
        if closes[i] > max(window):
            out[i] = "long"
        elif closes[i] < min(window):
            out[i] = "short"
    return out


def ema_exit(closes: list[float], side: str, n: int = EMA_N) -> bool:
    """True when the last close sits on the wrong side of its ``n``-day EMA for ``side`` (the state exit). Needs ``n`` closes."""
    if side not in ("long", "short") or len(closes) < n:
        return False
    k = 2 / (n + 1)
    ema = sum(closes[:n]) / n
    for v in closes[n:]:
        ema = v * k + ema * (1 - k)
    return closes[-1] < ema if side == "long" else closes[-1] > ema


def next_action(closes: list[float], position: str | None) -> str:
    """The EMA-only part of the cell's decision for the latest close: ``enter_long`` / ``enter_short`` / ``exit`` /
    ``hold`` / ``wait``. It does **not** evaluate the 5 ATR stop (that needs the entry price and the bar's high and
    low), so it is not yet the full rule a daily paper cell would score; ``lab/trend_replay`` is the reference."""
    if position is None:
        sig = breakout_signals(closes + [closes[-1]]) if len(closes) > BREAKOUT_N else {}
        side = sig.get(len(closes) - 1)
        return f"enter_{side}" if side else "wait"
    return "exit" if ema_exit(closes, position) else "hold"
