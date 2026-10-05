"""Add portfolio_id FK to portfolio_snapshots for per-portfolio tracking.

Revision ID: 0045_snapshot_portfolio_id
Revises: 0044_job_runs_tracking
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_snapshot_portfolio_id"
down_revision = "0044_job_runs_tracking"
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

    if inspector.has_table("portfolio_snapshots"):
        columns = {c["name"] for c in inspector.get_columns("portfolio_snapshots")}

        if "portfolio_id" not in columns:
            # Use batch mode for SQLite compatibility (FK constraints require copy-and-move)
            with op.batch_alter_table("portfolio_snapshots") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "portfolio_id",
                        sa.String(36),
                        sa.ForeignKey("portfolios.id", ondelete="SET NULL"),
                        nullable=True,
                    ),
                )
            index_names = {idx["name"] for idx in inspector.get_indexes("portfolio_snapshots")}
            if "ix_portfolio_snapshots_portfolio_date" not in index_names:
                safe_create_index(
                    "ix_portfolio_snapshots_portfolio_date",
                    "portfolio_snapshots",
                    ["portfolio_id", "date"],
                )

            op.execute(
                """
                UPDATE portfolio_snapshots
                SET portfolio_id = (
                    SELECT id FROM portfolios
                    WHERE portfolios.user_id = portfolio_snapshots.user_id
                    LIMIT 1
                )
                """
            )

            # Check if the constraint exists before dropping
            unique_constraints = inspector.get_unique_constraints("portfolio_snapshots")
            constraint_names = {uc["name"] for uc in unique_constraints}
            if "uq_portfolio_snapshots_user_date_source" in constraint_names:
                with op.batch_alter_table("portfolio_snapshots") as batch_op:
                    batch_op.drop_constraint(
                        "uq_portfolio_snapshots_user_date_source",
                        type_="unique",
                    )
            # Only create new constraint if it doesn't already exist
            if "uq_portfolio_snapshots_portfolio_date_source" not in constraint_names:
                with op.batch_alter_table("portfolio_snapshots") as batch_op:
                    batch_op.create_unique_constraint(
                        "uq_portfolio_snapshots_portfolio_date_source",
                        ["portfolio_id", "date", "source"],
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("portfolio_snapshots"):
        return

    # Collect current schema metadata
    unique_constraints = inspector.get_unique_constraints("portfolio_snapshots")
    constraint_names = {uc["name"] for uc in unique_constraints}
    index_names = {idx["name"] for idx in inspector.get_indexes("portfolio_snapshots")}
    column_names = {c["name"] for c in inspector.get_columns("portfolio_snapshots")}

    # Drop new unique constraint if present
    if "uq_portfolio_snapshots_portfolio_date_source" in constraint_names:
        with op.batch_alter_table("portfolio_snapshots") as batch_op:
            batch_op.drop_constraint(
                "uq_portfolio_snapshots_portfolio_date_source",
                type_="unique",
            )

    # Drop new index if present
    if "ix_portfolio_snapshots_portfolio_date" in index_names:
        safe_drop_index(
            "ix_portfolio_snapshots_portfolio_date",
            table_name="portfolio_snapshots",
        )

    # Drop portfolio_id column if present
    if "portfolio_id" in column_names:
        with op.batch_alter_table("portfolio_snapshots") as batch_op:
            batch_op.drop_column("portfolio_id")

    # Restore original unique constraint if not already present
    # Re-inspect after drops to account for batch-mode schema changes
    remaining = sa.inspect(op.get_bind()).get_unique_constraints("portfolio_snapshots")
    remaining_names = {uc["name"] for uc in remaining}
    if "uq_portfolio_snapshots_user_date_source" not in remaining_names:
        # Deduplicate to avoid unique constraint violation on restore
        # Keep the first row per (user_id, date, source) group
        conn = op.get_bind()
        conn.execute(
            sa.text(
                """
                DELETE FROM portfolio_snapshots
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM portfolio_snapshots
                    GROUP BY user_id, date, source
                )
                """
            )
        )
        with op.batch_alter_table("portfolio_snapshots") as batch_op:
            batch_op.create_unique_constraint(
                "uq_portfolio_snapshots_user_date_source",
                ["user_id", "date", "source"],
            )
