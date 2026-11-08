"""The Discover BUY gate's evidence from selection_w (ADR 0018, amendment of 2026-11-08, §4)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover.candidate_gate import evaluate_track_record
from app.decision.verification.daily_tests import (
    BUY_GATE_SETTING_KEY,
    BUY_MIN_DAYS,
    PRIMARY_TEST_START,
    V2_TEST_START,
    buy_gate_evidence,
    store_buy_gate_evidence,
)
from app.foundation.core.db import Base
from app.foundation.models.entities import TrustDailyActiveReturn, User
from app.foundation.settings import get_setting_row


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db, name="gate") -> str:
    user = User(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user.id


def _seed(db, uid, series, values, first):
    for day, value in zip(pd.bdate_range(first, periods=len(values)), values, strict=True):
        db.add(TrustDailyActiveReturn(
            user_id=uid, series=series, day=day.date(), n_open=15, n_stale=0, value=float(value),
            benchmark="EUNL.DE", detail_json={},
        ))
    db.commit()


def _strong(n, seed=8):
    """About 4 % per 21 days over the controls with 2 % noise: proven well within a year."""
    return np.random.default_rng(seed).normal(0.04 / 21, 0.02 / np.sqrt(21), n)


def test_no_days_is_insufficient_data():
    db = _memory_db()
    uid = _user(db)
    ev = buy_gate_evidence(db, uid)
    assert ev["status"] == "insufficient_data" and ev["start"] == V2_TEST_START.isoformat()


def test_a_strong_selection_edge_is_proven_but_not_before_the_first_noise_look():
    db = _memory_db()
    uid = _user(db)
    x = _strong(200)
    _seed(db, uid, "selection_w", x[:BUY_MIN_DAYS - 1], V2_TEST_START)
    early = buy_gate_evidence(db, uid)
    _seed(db, uid, "selection_w", x[BUY_MIN_DAYS - 1:], pd.bdate_range(V2_TEST_START, periods=BUY_MIN_DAYS)[-1])
    late = buy_gate_evidence(db, uid)
    assert early["n_days"] == BUY_MIN_DAYS - 1 and early["status"] == "unproven"
    assert late["status"] == "proven" and late["e_skill"] >= 100 and not any(late["guards"].values())


def test_days_before_the_new_start_never_count():
    db = _memory_db()
    uid = _user(db)
    _seed(db, uid, "selection_w", _strong(200), PRIMARY_TEST_START - pd.Timedelta(days=300))
    assert buy_gate_evidence(db, uid)["status"] == "insufficient_data"


def test_harm_against_msci_world_blocks_a_proven_selection_record():
    db = _memory_db()
    uid = _user(db)
    _seed(db, uid, "selection_w", _strong(200), V2_TEST_START)
    _seed(db, uid, "ideas", np.random.default_rng(2).normal(-0.006, 0.004, 300), PRIMARY_TEST_START)
    ev = buy_gate_evidence(db, uid)
    assert ev["guards"]["f1_harm"] and ev["status"] == "unproven"


def test_other_users_rows_do_not_count():
    db = _memory_db()
    uid, other = _user(db), _user(db, "other")
    _seed(db, other, "selection_w", _strong(200), V2_TEST_START)
    assert buy_gate_evidence(db, uid)["status"] == "insufficient_data"


def test_the_weekly_job_stores_it_and_discover_reads_it():
    db = _memory_db()
    uid = _user(db)
    _seed(db, uid, "selection_w", _strong(200), V2_TEST_START)
    assert store_buy_gate_evidence(db, [uid]) == {uid: "proven"}
    db.commit()
    assert get_setting_row(db, BUY_GATE_SETTING_KEY)[uid]["status"] == "proven"
    gate = evaluate_track_record(db, "equity", user_id=uid)
    assert gate["status"] == "proven" and gate["method"] == "selection_w_e_process"
