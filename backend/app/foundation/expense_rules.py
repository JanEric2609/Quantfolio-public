"""Expense rule engine: automatic categorization via pattern matching and LLM fallback."""

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Category, Expense, ExpenseRule
from app.foundation.budget import ensure_default_categories

logger = logging.getLogger(__name__)

# Default merchant → category name mappings (category names must match ensure_default_categories)
DEFAULT_RULES: list[dict[str, Any]] = [
    # Groceries
    {"pattern": "REWE", "category": "Groceries", "priority": 10},
    {"pattern": "EDEKA", "category": "Groceries", "priority": 10},
    {"pattern": "ALDI", "category": "Groceries", "priority": 10},
    {"pattern": "LIDL", "category": "Groceries", "priority": 10},
    # Drugstore
    {"pattern": "DM", "category": "Healthcare", "priority": 10},
    {"pattern": "ROSSMANN", "category": "Healthcare", "priority": 10},
    # Transport
    {"pattern": "DB BAHN", "category": "Transport", "priority": 10},
    {"pattern": "DEUTSCHE BAHN", "category": "Transport", "priority": 10},
    # Fuel
    {"pattern": "ARAL", "category": "Transport", "priority": 10},
    {"pattern": "SHELL", "category": "Transport", "priority": 10},
    # Telecom
    {"pattern": "TELEKOM", "category": "Utilities", "priority": 10},
    {"pattern": "VODAFONE", "category": "Utilities", "priority": 10},
    {"pattern": "O2", "category": "Utilities", "priority": 10},
    # Entertainment
    {"pattern": "NETFLIX", "category": "Subscriptions", "priority": 10},
    {"pattern": "SPOTIFY", "category": "Subscriptions", "priority": 10},
    # Shopping
    {"pattern": "AMAZON", "category": "Shopping", "priority": 10},
    # Rent
    {"pattern": "MIETE", "category": "Housing", "priority": 10},
    # Utilities
    {"pattern": "STADTWERKE", "category": "Utilities", "priority": 10},
    {"pattern": "E.ON", "category": "Utilities", "priority": 10},
    {"pattern": "EON", "category": "Utilities", "priority": 10},
    # Income
    {"pattern": "GEHALT", "category": "Income", "priority": 5, "match_type": "contains"},
    {"pattern": "LOHN", "category": "Income", "priority": 5, "match_type": "contains"},
]


def seed_default_rules(db: Session, user_id: str) -> list[ExpenseRule]:
    """Idempotently seed default merchant rules for a user.

    Ensures default categories exist, then creates any missing system rules.
    """
    categories = ensure_default_categories(db, user_id)
    category_by_name: dict[str, str] = {c.name: c.id for c in categories}

    existing_patterns = {
        row.pattern: row
        for row in db.query(ExpenseRule).filter(
            ExpenseRule.user_id == user_id, ExpenseRule.source == "system"
        ).all()
    }

    created: list[ExpenseRule] = []
    for rule_def in DEFAULT_RULES:
        pattern = rule_def["pattern"]
        if pattern in existing_patterns:
            continue
        cat_name = rule_def["category"]
        category_id = category_by_name.get(cat_name)
        if category_id is None:
            logger.warning("Default rule %s skipped: category %s not found", pattern, cat_name)
            continue
        rule = ExpenseRule(
            user_id=user_id,
            pattern=pattern,
            match_type=rule_def.get("match_type", "contains"),
            category_id=category_id,
            priority=rule_def.get("priority", 10),
            source="system",
        )
        db.add(rule)
        created.append(rule)

    if created:
        db.commit()
    return db.query(ExpenseRule).filter(ExpenseRule.user_id == user_id).order_by(ExpenseRule.priority.asc()).all()


def _first_matching_category(rules: list[ExpenseRule], description: str) -> str | None:
    """Return the category_id of the first rule (in priority order) matching description."""
    desc = description.upper()
    for rule in rules:
        if rule.match_type == "regex":
            try:
                if re.search(rule.pattern, desc, re.IGNORECASE):
                    return rule.category_id
            except re.error:
                logger.warning("Invalid regex in expense rule %s: %s", rule.id, rule.pattern)
                continue
        else:
            if rule.pattern.upper() in desc:
                return rule.category_id
    return None


def categorize_description(db: Session, user_id: str, description: str) -> str | None:
    """Match a single free-text description against the user's rules (Telegram freeform entry)."""
    rules = (
        db.query(ExpenseRule)
        .filter(ExpenseRule.user_id == user_id)
        .order_by(ExpenseRule.priority.asc(), ExpenseRule.created_at.asc())
        .all()
    )
    if not rules:
        return None
    return _first_matching_category(rules, description)


def apply_rules(db: Session, user_id: str) -> int:
    """Apply expense rules to uncategorized expenses for a user.

    Rules are evaluated in priority order (lowest number first). The first
    matching rule wins and sets the expense category.

    Returns:
        Number of expenses categorized.
    """
    rules = (
        db.query(ExpenseRule)
        .filter(ExpenseRule.user_id == user_id)
        .order_by(ExpenseRule.priority.asc(), ExpenseRule.created_at.asc())
        .all()
    )
    if not rules:
        return 0

    uncategorized = (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.category_id.is_(None))
        .all()
    )

    categorized_count = 0
    for expense in uncategorized:
        category_id = _first_matching_category(rules, expense.description or "")
        if category_id is not None:
            expense.category_id = category_id
            categorized_count += 1

    if categorized_count:
        db.commit()
    return categorized_count


def llm_categorize_uncategorized(
    db: Session,
    user_id: str,
    llm_func: Callable[[str], str],
    batch_size: int = 20,
) -> int:
    """Categorize remaining uncategorized expenses using an LLM.

    Args:
        db: Database session
        user_id: User to categorize for
        llm_func: Callable that takes a prompt string and returns JSON text
        batch_size: Max expenses per LLM call

    Returns:
        Number of expenses successfully categorized by LLM.
    """
    categories = {c.id: c.name for c in db.query(Category).filter(Category.user_id == user_id).all()}
    if not categories:
        return 0

    uncategorized = (
        db.query(Expense)
        .filter(Expense.user_id == user_id, Expense.category_id.is_(None))
        .limit(batch_size)
        .all()
    )
    if not uncategorized:
        return 0

    expense_lines = []
    for idx, expense in enumerate(uncategorized, start=1):
        expense_lines.append(f'{idx}. "{expense.description}"')

    category_lines = [f'"{name}"' for name in categories.values()]

    prompt = (
        "Categorize each expense description into one of the provided categories.\n\n"
        "Expenses:\n" + "\n".join(expense_lines) + "\n\n"
        "Categories:\n" + "\n".join(category_lines) + "\n\n"
        "Respond with strict JSON only: {\"1\": \"CategoryName\", \"2\": \"CategoryName\", ...}. "
        "Use null for unknown categories. No extra text."
    )

    try:
        raw = llm_func(prompt)
        mapping = json.loads(raw)
    except Exception as exc:
        logger.warning("LLM categorization failed or returned invalid JSON: %s", exc)
        return 0

    name_to_id = {name: cid for cid, name in categories.items()}
    categorized_count = 0
    for idx, expense in enumerate(uncategorized, start=1):
        cat_name = mapping.get(str(idx))
        if not cat_name or cat_name == "null":
            continue
        cat_id = name_to_id.get(cat_name)
        if cat_id is None:
            continue
        expense.category_id = cat_id
        categorized_count += 1

    if categorized_count:
        db.commit()
    return categorized_count
