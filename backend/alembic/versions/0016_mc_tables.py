"""Create Monte Carlo tables for Phase 2

Revision ID: 0016_mc_tables
Revises: 0015_hypertables
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_mc_tables"
down_revision = "0015_hypertables"
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

    # Create mc_runs table (regular table, not hypertable)
    if not inspector.has_table("mc_runs"):
        op.create_table(
            "mc_runs",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("results_json", sa.Text(), nullable=False),
            sa.Column("paths_summary_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_mc_runs_user_id_created_at", "mc_runs", ["user_id", sa.desc("created_at")])

    # Create mc_path_samples hypertable (7-day chunks)
    if not inspector.has_table("mc_path_samples"):
        op.create_table(
            "mc_path_samples",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("run_id", sa.UUID(), nullable=False),
            sa.Column("path_id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts"),
            sa.ForeignKeyConstraint(["run_id"], ["mc_runs.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_mc_path_samples_run_id_path_id_ts", "mc_path_samples",
                       ["run_id", "path_id", "ts"])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('mc_path_samples', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop hypertables (and their data)
    if inspector.has_table("mc_path_samples"):
        op.drop_table("mc_path_samples")

    if inspector.has_table("mc_runs"):
        op.drop_table("mc_runs")
