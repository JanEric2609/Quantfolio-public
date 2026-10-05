"""Discover's ic_icir signal: the candidate's exposure to validated factors.

stage_alpha_miner used to measure each factor's IC across the candidate and
three benchmark ETFs -- a four-point rank correlation, and a property of the
factor rather than of the candidate. Since 2026-09-28 it scores the
candidate's cross-sectional z-score on every validated FactorsLibrary factor
within the miner universe and combines them Grinold-Kahn style:
``Phi(sum_f w_f sign(IC_f) z_f / sqrt(sum_f w_f^2))`` with ``w_f ~ |ICIR_f|``.
"""

import json
import math

import numpy as np
import pandas as pd
import pytest
from conftest import _memory_db

from app.decision.discover import pipeline
from app.decision.discover.composite import ic_signal_usable
from app.foundation.models.entities import FactorsLibrary
from app.lab.alphacrafter import miner as real_miner

UNIVERSE = [f"U{i:02d}" for i in range(40)]


def _panel(symbols: list[str], *, days: int = 60, slopes: dict[str, float] | None = None) -> dict[str, pd.DataFrame]:
    """Linear price paths; each symbol's 5-day change is its slope * 5."""
    idx = pd.bdate_range("2026-06-01", periods=days)
    slopes = slopes or {}
    close = pd.DataFrame(
        {s: 100.0 + np.arange(days) * slopes.get(s, 0.01 * (i + 1)) for i, s in enumerate(symbols)},
        index=idx,
    )
    return {"close": close}


def _add_factor(db, name: str, *, dsl: str | None, ic: float, icir: float, retired: bool = False) -> None:
    db.add(
        FactorsLibrary(
            name=name,
            formula_json=json.dumps({"formula": dsl or name, "dsl": dsl, "name": name}),
            source="test",
            ic_summary_json=json.dumps({"ic": ic, "icir": icir, "n_obs": 100}),
            retired_at=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime() if retired else None,
        )
    )
    db.commit()


@pytest.fixture
def stage(monkeypatch):
    """Real miner.factor_values on a synthetic universe panel; the candidate's
    own panel is whatever the test registers in ``extra``."""
    extra: dict[str, dict[str, pd.DataFrame]] = {}
    calls: list[list[str]] = []

    def build_panel(db, universe, start, end, include_fundamentals=False):
        calls.append(list(universe))
        if len(universe) == 1 and universe[0] in extra:
            return extra[universe[0]]
        if len(universe) == 1:
            return {}
        return _panel(UNIVERSE)

    monkeypatch.setattr(pipeline, "_ALPHACRAFTER_AVAILABLE", True)
    monkeypatch.setattr(pipeline, "alpha_miner_module", real_miner)
    monkeypatch.setattr(pipeline, "alpha_build_panel", build_panel)
    monkeypatch.setattr("app.lab.alphacrafter.universe.MINER_UNIVERSE", UNIVERSE)
    pipeline._EXPOSURE_CACHE.clear()
    yield {"extra": extra, "calls": calls}
    pipeline._EXPOSURE_CACHE.clear()


def _z(symbol: str, values: dict[str, float]) -> float:
    s = pd.Series(values)
    return (s[symbol] - s.mean()) / s.std()


def test_no_validated_factors_is_neutral_and_not_weighted(stage):
    db = _memory_db()
    scores, reject = pipeline.stage_alpha_miner(db, "U39")
    assert reject is None
    assert scores["exposure_score"] is None
    assert scores["concerns"] == ["alphacrafter_no_validated_factors"]
    assert not ic_signal_usable(scores)
    assert stage["calls"] == []  # no panel is built for nothing to score


def test_obsidian_hypotheses_and_retired_factors_are_not_validated(stage):
    db = _memory_db()
    db.add(FactorsLibrary(name="hyp", formula_json="a prose idea", source="obsidian_sync",
                          ic_summary_json=json.dumps({"regime": "bull", "confidence": 0.7})))
    db.commit()
    _add_factor(db, "old", dsl="delta(close, 5)", ic=0.05, icir=0.8, retired=True)
    assert pipeline._library_factors(db) == []


def test_top_of_a_predictive_factor_scores_high(stage):
    """U39 has the steepest slope, so the largest 5-day delta in the
    cross-section; a positive-IC factor on it lifts the score above 0.5."""
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)

    scores, _ = pipeline.stage_alpha_miner(db, "U39")

    deltas = {s: 0.01 * (i + 1) * 5 for i, s in enumerate(UNIVERSE)}
    z = min(3.0, _z("U39", deltas))
    assert scores["n_factors"] == 1
    assert scores["panel_size"] == 40
    assert scores["factors"][0]["z"] == pytest.approx(z, abs=1e-4)
    assert scores["exposure_score"] == pytest.approx(0.5 * (1 + math.erf(z / math.sqrt(2))), abs=1e-4)
    assert scores["exposure_score"] > 0.9
    assert ic_signal_usable(scores)


def test_negative_ic_flips_the_exposure(stage):
    """An anti-predictive factor (IC < 0): loading heavily on it is bad news."""
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=-0.04, icir=-0.6)
    scores, _ = pipeline.stage_alpha_miner(db, "U39")
    assert scores["exposure_score"] < 0.1


def test_factors_combine_weighted_by_icir(stage):
    """Two factors with opposite exposure: the one with the larger |ICIR|
    decides, and the combination is normalised by sqrt(sum w^2)."""
    db = _memory_db()
    _add_factor(db, "up", dsl="delta(close, 5)", ic=0.03, icir=0.9)
    _add_factor(db, "down", dsl="-1 * delta(close, 5)", ic=0.03, icir=0.3)

    scores, _ = pipeline.stage_alpha_miner(db, "U39")

    z = {f["name"]: f["z"] for f in scores["factors"]}
    assert z["up"] == pytest.approx(-z["down"], abs=1e-4)
    w_up, w_down = 0.75, 0.25
    s = w_up * z["up"] + w_down * z["down"]
    expected = 0.5 * (1 + math.erf(s / math.sqrt(w_up**2 + w_down**2) / math.sqrt(2)))
    assert scores["exposure_score"] == pytest.approx(expected, abs=1e-3)
    assert scores["icir"] == pytest.approx(w_up * 0.9 + w_down * 0.3, abs=1e-4)


def test_z_scores_are_clipped(stage):
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)
    stage["extra"]["OUTLIER"] = _panel(["OUTLIER"], slopes={"OUTLIER": 50.0})
    scores, _ = pipeline.stage_alpha_miner(db, "OUTLIER")
    assert scores["factors"][0]["z"] == pytest.approx(3.0)
    assert scores["panel_size"] == 41


def test_a_candidate_outside_the_universe_joins_the_cross_section(stage):
    """A seed factor (by name) computes the candidate's column on its own
    panel; the universe's values are computed once per day, not per candidate."""
    db = _memory_db()
    _add_factor(db, "volatility_21d", dsl=None, ic=-0.03, icir=-0.5)
    stage["extra"]["NEW"] = _panel(["NEW"], slopes={"NEW": 0.2})

    first, _ = pipeline.stage_alpha_miner(db, "NEW")
    second, _ = pipeline.stage_alpha_miner(db, "U05")

    assert first["n_factors"] == 1 and first["panel_size"] == 41
    assert second["n_factors"] == 1 and second["panel_size"] == 40
    universe_builds = [c for c in stage["calls"] if len(c) > 1]
    assert len(universe_builds) == 1
    assert len(pipeline._EXPOSURE_CACHE["values"]) == 1


def test_a_candidate_without_bars_is_insufficient(stage):
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)
    scores, _ = pipeline.stage_alpha_miner(db, "NOBARS")
    assert scores["exposure_score"] is None
    assert scores["concerns"] == ["alphacrafter_miner_insufficient_panel"]
    assert not ic_signal_usable(scores)


def test_a_stale_candidate_is_not_scored(stage):
    """A suspended name whose last bar is weeks before the universe's."""
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)
    stale = _panel(["STALE"], days=30)
    stage["extra"]["STALE"] = stale
    scores, _ = pipeline.stage_alpha_miner(db, "STALE")
    assert scores["exposure_score"] is None
    assert scores["concerns"] == ["alphacrafter_miner_insufficient_panel"]


def test_a_small_universe_is_not_a_cross_section(stage, monkeypatch):
    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)
    monkeypatch.setattr(
        pipeline, "alpha_build_panel",
        lambda db, universe, start, end, include_fundamentals=False: _panel(UNIVERSE[:10]),
    )
    scores, _ = pipeline.stage_alpha_miner(db, "U05")
    assert scores["exposure_score"] is None
    assert not ic_signal_usable(scores)


def test_the_stage_writes_no_trial_ledger_rows(stage):
    """Scoring validated factors is not a new trial (ADR 0015 ledger)."""
    from app.decision.discover.trial_ledger import list_trials

    db = _memory_db()
    _add_factor(db, "delta5", dsl="delta(close, 5)", ic=0.04, icir=0.6)
    pipeline.stage_alpha_miner(db, "U39", run_id="run-1")
    assert list_trials(db) == []
