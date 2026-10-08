"""A2: fund-choice rule, suggest only (ADR 0019 §2)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.monthly_plan import build_monthly_plan
from app.decision.monthly_plan_funds import (
    ACC_DIST_NOTE,
    CANDIDATES,
    FundCandidate,
    score_fund,
    suggest_all,
    suggest_fund,
)
from app.foundation.core.db import Base
from app.foundation.models.entities import User
from app.foundation.settings import upsert_public_settings


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_cheaper_fund_wins_ceteris_paribus():
    base = FundCandidate("core", "IE00X", "X", "MSCI World", 0.20, 10.0, "full", 50.0, True, True)
    pricey = FundCandidate("core", "IE00Y", "Y", "MSCI World", 0.40, 10.0, "full", 50.0, True, True)
    assert score_fund(base) > score_fund(pricey)


def test_core_suggestion_names_a_fund_with_a_reason():
    s = suggest_fund("core")
    assert s.isin and s.name and s.why
    assert "TER" in s.why and s.isin in s.why
    assert s.overridden is False


def test_manual_tilt_override_wins_and_says_so():
    up = suggest_fund("tilt", {"IE00BF4RF909"})
    assert up.isin == "IE00BF4RF909"
    assert up.overridden is True
    assert "manual override" in up.why


def test_unknown_override_is_reported_honestly():
    up = suggest_fund("tilt", {"IE00MADEUP00"})
    assert up.isin == "IE00MADEUP00"
    assert up.overridden is True
    assert "Confirm it tracks" in up.why


def test_satellite_suggests_no_fund():
    s = suggest_fund("satellite")
    assert s.isin is None
    assert "evidence gate" in s.why


def test_plan_response_carries_suggestions_and_acc_dist_note():
    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    plan = build_monthly_plan(db, user.id)
    by_sleeve = {s["sleeve"]: s for s in plan["suggested_funds"]}
    assert set(by_sleeve) == {"core", "tilt", "satellite"}
    assert all(s["why"] for s in plan["suggested_funds"])
    assert "NV-Bescheinigung" in plan["acc_dist_note"]
    assert "Vorabpauschale" in plan["acc_dist_note"]
    assert ACC_DIST_NOTE == plan["acc_dist_note"]


def test_tilt_override_flows_through_the_plan():
    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BF4RF909"})
    plan = build_monthly_plan(db, user.id)
    tilt = next(s for s in plan["suggested_funds"] if s["sleeve"] == "tilt")
    assert tilt["isin"] == "IE00BF4RF909" and tilt["overridden"] is True
    assert len(CANDIDATES) > 0
