"""recreate analytics tables dropped by 2ce7981ac5c5

The head migration 2ce7981ac5c5 (fk_indices_and_relationships) drops seven
analytics tables in its ``upgrade()`` (bar_prices, regime_snapshots,
provider_health_history, factor_loadings_daily, recommendation_reports,
lean_backtest_runs, tax_year_summaries, recommendation_items) and only
recreates them in its ``downgrade()``. On production (PostgreSQL) the app does
NOT call ``Base.metadata.create_all`` at startup (that path is local/SQLite
only), so once the database reached head those tables were deleted and the
price-history / regime / provider-health data they held was lost. QuantLab,
regime gating, price backfill, recommendations and tax summaries all read or
write these tables.

This migration restores them at head (idempotently, guarded by has_table) so a
routine ``alembic upgrade head`` recreates the schema without touching any
data that may already have been re-backfilled. The four time-series tables are
recreated as TimescaleDB hypertables (matching 0015_hypertables), including the
continuous aggregates and retention policy for bar_prices.

Revision ID: 2ce7981ac5c6_recreate_analytics
Revises: 2ce7981ac5c5
Create Date: 2026-08-29 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2ce7981ac5c6_recreate_analytics"
down_revision: Union[str, Sequence[str], None] = "2ce7981ac5c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_exists(inspector, table_name, index_name):
    """True if an index named ``index_name`` already exists on ``table_name``."""
    try:
        return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return False


def safe_create_index(inspector, table_name, index_name, columns, unique=False):
    resolved = op.f(index_name)
    if not _index_exists(inspector, table_name, resolved):
        op.create_index(resolved, table_name, columns, unique=unique)


def _create_hypertable(bind, table, column, interval):
    """Convert ``table`` to a TimescaleDB hypertable on PostgreSQL only (no-op on SQLite)."""
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "SELECT create_hypertable('{tbl}', '{col}', "
                "chunk_time_interval => interval '{iv}', if_not_exists => TRUE)".format(
                    tbl=table, col=column, iv=interval
                )
            )
        )


# CAGG creation moved to 2ce7981ac5c7_bar_prices_caggs: TimescaleDB continuous
# aggregates cannot be created inside Alembic's transactional DDL block (the
# autocommit connection cannot see the not-yet-committed bar_prices table).


def upgrade() -> None:
    """Upgrade schema: recreate the seven analytics tables if missing."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # bar_prices (hypertable)
    if not inspector.has_table("bar_prices"):
        op.create_table(
            "bar_prices",
            sa.Column("id", sa.BIGINT(), nullable=False),
            sa.Column("symbol", sa.VARCHAR(length=32), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("open", sa.FLOAT(), nullable=False),
            sa.Column("high", sa.FLOAT(), nullable=False),
            sa.Column("low", sa.FLOAT(), nullable=False),
            sa.Column("close", sa.FLOAT(), nullable=False),
            sa.Column("volume", sa.BIGINT(), nullable=False),
            sa.Column("currency", sa.VARCHAR(length=3), server_default=sa.text("'USD'"), nullable=False),
            sa.Column("provider", sa.VARCHAR(length=40), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts", name=op.f("pk_bar_prices")),
        )
        safe_create_index(inspector, "bar_prices", "ix_bar_prices_symbol_ts", ["symbol", "ts"])
        _create_hypertable(bind, "bar_prices", "ts", "7 days")

    # factor_loadings_daily (hypertable)
    if not inspector.has_table("factor_loadings_daily"):
        op.create_table(
            "factor_loadings_daily",
            sa.Column("id", sa.BIGINT(), nullable=False),
            sa.Column("symbol", sa.VARCHAR(length=32), nullable=False),
            sa.Column("factor", sa.VARCHAR(length=80), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("loading", sa.FLOAT(), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts", name=op.f("pk_factor_loadings_daily")),
        )
        safe_create_index(inspector, "factor_loadings_daily", "ix_factor_loadings_daily_symbol_factor_ts", ["symbol", "factor", "ts"])
        safe_create_index(inspector, "factor_loadings_daily", "ix_factor_loadings_daily_factor_ts", ["factor", "ts"])
        _create_hypertable(bind, "factor_loadings_daily", "ts", "30 days")

    # regime_snapshots (hypertable)
    if not inspector.has_table("regime_snapshots"):
        op.create_table(
            "regime_snapshots",
            sa.Column("id", sa.BIGINT(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("label", sa.VARCHAR(length=80), nullable=False),
            sa.Column("score", sa.FLOAT(), nullable=False),
            sa.Column("source", sa.VARCHAR(length=40), nullable=False),
            sa.Column("payload_json", sa.TEXT(), server_default=sa.text("'{}'"), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts", name=op.f("pk_regime_snapshots")),
        )
        safe_create_index(inspector, "regime_snapshots", "ix_regime_snapshots_label_ts", ["label", "ts"])
        _create_hypertable(bind, "regime_snapshots", "ts", "30 days")

    # provider_health_history (hypertable)
    if not inspector.has_table("provider_health_history"):
        op.create_table(
            "provider_health_history",
            sa.Column("id", sa.BIGINT(), nullable=False),
            sa.Column("provider", sa.VARCHAR(length=40), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("capability", sa.VARCHAR(length=80), nullable=False),
            sa.Column("ok", sa.BOOLEAN(), nullable=False),
            sa.Column("latency_ms", sa.FLOAT(), nullable=True),
            sa.Column("message", sa.TEXT(), nullable=True),
            sa.PrimaryKeyConstraint("id", "ts", name=op.f("pk_provider_health_history")),
        )
        safe_create_index(inspector, "provider_health_history", "ix_provider_health_history_provider_ts", ["provider", "ts"])
        _create_hypertable(bind, "provider_health_history", "ts", "7 days")

    # recommendation_reports
    if not inspector.has_table("recommendation_reports"):
        op.create_table(
            "recommendation_reports",
            sa.Column("id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("user_id", sa.VARCHAR(length=36), nullable=True),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("portfolio_value", sa.FLOAT(), server_default=sa.text("'0.0'"), nullable=True),
            sa.Column("currency", sa.VARCHAR(length=3), server_default=sa.text("'EUR'"), nullable=True),
            sa.Column("regime_label", sa.VARCHAR(length=32), server_default=sa.text("'unknown'"), nullable=True),
            sa.Column("regime_confidence", sa.FLOAT(), server_default=sa.text("'0.0'"), nullable=True),
            sa.Column("estimate", sa.BOOLEAN(), server_default=sa.text("(true)"), nullable=True),
            sa.Column("not_tax_advice", sa.BOOLEAN(), server_default=sa.text("(true)"), nullable=True),
            sa.Column("weights_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=True),
            sa.Column("source_health_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=True),
            sa.Column("raw_llm_output", sa.TEXT(), server_default=sa.text("('')"), nullable=True),
            sa.Column("validated_output", sa.TEXT(), server_default=sa.text("('')"), nullable=True),
            sa.Column("stripped_claims_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_recommendation_reports_user_id_users"), ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_reports")),
        )
        safe_create_index(inspector, "recommendation_reports", "ix_recommendation_reports_user_id", ["user_id"])

    # lean_backtest_runs
    if not inspector.has_table("lean_backtest_runs"):
        op.create_table(
            "lean_backtest_runs",
            sa.Column("id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("user_id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("project_name", sa.VARCHAR(length=160), nullable=False),
            sa.Column("status", sa.VARCHAR(length=24), server_default=sa.text("'created'"), nullable=False),
            sa.Column("command", sa.VARCHAR(length=80), server_default=sa.text("'backtest'"), nullable=False),
            sa.Column("project_dir", sa.TEXT(), nullable=True),
            sa.Column("template_json", sa.TEXT(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("result_json", sa.TEXT(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("error_message", sa.TEXT(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_lean_backtest_runs_user_id_users"), ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_lean_backtest_runs")),
        )
        safe_create_index(inspector, "lean_backtest_runs", "ix_lean_backtest_runs_user_id", ["user_id"])
        safe_create_index(inspector, "lean_backtest_runs", "ix_lean_backtest_runs_status", ["status"])

    # tax_year_summaries
    if not inspector.has_table("tax_year_summaries"):
        op.create_table(
            "tax_year_summaries",
            sa.Column("id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("user_id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("tax_year", sa.INTEGER(), nullable=False),
            sa.Column("payload_json", sa.TEXT(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_tax_year_summaries_user_id_users"), ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_tax_year_summaries")),
            sa.UniqueConstraint("user_id", "tax_year", name=op.f("uq_tax_year_summaries_user_year")),
        )
        safe_create_index(inspector, "tax_year_summaries", "ix_tax_year_summaries_user_id", ["user_id"])
        safe_create_index(inspector, "tax_year_summaries", "ix_tax_year_summaries_tax_year", ["tax_year"])

    # recommendation_items
    if not inspector.has_table("recommendation_items"):
        op.create_table(
            "recommendation_items",
            sa.Column("id", sa.VARCHAR(length=36), nullable=False),
            sa.Column("report_id", sa.VARCHAR(length=36), nullable=True),
            sa.Column("ticker", sa.VARCHAR(length=32), nullable=False),
            sa.Column("action", sa.VARCHAR(length=16), nullable=False),
            sa.Column("confidence", sa.VARCHAR(length=16), server_default=sa.text("'medium'"), nullable=True),
            sa.Column("timeframe", sa.VARCHAR(length=64), server_default=sa.text("'3-6 months'"), nullable=True),
            sa.Column("thesis", sa.TEXT(), server_default=sa.text("('')"), nullable=True),
            sa.Column("evidence_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=True),
            sa.Column("risks_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=True),
            sa.Column("data_quality", sa.VARCHAR(length=16), server_default=sa.text("'complete'"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["report_id"], ["recommendation_reports.id"], name=op.f("fk_recommendation_items_report_id_recommendation_reports"), ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_items")),
        )
        safe_create_index(inspector, "recommendation_items", "ix_recommendation_items_ticker", ["ticker"])
        safe_create_index(inspector, "recommendation_items", "ix_recommendation_items_report_id", ["report_id"])


def downgrade() -> None:
    """Downgrade schema: drop the recreated tables (and bar_prices continuous aggregates)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("bar_prices_monthly"):
        bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_monthly CASCADE"))
    if inspector.has_table("bar_prices_weekly"):
        bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_weekly CASCADE"))

    if inspector.has_table("recommendation_items"):
        op.drop_table("recommendation_items")
    if inspector.has_table("tax_year_summaries"):
        op.drop_table("tax_year_summaries")
    if inspector.has_table("lean_backtest_runs"):
        op.drop_table("lean_backtest_runs")
    if inspector.has_table("recommendation_reports"):
        op.drop_table("recommendation_reports")
    if inspector.has_table("provider_health_history"):
        op.drop_table("provider_health_history")
    if inspector.has_table("regime_snapshots"):
        op.drop_table("regime_snapshots")
    if inspector.has_table("factor_loadings_daily"):
        op.drop_table("factor_loadings_daily")
    if inspector.has_table("bar_prices"):
        op.drop_table("bar_prices")
