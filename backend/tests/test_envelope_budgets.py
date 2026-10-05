from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from conftest import _memory_db

from app.foundation.models.entities import Category, EnvelopeBudget, User


def test_envelope_budget_insert_and_unique_constraint():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()

    db.add(
        EnvelopeBudget(
            user_id=user.id, category_id=category.id, year=2026, month=8, budgeted_amount=Decimal("200")
        )
    )
    db.commit()

    db.add(
        EnvelopeBudget(
            user_id=user.id, category_id=category.id, year=2026, month=8, budgeted_amount=Decimal("250")
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
