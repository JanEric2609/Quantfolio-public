"""ADR 0018 §8: the pre-registered factor study and the gated factor-neutral row.

Prices and FX come from injected loaders (no provider, no network); the DB is a
real in-memory SQLite session.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification import factor_study as fs
from app.decision.verification.trust import build_verdict
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DiscoverCandidateSnapshot,
    DiscoveryPrediction,
    TrustDailyActiveReturn,
    TrustFactorStudy,
    User,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> str:
    user = User(username="factor", password_hash="x")
    db.add(user)
    db.commit()
    return user.id


def _business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _business_days(date(2024, 1, 2), 520)
CORE = "EUNL.DE"
PICKS = ("AAA", "BBB", "CCC")
PEERS = ("DDD", "EEE")


def _closes(returns: np.ndarray) -> dict[date, float]:
    return {d: float(c) for d, c in zip(DAYS, 100.0 * np.cumprod(1.0 + returns))}


def _market(loads_value: float, seed: int = 11) -> tuple[dict[str, dict[date, float]], dict[str, float]]:
    """Daily EUR closes for the core, the factor ETFs, the picks and their peers."""
    rng = np.random.default_rng(seed)
    n = len(DAYS)
    core = rng.normal(0.0, 0.01, n)
    value = rng.normal(0.0, 0.006, n)
    series = {CORE: _closes(core)}
    for name, ticker in fs.FACTOR_ETFS.items():
        spread = value if name == "value" else rng.normal(0.0, 0.003, n)
        series[ticker] = _closes(core + spread)
    for s in PICKS:
        idio = rng.normal(0.0, 0.002 if loads_value else 0.012, n)
        series[s] = _closes(core + loads_value * value + idio)
    for s in PEERS:
        series[s] = _closes(core + rng.normal(0.0, 0.004, n))
    fx = {d.isoformat(): float(r) for d, r in zip(DAYS, 0.9 * np.cumprod(1.0 + rng.normal(0.0, 0.004, n)))}
    return series, fx


class Loaders:
    def __init__(self, series: dict[str, dict[date, float]], fx: dict[str, float]):
        self.series, self.fx = series, fx

    def prices(self, db, symbol, days, refresh):
        return {d.isoformat(): c for d, c in self.series.get(symbol.upper(), {}).items()}

    def fx_rates(self, db, currency, days):
        assert currency == "USD"
        return self.fx


def _backfilled_runs(db, uid: str, last_index: int) -> None:
    for k, i in enumerate(range(0, last_index, 5)):
        issued = DAYS[i]
        for sym in (*PICKS, *PEERS):
            db.add(DiscoverCandidateSnapshot(
                user_id=uid, run_id=f"bf{k}", issued_at=datetime.combine(issued, datetime.min.time(), tzinfo=UTC),
                issue_date=issued, symbol=sym, instrument_group="stock", sector="Tech", evaluable=True,
                composite=1.0, components_json={}, n_evaluable_stocks=5, shortlisted=sym in PICKS,
                sector_capped=False, picked=sym in PICKS, cohort_id="legacy_unstamped", provenance_json={},
                backfilled=True,
            ))
    db.commit()


def _study(db, uid, loaders, today=DAYS[399]):
    return fs.run_factor_study(db, uid, today=today, loader=loaders.prices, fx_loader=loaders.fx_rates)


def test_the_spec_fixes_the_factor_set_and_the_gate():
    spec = fs.spec(CORE)
    assert spec["version"] == fs.SPEC_VERSION == 1
    assert set(spec["factors"]) == set(fs.FACTORS) == {
        "market", "sector", "usd_eur", "value", "momentum", "quality", "min_vol", "size", "europe",
    }
    assert spec["gate"] == {"build": 0.30, "drop": 0.15}
    assert fs.decide(0.30) == "build" and fs.decide(0.2999) == "neither" and fs.decide(0.1499) == "drop"


def test_too_little_backfilled_data_waits_and_records_nothing():
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(1.0)
    _backfilled_runs(db, uid, 100)
    out = _study(db, uid, Loaders(series, fx))
    assert out["status"] == "waiting" and out["n_days_needed"] == fs.MIN_FIT_DAYS + fs.MIN_OOS_DAYS
    assert db.query(TrustFactorStudy).count() == 0


def test_a_basket_driven_by_a_factor_is_built_once_and_the_record_is_binding():
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(1.0)
    _backfilled_runs(db, uid, 380)
    loaders = Loaders(series, fx)
    out = _study(db, uid, loaders)
    db.commit()
    assert out["status"] == "decided" and out["decision"] == "build"
    assert out["oos_r2"] > 0.8 and out["n_oos_days"] >= fs.MIN_OOS_DAYS
    assert out["betas"]["value"] == pytest.approx(1.0, abs=0.1)
    assert out["spec"]["factors"]["value"].startswith("IS3S.DE")

    # Different data later never changes the recorded decision.
    noisy, fx2 = _market(0.0, seed=99)
    again = _study(db, uid, Loaders(noisy, fx2))
    assert again["decision"] == "build" and again["oos_r2"] == out["oos_r2"]
    assert db.query(TrustFactorStudy).count() == 1


def test_a_basket_unrelated_to_the_factors_is_dropped_and_no_row_is_built():
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(0.0)
    _backfilled_runs(db, uid, 380)
    loaders = Loaders(series, fx)
    out = _study(db, uid, loaders)
    db.commit()
    assert out["decision"] == "drop" and out["oos_r2"] < fs.DROP_R2
    assert fs.freeze_neutral_series(db, uid, today=DAYS[-1], loader=loaders.prices, fx_loader=loaders.fx_rates) == 0
    summary = fs.factor_neutral_summary(db, uid)
    assert summary["study"]["decision"] == "drop" and summary["row"] is None


def test_the_neutral_row_removes_the_fitted_factor_moves_from_frozen_ideas_days(monkeypatch):
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(1.0)
    _backfilled_runs(db, uid, 380)
    loaders = Loaders(series, fx)
    _study(db, uid, loaders)
    db.commit()

    # Live F1: one pick of AAA on day 420, frozen ideas days over its window.
    issue = DAYS[420]
    db.add(DiscoveryPrediction(
        user_id=uid, run_id="live1", symbol="AAA", predicted_at=datetime.combine(issue, datetime.min.time(), tzinfo=UTC),
        horizon_days=21, resolve_at=datetime.combine(DAYS[441], datetime.min.time(), tzinfo=UTC),
        direction="buy", conviction=0.6,
    ))
    window = DAYS[421:442]
    closes_a, closes_core = series["AAA"], series[CORE]
    for prev, day in zip(DAYS[420:441], window):
        y = (closes_a[day] / closes_a[prev] - 1.0) - (closes_core[day] / closes_core[prev] - 1.0)
        db.add(TrustDailyActiveReturn(
            user_id=uid, series="ideas", day=day, n_open=1, n_stale=0, value=y, benchmark=CORE, detail_json={},
        ))
    db.commit()
    monkeypatch.setattr(fs, "PRIMARY_TEST_START", DAYS[0])

    written = fs.freeze_neutral_series(db, uid, today=DAYS[-1], loader=loaders.prices, fx_loader=loaders.fx_rates)
    db.commit()
    assert written == len(window)
    rows = (
        db.query(TrustDailyActiveReturn)
        .filter(TrustDailyActiveReturn.series == fs.SERIES_IDEAS_NEUTRAL)
        .order_by(TrustDailyActiveReturn.day)
        .all()
    )
    raw = [r.value for r in db.query(TrustDailyActiveReturn).filter_by(series="ideas").order_by("day")]
    # The value tilt explains most of AAA's active move: the residual is much smaller.
    assert np.std([r.value for r in rows]) < 0.5 * np.std(raw)
    assert set(rows[0].detail_json["betas"]) == set(fs.FACTORS)
    # Written once: a second pass adds nothing.
    assert fs.freeze_neutral_series(db, uid, today=DAYS[-1], loader=loaders.prices, fx_loader=loaders.fx_rates) == 0

    summary = fs.factor_neutral_summary(db, uid)
    assert summary["row"]["n_days"] == len(window)
    assert summary["study"]["decision"] == "build"


def test_the_verdict_carries_the_study_state():
    db = _memory_db()
    uid = _user(db)
    verdict = build_verdict(db, uid, now=datetime(2026, 10, 8, tzinfo=UTC))
    fn = verdict["daily_tests"]["factor_neutral"]
    assert fn["study"]["status"] == "waiting" and fn["row"] is None
    assert fn["gate"]["build"] == 0.30


def test_a_missing_factor_close_drops_the_day_instead_of_counting_it_as_zero():
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(1.0)
    _backfilled_runs(db, uid, 380)
    loaders = Loaders(series, fx)
    full = fs.build_panel(db, fs.study_picks(db, uid), today=DAYS[399], loader=loaders.prices, fx_loader=loaders.fx_rates)
    gap = DAYS[100:120]
    for d in gap:
        del series["IQQ0.DE"][d]
    del fx[DAYS[150].isoformat()]
    thinner = fs.build_panel(db, fs.study_picks(db, uid), today=DAYS[399], loader=loaders.prices, fx_loader=loaders.fx_rates)
    kept = {r.day for r in thinner}
    # The gap days, the day after the gap (its "before" is missing) and the
    # two days whose FX move would span the missing rate.
    dropped = {*gap, DAYS[120], DAYS[150], DAYS[151]}
    assert not kept & dropped
    assert kept == {r.day for r in full} - dropped

    # No factor ETF data at all: the study waits rather than recording a decision.
    for ticker in fs.FACTOR_ETFS.values():
        series[ticker] = {}
    out = _study(db, uid, Loaders(series, fx))
    assert out["status"] == "waiting" and db.query(TrustFactorStudy).count() == 0


def test_days_too_early_to_fit_do_not_stall_the_neutral_row(monkeypatch):
    db = _memory_db()
    uid = _user(db)
    series, fx = _market(1.0)
    _backfilled_runs(db, uid, 380)
    loaders = Loaders(series, fx)
    _study(db, uid, loaders)
    issue = DAYS[420]
    db.add(DiscoveryPrediction(
        user_id=uid, run_id="live1", symbol="AAA", predicted_at=datetime.combine(issue, datetime.min.time(), tzinfo=UTC),
        horizon_days=21, resolve_at=datetime.combine(DAYS[441], datetime.min.time(), tzinfo=UTC),
        direction="buy", conviction=0.6,
    ))
    for day in DAYS[421:442]:
        db.add(TrustDailyActiveReturn(
            user_id=uid, series="ideas", day=day, n_open=1, n_stale=0, value=0.001, benchmark=CORE, detail_json={},
        ))
    db.commit()
    kwargs = {"today": DAYS[-1], "loader": loaders.prices, "fx_loader": loaders.fx_rates}
    # The study's days alone are too few: the first ten live days only train.
    n_study = len([r for r in fs.build_panel(db, fs.study_picks(db, uid), **kwargs) if r.day < DAYS[421]])
    monkeypatch.setattr(fs, "MIN_FIT_DAYS", n_study + 10)
    assert fs.freeze_neutral_series(db, uid, **kwargs) == 11
    db.commit()
    written = {r.day for r in db.query(TrustDailyActiveReturn).filter_by(series=fs.SERIES_IDEAS_NEUTRAL)}
    assert min(written) == DAYS[431]
    assert fs.freeze_neutral_series(db, uid, **kwargs) == 0


def test_live_peers_come_from_the_predictions_own_run():
    db = _memory_db()
    uid = _user(db)
    day = DAYS[10]
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    for run_id, peers in (("r1", ("DDD",)), ("r2", ("EEE",))):
        for sym, picked in (("AAA", True), *((p, False) for p in peers)):
            db.add(DiscoverCandidateSnapshot(
                user_id=uid, run_id=run_id, issued_at=at, issue_date=day, symbol=sym, instrument_group="stock",
                sector="Tech", evaluable=True, composite=1.0, components_json={}, n_evaluable_stocks=2,
                shortlisted=picked, sector_capped=False, picked=picked, cohort_id="c", provenance_json={},
            ))
        db.add(DiscoveryPrediction(
            user_id=uid, run_id=run_id, symbol="AAA", predicted_at=at, horizon_days=21,
            resolve_at=datetime.combine(DAYS[31], datetime.min.time(), tzinfo=UTC), direction="buy", conviction=0.5,
        ))
    db.commit()
    peers = sorted(p.peers for p in fs.live_picks(db, uid))
    assert peers == [("DDD",), ("EEE",)]
