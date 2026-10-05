"""M3a — ac_trial_ledger table + idempotent trial-ledger service.

Audit F13 (docs/archive/audits/2026-08-discover-maths): the discover alpha-miner
stage aggregated ``abs(ic)``, masking sign-flipping and anti-predictive
factors. The trial ledger persists SIGNED IC/ICIR evaluations so downstream
consumers (composite.py already treats ic as signed) see the true signal.
"""

from datetime import UTC, datetime

from conftest import _memory_db

from app.foundation.models.entities import AcTrialLedger
from app.decision.discover.trial_ledger import (
    append_trial,
    canonical_config_hash,
    family_size,
    list_trials,
)

_RUN = "run-1"
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _append(db, **overrides):
    kwargs = {
        "run_id": _RUN,
        "factor_name": "momentum_12_1",
        "symbol": "AAPL",
        "ic": 0.03,
        "icir": 0.4,
        "n_obs": 120,
        "horizon_days": 5,
        "config_hash": _HASH_A,
    }
    kwargs.update(overrides)
    return append_trial(db, **kwargs)


def test_append_idempotent_same_key():
    db = _memory_db()

    first = _append(db)
    second = _append(db)

    assert first is True
    assert second is False
    rows = list_trials(db, run_id=_RUN)
    assert len(rows) == 1


def test_variant_config_hash_two_rows():
    db = _memory_db()

    assert _append(db, config_hash=_HASH_A) is True
    assert _append(db, config_hash=_HASH_B) is True

    rows = list_trials(db, run_id=_RUN)
    assert len(rows) == 2
    assert {r.config_hash for r in rows} == {_HASH_A, _HASH_B}


def test_different_run_id_two_rows():
    db = _memory_db()

    assert _append(db, run_id="run-1") is True
    assert _append(db, run_id="run-2") is True

    # History is preserved ACROSS runs; idempotency applies WITHIN a run.
    assert len(list_trials(db, run_id="run-1")) == 1
    assert len(list_trials(db, run_id="run-2")) == 1


def test_survives_session_recreation():
    from sqlalchemy.orm import sessionmaker

    db = _memory_db()
    assert _append(db) is True
    # Detach everything so the identity map cannot mask a missing row.
    db.expunge_all()
    db.commit()
    db.close()

    # New session from the SAME engine (StaticPool keeps the :memory: db alive).
    fresh = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)()
    try:
        rows = list_trials(fresh, run_id=_RUN)
        assert len(rows) == 1
        assert rows[0].factor_name == "momentum_12_1"
    finally:
        fresh.close()


def test_family_size_counts_distinct_configs():
    db = _memory_db()

    assert _append(db, config_hash=_HASH_A) is True
    assert _append(db, config_hash=_HASH_B) is True
    assert _append(db, config_hash=_HASH_B) is False  # duplicate ignored
    assert _append(
        db,
        factor_name="rsi_14",
        config_hash=_HASH_A,
    ) is True

    assert family_size(db) == 2
    assert family_size(db, factor_names=["momentum_12_1"]) == 2
    assert family_size(db, factor_names=["rsi_14"]) == 1
    assert family_size(db, factor_names=["never_seen"]) == 0


def test_signed_ic_stored_verbatim():
    db = _memory_db()

    assert _append(db, ic=-0.08, icir=-0.9) is True

    row = list_trials(db, run_id=_RUN)[0]
    assert row.ic == -0.08  # NOT abs() — sign is the whole point (F13)
    assert row.icir == -0.9


def test_canonical_config_hash_stable_and_sensitive():
    def_a = {"name": "momentum_12_1", "formula": "close.pct_change(252)", "source": "quant_factors"}
    def_b = {"source": "quant_factors", "name": "momentum_12_1", "formula": "close.pct_change(252)"}
    def_c = dict(def_a, params={"skip": 1})

    h1 = canonical_config_hash(def_a, 5)
    h2 = canonical_config_hash(def_b, 5)
    h3 = canonical_config_hash(def_a, 10)
    h4 = canonical_config_hash(def_c, 5)

    assert h1 == h2  # key order irrelevant
    assert len(h1) == 64
    assert h1 != h3  # horizon participates
    assert h1 != h4  # params participate when present
