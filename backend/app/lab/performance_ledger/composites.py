"""Composite portfolio management (user-defined groupings)."""

import json
from datetime import date
from sqlalchemy.orm import Session
from app.foundation.models.entities import Composite, CompositeMembership, Portfolio


def create_composite(
    db: Session,
    user_id: str,
    name: str,
    portfolio_ids: list[str],
) -> Composite:
    """
    Create a composite (named grouping of portfolios).

    Args:
        db: Database session
        user_id: User ID
        name: Composite name
        portfolio_ids: List of portfolio IDs to include

    Returns:
        Created Composite
    """
    definition = {
        "portfolios": portfolio_ids,
        "weights": [1.0 / len(portfolio_ids) if portfolio_ids else 0.0] * len(portfolio_ids),
        "rebalance_frequency": "quarterly",
    }

    composite = Composite(
        user_id=user_id,
        name=name,
        definition_json=json.dumps(definition),
    )
    db.add(composite)
    db.flush()

    # Add membership records
    today = date.today()
    for portfolio_id in portfolio_ids:
        membership = CompositeMembership(
            composite_id=composite.id,
            portfolio_id=portfolio_id,
            valid_from=today,
            valid_to=None,
        )
        db.add(membership)

    db.flush()
    return composite


def list_composites(db: Session, user_id: str) -> list[Composite]:
    """List all composites for a user."""
    return db.query(Composite).filter_by(user_id=user_id).order_by(Composite.created_at.desc()).all()


def ensure_default_composite(db: Session, user_id: str) -> Composite | None:
    """Create an 'All accounts' composite over the main portfolio if none exists.

    Idempotent: safe to call multiple times. Returns the existing or newly
    created composite, or None if the user has no portfolios.
    """
    existing = db.query(Composite).filter_by(user_id=user_id, name="All accounts").first()
    if existing:
        return existing

    main_portfolio = (
        db.query(Portfolio)
        .filter_by(user_id=user_id)
        .order_by(Portfolio.id.asc())
        .first()
    )
    if not main_portfolio:
        return None

    return create_composite(
        db,
        user_id=user_id,
        name="All accounts",
        portfolio_ids=[main_portfolio.id],
    )


def get_composite(db: Session, composite_id: str) -> Composite | None:
    """Get a specific composite."""
    return db.query(Composite).filter_by(id=composite_id).first()


def add_portfolio_to_composite(
    db: Session,
    composite_id: str,
    portfolio_id: str,
    valid_from: date | None = None,
) -> CompositeMembership:
    """Add portfolio to composite with effective date."""
    if valid_from is None:
        valid_from = date.today()

    membership = CompositeMembership(
        composite_id=composite_id,
        portfolio_id=portfolio_id,
        valid_from=valid_from,
        valid_to=None,
    )
    db.add(membership)
    db.flush()
    return membership


def remove_portfolio_from_composite(
    db: Session,
    composite_id: str,
    portfolio_id: str,
    valid_to: date | None = None,
) -> None:
    """Remove portfolio from composite (mark valid_to)."""
    if valid_to is None:
        valid_to = date.today()

    membership = (
        db.query(CompositeMembership)
        .filter_by(composite_id=composite_id, portfolio_id=portfolio_id, valid_to=None)
        .first()
    )
    if membership:
        membership.valid_to = valid_to
        db.flush()


def get_composite_members(db: Session, composite_id: str, as_of: date | None = None) -> list[str]:
    """Get portfolio IDs in composite as of a date."""
    if as_of is None:
        as_of = date.today()

    members = (
        db.query(CompositeMembership.portfolio_id)
        .filter_by(composite_id=composite_id)
        .filter(CompositeMembership.valid_from <= as_of)
        .filter(
            (CompositeMembership.valid_to.is_(None)) | (CompositeMembership.valid_to >= as_of)
        )
        .all()
    )
    return [m[0] for m in members]
