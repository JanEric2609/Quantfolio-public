"""add Phase 9 entities: goals, aspects, knowledge_items, audit_logs

Revision ID: 0013_phase9_entities
Revises: 0012_quant_saved_scenarios
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_phase9_entities"
down_revision = "0012_quant_saved_scenarios"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("goals"):
        op.create_table(
            "goals",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("target_amount", sa.Float(), nullable=True),
            sa.Column("target_date", sa.Date(), nullable=True),
            sa.Column("progress", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_goals_user_id", "goals", ["user_id"])

    if not inspector.has_table("aspects"):
        op.create_table(
            "aspects",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_aspects_user_id", "aspects", ["user_id"])

    if not inspector.has_table("knowledge_items"):
        op.create_table(
            "knowledge_items",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=80), nullable=False, server_default="general"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_knowledge_items_user_id", "knowledge_items", ["user_id"])

    if not inspector.has_table("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("severity", sa.String(length=24), nullable=False, server_default="info"),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
        safe_create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("audit_logs"):
        safe_drop_index("ix_audit_logs_event_type", table_name="audit_logs")
        safe_drop_index("ix_audit_logs_user_id", table_name="audit_logs")
        op.drop_table("audit_logs")

    if inspector.has_table("knowledge_items"):
        safe_drop_index("ix_knowledge_items_user_id", table_name="knowledge_items")
        op.drop_table("knowledge_items")

    if inspector.has_table("aspects"):
        safe_drop_index("ix_aspects_user_id", table_name="aspects")
        op.drop_table("aspects")

    if inspector.has_table("goals"):
        safe_drop_index("ix_goals_user_id", table_name="goals")
        op.drop_table("goals")
