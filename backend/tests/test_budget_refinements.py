from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import (
    Category,
    DkbAccount,
    DkbTransaction,
    Expense,
    Invoice,
    Subscription,
    User,
)
from app.foundation.budget import (
    ensure_default_categories,
    expense_output,
    mark_invoice_paid,
    mark_subscription_paid,
    monthly_cashflow,
    monthly_summary,
)


def test_mark_subscription_paid_creates_expense_and_advances_cycle():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    subscription = Subscription(
        user_id=user.id,
        name="Streaming",
        amount=Decimal("12.99"),
        currency="EUR",
        billing_cycle="monthly",
        next_due_date=date(2026, 5, 1),
    )
    db.add(subscription)
    db.commit()

    updated = mark_subscription_paid(db, user.id, subscription, paid_date=date(2026, 5, 16))

    assert updated.next_due_date == date(2026, 6, 1)
    assert db.query(Expense).filter(Expense.user_id == user.id, Expense.source == "subscription").count() == 1


def test_mark_invoice_paid_creates_expense_and_defaults_categories():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    invoice = Invoice(user_id=user.id, issuer="Power", amount=Decimal("42"), due_date=date(2026, 5, 20))
    db.add(invoice)
    db.commit()

    mark_invoice_paid(db, user.id, invoice, paid_date=date(2026, 5, 16))
    categories = ensure_default_categories(db, user.id)

    assert invoice.paid is True
    assert db.query(Expense).filter(Expense.user_id == user.id, Expense.source == "invoice").count() == 1
    assert any(category.name == "Subscriptions" for category in categories)


def test_cashflow_pads_months_and_classifies_income_and_spend():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    income = Category(user_id=user.id, name="Salary", type="income")
    db.add(income)
    db.commit()
    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 5, 2), amount=Decimal("20"), description="Manual", source="manual"),
            Expense(user_id=user.id, date=date(2026, 5, 3), amount=Decimal("-7"), description="Card", source="dkb"),
            Expense(user_id=user.id, date=date(2026, 5, 4), amount=Decimal("100"), description="Salary", category_id=income.id),
            Expense(user_id=user.id, date=date(2026, 4, 4), amount=Decimal("50"), description="Refund", source="dkb_auto"),
            Expense(user_id=user.id, date=date(2026, 4, 5), amount=Decimal("10"), description="Verified refund", source="dkb_verified"),
        ]
    )
    db.commit()

    rows = monthly_cashflow(db, user.id, today=date(2026, 5, 22))

    assert len(rows) == 12
    assert rows[0]["month"] == "2025-06"
    assert rows[-1] == {"month": "2026-05", "income": 100.0, "spend": 27.0}
    assert rows[-2] == {"month": "2026-04", "income": 60.0, "spend": 0.0}


def test_summary_uses_monthly_subscription_equivalents_and_signed_net_income():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    income = Category(user_id=user.id, name="Income", type="income")
    db.add(income)
    db.commit()
    db.add_all(
        [
            Expense(user_id=user.id, date=date.today().replace(day=1), amount=Decimal("900"), description="Pay", category_id=income.id),
            Expense(user_id=user.id, date=date.today().replace(day=2), amount=Decimal("125"), description="Rent"),
            Subscription(user_id=user.id, name="Weekly", amount=Decimal("12"), billing_cycle="weekly", next_due_date=date(2026, 5, 1)),
            Subscription(user_id=user.id, name="Quarterly", amount=Decimal("30"), billing_cycle="quarterly", next_due_date=date(2026, 5, 1)),
            Subscription(user_id=user.id, name="Annual", amount=Decimal("120"), billing_cycle="annual", next_due_date=date(2026, 5, 1)),
        ]
    )
    db.commit()

    summary = monthly_summary(db, user.id)

    assert summary["spent"] == 125.0
    assert summary["income"] == 900.0
    assert summary["net_income"] == 775.0
    assert summary["active_subscription_run_rate"] == 72.0


def test_expense_output_exposes_only_linked_dkb_account_name():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(user_id=user.id, type="checking", iban="DE02120300000000202051")
    linked = Expense(user_id=user.id, date=date(2026, 5, 2), amount=Decimal("-10"), description="Shop", source="dkb_auto", dkb_dedupe_hash="tx")
    verified = Expense(user_id=user.id, date=date(2026, 5, 3), amount=Decimal("-11"), description="Shop", source="dkb_verified", dkb_dedupe_hash="tx-verified")
    manual = Expense(user_id=user.id, date=date(2026, 5, 2), amount=Decimal("10"), description="Cash")
    db.add_all([account, linked, verified, manual])
    db.commit()
    db.add_all(
        [
            DkbTransaction(account_id=account.id, date=linked.date, amount=linked.amount, reference=linked.description, dedupe_hash="tx"),
            DkbTransaction(account_id=account.id, date=verified.date, amount=verified.amount, reference=verified.description, dedupe_hash="tx-verified"),
        ]
    )
    db.commit()

    assert expense_output(db, linked)["account_name"] == "Checking *2051"
    assert expense_output(db, verified)["account_name"] == "Checking *2051"
    assert expense_output(db, manual)["account_name"] is None


def test_default_categories_are_student_oriented_without_investments():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    categories = ensure_default_categories(db, user.id)
    names = {category.name for category in categories}

    assert names == {
        "Groceries",
        "Eating Out",
        "Housing/Rent",
        "Utilities",
        "Transport",
        "Education",
        "Health",
        "Subscriptions",
        "Social & Entertainment",
        "Travel",
        "Miscellaneous",
    }
    assert "Investments" not in names
