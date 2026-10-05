"""Tests for the factor-premia study (report Phase 3): portfolios, evidence, plan wiring."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import monthly_plan
from app.decision.monthly_plan import build_monthly_plan, evidence_state
from app.foundation.core.db import Base
from app.foundation.data_engineering import _pit_duckdb
from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    FactorEvidenceCard,
    TrialLedgerEntry,
    User,
)
from app.foundation.settings import upsert_public_settings
from app.lab.factor_premia import STRATEGY_BY_KEY, evaluate, factor_series, latest_cards, run_study
from app.lab.factor_premia import strategies as S


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_MEMORY_LIMIT", "256MB")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_THREADS", "1")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_TMPDIR", str(tmp_path / "duckdb"))
    _pit_duckdb.reset_connection()
    yield
    _pit_duckdb.reset_connection()


def _row(gvkey, eom, country, *, be_me=1.0, mom=0.0, r=0.0, me=100.0, size="large", ingested="2026-09-01"):
    return {
        "gvkey": gvkey, "permno": None, "eom": pd.Timestamp(eom), "excntry": country, "size_grp": size,
        "me": me, "mom_12_1": mom, "be_me": be_me, "gp_at": 0.1, "at_gr1": 0.05,
        "ret_exc_lead1m": r, "source": "wrds_factor_characteristics", "ingested_at": pd.Timestamp(ingested),
    }


def _write_panel(root: Path, rows: list[dict], name: str = "x.0000") -> Path:
    out = root / "wrds_factor_characteristics_pit"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out / f"{name}.parquet", index=False)
    return root


# --- portfolios -----------------------------------------------------------------


def test_terciles_are_value_weighted_within_country(tmp_path):
    # 15 equal-cap stocks: value rank i earns i %. Top third = ranks 11-15
    # (percentile >= 2/3), bottom third = ranks 1-5.
    rows = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), r=i / 100) for i in range(15)]
    panel = _write_panel(tmp_path, rows)

    series = factor_series(panel, ("DEU",))
    value = series[series["strategy"] == "value"].iloc[0]

    assert str(value["month"]) == "2020-02"  # the return month, not eom
    assert value["top"] == pytest.approx(0.12)
    assert value["bottom"] == pytest.approx(0.02)
    assert value["market"] == pytest.approx(0.07)
    assert value["long_short"] == pytest.approx(0.10)
    assert value["long_only"] == pytest.approx(0.05)
    assert value["n_stocks"] == 15


def test_universe_excludes_other_regions_micro_caps_and_thin_countries(tmp_path):
    rows = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), r=i / 100) for i in range(15)]
    rows += [_row(f"M{i}", "2020-01-31", "DEU", be_me=99.0, r=5.0, size="micro") for i in range(5)]
    rows += [_row(f"U{i}", "2020-01-31", "USA", be_me=float(i), r=1.0) for i in range(30)]
    rows += [_row(f"F{i}", "2020-01-31", "FRA", be_me=float(i), r=1.0) for i in range(10)]  # < 15 names
    panel = _write_panel(tmp_path, rows)

    value = factor_series(panel, S.EUROPE_DEVELOPED)
    value = value[value["strategy"] == "value"].iloc[0]

    assert value["top"] == pytest.approx(0.12)
    assert value["n_stocks"] == 15


def test_countries_are_combined_by_market_cap(tmp_path):
    rows = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), r=0.01 * (i >= 10), me=300.0) for i in range(15)]
    rows += [_row(f"N{i}", "2020-01-31", "NLD", be_me=float(i), r=0.0, me=100.0) for i in range(15)]
    panel = _write_panel(tmp_path, rows)

    value = factor_series(panel, S.EUROPE_DEVELOPED)
    value = value[value["strategy"] == "value"].iloc[0]

    # DEU top earns 1 %, NLD top 0 %; DEU is 3/4 of the cap.
    assert value["top"] == pytest.approx(0.0075)


def test_latest_extract_wins(tmp_path):
    old = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), r=0.0, ingested="2026-01-01") for i in range(15)]
    new = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), r=i / 100, ingested="2026-09-01") for i in range(15)]
    _write_panel(tmp_path, old, "a.0000")
    panel = _write_panel(tmp_path, new, "b.0000")

    value = factor_series(panel, ("DEU",))
    assert value[value["strategy"] == "value"].iloc[0]["top"] == pytest.approx(0.12)


def test_value_momentum_ranks_the_average_of_both_ranks(tmp_path):
    rows = [_row(f"D{i}", "2020-01-31", "DEU", be_me=float(i), mom=float(i), r=i / 100) for i in range(15)]
    panel = _write_panel(tmp_path, rows)

    combo = factor_series(panel, ("DEU",))
    assert combo[combo["strategy"] == "value_momentum"].iloc[0]["long_short"] == pytest.approx(0.10)


def test_missing_panel_gives_an_empty_frame(tmp_path):
    assert factor_series(tmp_path, ("DEU",)).empty


# --- evidence card --------------------------------------------------------------


def _series(ls: np.ndarray, lo: np.ndarray, start: str = "1990-01") -> pd.DataFrame:
    months = pd.period_range(start, periods=len(ls), freq="M")
    return pd.DataFrame({"month": months, "long_short": ls, "long_only": lo})


def test_a_strong_documented_premium_passes():
    rng = np.random.default_rng(1)
    n = 360
    card = evaluate(
        STRATEGY_BY_KEY["value"],
        _series(0.006 + rng.normal(0, 0.02, n), 0.003 + rng.normal(0, 0.01, n)),
        region="europe", tilt_cap_pct=15.0,
    )

    assert card.passed
    assert card.months == n and card.start == "1990-01"
    assert card.long_short_t is not None and card.long_short_t > S.MIN_T_STAT
    expected = card.long_only_annual * (1 - S.PUBLICATION_DECAY)
    assert card.expected_annual == pytest.approx(expected)
    assert card.net_expected_annual == pytest.approx((expected - S.IMPLEMENTATION_COST) * S.AFTER_TAX_FACTOR)
    assert card.book_worst_5y_lag == pytest.approx(card.worst_5y_excess * 0.15)
    assert len(card.yearly_long_only) == 30


def test_short_history_and_noise_fail_with_named_checks():
    rng = np.random.default_rng(2)
    card = evaluate(
        STRATEGY_BY_KEY["momentum"],
        _series(rng.normal(0, 0.02, 120), rng.normal(0, 0.01, 120), start="2010-01"),
        region="europe", tilt_cap_pct=15.0,
    )
    failed = {c["name"] for c in card.checks if not c["passed"]}
    assert not card.passed
    assert "history" in failed and "premium" in failed


def test_a_premium_that_died_after_publication_fails():
    # 1990-2013 strong, 2014-2019 negative (profitability, published 2013).
    ls = np.r_[np.full(288, 0.01), np.full(72, -0.002)] + np.random.default_rng(3).normal(0, 0.005, 360)
    card = evaluate(STRATEGY_BY_KEY["profitability"], _series(ls, ls / 2), region="europe", tilt_cap_pct=15.0)

    assert {c["name"] for c in card.checks if not c["passed"]} == {"post_publication"}
    assert card.post_publication_months == 72


def test_costs_can_eat_a_small_premium():
    rng = np.random.default_rng(4)
    ls = 0.006 + rng.normal(0, 0.01, 360)
    lo = np.full(360, 0.0005)  # 0.6 % a year long-only, before decay and costs
    card = evaluate(STRATEGY_BY_KEY["value"], _series(ls, lo), region="europe", tilt_cap_pct=15.0)

    assert card.net_expected_annual is not None and card.net_expected_annual < 0
    assert not card.passed


# --- study run and persistence --------------------------------------------------


def _signal_panel(tmp_path: Path, months: int = 300) -> Path:
    """Two countries where value predicts returns and everything else is noise."""
    rng = np.random.default_rng(0)
    rows = []
    for eom in pd.date_range("1995-01-31", periods=months, freq="ME"):
        for country in ("DEU", "FRA"):
            for i in range(20):
                be = float(rng.lognormal(0, 0.5))
                rows.append(_row(
                    f"{country}{i}", eom, country, be_me=be, mom=float(rng.normal()),
                    r=float(0.005 + 0.01 * np.log(be) + rng.normal(0, 0.05)),
                ))
    return _write_panel(tmp_path, rows)


def test_run_study_stores_cards_and_counts_trials_once(tmp_path):
    db = _memory_db()
    panel = _signal_panel(tmp_path)

    cards = run_study(db, panel_dir=panel)
    run_study(db, panel_dir=panel)

    assert [c.strategy for c in cards] == [s.key for s in S.STRATEGIES]
    by_key = {c.strategy: c for c in cards}
    assert by_key["value"].passed
    assert not by_key["momentum"].passed
    assert db.query(FactorEvidenceCard).count() == 2 * len(S.STRATEGIES)
    assert db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "factor_premia").count() == len(S.STRATEGIES)

    latest = latest_cards(db)
    assert [c["strategy"] for c in latest] == [s.key for s in S.STRATEGIES]
    assert latest[0]["computed_at"]
    assert {c["region"] for c in latest} == {"world"}  # the default region


def test_world_covers_the_msci_world_markets_and_contains_europe():
    assert len(S.WORLD_DEVELOPED) == len(set(S.WORLD_DEVELOPED)) == 23
    assert set(S.EUROPE_DEVELOPED) < set(S.WORLD_DEVELOPED)
    assert {"USA", "JPN", "CAN"} < set(S.WORLD_DEVELOPED)


def test_latest_cards_lists_each_region_world_first(tmp_path):
    db = _memory_db()
    panel = _signal_panel(tmp_path)
    run_study(db, region="europe", panel_dir=panel)
    run_study(db, region="world", panel_dir=panel)
    run_study(db, region="europe", panel_dir=panel)

    latest = latest_cards(db)

    n = len(S.STRATEGIES)
    assert len(latest) == 2 * n
    assert [c["region"] for c in latest] == ["world"] * n + ["europe"] * n
    assert db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "factor_premia").count() == 2 * n


def test_run_study_without_data_stores_nothing(tmp_path):
    db = _memory_db()
    assert run_study(db, panel_dir=tmp_path) == []
    assert db.query(FactorEvidenceCard).count() == 0


def test_dry_run_stores_nothing(tmp_path):
    db = _memory_db()
    assert run_study(db, panel_dir=_signal_panel(tmp_path, months=24), persist=False)
    assert db.query(FactorEvidenceCard).count() == 0


# --- monthly plan ---------------------------------------------------------------


def _card(db, strategy: str, passed: bool, run_id: str = "run-1", region: str = "world") -> None:
    db.add(FactorEvidenceCard(
        run_id=run_id, strategy=strategy, region=region, months=360, passed=passed,
        card_json={
            "strategy": strategy, "label": STRATEGY_BY_KEY[strategy].label, "region": region,
            "passed": passed, "net_expected_annual": 0.004 if passed else -0.001,
            "etf_hint": STRATEGY_BY_KEY[strategy].etf_hint,
        },
    ))
    db.commit()


def test_tilt_stays_locked_until_the_study_has_run():
    state = evidence_state(_memory_db())
    assert state["tilt_unlocked"] is False
    assert "python -m app.lab.factor_premia" in state["tilt_reason"]


def test_only_world_evidence_gates_the_tilt():
    """The tilt is a world factor ETF; a pass on Europe alone does not unlock it."""
    db = _memory_db()
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZB59"})
    _card(db, "value", passed=True, region="europe")

    state = evidence_state(db)
    assert state["tilt_unlocked"] is False
    assert "tested on world data" in state["tilt_reason"]

    _card(db, "value", passed=False, run_id="run-2", region="world")
    assert "None of the 1 pre-registered" in evidence_state(db)["tilt_reason"]


def test_tilt_stays_locked_when_nothing_passed():
    db = _memory_db()
    _card(db, "value", passed=False)
    _card(db, "momentum", passed=False)

    state = evidence_state(db)
    assert state["tilt_unlocked"] is False
    assert "None of the 2 pre-registered" in state["tilt_reason"]


def test_a_passing_card_asks_for_an_etf_before_unlocking():
    db = _memory_db()
    _card(db, "value", passed=True)

    state = evidence_state(db)
    assert state["tilt_unlocked"] is False
    assert "Value passed" in state["tilt_reason"]
    assert "world value factor ETF" in state["tilt_reason"]
    assert "+0.4 %" in state["tilt_reason"]


def test_a_passing_card_and_a_chosen_etf_unlock_the_tilt():
    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE00DEPOT", balance=0)
    db.add(account)
    db.flush()
    db.add(DkbPosition(account_id=account.id, isin="IE00B4L5Y983", name="iShares Core MSCI World",
                       quantity=1, current_value=36_000))
    db.commit()
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZB59"})
    _card(db, "value", passed=True)

    plan = build_monthly_plan(db, user.id)

    tilt = next(s for s in plan["sleeves"] if s["key"] == "tilt")
    assert tilt["unlocked"] is True and tilt["target_pct"] == 15.0
    assert tilt["status"].startswith("Unlocked: Value passed")
    action = next(a for a in plan["actions"] if a["sleeve"] == "tilt")
    assert action["isin"] == "IE00BP3QZB59" and action["amount_eur"] == 1_000.0


def test_only_the_latest_run_counts():
    db = _memory_db()
    _card(db, "value", passed=True, run_id="old")
    import time
    time.sleep(0.01)
    _card(db, "value", passed=False, run_id="new")

    assert monthly_plan.evidence_state(db)["tilt_unlocked"] is False
    assert "None of the 1" in monthly_plan.evidence_state(db)["tilt_reason"]


# --- API ------------------------------------------------------------------------


def test_factor_premia_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    run_study(db, panel_dir=_signal_panel(tmp_path, months=36))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = TestClient(app).get("/api/evidence/factor-premia")
        assert resp.status_code == 200
        body = resp.json()
        assert [c["strategy"] for c in body] == [s.key for s in S.STRATEGIES]
        assert body[0]["checks"][0]["name"] == "history"
        assert body[0]["passed"] is False  # 3 years is not enough history
    finally:
        app.dependency_overrides.clear()
