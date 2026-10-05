"""Tax-free gain harvesting under an NV certificate (tax_calc + tax_cockpit + GET /api/tax/harvest)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbPosition, TaxLot, User
from app.foundation.settings import upsert_public_settings
from app.foundation.tax_calc import FUTURE_TAX_RATE, Holding, OpenLot, plan_gain_harvest, tax_free_room
from app.foundation.tax_allowances import compute_gain_harvest

D = Decimal
WORLD = "IE00B4L5Y983"
TODAY = date(2026, 11, 1)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _fee(notional: Decimal) -> Decimal:
    return D("10")


# --- the room ---------------------------------------------------------------------


def test_room_is_grundfreibetrag_plus_pauschbetrag_less_income():
    room = tax_free_room(2026, other_income_eur=D("0"), capital_income_so_far_eur=D("0"))
    assert room.room_eur == D("13348") and not room.grundfreibetrag_assumed
    used = tax_free_room(2026, other_income_eur=D("5000"), capital_income_so_far_eur=D("400"))
    assert used.room_eur == D("7948")
    assert tax_free_room(2026, other_income_eur=D("20000"), capital_income_so_far_eur=D("0")).room_eur == 0


def test_german_family_insurance_caps_the_room_and_spouses_double_it():
    family = tax_free_room(2026, other_income_eur=D("0"), capital_income_so_far_eur=D("0"), german_family_insurance=True)
    assert family.family_insurance_room_eur == D("565") * 12 + 1000
    assert family.room_eur == family.family_insurance_room_eur < family.tax_free_room_eur
    spouse = tax_free_room(2026, other_income_eur=D("0"), capital_income_so_far_eur=D("0"), spouse=True)
    assert spouse.room_eur == D("12348") * 2 + 2000


def test_an_unknown_year_uses_the_latest_grundfreibetrag_and_says_so():
    room = tax_free_room(2031, other_income_eur=D("0"), capital_income_so_far_eur=D("0"))
    assert room.grundfreibetrag_assumed and room.grundfreibetrag_eur == D("12348")


# --- the plan ---------------------------------------------------------------------


def _etf(lots, price="100", tf="0.30", average=False, isin=WORLD):
    return Holding(
        isin=isin, name="World", price_eur=D(price), teilfreistellung_pct=D(tf),
        lots=tuple(OpenLot(date(2020 + i, 1, 1), D(q), D(b)) for i, (q, b) in enumerate(lots)),
        average_cost_only=average,
    )


def test_sale_is_fifo_and_stops_inside_the_room():
    # Oldest lot: 100 units bought for 5,000 (gain 5,000, taxable 3,500 after 30 %).
    # Next: 100 units for 8,000 (gain 2,000, taxable 1,400).
    holding = _etf([("100", "5000"), ("100", "8000")])
    [step] = plan_gain_harvest([holding], D("4200"), _fee, D("10"))
    # The whole first lot, then half the second (700 of its 1,400 taxable).
    assert step.sell_quantity == D("150") and not step.whole_position
    assert step.taxable_gain_eur == D("4200.00")
    assert step.gain_eur == D("6000.00")
    assert step.future_tax_avoided_eur == (D("4200") * FUTURE_TAX_RATE).quantize(D("0.01"))
    # A fee and a 10 bps half-spread on the sale and on the rebuy.
    assert step.trading_cost_eur == D("20") + 2 * D("15000") * D("0.001")


def test_the_whole_position_when_it_fits():
    [step] = plan_gain_harvest([_etf([("100", "5000"), ("100", "8000")])], D("13348"), _fee, D("10"))
    assert step.whole_position and step.taxable_gain_eur == D("4900.00")


def test_average_cost_is_all_or_nothing():
    holding = _etf([("200", "13000")], average=True)
    assert plan_gain_harvest([holding], D("4000"), _fee, D("10")) == []
    [step] = plan_gain_harvest([holding], D("5000"), _fee, D("10"))
    assert step.whole_position and step.sell_quantity == 200


def test_a_gain_too_small_to_pay_for_the_trades_is_skipped():
    assert plan_gain_harvest([_etf([("10", "990")])], D("13348"), _fee, D("10")) == []


def test_the_room_goes_to_the_most_gain_per_euro_first():
    rich = _etf([("100", "2000")], isin="RICH")   # gain 8,000 on 10,000
    thin = _etf([("100", "9000")], isin="THIN")   # gain 1,000 on 10,000
    steps = plan_gain_harvest([thin, rich], D("5000"), _fee, D("10"))
    assert [s.isin for s in steps] == ["RICH"]
    assert steps[0].taxable_gain_eur == D("5000.00")
    # Once the richer gain is used up, the rest goes to the thinner one.
    steps = plan_gain_harvest([thin, rich], D("6000"), _fee, D("10"))
    assert [(s.isin, s.taxable_gain_eur) for s in steps] == [("RICH", D("5600.00")), ("THIN", D("400.00"))]


# --- the cockpit ------------------------------------------------------------------


def _setup(db, *, nv=True, lots=True, insurance="foreign", quantity="200"):
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE00DEPOT", balance=0)
    db.add(account)
    db.flush()
    db.add(DkbPosition(
        account_id=account.id, isin=WORLD, ticker="EUNL.DE", name="iShares Core MSCI World",
        quantity=D(quantity), avg_buy_price=D("65"), current_price=D("100"), current_value=D(quantity) * 100,
        last_synced=datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
    ))
    if lots:
        for acquired, basis in ((date(2021, 1, 4), "5000"), (date(2023, 1, 4), "8000")):
            db.add(TaxLot(
                user_id=user.id, isin=WORLD, symbol="EUNL.DE", fund_class="aktienfonds",
                teilfreistellung_pct=D("0.30"), acquired_at=acquired, quantity_initial=D("100"),
                quantity_remaining=D("100"), cost_basis_eur=D(basis), source="manual",
            ))
    db.commit()
    upsert_public_settings(db, {
        "tax_nv_certificate": nv, "tax_nv_valid_until": "2026-12-31", "tax_health_insurance": insurance,
    })
    return user


def test_no_nv_certificate_means_no_suggestion():
    db = _memory_db()
    user = _setup(db, nv=False)
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)
    assert out["estimate"] is True and out["not_tax_advice"] is True
    assert out["enabled"] is False and out["steps"] == []


def test_the_cockpit_harvests_the_lots_within_the_room():
    db = _memory_db()
    user = _setup(db)
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)

    assert out["estimate"] is True and out["not_tax_advice"] is True and out["enabled"]
    # Co-insured abroad: the German family-insurance limit is applied to be safe.
    assert out["room"]["tax_free_room_eur"] == 13348.0
    assert out["room"]["room_eur"] == 565 * 12 + 1000
    [step] = out["steps"]
    assert step["whole_position"] and step["taxable_gain_eur"] == 4900.0
    assert out["totals"]["future_tax_avoided_eur"] == pytest.approx(4900 * float(FUTURE_TAX_RATE), abs=0.01)
    codes = {w["code"] for w in out["warnings"]}
    assert {"nv_expiring", "foreign_insurance", "return_nv", "settle_in_year"} <= codes
    assert "family_insurance" not in codes


def test_lots_that_do_not_match_the_depot_fall_back_to_average_cost(monkeypatch):
    # Without matching lots the fund class comes from the ETF universe; this
    # used to read the machine-wide justETF cache, so the test depended on
    # whether it was fresh and whole.
    from app.foundation import tax_cockpit

    world = {"isin": WORLD, "name": "iShares Core MSCI World UCITS ETF USD (Acc)", "strategy": "Long-only"}
    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {WORLD: world})
    db = _memory_db()
    user = _setup(db, quantity="250")
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)
    [step] = out["steps"]
    # 250 units at 100 against an average 65: gain 8,750, taxable 6,125.
    assert step["whole_position"] and step["taxable_gain_eur"] == 6125.0
    assert "lots_mismatch" in {w["code"] for w in out["warnings"]}


def test_german_family_insurance_is_warned_and_caps_the_room():
    db = _memory_db()
    user = _setup(db, insurance="de_family")
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)
    assert out["room"]["room_eur"] == 565 * 12 + 1000
    assert "family_insurance" in {w["code"] for w in out["warnings"]}


def test_a_past_year_suggests_nothing():
    db = _memory_db()
    user = _setup(db)
    out = compute_gain_harvest(db, user.id, 2025, today=TODAY)
    assert out["enabled"] and out["steps"] == []
    assert "past_year" in {w["code"] for w in out["warnings"]}


def test_a_future_year_suggests_no_trades_yet():
    # A sale placed now would settle in this year and use this year's room.
    db = _memory_db()
    user = _setup(db)
    upsert_public_settings(db, {"tax_nv_valid_until": "2027-12-31"})
    out = compute_gain_harvest(db, user.id, 2027, today=TODAY)
    assert out["estimate"] is True and out["not_tax_advice"] is True
    assert out["enabled"] and out["steps"] == []
    codes = {w["code"] for w in out["warnings"]}
    assert "future_year" in codes and "settle_in_year" not in codes


def test_harvest_endpoint_and_settings_round_trip():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _setup(db, nv=False)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        bad = client.put("/api/tax/settings", json={"tax_nv_valid_until": "next year"})
        assert bad.status_code == 422
        saved = client.put("/api/tax/settings", json={
            "tax_nv_certificate": True, "tax_nv_valid_until": "2028-12-31", "tax_health_insurance": "foreign",
            "tax_other_income_eur": 0, "tax_bafoeg": False,
        })
        assert saved.status_code == 200
        assert saved.json()["settings"]["tax_nv_valid_until"] == "2028-12-31"
        resp = client.get("/api/tax/harvest", params={"year": date.today().year})
        assert resp.status_code == 200
        body = resp.json()
        assert body["estimate"] is True and body["not_tax_advice"] is True and body["enabled"] is True
    finally:
        app.dependency_overrides.clear()
