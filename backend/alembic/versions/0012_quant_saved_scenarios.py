"""add quant_saved_scenarios table

Revision ID: 0012_quant_saved_scenarios
Revises: 0011_chat_conversation_id
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_quant_saved_scenarios"
down_revision = "0011_chat_conversation_id"
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
    if inspector.has_table("quant_saved_scenarios"):
        return
    op.create_table(
        "quant_saved_scenarios",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("annual_return", sa.Numeric(10, 6), nullable=False),
        sa.Column("annual_volatility", sa.Numeric(10, 6), nullable=False),
        sa.Column("years", sa.Integer(), nullable=False),
        sa.Column("simulations", sa.Integer(), nullable=False),
        sa.Column("start_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("p05_terminal", sa.Numeric(20, 6), nullable=False),
        sa.Column("median_terminal", sa.Numeric(20, 6), nullable=False),
        sa.Column("p95_terminal", sa.Numeric(20, 6), nullable=False),
        sa.Column("fan_data_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    safe_create_index("ix_quant_saved_scenarios_user_created", "quant_saved_scenarios", ["user_id", "created_at"])
    safe_create_index("ix_quant_saved_scenarios_user_id", "quant_saved_scenarios", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_saved_scenarios"):
        return
    safe_drop_index("ix_quant_saved_scenarios_user_id", table_name="quant_saved_scenarios")
    safe_drop_index("ix_quant_saved_scenarios_user_created", table_name="quant_saved_scenarios")
    op.drop_table("quant_saved_scenarios")

