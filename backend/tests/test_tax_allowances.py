"""Freistellungsauftrag per bank, NV certificate per bank, DKB interest, Vorabpauschale year."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    DkbTransaction,
    TaxLedgerEvent,
    TaxLot,
    User,
)
from app.foundation.settings import upsert_public_settings
from app.foundation.tax_allowances import compute_allowance_plan, nv_status
from app.foundation.tax_calc.jurisdictions.de.allowance_split import (
    BankYear,
    nv_eligibility,
    plan_split,
    withholding_rate,
)

TODAY = date(2026, 10, 2)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _bank(bank, fsa, booked, expected, *, nv=False, harvest="0"):
    return BankYear(bank=bank, label=bank.upper(), fsa_eur=None if fsa is None else D(fsa), nv_filed=nv,
                    booked_eur=D(booked), expected_rest_eur=D(expected), harvestable_eur=D(harvest))


# --- the pure split ------------------------------------------------------------


def test_split_covers_projected_income_and_withholds_less():
    plan = plan_split([_bank("dkb", "500", "100", "200"), _bank("scalable", "500", "400", "500")],
                      allowance_eur=D("1000"))
    dkb, sc = plan.banks
    rate = withholding_rate()
    assert sc.projected_withheld_eur == (D("400") * rate).quantize(D("0.01"))
    assert dkb.projected_withheld_eur == 0
    assert dkb.recommended_fsa_eur + sc.recommended_fsa_eur == 1000
    # Income (1,200) exceeds the allowance, so 200 stays uncovered whatever the split.
    assert plan.recommended_withheld_eur == (D("200") * rate).quantize(D("0.01"))
    assert dkb.recommended_fsa_eur >= dkb.minimum_fsa_eur == 100
    assert sc.recommended_fsa_eur >= sc.minimum_fsa_eur == 400


def test_nv_at_one_bank_moves_the_whole_allowance_to_the_other():
    plan = plan_split([_bank("dkb", "500", "100", "200", nv=True), _bank("scalable", "500", "400", "500")],
                      allowance_eur=D("1000"), nv_valid=True)
    dkb, sc = plan.banks
    assert dkb.nv_covers and not sc.nv_covers
    assert dkb.projected_withheld_eur == 0
    assert sc.projected_withheld_eur > 0
    assert (dkb.recommended_fsa_eur, sc.recommended_fsa_eur) == (0, 1000)
    assert plan.recommended_withheld_eur == 0


def test_an_expired_nv_certificate_covers_nothing():
    plan = plan_split([_bank("dkb", "0", "300", "0", nv=True)], allowance_eur=D("1000"), nv_valid=False)
    assert not plan.banks[0].nv_covers
    assert plan.banks[0].projected_withheld_eur > 0


def test_used_allowance_cannot_be_moved_and_over_assignment_is_flagged():
    plan = plan_split([_bank("dkb", "800", "50", "0"), _bank("scalable", "500", "450", "300")],
                      allowance_eur=D("1000"))
    assert plan.over_assigned
    dkb, sc = plan.banks
    assert sc.minimum_fsa_eur == 450 and dkb.minimum_fsa_eur == 50
    assert dkb.recommended_fsa_eur + sc.recommended_fsa_eur <= 1000
    # Scalable's projected 750 is covered first; the spare 200 stays with the bank with the most income.
    assert sc.recommended_fsa_eur == 950 and dkb.recommended_fsa_eur == 50


def test_spare_allowance_goes_where_gains_can_be_harvested():
    plan = plan_split([_bank("dkb", "500", "50", "0"), _bank("scalable", "500", "50", "0", harvest="3000")],
                      allowance_eur=D("1000"), other_banks_eur=D("100"))
    dkb, sc = plan.banks
    assert plan.available_eur == 900
    assert (dkb.recommended_fsa_eur, sc.recommended_fsa_eur) == (50, 850)


def test_idle_orders_at_other_banks_still_count_against_the_allowance():
    # 1,000 on file elsewhere, none of it used: nothing is left for DKB and Scalable.
    plan = plan_split([_bank("dkb", "0", "0", "0"), _bank("scalable", "0", "0", "0", harvest="500")],
                      allowance_eur=D("1000"), other_banks_eur=D("1000"))
    assert plan.available_eur == 0
    assert all(b.recommended_fsa_eur == 0 for b in plan.banks)


def test_the_cockpit_takes_the_orders_at_other_banks_not_just_what_they_used():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_dkb_eur=0, freistellungsauftrag_scalable_eur=0,
               freistellungsauftrag_used=50, freistellungsauftrag_other_banks_eur=600)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["other_banks_eur"] == 600 and out["available_eur"] == 400
    assert sum(b["recommended_fsa_eur"] for b in out["banks"]) <= 400
    # Without the orders entered, the allowance used elsewhere is the floor.
    upsert_public_settings(db, {"freistellungsauftrag_other_banks_eur": None})
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["other_banks_eur"] == 50 and out["available_eur"] == 950


def test_nv_eligibility_uses_grundfreibetrag_plus_allowance():
    ok = nv_eligibility(2026, other_income_eur=D("0"), capital_income_eur=D("5000"))
    assert ok.limit_eur == D("13348") and ok.likely_eligible
    assert not nv_eligibility(2026, other_income_eur=D("13000"), capital_income_eur=D("500")).likely_eligible
    assert nv_eligibility(2026, other_income_eur=D("0"), capital_income_eur=D("0"), spouse=True).limit_eur == D("26696")


# --- the cockpit ------------------------------------------------------------------


def _user(db) -> User:
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _two_banks(db, user, **settings):
    db.add(DkbAccount(user_id=user.id, type="tagesgeld", iban="DE00TG", balance=D("10000")))
    db.add(ConnectedAccount(user_id=user.id, source="scalable", external_id="pf:overnight", name="overnight",
                            account_type="savings", balance=D("20000")))
    db.add(ConnectedAccount(user_id=user.id, source="scalable", external_id="pf", name="depot",
                            account_type="depot", balance=D("0"),
                            raw_json=json.dumps({"overnight": {"interest_rate": "2.6"}})))
    for source, amount in (("scalable_sync", "300"), ("dkb_sync", "100")):
        db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 6, 30), event_type="interest",
                              gross_eur=D(amount), source=source, source_ref=f"{source}:1"))
    db.commit()
    upsert_public_settings(db, {
        "freistellungsauftrag_dkb_eur": 500, "freistellungsauftrag_scalable_eur": 500,
        "tax_interest_rate_dkb": 0.02, **settings,
    })


def test_nv_filed_only_at_dkb_tells_you_to_send_it_to_scalable():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, tax_nv_certificate=True, tax_nv_valid_until="2027-12-31", tax_nv_filed_dkb=True)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["estimate"] is True and out["not_tax_advice"] is True and out["applicable"]
    dkb, sc = out["banks"]
    assert dkb["bank"] == "dkb" and dkb["nv_covers"] and not sc["nv_covers"]
    assert sc["booked"]["interest_eur"] == 300
    # 20,000 at 2.6 % for the rest of the year (90 of 365 days left after 2 October).
    assert sc["expected"]["interest_basis"] == "rate"
    assert sc["expected"]["interest_eur"] == pytest.approx(20000 * 0.026 * 90 / 365, abs=0.05)
    assert sc["projected_eur"] == pytest.approx(300 + 20000 * 0.026 * 90 / 365, abs=0.05)
    action = next(a for a in out["actions"] if a["code"] == "nv_missing_at_bank")
    assert action["bank"] == "scalable" and action["deadline"] == "2026-12-15"
    assert "Seitzstraße 8e" in action["how_to"]
    assert out["sources"]


def test_without_nv_the_plan_suggests_a_better_split():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_dkb_eur=800, freistellungsauftrag_scalable_eur=200)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    dkb, sc = out["banks"]
    assert sc["projected_withheld_eur"] > 0
    assert sc["recommended_fsa_eur"] > 200 and dkb["recommended_fsa_eur"] < 800
    assert out["recommended_withheld_eur"] < out["projected_withheld_eur"]
    resplit = next(a for a in out["actions"] if a["code"] == "fsa_resplit")
    assert resplit["deadline"] == "2026-12-31"
    assert any(a["code"] == "nv_apply" for a in out["actions"])  # no other income: an NV is plausible


def test_legacy_nv_flag_without_banks_assumes_every_bank_and_asks():
    settings = {"tax_nv_certificate": True, "tax_nv_valid_until": "2026-12-31"}
    status = nv_status(settings, 2026)
    assert status["valid_for_year"] and status["ends_this_year"] and not status["banks_known"]
    assert status["filed"] == {"dkb": True, "scalable": True}
    assert not nv_status({**settings, "tax_nv_valid_until": "2025-12-31"}, 2026)["valid_for_year"]


def test_losses_at_one_bank_and_income_at_the_other_ask_for_a_verlustbescheinigung():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 3, 1), event_type="sale",
                          realised_gain_eur=D("-2000"), bucket="aktien", source="manual", institution="dkb"))
    db.commit()
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    action = next(a for a in out["actions"] if a["code"] == "verlustbescheinigung")
    assert action["bank"] == "dkb" and action["deadline"] == "2026-12-15"


def test_a_non_german_residence_is_not_applicable():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"tax_residency_country": "NL"})
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["applicable"] is False and "DE" in out["reason"]
    assert out["estimate"] is True and out["not_tax_advice"] is True


def test_allowance_endpoint_and_event_bank_round_trip():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        body = client.get("/api/tax/allowances", params={"year": 2026}).json()
        assert body["estimate"] is True and body["not_tax_advice"] is True and body["applicable"] is True
        assert [b["bank"] for b in body["banks"]] == ["dkb", "scalable"]
        # 500 at DKB + 700 at Scalable is more than the 1,000 € allowance.
        over = client.put("/api/tax/settings", json={"freistellungsauftrag_scalable_eur": 700})
        assert over.status_code == 422, over.json()
        assert "1,000" in str(over.json()), over.json()
        saved = client.put("/api/tax/settings", json={"freistellungsauftrag_scalable_eur": 400,
                                                       "tax_nv_filed_scalable": True})
        assert saved.json()["settings"]["freistellungsauftrag_scalable_eur"] == 400
        assert client.put("/api/tax/settings", json={"tax_nv_valid_until": "2027-06-30"}).status_code == 422
        assert client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": 5000}).status_code == 422
        created = client.post("/api/tax/events", json={
            "event_type": "dividend", "event_date": "2026-05-02", "gross_eur": 40, "institution": " Scalable "})
        assert created.status_code == 200
        events = client.get("/api/tax/events", params={"year": 2026}).json()["items"]
        assert {e["institution"] for e in events} == {"scalable", "dkb"}
    finally:
        app.dependency_overrides.clear()


# --- DKB interest ingestion ---------------------------------------------------------


def test_dkb_interest_credits_become_interest_events():
    from app.foundation.dkb.dividend_ingestion import ingest_dkb_dividends, is_interest_credit

    assert is_interest_credit("Abschluss per 30.09.2026", "tagesgeld")
    assert is_interest_credit("Habenzinsen 3. Quartal", "giro")
    assert not is_interest_credit("Zinsen Darlehen Max", "giro")
    assert not is_interest_credit("Ertrag IE00B4L5Y983 Zinsen", "tagesgeld")

    db = _memory_db()
    user = _user(db)
    savings = DkbAccount(user_id=user.id, type="tagesgeld", iban="DE00TG", balance=D("1000"))
    db.add(savings)
    db.flush()
    db.add(DkbTransaction(account_id=savings.id, date=date(2026, 9, 30), amount=D("12.34"), currency="EUR",
                          reference="Abschluss Zinsen", source="dkb", dedupe_hash="h1"))
    db.commit()
    result = ingest_dkb_dividends(db, user.id)
    assert result["ingested_interest"] == 1
    event = db.query(TaxLedgerEvent).one()
    assert (event.event_type, event.institution, event.gross_eur) == ("interest", "dkb", D("12.34"))
    assert ingest_dkb_dividends(db, user.id)["skipped_duplicates"] == 1


# --- Vorabpauschale belongs to the following tax year (Sec. 18(3) InvStG) ----------


def test_year_overview_takes_the_vorabpauschale_of_the_previous_fund_year(monkeypatch):
    from app.foundation import tax_cockpit

    seen = []

    def fake(db, user_id, year):
        seen.append(year)
        return [], D("0")

    monkeypatch.setattr(tax_cockpit, "_estimate_holdings_vorabpauschale", fake)
    db = _memory_db()
    user = _user(db)
    out = tax_cockpit.compute_year_overview(db, user.id, 2026)
    assert seen == [2025]
    assert out["vorabpauschale_estimate"]["fund_year"] == 2025


# --- harvesting without an NV certificate ---------------------------------------------


def test_harvest_without_nv_stays_inside_the_banks_allowance():
    from app.foundation.tax_allowances import compute_gain_harvest

    db = _memory_db()
    user = _user(db)
    depot = DkbAccount(user_id=user.id, type="depot", iban="DE00DEPOT", balance=0)
    db.add(depot)
    db.flush()
    isin = "IE00B4L5Y983"
    db.add(DkbPosition(account_id=depot.id, isin=isin, ticker="EUNL.DE", name="World", quantity=D("100"),
                       avg_buy_price=D("50"), current_price=D("100"), current_value=D("10000"),
                       last_synced=datetime(2026, 10, 1, tzinfo=UTC)))
    db.add(TaxLot(user_id=user.id, isin=isin, fund_class="aktienfonds", teilfreistellung_pct=D("0.30"),
                  acquired_at=date(2021, 1, 4), quantity_initial=D("100"), quantity_remaining=D("100"),
                  cost_basis_eur=D("5000"), source="manual"))
    db.commit()
    upsert_public_settings(db, {"freistellungsauftrag_dkb_eur": 1000, "freistellungsauftrag_scalable_eur": 0,
                                "tax_other_income_eur": 20000})
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)
    assert out["enabled"] and out["mode"] == "allowance"
    assert out["estimate"] is True and out["not_tax_advice"] is True
    [step] = out["steps"]
    assert step["bank"] == "dkb"
    assert step["taxable_gain_eur"] <= 1000
    assert {"bank": "dkb", "label": "DKB", "nv_covers": False, "room_eur": 1000.0} in out["bank_rooms"]


def test_leap_day_does_not_break_last_years_window():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    out = compute_allowance_plan(db, user.id, 2028, today=date(2028, 2, 29))
    assert out["applicable"] and out["estimate"] is True and out["not_tax_advice"] is True


def test_moving_an_idle_order_under_nv_says_to_restore_it_later():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, tax_nv_certificate=True, tax_nv_valid_until="2026-12-31", tax_nv_filed_dkb=True,
               freistellungsauftrag_dkb_eur=600, freistellungsauftrag_scalable_eur=400)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    resplit = next(a for a in out["actions"] if a["code"] == "fsa_resplit")
    assert "DKB" in resplit["message"] and "certificate ends" in resplit["message"]
    assert any(a["code"] == "nv_ends" for a in out["actions"])


def test_events_at_another_bank_count_in_the_total_but_not_as_missing_a_bank():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 4, 1), event_type="interest",
                          gross_eur=D("80"), source="manual", institution="other"))
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 4, 2), event_type="dividend",
                          gross_eur=D("20"), source="manual"))
    db.commit()
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["other_banks_income_eur"] == 80
    assert out["unassigned"]["events"] == 1
    banks_total = sum(b["projected_eur"] for b in out["banks"])
    assert out["projected_capital_income_eur"] == pytest.approx(banks_total + 80 + 20, abs=0.02)


# --- review round 1 (PR 306) ------------------------------------------------------


def test_used_allowance_is_rounded_up_never_down():
    plan = plan_split([_bank("dkb", "500", "10.25", "0")], allowance_eur=D("1000"))
    assert plan.banks[0].minimum_fsa_eur == 11
    assert plan.banks[0].recommended_fsa_eur >= 11


def test_explicit_no_at_both_banks_is_not_the_legacy_case():
    settings = {"tax_nv_certificate": True, "tax_nv_valid_until": "2027-12-31",
                "tax_nv_filed_dkb": False, "tax_nv_filed_scalable": False}
    status = nv_status(settings, 2026)
    assert status["banks_known"] and status["filed"] == {"dkb": False, "scalable": False}
    legacy = nv_status({**settings, "tax_nv_filed_dkb": None, "tax_nv_filed_scalable": None}, 2026)
    assert not legacy["banks_known"] and legacy["filed"] == {"dkb": True, "scalable": True}


def test_an_nv_certificate_ending_mid_year_does_not_cover_the_year():
    status = nv_status({"tax_nv_certificate": True, "tax_nv_valid_until": "2026-06-30",
                        "tax_nv_filed_dkb": True}, 2026)
    assert not status["valid_for_year"] and status["ends_midyear"]
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, tax_nv_certificate=True, tax_nv_valid_until="2026-06-30", tax_nv_filed_dkb=True)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert not any(b["nv_covers"] for b in out["banks"])
    assert any(a["code"] == "nv_ends_midyear" for a in out["actions"])


def test_vorabpauschale_counts_from_the_first_working_day():
    from app.foundation.tax_allowances import first_working_day

    assert first_working_day(2027) == date(2027, 1, 4)  # 2 and 3 January 2027 are a weekend
    assert first_working_day(2026) == date(2026, 1, 2)


def test_net_credits_above_the_order_are_grossed_up():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_scalable_eur=100)
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    sc = out["banks"][1]
    rate = float(withholding_rate())
    # 300 booked net at Scalable, order 100: the 200 above it were credited after tax.
    assert sc["booked"]["withheld_grossed_up_eur"] == pytest.approx(200 * rate / (1 - rate), abs=0.02)
    assert sc["booked_eur"] == pytest.approx(300 + 200 * rate / (1 - rate), abs=0.02)


def _sale(db, user, when, gain, bank="scalable"):
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=when, event_type="sale",
                          realised_gain_eur=D(gain), bucket="sonstige", source="manual", institution=bank))
    db.commit()


def test_a_credit_booked_before_the_order_ran_out_is_not_grossed_up():
    # 300 interest on 30 June with 1,000 on file; a 2,000 gain in July used the order up later.
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_scalable_eur=1000)
    _sale(db, user, date(2026, 7, 15), "2000")
    sc = compute_allowance_plan(db, user.id, 2026, today=TODAY)["banks"][1]
    assert sc["booked"]["withheld_grossed_up_eur"] == 0
    assert sc["booked_eur"] == pytest.approx(2300, abs=0.01)


def test_a_credit_booked_after_the_order_ran_out_is_grossed_up():
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_scalable_eur=1000)
    _sale(db, user, date(2026, 3, 1), "2000")
    sc = compute_allowance_plan(db, user.id, 2026, today=TODAY)["banks"][1]
    rate = float(withholding_rate())
    assert sc["booked"]["withheld_grossed_up_eur"] == pytest.approx(300 * rate / (1 - rate), abs=0.02)


def test_allowance_harvest_room_ignores_income_at_other_banks():
    from app.foundation.tax_allowances import compute_gain_harvest

    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, freistellungsauftrag_dkb_eur=500, freistellungsauftrag_scalable_eur=400,
               freistellungsauftrag_other_banks_eur=100, tax_interest_rate_dkb=0)
    # 80 at a third bank, under that bank's own 100 order.
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 5, 1), event_type="interest",
                          gross_eur=D("80"), source="manual", institution="other"))
    db.commit()
    out = compute_gain_harvest(db, user.id, 2026, today=TODAY)
    assert out["mode"] == "allowance"
    rooms = {r["bank"]: r["room_eur"] for r in out["bank_rooms"]}
    assert out["room"]["room_eur"] == pytest.approx(rooms["dkb"] + rooms["scalable"], abs=0.01)


def test_a_booked_vorabpauschale_is_not_estimated_again(monkeypatch):
    from app.foundation import tax_allowances

    isin = "IE00B4L5Y983"
    monkeypatch.setattr(tax_allowances, "_estimate_holdings_vorabpauschale", lambda db, user_id, year: (
        [{"isin": isin, "taxable_after_teilfreistellung_eur": 50.0}], D("50")))
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 1, 2), event_type="vorabpauschale",
                          isin=isin, gross_eur=D("50"), source="manual"))
    db.commit()
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    assert out["unassigned"]["vorabpauschale_eur"] == 0


def test_per_bank_settings_belong_to_the_user_who_set_them():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    me = _user(db)
    other = User(username="other", password_hash="x")
    db.add(other)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: me
    try:
        client = TestClient(app)
        assert client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": 400}).status_code == 200
        # null resets to "not entered"
        cleared = client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": None})
        assert cleared.json()["settings"]["freistellungsauftrag_dkb_eur"] is None
        assert client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": 400}).status_code == 200
        app.dependency_overrides[current_user] = lambda: other
        assert client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": 900}).status_code == 403
        assert client.put("/api/settings", json={"settings": {"tax_nv_filed_dkb": True}}).status_code == 403
        # Re-sending the stored value is not a change; other tax settings stay shared.
        assert client.put("/api/tax/settings", json={"freistellungsauftrag_dkb_eur": 400}).status_code == 200
        plan = client.get("/api/tax/allowances", params={"year": 2026}).json()
        assert plan["banks"][0]["fsa_eur"] is None
        assert any(a["code"] == "bank_settings_other_user" for a in plan["actions"])
        app.dependency_overrides[current_user] = lambda: me
        assert client.get("/api/tax/allowances", params={"year": 2026}).json()["banks"][0]["fsa_eur"] == 400
    finally:
        app.dependency_overrides.clear()


def test_a_fund_first_bought_after_the_fund_year_has_no_vorabpauschale_for_it(monkeypatch):
    from app.foundation import tax_cockpit
    from app.foundation.models.entities import Holding, Portfolio, PriceCache

    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {})
    db = _memory_db()
    user = _user(db)
    pf = Portfolio(user_id=user.id, name="Main")
    db.add(pf)
    db.commit()
    for isin, ticker in (("IE00B4L5Y983", "IWDA"), ("IE00BK5BQT80", "VWCE")):
        db.add(Holding(portfolio_id=pf.id, isin=isin, ticker=ticker, name=ticker, asset_type="etf",
                       quantity=D("10"), avg_buy_price=D("90")))
        now = datetime.now(UTC)
        db.add(PriceCache(ticker=ticker, date=date(2025, 1, 2), close=D("100"), fetched_at=now))
        db.add(PriceCache(ticker=ticker, date=date(2025, 12, 30), close=D("120"), fetched_at=now))
    # VWCE was first bought in 2026: no Vorabpauschale for fund year 2025.
    db.add(TaxLot(user_id=user.id, isin="IE00BK5BQT80", fund_class="aktienfonds", teilfreistellung_pct=D("0.30"),
                  acquired_at=date(2026, 3, 1), quantity_initial=D("10"), quantity_remaining=D("10"),
                  cost_basis_eur=D("900"), source="manual"))
    db.commit()
    items, _total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, 2025)
    assert [i["ticker"] for i in items] == ["IWDA"]


# --- review round 2 (PR 306) ------------------------------------------------------


def test_the_allowance_only_shelters_capital_income():
    assert not nv_eligibility(2026, other_income_eur=D("13000"), capital_income_eur=D("0")).likely_eligible
    assert nv_eligibility(2026, other_income_eur=D("12000"), capital_income_eur=D("1300")).likely_eligible
    assert not nv_eligibility(2026, other_income_eur=D("12000"), capital_income_eur=D("1400")).likely_eligible


def test_church_tax_lowers_the_kest_in_the_withholding_rate():
    assert withholding_rate() == D("0.26375")
    assert withholding_rate(D("0.08")) == pytest.approx(D("1.135") / D("4.08"))
    assert float(withholding_rate(D("0.09"))) == pytest.approx(0.2799, abs=1e-4)


def test_a_booked_vorabpauschale_at_one_bank_keeps_the_other_banks_share(monkeypatch):
    from app.foundation import tax_allowances
    from app.foundation.models.entities import BrokerPosition

    isin = "IE00B4L5Y983"
    monkeypatch.setattr(tax_allowances, "_estimate_holdings_vorabpauschale", lambda db, user_id, year: (
        [{"isin": isin, "taxable_after_teilfreistellung_eur": 100.0}], D("100")))
    db = _memory_db()
    user = _user(db)
    _two_banks(db, user)
    depot = DkbAccount(user_id=user.id, type="depot", iban="DE00DEPOT", balance=0)
    db.add(depot)
    db.flush()
    db.add(DkbPosition(account_id=depot.id, isin=isin, name="World", quantity=D("10"), current_price=D("100")))
    sc_depot = db.query(ConnectedAccount).filter(ConnectedAccount.external_id == "pf").one()
    db.add(BrokerPosition(user_id=user.id, connected_account_id=sc_depot.id, source="scalable", isin=isin,
                          name="World", quantity=D("30"), currency="EUR"))
    db.add(TaxLedgerEvent(user_id=user.id, tax_year=2026, event_date=date(2026, 1, 2), event_type="vorabpauschale",
                          isin=isin, gross_eur=D("25"), source="manual", institution="dkb"))
    db.commit()
    out = compute_allowance_plan(db, user.id, 2026, today=TODAY)
    dkb, sc = out["banks"]
    assert dkb["vorabpauschale_estimated_eur"] == 0  # booked there already
    assert sc["vorabpauschale_estimated_eur"] == pytest.approx(75, abs=0.01)  # 30 of 40 units


def test_another_user_does_not_inherit_the_nv_certificate():
    from app.foundation.tax_allowances import claim_bank_settings, settings_for_user

    db = _memory_db()
    me = _user(db)
    other = User(username="other", password_hash="x")
    db.add(other)
    db.commit()
    changes = claim_bank_settings(db, me.id, {"tax_nv_certificate": True, "tax_nv_valid_until": "2027-12-31"})
    upsert_public_settings(db, changes)
    assert settings_for_user(db, me.id)["tax_nv_certificate"] is True
    theirs = settings_for_user(db, other.id)
    assert theirs["tax_nv_certificate"] is False and theirs["tax_nv_valid_until"] is None


def test_a_racing_first_claim_loses_cleanly(monkeypatch):
    from app.foundation import tax_allowances
    from app.foundation.tax_allowances import BankSettingsOwnedByOtherUser, claim_bank_settings

    db = _memory_db()
    me = _user(db)
    other = User(username="other", password_hash="x")
    db.add(other)
    db.commit()
    claim_bank_settings(db, me.id, {"freistellungsauftrag_dkb_eur": 500})
    # The other request read the settings before the claim landed.
    stale = {k: v for k, v in tax_allowances.get_public_settings(db).items()
             if k != tax_allowances.BANK_SETTINGS_OWNER_KEY}
    monkeypatch.setattr(tax_allowances, "get_public_settings", lambda _db: stale)
    with pytest.raises(BankSettingsOwnedByOtherUser):
        claim_bank_settings(db, other.id, {"freistellungsauftrag_dkb_eur": 900})


def test_a_fund_bought_back_during_the_year_is_prorated(monkeypatch):
    from app.foundation import tax_cockpit
    from app.foundation.models.entities import Holding, Portfolio, PriceCache

    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {})
    db = _memory_db()
    user = _user(db)
    pf = Portfolio(user_id=user.id, name="Main")
    db.add(pf)
    db.commit()
    isin = "IE00B4L5Y983"
    db.add(Holding(portfolio_id=pf.id, isin=isin, ticker="IWDA", name="IWDA", asset_type="etf",
                   quantity=D("10"), avg_buy_price=D("90")))
    now = datetime.now(UTC)
    db.add(PriceCache(ticker="IWDA", date=date(2025, 1, 2), close=D("100"), fetched_at=now))
    db.add(PriceCache(ticker="IWDA", date=date(2025, 12, 30), close=D("120"), fetched_at=now))
    for acquired, remaining in ((date(2021, 1, 4), "0"), (date(2025, 7, 1), "10")):
        db.add(TaxLot(user_id=user.id, isin=isin, fund_class="aktienfonds", teilfreistellung_pct=D("0.30"),
                      acquired_at=acquired, quantity_initial=D("10"), quantity_remaining=D(remaining),
                      cost_basis_eur=D("900"), source="manual"))
    db.commit()
    [item] = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, 2025)[0]
    assert item["months_held"] == 6


def test_tax_position_returns_what_a_bank_without_nv_withholds():
    """NV at DKB only, Scalable's order too small: Scalable withholds, nothing is owed."""
    from app.foundation.tax_allowances import tax_position

    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, tax_nv_certificate=True, tax_nv_valid_until="2027-12-31", tax_nv_filed_dkb=True,
               tax_nv_filed_scalable=False, freistellungsauftrag_scalable_eur=100)
    pos = tax_position(db, user.id, 2026, today=TODAY)
    assert pos["estimate"] is True and pos["not_tax_advice"] is True and pos["applicable"]
    assert pos["final_tax_eur"] == 0
    assert pos["withheld_by_banks_eur"] > 0
    assert pos["refund_with_return_eur"] == pos["withheld_by_banks_eur"]
    assert "return" in pos["headline"]


def test_tax_position_with_a_salary_is_the_flat_rate():
    from app.foundation.tax_allowances import tax_position

    db = _memory_db()
    user = _user(db)
    _two_banks(db, user, tax_other_income_eur=90000)
    pos = tax_position(db, user.id, 2026, today=TODAY)
    assert pos["use_guenstigerpruefung"] is False
    assert pos["final_tax_eur"] == pos["flat_rate_tax_eur"]
