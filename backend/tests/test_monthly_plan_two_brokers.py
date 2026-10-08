"""Phase 3: the monthly plan per depot.

The fixture is a two-broker set-up: a legacy MSCI World at DKB, new money at
Scalable Capital (All-World, EM IMI and two single stocks bought by savings
plan), cash at both, and the Scalable Tagesgeld.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import monthly_plan
from app.decision.monthly_plan import build_monthly_plan
from app.foundation.core.db import Base
from app.foundation.models.entities import BrokerPosition, BrokerSyncLog, ConnectedAccount, DkbAccount, DkbPosition, User
from app.foundation.settings import upsert_public_settings

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
MSCI_WORLD = "IE00B4L5Y983"
ALL_WORLD = "IE00BK5BQT80"
EM_IMI = "IE00BKM4GZ66"
MOMENTUM = "IE00BP3QZ825"

# Scalable savings plans as the sync stores them.
OWNER_PLANS = [
    {"isin": ALL_WORLD, "name": "Vanguard FTSE All-World (Acc)", "amount": "20.00", "frequency": "MONTHLY",
     "day_of_month": 1, "next_execution_date": "2026-11-02", "kind": "security"},
    {"isin": EM_IMI, "name": "iShares Core MSCI Emerging Markets IMI (Acc)", "amount": "12.00",
     "frequency": "MONTHLY", "day_of_month": 1, "next_execution_date": "2026-11-02", "kind": "security"},
    {"isin": "NL0010273215", "name": "ASML Holding", "amount": "10", "frequency": "MONTHLY",
     "day_of_month": 7, "next_execution_date": "2026-11-09", "kind": "security"},
    {"isin": "US67066G1040", "name": "NVIDIA", "amount": "30", "frequency": "MONTHLY",
     "day_of_month": 4, "next_execution_date": "2026-11-04", "kind": "security"},
]


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> str:
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    return user.id


def _dkb(db, user_id: str, positions=((MSCI_WORLD, "iShares Core MSCI World", 30_000.0, 20_000.0),),
         giro: float = 2_500.0) -> None:
    depot = DkbAccount(user_id=user_id, type="depot", iban="DE00DEPOT", balance=0)
    db.add_all([depot, DkbAccount(user_id=user_id, type="giro", iban="DE00GIRO", balance=Decimal(str(giro)))])
    db.flush()
    for isin, name, value, cost in positions:
        db.add(DkbPosition(
            account_id=depot.id, isin=isin, name=name, quantity=Decimal("100"),
            avg_buy_price=Decimal(str(cost / 100)), current_value=Decimal(str(value)),
            last_synced=datetime(2026, 10, 2, 18, 0, tzinfo=UTC),
        ))
    db.commit()


def _scalable(db, user_id: str, *, plans=OWNER_PLANS, buying_power: str | None = "7.10",
              overnight: float = 812.40, positions=()) -> None:
    raw = {"savings_plans": plans}
    if buying_power is not None:
        raw["cash"] = {"cash_balance": "42.10", "buying_power": buying_power}
    depot = ConnectedAccount(user_id=user_id, source="scalable", name="Scalable Capital depot",
                             account_type="depot", balance=Decimal("0"), raw_json=json.dumps(raw))
    db.add_all([
        depot,
        ConnectedAccount(user_id=user_id, source="scalable", name="Scalable Capital cash",
                         account_type="cash", balance=Decimal("42.10")),
        ConnectedAccount(user_id=user_id, source="scalable", name="Scalable Capital overnight",
                         account_type="savings", balance=Decimal(str(overnight))),
    ])
    db.flush()
    for isin, name, value, cost in positions:
        db.add(BrokerPosition(
            user_id=user_id, connected_account_id=depot.id, source="scalable", isin=isin, name=name,
            security_type="ETF", quantity=Decimal("10"), avg_buy_price=Decimal(str(cost / 10)),
            current_value=Decimal(str(value)),
        ))
    db.commit()


def _core_buy(plan: dict) -> dict:
    return next(a for a in plan["actions"] if a["sleeve"] == "core" and a["kind"] != "one_off")


# --- broker per buy -----------------------------------------------------------


def test_automatic_broker_sends_new_money_to_scalable_once_its_depot_is_synced():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id, plans=[])
    upsert_public_settings(db, {"plan_contribution_mode": "manual_orders", "monthly_contribution_eur": 100})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["broker_choice"] == "auto" and plan["brokers_connected"] == ["dkb", "scalable"]
    assert plan["broker"] == "scalable"
    action = _core_buy(plan)
    assert action["broker"] == "scalable" and action["broker_label"] == "Scalable Capital"
    assert "0,99 €" in action["note"]
    assert plan["headline"].startswith("Buy 100 € of EUNL.DE at Scalable Capital.")


def test_automatic_broker_is_dkb_with_only_dkb_synced_and_an_explicit_choice_wins():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    assert build_monthly_plan(db, user_id, now=NOW)["broker"] == "dkb"

    _scalable(db, user_id, plans=[])
    upsert_public_settings(db, {"plan_broker": "dkb"})
    assert build_monthly_plan(db, user_id, now=NOW)["broker"] == "dkb"


# --- savings plans that already run --------------------------------------------


def test_running_plans_are_sorted_into_sleeves_and_em_counts_as_core():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id)

    plan = build_monthly_plan(db, user_id, now=NOW)

    running = plan["savings_plans"]
    assert running["monthly_eur"] == 72.0
    assert running["by_sleeve"] == {"core": 32.0, "tilt": 0.0, "satellite": 40.0}
    assert {p["name"]: p["sleeve"] for p in running["items"]}["iShares Core MSCI Emerging Markets IMI (Acc)"] == "core"
    # The default 1,000 € contribution is not what runs: say so, and ask for
    # the missing core money instead of a second plan.
    assert plan["no_change"] is False
    # The 40 € of stock-pick plans are the owner's own budget: the core needs the rest.
    assert plan["headline"] == "Raise your core savings plans by 928 € to 960 € a month (now 32 €)."
    assert "raise them by 928 €" in _core_buy(plan)["note"]
    notes = " ".join(plan["notes"])
    assert "total 72 € a month" in notes and "set to 1.000 €" in notes
    assert "40 € of your 1.000 € a month (4 %) goes into your own savings plans (ASML Holding, NVIDIA)" in notes


def test_plans_that_cover_the_core_are_left_alone():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id, plans=OWNER_PLANS[:2])
    upsert_public_settings(db, {"monthly_contribution_eur": 32})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["no_change"] is True
    assert plan["headline"] == "Let your savings plans run: 32 € a month into the core. Nothing else to do this month."
    assert _core_buy(plan)["note"].startswith("Already covered: your savings plans at Scalable Capital")
    # Only the reminder to set a reserve: nothing about the plans themselves.
    assert [n for n in plan["notes"] if "Set your emergency reserve" not in n] == []


def test_plans_into_a_locked_sleeve_are_not_a_no_change_month():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id)
    upsert_public_settings(db, {"monthly_contribution_eur": 32})

    plan = build_monthly_plan(db, user_id, now=NOW)

    # The picks (40 €) already exceed the 32 € contribution: nothing is left for the core to buy.
    assert plan["no_change"] is True
    assert plan["headline"] == (
        "Let your savings plans run: 32 € a month into the core and 40 € into your own picks. "
        "Nothing else to do this month."
    )
    assert "total 72 € a month" in " ".join(plan["notes"])


PICK_PLANS = [
    {"isin": "DE0007164600", "name": "SAP", "amount": "20", "frequency": "MONTHLY",
     "day_of_month": 4, "next_execution_date": "2026-11-04", "kind": "security", "dynamization_rate": None},
    OWNER_PLANS[2], OWNER_PLANS[3],
]


def test_stock_pick_plans_are_a_budget_the_core_plans_get_the_rest():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id, plans=[*OWNER_PLANS[:2], *PICK_PLANS])
    upsert_public_settings(db, {"monthly_contribution_eur": 200})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["headline"].startswith("Raise your core savings plans by 108 € to 140 € a month (now 32 €).")
    assert _core_buy(plan)["amount_eur"] == 140.0
    sp = plan["savings_plans"]
    assert sp["other_budget_pct"] == 30.0 and sp["core_needed_eur"] == 140.0
    assert sum(1 for n in plan["notes"] if "30 %" in n) == 1
    by_key = {s["key"]: s for s in plan["sleeves"]}
    assert by_key["satellite"]["contribution_eur"] == 60.0
    assert by_key["core"]["contribution_eur"] == 140.0


def test_picks_plus_core_plans_covering_the_budget_leave_nothing_to_do():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    core = [{**OWNER_PLANS[0], "amount": "128"}, OWNER_PLANS[1]]
    _scalable(db, user_id, plans=[*core, *PICK_PLANS])
    upsert_public_settings(db, {"monthly_contribution_eur": 200})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["no_change"] is True
    assert plan["headline"].startswith("Let your savings plans run: 140 € a month into the core and 60 € into your own picks")


def test_a_removed_plan_leaves_the_position_and_the_sleeve_values_alone():
    stock = ("US67066G1040", "NVIDIA", 800.0, 600.0)
    results = []
    for plans in ([*OWNER_PLANS[:2], *PICK_PLANS], [*OWNER_PLANS[:2], *PICK_PLANS[:2]]):
        db = _memory_db()
        user_id = _user(db)
        _dkb(db, user_id)
        _scalable(db, user_id, plans=plans, positions=[stock])
        results.append(build_monthly_plan(db, user_id, now=NOW))
    with_plan, without_plan = results
    assert len(without_plan["savings_plans"]["items"]) == len(with_plan["savings_plans"]["items"]) - 1
    assert [(s["key"], s["current_eur"]) for s in with_plan["sleeves"]] == [
        (s["key"], s["current_eur"]) for s in without_plan["sleeves"]
    ]
    assert next(s for s in without_plan["sleeves"] if s["key"] == "satellite")["current_eur"] == 800.0


def test_paused_and_overdue_plans_are_listed_but_not_counted():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    paused = {**PICK_PLANS[0], "paused": True}
    overdue = {**OWNER_PLANS[3], "next_execution_date": "2026-09-20"}
    just_late = {**OWNER_PLANS[2], "next_execution_date": "2026-09-30"}  # 3 days: still running
    _scalable(db, user_id, plans=[*OWNER_PLANS[:2], paused, overdue, just_late])
    upsert_public_settings(db, {"monthly_contribution_eur": 200})

    plan = build_monthly_plan(db, user_id, now=NOW)

    sp = plan["savings_plans"]
    assert {p["name"]: p["not_running_reason"] for p in sp["stopped"]} == {"SAP": "paused", "NVIDIA": "overdue"}
    assert sp["by_sleeve"]["satellite"] == 10.0
    assert sp["monthly_eur"] == 42.0
    notes = " ".join(plan["notes"])
    assert "SAP savings plan is paused" in notes and "NVIDIA savings plan was due on 2026-09-20" in notes


def test_dynamization_and_plan_freshness_are_reported():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    plans = [{**OWNER_PLANS[0], "dynamization_rate": "2"}]
    _scalable(db, user_id, plans=plans)
    depot = db.query(ConnectedAccount).filter_by(account_type="depot").one()
    raw = json.loads(depot.raw_json)
    raw["plans_synced_at"] = "2026-10-03T08:00:00+00:00"
    depot.raw_json = json.dumps(raw)
    db.add(BrokerSyncLog(user_id=user_id, source="scalable", trigger="manual", state="warning",
                         started_at=NOW, counts_json=json.dumps({"plans_unavailable": True})))
    db.commit()

    sp = build_monthly_plan(db, user_id, now=NOW)["savings_plans"]

    assert sp["items"][0]["dynamization_rate"] == 2.0
    assert sp["synced_at"] == "2026-10-03T08:00:00+00:00"
    assert sp["last_fetch_failed"] is True


def test_manual_orders_mention_the_plans_that_buy_on_their_own():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=OWNER_PLANS[:2])
    upsert_public_settings(db, {"plan_contribution_mode": "manual_orders", "monthly_contribution_eur": 200})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["no_change"] is True
    assert any("also buy 32 € a month on their own" in n for n in plan["notes"])


def test_a_plan_with_an_unknown_schedule_is_listed_but_not_counted():
    db = _memory_db()
    user_id = _user(db)
    odd = {**OWNER_PLANS[0], "frequency": "LUNAR"}
    _scalable(db, user_id, plans=[odd, OWNER_PLANS[1]])

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["savings_plans"]["monthly_eur"] == 12.0
    assert [p["monthly_eur"] for p in plan["savings_plans"]["items"]] == [None, 12.0]
    assert any("runs lunar" in n for n in plan["notes"])


def test_a_weekly_plan_counts_at_its_monthly_rate():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=[{**OWNER_PLANS[0], "amount": "12", "frequency": "WEEKLY"}])

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["savings_plans"]["monthly_eur"] == 52.0


# --- cash above the emergency reserve -------------------------------------------


def test_cash_above_the_reserve_is_an_optional_one_off_and_giro_never_counts():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id, giro=4_000.0)
    _scalable(db, user_id, plans=OWNER_PLANS[:2])
    upsert_public_settings(db, {"monthly_contribution_eur": 32, "emergency_reserve_eur": 500})

    plan = build_monthly_plan(db, user_id, now=NOW)

    cash = plan["cash"]
    assert cash["savings_eur"] == 812.40
    assert cash["broker_cash_eur"] == 7.10  # buying power, not the 42.10 € earmarked for plans
    assert cash["reserve_set"] is True and cash["investable_eur"] == 319.50
    [one_off] = [a for a in plan["actions"] if a["kind"] == "one_off"]
    assert one_off["sleeve"] == "core" and one_off["amount_eur"] == 319.50
    assert one_off["broker"] == "scalable"
    assert one_off["note"].startswith("Optional one-off from cash above your 500 € reserve: one order at Scalable")
    assert plan["no_change"] is True  # the one-off is optional
    assert plan["headline"].endswith("Optionally, invest 320 € of cash above your reserve.")
    assert plan["notes"] == [
        "For the one-off, move 312 € into your Scalable cash account first, from Tagesgeld or wherever the "
        "money sits; 7 € is already free there."
    ]
    # Optional money does not move the sleeves.
    core = next(s for s in plan["sleeves"] if s["key"] == "core")
    assert core["contribution_eur"] == 32.0


def test_without_a_reserve_the_plan_suggests_nothing_from_cash():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=[])

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["cash"]["reserve_set"] is False and plan["cash"]["investable_eur"] == 0.0
    assert not [a for a in plan["actions"] if a["kind"] == "one_off"]
    assert any("Set your emergency reserve" in n for n in plan["notes"])


def test_a_one_off_too_small_for_a_one_percent_fee_stays_in_cash():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=[])
    upsert_public_settings(db, {"emergency_reserve_eur": 768})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["cash"]["investable_eur"] == 51.50
    assert not [a for a in plan["actions"] if a["kind"] == "one_off"]
    assert any("too little for one order at Scalable Capital" in n for n in plan["notes"])


def test_buying_power_falls_back_to_the_cash_balance_before_it_is_read():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=[], buying_power=None)

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["cash"]["broker_cash_eur"] == 42.10


# --- sales per depot and look-through -------------------------------------------


def test_a_sale_starts_at_the_depot_with_the_smallest_gain_per_euro(monkeypatch):
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id, positions=(
        (MSCI_WORLD, "iShares Core MSCI World", 70_000.0, 50_000.0),
        (MOMENTUM, "World Momentum", 20_000.0, 10_000.0),  # 50 % of it is gain
    ))
    _scalable(db, user_id, plans=[], positions=((MOMENTUM, "World Momentum", 10_000.0, 9_500.0),))  # 5 %
    upsert_public_settings(db, {"plan_tilt_isins": MOMENTUM})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user_id, now=NOW)

    sales = [a for a in plan["actions"] if a["kind"] == "sale"]
    assert [(a["broker"], a["amount_eur"]) for a in sales] == [("scalable", 10_000.0), ("dkb", 4_850.0)]
    assert sales[0]["note"].startswith("Sells at Scalable Capital back to the 15 % target.")
    assert "FIFO runs per depot" in sales[0]["note"]


def test_core_look_through_reports_the_emerging_markets_share():
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id, plans=[], positions=(
        (ALL_WORLD, "Vanguard FTSE All-World", 1_000.0, 900.0),
        (EM_IMI, "iShares Core MSCI EM IMI", 300.0, 280.0),
    ))

    plan = build_monthly_plan(db, user_id, now=NOW)

    look = plan["core_look_through"]
    assert look["em_eur"] == 400.0
    assert look["em_pct"] == pytest.approx(round(100 * 400 / 31_300, 1))
    assert look["world_em_pct"] == 10.0
    assert next(s for s in plan["sleeves"] if s["key"] == "satellite")["current_eur"] == 0.0


def test_core_look_through_reports_a_core_of_unknown_split():
    from app.decision.monthly_plan import core_look_through

    look = core_look_through([{"isin": "IE000UNKNOWN0", "value_eur": 5_000.0}])
    assert look == {"em_eur": 0.0, "em_pct": None, "world_em_pct": 10.0, "unknown_eur": 5_000.0}
    assert core_look_through([]) is None


def test_month_endpoint_carries_the_new_fields():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _scalable(db, user_id)
    upsert_public_settings(db, {"emergency_reserve_eur": 500})
    user = db.get(User, user_id)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/plan/month").json()
    finally:
        app.dependency_overrides.clear()

    assert body["savings_plans"]["monthly_eur"] == 72.0
    assert body["cash"]["investable_eur"] == 319.50
    assert body["core_look_through"]["world_em_pct"] == 10.0
    assert {a["kind"] for a in body["actions"]} >= {"savings_plan", "one_off"}
    assert all(a["broker"] for a in body["actions"])
    assert body["savings_plans"]["items"][0]["running"] is True
    assert body["savings_plans"]["core_needed_eur"] is not None
    assert body["evidence"]["tilt"]["passed"] is False


def _tagesgeld(db, user_id: str, balance: float) -> None:
    db.add(DkbAccount(user_id=user_id, type="tagesgeld", iban="DE00TG", balance=Decimal(str(balance))))
    db.commit()


def test_each_one_off_order_keeps_its_own_fee_at_one_percent(monkeypatch):
    # DKB only: 1,000 € passes as one order (10 € = 1 %), but split over two
    # tilt ETFs it would be two 10 € orders at 2 % each; it goes to the core.
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id)
    _tagesgeld(db, user_id, 1_500.0)
    upsert_public_settings(db, {"emergency_reserve_eur": 500, "plan_tilt_isins": f"{MOMENTUM},IE00BP3QZB59",
                                "monthly_contribution_eur": 0})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user_id, now=NOW)

    one_offs = [(a["sleeve"], a["amount_eur"], a["broker"]) for a in plan["actions"] if a["kind"] == "one_off"]
    assert one_offs == [("core", 1_000.0, "dkb")]


def test_a_one_off_stock_order_is_checked_at_the_satellite_broker(monkeypatch):
    # New money goes to Scalable, but the satellite was tested at DKB: a 200 €
    # stock order there costs 10 € (5 %), so that money goes to the core.
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id, plans=[], overnight=2_500.0, buying_power="0")
    upsert_public_settings(db, {"emergency_reserve_eur": 500, "plan_min_order_eur": 100,
                                "monthly_contribution_eur": 0})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": False, "tilt_reason": "locked", "satellite_unlocked": True, "satellite_reason": "",
        "satellite_broker": "dkb",
    })

    plan = build_monthly_plan(db, user_id, now=NOW)

    one_offs = [(a["sleeve"], a["amount_eur"], a["broker"]) for a in plan["actions"] if a["kind"] == "one_off"]
    assert one_offs == [("core", 2_000.0, "scalable")]


def test_manual_orders_with_a_plan_into_a_locked_sleeve_are_not_a_no_change_month():
    db = _memory_db()
    user_id = _user(db)
    _scalable(db, user_id)
    upsert_public_settings(db, {"plan_contribution_mode": "manual_orders", "monthly_contribution_eur": 200})

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["no_change"] is False
    assert plan["headline"] == (
        "Buy 200 € of EUNL.DE at Scalable Capital. 40 € a month of your savings plans goes into a sleeve "
        "that is still locked."
    )


def test_a_sale_names_the_depot_when_one_broker_holds_the_fund_twice(monkeypatch):
    db = _memory_db()
    user_id = _user(db)
    _dkb(db, user_id, positions=((MSCI_WORLD, "iShares Core MSCI World", 70_000.0, 50_000.0),))
    second = DkbAccount(user_id=user_id, type="depot", iban="DE00000000000000004711", balance=0)
    db.add(second)
    db.flush()
    first = db.query(DkbAccount).filter_by(user_id=user_id, iban="DE00DEPOT").one()
    for account, value, cost in ((first, 20_000.0, 10_000.0), (second, 10_000.0, 9_500.0)):
        db.add(DkbPosition(
            account_id=account.id, isin=MOMENTUM, name="World Momentum", quantity=Decimal("100"),
            avg_buy_price=Decimal(str(cost / 100)), current_value=Decimal(str(value)),
        ))
    db.commit()
    upsert_public_settings(db, {"plan_tilt_isins": MOMENTUM})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user_id, now=NOW)

    sales = [a for a in plan["actions"] if a["kind"] == "sale"]
    assert sales[0]["account_id"] == second.id
    assert sales[0]["note"].startswith("Sells from your DKB depot …4711 back to the 15 % target.")


def test_each_portfolio_without_buying_power_counts_only_its_own_cash():
    db = _memory_db()
    user_id = _user(db)
    for pid, cash in (("pf1", "42.10"), ("pf2", "100")):
        db.add_all([
            ConnectedAccount(user_id=user_id, source="scalable", external_id=pid, name=f"Scalable {pid}",
                             account_type="depot", balance=Decimal("0"), raw_json="{}"),
            ConnectedAccount(user_id=user_id, source="scalable", external_id=f"{pid}:cash", name=f"cash {pid}",
                             account_type="cash", balance=Decimal(cash)),
        ])
    db.commit()

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["cash"]["broker_cash_eur"] == 142.10  # not each balance once per portfolio


def test_the_pooled_fallback_skips_cash_accounts_another_portfolio_owns():
    db = _memory_db()
    user_id = _user(db)
    db.add_all([
        # pf1 has its buying power read; pf2 has neither buying power nor a
        # cash account of its own, so it falls back to the unowned cash.
        ConnectedAccount(user_id=user_id, source="scalable", external_id="pf1", name="Scalable pf1",
                         account_type="depot", balance=Decimal("0"),
                         raw_json=json.dumps({"cash": {"buying_power": 200.0}})),
        ConnectedAccount(user_id=user_id, source="scalable", external_id="pf1:cash", name="cash pf1",
                         account_type="cash", balance=Decimal("200")),
        ConnectedAccount(user_id=user_id, source="scalable", external_id="pf2", name="Scalable pf2",
                         account_type="depot", balance=Decimal("0"), raw_json="{}"),
        ConnectedAccount(user_id=user_id, source="scalable", external_id="legacy", name="cash",
                         account_type="cash", balance=Decimal("50")),
    ])
    db.commit()

    plan = build_monthly_plan(db, user_id, now=NOW)

    assert plan["cash"]["broker_cash_eur"] == 250.0  # pf1's 200 once, plus the unowned 50


def test_a_hand_entered_holdings_cost_is_converted_to_eur_like_its_value(monkeypatch):
    from app.foundation.models.entities import Holding, Portfolio

    db = _memory_db()
    user_id = _user(db)
    portfolio = Portfolio(user_id=user_id, name="Main Portfolio", currency="EUR")
    db.add(portfolio)
    db.flush()
    db.add(Holding(portfolio_id=portfolio.id, isin="US0378331005", ticker=None, name="Apple", asset_type="stock",
                   quantity=Decimal("10"), avg_buy_price=Decimal("100"), currency="USD", source="manual"))
    db.commit()
    monkeypatch.setattr(monthly_plan, "fx_convert",
                        lambda amount, src, dst, db: amount * 0.9 if src == "USD" and dst == "EUR" else amount)

    rows, _ = monthly_plan._holdings(db, user_id, None)

    apple = next(r for r in rows if r["isin"] == "US0378331005")
    assert apple["cost_eur"] == pytest.approx(900.0)
