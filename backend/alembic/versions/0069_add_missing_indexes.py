"""Add missing DB indexes on FK columns used in WHERE/JOIN/ORDER BY queries.

Revision ID: 0069_add_missing_indexes
Revises: 0068_promote_sole_user_admin
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa


revision = "0069_add_missing_indexes"
down_revision = "0068_promote_sole_user_admin"
branch_labels = None
depends_on = None


def _ensure_index(index_name: str, table: str, columns: list[str]) -> None:
    """Create index if table exists and index does not already exist."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return
    existing = {ix["name"] for ix in inspector.get_indexes(table)}
    if index_name not in existing:
        safe_create_index(index_name, table, columns)


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
    # ── Portfolio domain ──────────────────────────────────────────
    _ensure_index("ix_portfolios_user_id", "portfolios", ["user_id"])
    _ensure_index("ix_holdings_portfolio_id", "holdings", ["portfolio_id"])
    _ensure_index("ix_transactions_log_holding_id", "transactions_log", ["holding_id"])
    _ensure_index("ix_activity_ledger_entries_connected_account_id", "activity_ledger_entries", ["connected_account_id"])

    # ── Paper portfolio domain ────────────────────────────────────
    _ensure_index("ix_paper_portfolios_user_id", "paper_portfolios", ["user_id"])
    _ensure_index("ix_paper_holdings_portfolio_id", "paper_holdings", ["portfolio_id"])
    _ensure_index("ix_paper_trades_portfolio_id", "paper_trades", ["portfolio_id"])
    _ensure_index("ix_paper_trades_holding_id", "paper_trades", ["holding_id"])
    _ensure_index("ix_llm_portfolio_decisions_portfolio_id", "llm_portfolio_decisions", ["portfolio_id"])
    _ensure_index("ix_llm_advice_cards_user_id", "llm_advice_cards", ["user_id"])
    _ensure_index("ix_llm_advice_cards_portfolio_id", "llm_advice_cards", ["portfolio_id"])

    # ── Competition domain ────────────────────────────────────────
    _ensure_index("ix_competition_runs_portfolio_a_id", "competition_runs", ["portfolio_a_id"])
    _ensure_index("ix_competition_runs_portfolio_b_id", "competition_runs", ["portfolio_b_id"])
    _ensure_index("ix_competition_decisions_run_id", "competition_decisions", ["run_id"])
    _ensure_index("ix_competition_decisions_portfolio_id", "competition_decisions", ["portfolio_id"])

    # ── Auth domain ───────────────────────────────────────────────
    _ensure_index("ix_passkey_credentials_user_id", "passkey_credentials", ["user_id"])

    # ── DKB domain ────────────────────────────────────────────────
    _ensure_index("ix_dkb_transactions_account_id", "dkb_transactions", ["account_id"])
    _ensure_index("ix_dkb_transactions_category_id", "dkb_transactions", ["category_id"])
    _ensure_index("ix_dkb_sync_logs_created_at", "dkb_sync_logs", ["created_at"])

    # ── Budget domain ─────────────────────────────────────────────
    _ensure_index("ix_expenses_category_id", "expenses", ["category_id"])
    _ensure_index("ix_expense_rules_category_id", "expense_rules", ["category_id"])
    _ensure_index("ix_subscriptions_category_id", "subscriptions", ["category_id"])

    # ── AlphaCrafter domain ───────────────────────────────────────
    _ensure_index("ix_recommendation_dossiers_attribution_run_id", "recommendation_dossiers", ["attribution_run_id"])
    _ensure_index("ix_recommendation_dossiers_mc_run_id", "recommendation_dossiers", ["mc_run_id"])
    _ensure_index("ix_alphacrafter_job_runs_created_at", "alphacrafter_job_runs", ["created_at"])

    # ── Discover domain ───────────────────────────────────────────
    _ensure_index("ix_discover_candidates_dossier_id", "discover_candidates", ["dossier_id"])
    _ensure_index("ix_discover_candidates_recommendation_id", "discover_candidates", ["recommendation_id"])
    _ensure_index("ix_discovery_config_parent_config_id", "discovery_config", ["parent_config_id"])
    _ensure_index("ix_discovery_config_review_champion_id", "discovery_config_review", ["champion_id"])
    _ensure_index("ix_discovery_config_review_promoted_id", "discovery_config_review", ["promoted_id"])

    # ── Research domain ───────────────────────────────────────────
    _ensure_index("ix_stock_research_reports_user_id", "stock_research_reports", ["user_id"])
    _ensure_index("ix_multi_horizon_verdicts_user_id", "multi_horizon_verdicts", ["user_id"])

    # ── Quant domain ──────────────────────────────────────────────
    _ensure_index("ix_quant_runs_created_at", "quant_runs", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _drop_if_exists(index_name: str, table_name: str) -> None:
        if inspector.has_table(table_name):
            if any(ix["name"] == index_name for ix in inspector.get_indexes(table_name)):
                safe_drop_index(index_name, table_name=table_name)

    # ── Portfolio domain ──────────────────────────────────────────
    _drop_if_exists("ix_portfolios_user_id", "portfolios")
    _drop_if_exists("ix_holdings_portfolio_id", "holdings")
    _drop_if_exists("ix_transactions_log_holding_id", "transactions_log")
    _drop_if_exists("ix_activity_ledger_entries_connected_account_id", "activity_ledger_entries")

    # ── Paper portfolio domain ────────────────────────────────────
    _drop_if_exists("ix_paper_portfolios_user_id", "paper_portfolios")
    _drop_if_exists("ix_paper_holdings_portfolio_id", "paper_holdings")
    _drop_if_exists("ix_paper_trades_portfolio_id", "paper_trades")
    _drop_if_exists("ix_paper_trades_holding_id", "paper_trades")
    _drop_if_exists("ix_llm_portfolio_decisions_portfolio_id", "llm_portfolio_decisions")
    _drop_if_exists("ix_llm_advice_cards_user_id", "llm_advice_cards")
    _drop_if_exists("ix_llm_advice_cards_portfolio_id", "llm_advice_cards")

    # ── Competition domain ────────────────────────────────────────
    _drop_if_exists("ix_competition_runs_portfolio_a_id", "competition_runs")
    _drop_if_exists("ix_competition_runs_portfolio_b_id", "competition_runs")
    _drop_if_exists("ix_competition_decisions_run_id", "competition_decisions")
    _drop_if_exists("ix_competition_decisions_portfolio_id", "competition_decisions")

    # ── Auth domain ───────────────────────────────────────────────
    _drop_if_exists("ix_passkey_credentials_user_id", "passkey_credentials")

    # ── DKB domain ────────────────────────────────────────────────
    _drop_if_exists("ix_dkb_transactions_account_id", "dkb_transactions")
    _drop_if_exists("ix_dkb_transactions_category_id", "dkb_transactions")
    _drop_if_exists("ix_dkb_sync_logs_created_at", "dkb_sync_logs")

    # ── Budget domain ─────────────────────────────────────────────
    _drop_if_exists("ix_expenses_category_id", "expenses")
    _drop_if_exists("ix_expense_rules_category_id", "expense_rules")
    _drop_if_exists("ix_subscriptions_category_id", "subscriptions")

    # ── AlphaCrafter domain ───────────────────────────────────────
    _drop_if_exists("ix_recommendation_dossiers_attribution_run_id", "recommendation_dossiers")
    _drop_if_exists("ix_recommendation_dossiers_mc_run_id", "recommendation_dossiers")
    _drop_if_exists("ix_alphacrafter_job_runs_created_at", "alphacrafter_job_runs")

    # ── Discover domain ───────────────────────────────────────────
    _drop_if_exists("ix_discover_candidates_dossier_id", "discover_candidates")
    _drop_if_exists("ix_discover_candidates_recommendation_id", "discover_candidates")
    _drop_if_exists("ix_discovery_config_parent_config_id", "discovery_config")
    _drop_if_exists("ix_discovery_config_review_champion_id", "discovery_config_review")
    _drop_if_exists("ix_discovery_config_review_promoted_id", "discovery_config_review")

    # ── Research domain ───────────────────────────────────────────
    _drop_if_exists("ix_stock_research_reports_user_id", "stock_research_reports")
    _drop_if_exists("ix_multi_horizon_verdicts_user_id", "multi_horizon_verdicts")

    # ── Quant domain ──────────────────────────────────────────────
    _drop_if_exists("ix_quant_runs_created_at", "quant_runs")
