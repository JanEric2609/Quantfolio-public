"""Create backfill_jobs table for asynchronous price-backfill offload (P4).

Long provider backfills used to run inline on the request. They now enqueue a
durable job row and run on the job system; the client polls this table by id.

Revision ID: 0076_backfill_jobs
Revises: 0075_bar_prices_unique_index
"""
from alembic import op
import sqlalchemy as sa

revision = "0076_backfill_jobs"
down_revision = "0075_bar_prices_unique_index"
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
    if not inspector.has_table("backfill_jobs"):
        op.create_table(
            "backfill_jobs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("days", sa.Integer(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=True),
            sa.Column("succeeded", sa.Integer(), nullable=True),
            sa.Column("failed", sa.Integer(), nullable=True),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_backfill_jobs_user_id", "backfill_jobs", ["user_id"])
        safe_create_index("ix_backfill_jobs_status", "backfill_jobs", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("backfill_jobs"):
        safe_drop_index("ix_backfill_jobs_status", table_name="backfill_jobs")
        safe_drop_index("ix_backfill_jobs_user_id", table_name="backfill_jobs")
        op.drop_table("backfill_jobs")
