"""Phase 4: AlphaCrafter (Miner / Screener / Trader) tables

Revision ID: 0018_phase4_alphacrafter
Revises: 0017_attribution_engine
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_phase4_alphacrafter"
down_revision = "0017_attribution_engine"
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

    # Create factors_library table
    if not inspector.has_table("factors_library"):
        op.create_table(
            "factors_library",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("formula_json", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ic_summary_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_factors_library_retired_at", "factors_library", ["retired_at"])
        safe_create_index("ix_factors_library_name", "factors_library", ["name"])

    # Create alpha_signals hypertable (7-day chunks)
    if not inspector.has_table("alpha_signals"):
        op.create_table(
            "alpha_signals",
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("factor_id", sa.UUID(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("ic_window_value", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("symbol", "factor_id", "ts"),
            sa.ForeignKeyConstraint(["factor_id"], ["factors_library.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_alpha_signals_factor_id_ts", "alpha_signals", ["factor_id", sa.desc("ts")])
        safe_create_index("ix_alpha_signals_symbol_ts", "alpha_signals", ["symbol", sa.desc("ts")])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('alpha_signals', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))

    # Create screener_runs table
    if not inspector.has_table("screener_runs"):
        op.create_table(
            "screener_runs",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("regime_label", sa.String(length=80), nullable=True),
            sa.Column("selected_factor_ids", sa.Text(), nullable=False),
            sa.Column("scores_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_screener_runs_ts_desc", "screener_runs", [sa.desc("ts")])

    # Create trader_backtests table
    if not inspector.has_table("trader_backtests"):
        op.create_table(
            "trader_backtests",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("screener_run_id", sa.UUID(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["screener_run_id"], ["screener_runs.id"], ondelete="CASCADE"),
        )
        safe_create_index("ix_trader_backtests_screener_run_id", "trader_backtests", ["screener_run_id"])
        safe_create_index("ix_trader_backtests_created_at", "trader_backtests", [sa.desc("created_at")])

    # Create recommendation_dossiers table
    if not inspector.has_table("recommendation_dossiers"):
        op.create_table(
            "recommendation_dossiers",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("conviction", sa.Float(), nullable=False),
            sa.Column("dossier_json", sa.Text(), nullable=False),
            sa.Column("attribution_run_id", sa.UUID(), nullable=True),
            sa.Column("mc_run_id", sa.UUID(), nullable=True),
            sa.Column("goal_ids", sa.Text(), nullable=True),
            sa.Column("aspect_ids", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["attribution_run_id"], ["attribution_runs.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["mc_run_id"], ["mc_runs.id"], ondelete="SET NULL"),
        )
        safe_create_index("ix_recommendation_dossiers_ts_desc", "recommendation_dossiers", [sa.desc("ts")])
        safe_create_index("ix_recommendation_dossiers_conviction_desc", "recommendation_dossiers", [sa.desc("conviction")])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("recommendation_dossiers"):
        op.drop_table("recommendation_dossiers")
    if inspector.has_table("trader_backtests"):
        op.drop_table("trader_backtests")
    if inspector.has_table("screener_runs"):
        op.drop_table("screener_runs")
    if inspector.has_table("alpha_signals"):
        op.drop_table("alpha_signals")
    if inspector.has_table("factors_library"):
        op.drop_table("factors_library")
