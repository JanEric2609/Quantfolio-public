"""D6 (plan 2026-10-14): the plan's long-history fan and the monthly one-year bands (synthetic data only)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import PlanBandForecast, PlanBandScore, User
from app.foundation.wealth_planner import return_band
from app.lab import plan_bands
from app.lab.long_history.plan_fan import (
    TAX_RATE,
    TER,
    Mix,
    after_tax,
    histories,
    one_year_band,
    project,
    simulate,
)
from app.lab.plan_bands import service


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _raw(eq: float = 0.05, inflation: float = 0.02, countries=("Germany", "France")) -> pd.DataFrame:
    rows = []
    for country in countries:
        for i, year in enumerate(range(1900, 1941)):
            rows.append({"country": country, "year": year, "eq_tr": eq, "bond_tr": 0.02, "bill_rate": 0.01,
                         "cpi": 100.0 * (1.0 + inflation) ** i, "xrusd": 1.0, "gdp": 100.0})
    return pd.DataFrame(rows)


def test_histories_are_real_and_haircut_like_adr_0022():
    raw = _raw()
    plain = histories(raw, ("Germany",), haircut_share=0.0)[0]
    cut = histories(raw, ("Germany",))[0]
    real_eq = 1.05 / 1.02 - 1.0
    real_bill = 1.01 / 1.02 - 1.0
    assert plain.real[:, 0] == pytest.approx(real_eq)
    assert plain.inflation == pytest.approx(0.02)
    assert cut.real[:, 0] == pytest.approx(real_eq - 0.3 * (real_eq - real_bill))
    assert cut.real[:, 1] == pytest.approx(plain.real[:, 1])  # bonds are not haircut


def test_constant_returns_give_the_compound_wealth_and_band():
    hist = histories(_raw(), ("Germany", "France"), haircut_share=0.0)
    r = 1.05 / 1.02 - 1.0 - TER[0]
    sim = simulate(hist, start_value=1000.0, monthly_contribution=100.0, years=3, paths=50)
    w = 1000.0
    for _ in range(3):
        w = (w + 1200.0) * (1.0 + r)
    assert sim["wealth"][:, -1] == pytest.approx(w)
    band = one_year_band(hist)
    assert band[10] == pytest.approx(r) and band[90] == pytest.approx(r)


def test_tax_falls_on_nominal_gains_so_inflation_raises_it():
    # Equity earns exactly the inflation: no real gain, but a nominal one the tax still takes.
    hist = histories(_raw(eq=0.05, inflation=0.05), ("Germany",), haircut_share=0.0)
    sim = simulate(hist, start_value=100_000.0, monthly_contribution=0.0, years=20, paths=20)
    wealth = sim["wealth"][:, -1]
    taxed = after_tax(wealth, sim["basis"], Mix())
    nominal_gain_real = wealth - 100_000.0 / 1.05 ** 20
    assert sim["basis"] == pytest.approx(100_000.0 / 1.05 ** 20)
    assert wealth - taxed == pytest.approx(TAX_RATE * (0.7 * nominal_gain_real - 1000.0), rel=1e-6)


def test_projection_carries_the_tax_flags_and_the_stress_views():
    hist = histories(_raw(), ("Germany", "France"))
    out = project(hist, hist, start_value=10_000.0, monthly_contribution=100.0, years=12, goal=20_000.0,
                  paths=400)
    assert out["estimate"] is True and out["not_tax_advice"] is True and out["real_terms"] is True
    assert len(out["fan"]) == 13 and out["fan"][0]["p50"] == pytest.approx(10_000.0)
    assert out["after_tax"]["median"] <= out["terminal"]["median"]
    assert out["bad_start"]["first_years"] == 6
    assert out["goal"]["probability_after_tax"] <= out["goal"]["probability"]
    assert out["assumptions"]["homes"] == ["Germany", "France"]


def test_a_bond_share_narrows_the_band():
    rng = np.random.default_rng(1)
    raw = _raw()
    raw["eq_tr"] = rng.normal(0.07, 0.2, len(raw))
    hist = histories(raw, ("Germany", "France"))
    equity, mixed = one_year_band(hist), one_year_band(hist, Mix(0.5, 0.5, 0.0))
    assert mixed[90] - mixed[10] < equity[90] - equity[10]


def test_the_planner_band_is_centred_on_its_real_return():
    band = return_band(0.042, 0.16)
    assert band[10] < band[50] < band[90]
    assert band[50] == pytest.approx(0.042, abs=0.01)
    assert band[90] - band[10] == pytest.approx(2 * 1.2816 * 0.16, rel=0.15)


def _book(db, value: float = 10_000.0, name: str = "iShares Core MSCI World UCITS ETF") -> User:
    from app.foundation.models.entities import BrokerPosition, ConnectedAccount

    user = User(username="p", password_hash="x")
    db.add(user)
    db.commit()
    account = ConnectedAccount(user_id=user.id, source="scalable", name="Depot", account_type="depot")
    db.add(account)
    db.commit()
    db.add(BrokerPosition(user_id=user.id, connected_account_id=account.id, source="scalable", isin="IE00B4L5Y983",
                          name=name, quantity=Decimal(100), current_price=Decimal(str(value / 100)),
                          current_value=Decimal(str(value))))
    db.commit()
    return user


@pytest.fixture
def _models(monkeypatch):
    hist = tuple(histories(_raw(), ("Germany", "France")))
    monkeypatch.setattr("app.lab.long_history.plan_fan.load_histories", lambda: (hist, hist))
    monkeypatch.setattr("app.lab.long_history.load_histories", lambda: (hist, hist))
    monkeypatch.setattr("app.foundation.wealth_planner.book_volatility", lambda _db, _uid: (0.15, "test"))


def test_bands_are_stored_once_a_month_per_model(_models):
    db = _memory_db()
    user = _book(db)
    first = plan_bands.publish_due(db, today=date(2026, 10, 15))
    assert first["bands"] == 2
    assert plan_bands.publish_due(db, today=date(2026, 10, 16))["bands"] == 0
    assert plan_bands.publish_due(db, today=date(2026, 11, 2))["bands"] == 2
    rows = db.query(PlanBandForecast).filter(PlanBandForecast.forecast_day == date(2026, 10, 15)).all()
    assert {r.model for r in rows} == {"planner", "long_history"}
    for r in rows:
        assert r.user_id == user.id and r.end_day == date(2027, 10, 15)
        assert r.p10 < r.p50 < r.p90 or r.model == "long_history"
        assert r.value_p10 <= r.value_p50 <= r.value_p90
        assert r.start_value == pytest.approx(10_000.0)
    lh = next(r for r in rows if r.model == "long_history")
    assert lh.inputs_json["mix"]["equity"] == pytest.approx(1.0)


def test_without_the_data_file_only_the_planner_band_is_stored(monkeypatch):
    monkeypatch.setattr("app.lab.long_history.plan_fan.load_histories", lambda: None)
    monkeypatch.setattr("app.foundation.wealth_planner.book_volatility", lambda _db, _uid: (0.15, "test"))
    db = _memory_db()
    _book(db)
    out = plan_bands.publish_due(db, today=date(2026, 10, 15))
    assert out["bands"] == 1 and out["skipped"] == ["long_history: the JST file is not installed"]


def test_a_bond_fund_counts_as_bonds_in_the_mix():
    class P:
        def __init__(self, name, value):
            self.name, self.value, self.isin = name, Decimal(value), None

    mix = plan_bands.book_mix([P("iShares Core MSCI World UCITS ETF", 600),
                               P("iShares Euro Government Bond 15-30yr UCITS ETF", 300),
                               P("Xtrackers II EUR Overnight Rate Swap UCITS ETF", 100)])
    assert (mix.equity, mix.bond, mix.cash) == pytest.approx((0.6, 0.3, 0.1))


def _hicp(db, month: date, pct: float):
    from app.foundation.models.entities import MacroIndicator

    db.add(MacroIndicator(name="HICP_EA", value=Decimal(str(pct)), date=month, source="ecb"))
    db.commit()


def test_scores_wait_for_inflation_then_use_the_real_time_weighted_return(_models, monkeypatch):
    db = _memory_db()
    user = _book(db)
    day = date(2025, 10, 15)
    plan_bands.publish_due(db, today=day)
    end = day + timedelta(days=365)
    # The book doubled its unit value: +100 % nominal, far above any band.
    monkeypatch.setattr(service, "_unit_values", lambda _db, _uid, _start: [(day, 1.0), (end, 2.0)])

    waiting = plan_bands.score_due(db, today=end + timedelta(days=3))
    assert waiting["scored"] == 0 and "HICP" in waiting["waiting"][0]

    _hicp(db, end.replace(day=1), 2.0)
    done = plan_bands.score_due(db, today=end + timedelta(days=3))
    assert done["scored"] == 2
    scores = db.query(PlanBandScore).all()
    assert all(s.real_return == pytest.approx(2.0 / 1.02 - 1.0) and s.side == 1 and not s.inside for s in scores)
    assert plan_bands.score_due(db, today=end + timedelta(days=4))["scored"] == 0

    out = {m["model"]: m for m in plan_bands.summary(db, user.id)}
    assert out["planner"]["n_scored"] == 1 and out["planner"]["above"] == 1
    assert out["planner"]["n_independent"] == 1 and out["planner"]["coverage_high"] is not None


def test_a_year_without_a_unit_value_near_its_start_waits(_models, monkeypatch):
    db = _memory_db()
    _book(db)
    day = date(2025, 10, 15)
    plan_bands.publish_due(db, today=day)
    end = day + timedelta(days=365)
    _hicp(db, end.replace(day=1), 2.0)
    monkeypatch.setattr(service, "_unit_values", lambda _db, _uid, _start: [(day + timedelta(days=30), 1.0),
                                                                            (end, 1.1)])
    out = plan_bands.score_due(db, today=end)
    assert out["scored"] == 0 and "no unit value" in out["waiting"][0]


def test_overlapping_years_count_once_in_the_interval(_models, monkeypatch):
    db = _memory_db()
    user = _book(db)
    days = [date(2024, 10, 15), date(2024, 11, 15), date(2025, 10, 20)]
    for d in days:
        plan_bands.publish_due(db, today=d)
    for d in days:
        _hicp(db, (d + timedelta(days=365)).replace(day=1), 0.0)
    monkeypatch.setattr(service, "_unit_values",
                        lambda _db, _uid, start: [(start, 1.0), (start + timedelta(days=365), 1.03)])
    plan_bands.score_due(db, today=date(2025, 11, 20))
    plan_bands.score_due(db, today=date(2026, 10, 25))
    planner = next(m for m in plan_bands.summary(db, user.id) if m["model"] == "planner")
    assert planner["n_scored"] == 3 and planner["n_independent"] == 2


def test_plan_bands_endpoint_and_long_history_projection(_models):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _book(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        assert client.get("/api/trust/plan-bands").json()["models"] == []
        plan_bands.publish_due(db, today=date.today())
        body = client.get("/api/trust/plan-bands").json()
        proj = client.get("/api/quant/portfolio/projection/long-history?years=10&contribution_eur=100").json()
    finally:
        app.dependency_overrides.clear()
    assert [m["model"] for m in body["models"]] == ["planner", "long_history"]
    assert body["models"][0]["first_check"] == (date.today() + timedelta(days=365)).isoformat()
    assert proj["available"] is True and proj["not_tax_advice"] is True and proj["estimate"] is True
    assert proj["inputs"]["start_value_eur"] == pytest.approx(10_000.0) and len(proj["fan"]) == 11


def test_long_history_projection_without_the_file(monkeypatch):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    monkeypatch.setattr("app.lab.long_history.load_histories", lambda: None)
    db = _memory_db()
    user = User(username="n", password_hash="x")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/quant/portfolio/projection/long-history").json()
    finally:
        app.dependency_overrides.clear()
    assert body["available"] is False and body["not_tax_advice"] is True
