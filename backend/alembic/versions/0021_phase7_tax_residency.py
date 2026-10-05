"""Phase 7: NL Box 3 jurisdiction support - tax residency periods

Revision ID: 0021_phase7_tax_residency
Revises: 0020_phase6_pgvector
Create Date: 2026-05-24

Adds:
- tax_residency_periods table for tracking jurisdiction (DE/NL) by date range
- jurisdiction_at_acquisition column on tax_lots for per-lot jurisdiction tracking
- default_tax_jurisdiction public setting
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021_phase7_tax_residency"
down_revision = "0020_phase6_pgvector"
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

    # Create tax_residency_periods table if not exists
    if not inspector.has_table("tax_residency_periods"):
        op.create_table(
            "tax_residency_periods",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("country", sa.String(2), nullable=False),  # "DE", "NL", etc.
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        # Indexes for tax_residency_periods
        safe_create_index(
            "ix_tax_residency_periods_user_id_valid_from",
            "tax_residency_periods",
            ["user_id", sa.desc("valid_from")],
            unique=False,
        )
        safe_create_index(
            "ix_tax_residency_periods_user_id_country",
            "tax_residency_periods",
            ["user_id", "country"],
            unique=False,
        )

    # Add jurisdiction_at_acquisition column to tax_lots if not exists
    if inspector.has_table("tax_lots"):
        existing_columns = {c["name"] for c in inspector.get_columns("tax_lots")}
        if "jurisdiction_at_acquisition" not in existing_columns:
            op.add_column(
                "tax_lots",
                sa.Column(
                    "jurisdiction_at_acquisition",
                    sa.String(2),
                    nullable=True,
                    server_default="DE",
                ),
            )
            # Create index on jurisdiction_at_acquisition
            safe_create_index(
                "ix_tax_lots_jurisdiction_at_acquisition",
                "tax_lots",
                ["jurisdiction_at_acquisition"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop jurisdiction_at_acquisition from tax_lots
    if inspector.has_table("tax_lots"):
        existing_columns = {c["name"] for c in inspector.get_columns("tax_lots")}
        if "jurisdiction_at_acquisition" in existing_columns:
            safe_drop_index("ix_tax_lots_jurisdiction_at_acquisition", table_name="tax_lots")
            op.drop_column("tax_lots", "jurisdiction_at_acquisition")

    # Drop tax_residency_periods table
    if inspector.has_table("tax_residency_periods"):
        safe_drop_index("ix_tax_residency_periods_user_id_valid_from", table_name="tax_residency_periods")
        safe_drop_index("ix_tax_residency_periods_user_id_country", table_name="tax_residency_periods")
        op.drop_table("tax_residency_periods")
