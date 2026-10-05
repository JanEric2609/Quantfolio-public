"""Golden-response parity + route-introspection tests for the budget router.

Plan: .omo/plans/audit-fixes-2026-08.md todo 32 (typing campaign I).

Two phases, one harness:

1. CAPTURE (run BEFORE any response_model existed):
       cd backend && BUDGET_GOLDEN_CAPTURE=1 .venv/bin/pytest \
           tests/test_budget_response_models.py::test_capture_budget_golden_responses -q
   Drives every /api/budget route via TestClient against seeded in-memory SQLite
   and writes the live JSON payloads to
   ``backend/tests/fixtures/budget_golden_responses.json``. Those fixtures are the
   serialization-by-luck contract that response models must preserve byte-shape-wise.

2. PARITY (default mode): replays the identical request sequence on identically
   seeded databases and compares captured-vs-live key-by-key. Adding a
   ``response_model=`` FILTERS fields — any lost/renamed/retyped key fails here.
   The regression fix is widening the model, never shrinking the payload.

Determinism strategy:
- All seeded rows use fixed non-UUID-shaped ids, so values compare strictly.
- ``date.today()`` is frozen to FROZEN_TODAY in every module the request path
  touches (api/budget, services/budget, services/budget_insights, services/envelope).
- Server-generated volatile leaf values (uuid4 primary keys of system-seeded
  categories/rules, wall-clock created_at timestamps) are masked to sentinels
  before comparison; every other value — including all foreign-key ids set by
  seeding — compares strictly, as does JSON type (bool vs int drift is caught).
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
    Category,
    DkbAccount,
    DkbTransaction,
    EnvelopeBudget,
    Expense,
    ExpenseRule,
    IncomeSource,
    Invoice,
    Subscription,
    User,
)
from app.foundation.auth import current_user

FROZEN_TODAY = date(2026, 8, 15)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "budget_golden_responses.json"


class _FakeDate(date):
    """Drop-in for ``date`` whose today() is pinned; constructors still work."""

    @classmethod
    def today(cls) -> date:
        return FROZEN_TODAY


@pytest.fixture
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    for module_name in (
        "app.interface.api.budget",
        "app.foundation.budget",
        "app.foundation.budget_insights",
        "app.foundation.envelope",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "date", _FakeDate)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_user(db) -> User:
    user = User(id="bud-user", username="budgetparity", password_hash="h")
    db.add(user)
    db.commit()
    return user


def _seed_world(db) -> User:
    """Representative budget world covering every route's happy path."""
    user = _seed_user(db)
    cat_income = Category(
        id="bud-cat-income", user_id=user.id, name="Income", color="#22C55E", icon="banknote", type="income"
    )
    cat_groceries = Category(
        id="bud-cat-groceries", user_id=user.id, name="Groceries", color="#3B82F6", icon="shopping-cart", type="expense"
    )
    cat_laptop = Category(
        id="bud-cat-laptop",
        user_id=user.id,
        name="Laptop Fund",
        color="#F59E0B",
        icon="laptop",
        type="expense",
        target_amount=Decimal("1200"),
        target_date=date(2027, 1, 1),
    )
    cat_travel = Category(
        id="bud-cat-travel", user_id=user.id, name="Travel", color="#8B5CF6", icon="plane", type="expense"
    )
    db.add_all([cat_income, cat_groceries, cat_laptop, cat_travel])
    db.commit()
    db.add_all(
        [
            Expense(
                id="bud-exp-income",
                user_id=user.id,
                date=date(2026, 8, 1),
                amount=Decimal("2500.00"),
                currency="EUR",
                description="Job Gehalt August",
                category_id=cat_income.id,
                source="manual",
            ),
            Expense(
                id="bud-exp-rewe-synced",
                user_id=user.id,
                date=date(2026, 8, 3),
                amount=Decimal("-42.50"),
                currency="EUR",
                description="REWE Filiale 400",
                category_id=cat_groceries.id,
                source="dkb_auto",
                dkb_dedupe_hash="bud-hash-rewe",
            ),
            # Uncategorized manual twin of the synced REWE row: feeds the monthly
            # review's uncategorized list AND its potential-duplicates pair.
            Expense(
                id="bud-exp-rewe-manual",
                user_id=user.id,
                date=date(2026, 8, 2),
                amount=Decimal("-42.50"),
                currency="EUR",
                description="REWE Markt",
                category_id=None,
                source="manual",
            ),
            Expense(
                id="bud-exp-book",
                user_id=user.id,
                date=date(2026, 3, 15),
                amount=Decimal("-19.99"),
                currency="EUR",
                description="Book Store",
                category_id=cat_groceries.id,
                source="manual",
            ),
        ]
    )
    db.commit()
    account = DkbAccount(id="bud-acct", user_id=user.id, type="checking", iban="DE02120300000000202051")
    db.add(account)
    db.commit()
    # Two same-merchant, same-amount bank rows ~4 weeks apart -> subscription suggestion.
    db.add_all(
        [
            DkbTransaction(
                account_id=account.id,
                date=date(2026, 7, 1),
                amount=Decimal("-10.99"),
                reference="Spotify Premium",
                dedupe_hash="bud-tx-spot1",
            ),
            DkbTransaction(
                account_id=account.id,
                date=date(2026, 7, 29),
                amount=Decimal("-10.99"),
                reference="Spotify Premium",
                dedupe_hash="bud-tx-spot2",
            ),
        ]
    )
    db.commit()
    db.add_all(
        [
            Subscription(
                id="bud-sub-netflix",
                user_id=user.id,
                name="Netflix",
                amount=Decimal("13.99"),
                currency="EUR",
                billing_cycle="monthly",
                next_due_date=date(2026, 8, 20),
                category_id=cat_groceries.id,
                payment_method="Visa",
                logo_url=None,
                active=True,
                notes="streaming",
            ),
            Subscription(
                id="bud-sub-gym",
                user_id=user.id,
                name="Old Gym",
                amount=Decimal("29.90"),
                currency="EUR",
                billing_cycle="quarterly",
                next_due_date=date(2026, 1, 10),
                active=False,
            ),
            IncomeSource(
                id="bud-inc-job",
                user_id=user.id,
                name="Job",
                amount=Decimal("2500"),
                currency="EUR",
                cadence="monthly",
                next_date=date(2026, 9, 1),
                active=True,
            ),
            IncomeSource(
                id="bud-inc-parents",
                user_id=user.id,
                name="Parents",
                amount=Decimal("200"),
                currency="EUR",
                cadence="monthly",
                next_date=date(2026, 9, 1),
                active=False,
            ),
            Invoice(
                id="bud-inv-power",
                user_id=user.id,
                issuer="Power Company",
                amount=Decimal("89.00"),
                currency="EUR",
                due_date=date(2026, 8, 25),
                paid=False,
                notes=None,
            ),
            Invoice(
                id="bud-inv-net",
                user_id=user.id,
                issuer="Internet",
                amount=Decimal("39.99"),
                currency="EUR",
                due_date=date(2026, 7, 1),
                paid=True,
            ),
            EnvelopeBudget(
                id="bud-env-groc",
                user_id=user.id,
                category_id=cat_groceries.id,
                year=2026,
                month=8,
                budgeted_amount=Decimal("150"),
            ),
            EnvelopeBudget(
                id="bud-env-laptop",
                user_id=user.id,
                category_id=cat_laptop.id,
                year=2026,
                month=8,
                budgeted_amount=Decimal("100"),
            ),
        ]
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


CASES: dict[str, Any] = {}


def case(name: str, seed=_seed_world):
    def wrap(fn):
        CASES[name] = (seed, fn)
        return fn

    return wrap


@case("expenses_list_filled")
def _c_expenses_list(client, user):
    return {"GET /expenses": _drive(client, "get", "/api/budget/expenses")}


@case("expenses_create")
def _c_expenses_create(client, user):
    payload = {
        "date": "2026-08-10",
        "amount": "33.30",
        "currency": "EUR",
        "description": "Bäckerei",
        "category_id": "bud-cat-groceries",
    }
    return {"POST /expenses": _drive(client, "post", "/api/budget/expenses", json_body=payload)}


@case("expenses_update")
def _c_expenses_update(client, user):
    payload = {
        "date": "2026-03-15",
        "amount": "-24.99",
        "currency": "EUR",
        "description": "Book Store II",
        "category_id": "bud-cat-groceries",
        "notes": "updated",
    }
    return {"PUT /expenses/{id}": _drive(client, "put", "/api/budget/expenses/bud-exp-book", json_body=payload)}


@case("expenses_delete")
def _c_expenses_delete(client, user):
    return {"DELETE /expenses/{id}": _drive(client, "delete", "/api/budget/expenses/bud-exp-book")}


@case("expenses_list_empty", seed=_seed_user)
def _c_expenses_empty(client, user):
    return {"GET /expenses (empty)": _drive(client, "get", "/api/budget/expenses")}


@case("summary_filled")
def _c_summary(client, user):
    return {"GET /summary": _drive(client, "get", "/api/budget/summary?year=2026&month=8")}


@case("summary_empty", seed=_seed_user)
def _c_summary_empty(client, user):
    return {"GET /summary (empty)": _drive(client, "get", "/api/budget/summary?year=2026&month=8")}


@case("subscriptions_list")
def _c_subscriptions_list(client, user):
    return {"GET /subscriptions": _drive(client, "get", "/api/budget/subscriptions")}


@case("subscriptions_create")
def _c_subscriptions_create(client, user):
    payload = {
        "name": "Spotify",
        "amount": "10.99",
        "currency": "EUR",
        "billing_cycle": "monthly",
        "next_due_date": "2026-09-01",
        "category_id": "bud-cat-groceries",
        "payment_method": "Visa",
        "notes": None,
    }
    return {"POST /subscriptions": _drive(client, "post", "/api/budget/subscriptions", json_body=payload)}


@case("subscriptions_update")
def _c_subscriptions_update(client, user):
    payload = {
        "name": "Netflix Premium",
        "amount": "17.99",
        "currency": "EUR",
        "billing_cycle": "monthly",
        "next_due_date": "2026-08-20",
        "category_id": "bud-cat-groceries",
        "payment_method": "Visa",
        "active": True,
        "notes": "upgraded",
    }
    return {
        "PUT /subscriptions/{id}": _drive(client, "put", "/api/budget/subscriptions/bud-sub-netflix", json_body=payload)
    }


@case("subscriptions_delete")
def _c_subscriptions_delete(client, user):
    return {"DELETE /subscriptions/{id}": _drive(client, "delete", "/api/budget/subscriptions/bud-sub-gym")}


@case("subscriptions_mark_paid")
def _c_subscriptions_mark_paid(client, user):
    payload = {"paid_date": "2026-08-16", "create_expense": True}
    return {
        "POST /subscriptions/{id}/mark-paid": _drive(
            client, "post", "/api/budget/subscriptions/bud-sub-netflix/mark-paid", json_body=payload
        )
    }


@case("subscriptions_list_empty", seed=_seed_user)
def _c_subscriptions_empty(client, user):
    return {"GET /subscriptions (empty)": _drive(client, "get", "/api/budget/subscriptions")}


@case("suggestions_hit")
def _c_suggestions(client, user):
    return {"GET /subscriptions/suggestions": _drive(client, "get", "/api/budget/subscriptions/suggestions")}


@case("suggestions_empty", seed=_seed_user)
def _c_suggestions_empty(client, user):
    return {"GET /subscriptions/suggestions (empty)": _drive(client, "get", "/api/budget/subscriptions/suggestions")}


@case("income_sources_list")
def _c_income_list(client, user):
    return {"GET /income-sources": _drive(client, "get", "/api/budget/income-sources")}


@case("income_sources_create")
def _c_income_create(client, user):
    payload = {"name": "BAföG", "amount": "450", "currency": "EUR", "cadence": "monthly", "next_date": "2026-09-01"}
    return {"POST /income-sources": _drive(client, "post", "/api/budget/income-sources", json_body=payload)}


@case("income_sources_update")
def _c_income_update(client, user):
    payload = {
        "name": "Job",
        "amount": "2600",
        "currency": "EUR",
        "cadence": "monthly",
        "next_date": "2026-09-01",
        "active": True,
    }
    return {"PUT /income-sources/{id}": _drive(client, "put", "/api/budget/income-sources/bud-inc-job", json_body=payload)}


@case("income_sources_delete")
def _c_income_delete(client, user):
    return {"DELETE /income-sources/{id}": _drive(client, "delete", "/api/budget/income-sources/bud-inc-parents")}


@case("income_sources_list_empty", seed=_seed_user)
def _c_income_empty(client, user):
    return {"GET /income-sources (empty)": _drive(client, "get", "/api/budget/income-sources")}


@case("invoices_list")
def _c_invoices_list(client, user):
    return {"GET /invoices": _drive(client, "get", "/api/budget/invoices")}


@case("invoices_create")
def _c_invoices_create(client, user):
    payload = {"issuer": "Mobile Carrier", "amount": "19.99", "due_date": "2026-09-15", "paid": False}
    return {"POST /invoices": _drive(client, "post", "/api/budget/invoices", json_body=payload)}


@case("invoices_update")
def _c_invoices_update(client, user):
    payload = {"issuer": "Power Company", "amount": "95.50", "due_date": "2026-08-25", "paid": False, "notes": "hike"}
    return {"PUT /invoices/{id}": _drive(client, "put", "/api/budget/invoices/bud-inv-power", json_body=payload)}


@case("invoices_delete")
def _c_invoices_delete(client, user):
    return {"DELETE /invoices/{id}": _drive(client, "delete", "/api/budget/invoices/bud-inv-net")}


@case("invoices_mark_paid")
def _c_invoices_mark_paid(client, user):
    payload = {"paid_date": "2026-08-14", "create_expense": True}
    return {
        "POST /invoices/{id}/mark-paid": _drive(
            client, "post", "/api/budget/invoices/bud-inv-power/mark-paid", json_body=payload
        )
    }


@case("invoices_mark_unpaid")
def _c_invoices_mark_unpaid(client, user):
    return {"POST /invoices/{id}/mark-unpaid": _drive(client, "post", "/api/budget/invoices/bud-inv-net/mark-unpaid")}


@case("invoices_list_empty", seed=_seed_user)
def _c_invoices_empty(client, user):
    return {"GET /invoices (empty)": _drive(client, "get", "/api/budget/invoices")}


@case("categories_list")
def _c_categories_list(client, user):
    return {"GET /categories": _drive(client, "get", "/api/budget/categories")}


@case("categories_seed")
def _c_categories_seed(client, user):
    return {"POST /categories/seed": _drive(client, "post", "/api/budget/categories/seed")}


@case("categories_create")
def _c_categories_create(client, user):
    payload = {"name": "Hobby", "color": "#EF4444", "icon": "gamepad", "type": "expense"}
    return {"POST /categories": _drive(client, "post", "/api/budget/categories", json_body=payload)}


@case("categories_update")
def _c_categories_update(client, user):
    payload = {"name": "Vacation", "color": "#8B5CF6", "icon": "plane", "type": "expense"}
    return {"PUT /categories/{id}": _drive(client, "put", "/api/budget/categories/bud-cat-travel", json_body=payload)}


@case("categories_delete")
def _c_categories_delete(client, user):
    return {"DELETE /categories/{id}": _drive(client, "delete", "/api/budget/categories/bud-cat-travel")}


@case("cashflow_filled")
def _c_cashflow(client, user):
    return {"GET /cashflow": _drive(client, "get", "/api/budget/cashflow")}


@case("cashflow_empty", seed=_seed_user)
def _c_cashflow_empty(client, user):
    return {"GET /cashflow (empty)": _drive(client, "get", "/api/budget/cashflow")}


def _seed_user_rule(db) -> User:
    """World plus one fixed-id user rule (GET /rules also auto-seeds system rules)."""
    user = _seed_world(db)
    db.add(
        ExpenseRule(
            id="bud-rule-user",
            user_id=user.id,
            pattern="SPOTIFY",
            match_type="contains",
            category_id="bud-cat-groceries",
            priority=5,
            source="user",
        )
    )
    db.commit()
    return user


@case("rules_list", seed=_seed_user_rule)
def _c_rules_list(client, user):
    return {"GET /rules": _drive(client, "get", "/api/budget/rules")}


@case("rules_create", seed=_seed_user_rule)
def _c_rules_create(client, user):
    payload = {"pattern": "AMAZON PRIME", "match_type": "contains", "category_id": None, "priority": 20}
    return {"POST /rules": _drive(client, "post", "/api/budget/rules", json_body=payload)}


@case("rules_delete", seed=_seed_user_rule)
def _c_rules_delete(client, user):
    return {"DELETE /rules/{id}": _drive(client, "delete", "/api/budget/rules/bud-rule-user")}


@case("categorize_run_rule_hit")
def _c_categorize_hit(client, user):
    return {"POST /categorize/run": _drive(client, "post", "/api/budget/categorize/run")}


@case("categorize_run_empty", seed=_seed_user)
def _c_categorize_empty(client, user):
    return {"POST /categorize/run (empty)": _drive(client, "post", "/api/budget/categorize/run")}


@case("envelopes_get")
def _c_envelopes_get(client, user):
    return {"GET /envelopes": _drive(client, "get", "/api/budget/envelopes?year=2026&month=8")}


@case("envelopes_get_empty", seed=_seed_user)
def _c_envelopes_empty(client, user):
    return {"GET /envelopes (empty)": _drive(client, "get", "/api/budget/envelopes?year=2026&month=8")}


@case("envelopes_put")
def _c_envelopes_put(client, user):
    payload = {"year": 2026, "month": 8, "budgeted_amount": "120"}
    return {
        "PUT /envelopes/{category_id}": _drive(
            client, "put", "/api/budget/envelopes/bud-cat-travel", json_body=payload
        )
    }


@case("envelopes_cover")
def _c_envelopes_cover(client, user):
    payload = {
        "from_category_id": "bud-cat-groceries",
        "to_category_id": "bud-cat-travel",
        "amount": "30",
        "year": 2026,
        "month": 8,
    }
    return {"POST /envelopes/cover": _drive(client, "post", "/api/budget/envelopes/cover", json_body=payload)}


@case("envelopes_history")
def _c_envelopes_history(client, user):
    return {"GET /envelopes/history": _drive(client, "get", "/api/budget/envelopes/history?months=3")}


@case("buffer_filled")
def _c_buffer(client, user):
    return {"GET /buffer": _drive(client, "get", "/api/budget/buffer")}


@case("buffer_empty", seed=_seed_user)
def _c_buffer_empty(client, user):
    return {"GET /buffer (empty)": _drive(client, "get", "/api/budget/buffer")}


@case("sankey_filled")
def _c_sankey(client, user):
    return {"GET /reports/sankey": _drive(client, "get", "/api/budget/reports/sankey?year=2026")}


@case("sankey_empty")
def _c_sankey_empty(client, user):
    return {"GET /reports/sankey (no data for year)": _drive(client, "get", "/api/budget/reports/sankey?year=1999")}


@case("review_filled")
def _c_review(client, user):
    return {"GET /review": _drive(client, "get", "/api/budget/review?year=2026&month=8")}


@case("review_empty")
def _c_review_empty(client, user):
    return {"GET /review (empty month)": _drive(client, "get", "/api/budget/review?year=2025&month=1")}


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


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _mask(value: Any) -> Any:
    """Volatile server-generated leaves (uuid4 pks of system rows, wall-clock
    timestamps) mask to sentinels; all seeded ids and foreign keys stay strict."""
    if isinstance(value, str):
        if _UUID_RE.match(value):
            return "<UUID>"
        if _ISO_DATETIME_RE.match(value):
            return "<DATETIME>"
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
            for index, (g, l) in enumerate(zip(golden, live)):
                _compare(g, l, f"{path}[{index}]", problems)
    else:
        if _mask(golden) != _mask(live):
            problems.append(f"{path}: VALUE {golden!r} -> {live!r}")


def test_capture_budget_golden_responses(frozen_today: None) -> None:
    """Run with: BUDGET_GOLDEN_CAPTURE=1 pytest tests/test_budget_response_models.py::test_capture_budget_golden_responses"""
    if os.environ.get("BUDGET_GOLDEN_CAPTURE") != "1":
        pytest.skip("capture mode only runs with BUDGET_GOLDEN_CAPTURE=1")
    live = _run_all_cases()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert len(live) == len(CASES)


def test_budget_golden_response_parity(frozen_today: None) -> None:
    """Replay every budget route against pre-typing golden captures, key-by-key."""
    if not FIXTURE_PATH.exists():
        pytest.fail(
            f"Golden fixture missing: {FIXTURE_PATH}. "
            "Run BUDGET_GOLDEN_CAPTURE=1 pytest tests/test_budget_response_models.py::test_capture_budget_golden_responses"
        )
    golden = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = _run_all_cases()

    missing = sorted(set(golden) - set(live))
    added = sorted(set(live) - set(golden))
    assert not missing and not added, f"case drift: missing={missing} added={added}"

    problems: list[str] = []
    for name in sorted(golden):
        case_problems: list[str] = []
        _compare(golden[name], live[name], f"{name}", case_problems)
        for problem in case_problems:
            problems.append(problem)
    assert not problems, (
        "Golden-response parity violated for the budget router "
        f"({len(problems)} drifts). Widen the response model; never shrink payloads:\n" + "\n".join(problems)
    )


def test_every_budget_route_has_response_model() -> None:
    """Route introspection: all 38 /api/budget routes must declare a response_model.

    FastAPI >=0.141 defers included routers behind _IncludedRouter wrappers, so
    introspect the budget router object itself and cross-check the openapi map.
    """
    from app.interface.api.budget import router as budget_router

    routes = [route for route in budget_router.routes if isinstance(route, APIRoute)]
    paths = sorted({route.path for route in routes})
    assert len(routes) == 38, f"expected 38 budget routes, found {len(routes)}: {paths}"
    untyped = sorted(f"{sorted(route.methods)} {route.path}" for route in routes if route.response_model is None)
    assert untyped == [], f"{len(untyped)} budget routes still lack response_model: {untyped}"

    openapi_paths = [path for path in app.openapi()["paths"] if path.startswith("/api/budget")]
    assert sorted(openapi_paths) == paths, "openapi map and router object disagree on the budget surface"
