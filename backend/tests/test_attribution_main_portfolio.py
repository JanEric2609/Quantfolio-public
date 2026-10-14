"""Attribution endpoints resolve the "main" portfolio (found by the API scan)."""
from conftest import _memory_db

from app.foundation.models.entities import Portfolio, User
from app.interface.api.attribution import _resolve_portfolio


def test_main_resolves_the_users_portfolio_without_created_at():
    db = _memory_db()
    db.add_all([User(id="u1", username="a", password_hash="!"), User(id="u2", username="b", password_hash="!")])
    db.add_all([Portfolio(id="p1", user_id="u1", name="Main"), Portfolio(id="p2", user_id="u2", name="Other")])
    db.commit()
    assert _resolve_portfolio(db, "u1", "main").id == "p1"
    assert _resolve_portfolio(db, "u1", "").id == "p1"
    assert _resolve_portfolio(db, "u1", "p2") is None  # another user's portfolio
    assert _resolve_portfolio(db, "u2", "default").id == "p2"


def test_missing_benchmark_is_a_client_error_not_501():
    """API scan, 2026-10-14: an empty benchmark ticker answered 501."""
    from datetime import date
    from decimal import Decimal

    import pytest
    from fastapi import HTTPException

    from app.foundation.models.entities import Holding
    from app.interface.api.attribution import BrinsonRequest, run_brinson_attribution

    db = _memory_db()
    user = User(id="u1", username="a", password_hash="!")
    db.add_all([user, Portfolio(id="p1", user_id="u1", name="Main")])
    db.add(Holding(portfolio_id="p1", ticker="XYZ", name="XYZ", quantity=Decimal("1"), avg_buy_price=Decimal("10")))
    db.commit()
    req = BrinsonRequest(portfolio_id="", benchmark_ticker="", date_from=date(2000, 1, 1), date_to=date(1970, 1, 1))
    with pytest.raises(HTTPException) as exc:
        run_brinson_attribution(req, db=db, user=user)
    assert exc.value.status_code == 422
