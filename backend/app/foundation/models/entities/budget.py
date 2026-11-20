from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    color: Mapped[str] = mapped_column(String(24), default="#3B82F6")
    icon: Mapped[str] = mapped_column(String(40), default="tag")
    type: Mapped[str] = mapped_column(String(20), default="expense")
    target_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    description: Mapped[str] = mapped_column(String(250))
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(24), default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    dkb_dedupe_hash: Mapped[str | None] = mapped_column(String(64), unique=True)


class ExpenseRule(Base):
    __tablename__ = "expense_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pattern: Mapped[str] = mapped_column(String(250), nullable=False)
    match_type: Mapped[str] = mapped_column(String(20), nullable=False, default="contains")
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    billing_cycle: Mapped[str] = mapped_column(String(20), default="monthly")
    next_due_date: Mapped[date] = mapped_column(Date)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    payment_method: Mapped[str | None] = mapped_column(String(80))
    logo_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(Text)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issuer: Mapped[str] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    due_date: Mapped[date | None] = mapped_column(Date)
    paid: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class EnvelopeBudget(Base):
    """One row per (user, category, year, month): the budgeted amount for that category that month.

    Rollover balance is DERIVED at read time (services/envelope.py), never stored here.
    """

    __tablename__ = "envelope_budgets"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "category_id", "year", "month", name="uq_envelope_budget_user_category_month"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    budgeted_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))


class TelegramAccount(Base):
    """Links a Quantfolio user to a Telegram chat for bot entry + alert delivery."""

    __tablename__ = "telegram_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TelegramPairingCode(Base):
    """Short-lived one-time code shown in Settings, claimed via /start in Telegram."""

    __tablename__ = "telegram_pairing_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncomeSource(Base):
    """Recurring expected income. Projection-only — never creates Expense rows."""

    __tablename__ = "income_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    cadence: Mapped[str] = mapped_column(String(20), default="monthly")
    next_date: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(default=True)
