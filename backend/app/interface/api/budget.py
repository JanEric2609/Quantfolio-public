import logging
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import Category, DkbTransaction, Expense, ExpenseRule, IncomeSource, Invoice, Subscription, User
from app.foundation.schemas import (
    AnnualSankeyOut,
    BudgetSummaryOut,
    BufferMetricOut,
    CategorizeRunOut,
    CashflowMonthOut,
    CategoryIn,
    CategoryOut,
    EnvelopeBudgetSetOut,
    EnvelopeCoverOut,
    EnvelopeHistoryRowOut,
    EnvelopesOut,
    ExpenseIn,
    ExpenseOut,
    IncomeSourceIn,
    IncomeSourceOut,
    InvoiceIn,
    InvoiceOut,
    MarkPaidRequest,
    Message,
    MonthlyReviewOut,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionSuggestionOut,
)
from app.foundation.auth import current_user
from app.foundation.budget import (
    ensure_default_categories,
    expense_output,
    mark_invoice_paid,
    mark_subscription_paid,
    money_to_budget,
    monthly_cashflow,
    monthly_summary,
    subscription_suggestions,
)
from app.foundation.budget_insights import (
    annual_sankey_data,
    buffer_metric,
    envelope_history,
    monthly_review_summary,
)
from app.foundation.envelope import (
    check_envelope_threshold,
    cover_overspend,
    envelope_status,
    set_envelope_budget,
    sinking_fund_progress,
)
from app.foundation.expense_rules import apply_rules, llm_categorize_uncategorized, seed_default_rules

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/budget", tags=["budget"])


@router.get("/expenses", response_model=list[ExpenseOut])
def list_expenses(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    rows = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.date.desc()).all()
    results = []
    for row in rows:
        try:
            results.append(expense_output(db, row))
        except SQLAlchemyError as e:
            logger.warning("Error processing expense %s: %s", row.id, e)
            results.append({
                "id": row.id,
                "date": row.date,
                "amount": row.amount,
                "currency": row.currency,
                "description": row.description,
                "category_id": row.category_id,
                "source": row.source,
                "notes": row.notes,
                "account_name": None,
            })
    return results


@router.post("/expenses", response_model=ExpenseOut)
def create_expense(payload: ExpenseIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    values = payload.model_dump()
    values["category_id"] = values["category_id"] or None
    expense = Expense(user_id=user.id, **values)
    db.add(expense)
    db.commit()
    db.refresh(expense)
    if expense.category_id:
        try:
            check_envelope_threshold(db, user.id, expense.category_id, expense.date.year, expense.date.month)
        except Exception as e:
            logger.error("Envelope threshold check failed for expense %s: %s", expense.id, e)
    return expense_output(db, expense)


@router.put("/expenses/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: str,
    payload: ExpenseIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    expense = db.get(Expense, expense_id)
    if expense is None or expense.user_id != user.id:
        raise HTTPException(status_code=404, detail="Expense not found")
    values = payload.model_dump()
    values["category_id"] = values["category_id"] or None
    for key, value in values.items():
        setattr(expense, key, value)
    db.commit()
    db.refresh(expense)
    return expense_output(db, expense)


@router.delete("/expenses/{expense_id}", response_model=Message)
def delete_expense(expense_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    expense = db.get(Expense, expense_id)
    if expense is None or expense.user_id != user.id:
        raise HTTPException(status_code=404, detail="Expense not found")
    db.delete(expense)
    db.commit()
    return {"message": "Expense deleted"}


@router.get("/summary", response_model=BudgetSummaryOut)
def summary(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return monthly_summary(db, user.id, year, month)


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def subscriptions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[Subscription]:
    return (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id)
        .order_by(Subscription.active.desc(), Subscription.next_due_date.asc())
        .all()
    )


@router.post("/subscriptions", response_model=SubscriptionOut)
def create_subscription(payload: SubscriptionIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Subscription:
    values = payload.model_dump()
    values["category_id"] = values["category_id"] or None
    row = Subscription(user_id=user.id, **values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    subscription_id: str,
    payload: SubscriptionIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Subscription:
    row = db.get(Subscription, subscription_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    values = payload.model_dump()
    values["category_id"] = values["category_id"] or None
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/subscriptions/{subscription_id}", response_model=Message)
def delete_subscription(subscription_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    row = db.get(Subscription, subscription_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    db.delete(row)
    db.commit()
    return {"message": "Subscription deleted"}


@router.post("/subscriptions/{subscription_id}/mark-paid", response_model=SubscriptionOut)
def pay_subscription(
    subscription_id: str,
    payload: MarkPaidRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Subscription:
    row = db.get(Subscription, subscription_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return mark_subscription_paid(
        db,
        user.id,
        row,
        paid_date=payload.paid_date,
        create_expense=payload.create_expense,
    )


@router.get("/subscriptions/suggestions", response_model=list[SubscriptionSuggestionOut])
def suggested_subscriptions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return subscription_suggestions(db, user.id)


@router.get("/income-sources", response_model=list[IncomeSourceOut])
def income_sources(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[IncomeSource]:
    return (
        db.query(IncomeSource)
        .filter(IncomeSource.user_id == user.id)
        .order_by(IncomeSource.active.desc(), IncomeSource.next_date.asc())
        .all()
    )


@router.post("/income-sources", response_model=IncomeSourceOut)
def create_income_source(
    payload: IncomeSourceIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> IncomeSource:
    row = IncomeSource(user_id=user.id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/income-sources/{income_source_id}", response_model=IncomeSourceOut)
def update_income_source(
    income_source_id: str,
    payload: IncomeSourceIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> IncomeSource:
    row = db.get(IncomeSource, income_source_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Income source not found")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/income-sources/{income_source_id}", response_model=Message)
def delete_income_source(
    income_source_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    row = db.get(IncomeSource, income_source_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Income source not found")
    db.delete(row)
    db.commit()
    return {"message": "Income source deleted"}


@router.get("/invoices", response_model=list[InvoiceOut])
def invoices(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[Invoice]:
    return db.query(Invoice).filter(Invoice.user_id == user.id).order_by(Invoice.paid.asc(), Invoice.due_date.asc()).all()


@router.post("/invoices", response_model=InvoiceOut)
def create_invoice(payload: InvoiceIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Invoice:
    row = Invoice(user_id=user.id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/invoices/{invoice_id}", response_model=InvoiceOut)
def update_invoice(
    invoice_id: str,
    payload: InvoiceIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Invoice:
    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/invoices/{invoice_id}", response_model=Message)
def delete_invoice(invoice_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    db.delete(row)
    db.commit()
    return {"message": "Invoice deleted"}


@router.post("/invoices/{invoice_id}/mark-paid", response_model=InvoiceOut)
def pay_invoice(
    invoice_id: str,
    payload: MarkPaidRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Invoice:
    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return mark_invoice_paid(db, user.id, row, paid_date=payload.paid_date, create_expense=payload.create_expense)


@router.post("/invoices/{invoice_id}/mark-unpaid", response_model=InvoiceOut)
def unpay_invoice(invoice_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Invoice:
    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    row.paid = False
    db.commit()
    db.refresh(row)
    return row


@router.get("/categories", response_model=list[CategoryOut])
def categories(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[Category]:
    return ensure_default_categories(db, user.id)


@router.post("/categories/seed", response_model=list[CategoryOut])
def seed_categories(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[Category]:
    return ensure_default_categories(db, user.id)


@router.post("/categories", response_model=CategoryOut)
def create_category(payload: CategoryIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Category:
    row = Category(user_id=user.id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: str,
    payload: CategoryIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Category:
    row = db.get(Category, category_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/categories/{category_id}", response_model=Message)
def delete_category(category_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    row = db.get(Category, category_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")
    db.query(Expense).filter(Expense.user_id == user.id, Expense.category_id == row.id).update({Expense.category_id: None})
    db.query(Subscription).filter(Subscription.user_id == user.id, Subscription.category_id == row.id).update(
        {Subscription.category_id: None}
    )
    db.query(DkbTransaction).filter(DkbTransaction.category_id == row.id).update({DkbTransaction.category_id: None})
    db.delete(row)
    db.commit()
    return {"message": "Category deleted"}


@router.get("/cashflow", response_model=list[CashflowMonthOut])
def cashflow(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return monthly_cashflow(db, user.id)


class ExpenseRuleIn(BaseModel):
    pattern: str
    match_type: str = "contains"
    category_id: str | None = None
    priority: int = 10


class ExpenseRuleOut(BaseModel):
    id: str
    user_id: str
    pattern: str
    match_type: str
    category_id: str | None = None
    priority: int
    source: str
    created_at: str | None = None


@router.get("/rules", response_model=list[ExpenseRuleOut])
@safe_endpoint
def list_rules(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    seed_default_rules(db, user.id)
    rows = db.query(ExpenseRule).filter(ExpenseRule.user_id == user.id).order_by(ExpenseRule.priority.asc()).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "pattern": r.pattern,
            "match_type": r.match_type,
            "category_id": r.category_id,
            "priority": r.priority,
            "source": r.source,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/rules", response_model=ExpenseRuleOut)
@safe_endpoint
def create_rule(payload: ExpenseRuleIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    rule = ExpenseRule(
        user_id=user.id,
        pattern=payload.pattern,
        match_type=payload.match_type,
        category_id=payload.category_id,
        priority=payload.priority,
        source="user",
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {
        "id": rule.id,
        "user_id": rule.user_id,
        "pattern": rule.pattern,
        "match_type": rule.match_type,
        "category_id": rule.category_id,
        "priority": rule.priority,
        "source": rule.source,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


@router.delete("/rules/{rule_id}", response_model=Message)
@safe_endpoint
def delete_rule(rule_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    rule = db.get(ExpenseRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"message": "Rule deleted"}


@router.post("/categorize/run", response_model=CategorizeRunOut)
@safe_endpoint
def run_categorization(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.llm.router import call as llm_call
    import asyncio

    seed_default_rules(db, user.id)
    rule_count = apply_rules(db, user.id)

    def _llm_wrapper(prompt: str) -> str:
        loop = asyncio.new_event_loop()
        try:
            completion = loop.run_until_complete(
                llm_call(
                    db,
                    task_type="routine",
                    messages=[
                        {"role": "system", "content": "You are a helpful expense categorization assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    user_id=user.id,
                    # Bank transaction descriptions must never leave the homelab.
                    force_local=True,
                )
            )
            return completion.content
        finally:
            loop.close()

    llm_count = llm_categorize_uncategorized(db, user.id, _llm_wrapper)
    return {"rule_categorized": rule_count, "llm_categorized": llm_count}


class EnvelopeBudgetSet(BaseModel):
    year: int
    month: int
    budgeted_amount: Decimal


@router.get("/envelopes", response_model=EnvelopesOut)
def envelopes(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    today = date.today()
    target_year, target_month = year or today.year, month or today.month
    rows = envelope_status(db, user.id, target_year, target_month)
    categories_by_id = {
        category.id: category
        for category in db.query(Category).filter(Category.user_id == user.id).all()
    }
    for row in rows:
        category = categories_by_id.get(row["category_id"])
        row["goal"] = sinking_fund_progress(db, user.id, category, target_year, target_month) if category else None
    return {"money_to_budget": float(money_to_budget(db, user.id)), "envelopes": rows}


@router.put("/envelopes/{category_id}", response_model=EnvelopeBudgetSetOut)
def update_envelope_budget(
    category_id: str,
    payload: EnvelopeBudgetSet,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    category = db.get(Category, category_id)
    if category is None or category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")
    row = set_envelope_budget(db, user.id, category_id, payload.year, payload.month, payload.budgeted_amount)
    return {
        "category_id": row.category_id,
        "year": row.year,
        "month": row.month,
        "budgeted_amount": float(row.budgeted_amount),
    }


class EnvelopeCoverRequest(BaseModel):
    from_category_id: str
    to_category_id: str
    amount: Decimal
    year: int
    month: int


@router.post("/envelopes/cover", response_model=EnvelopeCoverOut)
def cover_envelope_overspend(
    payload: EnvelopeCoverRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from_category = db.get(Category, payload.from_category_id)
    to_category = db.get(Category, payload.to_category_id)
    if from_category is None or from_category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Source category not found")
    if to_category is None or to_category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Destination category not found")
    try:
        return cover_overspend(
            db, user.id, payload.from_category_id, payload.to_category_id, payload.amount, payload.year, payload.month
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/envelopes/history", response_model=list[EnvelopeHistoryRowOut])
def envelope_budget_history(
    months: int = 6,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    return envelope_history(db, user.id, months=months)


@router.get("/buffer", response_model=BufferMetricOut)
def buffer(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return buffer_metric(db, user.id)


@router.get("/reports/sankey", response_model=AnnualSankeyOut)
def annual_sankey(
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return annual_sankey_data(db, user.id, year or date.today().year)


@router.get("/review", response_model=MonthlyReviewOut)
def monthly_review(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    today = date.today()
    return monthly_review_summary(db, user.id, year or today.year, month or today.month)
