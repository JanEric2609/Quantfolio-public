import json
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.foundation.models.entities import Category, EnvelopeBudget, Expense, Nudge
from app.foundation.budget import classify_cashflow
from app.foundation.notify import send_notification

THRESHOLD_TIERS = ((Decimal("1"), "critical", "100%"), (Decimal("0.9"), "warning", "90%"))


def _get_or_create_envelope_budget(
    db: Session, user_id: str, category_id: str, year: int, month: int
) -> EnvelopeBudget:
    row = (
        db.query(EnvelopeBudget)
        .filter(
            EnvelopeBudget.user_id == user_id,
            EnvelopeBudget.category_id == category_id,
            EnvelopeBudget.year == year,
            EnvelopeBudget.month == month,
        )
        .one_or_none()
    )
    if row is None:
        row = EnvelopeBudget(
            user_id=user_id,
            category_id=category_id,
            year=year,
            month=month,
            budgeted_amount=Decimal("0"),
        )
        db.add(row)
        db.flush()
    return row


def set_envelope_budget(
    db: Session, user_id: str, category_id: str, year: int, month: int, amount: Decimal
) -> EnvelopeBudget:
    row = _get_or_create_envelope_budget(db, user_id, category_id, year, month)
    row.budgeted_amount = amount
    db.commit()
    db.refresh(row)
    return row


def envelope_rollover_balance(db: Session, user_id: str, category_id: str, year: int, month: int) -> Decimal:
    rows = (
        db.query(EnvelopeBudget)
        .filter(EnvelopeBudget.user_id == user_id, EnvelopeBudget.category_id == category_id)
        .filter(
            or_(
                EnvelopeBudget.year < year,
                and_(EnvelopeBudget.year == year, EnvelopeBudget.month <= month),
            )
        )
        .all()
    )
    if rows:
        total_budgeted = sum((Decimal(row.budgeted_amount) for row in rows), Decimal("0"))
        earliest_year, earliest_month = min((row.year, row.month) for row in rows)
        start = date(earliest_year, earliest_month, 1)
    else:
        # No budget has ever been set for this category. There is no budgeted
        # history to accumulate against, so only the target month's spend
        # counts against the implicit €0 envelope.
        total_budgeted = Decimal("0")
        start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    category = db.get(Category, category_id)
    total_spent = Decimal("0")
    for expense in (
        db.query(Expense)
        .filter(
            Expense.user_id == user_id,
            Expense.category_id == category_id,
            Expense.date >= start,
            Expense.date <= end,
        )
        .all()
    ):
        _, spend = classify_cashflow(expense, category)
        total_spent += spend

    return total_budgeted - total_spent


def sinking_fund_progress(
    db: Session, user_id: str, category: Category, year: int, month: int
) -> dict[str, Any] | None:
    if category.target_amount is None or category.target_date is None:
        return None
    progress = envelope_rollover_balance(db, user_id, category.id, year, month)
    remaining = max(Decimal("0"), Decimal(category.target_amount) - progress)
    target = date(category.target_date.year, category.target_date.month, 1)
    current = date(year, month, 1)
    months_remaining = max(1, (target.year - current.year) * 12 + (target.month - current.month) + 1)
    required_monthly = (remaining / months_remaining) if remaining > 0 else Decimal("0")
    return {
        "target_amount": float(category.target_amount),
        "target_date": category.target_date.isoformat(),
        "progress": float(progress),
        "required_monthly": float(required_monthly),
    }


def envelope_status(db: Session, user_id: str, year: int, month: int) -> list[dict[str, Any]]:
    categories = (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.type == "expense")
        .order_by(Category.name.asc())
        .all()
    )
    budgets = {
        row.category_id: row
        for row in db.query(EnvelopeBudget)
        .filter(EnvelopeBudget.user_id == user_id, EnvelopeBudget.year == year, EnvelopeBudget.month == month)
        .all()
    }
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    categories_by_id = {category.id: category for category in categories}
    spent_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for expense in (
        db.query(Expense)
        .filter(
            Expense.user_id == user_id,
            Expense.date >= start,
            Expense.date <= end,
            Expense.category_id.isnot(None),
        )
        .all()
    ):
        category_id = expense.category_id
        if category_id is None:
            continue
        category = categories_by_id.get(category_id)
        if category is None:
            continue
        _, spend = classify_cashflow(expense, category)
        spent_by_category[category_id] += spend

    results = []
    for category in categories:
        budget_row = budgets.get(category.id)
        budgeted = Decimal(budget_row.budgeted_amount) if budget_row else Decimal("0")
        spent = spent_by_category.get(category.id, Decimal("0"))
        remaining = envelope_rollover_balance(db, user_id, category.id, year, month)
        results.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "color": category.color,
                "icon": category.icon,
                "budgeted": float(budgeted),
                "spent": float(spent),
                "remaining": float(remaining),
                "overspent": remaining < 0,
            }
        )
    return results


def cover_overspend(
    db: Session,
    user_id: str,
    from_category_id: str,
    to_category_id: str,
    amount: Decimal,
    year: int,
    month: int,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError("Cover amount must be positive")
    if from_category_id == to_category_id:
        raise ValueError("Source and destination categories must be different")

    from_budget = _get_or_create_envelope_budget(db, user_id, from_category_id, year, month)
    to_budget = _get_or_create_envelope_budget(db, user_id, to_category_id, year, month)
    if Decimal(from_budget.budgeted_amount) < amount:
        raise ValueError("Source category does not have enough budgeted amount to cover this transfer")
    from_budget.budgeted_amount = Decimal(from_budget.budgeted_amount) - amount
    to_budget.budgeted_amount = Decimal(to_budget.budgeted_amount) + amount
    db.commit()
    db.refresh(from_budget)
    db.refresh(to_budget)
    return {
        "from_category_id": from_category_id,
        "from_remaining_budgeted": float(from_budget.budgeted_amount),
        "to_category_id": to_category_id,
        "to_remaining_budgeted": float(to_budget.budgeted_amount),
        "amount": float(amount),
    }


def check_envelope_threshold(db: Session, user_id: str, category_id: str, year: int, month: int) -> Nudge | None:
    budget_row = (
        db.query(EnvelopeBudget)
        .filter(
            EnvelopeBudget.user_id == user_id,
            EnvelopeBudget.category_id == category_id,
            EnvelopeBudget.year == year,
            EnvelopeBudget.month == month,
        )
        .one_or_none()
    )
    if budget_row is None or Decimal(budget_row.budgeted_amount) <= 0:
        return None

    category = db.get(Category, category_id)
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    spent = Decimal("0")
    for expense in (
        db.query(Expense)
        .filter(
            Expense.user_id == user_id,
            Expense.category_id == category_id,
            Expense.date >= start,
            Expense.date <= end,
        )
        .all()
    ):
        _, spend = classify_cashflow(expense, category)
        spent += spend
    ratio = spent / Decimal(budget_row.budgeted_amount)

    for threshold, severity, label in THRESHOLD_TIERS:
        if ratio < threshold:
            continue
        dedupe_key = f"budget_threshold|{category_id}|{year}-{month:02d}|{label}"
        already_sent = (
            db.query(Nudge)
            .filter(
                Nudge.user_id == user_id,
                Nudge.source == "budget_threshold",
                Nudge.payload_json.like(f'%"key": "{dedupe_key}"%'),
            )
            .first()
        )
        if already_sent is not None:
            return None
        nudge = Nudge(
            user_id=user_id,
            source="budget_threshold",
            severity=severity,
            payload_json=json.dumps(
                {
                    "key": dedupe_key,
                    "category_id": category_id,
                    "category_name": category.name if category else "",
                    "label": label,
                    "ratio": float(ratio),
                }
            ),
        )
        db.add(nudge)
        db.commit()
        db.refresh(nudge)
        send_notification(db, user_id, "bell", nudge)
        send_notification(db, user_id, "telegram", nudge)
        return nudge
    return None


def run_monthly_envelope_rollover(db: Session, *, as_of: date | None = None) -> dict[str, int]:
    """Seed next month's EnvelopeBudget rows from the prior month's budgeted amounts.

    The rollover *balance* shown to users (``envelope_rollover_balance``) is
    always derived at read time and does not depend on this job having run —
    this only pre-fills next month's budgeted-amount inputs so the UI isn't
    empty on the 1st. Idempotent: never overwrites a row that already exists
    for the target month (e.g. one the user already edited).
    """
    target = as_of or date.today()
    target_year, target_month = target.year, target.month
    prior_year, prior_month = (target_year - 1, 12) if target_month == 1 else (target_year, target_month - 1)

    user_ids = [
        row[0]
        for row in db.query(EnvelopeBudget.user_id)
        .filter(EnvelopeBudget.year == prior_year, EnvelopeBudget.month == prior_month)
        .distinct()
        .all()
    ]

    seeded = 0
    for user_id in user_ids:
        prior_rows = (
            db.query(EnvelopeBudget)
            .filter(
                EnvelopeBudget.user_id == user_id,
                EnvelopeBudget.year == prior_year,
                EnvelopeBudget.month == prior_month,
            )
            .all()
        )
        existing_target_category_ids = {
            row.category_id
            for row in db.query(EnvelopeBudget)
            .filter(
                EnvelopeBudget.user_id == user_id,
                EnvelopeBudget.year == target_year,
                EnvelopeBudget.month == target_month,
            )
            .all()
        }
        for prior_row in prior_rows:
            if prior_row.category_id in existing_target_category_ids:
                continue
            db.add(
                EnvelopeBudget(
                    user_id=user_id,
                    category_id=prior_row.category_id,
                    year=target_year,
                    month=target_month,
                    budgeted_amount=prior_row.budgeted_amount,
                )
            )
            seeded += 1

    db.commit()
    return {"users_processed": len(user_ids), "envelopes_seeded": seeded}
