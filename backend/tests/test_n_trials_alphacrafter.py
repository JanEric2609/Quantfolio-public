"""n_trials counts AlphaCrafter's search (plan 2026-10-12, D10 / WP9)."""
from __future__ import annotations

from conftest import _memory_db

from app.foundation.models.entities import AcTrialLedger, TrialLedgerEntry
from app.foundation.quant_metrics import effective_n_trials, resolve_n_trials


def _seed(db):
    for i in range(3):
        db.add(TrialLedgerEntry(context="quant_lab", trial_key=f"t{i}", metadata_json={"family": "grid1"}))
    for run in ("run-a", "run-b"):
        for i in range(4):
            db.add(AcTrialLedger(run_id=run, factor_name=f"f{i}", symbol="AAPL", ic=0.01, icir=0.1,
                                 n_obs=100, horizon_days=21, config_hash="h"))
    db.commit()


def test_rows_in_both_ledgers_are_summed():
    db = _memory_db()
    _seed(db)
    assert resolve_n_trials(db) == 3 + 8


def test_each_alphacrafter_run_is_one_family():
    db = _memory_db()
    _seed(db)
    assert effective_n_trials(db) == 1 + 2


def test_empty_ledgers_keep_the_floor():
    assert resolve_n_trials(_memory_db()) == 1
