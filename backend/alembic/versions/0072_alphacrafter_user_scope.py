"""Add user_id to alphacrafter_job_runs and recommendation_dossiers for user scoping.

Fixes IDOR (issue #127): jobs and dossiers were global; any authenticated user
could read any job's shared memory or any dossier. Columns are nullable so
legacy rows survive; if the deployment has exactly one user, legacy rows are
backfilled to that user (mirrors 0068_promote_sole_user_admin).

Revision ID: 0072_alphacrafter_user_scope
Revises: 0071_news_published_at_idx
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa


revision = "0072_alphacrafter_user_scope"
down_revision = "0071_news_published_at_idx"
branch_labels = None
depends_on = None

_TABLES = ("alphacrafter_job_runs", "recommendation_dossiers")


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

    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        columns = {col["name"] for col in inspector.get_columns(table)}
        if "user_id" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "user_id",
                    sa.String(36),
                    sa.ForeignKey("users.id", ondelete="CASCADE"),
                    nullable=True,
                ),
            )
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        index_name = f"ix_{table}_user_id"
        if index_name not in existing:
            safe_create_index(index_name, table, ["user_id"])

    # Backfill: single-user deployments attribute all legacy rows to the sole user.
    if inspector.has_table("users"):
        users = bind.execute(sa.text("SELECT id FROM users LIMIT 2")).fetchall()
        if len(users) == 1:
            sole_user_id = users[0][0]
            for table in _TABLES:
                if inspector.has_table(table):
                    bind.execute(
                        sa.text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"),  # noqa: S608
                        {"uid": sole_user_id},
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        index_name = f"ix_{table}_user_id"
        if any(ix["name"] == index_name for ix in inspector.get_indexes(table)):
            safe_drop_index(index_name, table_name=table)
        columns = {col["name"] for col in inspector.get_columns(table)}
        if "user_id" in columns:
            op.drop_column(table, "user_id")
