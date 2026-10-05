from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation import quant_metrics as qm
from app.lab.quant_lab.strategy_backtest import STRATEGIES, run_strategy_backtest, strategy_catalog


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _walk(n: int, seed: int, drift: float = 0.0003, vol: float = 0.01) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    days, d = [], date(2026, 10, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    prices = 100 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))
    return {x.isoformat(): float(p) for x, p in zip(days, prices)}


def _run(db, series: dict[str, dict[str, float]], **kw):
    def fake(_db, symbol, days=730, **_k):
        return series.get(symbol, {})

    with patch("app.foundation.eur_prices.eur_closes", side_effect=fake), \
            patch("app.foundation.eur_prices.benchmark_ticker", return_value="EUNL.DE"), \
            patch("app.foundation.settings.get_risk_free_rate", return_value=0.02):
        return run_strategy_backtest(db, kw.pop("ticker", "AAA"), kw.pop("strategy", "trend"), **kw)


def test_three_lines_share_one_window_after_the_warmup():
    db = _memory_db()
    out = _run(db, {"AAA": _walk(1500, 1), "EUNL.DE": _walk(1500, 2)})
    assert out["available"] and out["estimate"] is True
    assert out["warmup_days"] == 250  # the longest look-back of the trend grid
    first = out["series"][0]
    assert first["strategy"] == first["buy_hold"] == first["benchmark"] == 100.0
    assert len(out["series"]) == 1500 - 251
    assert out["benchmark"] == "EUNL.DE"
    assert out["stats"]["buy_hold"]["taxes_eur"] == 0.0
    assert out["evidence"]["pbo"] is not None and 0.0 <= out["evidence"]["pbo"] <= 1.0


def test_signal_is_traded_at_the_next_close():
    # Flat, then a jump: the 2-day trend signal turns on at the jump's close
    # and the buy must be booked one close later, at the price after the jump.
    days = list(_walk(600, 3, drift=0.0, vol=0.0).keys())
    prices = {d: (100.0 if i < 400 else 120.0 + 0.01 * i) for i, d in enumerate(days)}
    db = _memory_db()
    out = _run(db, {"AAA": prices, "EUNL.DE": prices}, params={"window": 2})
    buys = [t for t in out["trades"] if t["side"] == "buy"]
    assert buys and buys[0]["date"] == days[401]
    assert buys[0]["price_eur"] == pytest.approx(120.0 + 0.01 * 401, abs=1e-6)


def test_buy_and_hold_is_the_reference_not_a_test():
    db = _memory_db()
    out = _run(db, {"AAA": _walk(1500, 4), "EUNL.DE": _walk(1500, 5)}, strategy="buy_hold")
    assert out["evidence"]["verdict"] == "reference"
    assert out["evidence"]["dsr"] is None
    assert out["series"][-1]["strategy"] == pytest.approx(out["series"][-1]["buy_hold"])


def test_noise_is_not_evidence_and_every_run_is_a_trial():
    db = _memory_db()
    data = {"AAA": _walk(1800, 6), "EUNL.DE": _walk(1800, 7)}
    out = _run(db, data, strategy="sma_cross")
    assert out["evidence"]["verdict"] in {"insufficient_evidence", "too_short"}
    assert out["evidence"]["verdict"] != "evidence"
    assert qm.resolve_n_trials(db) == 1
    _run(db, data, strategy="sma_cross")  # the same run again is the same trial
    assert qm.resolve_n_trials(db) == 1
    again = _run(db, data, strategy="sma_cross", params={"fast": 20, "slow": 100})
    assert again["evidence"]["n_trials"] == 2
    assert again["evidence"]["n_trials_effective"] == 1  # one search: AAA x sma_cross


def test_short_history_and_bad_params_are_refused():
    db = _memory_db()
    out = _run(db, {"AAA": _walk(300, 8), "EUNL.DE": _walk(300, 9)})
    assert out["available"] is False and "warm up" in out["reason"]
    with pytest.raises(ValueError):
        _run(db, {"AAA": _walk(1500, 1)}, params={"nope": 3})
    with pytest.raises(ValueError):
        _run(db, {"AAA": _walk(1500, 1)}, strategy="sma_cross", params={"fast": 200, "slow": 50})


def test_tax_is_charged_on_sales_and_costs_on_trades():
    db = _memory_db()
    data = {"AAA": _walk(1500, 10, drift=0.0008), "EUNL.DE": _walk(1500, 11)}
    with_tax = _run(db, data, strategy="sma_cross", params={"fast": 10, "slow": 50}, fund_class="aktien")
    no_tax = _run(db, data, strategy="sma_cross", params={"fast": 10, "slow": 50}, apply_tax=False)
    s = with_tax["stats"]["strategy"]
    assert s["trades"] >= 2 and s["costs_eur"] > 0
    assert s["taxes_eur"] >= 0
    assert no_tax["stats"]["strategy"]["taxes_eur"] == 0
    assert with_tax["series"][-1]["strategy"] <= no_tax["series"][-1]["strategy"] + 1e-9


def test_catalog_lists_every_strategy_with_its_grid():
    cat = {c["key"]: c for c in strategy_catalog()}
    assert set(cat) == set(STRATEGIES)
    assert cat["buy_hold"]["grid_size"] == 0
    assert cat["trend"]["defaults"] == {"window": 200}


def _api_client():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import User
    from app.main import create_app

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    user_id = user.id
    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user_id)
    return TestClient(app), maker


def test_run_endpoint_records_the_run_and_refuses_unknown_rules():
    from app.foundation.models.entities import BacktestResult

    client, maker = _api_client()
    assert {s["key"] for s in client.get("/api/quant/backtest/strategies").json()} == set(STRATEGIES)
    assert client.post("/api/quant/backtest/run", json={"ticker": "AAA", "strategy": "astrology"}).status_code == 422
    data = {"AAA": _walk(1500, 12), "EUNL.DE": _walk(1500, 13)}
    with patch("app.foundation.eur_prices.eur_closes", side_effect=lambda _db, s, days=730, **_k: data.get(s, {})), \
            patch("app.foundation.eur_prices.benchmark_ticker", return_value="EUNL.DE"):
        body = client.post("/api/quant/backtest/run", json={"ticker": "aaa", "strategy": "momentum"}).json()
    assert body["available"] and body["ticker"] == "AAA"
    assert body["evidence"]["n_trials"] == 1
    assert maker().query(BacktestResult).count() == 1
