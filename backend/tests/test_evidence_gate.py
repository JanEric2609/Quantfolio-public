"""DSR and PBO over the pre-registered trials (report Phase 3, step 4)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.factor_evidence import latest_evidence_gate
from app.foundation.models.entities import TrialLedgerEntry
from app.foundation.quant_metrics import DEFAULT_N_TRIALS_FLOOR, record_trial
from app.lab import pooled_model, satellite
from app.lab.evidence_gate import GATING_FAMILY, grade_family, pbo_or_none, run_evidence_gate
from app.lab.evidence_gate.gate import satellite_wide


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


MONTHS = pd.period_range("1990-01", periods=420, freq="M").astype(str)
MODELS = ("ridge", "gbm", "value_momentum")
BROKERS = ("dkb", "scalable")


def _noise(seed: int, mean: float = 0.0, sd: float = 0.04) -> np.ndarray:
    return np.random.default_rng(seed).normal(mean, sd, len(MONTHS))


def _write(panel: Path, *, pooled_edge: float = 0.0, satellite_edge: float = 0.0) -> Path:
    """Stored results as steps 2 and 3 write them; ridge carries the edges."""
    derived = panel / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    pooled = [
        {"model": m, "month": month, "long_short": v + (pooled_edge if m == "ridge" else 0.0)}
        for i, m in enumerate(MODELS) for month, v in zip(MONTHS, _noise(i), strict=True)
    ]
    pooled_model.results_path(panel, "world").write_text(json.dumps({"series": pooled}))
    series, cards = [], []
    for j, b in enumerate(BROKERS):
        for i, m in enumerate(MODELS):
            core = _noise(100, 0.006)
            net = core + _noise(10 * j + i + 1) + (satellite_edge if m == "ridge" else 0.0)
            series += [
                {"broker": b, "model": m, "month": month, "gross": n + 0.002, "net": n, "after_tax": n - 0.002,
                 "core": c}
                for month, n, c in zip(MONTHS, net, core, strict=True)
            ]
            cards.append({"broker": b, "model": m, "core_tax_annual": 0.012})
    satellite.results_path(panel, "world").write_text(json.dumps({"series": series, "cards": cards}))
    return panel


def test_no_results_means_no_gate(tmp_path):
    assert run_evidence_gate(_memory_db(), panel_dir=tmp_path, persist=False) is None


def test_noise_stays_locked(tmp_path):
    result = run_evidence_gate(_memory_db(), panel_dir=_write(tmp_path), persist=False)

    assert result is not None and not result.satellite_unlocked
    assert "Deflated Sharpe" in result.reason
    assert result.n_trials == DEFAULT_N_TRIALS_FLOOR  # empty ledger: 1
    families = {f.name: f for f in result.families}
    assert set(families) == {"pooled_long_short", GATING_FAMILY, "satellite_tax_free"}
    gating = families[GATING_FAMILY]
    assert gating.gates_money and not families["satellite_tax_free"].gates_money
    assert {c.name for c in gating.candidates if c.mined} == {"dkb:ridge", "dkb:gbm", "scalable:ridge", "scalable:gbm"}
    assert all(not c.passes_dsr for c in gating.candidates)
    assert gating.pbo is not None and gating.common_months == len(MONTHS)


def test_a_real_after_tax_edge_unlocks_and_is_stored(tmp_path):
    panel = _write(tmp_path, pooled_edge=0.02, satellite_edge=0.02)
    db = _memory_db()
    result = run_evidence_gate(db, panel_dir=panel)

    assert result is not None and result.satellite_unlocked
    gating = next(f for f in result.families if f.name == GATING_FAMILY)
    assert {c.name for c in gating.candidates if c.passes_dsr} == {"dkb:ridge", "scalable:ridge"}
    assert sorted(result.unlocked_by) == ["dkb:ridge", "scalable:ridge"]
    assert gating.pbo is not None and gating.pbo <= 0.1
    assert result.artifact is not None
    saved = json.loads(result.artifact.read_text())
    assert saved["satellite_unlocked"] is True and len(saved["families"]) == 3
    # The verdict the monthly plan reads.
    stored = latest_evidence_gate(db, "world")
    assert stored is not None and stored["satellite_unlocked"] is True
    assert sorted(stored["unlocked_by"]) == ["dkb:ridge", "scalable:ridge"]
    assert stored["n_trials"] == result.n_trials


def test_a_dry_run_stores_nothing(tmp_path):
    db = _memory_db()
    assert run_evidence_gate(db, panel_dir=_write(tmp_path), persist=False) is not None
    assert latest_evidence_gate(db, "world") is None


def test_the_baseline_passing_does_not_unlock(tmp_path):
    panel = _write(tmp_path)
    path = satellite.results_path(panel, "world")
    stored = json.loads(path.read_text())
    for row in stored["series"]:
        if row["model"] == "value_momentum":
            row["after_tax"] += 0.02
    path.write_text(json.dumps(stored))

    result = run_evidence_gate(_memory_db(), panel_dir=panel, persist=False)

    assert result is not None and not result.satellite_unlocked
    gating = next(f for f in result.families if f.name == GATING_FAMILY)
    assert any(c.passes_dsr and not c.mined for c in gating.candidates)


def test_more_trials_on_the_ledger_raise_the_bar():
    edge = pd.DataFrame({"a": _noise(1, 0.012), "b": _noise(2)}, index=MONTHS)
    few = grade_family("x", edge, {"a"}, n_trials=10).candidates[0].dsr
    many = grade_family("x", edge, {"a"}, n_trials=100_000).candidates[0].dsr
    assert many < few


def test_the_gate_reads_the_ledger_count(tmp_path):
    db = _memory_db()
    for i in range(DEFAULT_N_TRIALS_FLOOR + 5):
        record_trial(db, "test", str(i))
    db.commit()
    assert db.query(TrialLedgerEntry).count() == DEFAULT_N_TRIALS_FLOOR + 5
    result = run_evidence_gate(db, panel_dir=_write(tmp_path), persist=False)
    assert result is not None and result.n_trials == DEFAULT_N_TRIALS_FLOOR + 5


def test_pbo_is_not_measured_on_a_short_or_single_record():
    short = pd.DataFrame({"a": _noise(1)[:20], "b": _noise(2)[:20]})
    assert pbo_or_none(short) == (None, 20)
    assert pbo_or_none(pd.DataFrame({"a": _noise(1)}))[0] is None
    family = grade_family("x", short, {"a"}, n_trials=500)
    assert family.pbo is None and not family.passes_pbo


def test_after_tax_excess_adds_back_the_cores_deferred_tax(tmp_path):
    stored = json.loads(satellite.results_path(_write(tmp_path), "world").read_text())
    taxed = satellite_wide(stored, after_tax=True)["dkb:ridge"]
    free = satellite_wide(stored, after_tax=False)["dkb:ridge"]
    assert (free - taxed).to_numpy() == pytest.approx(0.002 - 0.012 / 12)
