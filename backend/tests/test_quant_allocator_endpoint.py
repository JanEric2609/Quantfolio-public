"""GET /api/quant/portfolio/allocator and the benchmark-overlap field of /portfolio/risk."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from conftest import _memory_db

from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import DkbAccount, DkbPosition, User
from app.main import create_app


def _client(db):
    app = create_app()

    def _db():
        yield db

    user = db.query(User).first()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def _seed(db):
    user = User(username="u1", password_hash="x")
    db.add(user)
    db.flush()
    acct = DkbAccount(id=uuid4().hex, user_id=user.id, type="depot", iban="DE0012345", balance=Decimal("0"), currency="EUR")
    db.add(acct)
    db.flush()
    db.add(DkbPosition(
        id=uuid4().hex, account_id=acct.id, isin="IE00B4L5Y983", ticker="EUNL.DE", name="World",
        quantity=Decimal("100"), avg_buy_price=Decimal("80"), current_price=Decimal("90"), current_value=Decimal("9000"),
    ))
    db.commit()


def _prices(monkeypatch, young_days=None):
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2025-01-01", periods=300)
    out = {}
    for t, vol in (("EUNL.DE", 0.01), ("VWRA.L", 0.011)):
        px = 100 * np.cumprod(1 + rng.normal(0, vol, len(idx)))
        out[t] = {str(d.date()): float(p) for d, p in zip(idx, px)}
    if young_days:
        out["VWRA.L"] = dict(list(out["VWRA.L"].items())[-young_days:])
    monkeypatch.setattr("app.foundation.allocation.candidate_prices", lambda db, tickers: (
        {t: out[t] for t in tickers if t in out}, [t for t in tickers if t not in out]))
    monkeypatch.setattr("app.foundation.portfolio.isin_resolver._try_resolve_isin",
                        lambda isin, db=None: {"IE00BK5BQT80": "VWRA.L"}.get(isin))


def test_allocator_defaults_and_steers_new_money_to_unheld_candidate(monkeypatch):
    db = _memory_db()
    _seed(db)
    _prices(monkeypatch)
    r = _client(db).get("/api/quant/portfolio/allocator", params={"isins": "IE00B4L5Y983,IE00BK5BQT80,IE00BKM4GZ66"})
    body = r.json()
    assert r.status_code == 200 and body["available"]
    assert body["unresolved"] == ["IE00BKM4GZ66"]
    assert body["contribution_eur"] == 1000  # monthly_contribution_eur default
    eq = body["plans"]["equal"]
    assert eq["eur_this_month"]["VWRA.L"] > eq["eur_this_month"]["EUNL.DE"]
    assert sum(eq["eur_this_month"].values()) == pytest_approx(1000)


def pytest_approx(v):
    import pytest
    return pytest.approx(v)


def test_allocator_reports_the_limiting_young_candidate(monkeypatch):
    db = _memory_db()
    _seed(db)
    _prices(monkeypatch, young_days=120)
    body = _client(db).get("/api/quant/portfolio/allocator", params={"isins": "IE00B4L5Y983,IE00BK5BQT80"}).json()
    assert body["history"]["limited_by"] == "VWRA.L" and body["history"]["days"] == 120


def test_allocator_unavailable_when_history_is_too_short(monkeypatch):
    db = _memory_db()
    _seed(db)
    _prices(monkeypatch, young_days=20)
    body = _client(db).get("/api/quant/portfolio/allocator", params={"isins": "IE00B4L5Y983,IE00BK5BQT80"}).json()
    assert body["available"] is False and "60" in body["reason"]


def test_risk_reports_how_much_of_the_book_is_the_benchmark(monkeypatch):
    from app.foundation.portfolio_price_service import PortfolioPriceService

    db = _memory_db()
    _seed(db)
    idx = [str(d.date()) for d in pd.bdate_range("2025-01-01", periods=120)]
    rng = np.random.default_rng(2)
    px = {t: dict(zip(idx, 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))))) for t in ("EUNL.DE", "NVDA")}
    monkeypatch.setattr(PortfolioPriceService, "price_matrix", lambda self: (
        px, {"EUNL.DE": 9700.0, "NVDA": 300.0}, {}))
    monkeypatch.setattr("app.foundation.eur_prices.benchmark_returns_eur", lambda db, b=None: (
        "EUNL.DE", {d: r for d, r in zip(idx[1:], np.diff(list(px["EUNL.DE"].values())) / 100)}))
    body = _client(db).get("/api/quant/portfolio/risk").json()
    assert body["available"] and body["benchmark_overlap_pct"] == 97.0


def test_allocator_default_universe_is_world_plus_em_and_all_world_gets_a_note(monkeypatch):
    db = _memory_db()
    _seed(db)
    _prices(monkeypatch)
    monkeypatch.setattr("app.foundation.portfolio.isin_resolver._try_resolve_isin",
                        lambda isin, db=None: {"IE00BKM4GZ66": "VWRA.L", "IE00BK5BQT80": "VWRA.L"}.get(isin))
    client = _client(db)
    body = client.get("/api/quant/portfolio/allocator").json()
    assert [u["isin"] for u in body["universe"]] == ["IE00B4L5Y983", "IE00BKM4GZ66"] and "overlap_note" not in body
    body = client.get("/api/quant/portfolio/allocator", params={"isins": "IE00B4L5Y983,IE00BK5BQT80"}).json()
    assert "All-World" in body["overlap_note"]
