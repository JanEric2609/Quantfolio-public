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
