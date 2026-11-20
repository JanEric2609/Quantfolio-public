"""Phase 5 reporting: envelope over/under history, the buffer ("Age of Money")
metric, the annual Sankey flow, and the monthly review flow's data feed.

All of this is derived from existing tables (``EnvelopeBudget``, ``Expense``,
``Category``, ``IncomeSource``) — no new schema.
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.foundation.models.entities import Category, EnvelopeBudget, Expense, IncomeSource
from app.foundation.budget import DKB_EXPENSE_SOURCES, classify_cashflow
from app.foundation.envelope import envelope_rollover_balance, sinking_fund_progress


def envelope_history(db: Session, user_id: str, *, months: int = 6, as_of: date | None = None) -> list[dict[str, Any]]:
    """Per-category budgeted vs. spent for the trailing ``months`` (current month included).

    Flags categories that consistently run over or under budget, to inform
    target adjustments in the monthly review flow.
    """
    today = as_of or date.today()
    month_keys: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(months):
        month_keys.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    month_keys.reverse()

    earliest_year, earliest_month = month_keys[0]
    latest_year, latest_month = month_keys[-1]
    range_start = date(earliest_year, earliest_month, 1)
    range_end = date(latest_year, latest_month, monthrange(latest_year, latest_month)[1])

    categories = (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.type == "expense")
        .order_by(Category.name.asc())
        .all()
    )
    categories_by_id = {category.id: category for category in categories}

    spent_by_category_month: dict[tuple[str, int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for expense in (
        db.query(Expense)
        .filter(
            Expense.user_id == user_id,
            Expense.date >= range_start,
            Expense.date <= range_end,
            Expense.category_id.isnot(None),
        )
        .all()
    ):
        category = categories_by_id.get(expense.category_id or "")
        if category is None:
            continue
        _, spend = classify_cashflow(expense, category)
        spent_by_category_month[(category.id, expense.date.year, expense.date.month)] += spend

    budgeted_by_category_month: dict[tuple[str, int, int], Decimal] = {}
    if categories_by_id:
        month_filter = or_(*(and_(EnvelopeBudget.year == y, EnvelopeBudget.month == m) for y, m in month_keys))
        for row in (
            db.query(EnvelopeBudget)
            .filter(EnvelopeBudget.user_id == user_id, month_filter)
            .all()
        ):
            budgeted_by_category_month[(row.category_id, row.year, row.month)] = Decimal(row.budgeted_amount)

    results = []
    for category in categories:
        monthly = []
        months_over = 0
        months_with_budget = 0
        for y, m in month_keys:
            spent = spent_by_category_month.get((category.id, y, m), Decimal("0"))
            budgeted = budgeted_by_category_month.get((category.id, y, m), Decimal("0"))
            if budgeted > 0:
                months_with_budget += 1
                if spent > budgeted:
                    months_over += 1
            monthly.append(
                {
                    "year": y,
                    "month": m,
                    "budgeted": float(budgeted),
                    "spent": float(spent),
                    "over": float(spent - budgeted),
                }
            )
        results.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "color": category.color,
                "months": monthly,
                "months_over_budget": months_over,
                "months_with_budget": months_with_budget,
                "consistently_over": months_with_budget >= 3 and months_over / months_with_budget >= 0.6,
                "consistently_under": months_with_budget >= 3 and months_over == 0,
            }
        )
    return results


def buffer_metric(db: Session, user_id: str, *, as_of: date | None = None, lookback_days: int = 90) -> dict[str, Any]:
    """An adapted "Age of Money": how many days of spending the current buffer covers.

    ``buffer`` is the sum of rollover balances across ordinary expense
    envelopes (sinking funds are excluded — that money already has a job,
    a future lump cost, so it isn't discretionary runway). The rate is the
    average daily spend over the trailing ``lookback_days``.
    """
    today = as_of or date.today()
    all_categories = db.query(Category).filter(Category.user_id == user_id).all()
    categories_by_id = {category.id: category for category in all_categories}
    discretionary_categories = [
        category for category in all_categories if category.type == "expense" and category.target_amount is None
    ]

    buffer_total = Decimal("0")
    for category in discretionary_categories:
        buffer_total += envelope_rollover_balance(db, user_id, category.id, today.year, today.month)

    window_start = today - timedelta(days=lookback_days - 1)
    total_spend = Decimal("0")
    for expense in (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.date >= window_start, Expense.date <= today)
        .all()
    ):
        category = categories_by_id.get(expense.category_id) if expense.category_id else None
        _, spend = classify_cashflow(expense, category)
        total_spend += spend

    avg_daily_spend = total_spend / Decimal(lookback_days)
    buffer_days = float(buffer_total / avg_daily_spend) if avg_daily_spend > 0 else None
    return {
        "buffer_amount": float(buffer_total),
        "avg_daily_spend": float(avg_daily_spend),
        "buffer_days": buffer_days,
    }


def annual_sankey_data(db: Session, user_id: str, year: int) -> dict[str, Any]:
    """Income sources -> Income -> flat expense categories -> top merchants.

    Actuals only (real ``Expense`` rows), scoped to the calendar year.
    ``IncomeSource`` names are used to label the income side when an actual
    income transaction's description matches a configured source; otherwise
    it falls back to the income category name (or "Other income").
    """
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    categories_by_id = {category.id: category for category in db.query(Category).filter(Category.user_id == user_id).all()}
    income_sources = db.query(IncomeSource).filter(IncomeSource.user_id == user_id).all()

    income_by_label: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    spend_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    merchant_by_category: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))

    for expense in (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .all()
    ):
        category = categories_by_id.get(expense.category_id) if expense.category_id else None
        income_amount, spend_amount = classify_cashflow(expense, category)
        if income_amount > 0:
            label = _income_source_label(expense.description, income_sources) or (
                category.name if category else "Other income"
            )
            income_by_label[label] += income_amount
        if spend_amount > 0:
            category_name = category.name if category else "Uncategorized"
            spend_by_category[category_name] += spend_amount
            merchant_by_category[category_name][_merchant_label(expense.description)] += spend_amount

    links: list[dict[str, Any]] = []
    for label, amount in sorted(income_by_label.items(), key=lambda kv: -kv[1]):
        links.append({"source": label, "target": "Income", "value": float(amount)})
    for category_name, amount in sorted(spend_by_category.items(), key=lambda kv: -kv[1]):
        links.append({"source": "Income", "target": category_name, "value": float(amount)})
        top_merchants = sorted(merchant_by_category[category_name].items(), key=lambda kv: -kv[1])[:5]
        top_total = sum((value for _, value in top_merchants), Decimal("0"))
        for merchant, merchant_amount in top_merchants:
            links.append(
                {"source": category_name, "target": f"{merchant} · {category_name}", "value": float(merchant_amount)}
            )
        other_amount = amount - top_total
        if other_amount > Decimal("0.01"):
            links.append({"source": category_name, "target": f"Other · {category_name}", "value": float(other_amount)})

    return {
        "year": year,
        "total_income": float(sum(income_by_label.values(), Decimal("0"))),
        "total_expense": float(sum(spend_by_category.values(), Decimal("0"))),
        "links": links,
    }


def _income_source_label(description: str, income_sources: list[IncomeSource]) -> str | None:
    normalized = description.lower()
    for source in income_sources:
        if source.name.lower() in normalized:
            return source.name
    return None


def _merchant_label(description: str) -> str:
    value = re.sub(r"\s+", " ", description).strip()
    return value[:40] or "Unknown"


def potential_duplicate_pairs(
    db: Session, user_id: str, year: int, month: int, *, date_tolerance_days: int = 3
) -> list[dict[str, Any]]:
    """Manual/DKB-synced expense pairs that look like the same transaction but
    weren't caught by the exact-hash auto-merge (e.g. rounded amount, off-by-a-day
    posting date). Surfaced for the user to reconcile by hand in the monthly review.
    """
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    expenses = (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .all()
    )
    manual = [expense for expense in expenses if expense.source == "manual"]
    synced = [expense for expense in expenses if expense.source in DKB_EXPENSE_SOURCES]

    pairs = []
    matched_synced_ids: set[str] = set()
    for m in manual:
        m_amount = abs(Decimal(m.amount))
        for s in synced:
            if s.id in matched_synced_ids:
                continue
            if abs(m_amount - abs(Decimal(s.amount))) > Decimal("0.01"):
                continue
            if abs((m.date - s.date).days) > date_tolerance_days:
                continue
            pairs.append(
                {
                    "manual_expense_id": m.id,
                    "manual_description": m.description,
                    "manual_date": m.date.isoformat(),
                    "synced_expense_id": s.id,
                    "synced_description": s.description,
                    "synced_date": s.date.isoformat(),
                    "amount": float(m_amount),
                }
            )
            matched_synced_ids.add(s.id)
            break
    return pairs


def monthly_review_summary(db: Session, user_id: str, year: int, month: int) -> dict[str, Any]:
    """Everything the "Review <month>?" flow needs in one call: uncategorized
    expenses to bulk-triage, likely unreconciled DKB/manual duplicates, and
    sinking-fund progress check-ins.
    """
    categories = db.query(Category).filter(Category.user_id == user_id).all()
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    uncategorized = (
        db.query(Expense)
        .filter(
            Expense.user_id == user_id,
            Expense.date >= start,
            Expense.date <= end,
            Expense.category_id.is_(None),
        )
        .order_by(Expense.date.desc())
        .all()
    )

    sinking_funds = []
    for category in categories:
        if category.target_amount is None:
            continue
        progress = sinking_fund_progress(db, user_id, category, year, month)
        if progress is not None:
            sinking_funds.append({"category_id": category.id, "category_name": category.name, **progress})

    return {
        "year": year,
        "month": month,
        "uncategorized_expenses": [
            {
                "id": expense.id,
                "date": expense.date.isoformat(),
                "amount": float(expense.amount),
                "currency": expense.currency,
                "description": expense.description,
                "source": expense.source,
                "notes": expense.notes,
            }
            for expense in uncategorized
        ],
        "potential_duplicates": potential_duplicate_pairs(db, user_id, year, month),
        "sinking_funds": sinking_funds,
    }
