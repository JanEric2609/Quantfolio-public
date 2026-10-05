from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import Goal, User
from app.main import create_app


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _client(maker, user_id):
    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user_id)
    return TestClient(app)


def _goal(maker, monthly):
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    goal = Goal(
        user_id=user.id, title="Flat", target_amount=Decimal("60000"), progress=Decimal("10000"),
        target_date=date.today() + timedelta(days=10 * 365), monthly_contribution=Decimal(monthly),
    )
    db.add(goal)
    db.commit()
    return user.id, goal.id


def test_goal_monte_carlo_counts_the_monthly_contribution():
    maker = _memory_db()
    user_id, goal_id = _goal(maker, "300")
    with patch("app.interface.api.quant.goals.book_volatility", return_value=(0.15, "test")):
        body = _client(maker, user_id).post(f"/api/quant/goals/{goal_id}/mc").json()
    assert body["status"] == "completed"
    assert body["assumptions"]["monthly_contribution"] == 300.0
    assert body["assumptions"]["years"] == 10
    assert len(body["fan_chart"]) == 11
    # 10,000 + 36,000 paid in at 4.2 % real: the median is well above the 46,000 put in.
    assert 55_000 < body["fan_chart"][-1]["median"] < 75_000
    assert 0.3 < body["probability"] < 0.8
    assert body["estimate"] is True


def test_goal_monte_carlo_without_contributions_is_unlikely():
    maker = _memory_db()
    user_id, goal_id = _goal(maker, "0")
    with patch("app.interface.api.quant.goals.book_volatility", return_value=(0.15, "test")):
        body = _client(maker, user_id).post(f"/api/quant/goals/{goal_id}/mc").json()
    assert body["probability"] < 0.05
    assert body["on_track"] is False


def test_required_return_solves_the_annuity():
    from app.interface.api.quant.goals import _required_real_return

    assert abs(_required_real_return(10_000, 0, 10, 10_000)) < 1e-9
    # 12 payments of 100 at 0 % reach 1,200.
    assert abs(_required_real_return(0, 100, 1, 1_200)) < 1e-9
    r = _required_real_return(10_000, 300, 10, 60_000)
    assert 0.0 < r < 0.05
