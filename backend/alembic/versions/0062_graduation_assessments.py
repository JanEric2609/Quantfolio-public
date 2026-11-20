"""Add graduation_assessments table.

Records point-in-time verdicts on whether the LLM has statistically proven
itself on the paper portfolio and may surface real-portfolio recommendations.

Revision ID: 0062_graduation_assessments
Revises: 0061_competition_tables
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_graduation_assessments"
down_revision = "0061_competition_tables"
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
    """
    Create the graduation_assessments table to store point-in-time graduation assessment verdicts for users.
    
    The table includes columns for graduation status, overall progress, assessment criteria and metrics, and audit timing with a composite index on user_id and created_at.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("graduation_assessments"):
        op.create_table(
            "graduation_assessments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("graduated", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("overall_progress", sa.Float, nullable=False, server_default=sa.text("0.0")),
            sa.Column("criteria_json", sa.Text, nullable=False, server_default=sa.text("'[]'")),
            sa.Column("metrics_json", sa.Text, nullable=False, server_default=sa.text("'{}'")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        safe_create_index(
            "ix_graduation_assessments_user_created",
            "graduation_assessments",
            ["user_id", "created_at"],
        )


def downgrade() -> None:
    """
    Remove the graduation_assessments table and its composite index.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("graduation_assessments"):
        safe_drop_index(
            "ix_graduation_assessments_user_created",
            table_name="graduation_assessments",
        )
        op.drop_table("graduation_assessments")
