"""Unknown PIT signals stay unknown instead of scoring as neutral evidence.

Prod run 71d23ec4 (2026-09-28): the insider extract ends 2024-03-29, so all
133 matched US names read cluster 0 / flow 0; ``_safe_float`` turned NaN
into 0.0 on top. Both made the composite weight a signal that was never
observed, at ~5.5% of every US score.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.decision.discover import pipeline
from app.decision.discover.composite import derive_signals_from_scores


def test_nan_and_inf_are_missing():
    assert pipeline._safe_float(float("nan")) is None
    assert pipeline._safe_float(np.float64("nan")) is None
    assert pipeline._safe_float(float("inf")) is None
    assert pipeline._safe_float(pd.NA) is None
    assert pipeline._safe_float("x") is None
    assert pipeline._safe_float(np.float64(0.25)) == 0.25
    assert pipeline._safe_float(0) == 0.0


def _frame(**cols):
    return pd.DataFrame({**cols, "data_confidence": ["ticker_match"]})


def test_insider_window_outside_the_extract_is_dropped(monkeypatch):
    monkeypatch.setattr(
        pipeline, "pit_insider_signal_for_symbol",
        lambda db, symbol, dates: _frame(cluster_buy_score=[math.nan], net_insider_flow_usd=[math.nan]),
    )
    scores, reject = pipeline.stage_insider_signal(None, "MU")

    assert reject is None
    assert scores["cluster_buy_score"] is None
    assert "insider_data_unavailable" in scores["concerns"]
    assert "insider_signal" not in derive_signals_from_scores({"insider_signal": scores})


def test_observed_zero_insider_activity_still_counts(monkeypatch):
    monkeypatch.setattr(
        pipeline, "pit_insider_signal_for_symbol",
        lambda db, symbol, dates: _frame(cluster_buy_score=[0.0], net_insider_flow_usd=[0.0]),
    )
    scores, _ = pipeline.stage_insider_signal(None, "MU")

    assert scores["cluster_buy_score"] == 0.0
    assert derive_signals_from_scores({"insider_signal": scores})["insider_signal"] == 0.5


def test_ibes_without_a_current_revision_is_dropped(monkeypatch):
    monkeypatch.setattr(
        pipeline, "pit_ibes_estimate_signal_for_symbol",
        lambda db, symbol, dates: _frame(sue=[pd.NA], revision_momentum=[math.nan], dispersion=[0.04]),
    )
    scores, _ = pipeline.stage_estimate_revision_signal(None, "LRCX")

    assert scores["revision_momentum"] is None
    assert "estimate_data_unavailable" in scores["concerns"]
    assert "estimate_revision" not in derive_signals_from_scores({"estimate_revision_signal": scores})
