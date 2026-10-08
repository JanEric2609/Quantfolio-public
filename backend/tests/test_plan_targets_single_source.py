"""A1: one source for targets (ADR 0019 §1).

The analyzer defines no target of its own; the monthly plan's sleeves are
the single place targets come from, and the drift endpoint reports them.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import monthly_plan
from app.decision.monthly_plan import evidence_state, plan_settings, sleeve_targets_from
from app.decision.portfolio_advisor import analyzer
from app.foundation.core.db import Base
from app.foundation.models.entities import User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_analyzer_defines_no_target_constant():
    assert not hasattr(analyzer, "TARGET_ALLOCATION")


def test_gap_analysis_without_target_computes_no_drift():
    holdings = [
        {"name": "A", "current_value": 60000, "asset_type": "stock"},
        {"name": "B", "current_value": 40000, "asset_type": "bond"},
    ]
    result = analyzer.compute_gap_analysis(holdings)
    assert result["drift"] == []
    assert result["asset_allocation"]


def test_gap_analysis_uses_the_passed_target():
    holdings = [
        {"name": "A", "current_value": 60000, "asset_type": "stock"},
        {"name": "B", "current_value": 30000, "asset_type": "bond"},
        {"name": "C", "current_value": 10000, "asset_type": "cash"},
    ]
    result = analyzer.compute_gap_analysis(
        holdings, target={"stock": 0.5, "bond": 0.3, "cash": 0.2}
    )
    assert any(d["asset_type"] == "stock" and abs(d["drift_pct"] - 10) < 1 for d in result["drift"])


def test_drift_sleeve_targets_equal_plan_sleeve_targets():
    db = _memory_db()
    db.add(User(username="jan", password_hash="x"))
    db.commit()
    user = db.query(User).first()
    assert user is not None
    sleeves = monthly_plan.sleeve_plan(db, user.id)
    expected_targets, _ = sleeve_targets_from(
        plan_settings(db), evidence_state(db),
    )
    assert sleeves["targets"] == expected_targets
    # With no evidence everything stays in the core.
    assert sleeves["targets"] == {"tilt": 0.0, "satellite": 0.0, "core": 100.0}
    assert sleeves["unlocked"] == {"core": True, "tilt": False, "satellite": False}
