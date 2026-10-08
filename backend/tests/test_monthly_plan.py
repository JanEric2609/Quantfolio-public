"""Tests for the "This month" plan (decision/monthly_plan.py + GET /api/plan/month)."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import monthly_plan
from app.decision.monthly_plan import allocate_contribution, build_monthly_plan, tracking_error_label
from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbPosition, Holding, Portfolio, PriceCache, User
from app.foundation.settings import upsert_public_settings

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> User:
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _position(db, user_id: str, isin: str, name: str, value: float, ticker: str | None = None) -> None:
    account = db.query(DkbAccount).filter(DkbAccount.user_id == user_id).first()
    if account is None:
        account = DkbAccount(user_id=user_id, type="depot", iban="DE00DEPOT", balance=0)
        db.add(account)
        db.flush()
    db.add(DkbPosition(
        account_id=account.id, isin=isin, ticker=ticker, name=name,
        quantity=Decimal("1"), current_value=Decimal(str(value)),
        last_synced=datetime(2026, 9, 30, 18, 0, tzinfo=UTC),
    ))
    db.commit()


def _sleeve(plan: dict, key: str) -> dict:
    return next(s for s in plan["sleeves"] if s["key"] == key)


# --- allocation arithmetic ---------------------------------------------------


def test_allocation_fills_shortfalls_pro_rata_when_money_is_short():
    # Book 36k all core; tilt target 15 % of 37k = 5,550 shortfall > 1,000.
    split = allocate_contribution(
        {"core": 36_000.0, "tilt": 0.0, "satellite": 0.0},
        {"core": 85.0, "tilt": 15.0, "satellite": 0.0},
        1_000.0,
    )
    assert split == pytest.approx({"core": 0.0, "tilt": 1_000.0, "satellite": 0.0})


def test_allocation_spreads_leftover_by_target_weight():
    split = allocate_contribution(
        {"core": 850.0, "tilt": 150.0, "satellite": 0.0},
        {"core": 85.0, "tilt": 15.0, "satellite": 0.0},
        1_000.0,
    )
    assert split == pytest.approx({"core": 850.0, "tilt": 150.0, "satellite": 0.0})
    assert sum(split.values()) == pytest.approx(1_000.0)


def test_allocation_never_funds_a_zero_target_sleeve():
    split = allocate_contribution(
        {"core": 30_000.0, "tilt": 0.0, "satellite": 6_000.0},
        {"core": 100.0, "tilt": 0.0, "satellite": 0.0},
        1_000.0,
    )
    assert split == pytest.approx({"core": 1_000.0, "tilt": 0.0, "satellite": 0.0})


def test_allocation_with_zero_contribution_is_all_zero():
    split = allocate_contribution({"core": 1.0}, {"core": 100.0}, 0.0)
    assert split == {"core": 0.0}


@pytest.mark.parametrize(("amount", "text"), [(1_000, "1.000 €"), (750, "750 €"), (12_500.4, "12.500 €")])
def test_eur_uses_german_grouping(amount, text):
    assert monthly_plan.eur(amount) == text


@pytest.mark.parametrize(("cap", "label"), [(0, "Tight"), (15, "Tight"), (30, "Moderate"), (50, "Loose")])
def test_tracking_error_label(cap, label):
    assert tracking_error_label(cap) == label


# --- the plan ----------------------------------------------------------------


def test_core_only_book_gets_the_usual_no_change_answer():
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0, ticker="EUNL.DE")

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert plan["month"] == "2026-10"
    assert plan["no_change"] is True
    assert "EUNL.DE" in plan["headline"] and "Nothing else to do" in plan["headline"]
    assert plan["book_eur"] == 36_000.0
    assert plan["actions"] == [{
        "sleeve": "core", "kind": "savings_plan", "amount_eur": 1_000.0,
        "instrument": "iShares Core MSCI World (Acc)", "ticker": "EUNL.DE", "isin": "IE00B4L5Y983",
        "note": (
            "Your savings plan at DKB, 1,50 € per execution (0.15 %), "
            "nothing if it is one of DKB's promotional ETFs."
        ),
        "broker": "dkb", "broker_label": "DKB", "account_id": None,
    }]
    core = _sleeve(plan, "core")
    assert core["current_pct"] == 100.0 and core["target_pct"] == 100.0
    assert core["after_eur"] == 37_000.0
    for key in ("tilt", "satellite"):
        sleeve = _sleeve(plan, key)
        assert sleeve["unlocked"] is False
        assert sleeve["target_pct"] == 0.0
        assert sleeve["contribution_eur"] == 0.0
    assert plan["tracking_error_budget"]["label"] == "Tight"
    assert plan["never_sells"] is True
    assert plan["not_investment_advice"] is True
    assert plan["holdings_synced_at"].startswith("2026-09-30")


def test_other_world_etfs_count_as_core_and_stocks_as_satellite():
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00BK5BQT80", "Vanguard FTSE All-World", 9_000.0)
    _position(db, user.id, "DE0007164600", "SAP SE", 1_000.0)

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert _sleeve(plan, "core")["current_eur"] == 9_000.0
    satellite = _sleeve(plan, "satellite")
    assert satellite["current_pct"] == 10.0
    assert [p["name"] for p in satellite["positions"]] == ["SAP SE"]
    # A locked sleeve keeps its positions and says so; the money still goes to core.
    assert "stay as they are" in satellite["status"]
    assert satellite["contribution_eur"] == 0.0
    assert plan["actions"][0]["sleeve"] == "core" and plan["actions"][0]["amount_eur"] == 1_000.0


def test_settings_drive_contribution_and_extra_core_isins():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"monthly_contribution_eur": 750, "plan_core_isins": "LU0000000001"})
    _position(db, user.id, "LU0000000001", "Some World ETF", 5_000.0)

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert plan["contribution_eur"] == 750.0
    assert "Let your 750 € savings plan" in plan["headline"]
    assert _sleeve(plan, "core")["current_eur"] == 5_000.0
    assert _sleeve(plan, "satellite")["current_eur"] == 0.0


def test_no_holdings_still_answers_with_the_core_savings_plan():
    db = _memory_db()
    user = _user(db)

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert plan["has_holdings"] is False
    assert plan["book_eur"] == 0.0
    assert plan["no_change"] is True
    assert _sleeve(plan, "core")["after_pct"] == 100.0


def test_zero_contribution_has_no_actions():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"monthly_contribution_eur": 0})

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert plan["actions"] == []
    assert plan["no_change"] is False
    assert "nothing to invest" in plan["headline"]


def test_unlocked_tilt_receives_its_shortfall(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0)
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert _sleeve(plan, "tilt")["target_pct"] == 15.0
    assert [(a["sleeve"], a["kind"], a["amount_eur"]) for a in plan["actions"]] == [
        ("tilt", "savings_plan", 1_000.0),
    ]
    assert plan["no_change"] is False


def test_small_satellite_buy_is_routed_to_core(monkeypatch):
    """A stock order below the minimum lump is not worth DKB's flat fee."""
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 9_000.0)
    _position(db, user.id, "DE0007164600", "SAP SE", 600.0)
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": False, "tilt_reason": "locked", "satellite_unlocked": True, "satellite_reason": "",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    # Satellite target 10 % of 10,600 = 1,060 -> shortfall 460 < 1,000 minimum order.
    assert _sleeve(plan, "satellite")["contribution_eur"] == 0.0
    assert _sleeve(plan, "core")["contribution_eur"] == 1_000.0
    assert [a["sleeve"] for a in plan["actions"]] == ["core"]


def test_large_satellite_shortfall_becomes_one_order_with_fee_note(monkeypatch):
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"monthly_contribution_eur": 2_000})
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 30_000.0)
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": False, "tilt_reason": "locked", "satellite_unlocked": True, "satellite_reason": "",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    order = next(a for a in plan["actions"] if a["sleeve"] == "satellite")
    assert order["kind"] == "order"
    assert order["amount_eur"] == 2_000.0
    assert "0.5 %" in order["note"]


# --- API ---------------------------------------------------------------------


def test_month_endpoint_returns_the_plan():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0, ticker="EUNL.DE")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = TestClient(app).get("/api/plan/month")
        assert resp.status_code == 200
        body = resp.json()
        assert body["no_change"] is True
        assert body["actions"][0]["amount_eur"] == 1_000.0
        assert [s["key"] for s in body["sleeves"]] == ["core", "tilt", "satellite"]
        assert body["not_investment_advice"] is True
    finally:
        app.dependency_overrides.clear()


def test_tilt_money_is_shared_across_two_factor_etfs(monkeypatch):
    """Value + momentum means one ETF per leg; neither may be starved."""
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0)
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZ825, IE00BP3QZB59"})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert [(a["sleeve"], a["isin"], a["amount_eur"]) for a in plan["actions"]] == [
        ("tilt", "IE00BP3QZ825", 500.0),
        ("tilt", "IE00BP3QZB59", 500.0),
    ]


def test_tilt_money_goes_to_the_leg_that_is_behind(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0)
    _position(db, user.id, "IE00BP3QZ825", "World Momentum", 1_600.0)
    _position(db, user.id, "IE00BP3QZB59", "World Value", 1_000.0)
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZ825, IE00BP3QZB59"})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    # Tilt legs after: 1,600 + 200 and 1,000 + 800 -> 1,800 each.
    assert [(a["isin"], a["amount_eur"]) for a in plan["actions"]] == [
        ("IE00BP3QZ825", 200.0),
        ("IE00BP3QZB59", 800.0),
    ]
    assert [a["instrument"] for a in plan["actions"]] == ["World Momentum", "World Value"]


# --- evidence gate (Phase 4) ---------------------------------------------------


def _gate_run(db, unlocked: bool, unlocked_by: list[str], reason: str, n_trials: int = 500) -> None:
    from app.foundation.models.entities import EvidenceGateRun

    db.add(EvidenceGateRun(
        region="world", satellite_unlocked=unlocked, unlocked_by=unlocked_by, reason=reason,
        n_trials=n_trials, result_json={}, computed_at=datetime(2026, 9, 25, tzinfo=UTC),
    ))
    db.commit()


def test_satellite_asks_for_the_gate_until_it_has_run():
    db = _memory_db()
    state = monthly_plan.evidence_state(db)
    assert state["satellite_unlocked"] is False
    assert "app.lab.evidence_gate" in state["satellite_reason"]


def test_a_locked_gate_run_keeps_the_satellite_at_zero_and_says_why():
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0)
    _gate_run(db, False, [], "No mined score's after-tax satellite passes the Deflated Sharpe test "
              "(best: scalable:gbm, DSR 0.01).")

    plan = build_monthly_plan(db, user.id, now=NOW)

    satellite = _sleeve(plan, "satellite")
    assert satellite["unlocked"] is False and satellite["target_pct"] == 0.0
    assert "500 trials" in satellite["status"] and "scalable:gbm, DSR 0.01" in satellite["status"]
    assert plan["no_change"] is True


def test_the_latest_gate_run_wins_and_its_broker_sets_the_fee():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"monthly_contribution_eur": 2_000})
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 30_000.0)
    _gate_run(db, False, [], "locked")
    from app.foundation.models.entities import EvidenceGateRun

    db.add(EvidenceGateRun(
        region="world", satellite_unlocked=True, unlocked_by=["scalable:ridge"],
        reason="scalable:ridge pass DSR and the family's PBO is 4%.", n_trials=500, result_json={},
        computed_at=datetime(2026, 9, 26, tzinfo=UTC),
    ))
    db.commit()

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert _sleeve(plan, "satellite")["unlocked"] is True
    order = next(a for a in plan["actions"] if a["sleeve"] == "satellite")
    assert order["amount_eur"] == 2_000.0
    assert "Scalable Capital" in order["note"] and "0,99 €" in order["note"]


# --- drift band (Phase 4) --------------------------------------------------------


def test_drift_sales_need_an_unlocked_sleeve_past_the_band_that_contributions_cannot_fix():
    targets = {"core": 85.0, "tilt": 15.0, "satellite": 0.0}
    unlocked = {"core": True, "tilt": True, "satellite": False}
    # Within the band: 18.8 % against 15 + 5.
    assert monthly_plan.drift_sales({"core": 81_000.0, "tilt": 19_000.0, "satellite": 0.0},
                                    targets, unlocked, 1_000.0, 5.0) == {}
    # Past the band now (27.3 %), but a year of contributions takes it to 13.6 %.
    assert monthly_plan.drift_sales({"core": 7_000.0, "tilt": 3_000.0, "satellite": 0.0},
                                    targets, unlocked, 1_000.0, 5.0) == {}
    # A locked sleeve is never sold, however far off.
    assert monthly_plan.drift_sales({"core": 50_000.0, "tilt": 0.0, "satellite": 50_000.0},
                                    targets, unlocked, 1_000.0, 5.0) == {}
    # Past the band and out of reach: sold back to 15 % of the book after this month.
    sales = monthly_plan.drift_sales({"core": 70_000.0, "tilt": 30_000.0, "satellite": 0.0},
                                     targets, unlocked, 1_000.0, 5.0)
    assert sales == pytest.approx({"tilt": 30_000.0 - 0.15 * 101_000.0})


def test_a_sleeve_past_the_band_is_sold_down_and_the_proceeds_go_to_the_core(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 70_000.0, ticker="EUNL.DE")
    _position(db, user.id, "IE00BP3QZ825", "World Momentum", 30_000.0)
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZ825"})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert [(a["sleeve"], a["kind"], a["amount_eur"]) for a in plan["actions"]] == [
        ("tilt", "sale", 14_850.0),
        ("core", "savings_plan", 1_000.0),
        ("core", "order", 14_850.0),
    ]
    assert plan["never_sells"] is False and plan["no_change"] is False
    assert plan["drift_band_pp"] == 5.0
    assert "sell 14.850 €" in plan["headline"]
    sale = plan["actions"][0]
    assert sale["instrument"] == "World Momentum" and "NV certificate" in sale["note"]
    tilt = _sleeve(plan, "tilt")
    assert tilt["sale_eur"] == 14_850.0 and tilt["after_pct"] == 15.0
    assert "sold" in tilt["status"]
    assert _sleeve(plan, "core")["reinvest_eur"] == 14_850.0


def test_a_wider_band_means_no_sale(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 70_000.0)
    _position(db, user.id, "IE00BP3QZ825", "World Momentum", 30_000.0)
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZ825", "plan_drift_band_pp": 15})
    monkeypatch.setattr(monthly_plan, "evidence_state", lambda db: {
        "tilt_unlocked": True, "tilt_reason": "", "satellite_unlocked": False, "satellite_reason": "locked",
    })

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert all(a["kind"] != "sale" for a in plan["actions"])
    assert plan["never_sells"] is True


# ---------------------------------------------------------------------------
# Contribution mode and broker: the savings plan stopped (2026-09), money is
# now invested by hand and/or at Scalable Capital.
# ---------------------------------------------------------------------------

def test_manual_orders_at_dkb_list_the_core_buy_with_its_fee():
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 36_000.0, ticker="EUNL.DE")
    upsert_public_settings(db, {"plan_contribution_mode": "manual_orders", "monthly_contribution_eur": 500})

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert plan["contribution_mode"] == "manual_orders" and plan["broker"] == "dkb"
    assert plan["no_change"] is True
    assert plan["headline"] == "Buy 500 € of EUNL.DE at DKB. Nothing else to do this month."
    [action] = plan["actions"]
    assert action["kind"] == "order" and action["amount_eur"] == 500.0
    assert action["note"] == "This month's contribution: one order at DKB; the 10 € fee is 2.0 % of it."
    assert "you place the order" in _sleeve(plan, "core")["status"]


def test_manual_orders_at_scalable_cost_99_cents():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"plan_contribution_mode": "manual_orders", "plan_broker": "scalable"})

    plan = build_monthly_plan(db, user.id, now=NOW)

    [action] = plan["actions"]
    assert plan["broker_label"] == "Scalable Capital"
    # The default core (an iShares ETF) is a Prime ETF: free from 250 € at Scalable.
    assert action["note"] == "This month's contribution: one order at Scalable Capital, free of charge for this ETF."


def test_a_savings_plan_at_scalable_is_free():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"plan_broker": "scalable"})

    plan = build_monthly_plan(db, user.id, now=NOW)

    [action] = plan["actions"]
    assert action["kind"] == "savings_plan"
    assert action["note"] == "Your savings plan at Scalable Capital, free of charge."
    assert "savings plan run" in plan["headline"]


def test_unknown_mode_or_broker_falls_back_to_the_savings_plan_at_the_cheapest_broker():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"plan_contribution_mode": "yolo", "plan_broker": "trade_republic"})

    cfg = monthly_plan.plan_settings(db)

    assert cfg["contribution_mode"] == "savings_plan" and cfg["broker"] == "auto"
    # With no depot synced, "auto" means DKB, as before.
    assert build_monthly_plan(db, user.id, now=NOW)["broker"] == "dkb"


@pytest.mark.parametrize("amount, fee", [(500, 10.0), (5_000, 10.0), (5_001, 15.0), (20_000, 15.0), (25_000, 30.0)])
def test_dkb_order_fee_tiers(amount, fee):
    assert monthly_plan.order_fee_eur("dkb", amount) == fee


def test_positions_entered_by_hand_count_toward_the_sleeves():
    """A position bought at Scalable is entered under Portfolio -> Holdings;
    the DKB mirror rows (source dkb_sync) must not be counted twice."""
    db = _memory_db()
    user = _user(db)
    _position(db, user.id, "IE00B4L5Y983", "iShares Core MSCI World", 30_000.0, ticker="EUNL.DE")
    portfolio = Portfolio(user_id=user.id, name="Main Portfolio", currency="EUR")
    db.add(portfolio)
    db.flush()
    db.add(Holding(
        portfolio_id=portfolio.id, isin="IE00B4L5Y983", ticker="IWDA.L", name="iShares Core MSCI World",
        asset_type="etf", quantity=Decimal("100"), avg_buy_price=Decimal("100"), source="dkb_sync",
    ))
    # At Scalable, entered without an ISIN: matched to the core by ticker.
    db.add(Holding(
        portfolio_id=portfolio.id, isin=None, ticker="EUNL.DE", name="iShares Core MSCI World",
        asset_type="etf", quantity=Decimal("10"), avg_buy_price=Decimal("100"), source="manual",
    ))
    db.add(PriceCache(ticker="EUNL.DE", date=datetime(2026, 9, 30).date(), close=Decimal("110"), currency="EUR"))
    # A single stock held at Scalable.
    db.add(Holding(
        portfolio_id=portfolio.id, isin="US0378331005", ticker=None, name="Apple",
        asset_type="stock", quantity=Decimal("5"), avg_buy_price=Decimal("200"), source="manual",
    ))
    db.commit()

    plan = build_monthly_plan(db, user.id, now=NOW)

    assert _sleeve(plan, "core")["current_eur"] == 31_100.0
    assert _sleeve(plan, "satellite")["current_eur"] == 1_000.0
    assert plan["book_eur"] == 32_100.0


def test_satellite_text_shows_the_gate_date_trials_and_goes_stale_after_14_days():
    db = _memory_db()
    _gate_run(db, False, [], "no mined score passed", n_trials=500)  # computed 2026-09-25

    fresh = monthly_plan.evidence_state(db, datetime(2026, 10, 5, tzinfo=UTC))
    assert "500 trials" in fresh["satellite_reason"] and "as of 2026-09-25" in fresh["satellite_reason"]
    assert fresh["satellite_gate"]["stale"] is False

    stale = monthly_plan.evidence_state(db, datetime(2026, 10, 20, tzinfo=UTC))
    assert stale["satellite_gate"]["stale"] is True
    assert "out of date" in stale["satellite_reason"]
