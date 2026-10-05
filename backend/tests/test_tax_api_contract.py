"""Tax API contract hardening (audit-fixes-2026-08 todo 24, audit §6.5).

Contract guarantees enforced here:

1. EVERY POST route under ``/api/tax`` responds with ``estimate: true`` and
   ``not_tax_advice: true`` (safety constraint, non-negotiable).
2. Disabled/ineligible payloads (estimation off or residency != DE) still
   carry the structural keys the frontend destructures — ``lines``, ``tax``
   and ``provenance`` — so ``pages/tax/OverviewTab.tsx`` cannot crash.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.settings import upsert_public_settings


def _client(settings_overrides: dict | None = None):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)

    overrides = {"tax_estimation_enabled": True, "tax_residency_country": "DE"}
    if settings_overrides:
        overrides.update(settings_overrides)
    upsert_public_settings(db, overrides)

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    return TestClient(app), maker, user.id


# ---------------------------------------------------------------------------
# Census: every POST route registered under /api/tax must be covered below.
# ---------------------------------------------------------------------------

POST_CASES = [
    (
        "/api/tax/events",
        {"event_type": "dividend", "event_date": "2025-06-02", "gross_eur": 100.0},
        False,
    ),
    (
        "/api/tax/lots",
        {
            "isin": "IE00B4BNMY34",
            "acquired_at": "2024-01-15",
            "quantity": 10.0,
            "cost_basis_eur": 1000.0,
        },
        False,
    ),
    ("/api/tax/lots/seed-from-activity", None, False),
    (
        "/api/tax/sales/fifo",
        {
            "isin": "IE00B4BNMY34",
            "sell_date": "2025-06-02",
            "sell_quantity": 1.0,
            "sell_price_per_unit_eur": 105.0,
        },
        True,
    ),
    (
        "/api/tax/vorabpauschale/estimate",
        {
            "isin": "IE00B4BNMY34",
            "fund_value_start_of_year_eur": 1000.0,
            "fund_value_end_of_year_eur": 1050.0,
            "distributions_during_year_eur": 0.0,
            "tax_year": 2024,
        },
        False,
    ),
    ("/api/tax/residency", {"country": "DE"}, False),
]


def test_post_route_census_is_complete():
    """Every POST route on the tax router must appear in POST_CASES."""
    from app.interface.api.tax import router as tax_router

    registered = sorted(
        route.path
        for route in tax_router.routes
        if "POST" in getattr(route, "methods", set())
    )
    assert registered == sorted(path for path, _, _ in POST_CASES)


def test_every_tax_post_response_carries_estimate_flags():
    for path, body, needs_lot in POST_CASES:
        client, maker, user_id = _client()
        if needs_lot:
            lot_resp = client.post(
                "/api/tax/lots",
                json={
                    "isin": "IE00B4BNMY34",
                    "acquired_at": "2024-01-15",
                    "quantity": 10.0,
                    "cost_basis_eur": 1000.0,
                },
            )
            assert lot_resp.status_code == 200, lot_resp.text
        resp = client.post(path) if body is None else client.post(path, json=body)
        assert resp.status_code == 200, f"{path}: {resp.text}"
        data = resp.json()
        assert data.get("estimate") is True, f"{path} missing estimate flag"
        assert data.get("not_tax_advice") is True, f"{path} missing not_tax_advice flag"


# ---------------------------------------------------------------------------
# Disabled/ineligible payload completeness.
# ---------------------------------------------------------------------------

DISABLED_MODES = [
    {"tax_estimation_enabled": False},
    {"tax_estimation_enabled": True, "tax_residency_country": "NL"},
]
DISABLED_MODE_IDS = ["estimation-disabled", "residency-not-DE"]

GATED_POSTS = [
    ("/api/tax/lots/seed-from-activity", None),
    (
        "/api/tax/sales/fifo",
        {
            "isin": "IE00B4BNMY34",
            "sell_date": "2025-06-02",
            "sell_quantity": 1.0,
            "sell_price_per_unit_eur": 105.0,
        },
    ),
    (
        "/api/tax/vorabpauschale/estimate",
        {
            "isin": "IE00B4BNMY34",
            "fund_value_start_of_year_eur": 1000.0,
            "fund_value_end_of_year_eur": 1050.0,
            "distributions_during_year_eur": 0.0,
            "tax_year": 2024,
        },
    ),
]


def test_disabled_post_payloads_carry_structural_keys():
    for overrides, mode_id in zip(DISABLED_MODES, DISABLED_MODE_IDS):
        for path, body in GATED_POSTS:
            client, _, _ = _client(settings_overrides=overrides)
            resp = client.post(path) if body is None else client.post(path, json=body)
            assert resp.status_code == 200, f"[{mode_id}] {path}: {resp.text}"
            data = resp.json()
            assert data.get("disabled") is True, f"[{mode_id}] {path}: {data}"
            assert "lines" in data, f"[{mode_id}] {path} missing 'lines' key"
            assert "tax" in data, f"[{mode_id}] {path} missing 'tax' key"
            assert "provenance" in data, f"[{mode_id}] {path} missing 'provenance' key"
            assert data["estimate"] is True
            assert data["not_tax_advice"] is True


def test_disabled_overview_payload_cannot_crash_frontend_destructuring():
    """OverviewTab.tsx reads o.provenance.events_by_source — provenance must exist."""
    client, _, _ = _client(
        settings_overrides={"tax_estimation_enabled": False}
    )
    resp = client.get("/api/tax/overview?year=2025")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("disabled") is True
    assert isinstance(data["lines"], list)
    assert isinstance(data["tax"], dict)
    assert isinstance(data["provenance"], dict)
