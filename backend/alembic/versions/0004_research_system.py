"""research system scaffolding

Revision ID: 0004_research_system
Revises: 0003_dkb_ai_budget_quant
Create Date: 2026-05-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_research_system"
down_revision = "0003_dkb_ai_budget_quant"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column_if_missing(inspector: sa.Inspector, table: str, column: sa.Column) -> None:
    if table in inspector.get_table_names() and column.name not in _columns(inspector, table):
        op.add_column(table, column)


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

    if not inspector.has_table("assets"):
        op.create_table(
            "assets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("exchange", sa.String(length=32), nullable=True),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("asset_type", sa.String(length=24), nullable=False, server_default="stock"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("country", sa.String(length=80), nullable=True),
            sa.Column("region", sa.String(length=80), nullable=True),
            sa.Column("sector", sa.String(length=120), nullable=True),
            sa.Column("industry", sa.String(length=160), nullable=True),
            sa.Column("ucits", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("accumulating", sa.Boolean(), nullable=True),
            sa.Column("distributing", sa.Boolean(), nullable=True),
            sa.Column("ter", sa.Numeric(8, 4), nullable=True),
            sa.Column("provider_meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("isin", "exchange", "currency", name="uq_assets_isin_exchange_currency"),
        )
        safe_create_index("ix_assets_isin", "assets", ["isin"])
        safe_create_index("ix_assets_symbol", "assets", ["symbol"])
        safe_create_index("ix_assets_exchange", "assets", ["exchange"])
        safe_create_index("ix_assets_symbol_exchange", "assets", ["symbol", "exchange"])

    _add_column_if_missing(inspector, "recommendations", sa.Column("recommendation_v2_payload_json", sa.Text(), nullable=True))
    _add_column_if_missing(inspector, "recommendations", sa.Column("recommendation_expiry", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing(
        inspector,
        "recommendations",
        sa.Column("approval_state", sa.String(length=32), nullable=False, server_default="draft"),
    )
    _add_column_if_missing(inspector, "recommendations", sa.Column("mode", sa.String(length=24), nullable=True))
    _add_column_if_missing(inspector, "recommendations", sa.Column("data_quality_score", sa.Numeric(6, 2), nullable=True))
    _add_column_if_missing(inspector, "recommendations", sa.Column("risk_score", sa.Numeric(6, 2), nullable=True))
    _add_column_if_missing(inspector, "recommendations", sa.Column("portfolio_fit_score", sa.Numeric(6, 2), nullable=True))

    if inspector.has_table("recommendations"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("recommendations")}
        if "ix_recommendations_recommendation_expiry" not in existing_indexes:
            safe_create_index("ix_recommendations_recommendation_expiry", "recommendations", ["recommendation_expiry"])
        if "ix_recommendations_approval_state" not in existing_indexes:
            safe_create_index("ix_recommendations_approval_state", "recommendations", ["approval_state"])
        if "ix_recommendations_mode" not in existing_indexes:
            safe_create_index("ix_recommendations_mode", "recommendations", ["mode"])

    if not inspector.has_table("recommendation_reviews"):
        op.create_table(
            "recommendation_reviews",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("recommendation_id", sa.String(length=36), sa.ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("from_state", sa.String(length=32), nullable=True),
            sa.Column("to_state", sa.String(length=32), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_recommendation_reviews_recommendation_id", "recommendation_reviews", ["recommendation_id"])
        safe_create_index("ix_recommendation_reviews_user_id", "recommendation_reviews", ["user_id"])

    if not inspector.has_table("llm_audit_events"):
        op.create_table(
            "llm_audit_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("agent", sa.String(length=80), nullable=False, server_default="chat"),
            sa.Column("purpose", sa.String(length=120), nullable=False, server_default="general"),
            sa.Column("prompt_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("response_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("redacted_prompt_json", sa.Text(), nullable=True),
            sa.Column("redacted_response_json", sa.Text(), nullable=True),
            sa.Column("retention_state", sa.String(length=24), nullable=False, server_default="raw"),
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_llm_audit_events_user_id", "llm_audit_events", ["user_id"])
        safe_create_index("ix_llm_audit_events_retention_state", "llm_audit_events", ["retention_state"])
        safe_create_index("ix_llm_audit_events_created_state", "llm_audit_events", ["created_at", "retention_state"])

    if not inspector.has_table("quant_experiments"):
        op.create_table(
            "quant_experiments",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("mode", sa.String(length=24), nullable=False, server_default="long_term"),
            sa.Column("universe_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("benchmark_symbol", sa.String(length=32), nullable=True),
            sa.Column("start_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("rebalance_frequency", sa.String(length=24), nullable=False, server_default="monthly"),
            sa.Column("strategy_type", sa.String(length=80), nullable=False, server_default="momentum"),
            sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        safe_create_index("ix_quant_experiments_user_id", "quant_experiments", ["user_id"])

    if not inspector.has_table("quant_experiment_runs"):
        op.create_table(
            "quant_experiment_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("experiment_id", sa.String(length=36), sa.ForeignKey("quant_experiments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("equity_curve_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("trades_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("error_message", sa.Text(), nullable=True),
        )
        safe_create_index("ix_quant_experiment_runs_experiment_id", "quant_experiment_runs", ["experiment_id"])
        safe_create_index("ix_quant_experiment_runs_status", "quant_experiment_runs", ["status"])

    if not inspector.has_table("quant_signals"):
        op.create_table(
            "quant_signals",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("run_id", sa.String(length=36), sa.ForeignKey("quant_experiment_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("signal_name", sa.String(length=80), nullable=False),
            sa.Column("signal_value", sa.Numeric(20, 8), nullable=True),
            sa.Column("as_of_date", sa.Date(), nullable=True),
        )
        safe_create_index("ix_quant_signals_run_id", "quant_signals", ["run_id"])
        safe_create_index("ix_quant_signals_symbol", "quant_signals", ["symbol"])
        safe_create_index("ix_quant_signals_isin", "quant_signals", ["isin"])

    if not inspector.has_table("quant_factor_scores"):
        op.create_table(
            "quant_factor_scores",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("run_id", sa.String(length=36), sa.ForeignKey("quant_experiment_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("asset_id", sa.String(length=36), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("factor_name", sa.String(length=80), nullable=False),
            sa.Column("factor_value", sa.Numeric(20, 8), nullable=True),
            sa.Column("factor_score", sa.Numeric(6, 2), nullable=True),
            sa.Column("as_of_date", sa.Date(), nullable=True),
        )
        safe_create_index("ix_quant_factor_scores_run_id", "quant_factor_scores", ["run_id"])
        safe_create_index("ix_quant_factor_scores_asset_id", "quant_factor_scores", ["asset_id"])
        safe_create_index("ix_quant_factor_scores_symbol", "quant_factor_scores", ["symbol"])
        safe_create_index("ix_quant_factor_scores_isin", "quant_factor_scores", ["isin"])

    if not inspector.has_table("strategy_graveyard"):
        op.create_table(
            "strategy_graveyard",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("failed_metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_strategy_graveyard_user_id", "strategy_graveyard", ["user_id"])

    if not inspector.has_table("data_quality_events"):
        op.create_table(
            "data_quality_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("severity", sa.String(length=24), nullable=False, server_default="warning"),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_data_quality_events_provider", "data_quality_events", ["provider"])
        safe_create_index("ix_data_quality_events_symbol", "data_quality_events", ["symbol"])
        safe_create_index("ix_data_quality_events_isin", "data_quality_events", ["isin"])

    if not inspector.has_table("lean_backtest_runs"):
        op.create_table(
            "lean_backtest_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_name", sa.String(length=160), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="created"),
            sa.Column("command", sa.String(length=80), nullable=False, server_default="backtest"),
            sa.Column("project_dir", sa.Text(), nullable=True),
            sa.Column("template_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_lean_backtest_runs_user_id", "lean_backtest_runs", ["user_id"])
        safe_create_index("ix_lean_backtest_runs_status", "lean_backtest_runs", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "lean_backtest_runs",
        "data_quality_events",
        "strategy_graveyard",
        "quant_factor_scores",
        "quant_signals",
        "quant_experiment_runs",
        "quant_experiments",
        "llm_audit_events",
        "recommendation_reviews",
        "assets",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
