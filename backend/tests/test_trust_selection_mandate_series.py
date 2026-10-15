"""The selection and mandate series (ADR 0018 amendment of 2026-10-14, §2 and §5)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification import daily_ledger
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DiscoverCandidateSnapshot,
    PaperPortfolio,
    PaperSnapshot,
    TrustDailyActiveReturn,
    User,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> str:
    user = User(username="ledger", password_hash="x")
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


DAYS = _business_days(date(2026, 10, 15), 40)
BENCH = "EUNL.DE"


class FakePrices:
    def __init__(self, series: dict[str, dict[date, float]]):
        self.series = series

    def __call__(self, db, symbol, days, refresh):
        return {d.isoformat(): c for d, c in self.series.get(symbol.upper(), {}).items()}


def _geometric(days: list[date], daily: float, start: float = 100.0) -> dict[date, float]:
    return {d: start * (1.0 + daily) ** i for i, d in enumerate(days)}


def _snap(db, uid, run_id, issued, symbol, sector, *, picked=False, evaluable=True, group="stock"):
    db.add(DiscoverCandidateSnapshot(
        user_id=uid, run_id=run_id, issued_at=datetime.combine(issued, datetime.min.time(), tzinfo=UTC),
        issue_date=issued, symbol=symbol, instrument_group=group, sector=sector, evaluable=evaluable,
        composite=0.5, components_json={}, n_evaluable_stocks=10, shortlisted=False, sector_capped=False,
        picked=picked, cohort_id="c1-x", provenance_json={},
    ))


# --- selection ------------------------------------------------------------------


def test_selection_is_each_pick_against_its_sector_controls():
    db = _memory_db()
    uid = _user(db)
    d0 = DAYS[0]
    _snap(db, uid, "r1", d0, "P1", "Tech", picked=True)
    _snap(db, uid, "r1", d0, "P2", "Energy", picked=True)
    for s in ("T1", "T2", "T3"):
        _snap(db, uid, "r1", d0, s, "Tech")
    for s in ("E1", "E2", "E3"):
        _snap(db, uid, "r1", d0, s, "Energy")
    _snap(db, uid, "r1", d0, "NOPE", "Tech", evaluable=False)     # not evaluable: never a control
    _snap(db, uid, "r1", d0, "ETF1", "Tech", group="etf")         # not a stock
    db.commit()
    window = DAYS[:6]
    prices = FakePrices({
        BENCH: _geometric(window, 0.005),
        "P1": _geometric(window, 0.03), "P2": _geometric(window, 0.0),
        "T1": _geometric(window, 0.01), "T2": _geometric(window, 0.02), "T3": _geometric(window, 0.03),
        "E1": _geometric(window, 0.01), "E2": _geometric(window, 0.01), "E3": _geometric(window, 0.01),
        "NOPE": _geometric(window, 0.5), "ETF1": _geometric(window, 0.5),
    })
    daily_ledger.freeze_series(db, uid, "selection", today=DAYS[5], loader=prices)
    db.commit()
    row = db.query(TrustDailyActiveReturn).filter_by(series="selection", day=DAYS[1]).one()
    # P1: 3 % - mean(1, 2, 3) % = +1 %; P2: 0 % - 1 % = -1 %; the mean of the two is 0.
    assert row.n_open == 2 and row.value == pytest.approx(0.0, abs=1e-12)
    assert row.detail_json["n_fallback"] == 0
    members = {m.key: m for m in daily_ledger.selection_members(db, uid)}
    assert {p.symbol for p in members["r1:P1"].positions} == {"P1", "T1", "T2", "T3"}


def test_a_sector_with_two_controls_falls_back_to_all_non_picks():
    db = _memory_db()
    uid = _user(db)
    d0 = DAYS[0]
    _snap(db, uid, "r1", d0, "P1", "Tech", picked=True)
    _snap(db, uid, "r1", d0, "T1", "Tech")
    _snap(db, uid, "r1", d0, "T2", "Tech")
    _snap(db, uid, "r1", d0, "E1", "Energy")
    db.commit()
    (member,) = daily_ledger.selection_members(db, uid)
    assert member.fallback and {p.symbol for p in member.positions} == {"P1", "T1", "T2", "E1"}
    window = DAYS[:4]
    prices = FakePrices({BENCH: _geometric(window, 0.0), **{s: _geometric(window, 0.01) for s in ("P1", "T1", "T2", "E1")}})
    daily_ledger.freeze_series(db, uid, "selection", today=DAYS[3], loader=prices)
    db.commit()
    row = db.query(TrustDailyActiveReturn).filter_by(series="selection", day=DAYS[1]).one()
    assert row.detail_json["n_fallback"] == 1 and row.value == pytest.approx(0.0, abs=1e-12)


def test_a_delisted_control_is_carried_as_cash():
    db = _memory_db()
    uid = _user(db)
    d0 = DAYS[0]
    _snap(db, uid, "r1", d0, "P1", "Tech", picked=True)
    for s in ("T1", "T2", "T3"):
        _snap(db, uid, "r1", d0, s, "Tech")
    db.commit()
    window = DAYS[:12]
    prices = FakePrices({
        BENCH: _geometric(window, 0.0), "P1": _geometric(window, 0.01),
        "T1": _geometric(window, 0.01), "T2": _geometric(window, 0.01),
        "T3": _geometric(DAYS[:2], 0.01),  # stops trading after DAYS[1]
    })
    daily_ledger.freeze_series(db, uid, "selection", today=DAYS[11], loader=prices)
    db.commit()
    row = db.query(TrustDailyActiveReturn).filter_by(series="selection", day=DAYS[3]).one()
    # T3 earns 0 from then on: control = (1 + 1 + 0) / 3 %.
    assert row.n_stale == 1 and row.value == pytest.approx(0.01 - 0.02 / 3)


# --- mandates -------------------------------------------------------------------


def _sleeve(db, uid, mandate, inception: date) -> PaperPortfolio:
    p = PaperPortfolio(user_id=uid, name=f"Mandate {mandate}", mandate=mandate, managed_by="llm",
                       initial_cash=Decimal("10000"),
                       inception_at=datetime.combine(inception, datetime.min.time(), tzinfo=UTC))
    db.add(p)
    db.flush()
    return p


def _navs(db, sleeve, navs: dict[date, float]):
    for d, v in navs.items():
        db.add(PaperSnapshot(portfolio_id=sleeve.id, date=d, total_value=Decimal(str(round(v, 2))),
                             cash_balance=Decimal("0"), securities_value=Decimal(str(round(v, 2)))))
    db.commit()


def test_mandate_series_is_the_sleeve_against_the_core_from_inception():
    db = _memory_db()
    uid = _user(db)
    sleeve = _sleeve(db, uid, "A", DAYS[0])
    window = DAYS[:8]
    _navs(db, sleeve, {d: 10_000 * 1.002 ** i for i, d in enumerate(window)})
    prices = FakePrices({BENCH: _geometric([DAYS[0] - timedelta(days=3), *window], 0.001)})
    written = daily_ledger.freeze_series(db, uid, "mandate_a", today=DAYS[7], loader=prices)
    db.commit()
    rows = db.query(TrustDailyActiveReturn).filter_by(series="mandate_a").order_by(TrustDailyActiveReturn.day).all()
    assert written == len(rows) == 7  # every trading day after inception, none before
    assert rows[0].day == DAYS[1]
    for r in rows:
        assert r.n_open == 1 and r.value == pytest.approx(0.001, rel=1e-2)
    assert daily_ledger.freeze_series(db, uid, "mandate_b", today=DAYS[7], loader=prices) == 0  # no B sleeve


def test_a_missing_snapshot_waits_then_freezes_empty():
    db = _memory_db()
    uid = _user(db)
    sleeve = _sleeve(db, uid, "B", DAYS[0])
    navs = {d: 10_000.0 for d in DAYS[:12]}
    del navs[DAYS[3]]
    _navs(db, sleeve, navs)
    prices = FakePrices({BENCH: _geometric(DAYS[:12], 0.0)})
    daily_ledger.freeze_series(db, uid, "mandate_b", today=DAYS[5], loader=prices)
    db.commit()
    assert sorted(r.day for r in db.query(TrustDailyActiveReturn).all()) == DAYS[1:3]
    daily_ledger.freeze_series(db, uid, "mandate_b", today=DAYS[11], loader=prices)
    db.commit()
    gap = db.query(TrustDailyActiveReturn).filter_by(day=DAYS[3]).one()
    assert gap.value is None and gap.n_open == 0 and gap.detail_json["missing"] == "snapshot"


# --- the tests (S, U, M) and the retire rule -------------------------------------

import numpy as np  # noqa: E402

from app.decision.verification import daily_tests  # noqa: E402


def _rows(uid, series, values, start=daily_tests.PRIMARY_TEST_START):
    days = _business_days(start, len(values))
    return [
        TrustDailyActiveReturn(user_id=uid, series=series, day=d, n_open=1, n_stale=0, value=float(v),
                               benchmark=BENCH, detail_json={})
        for d, v in zip(days, values)
    ]


def test_a_losing_selection_is_judged_not_worth_following_within_ten_years():
    # -0.5 % per 21 days: 99 % of simulated paths reach the verdict within 10
    # years (median 2.6). With no edge at all the median is 8.7 years.
    rng = np.random.default_rng(11)
    values = rng.normal(-0.005 / 21, daily_tests.SELECTION_SIGMA_REF, 252 * 10)
    out = daily_tests.selection_result(_rows("u", "selection", values))
    assert out["e_futility"] >= daily_tests.FUTILITY_THRESHOLD
    assert out["state"] == "not_worth_following"


def test_an_edge_above_the_margin_is_never_judged_futile_in_twenty_years():
    rng = np.random.default_rng(12)
    mean = (daily_tests.FUTILITY_DELTA_21D + 0.002) / 21
    values = rng.normal(mean, daily_tests.SELECTION_SIGMA_REF, 252 * 20)
    out = daily_tests.selection_result(_rows("u", "selection", values))
    e = daily_tests._bet(list(values), daily_tests.SELECTION_CLIP, shift=daily_tests.FUTILITY_DELTA_21D / 21)[1]
    assert e.max_value < daily_tests.FUTILITY_THRESHOLD
    assert out["state"] != "not_worth_following"


def test_days_before_t0_never_enter_the_selection_test():
    early = _rows("u", "selection", [-0.05] * 30, start=daily_tests.PRIMARY_TEST_START - timedelta(days=60))
    out = daily_tests.selection_result(early)
    assert out["n_days"] == 0 and out["n_days_pre_registration"] == 30 and out["e_futility"] == 1.0


def test_mandate_sigma_comes_from_its_first_63_days_and_the_test_starts_after():
    rng = np.random.default_rng(3)
    values = rng.normal(0.0, 0.004, 63)
    out = {m["series"]: m for m in daily_tests.mandate_results({"mandate_a": _rows("u", "mandate_a", values)})}
    assert out["mandate_a"]["n_tested_days"] == 0 and out["mandate_a"]["e_futility"] == 1.0
    assert out["mandate_b"]["state"] == "too_early"


def test_a_futile_mandate_retires_once(monkeypatch):
    from app.decision.verification.jobs import RETIRED, retire_futile_mandates

    sent: list[str] = []
    monkeypatch.setattr("app.foundation.notify.alert_user", lambda db, user_id, **kw: sent.append(kw["title"]) or True)
    db = _memory_db()
    uid = _user(db)
    sleeve = _sleeve(db, uid, "A", daily_tests.PRIMARY_TEST_START)
    rng = np.random.default_rng(5)
    # A cash-heavy sleeve trailing the core by about 1 % per 21 days.
    values = rng.normal(-0.01 / 21, 0.004, 252 * 4)
    db.add_all(_rows(uid, "mandate_a", values))
    db.commit()
    assert retire_futile_mandates(db, uid) == ["A"]
    db.commit()
    assert retire_futile_mandates(db, uid) == []
    assert db.get(PaperPortfolio, sleeve.id).managed_by == RETIRED
    assert sent == ["Mandate A retired"]


def test_a_reset_sleeve_is_tested_on_its_new_run_only():
    from app.decision.verification.daily_ledger import mandate_starts

    db = _memory_db()
    uid = _user(db)
    rng = np.random.default_rng(5)
    old = _rows(uid, "mandate_a", rng.normal(-0.01 / 21, 0.004, 252 * 4))
    db.add_all(old)
    reset_day = old[-1].day
    _sleeve(db, uid, "A", reset_day)
    db.commit()
    starts = mandate_starts(db, uid)
    assert starts == {"mandate_a": reset_day}
    out = {m["series"]: m for m in daily_tests.mandate_results({"mandate_a": old}, starts)}
    assert out["mandate_a"]["n_days"] == 0 and out["mandate_a"]["retire"] is False
    unscoped = {m["series"]: m for m in daily_tests.mandate_results({"mandate_a": old})}
    assert unscoped["mandate_a"]["retire"] is True


def test_a_sleeve_with_flat_first_days_is_still_tested():
    values = [0.0] * 63 + [-0.01 / 21] * (252 * 4)
    out = {m["series"]: m for m in daily_tests.mandate_results({"mandate_a": _rows("u", "mandate_a", values)})}
    assert out["mandate_a"]["sigma_ref_daily"] == daily_tests.MANDATE_SIGMA_FLOOR
    assert out["mandate_a"]["n_tested_days"] > 0 and out["mandate_a"]["e_futility"] > 1.0


def test_mandate_owners_without_discovery_records_are_ledger_users():
    from app.decision.verification.daily_ledger import ledger_users, mandate_users

    db = _memory_db()
    uid = _user(db)
    _sleeve(db, uid, "B", daily_tests.PRIMARY_TEST_START)
    db.commit()
    assert ledger_users(db) == [] and mandate_users(db) == [uid]


def test_the_selection_test_shows_its_lag1_autocorrelation():
    rng = np.random.default_rng(7)
    out = daily_tests.selection_result(_rows("u", "selection", rng.normal(0.0, 0.004, 70)))
    assert out["rho1"] is not None and isinstance(out["rho1_flag"], bool)
    assert daily_tests.selection_result(_rows("u", "selection", [0.001] * 10))["rho1"] is None


def test_a_retired_mandate_is_not_reviewed():
    from app.decision.llm_portfolio.review import run_mandate_review

    db = _memory_db()
    uid = _user(db)
    sleeve = _sleeve(db, uid, "B", daily_tests.PRIMARY_TEST_START)
    sleeve.managed_by = "retired"
    db.commit()
    out = run_mandate_review(db, uid, "B")
    assert out["status"] == "retired" and out["trades"] == []


def test_the_skill_headline_names_the_cross_sector_fallback():
    from app.decision.verification.trust import _selection_scope

    assert _selection_scope({"n_fallback_days": 0}) == "same run, same sector"
    assert "3 trading days" in _selection_scope({"n_fallback_days": 3})
