"""The market regime every surface shows: the jump model's latest stored state.

Until 2026-10 this module computed its own rule-based label from VIX, the
10Y-3M spread and EUNL.DE momentum, with a hard-coded confidence (``low_vol``
was always "60 %") and silent fallbacks (VIX 20.0, momentum 0.0) that turned
a yfinance outage into "low_vol 60 %" cached for a day. Three regime systems
disagreed on the same screen. Now there is one: the statistical jump model
(Shu, Yu and Mulvey 2024) classified daily by ``classifier.classify_and_store``
into ``bull`` / ``sideways`` / ``bear`` and stored in ``regime_snapshots``.

This reader returns that state, the day it began, and the model's own
features (VIX, credit spread, drawdown) as context. No confidence number is
shown: the jump model labels a state, and its in-sample label reliability is
not a forecast probability. With no jump snapshot from the last
``STALE_AFTER_DAYS`` days the label is ``unknown`` (neutral weights), never a
guess. HMM rows written as the classifier's fallback are ignored.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

MODEL_SOURCE = "jump"
# The classifier runs every morning; four days spans a long weekend.
STALE_AFTER_DAYS = 4


def _as_datetime(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts if isinstance(ts, datetime) else None


def _state_since(db: Any, label: str, latest_ts: datetime) -> str | None:
    """First day of the current unbroken run of ``label`` in the jump history."""
    from app.foundation.data_backbone.regime_store import RegimeStore

    try:
        hist = RegimeStore(db).get_history(start=latest_ts - timedelta(days=730))
    except Exception:  # noqa: BLE001 - history is a nicety
        return None
    if hist is None or hist.empty or "source" not in hist:
        return None
    rows = cast("pd.DataFrame", hist[hist["source"] == MODEL_SOURCE]).sort_values("ts")
    since: Any = None
    for ts, lab in zip(rows["ts"], rows["label"], strict=True):
        if lab != label:
            since = None
        elif since is None:
            since = ts
    dt = _as_datetime(since.to_pydatetime() if hasattr(since, "to_pydatetime") else since)
    return dt.date().isoformat() if dt else None


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "label": "unknown",
        "confidence": None,
        "model": MODEL_SOURCE,
        "available": False,
        "reason": reason,
        "vix": None,
        "credit_spread": None,
        "drawdown": None,
        "yield_spread": None,
        "momentum_3m": None,
        "state_since": None,
        "as_of": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def compute_regime_snapshot(db=None) -> dict[str, Any]:
    """The jump model's latest regime as a JSON-safe dict (see module docstring)."""
    if db is None:
        return _unknown("no database session")
    from app.foundation.data_backbone.regime_store import RegimeStore

    snap = RegimeStore(db).get_latest_snapshot(source=MODEL_SOURCE)
    if snap is None:
        return _unknown("The jump model has not classified the market yet.")
    ts = _as_datetime(snap.get("ts"))
    if ts is None or datetime.now(UTC) - ts > timedelta(days=STALE_AFTER_DAYS):
        out = _unknown(f"The last jump-model classification is from {ts.date() if ts else 'an unknown day'}.")
        out["stale"] = True
        return out
    payload = snap.get("payload") or {}
    features = payload.get("features") or {}
    label = str(snap.get("label") or "unknown").lower()
    as_of = payload.get("feature_ts") or payload.get("last_bar_ts")
    return {
        "label": label,
        # A state, not a probability: see the module docstring.
        "confidence": None,
        "model": MODEL_SOURCE,
        "available": True,
        "reason": None,
        "crisis": bool(payload.get("crisis")),
        "triggers": payload.get("triggers") or [],
        "vix": features.get("vix"),
        "credit_spread": features.get("credit_spread"),
        "drawdown": features.get("drawdown"),
        # Kept for RegimeContext readers; the jump model does not use them.
        "yield_spread": None,
        "momentum_3m": None,
        "stale_bars": bool(payload.get("stale_bars")),
        "state_since": _state_since(db, label, ts),
        "as_of": str(as_of)[:10] if as_of else None,
        "updated_at": ts.isoformat(),
    }


def get_or_refresh_regime(db) -> dict[str, Any]:
    """The current regime. Reads the stored classification; never fetches data."""
    try:
        return compute_regime_snapshot(db)
    except Exception as exc:  # noqa: BLE001 - a broken read is "unknown", not a crash
        logger.warning("regime snapshot read failed: %s", exc)
        return _unknown("The regime could not be read.")
