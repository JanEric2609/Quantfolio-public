"""selection_w and ranking_ef: the lower-noise series of ADR 0018's amendment of 2026-11-08."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification import daily_ledger
from app.decision.verification.ledger_prices import EurSeries
from app.foundation import evidence_weights as ew
from app.foundation.core.db import Base
from app.foundation.models.entities import DiscoverCandidateSnapshot, TrustDailyActiveReturn, User

BENCH = "EUNL.DE"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> str:
    user = User(username="weighted", password_hash="x")
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


DAYS = _business_days(date(2025, 9, 1), 300)
ISSUE = DAYS[270]


class FakePrices:
    def __init__(self, series: dict[str, dict[date, float]]):
        self.series = series

    def __call__(self, db, symbol, days, refresh):
        return {d.isoformat(): c for d, c in self.series.get(symbol.upper(), {}).items()}


def _closes(returns: np.ndarray) -> dict[date, float]:
    return {d: float(c) for d, c in zip(DAYS, 100.0 * np.cumprod(1.0 + returns))}


def _snap(db, uid, symbol, sector, *, picked=False, composite=0.5, run_id="r1", issued=ISSUE):
    db.add(DiscoverCandidateSnapshot(
        user_id=uid, run_id=run_id, issued_at=datetime.combine(issued, datetime.min.time(), tzinfo=UTC),
        issue_date=issued, symbol=symbol, instrument_group="stock", sector=sector, evaluable=True,
        composite=composite, components_json={}, n_evaluable_stocks=10, shortlisted=False, sector_capped=False,
        picked=picked, cohort_id="c1-x", provenance_json={},
    ))


def _market(seed: int = 4):
    rng = np.random.default_rng(seed)
    m = rng.normal(0.0004, 0.01, len(DAYS))
    return rng, m


def _prices_for(db, loader, today=DAYS[-1]):
    prices = EurSeries(db, days=(today - DAYS[0]).days + 5, refresh=False, loader=loader)
    return prices, [d for d in prices.calendar(BENCH) if d <= today]


def test_controls_are_the_most_similar_same_sector_non_picks():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market()
    series = {BENCH: _closes(m)}
    # The pick and five look-alikes: beta 1, quiet. Three in its sector are wild and high-beta.
    series["P1"] = _closes(m + rng.normal(0, 0.008, len(DAYS)))
    for i in range(5):
        series[f"SIM{i}"] = _closes(m + rng.normal(0, 0.008, len(DAYS)))
    for i in range(3):
        series[f"WILD{i}"] = _closes(2.5 * m + rng.normal(0, 0.04, len(DAYS)))
    _snap(db, uid, "P1", "Tech", picked=True)
    for s in [*(f"SIM{i}" for i in range(5)), *(f"WILD{i}" for i in range(3))]:
        _snap(db, uid, s, "Tech")
    db.commit()
    prices, cal = _prices_for(db, FakePrices(series))
    (member,) = daily_ledger.selection_weighted_members(db, uid, prices, BENCH, cal)
    assert {p.symbol for p in member.positions} == {"P1", *(f"SIM{i}" for i in range(5))}
    assert not member.fallback and member.weight > 0
    # The frozen 2026-10-14 rule would have used all eight sector peers.
    (frozen,) = daily_ledger.selection_members(db, uid)
    assert len(frozen.positions) == 9


def test_the_window_ends_before_the_issue_day():
    db = _memory_db()
    _, m = _market()
    prices = EurSeries(db, days=500, refresh=False, loader=FakePrices({BENCH: _closes(m), "X": _closes(m)}))
    cal = prices.calendar(BENCH)
    got = daily_ledger.trailing_returns(prices, ["X"], BENCH, cal, ISSUE)
    assert got is not None
    kept, rets, bench = got
    assert kept == ["X"] and rets.shape == (ew.LOOKBACK, 1) and len(bench) == ew.LOOKBACK
    # Its last return is the one into the trading day before the issue day.
    i = DAYS.index(ISSUE)
    expected = series_return = _closes(m)[DAYS[i - 1]] / _closes(m)[DAYS[i - 2]] - 1.0
    assert rets[-1, 0] == pytest.approx(expected) and series_return == pytest.approx(expected)
    # Too little history: no window.
    assert daily_ledger.trailing_returns(prices, ["X"], BENCH, cal, DAYS[100]) is None


def test_quiet_picks_weigh_more_and_the_day_is_their_weighted_mean():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market(5)
    series = {BENCH: _closes(m)}
    n = len(DAYS)
    tech = rng.normal(0, 0.01, n)
    energy = rng.normal(0, 0.01, n)
    series["QUIET"] = _closes(m + tech + rng.normal(0, 0.003, n))
    series["LOUD"] = _closes(m + energy + rng.normal(0, 0.03, n))
    for i in range(5):
        # Peers correlate about 0.85: below the 0.95 that marks a second share class.
        series[f"T{i}"] = _closes(m + tech + rng.normal(0, 0.006, n))
        series[f"E{i}"] = _closes(m + energy + rng.normal(0, 0.006, n))
    _snap(db, uid, "QUIET", "Tech", picked=True)
    _snap(db, uid, "LOUD", "Energy", picked=True)
    for i in range(5):
        _snap(db, uid, f"T{i}", "Tech")
        _snap(db, uid, f"E{i}", "Energy")
    db.commit()
    loader = FakePrices(series)
    prices, cal = _prices_for(db, loader)
    members = {mb.key: mb for mb in daily_ledger.selection_weighted_members(db, uid, prices, BENCH, cal)}
    quiet, loud = members["r1:QUIET"], members["r1:LOUD"]
    assert quiet.weight > 20 * loud.weight  # spread noise about 0.3 % against 3 % a day

    daily_ledger.freeze_series(db, uid, "selection_w", today=DAYS[-1], loader=loader)
    db.commit()
    day = DAYS[275]
    row = db.query(TrustDailyActiveReturn).filter_by(series="selection_w", day=day).one()
    prev = DAYS[274]

    def spread(mb):
        return sum(p.weight * (series[p.symbol][day] / series[p.symbol][prev] - 1.0) for p in mb.positions)

    expected = (quiet.weight * spread(quiet) + loud.weight * spread(loud)) / (quiet.weight + loud.weight)
    assert row.n_open == 2 and row.value == pytest.approx(expected)
    # Nothing before the issue is written: the series starts at the first pick's window.
    assert not db.query(TrustDailyActiveReturn).filter(
        TrustDailyActiveReturn.series == "selection_w", TrustDailyActiveReturn.day <= ISSUE,
    ).count()


def test_a_pick_without_a_full_window_keeps_the_sector_rule_and_the_median_weight():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market(6)
    series = {BENCH: _closes(m)}
    for s in ("P1", "T0", "T1", "T2", "T3"):
        series[s] = _closes(m + rng.normal(0, 0.01, len(DAYS)))
    # NEW listed 40 days before the issue.
    series["NEW"] = {d: c for d, c in _closes(m + rng.normal(0, 0.01, len(DAYS))).items() if d >= DAYS[230]}
    for s, picked in (("P1", True), ("NEW", True), ("T0", False), ("T1", False), ("T2", False), ("T3", False)):
        _snap(db, uid, s, "Tech", picked=picked)
    db.commit()
    prices, cal = _prices_for(db, FakePrices(series))
    members = {mb.key: mb for mb in daily_ledger.selection_weighted_members(db, uid, prices, BENCH, cal)}
    new, p1 = members["r1:NEW"], members["r1:P1"]
    # The 2026-10-14 rule: every same-sector non-pick, equally weighted.
    assert {p.symbol for p in new.positions} == {"NEW", "T0", "T1", "T2", "T3"}
    assert new.weight == pytest.approx(p1.weight)  # the median of the one estimated weight


def test_ranking_ef_keeps_the_rank_exposure_with_zero_net_weight():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market(7)
    series = {BENCH: _closes(m)}
    n_stocks = 40
    for i in range(n_stocks):
        beta = 0.5 + i / n_stocks
        series[f"S{i:02d}"] = _closes(beta * m + rng.normal(0, 0.005 + 0.02 * (i % 4) / 3, len(DAYS)))
        _snap(db, uid, f"S{i:02d}", "Any", composite=float(rng.normal()))
    db.commit()
    prices, cal = _prices_for(db, FakePrices(series))
    (member,) = daily_ledger.ranking_efficient_members(db, uid, prices, BENCH, cal)
    w = {p.symbol: p.weight for p in member.positions}
    rows = db.query(DiscoverCandidateSnapshot).all()
    rank = dict(zip(sorted(r.symbol for r in rows), ew.centred_rank_weights(
        np.array([next(r.composite for r in rows if r.symbol == s) for s in sorted(r.symbol for r in rows)])
    )))
    assert sum(w.values()) == pytest.approx(0.0, abs=1e-9)
    assert sum(w[s] * rank[s] for s in w) == pytest.approx(sum(v * v for v in rank.values()))
    assert not member.vs_benchmark and member.weight == 1.0


def test_ranking_ef_needs_thirty_stocks_with_a_full_window():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market(8)
    series = {BENCH: _closes(m)}
    for i in range(29):
        series[f"S{i:02d}"] = _closes(m + rng.normal(0, 0.01, len(DAYS)))
        _snap(db, uid, f"S{i:02d}", "Any", composite=float(i))
    db.commit()
    prices, cal = _prices_for(db, FakePrices(series))
    assert daily_ledger.ranking_efficient_members(db, uid, prices, BENCH, cal) == []


def test_a_second_share_class_is_never_a_control_and_weights_are_capped():
    db = _memory_db()
    uid = _user(db)
    rng, m = _market(9)
    n = len(DAYS)
    series = {BENCH: _closes(m)}
    base = m + rng.normal(0, 0.01, n)
    series["ZPICK"] = _closes(base)
    series["APICK2"] = _closes(base + rng.normal(0, 0.0005, n))  # the pick's twin, sorts first
    for i in range(6):
        series[f"T{i}"] = _closes(m + rng.normal(0, 0.01, n))
    _snap(db, uid, "ZPICK", "Tech", picked=True)
    _snap(db, uid, "APICK2", "Tech")
    for i in range(6):
        _snap(db, uid, f"T{i}", "Tech")
    db.commit()
    prices, cal = _prices_for(db, FakePrices(series))
    (member,) = daily_ledger.selection_weighted_members(db, uid, prices, BENCH, cal)
    assert "APICK2" not in {p.symbol for p in member.positions}


def test_weights_are_capped_at_four_times_the_median():
    assert ew.cap_weights([1.0, 1.0, 1.0, 100.0]) == [1.0, 1.0, 1.0, 4.0]


def test_ranking_ef_drops_the_later_of_two_near_identical_listings():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 0.01, (252, 3))
    a[:, 2] = a[:, 0] + rng.normal(0, 0.0005, 252)
    assert ew.near_duplicates(a) == {2} and ew.near_duplicate_pairs(a) == [(0, 2)]
