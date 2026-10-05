"""AlphaCrafter miner basket defaults.

The four-ETF UCITS basket of audit-fixes-2026-08 todo 21 could never validate
a factor (a four-point cross-sectional IC); since 2026-09-28 the default is
the built-in Euro Stoxx 50 + S&P 100 universe, and a stored basket under
MIN_MINER_UNIVERSE names is ignored.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import AppSetting, User
from app.lab.alphacrafter.orchestrator import DEFAULT_UNIVERSE
from app.lab.alphacrafter.universe import MIN_MINER_UNIVERSE, MINER_UNIVERSE, resolve_miner_universe
from app.foundation.auth import current_user
from app.foundation.settings import DEFAULT_PUBLIC_SETTINGS

OLD_UCITS_BASKET = "IWDA.L,VWRL.L,EIMI.L,SXR8.DE"


def test_default_settings_basket_is_empty_so_the_built_in_universe_applies():
    assert DEFAULT_PUBLIC_SETTINGS["alphacrafter_index_basket"] == ""


def test_orchestrator_fallback_is_the_150_name_cross_section():
    assert DEFAULT_UNIVERSE == MINER_UNIVERSE
    assert len(MINER_UNIVERSE) == 150
    assert len(set(MINER_UNIVERSE)) == 150
    assert "SAP.DE" in MINER_UNIVERSE and "AAPL" in MINER_UNIVERSE


def test_a_basket_too_small_for_a_cross_section_is_replaced():
    # Prod still stores the seeded four-ETF basket.
    assert resolve_miner_universe({"alphacrafter_index_basket": OLD_UCITS_BASKET}) == MINER_UNIVERSE
    assert resolve_miner_universe({}) == MINER_UNIVERSE
    wide = [f"S{i}" for i in range(MIN_MINER_UNIVERSE)]
    assert resolve_miner_universe({"alphacrafter_index_basket": ",".join(wide)}) == wide
    # An explicit universe is the caller's choice (tests, API runs).
    assert resolve_miner_universe({}, universe=["A", "B", "A"]) == ["A", "B"]


def _client_with_stored_basket(value: str):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash", role="admin")
    db.add(user)
    if value is not None:
        db.add(AppSetting(key="alphacrafter_index_basket", value_json=f'"{value}"'))
    db.commit()
    db.refresh(user)

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    return TestClient(app), maker


def test_diagnostics_reports_the_built_in_universe_when_basket_empty():
    client, _maker = _client_with_stored_basket("")
    response = client.post("/api/alphacrafter/diagnostics")
    assert response.status_code == 200, response.text
    steps = response.json()["steps"]
    basket_step = next(s for s in steps if s["key"] == "basket")
    assert basket_step["status"] == "passed"
    assert "Euro Stoxx 50 + S&P 100" in basket_step["message"], basket_step["message"]


def test_diagnostics_warns_that_a_small_basket_is_ignored():
    client, _maker = _client_with_stored_basket(OLD_UCITS_BASKET)
    response = client.post("/api/alphacrafter/diagnostics")
    assert response.status_code == 200
    steps = response.json()["steps"]
    basket_step = next(s for s in steps if s["key"] == "basket")
    assert basket_step["status"] == "warning"
    assert "too few for a cross-sectional IC" in basket_step["message"]


def test_a_wide_stored_basket_wins_over_the_default():
    client, maker = _client_with_stored_basket(",".join(MINER_UNIVERSE[:40]))
    response = client.post("/api/alphacrafter/diagnostics")
    assert response.status_code == 200
    steps = response.json()["steps"]
    basket_step = next(s for s in steps if s["key"] == "basket")
    assert basket_step["status"] == "passed"
    assert "40 symbol(s)" in basket_step["message"]
