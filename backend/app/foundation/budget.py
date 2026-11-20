from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import re
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Category, DkbAccount, DkbTransaction, Expense, IncomeSource, Invoice, Subscription


DEFAULT_CATEGORIES = [
    ("Groceries", "#F59E0B", "shopping-cart"),
    ("Eating Out", "#FB923C", "utensils"),
    ("Housing/Rent", "#60A5FA", "home"),
    ("Utilities", "#22C55E", "bolt"),
    ("Transport", "#38BDF8", "car"),
    ("Education", "#A78BFA", "graduation-cap"),
    ("Health", "#FB7185", "heart-pulse"),
    ("Subscriptions", "#C084FC", "repeat"),
    ("Social & Entertainment", "#F472B6", "party-popper"),
    ("Travel", "#2DD4BF", "plane"),
    ("Miscellaneous", "#94A3B8", "more-horizontal"),
]

DKB_EXPENSE_SOURCES = {"dkb", "dkb_auto", "dkb_verified"}


def monthly_summary(db: Session, user_id: str, year: int | None = None, month: int | None = None) -> dict:
    today = date.today()
    year = year or today.year
    month = month or today.month
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    expenses = (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .all()
    )
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id, Subscription.active.is_(True))
        .all()
    )
    categories = {row.id: row for row in db.query(Category).filter(Category.user_id == user_id).all()}
    income = Decimal("0")
    spent = Decimal("0")
    for expense in expenses:
        income_amount, spend_amount = classify_cashflow(expense, categories.get(expense.category_id or ""))
        income += income_amount
        spent += spend_amount
    subscription_total = sum((monthly_subscription_amount(item) for item in subscriptions), Decimal("0"))
    net_income = income - spent
    return {
        "year": year,
        "month": month,
        "currency": "EUR",
        "spent": float(spent),
        "income": float(income),
        "net_income": float(net_income),
        "active_subscription_run_rate": float(subscription_total),
        "open_invoice_total": float(
            sum(
                (
                    Decimal(invoice.amount)
                    for invoice in db.query(Invoice)
                    .filter(Invoice.user_id == user_id, Invoice.paid.is_(False))
                    .all()
                ),
                Decimal("0"),
            )
        ),
        "open_invoice_count": db.query(Invoice)
        .filter(Invoice.user_id == user_id, Invoice.paid.is_(False))
        .count(),
        "expense_count": len(expenses),
        "investable_surplus_estimate": float(max(Decimal("0"), net_income)),
    }


def classify_cashflow(expense: Expense, category: Category | None = None) -> tuple[Decimal, Decimal]:
    amount = Decimal(expense.amount)
    if category is not None and category.type == "income":
        return amount, Decimal("0")
    if category is None and expense.source in DKB_EXPENSE_SOURCES and amount > 0:
        return amount, Decimal("0")
    return Decimal("0"), abs(amount)


def monthly_subscription_amount(subscription: Subscription) -> Decimal:
    amount = Decimal(subscription.amount)
    if subscription.billing_cycle == "weekly":
        return amount * Decimal("52") / Decimal("12")
    if subscription.billing_cycle == "quarterly":
        return amount / Decimal("3")
    if subscription.billing_cycle == "annual":
        return amount / Decimal("12")
    return amount


def monthly_cashflow(db: Session, user_id: str, *, today: date | None = None) -> list[dict[str, float | str]]:
    end = today or date.today()
    first_month_index = end.year * 12 + end.month - 12
    starts = [first_month_index + offset for offset in range(12)]
    months = {
        f"{index // 12:04d}-{index % 12 + 1:02d}": {"income": Decimal("0"), "spend": Decimal("0")}
        for index in starts
    }
    start_year, start_month = starts[0] // 12, starts[0] % 12 + 1
    categories = {row.id: row for row in db.query(Category).filter(Category.user_id == user_id).all()}
    rows = (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.date >= date(start_year, start_month, 1))
        .all()
    )
    for expense in rows:
        key = expense.date.strftime("%Y-%m")
        if key not in months:
            continue
        income, spend = classify_cashflow(expense, categories.get(expense.category_id or ""))
        months[key]["income"] += income
        months[key]["spend"] += spend
    return [{"month": key, "income": float(value["income"]), "spend": float(value["spend"])} for key, value in months.items()]


def account_name_for_expense(db: Session, expense: Expense) -> str | None:
    if expense.source not in DKB_EXPENSE_SOURCES or not expense.dkb_dedupe_hash:
        return None
    account = (
        db.query(DkbAccount)
        .join(DkbTransaction, DkbTransaction.account_id == DkbAccount.id)
        .filter(DkbTransaction.dedupe_hash == expense.dkb_dedupe_hash)
        .filter(DkbAccount.user_id == expense.user_id)
        .first()
    )
    if account is None:
        return None
    if account.iban:
        return f"{account.type.title()} *{account.iban[-4:]}"
    return account.type.title()


def expense_output(db: Session, expense: Expense) -> dict[str, Any]:
    return {
        "id": expense.id,
        "date": expense.date,
        "amount": expense.amount,
        "currency": expense.currency,
        "description": expense.description,
        "category_id": expense.category_id,
        "source": expense.source,
        "notes": expense.notes,
        "account_name": account_name_for_expense(db, expense),
    }


def ensure_default_categories(db: Session, user_id: str) -> list[Category]:
    existing = db.query(Category).filter(Category.user_id == user_id).all()
    names = {row.name.lower() for row in existing}
    created = []
    for name, color, icon in DEFAULT_CATEGORIES:
        if name.lower() not in names:
            created.append(Category(user_id=user_id, name=name, color=color, icon=icon, type="expense"))
    if created:
        db.add_all(created)
        db.commit()
    return db.query(Category).filter(Category.user_id == user_id).order_by(Category.name.asc()).all()


def advance_due_date(value: date, cycle: str) -> date:
    if cycle == "weekly":
        return value + timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "annual": 12}.get(cycle, 1)
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def mark_subscription_paid(
    db: Session,
    user_id: str,
    subscription: Subscription,
    *,
    paid_date: date | None = None,
    create_expense: bool = True,
) -> Subscription:
    paid_on = paid_date or date.today()
    if create_expense:
        dedupe = hashlib.sha256(f"subscription|{subscription.id}|{paid_on.isoformat()}".encode()).hexdigest()
        exists = db.query(Expense).filter(Expense.user_id == user_id, Expense.dkb_dedupe_hash == dedupe).one_or_none()
        if exists is None:
            db.add(
                Expense(
                    user_id=user_id,
                    date=paid_on,
                    amount=Decimal(subscription.amount),
                    currency=subscription.currency,
                    description=subscription.name,
                    category_id=subscription.category_id,
                    source="subscription",
                    dkb_dedupe_hash=dedupe,
                )
            )
    while subscription.next_due_date <= paid_on:
        subscription.next_due_date = advance_due_date(subscription.next_due_date, subscription.billing_cycle)
    db.commit()
    db.refresh(subscription)
    return subscription


def mark_invoice_paid(
    db: Session,
    user_id: str,
    invoice: Invoice,
    *,
    paid_date: date | None = None,
    create_expense: bool = True,
) -> Invoice:
    invoice.paid = True
    if create_expense:
        paid_on = paid_date or date.today()
        dedupe = hashlib.sha256(f"invoice|{invoice.id}|{paid_on.isoformat()}".encode()).hexdigest()
        exists = db.query(Expense).filter(Expense.user_id == user_id, Expense.dkb_dedupe_hash == dedupe).one_or_none()
        if exists is None:
            db.add(
                Expense(
                    user_id=user_id,
                    date=paid_on,
                    amount=Decimal(invoice.amount),
                    currency=invoice.currency,
                    description=invoice.issuer,
                    source="invoice",
                    dkb_dedupe_hash=dedupe,
                )
            )
    db.commit()
    db.refresh(invoice)
    return invoice


def subscription_suggestions(db: Session, user_id: str) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    existing = {row.name.lower() for row in db.query(Subscription).filter(Subscription.user_id == user_id).all()}

    groups: dict[tuple[str, str], list[tuple[date, Decimal]]] = defaultdict(list)
    for expense in db.query(Expense).filter(Expense.user_id == user_id).all():
        if Decimal(expense.amount) >= 0:
            continue
        key = (_normalize_merchant(expense.description), str(abs(Decimal(expense.amount)).quantize(Decimal("0.01"))))
        groups[key].append((expense.date, abs(Decimal(expense.amount))))

    for tx in db.query(DkbTransaction).join(DkbAccount).filter(DkbAccount.user_id == user_id).all():
        if Decimal(tx.amount) >= 0:
            continue
        key = (_normalize_merchant(tx.reference), str(abs(Decimal(tx.amount)).quantize(Decimal("0.01"))))
        groups[key].append((tx.date, abs(Decimal(tx.amount))))

    for (name, _amount_key), rows in groups.items():
        if len(rows) < 2 or not name or name.lower() in existing:
            continue
        rows = sorted(rows, key=lambda item: item[0])
        gaps = [(rows[idx][0] - rows[idx - 1][0]).days for idx in range(1, len(rows))]
        if not any(24 <= gap <= 38 for gap in gaps):
            continue
        last_date, amount = rows[-1]
        suggestions.append(
            {
                "name": name.title(),
                "amount": float(amount),
                "currency": "EUR",
                "billing_cycle": "monthly",
                "next_due_date": advance_due_date(last_date, "monthly").isoformat(),
                "source": "recurring_expense",
                "occurrences": len(rows),
            }
        )
    return suggestions[:20]


def _normalize_merchant(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().lower()
    value = re.sub(r"\b(invoice|rechnung|mandat|lastschrift|sepa|card|zahlung)\b", "", value)
    return re.sub(r"[^a-z0-9 ._-]", "", value).strip()[:80]


def monthly_income_amount(source: IncomeSource) -> Decimal:
    amount = Decimal(source.amount)
    if source.cadence == "weekly":
        return amount * Decimal("52") / Decimal("12")
    if source.cadence == "quarterly":
        return amount / Decimal("3")
    if source.cadence == "annual":
        return amount / Decimal("12")
    return amount


def money_to_budget(db: Session, user_id: str) -> Decimal:
    sources = (
        db.query(IncomeSource)
        .filter(IncomeSource.user_id == user_id, IncomeSource.active.is_(True))
        .all()
    )
    return sum((monthly_income_amount(source) for source in sources), Decimal("0"))
