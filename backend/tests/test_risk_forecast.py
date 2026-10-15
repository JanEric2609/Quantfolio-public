"""D3 (plan 2026-10-14): risk forecasts stored before the fact and scored by e-backtests."""
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    PaperHolding,
    PaperPortfolio,
    RiskForecastEntry,
    RiskForecastScore,
    User,
)
from app.lab.risk_forecast import backtest, run_forecasts, summary
from app.lab.risk_forecast.models import MIN_OBS, forecast


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_iid_forecast_matches_the_true_scale():
    r = np.random.default_rng(7).normal(0.0, 0.01, 750)
    f1, f21 = forecast(r, 1), forecast(r, 21)
    assert f1 is not None and f21 is not None
    assert f1.sigma == pytest.approx(0.01, rel=0.15)
    assert f21.sigma == pytest.approx(0.01 * 21 ** 0.5, rel=0.2)
    assert 0 < f1.var95 <= f1.var975 <= f1.es975
    assert forecast(r[: MIN_OBS - 1], 1) is None


def test_garch_forecast_follows_the_current_volatility():
    rng = np.random.default_rng(3)
    s2, out = 1e-4, []
    for _ in range(900):
        x = np.sqrt(s2) * rng.standard_t(5) / np.sqrt(5 / 3)
        out.append(x)
        s2 = 2e-6 + 0.1 * x * x + 0.88 * s2
    f = forecast(np.array(out), 1)
    assert f is not None and f.sigma == pytest.approx(np.sqrt(s2), rel=0.35)


def test_e_values_have_mean_one_under_correct_forecasts():
    z = np.random.default_rng(1).normal(size=200_000)
    var95, var975 = 1.6449, 1.9600
    es975 = 2.3378  # E[Z | Z > 1.96] for a standard normal
    assert np.mean([backtest.var_e(x, var95) for x in z[:50_000]]) == pytest.approx(1.0, abs=0.05)
    assert np.mean([backtest.es_e(x, var975, es975) for x in z]) == pytest.approx(1.0, abs=0.08)


def test_e_process_grows_only_when_bands_are_too_narrow():
    rng = np.random.default_rng(5)
    losses = rng.normal(size=1000)
    right = backtest.e_process(backtest.var_e(x, 1.6449) for x in losses)
    narrow = backtest.e_process(backtest.var_e(x, 1.0) for x in losses)  # breaches 16 % of days, not 5 %
    assert max(right) < 20
    assert narrow[-1] > 1e3


def _closes(n_days: int, end: date, vol: float, seed: int) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    days, d = [], end
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    levels = 100.0 * np.cumprod(1.0 + rng.normal(0.0, vol, len(days)))
    return {day.isoformat(): float(v) for day, v in zip(days, levels)}


def test_run_stores_forecasts_then_scores_them(monkeypatch):
    end = date(2026, 10, 14)
    series = {"EUNL.DE": _closes(800, end, 0.01, 1), "AAA": _closes(800, end, 0.02, 2)}
    monkeypatch.setattr("app.foundation.eur_prices.eur_closes",
                        lambda _db, sym, days=0, allow_live=True, rate_cache=None: series.get(sym.upper(), {}))
    db = _memory_db()
    user = User(username="r", password_hash="x")
    db.add(user)
    db.flush()
    p = PaperPortfolio(user_id=user.id, name="Mandate A", initial_cash=Decimal("1000"))
    db.add(p)
    db.flush()
    db.add(PaperHolding(portfolio_id=p.id, ticker="AAA", name="AAA", quantity=Decimal("10"),
                        avg_buy_price=Decimal("100")))
    db.commit()

    # The first run sees closes up to ten trading days before the end.
    cut = sorted(series["EUNL.DE"])[-11]
    full = dict(series)
    series.update({k: {d: v for d, v in s.items() if d <= cut} for k, s in full.items()})
    first = run_forecasts(db, today=date.fromisoformat(cut))
    assert first["forecasts"] == 4 and first["scored"] == 0  # benchmark and sleeve, 1 and 21 days
    sleeve = db.query(RiskForecastEntry).filter(RiskForecastEntry.target == f"paper:{p.id}",
                                                RiskForecastEntry.horizon_days == 1).one()
    assert set(sleeve.weights_json) == {"AAA", "cash"}
    assert sleeve.sigma > db.query(RiskForecastEntry).filter(
        RiskForecastEntry.target == "benchmark:EUNL.DE", RiskForecastEntry.horizon_days == 1).one().sigma * 0.5

    # Ten trading days later the 1-day forecasts are due; the 21-day ones are not.
    series.update(full)
    second = run_forecasts(db, today=end)
    assert second["scored"] == 2
    scores = db.query(RiskForecastScore).all()
    assert all(s.loss == pytest.approx(-s.realized_return) for s in scores)
    again = run_forecasts(db, today=end)
    assert again["forecasts"] == 0 and again["scored"] == 0

    out = {t["target"]: t for t in summary(db, user.id)}
    h1 = next(h for h in out[f"paper:{p.id}"]["horizons"] if h["horizon_days"] == 1)
    assert h1["n_scored"] == 1 and h1["forecast_day"] == end.isoformat()


def test_risk_bands_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = User(username="e", password_hash="x")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/trust/risk-bands").json()
    finally:
        app.dependency_overrides.clear()
    assert body["targets"] == [] and "before the fact" in body["note"]


def test_an_unpriced_holding_moves_the_target_by_zero_not_by_the_priced_lines():
    from app.lab.risk_forecast.service import _shares

    assert _shares({"AAA": 50.0}, 50.0) == {"AAA": 0.5}
    assert _shares({"AAA": 50.0, "cash": 50.0}, 0.0) == {"AAA": 0.5, "cash": 0.5}
    assert _shares({}, 10.0) == {}


def test_a_cash_only_book_still_gets_a_target(monkeypatch):
    from app.lab.risk_forecast.service import _Closes, book_targets

    db = _memory_db()
    user = User(username="c", password_hash="x")
    db.add(user)
    db.commit()
    monkeypatch.setattr("app.foundation.live_positions.combined_positions", lambda db, uid, **kw: [])
    monkeypatch.setattr("app.foundation.live_positions.synced_cash", lambda db, uid: (Decimal("500"), None))
    targets = book_targets(db, _Closes(db))
    assert [(t.key, t.weights) for t in targets] == [(f"book:{user.id}", {"cash": 1.0})]


def test_the_calibration_set_is_centred_on_the_fitting_window_only(monkeypatch):
    # A drift that starts only in the last third must not change what the
    # first two thirds' models are fitted on.
    from app.lab.risk_forecast import models

    seen: list[np.ndarray] = []
    real = models.fit_garch_t
    monkeypatch.setattr(models, "fit_garch_t", lambda r: seen.append(np.array(r)) or real(r))
    rng = np.random.default_rng(4)
    base = rng.normal(0.0, 0.01, MIN_OBS + 100)
    drifted = base.copy()
    drifted[(2 * len(base)) // 3:] += 0.002
    forecast(base, 1)
    first = seen[0]
    seen.clear()
    forecast(drifted, 1)
    np.testing.assert_array_equal(seen[0], first)
