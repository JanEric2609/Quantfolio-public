from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Category, EnvelopeBudget, User
from app.foundation.envelope import run_monthly_envelope_rollover


def test_rollover_job_seeds_next_month_from_prior_budgeted_amounts():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    db.add_all(
        [
            EnvelopeBudget(user_id=user.id, category_id=groceries.id, year=2026, month=7, budgeted_amount=Decimal("200")),
            EnvelopeBudget(user_id=user.id, category_id=travel.id, year=2026, month=7, budgeted_amount=Decimal("50")),
        ]
    )
    db.commit()

    result = run_monthly_envelope_rollover(db, as_of=date(2026, 8, 1))

    assert result == {"users_processed": 1, "envelopes_seeded": 2}
    august_rows = (
        db.query(EnvelopeBudget)
        .filter(EnvelopeBudget.user_id == user.id, EnvelopeBudget.year == 2026, EnvelopeBudget.month == 8)
        .all()
    )
    assert {(row.category_id, row.budgeted_amount) for row in august_rows} == {
        (groceries.id, Decimal("200")),
        (travel.id, Decimal("50")),
    }


def test_rollover_job_is_idempotent_and_does_not_overwrite_manual_edits():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(groceries)
    db.commit()
    db.add(EnvelopeBudget(user_id=user.id, category_id=groceries.id, year=2026, month=7, budgeted_amount=Decimal("200")))
    db.add(EnvelopeBudget(user_id=user.id, category_id=groceries.id, year=2026, month=8, budgeted_amount=Decimal("999")))
    db.commit()

    result = run_monthly_envelope_rollover(db, as_of=date(2026, 8, 1))

    assert result == {"users_processed": 1, "envelopes_seeded": 0}
    row = (
        db.query(EnvelopeBudget)
        .filter(EnvelopeBudget.user_id == user.id, EnvelopeBudget.year == 2026, EnvelopeBudget.month == 8)
        .one()
    )
    assert row.budgeted_amount == Decimal("999")


def test_rollover_job_handles_january_year_rollover():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(groceries)
    db.commit()
    db.add(EnvelopeBudget(user_id=user.id, category_id=groceries.id, year=2025, month=12, budgeted_amount=Decimal("150")))
    db.commit()

    result = run_monthly_envelope_rollover(db, as_of=date(2026, 1, 1))

    assert result == {"users_processed": 1, "envelopes_seeded": 1}
