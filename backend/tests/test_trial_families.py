"""Trials counted per family (plan 2026-11-10 WS2, ADR 0025).

A gate's N is the trials among which its candidate was selected: the families
on its lineage. The global total stays as a display number.
"""
from __future__ import annotations

import numpy as np

from conftest import _memory_db

from app.decision.advisor.strategy import ensure_champion, spawn_challenger
from app.foundation import quant_metrics as qm
from app.foundation.models.entities import AcTrialLedger, FactorsLibrary, TrialLedgerEntry, User
from app.lab.alphacrafter import miner


def _user(db) -> User:
    user = User(id="u1", username="jan", password_hash="x")
    db.add(user)
    db.commit()
    return user


def test_family_filter_counts_only_the_named_families():
    db = _memory_db()
    for i in range(3):
        qm.record_trial(db, "satellite", f"s{i}")
    for i in range(7):
        qm.record_trial(db, "advisor_challenger", f"a{i}")
    db.commit()
    assert qm.resolve_n_trials(db, ["satellite"]) == 3
    assert qm.resolve_n_trials(db, ["advisor_challenger"]) == 7
    assert qm.resolve_n_trials(db, ["satellite", "advisor_challenger"]) == 10
    assert qm.resolve_n_trials(db, ["nothing_here"]) == 1  # the floor
    assert qm.resolve_n_trials(db) == 10


def test_same_config_hash_in_one_family_is_one_trial():
    db = _memory_db()
    for day in range(5):  # a daily re-test of one configuration
        qm.record_trial(db, "alphacrafter_trader", f"run{day}", config_hash="cfgA")
    qm.record_trial(db, "alphacrafter_trader", "other", config_hash="cfgB")
    db.commit()
    assert qm.resolve_n_trials(db, ["alphacrafter_trader"]) == 2


def test_discover_legacy_counts_distinct_hashes_not_rows():
    db = _memory_db()
    for run, cfg in (("r1", "h1"), ("r2", "h1"), ("r3", "h2")):
        for sym in ("AAA", "BBB", "CCC"):
            db.add(AcTrialLedger(run_id=run, factor_name="mom", symbol=sym, ic=0.0, icir=0.0,
                                 n_obs=10, horizon_days=21, config_hash=cfg))
    db.commit()
    assert qm.resolve_n_trials(db, [qm.DISCOVER_LEGACY_FAMILY]) == 2
    assert qm.n_trials_by_family(db) == {qm.DISCOVER_LEGACY_FAMILY: 2}
    # No gate's families include it.
    assert qm.resolve_n_trials(db, qm.LAB_FAMILIES) == 1


def test_evidence_gate_n_is_the_sum_over_lab_families():
    db = _memory_db()
    for family, n in (("pooled_model", 2), ("satellite", 4), ("tier_b", 6), ("discover_replay", 12)):
        for i in range(n):
            qm.record_trial(db, family, f"{family}{i}")
    qm.record_trial(db, "advisor_challenger", "x")
    db.commit()
    assert qm.effective_n_trials(db, qm.LAB_FAMILIES) == 2 + 4 + 6 + 12


def test_li_ji_counts_correlated_series_once_and_independent_ones_fully():
    rng = np.random.default_rng(0)
    base = rng.normal(size=500)
    same = np.column_stack([base, base, base])
    assert qm.li_ji_effective_number(same) == 1.0
    independent = rng.normal(size=(5000, 4))
    assert 3.5 <= qm.li_ji_effective_number(independent) <= 4.0


def test_series_path_uses_li_ji_and_falls_back_to_raw():
    db = _memory_db()
    rng = np.random.default_rng(1)
    base = rng.normal(size=300).tolist()
    for i in range(4):  # four identical IC series (rescaled): one test
        scaled = (np.array(base) * (i + 1)).tolist()
        qm.record_trial(db, "alphacrafter_miner", f"f{i}", {"series": scaled}, config_hash=f"f{i}")
    qm.record_trial(db, "alphacrafter_miner", "no_series", config_hash="ns")
    db.commit()
    assert qm.resolve_n_trials(db, ["alphacrafter_miner"]) == 5
    assert qm.effective_n_trials(db, ["alphacrafter_miner"]) == 2  # 1 (Li-Ji) + 1 raw


def _candidate(name: str, ic: list[float]) -> miner.FactorCandidate:
    return miner.FactorCandidate(
        name=name, formula=f"f({name})", source="test", ic=float(np.mean(ic)), icir=0.0,
        turnover=0.0, ic_series=ic,
    )


def test_miner_reevaluation_of_one_formula_is_not_a_second_trial():
    db = _memory_db()
    rng = np.random.default_rng(2)
    for _day in range(3):
        miner.admit_candidates(db, [_candidate("mom", rng.normal(0.0, 0.1, 200).tolist())], horizon=5)
    assert db.query(TrialLedgerEntry).filter_by(context="alphacrafter_miner").count() == 1
    # A different horizon is a different hypothesis.
    miner.admit_candidates(db, [_candidate("mom", rng.normal(0.0, 0.1, 200).tolist())], horizon=21)
    assert qm.resolve_n_trials(db, ["alphacrafter_miner"]) == 2


def test_admission_hysteresis():
    assert miner.admission_decision(0.96, currently_admitted=False)
    assert not miner.admission_decision(0.90, currently_admitted=False)
    assert miner.admission_decision(0.90, currently_admitted=True)
    assert not miner.admission_decision(0.79, currently_admitted=True)


def test_admitted_factor_survives_a_middling_retest_and_retires_below_the_floor(monkeypatch):
    db = _memory_db()
    rng = np.random.default_rng(3)
    strong = (0.05 + rng.normal(0.0, 0.1, 500)).tolist()  # ICIR ~0.5 over 500 days
    [cand] = miner.admit_candidates(db, [_candidate("good", strong)], horizon=5)
    assert cand.valid and cand.deflated_icir is not None and cand.deflated_icir >= miner.ADMIT_DEFLATED
    assert db.query(FactorsLibrary).filter(FactorsLibrary.retired_at.is_(None)).count() == 1

    monkeypatch.setattr(miner, "_deflated", lambda _s, _n: 0.9)  # between the bars
    [cand] = miner.admit_candidates(db, [_candidate("good", strong)], horizon=5)
    assert cand.valid
    assert db.query(FactorsLibrary).filter(FactorsLibrary.retired_at.is_(None)).count() == 1

    monkeypatch.setattr(miner, "_deflated", lambda _s, _n: 0.5)
    [cand] = miner.admit_candidates(db, [_candidate("good", strong)], horizon=5)
    assert not cand.valid
    assert db.query(FactorsLibrary).filter(FactorsLibrary.retired_at.is_(None)).count() == 0

    # Once retired, 0.9 is not enough to come back in.
    monkeypatch.setattr(miner, "_deflated", lambda _s, _n: 0.9)
    [cand] = miner.admit_candidates(db, [_candidate("good", strong)], horizon=5)
    assert not cand.valid


def test_noise_factor_is_not_admitted():
    db = _memory_db()
    rng = np.random.default_rng(4)
    noise = rng.normal(0.0, 0.1, 250).tolist()
    [cand] = miner.admit_candidates(db, [_candidate("noise", noise)], horizon=5)
    assert not cand.valid
    assert db.query(FactorsLibrary).count() == 0


def test_spawn_challenger_writes_exactly_one_trial():
    db = _memory_db()
    user = _user(db)
    ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=7)
    rows = db.query(TrialLedgerEntry).filter_by(context="advisor_challenger").all()
    assert [r.trial_key for r in rows] == [challenger.id]
    assert rows[0].config_hash == qm.config_digest(challenger.config_json)
    assert qm.resolve_n_trials(db, qm.ADVISOR_FAMILIES) == 2  # champion + challenger
