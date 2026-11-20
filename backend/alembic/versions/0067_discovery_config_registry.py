"""Add DiscoveryConfig, DiscoveryConfigReview tables + config_id columns

Revision ID: 0067_discovery_config_registry
Revises: 0066_add_prediction_flags
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_discovery_config_registry"
down_revision = "0066_add_prediction_flags"
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

    # -- DiscoveryConfig table --
    if not inspector.has_table("discovery_config"):
        op.create_table(
            "discovery_config",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("config_type", sa.String(32), nullable=False),
            sa.Column("version_label", sa.String(64), nullable=False),
            sa.Column("description", sa.String(256), nullable=True),
            sa.Column("config_json", sa.JSON, nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="challenger"),
            sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("parent_config_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("champion_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metrics_json", sa.JSON, nullable=True),
        )
        safe_create_index("ix_discovery_config_type_status", "discovery_config", ["config_type", "status"])
        op.create_foreign_key(
            "fk_discovery_config_parent", "discovery_config",
            "discovery_config", ["parent_config_id"], ["id"],
            ondelete="SET NULL",
        )

    # -- DiscoveryConfigReview table --
    if not inspector.has_table("discovery_config_review"):
        op.create_table(
            "discovery_config_review",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("config_type", sa.String(32), nullable=False),
            sa.Column("champion_id", sa.String(36), sa.ForeignKey("discovery_config.id"), nullable=False),
            sa.Column("challenger_results", sa.JSON, nullable=False),
            sa.Column("promoted_id", sa.String(36), sa.ForeignKey("discovery_config.id"), nullable=True),
            sa.Column("rejected_ids", sa.JSON, nullable=False),
            sa.Column("details_json", sa.JSON, nullable=False),
        )

    # -- config_id on discovery_prediction --
    if inspector.has_table("discovery_prediction"):
        columns = [c["name"] for c in inspector.get_columns("discovery_prediction")]
        if "config_id" not in columns:
            op.add_column("discovery_prediction", sa.Column("config_id", sa.String(36), nullable=True))
            safe_create_index("ix_discovery_prediction_config_id", "discovery_prediction", ["config_id"])

    # -- config_id on discovery_skill_snapshot --
    if inspector.has_table("discovery_skill_snapshot"):
        columns = [c["name"] for c in inspector.get_columns("discovery_skill_snapshot")]
        if "config_id" not in columns:
            op.add_column("discovery_skill_snapshot", sa.Column("config_id", sa.String(36), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("discovery_config_review"):
        op.drop_table("discovery_config_review")
    if inspector.has_table("discovery_config"):
        op.drop_table("discovery_config")

    if inspector.has_table("discovery_prediction"):
        columns = [c["name"] for c in inspector.get_columns("discovery_prediction")]
        if "config_id" in columns:
            safe_drop_index("ix_discovery_prediction_config_id", table_name="discovery_prediction")
            op.drop_column("discovery_prediction", "config_id")

    if inspector.has_table("discovery_skill_snapshot"):
        columns = [c["name"] for c in inspector.get_columns("discovery_skill_snapshot")]
        if "config_id" in columns:
            op.drop_column("discovery_skill_snapshot", "config_id")
