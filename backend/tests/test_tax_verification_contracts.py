"""Golden-response parity + route-introspection tests for the tax router
(and route-shape introspection for the remaining verification risk routes).

Plan: .omo/plans/audit-fixes-2026-08.md todo 33 (typing campaign II), the same
harness pattern todo 32 established for the budget router.

The verification half of this harness (confidence, attribution, compare,
report, summary, audit-logs, alerts) was removed in 2026-10 together with those
endpoints; the surviving verification routes (risk, stress) are covered by
test_verification_risk.py / test_verification_alerts.py, and the trust routes
by test_trust_verdict.py.

1. CAPTURE (run BEFORE any response_model existed):
       cd backend && TAXVER_GOLDEN_CAPTURE=1 .venv/bin/pytest \
           tests/test_tax_verification_contracts.py::test_capture_golden_responses -q
   Drives every /api/tax route via TestClient against seeded in-memory SQLite
   (happy + empty/disabled variants) and writes the live JSON payloads to
   ``backend/tests/fixtures/tax_verification_golden_responses.json``.
   Those fixtures are the serialization-by-luck contract that response models
   must preserve shape-wise.

2. PARITY (default mode): replays the identical request sequence on identically
   seeded databases and compares captured-vs-live key-by-key. Adding a
   ``response_model=`` FILTERS fields — any lost/renamed/retyped key fails here.
   The regression fix is widening the model, never shrinking the payload.

3. INTROSPECTION: every tax route must declare a response_model; so must every
   verification route.

Determinism strategy:
- All seeded rows use fixed non-UUID-shaped ids, so values compare strictly.
- ``date.today()`` is frozen to FROZEN_TODAY in every module the request path
  touches (api/tax, services/tax_cockpit).
- Server-generated volatile leaf values (uuid4 primary keys, wall-clock
  timestamps) are masked to sentinels before comparison; every other value —
  including all foreign-key ids set by seeding — compares strictly, as does
  JSON type (bool vs int drift is caught).

Scope boundary (plan §33, audit §13 item 14): quant routers are consciously OUT
of this typing campaign; quant follow-up is tracked separately.
"""

import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import _memory_db
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import (
    TaxLedgerEvent,
    TaxLot,
    TaxResidencyPeriod,
    User,
)
from app.foundation.auth import current_user
from app.foundation.settings import upsert_public_settings

FROZEN_TODAY = date(2026, 8, 15)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tax_verification_golden_responses.json"


class _FakeDate(date):
    """Drop-in for ``date`` whose today() is pinned; constructors still work."""

    @classmethod
    def today(cls) -> date:
        return FROZEN_TODAY


@pytest.fixture
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    for module_name in (
        "app.interface.api.tax",
        "app.foundation.tax_cockpit",
        "app.foundation.tax_allowances",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "date", _FakeDate)


# ---------------------------------------------------------------------------
# Seeding — tax worlds
# ---------------------------------------------------------------------------


def _seed_user(db, user_id: str, username: str) -> User:
    user = User(id=user_id, username=username, password_hash="h")
    db.add(user)
    db.commit()
    return user


def _seed_tax_settings(db, overrides: dict | None = None) -> None:
    settings = {
        "tax_estimation_enabled": True,
        "tax_residency_country": "DE",
        "church_tax": "none",
        "freistellungsauftrag_amount": 1000.0,
    }
    if overrides:
        settings.update(overrides)
    upsert_public_settings(db, settings)
    db.commit()


def _seed_tax_user(db) -> User:
    user = _seed_user(db, "tax-user", "taxparity")
    _seed_tax_settings(db)
    return user


def _seed_tax_world(db) -> User:
    """Representative German tax world: lots + a full 2025 ledger year."""
    user = _seed_tax_user(db)
    db.add_all(
        [
            TaxLot(
                id="tax-lot-vwce",
                user_id=user.id,
                isin="IE00B4BNMY34",
                symbol="VWCE.DE",
                name="Vanguard FTSE All-World",
                fund_class="aktien",
                teilfreistellung_pct=Decimal("30"),
                acquired_at=date(2024, 1, 15),
                quantity_initial=Decimal("10"),
                quantity_remaining=Decimal("10"),
                cost_basis_eur=Decimal("1000"),
                fees_eur=Decimal("1.5"),
                source="manual",
            ),
            TaxLot(
                id="tax-lot-cash-fund",
                user_id=user.id,
                isin="DE000TEST02",
                name="Geldmarkt Fonds",
                fund_class="other",
                teilfreistellung_pct=Decimal("0"),
                acquired_at=date(2023, 6, 1),
                quantity_initial=Decimal("5"),
                quantity_remaining=Decimal("5"),
                cost_basis_eur=Decimal("500"),
                source="manual",
            ),
        ]
    )
    db.commit()
    db.add_all(
        [
            TaxLedgerEvent(
                id="tax-ev-div",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 6, 2),
                event_type="dividend",
                isin="IE00B4BNMY34",
                name="Vanguard FTSE All-World",
                fund_class="aktien",
                teilfreistellung_pct=Decimal("30"),
                gross_eur=Decimal("100"),
                withheld_eur=Decimal("2.5"),
                foreign_wht_eur=Decimal("15"),
                foreign_country="US",
                confidence="estimate",
                source="manual",
            ),
            TaxLedgerEvent(
                id="tax-ev-int",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 7, 1),
                event_type="interest",
                name="Tagesgeld",
                gross_eur=Decimal("50"),
                confidence="estimate",
                source="manual",
            ),
            TaxLedgerEvent(
                id="tax-ev-sale-gain",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 8, 15),
                event_type="sale",
                isin="IE00B4BNMY34",
                fund_class="aktien",
                teilfreistellung_pct=Decimal("30"),
                realised_gain_eur=Decimal("200"),
                bucket="aktien",
                confidence="estimate",
                source="manual",
            ),
            TaxLedgerEvent(
                id="tax-ev-sale-loss",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 9, 10),
                event_type="sale",
                isin="DE000TEST02",
                fund_class="other",
                realised_gain_eur=Decimal("-50"),
                bucket="sonstige",
                confidence="estimate",
                source="manual",
            ),
            TaxLedgerEvent(
                id="tax-ev-vorab",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 12, 31),
                event_type="vorabpauschale",
                isin="IE00B4BNMY34",
                fund_class="aktien",
                teilfreistellung_pct=Decimal("30"),
                gross_eur=Decimal("20"),
                confidence="estimate",
                source="manual",
            ),
            TaxLedgerEvent(
                id="tax-ev-wht",
                user_id=user.id,
                tax_year=2025,
                event_date=date(2025, 6, 2),
                event_type="withholding",
                name="Abgeltungsteuer Vorauszahlung",
                withheld_eur=Decimal("10"),
                confidence="estimate",
                source="manual",
            ),
        ]
    )
    db.commit()
    return user


def _seed_tax_disabled(db) -> User:
    user = _seed_user(db, "tax-user-off", "taxparityoff")
    _seed_tax_settings(db, {"tax_estimation_enabled": False})
    return user


def _seed_tax_nl(db) -> User:
    user = _seed_user(db, "tax-user-nl", "taxparitynl")
    _seed_tax_settings(db, {"tax_residency_country": "NL"})
    db.add(
        TaxResidencyPeriod(
            id="tax-res-nl",
            user_id=user.id,
            country="NL",
            valid_from=date(2026, 1, 1),
            valid_to=None,
        )
    )
    db.commit()
    return user


# ---------------------------------------------------------------------------
# Request driving
# ---------------------------------------------------------------------------


def _drive(client: TestClient, method: str, url: str, *, json_body: Any = None) -> Any:
    response = getattr(client, method)(url, json=json_body) if json_body is not None else getattr(client, method)(url)
    assert response.status_code == 200, f"{method.upper()} {url} -> {response.status_code}: {response.text}"
    return response.json()


_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?| \d{2}:\d{2} UTC)")


def _scrub(value: Any) -> Any:
    """Scrub wall-clock timestamps embedded anywhere inside report text."""
    if isinstance(value, str):
        return _TS_RE.sub("<TS>", value)
    return value


CASES: dict[str, Any] = {}


def case(name: str, seed=_seed_tax_world):
    def wrap(fn):
        CASES[name] = (seed, fn)
        return fn

    return wrap


# --- tax: settings & reference data ---


@case("tax_settings_default", seed=lambda db: _seed_user(db, "tax-user", "taxparity"))
def _c_tax_settings_default(client, user):
    return {"GET /settings (defaults)": _drive(client, "get", "/api/tax/settings")}


@case("tax_settings_seeded", seed=_seed_tax_user)
def _c_tax_settings_seeded(client, user):
    return {"GET /settings (seeded)": _drive(client, "get", "/api/tax/settings")}


@case("tax_settings_put", seed=_seed_tax_user)
def _c_tax_settings_put(client, user):
    payload = {
        "church_tax": "9",
        "freistellungsauftrag_amount": 2000.0,
        "freistellungsauftrag_used": 250.0,
        "tax_spouse_allowance": True,
    }
    return {"PUT /settings": _drive(client, "put", "/api/tax/settings", json_body=payload)}


@case("tax_basiszins", seed=_seed_tax_user)
def _c_tax_basiszins(client, user):
    return {"GET /basiszins": _drive(client, "get", "/api/tax/basiszins")}


# --- tax: reports & overview ---


@case("tax_kap_filled", seed=_seed_tax_world)
def _c_tax_kap_filled(client, user):
    return {"GET /reports/kap (filled)": _drive(client, "get", "/api/tax/reports/kap?year=2025")}


@case("tax_kap_disabled", seed=_seed_tax_disabled)
def _c_tax_kap_disabled(client, user):
    return {"GET /reports/kap (disabled)": _drive(client, "get", "/api/tax/reports/kap?year=2025")}


@case("tax_overview_de_filled", seed=_seed_tax_world)
def _c_tax_overview_de(client, user):
    return {"GET /overview (DE filled)": _drive(client, "get", "/api/tax/overview?year=2025&jurisdiction=DE")}


@case("tax_overview_auto_disabled", seed=_seed_tax_disabled)
def _c_tax_overview_disabled(client, user):
    return {"GET /overview (auto, disabled)": _drive(client, "get", "/api/tax/overview?year=2025")}


@case("tax_overview_nl", seed=_seed_tax_nl)
def _c_tax_overview_nl(client, user):
    return {"GET /overview (NL)": _drive(client, "get", "/api/tax/overview?year=2026&jurisdiction=NL")}


@case("tax_box3_nl", seed=_seed_tax_nl)
def _c_tax_box3_nl(client, user):
    url = "/api/tax/box3?year=2026&bank_deposits=25000&investments=60000&debts=10000"
    return {"GET /box3 (NL)": _drive(client, "get", url)}


@case("tax_box3_zero_wealth", seed=_seed_tax_nl)
def _c_tax_box3_zero(client, user):
    return {"GET /box3 (zero wealth)": _drive(client, "get", "/api/tax/box3?year=2026")}


@case("tax_box3_disabled", seed=_seed_tax_user)
def _c_tax_box3_disabled(client, user):
    return {"GET /box3 (DE residency, disabled)": _drive(client, "get", "/api/tax/box3?year=2026")}


# --- tax: events ---


@case("tax_events_list", seed=_seed_tax_world)
def _c_tax_events_list(client, user):
    return {"GET /events": _drive(client, "get", "/api/tax/events?year=2025")}


@case("tax_events_create", seed=_seed_tax_user)
def _c_tax_events_create(client, user):
    payload = {
        "event_type": "dividend",
        "event_date": "2025-06-02",
        "isin": "IE00B4BNMY34",
        "fund_class": "aktien",
        "teilfreistellung_pct": 30.0,
        "gross_eur": 75.0,
        "withheld_eur": 2.0,
        "foreign_wht_eur": 10.0,
        "foreign_country": "US",
    }
    return {"POST /events": _drive(client, "post", "/api/tax/events", json_body=payload)}


@case("tax_events_delete", seed=_seed_tax_world)
def _c_tax_events_delete(client, user):
    created = _drive(
        client,
        "post",
        "/api/tax/events",
        json_body={"event_type": "fee", "event_date": "2025-03-01", "gross_eur": 4.5},
    )
    deleted = _drive(client, "delete", f"/api/tax/events/{created['id']}")
    return {"POST /events (for delete)": created, "DELETE /events/{id}": deleted}


# --- tax: lots ---


@case("tax_lots_list", seed=_seed_tax_world)
def _c_tax_lots_list(client, user):
    return {"GET /lots": _drive(client, "get", "/api/tax/lots")}


@case("tax_lots_create", seed=_seed_tax_user)
def _c_tax_lots_create(client, user):
    payload = {
        "isin": "US5949181045",
        "symbol": "MSFT",
        "name": "Microsoft Corp",
        "fund_class": "aktien",
        "teilfreistellung_pct": 30.0,
        "acquired_at": "2025-02-10",
        "quantity": 3.0,
        "cost_basis_eur": 600.0,
        "fees_eur": 0.99,
    }
    return {"POST /lots": _drive(client, "post", "/api/tax/lots", json_body=payload)}


@case("tax_lots_delete", seed=_seed_tax_world)
def _c_tax_lots_delete(client, user):
    created = _drive(
        client,
        "post",
        "/api/tax/lots",
        json_body={
            "isin": "US0378331005",
            "acquired_at": "2025-01-05",
            "quantity": 1.0,
            "cost_basis_eur": 180.0,
        },
    )
    deleted = _drive(client, "delete", f"/api/tax/lots/{created['id']}")
    return {"POST /lots (for delete)": created, "DELETE /lots/{id}": deleted}


@case("tax_lots_seed_enabled", seed=_seed_tax_user)
def _c_tax_lots_seed_enabled(client, user):
    return {"POST /lots/seed-from-activity (enabled)": _drive(client, "post", "/api/tax/lots/seed-from-activity")}


@case("tax_lots_seed_disabled", seed=_seed_tax_disabled)
def _c_tax_lots_seed_disabled(client, user):
    return {"POST /lots/seed-from-activity (disabled)": _drive(client, "post", "/api/tax/lots/seed-from-activity")}


# --- tax: fifo sales & vorabpauschale ---


@case("tax_sale_fifo_enabled", seed=_seed_tax_world)
def _c_tax_sale_fifo(client, user):
    payload = {
        "isin": "IE00B4BNMY34",
        "sell_date": "2025-10-01",
        "sell_quantity": 2.0,
        "sell_price_per_unit_eur": 120.0,
        "sell_fees_eur": 1.0,
        "fund_class": "aktien",
        "bucket": "aktien",
    }
    return {"POST /sales/fifo (enabled)": _drive(client, "post", "/api/tax/sales/fifo", json_body=payload)}


@case("tax_sale_fifo_disabled", seed=_seed_tax_disabled)
def _c_tax_sale_fifo_disabled(client, user):
    payload = {
        "isin": "IE00B4BNMY34",
        "sell_date": "2025-10-01",
        "sell_quantity": 2.0,
        "sell_price_per_unit_eur": 120.0,
    }
    return {"POST /sales/fifo (disabled)": _drive(client, "post", "/api/tax/sales/fifo", json_body=payload)}


@case("tax_vorab_enabled", seed=_seed_tax_user)
def _c_tax_vorab(client, user):
    payload = {
        "isin": "IE00B4BNMY34",
        "fund_value_start_of_year_eur": 1000.0,
        "fund_value_end_of_year_eur": 1050.0,
        "distributions_during_year_eur": 0.0,
        "tax_year": 2025,
        "fund_class": "aktien",
    }
    return {"POST /vorabpauschale/estimate (enabled)": _drive(client, "post", "/api/tax/vorabpauschale/estimate", json_body=payload)}


@case("tax_vorab_disabled", seed=_seed_tax_disabled)
def _c_tax_vorab_disabled(client, user):
    payload = {
        "isin": "IE00B4BNMY34",
        "fund_value_start_of_year_eur": 1000.0,
        "fund_value_end_of_year_eur": 1050.0,
        "tax_year": 2025,
    }
    return {"POST /vorabpauschale/estimate (disabled)": _drive(client, "post", "/api/tax/vorabpauschale/estimate", json_body=payload)}


# --- tax: jurisdictions & residency ---


@case("tax_jurisdictions", seed=_seed_tax_user)
def _c_tax_jurisdictions(client, user):
    return {"GET /jurisdictions": _drive(client, "get", "/api/tax/jurisdictions")}


@case("tax_residency_post", seed=_seed_tax_user)
def _c_tax_residency_post(client, user):
    payload = {"country": "DE", "valid_from": "2025-01-01"}
    return {"POST /residency": _drive(client, "post", "/api/tax/residency", json_body=payload)}


@case("tax_residency_list_empty", seed=_seed_tax_user)
def _c_tax_residency_empty(client, user):
    return {"GET /residency (empty)": _drive(client, "get", "/api/tax/residency")}


@case("tax_residency_list_filled", seed=_seed_tax_nl)
def _c_tax_residency_filled(client, user):
    return {"GET /residency (filled)": _drive(client, "get", "/api/tax/residency")}






































@case("tax_harvest_disabled", seed=_seed_tax_world)
def _c_tax_harvest_disabled(client, user):
    return {"GET /harvest (no NV certificate)": _drive(client, "get", "/api/tax/harvest?year=2026")}


@case("tax_harvest_nv", seed=_seed_tax_world)
def _c_tax_harvest_nv(client, user):
    payload = {
        "tax_nv_certificate": True,
        "tax_nv_valid_until": "2026-12-31",
        "tax_other_income_eur": 0.0,
        "tax_health_insurance": "foreign",
    }
    _drive(client, "put", "/api/tax/settings", json_body=payload)
    return {"GET /harvest (NV certificate)": _drive(client, "get", "/api/tax/harvest?year=2026")}




def _run_all_cases() -> dict[str, Any]:
    captured: dict[str, Any] = {}
    for name in sorted(CASES):
        seed, fn = CASES[name]
        db = _memory_db()
        user = seed(db)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[current_user] = lambda: user
        try:
            captured[name] = fn(TestClient(app), user)
        finally:
            app.dependency_overrides.clear()
    return captured


# ---------------------------------------------------------------------------
# Masking & comparison
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$")  # RiskAlert.id = uuid4().hex[:12]
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")  # engine alert ids = uuid4().hex


def _mask(value: Any) -> Any:
    """Volatile server-generated leaves mask to sentinels; all seeded ids and
    foreign keys stay strict."""
    if isinstance(value, str):
        if _UUID_RE.match(value):
            return "<UUID>"
        if _ISO_DATETIME_RE.match(value):
            return "<DATETIME>"
        if _HEX12_RE.match(value) or _HEX32_RE.match(value):
            return "<HEXID>"
    return value


def _compare(golden: Any, live: Any, path: str, problems: list[str]) -> None:
    if type(golden) is not type(live):
        problems.append(
            f"{path}: TYPE DRIFT {type(golden).__name__} -> {type(live).__name__} ({golden!r} -> {live!r})"
        )
        return
    if isinstance(golden, dict):
        golden_keys, live_keys = set(golden), set(live)
        for key in sorted(golden_keys - live_keys):
            problems.append(f"{path}.{key}: KEY LOST (in golden, missing from live)")
        for key in sorted(live_keys - golden_keys):
            problems.append(f"{path}.{key}: KEY ADDED (absent from golden)")
        for key in sorted(golden_keys & live_keys):
            _compare(golden[key], live[key], f"{path}.{key}", problems)
    elif isinstance(golden, list):
        if len(golden) != len(live):
            problems.append(f"{path}: LENGTH {len(golden)} -> {len(live)}")
        else:
            for index, (gv, lv) in enumerate(zip(golden, live)):
                _compare(gv, lv, f"{path}[{index}]", problems)
    else:
        if _mask(golden) != _mask(live):
            problems.append(f"{path}: VALUE {golden!r} -> {live!r}")


def test_capture_golden_responses(frozen_today: None) -> None:
    """Run with: TAXVER_GOLDEN_CAPTURE=1 pytest tests/test_tax_verification_contracts.py::test_capture_golden_responses"""
    if os.environ.get("TAXVER_GOLDEN_CAPTURE") != "1":
        pytest.skip("capture mode only runs with TAXVER_GOLDEN_CAPTURE=1")
    live = _run_all_cases()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert len(live) == len(CASES)


def test_golden_response_parity(frozen_today: None) -> None:
    """Replay every tax route against pre-typing golden captures."""
    if not FIXTURE_PATH.exists():
        pytest.fail(
            f"Golden fixture missing: {FIXTURE_PATH}. "
            "Run TAXVER_GOLDEN_CAPTURE=1 pytest "
            "tests/test_tax_verification_contracts.py::test_capture_golden_responses"
        )
    golden = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = _run_all_cases()

    missing = sorted(set(golden) - set(live))
    added = sorted(set(live) - set(golden))
    assert not missing and not added, f"case drift: missing={missing} added={added}"

    problems: list[str] = []
    for name in sorted(golden):
        _compare(golden[name], live[name], name, problems)
    assert not problems, (
        "Golden-response parity violated for the tax router "
        f"({len(problems)} drifts). Widen the response model; never shrink payloads:\n" + "\n".join(problems)
    )


# ---------------------------------------------------------------------------
# Route introspection
# ---------------------------------------------------------------------------


def test_every_tax_route_has_response_model() -> None:
    """All 20 /api/tax routes must declare a typed response_model."""
    from app.interface.api.tax import router as tax_router

    routes = [route for route in tax_router.routes if isinstance(route, APIRoute)]
    paths = sorted({route.path for route in routes})
    assert len(routes) == 20, f"expected 20 tax routes, found {len(routes)}: {paths}"
    untyped = sorted(f"{sorted(route.methods)} {route.path}" for route in routes if route.response_model is None)
    assert untyped == [], f"{len(untyped)} tax routes still lack response_model: {untyped}"

    openapi_paths = [path for path in app.openapi()["paths"] if path.startswith("/api/tax")]
    assert sorted(openapi_paths) == paths, "openapi map and router object disagree on the tax surface"

    # Safety contract stays enforced at the type level: every POST model carries
    # both mandated flags.
    from app.foundation.schemas import GainHarvestOut, TaxMutationOut, TaxOverviewOut, VorabpauschaleEstimateOut

    for model in (TaxMutationOut, VorabpauschaleEstimateOut, TaxOverviewOut, GainHarvestOut):
        assert model.model_fields["estimate"].default is True
        assert model.model_fields["not_tax_advice"].default is True


def test_verification_routes_have_response_models() -> None:
    """Both remaining /api/verification routes (risk, stress) declare a response_model."""
    from app.interface.api.verification import router as ver_router

    routes = [route for route in ver_router.routes if isinstance(route, APIRoute)]
    paths = sorted({route.path for route in routes})
    assert paths == ["/api/verification/risk/{portfolio_id}", "/api/verification/stress/{portfolio_id}"]

    untyped = sorted(f"{sorted(route.methods)} {route.path}" for route in routes if route.response_model is None)
    assert untyped == [], f"verification routes lack response_model: {untyped}"

    openapi_paths = [path for path in app.openapi()["paths"] if path.startswith("/api/verification")]
    assert sorted(openapi_paths) == paths, "openapi map and router object disagree on the verification surface"
