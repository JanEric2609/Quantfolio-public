"""Add missing indexes on foreign key columns across tables.

Revision ID: 0105_add_fk_indexes
Revises: 0104_unwrap_fundamental_list
"""
from alembic import op
import sqlalchemy as sa

revision = "0105_add_fk_indexes"
down_revision = "0104_unwrap_fundamental_list"
branch_labels = None
depends_on = None

FK_INDEXES = [
    ("ix_recommendation_dossiers_attribution_run_id", "recommendation_dossiers", ["attribution_run_id"]),
    ("ix_recommendation_dossiers_mc_run_id", "recommendation_dossiers", ["mc_run_id"]),
    ("ix_strategy_lessons_portfolio_id", "strategy_lessons", ["portfolio_id"]),
    ("ix_strategy_lessons_source_scorecard_id", "strategy_lessons", ["source_scorecard_id"]),
    ("ix_advisor_graduation_state_strategy_id", "advisor_graduation_state", ["strategy_id"]),
    ("ix_passkey_credentials_user_id", "passkey_credentials", ["user_id"]),
    ("ix_expenses_category_id", "expenses", ["category_id"]),
    ("ix_expense_rules_category_id", "expense_rules", ["category_id"]),
    ("ix_subscriptions_category_id", "subscriptions", ["category_id"]),
    ("ix_competition_runs_portfolio_a_id", "competition_runs", ["portfolio_a_id"]),
    ("ix_competition_runs_portfolio_b_id", "competition_runs", ["portfolio_b_id"]),
    ("ix_competition_decisions_run_id", "competition_decisions", ["run_id"]),
    ("ix_competition_decisions_portfolio_id", "competition_decisions", ["portfolio_id"]),
    ("ix_discover_candidates_dossier_id", "discover_candidates", ["dossier_id"]),
    ("ix_discover_candidates_recommendation_id", "discover_candidates", ["recommendation_id"]),
    ("ix_discovery_config_parent_config_id", "discovery_config", ["parent_config_id"]),
    ("ix_discovery_config_review_champion_id", "discovery_config_review", ["champion_id"]),
    ("ix_discovery_config_review_promoted_id", "discovery_config_review", ["promoted_id"]),
    ("ix_dkb_transactions_account_id", "dkb_transactions", ["account_id"]),
    ("ix_dkb_transactions_category_id", "dkb_transactions", ["category_id"]),
    ("ix_dkb_positions_account_id", "dkb_positions", ["account_id"]),
    ("ix_paper_portfolios_user_id", "paper_portfolios", ["user_id"]),
    ("ix_paper_holdings_portfolio_id", "paper_holdings", ["portfolio_id"]),
    ("ix_paper_trades_portfolio_id", "paper_trades", ["portfolio_id"]),
    ("ix_paper_trades_holding_id", "paper_trades", ["holding_id"]),
    ("ix_paper_snapshots_portfolio_id", "paper_snapshots", ["portfolio_id"]),
    ("ix_llm_portfolio_decisions_portfolio_id", "llm_portfolio_decisions", ["portfolio_id"]),
    ("ix_llm_advice_cards_user_id", "llm_advice_cards", ["user_id"]),
    ("ix_llm_advice_cards_portfolio_id", "llm_advice_cards", ["portfolio_id"]),
    ("ix_metrics_snapshots_portfolio_id", "metrics_snapshots", ["portfolio_id"]),
    ("ix_composite_membership_composite_id", "composite_membership", ["composite_id"]),
    ("ix_composite_membership_portfolio_id", "composite_membership", ["portfolio_id"]),
    ("ix_holdings_portfolio_id", "holdings", ["portfolio_id"]),
    ("ix_transactions_log_holding_id", "transactions_log", ["holding_id"]),
    ("ix_activity_ledger_entries_connected_account_id", "activity_ledger_entries", ["connected_account_id"]),
    ("ix_stock_research_reports_user_id", "stock_research_reports", ["user_id"]),
    ("ix_multi_horizon_verdicts_user_id", "multi_horizon_verdicts", ["user_id"]),
    ("ix_security_aliases_security_id", "security_aliases", ["security_id"]),
    ("ix_tax_residency_periods_user_id", "tax_residency_periods", ["user_id"]),
    ("ix_verification_alerts_portfolio_id", "verification_alerts", ["portfolio_id"]),
]


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

    for index_name, table_name, columns in FK_INDEXES:
        if inspector.has_table(table_name):
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            if all(col in existing_cols for col in columns):
                existing_indexes = {ix["name"] for ix in inspector.get_indexes(table_name)}
                if index_name not in existing_indexes:
                    safe_create_index(index_name, table_name, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for index_name, table_name, _ in FK_INDEXES:
        if inspector.has_table(table_name):
            existing_indexes = {ix["name"] for ix in inspector.get_indexes(table_name)}
            if index_name in existing_indexes:
                safe_drop_index(index_name, table_name=table_name)
