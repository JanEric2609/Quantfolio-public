"""Phase 8 indexes for entity wiring.

Revision ID: 0022_phase8_indexes
Revises: 0021_phase7_tax_residency
Create Date: 2026-05-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_phase8_indexes"
down_revision = "0021_phase7_tax_residency"
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

    # audit_logs indexes — also created in 0019_phase5_verification.py, use IF NOT EXISTS
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_event_type_ts ON audit_logs (event_type, created_at DESC)"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_correlation_id ON audit_logs (correlation_id)"))

    # goals indexes (user_id already exists from foreign key, but ensure it)
    existing = [ix["name"] for ix in inspector.get_indexes("goals")]
    if "ix_goals_user_id" not in existing:
        safe_create_index("ix_goals_user_id", "goals", ["user_id"])

    # aspects indexes
    existing = [ix["name"] for ix in inspector.get_indexes("aspects")]
    if "ix_aspects_user_id" not in existing:
        safe_create_index("ix_aspects_user_id", "aspects", ["user_id"])

    # knowledge_items indexes
    existing = [ix["name"] for ix in inspector.get_indexes("knowledge_items")]
    if "ix_knowledge_items_item_type" not in existing:
        safe_create_index("ix_knowledge_items_item_type", "knowledge_items", ["category"])

    existing = [ix["name"] for ix in inspector.get_indexes("knowledge_items")]
    if "ix_knowledge_items_updated_at" not in existing:
        safe_create_index("ix_knowledge_items_updated_at", "knowledge_items", [sa.desc("created_at")])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _drop_if_exists(index_name: str, table_name: str) -> None:
        if inspector.has_table(table_name):
            if any(ix["name"] == index_name for ix in inspector.get_indexes(table_name)):
                safe_drop_index(index_name, table_name=table_name)

    _drop_if_exists("ix_audit_logs_event_type_ts", "audit_logs")
    _drop_if_exists("ix_audit_logs_correlation_id", "audit_logs")
    _drop_if_exists("ix_goals_user_id", "goals")
    _drop_if_exists("ix_aspects_user_id", "aspects")
    _drop_if_exists("ix_knowledge_items_item_type", "knowledge_items")
    _drop_if_exists("ix_knowledge_items_updated_at", "knowledge_items")
