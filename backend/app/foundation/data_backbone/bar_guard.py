"""Ingest guard for daily bars: calendar, spikes, splits, provider scale (plan 2026-10-12, D2).

A pure function over a provider's new bars and the bars already stored for
the symbol. It decides which new bars go to ``bar_prices`` and which go to
``bar_quarantine``, with a reason. It replaces the old ">50 % warn" block in
``DataIngester.ingest_bar_prices``, which logged and stored everything.

What it catches, each seen on prod (audit 2026-10-12, H2):

* ``non_session``: a bar dated on a day the listing's exchange was closed
  (SPY on 2025-12-25).
* ``spike_reversal``: a one-day move above the asset-class limit that the
  next bar takes back (I500/SGAS/8PSG/MWOE/P500 on 2025-10-24: +17 %, then
  -13 %; PR1J x178).
* ``split_signature``: a move above 40 % that is not taken back and whose
  price ratio sits within 2 % of a split ratio (P500.DE 100:1 on 2025-12-15).
  The caller re-pulls the series; a split still there after the re-pull keeps
  the symbol out of Discover (``bar_series_meta.quarantine_reason``).
* ``provider_scale``: a move above the limit on a bar from a provider other
  than the pinned one (``eod`` bars off the yfinance scale).

A real crash day without reversal (SPY 2020-03-16, -12 %) is accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import logging
from typing import Any, Literal, cast

import pandas as pd

logger = logging.getLogger(__name__)

GuardClass = Literal["equity_etf", "bond_mm_etf", "gold", "single_stock"]

#: One-day move above which a bar is suspect, per asset class (D2).
MOVE_LIMITS: dict[str, float] = {
    "equity_etf": 0.12,
    "bond_mm_etf": 0.05,
    "gold": 0.10,
    "single_stock": 0.40,
}
#: A reversal must take back at least this share of the spike.
REVERSAL_SHARE = 0.70
#: Moves above this, not reversed, are checked for a split ratio.
SPLIT_MOVE = 0.40
SPLIT_RATIOS = (2, 3, 4, 5, 10, 20, 50, 100)
SPLIT_TOLERANCE = 0.02
#: A second provider confirms a quarantined bar when its close is this close.
CONFIRM_TOLERANCE = 0.01

# yfinance suffix -> exchange_calendars code. No suffix means a US listing.
_SUFFIX_CALENDARS: dict[str, str] = {
    "DE": "XETR", "F": "XFRA", "L": "XLON", "PA": "XPAR", "AS": "XAMS", "MC": "XMAD",
    "MI": "XMIL", "SW": "XSWX", "BR": "XBRU", "LS": "XLIS", "VI": "XWBO", "ST": "XSTO",
    "CO": "XCSE", "HE": "XHEL", "OL": "XOSL", "IR": "XDUB", "T": "XTKS", "HK": "XHKG",
    "TO": "XTSE", "AX": "XASX",
}

_GOLD_KEYWORDS = ("gold", "xetra-gold", "physical gold", "goldbarren")
_GOLD_TICKERS = frozenset({"4GLD.DE", "EWG2.DE", "PPFB.DE", "SGLN.L", "IGLN.L", "PHAU.L", "XAD5.DE", "GLD", "IAU"})
#: ETFs that may have no ``Asset`` row: the benchmarks Discover ingests.
_KNOWN_ETFS = frozenset({"SPY", "URTH", "VGK", "EWJ", "VWCE.DE", "VWRA.L", "IWDA.AS", "SXR8.DE", "EUNL.DE",
                         "CBE3.L", "EUNH.DE", "EUNA.DE", "XEON.DE"})


@dataclass(frozen=True)
class QuarantinedBar:
    ts: pd.Timestamp
    open: float | None
    high: float | None
    low: float | None
    close: float
    provider: str
    reason: str
    stored: bool = False  # True when the bar is already in bar_prices


@dataclass
class GuardResult:
    accepted: pd.DataFrame
    quarantined: list[QuarantinedBar] = field(default_factory=list)
    split_suspected: bool = False
    #: Each split signature: the day and its close ratio to the day before.
    split_days: list[tuple[pd.Timestamp, float]] = field(default_factory=list)

    @property
    def stored_to_quarantine(self) -> list[QuarantinedBar]:
        return [q for q in self.quarantined if q.stored]


def calendar_code(symbol: str) -> str | None:
    """The exchange_calendars code for a yfinance-style symbol; None for indices/FX."""
    sym = symbol.upper()
    if sym.startswith("^") or "=" in sym or "/" in sym:
        return None
    if "." in sym:
        suffix = sym.rsplit(".", 1)[1]
        if suffix in _SUFFIX_CALENDARS:
            return _SUFFIX_CALENDARS[suffix]
        if len(suffix) == 1:  # US share class, BRK.B
            return "XNYS"
        return None
    return "XNYS"


@lru_cache(maxsize=32)
def _calendar(code: str) -> Any:
    import exchange_calendars as xc

    return xc.get_calendar(code, start="1995-01-01")


def is_session(symbol: str, day: pd.Timestamp) -> bool:
    """False only when the exchange is known and was closed on ``day``."""
    code = calendar_code(symbol)
    if code is None:
        return True
    try:
        cal = _calendar(code)
    except Exception:  # noqa: BLE001 - an unknown calendar must not block ingestion
        logger.debug("bar_guard: no calendar %s for %s", code, symbol, exc_info=True)
        return True
    day = _day(day)
    if day < cal.first_session or day > cal.last_session:
        return True
    return bool(cal.is_session(day))


def _day(value: Any) -> pd.Timestamp:
    """``value`` as a tz-naive midnight Timestamp."""
    ts = cast(pd.Timestamp, pd.Timestamp(value))
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], frame.to_dict(orient="records"))


def _split_like(ratio: float) -> bool:
    for k in SPLIT_RATIOS:
        for target in (1.0 / k, float(k)):
            if abs(ratio / target - 1.0) <= SPLIT_TOLERANCE:
                return True
    return False


def _is_reversal(r0: float, r1: float | None) -> bool:
    if r1 is None or r0 == 0 or (r0 > 0) == (r1 > 0):
        return False
    return abs(r1) >= REVERSAL_SHARE * abs(r0) / (1.0 + r0)


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _quarantined(row: Any, reason: str, *, stored: bool) -> QuarantinedBar:
    return QuarantinedBar(
        ts=cast(pd.Timestamp, pd.Timestamp(row["ts"])),
        open=_as_float(row.get("open")),
        high=_as_float(row.get("high")),
        low=_as_float(row.get("low")),
        close=float(row["close"]),
        provider=str(row.get("provider") or "unknown"),
        reason=reason,
        stored=stored,
    )


def guard_bars(
    new: pd.DataFrame,
    stored: pd.DataFrame | None = None,
    *,
    symbol: str,
    guard_class: str = "single_stock",
    pinned_provider: str | None = None,
    allow_split: bool = False,
    check_calendar: bool = True,
) -> GuardResult:
    """Split ``new`` bars into accepted and quarantined.

    ``new`` and ``stored`` carry at least ``ts`` and ``close`` (``provider``,
    ``open``/``high``/``low`` optional). Only the newest stored bar can be
    moved to quarantine (``stored=True``): it is the one whose reversal a
    later ingest can reveal. Older stored history is left to the repair
    script. ``allow_split=True`` (the re-pull path) flags a split signature
    without quarantining the bars after it.
    """
    limit = MOVE_LIMITS.get(guard_class, MOVE_LIMITS["single_stock"])
    result = GuardResult(accepted=new.iloc[0:0].copy())
    if new.empty:
        return result

    columns = list(new.columns) + ([] if "provider" in new.columns else ["provider"])
    rows = sorted(_records(new), key=lambda r: _day(r["ts"]))
    by_day: dict[pd.Timestamp, dict[str, Any]] = {}
    for r in rows:
        r["ts"] = cast(pd.Timestamp, pd.Timestamp(r["ts"]))
        r.setdefault("provider", pinned_provider or "unknown")
        by_day[_day(r["ts"])] = r  # a duplicate date keeps the last row
    fresh: list[dict[str, Any]] = []
    for r in by_day.values():
        if check_calendar and not is_session(symbol, r["ts"]):
            result.quarantined.append(_quarantined(r, "non_session", stored=False))
            continue
        fresh.append(r)

    # One sequence: the stored tail before the first new bar, then the new bars.
    seq: list[dict[str, Any]] = []
    if stored is not None and not stored.empty and fresh:
        first = _day(fresh[0]["ts"])
        prior = sorted(
            (r for r in _records(stored) if _day(r["ts"]) < first), key=lambda r: _day(r["ts"]),
        )
        if len(prior) >= 2:
            seq.append({**prior[-2], "_new": False, "_movable": False})
        if prior:
            seq.append({**prior[-1], "_new": False, "_movable": True})
    seq.extend({**r, "_new": True, "_movable": True} for r in fresh)

    drop: set[int] = set()
    split_from: int | None = None
    prev_idx: int | None = None
    for i, row in enumerate(seq):
        if prev_idx is None:
            prev_idx = i
            continue
        c_prev = float(seq[prev_idx]["close"])
        c_t = float(row["close"])
        if c_prev <= 0 or c_t <= 0:
            prev_idx = i
            continue
        r0 = c_t / c_prev - 1.0
        r1 = None
        if i + 1 < len(seq) and float(seq[i + 1]["close"]) > 0:
            r1 = float(seq[i + 1]["close"]) / c_t - 1.0
        if abs(r0) > limit and _is_reversal(r0, r1):
            if row["_movable"]:
                drop.add(i)
                result.quarantined.append(_quarantined(row, "spike_reversal", stored=not row["_new"]))
                continue  # the next bar is measured against the bar before the spike
        elif abs(r0) > SPLIT_MOVE and _split_like(c_t / c_prev):
            result.split_suspected = True
            result.split_days.append((_day(row["ts"]), c_t / c_prev))
            if not allow_split and row["_new"]:
                split_from = i
                break
        elif (
            abs(r0) > limit
            and pinned_provider
            and row["_new"]
            and str(row.get("provider") or "") != pinned_provider
        ):
            drop.add(i)
            result.quarantined.append(_quarantined(row, "provider_scale", stored=False))
            continue
        prev_idx = i

    if split_from is not None:
        for j in range(split_from, len(seq)):
            if seq[j]["_new"]:
                drop.add(j)
                result.quarantined.append(_quarantined(seq[j], "split_signature", stored=False))

    accepted_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for i, r in enumerate(seq) if r["_new"] and i not in drop
    ]
    result.accepted = pd.DataFrame(accepted_rows, columns=pd.Index(columns))
    return result


def confirms(quarantined_close: float, other_close: float | None) -> bool:
    """True when a second provider's close for the same date is within 1 %."""
    if other_close is None or other_close <= 0 or quarantined_close <= 0:
        return False
    return abs(quarantined_close / other_close - 1.0) <= CONFIRM_TOLERANCE


def guard_class_for(db: Any, symbol: str) -> str:
    """Asset class used for the move limit; network-free, fails open to ``single_stock``."""
    sym = symbol.upper()
    if sym in _GOLD_TICKERS:
        return "gold"
    name = ""
    asset_type = ""
    try:
        from app.foundation.models.entities import Asset

        asset = db.query(Asset).filter(Asset.symbol == symbol).first()
        if asset is not None:
            name = (asset.name or "").lower()
            asset_type = (asset.asset_type or "").lower()
    except Exception:  # noqa: BLE001
        logger.debug("bar_guard: Asset lookup failed for %s", symbol, exc_info=True)
    if not asset_type and sym in _KNOWN_ETFS:
        asset_type = "etf"
    if any(k in name for k in _GOLD_KEYWORDS):
        return "gold"
    if asset_type in ("money_market", "bond"):
        return "bond_mm_etf"
    if name:
        from app.foundation.etf_universe import is_bond_fund, is_money_market_fund

        if is_money_market_fund(name) or is_bond_fund(name):
            return "bond_mm_etf"
    if asset_type in ("etf", "fund"):
        return "equity_etf"
    return "single_stock"
