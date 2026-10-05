"""Phase 3: Attribution engine and performance ledger tables

Revision ID: 0017_attribution_engine
Revises: 0016_mc_tables
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_attribution_engine"
down_revision = "0016_mc_tables"
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

    # Create composites table (user-defined portfolio groupings)
    if not inspector.has_table("composites"):
        op.create_table(
            "composites",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("definition_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_composites_user_id_name", "composites", ["user_id", "name"])

    # Create composite_membership table (many-to-many: composite -> portfolio)
    if not inspector.has_table("composite_membership"):
        op.create_table(
            "composite_membership",
            sa.Column("composite_id", sa.UUID(), nullable=False),
            sa.Column("portfolio_id", sa.UUID(), nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.PrimaryKeyConstraint("composite_id", "portfolio_id", "valid_from"),
            sa.ForeignKeyConstraint(["composite_id"], ["composites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_composite_membership_portfolio_id", "composite_membership", ["portfolio_id"])

    # Create attribution_runs table (regular table, not hypertable)
    if not inspector.has_table("attribution_runs"):
        op.create_table(
            "attribution_runs",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("portfolio_id", sa.UUID(), nullable=False),
            sa.Column("benchmark", sa.String(length=80), nullable=False),
            sa.Column("date_from", sa.Date(), nullable=False),
            sa.Column("date_to", sa.Date(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_attribution_runs_user_id_created_at", "attribution_runs",
                       ["user_id", sa.desc("created_at")])
        safe_create_index("ix_attribution_runs_portfolio_id", "attribution_runs", ["portfolio_id"])

    # Create performance_ledger_entries hypertable (30-day chunks)
    if not inspector.has_table("performance_ledger_entries"):
        op.create_table(
            "performance_ledger_entries",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("composite_id", sa.UUID(), nullable=False),
            sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("twr", sa.Float(), nullable=False),
            sa.Column("mwr", sa.Float(), nullable=False),
            sa.Column("dispersion", sa.Float(), nullable=True),
            sa.Column("ex_post_risk_json", sa.Text(), nullable=False),
            sa.Column("snapshot_meta_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id", "as_of"),
            sa.ForeignKeyConstraint(["composite_id"], ["composites.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_performance_ledger_entries_composite_id_as_of", "performance_ledger_entries",
                       ["composite_id", sa.desc("as_of")])

        # Convert to hypertable with 30-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('performance_ledger_entries', 'as_of',
                                         chunk_time_interval => interval '30 days',
                                         if_not_exists => TRUE)
            """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop hypertable
    if inspector.has_table("performance_ledger_entries"):
        op.drop_table("performance_ledger_entries")

    # Drop regular tables
    if inspector.has_table("attribution_runs"):
        op.drop_table("attribution_runs")

    if inspector.has_table("composite_membership"):
        op.drop_table("composite_membership")

    if inspector.has_table("composites"):
        op.drop_table("composites")
