"""The signals the nightly forward-test lab scores (plan 2026-11-09, §2).

Every one has a published record and none is a new idea: the lab exists to
check, going forward, whether each still works on today's MSCI World names in
EUR, not to search for new ones (a larger search would raise the bar for every
idea, Harvey, Liu & Zhu 2016). The list is fixed in advance; adding a signal is
a code change with a reason, never a tuning step.

Each price signal takes one stock's EUR closes (oldest first, trading days of
the panel) and the benchmark's, and returns a score where higher means
"predicted to do better", or ``None`` when the history is too short.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from app.foundation import price_signals as ps

#: Calendar days of closes loaded per name: 24 months of residual-momentum
#: regression plus the skipped month, with slack for holidays.
HISTORY_DAYS = 800


@dataclass(frozen=True)
class Signal:
    name: str
    label: str
    source: str
    score: Callable[[np.ndarray, np.ndarray, list[str]], float | None] | None = None


def _neg(value: float | None) -> float | None:
    return None if value is None else -value


def _low_beta(closes: np.ndarray, bench: np.ndarray, _dates: list[str]) -> float | None:
    if len(closes) < 253:
        return None
    rc = closes[-253:]
    rb = bench[-253:]
    beta = ps.welch_beta(list(rc[1:] / rc[:-1] - 1.0), list(rb[1:] / rb[:-1] - 1.0))
    return _neg(beta)


PRICE_SIGNALS: tuple[Signal, ...] = (
    Signal(
        "momentum_12_1", "12-1 month momentum", "Jegadeesh & Titman 1993",
        lambda c, _b, _d: ps.momentum_12_1(c),
    ),
    Signal(
        "momentum_without_largest_day", "Momentum without its largest day (Discover's input)",
        "Da, Gurun & Warachka 2014",
        lambda c, _b, _d: ps.momentum_without_largest_day(c),
    ),
    Signal(
        "residual_momentum", "Residual momentum", "Blitz, Huij & Martens 2011",
        lambda c, b, d: ps.residual_momentum(d, c, b) if len(c) >= 520 else None,
    ),
    Signal(
        "high_52w_ratio", "Nearness to the 52-week high", "George & Hwang 2004",
        lambda c, _b, _d: ps.high_52w_ratio(c),
    ),
    Signal(
        "steady_climb", "Steady climb (sign-balance of the momentum days)", "Da, Gurun & Warachka 2014",
        lambda c, _b, _d: _neg(ps.information_discreteness(c)),
    ),
    Signal(
        "low_volatility", "Low volatility", "Ang, Hodrick, Xing & Zhang 2006; Blitz & van Vliet 2007",
        lambda c, _b, _d: _neg(ps.volatility(c, 126)),
    ),
    Signal(
        "short_term_reversal", "Last month's losers", "Jegadeesh 1990; Lehmann 1990",
        lambda c, _b, _d: _neg(ps.return_over(c, 21)),
    ),
    Signal("low_beta", "Low market beta", "Frazzini & Pedersen 2014", _low_beta),
)

#: Scored from the overnight LLM reading of headlines (``news_tone``), not prices.
NEWS_SIGNAL = Signal(
    "news_tone", "Overnight AI reading of the headlines (7 nights)",
    "Lopez-Lira & Tang 2023; Glasserman & Lin 2023",
)

SIGNALS: tuple[Signal, ...] = (*PRICE_SIGNALS, NEWS_SIGNAL)
SIGNAL_NAMES: tuple[str, ...] = tuple(s.name for s in SIGNALS)
BY_NAME: dict[str, Signal] = {s.name: s for s in SIGNALS}

__all__ = ["BY_NAME", "HISTORY_DAYS", "NEWS_SIGNAL", "PRICE_SIGNALS", "SIGNALS", "SIGNAL_NAMES", "Signal"]
