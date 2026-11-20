"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-15

NOTE (rewritten 2026-08-20): the original body of this migration called
``Base.metadata.create_all(bind=op.get_bind())``, which reflects TODAY's
live ``app.foundation.models.entities`` models rather than the schema as it existed
when this migration was first written. On an incrementally-upgraded
database that never mattered (0001 never re-runs once stamped), but on a
genuinely fresh install it meant 0001 silently created every table/column
the 89 downstream migrations expect to add themselves -- so every
``if not inspector.has_table(...)``/``has_column(...)`` guard in 0002..head
saw its target already present and no-op'd. The guarded DDL in those
migrations was therefore never actually exercised on a fresh install.

This repository's git history has been squashed, so there is no recoverable
"originally intended" 0001 schema to restore. The table list below was
reconstructed empirically: starting from a no-op 0001, `alembic upgrade
head` was run against a blank SQLite database repeatedly, and each table
that a downstream migration could not correctly (re)create on its own was
added here explicitly, using today's ORM model shape -- frozen as literal
`op.create_table(...)`/`sa.Column(...)` calls below, not a live import of
`Base.metadata`, so future model edits cannot change this migration's
behavior again. Two categories of table ended up here:

  1. Tables no migration between 0002 and head ever (re)creates -- these
     must always have existed, i.e. they were part of the true original
     0001: users, categories, portfolios, holdings, transactions_log,
     expenses, watchlist, recommendations, quant_runs, app_settings,
     api_keys, backtest_results, benchmarks, chat_messages, dkb_accounts,
     dkb_positions, dkb_transactions, executive_summaries, fundamentals,
     invoices, news_items, passkey_credentials, price_cache, subscriptions.

  2. Tables whose creating migration (the "Phase 4/5/6" batch, 0016-0020)
     predates a later refactor of ID generation (server-side
     `gen_random_uuid()` into a native `UUID` column -> app-side
     `uuid_pk()` into `String(36)`) and, for four of them, a hypertable
     composite `(id, ts)`/`(id, as_of)` primary key that has since been
     simplified to a plain single-column `id` primary key. Running those
     migrations' original DDL as-is on a fresh install hard-crashes on
     SQLite (confirmed empirically: `sqlalchemy.exc.CompileError: SQLite
     does not support autoincrement for composite primary keys`, first hit
     in 0019 on `shadow_positions`) and, even where it would not crash on
     PostgreSQL, would silently create a schema that no longer matches the
     current ORM models: mc_runs, mc_path_samples, composites,
     attribution_runs, performance_ledger_entries, factors_library,
     screener_runs, trader_backtests, recommendation_dossiers,
     shadow_positions, q_values, meta_policy_snapshots, nudges, embeddings,
     finagent_runs.

  3. portfolio_snapshots: created fine by 0005, but a later migration (0045)
     evolves it via `batch_alter_table(...).add_column(sa.Column(...,
     sa.ForeignKey(...)))`. SQLite batch mode requires named constraints
     when recreating a table around a new FK column, and this one is
     unnamed, so running 0045 for real on a fresh SQLite install raises
     `ValueError: Constraint must have a name` (confirmed empirically).
     Pre-creating it here with today's final shape (which already includes
     portfolio_id and both unique constraints) lets 0045 and 0051's guards
     skip it, exactly as happens today.

  4. discovery_config: created by 0067, which adds its self-referential
     parent_config_id FK via a standalone `op.create_foreign_key(...)` call
     (not inline in create_table). SQLite has no ALTER-ADD-CONSTRAINT
     support outside of batch mode, so running this for real on a fresh
     SQLite install raises `NotImplementedError: No support for ALTER of
     constraints in SQLite dialect` (confirmed empirically). Pre-creating
     the table here with the FK inline (supported by both dialects at
     CREATE TABLE time) lets 0067's guard skip it.

  5. alphacrafter_job_runs: created by 0034, but 0072 adds its user_id
     scoping column via a plain (non-batch) `op.add_column(table,
     sa.Column(..., sa.ForeignKey(...)))`. Same SQLite limitation as #4 --
     confirmed empirically. Pre-creating it here with today's final shape
     (already including user_id) lets 0072's guard skip it.

  6. composite_membership, alpha_signals, tax_residency_periods: each is
     created (without crashing) by its own migration (0017, 0018, 0021)
     using SQLAlchemy's native `sa.UUID()`/`sa.Uuid()` column type for what
     are today plain app-generated `String(36)` (uuid_pk()) id/FK columns --
     part of the same pre-refactor batch as #2, just surfaced by the full
     scratch1-vs-scratch2 schema diff rather than a hard crash. On
     PostgreSQL a `CHAR(32)` column (tax_residency_periods) would reject
     the app's 36-character dashed uuid string outright, and a native
     `UUID` column (composite_membership, alpha_signals) hands back
     `uuid.UUID` objects to a model that expects `str`. A handful of
     *other* mismatches the same diff turned up -- e.g.
     confidence_scores.portfolio_id and verification_alerts'
     user_id/portfolio_id as unbounded `String()` instead of `String(36)`,
     and several tables' created_at/updated_at ending up nullable instead
     of NOT NULL -- are deliberately left alone: those are safe supersets
     of today's constraint (anything valid under the tighter constraint is
     still valid under the looser one), so they're kept as evidence that
     0002..head's guarded DDL is now genuinely executing on a fresh
     install, which is the entire point of this rewrite.

Every other ORM-mapped table (~86 of the ~110 total, plus the handful of
raw-SQL-only hypertables such as bar_prices that have no ORM model at all)
is intentionally left OUT of this migration and is still created for real,
later in the chain, by its own guarded migration (0002..head) -- exactly as
a genuinely incremental install would exercise it. This migration is the
root: unlike every migration after it, it assumes a blank database and does
not guard its DDL with `has_table`/`has_column` checks.

Existing, already-migrated (production) databases are unaffected by this
change: they are already stamped past 0001 in `alembic_version` and will
never re-run it. This rewrite only changes the path a genuinely fresh
install takes.
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_schema"
down_revision = None
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
    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_executive_summary_generated", sa.Date(), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("risk_profile", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_users_username", "users", ['username'], unique=True)

    # --- categories ---
    op.create_table(
        "categories",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("color", sa.String(24), nullable=False),
        sa.Column("icon", sa.String(40), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("target_amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_categories_user_id", "categories", ['user_id'])

    # --- portfolios ---
    op.create_table(
        "portfolios",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_portfolios_user_id", "portfolios", ['user_id'])

    # --- holdings ---
    op.create_table(
        "holdings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("asset_type", sa.String(24), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("avg_buy_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("buy_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("dkb_available", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('portfolio_id', 'isin', name="uq_holdings_portfolio_isin"),
    )
    safe_create_index("ix_holdings_ticker", "holdings", ['ticker'])
    safe_create_index("ix_holdings_isin", "holdings", ['isin'])

    # --- transactions_log ---
    op.create_table(
        "transactions_log",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("holding_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("fees", sa.Numeric(20, 6), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['holding_id'], ['holdings.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )

    # --- expenses ---
    op.create_table(
        "expenses",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.String(250), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("dkb_dedupe_hash", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dkb_dedupe_hash'),
    )
    safe_create_index("ix_expenses_user_id", "expenses", ['user_id'])

    # --- watchlist ---
    op.create_table(
        "watchlist",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("added_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("horizon_tag", sa.String(16), nullable=False),
        sa.Column("target_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("alert_triggered", sa.Boolean(), nullable=False),
        sa.Column("alert_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_watchlist_ticker", "watchlist", ['ticker'])
    safe_create_index("ix_watchlist_isin", "watchlist", ['isin'])
    safe_create_index("ix_watchlist_user_id", "watchlist", ['user_id'])

    # --- recommendations ---
    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("horizon", sa.String(24), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Numeric(6, 4), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("backtest_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recommendation_v2_payload_json", sa.Text(), nullable=True),
        sa.Column("recommendation_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_state", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(24), nullable=True),
        sa.Column("data_quality_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("risk_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("portfolio_fit_score", sa.Numeric(6, 2), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_recommendations_user_id", "recommendations", ['user_id'])
    safe_create_index("ix_recommendations_mode", "recommendations", ['mode'])
    safe_create_index("ix_recommendations_recommendation_expiry", "recommendations", ['recommendation_expiry'])
    safe_create_index("ix_recommendations_approval_state", "recommendations", ['approval_state'])
    safe_create_index("ix_recommendations_ticker", "recommendations", ['ticker'])

    # --- quant_runs ---
    op.create_table(
        "quant_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_quant_runs_type", "quant_runs", ['type'])

    # --- app_settings ---
    op.create_table(
        "app_settings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_app_settings_key", "app_settings", ['key'], unique=True)

    # --- api_keys ---
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("meta_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_api_keys_service", "api_keys", ['service'], unique=True)

    # --- backtest_results ---
    op.create_table(
        "backtest_results",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("strategy", sa.String(40), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("results_json", sa.Text(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_backtest_results_ticker", "backtest_results", ['ticker'])
    safe_create_index("ix_backtest_results_user_id", "backtest_results", ['user_id'])
    safe_create_index("ix_backtest_results_strategy", "backtest_results", ['strategy'])

    # --- benchmarks ---
    op.create_table(
        "benchmarks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticker'),
    )

    # --- chat_messages ---
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_chat_messages_user_id", "chat_messages", ['user_id'])
    safe_create_index("ix_chat_messages_user_conv", "chat_messages", ['user_id', 'conversation_id'])

    # --- dkb_accounts ---
    op.create_table(
        "dkb_accounts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("iban", sa.String(34), nullable=True),
        sa.Column("balance", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'iban', name="uq_dkb_account_user_iban"),
    )
    safe_create_index("ix_dkb_accounts_user_id", "dkb_accounts", ['user_id'])
    safe_create_index("ix_dkb_accounts_iban", "dkb_accounts", ['iban'])

    # --- dkb_positions ---
    op.create_table(
        "dkb_positions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("isin", sa.String(12), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("avg_buy_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("current_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("current_value", sa.Numeric(20, 6), nullable=True),
        sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['dkb_accounts.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'isin', name="uq_dkb_positions_account_isin"),
    )
    safe_create_index("ix_dkb_positions_ticker", "dkb_positions", ['ticker'])
    safe_create_index("ix_dkb_positions_account_isin", "dkb_positions", ['account_id', 'isin'])
    safe_create_index("ix_dkb_positions_isin", "dkb_positions", ['isin'])

    # --- dkb_transactions ---
    op.create_table(
        "dkb_transactions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("dedupe_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['dkb_accounts.id'], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'dedupe_hash', name="uq_dkb_tx_account_hash"),
    )
    safe_create_index("ix_dkb_transactions_dedupe_hash", "dkb_transactions", ['dedupe_hash'])

    # --- executive_summaries ---
    op.create_table(
        "executive_summaries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'date', name="uq_summary_user_date"),
    )
    safe_create_index("ix_executive_summaries_date", "executive_summaries", ['date'])
    safe_create_index("ix_executive_summaries_user_id", "executive_summaries", ['user_id'])

    # --- fundamentals ---
    op.create_table(
        "fundamentals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_fundamentals_ticker", "fundamentals", ['ticker'], unique=True)

    # --- invoices ---
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("issuer", sa.String(160), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_invoices_user_id", "invoices", ['user_id'])

    # --- news_items ---
    op.create_table(
        "news_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("sentiment_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("sentiment_label", sa.String(24), nullable=True),
        sa.Column("relevance_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("relevance_label", sa.String(16), nullable=True),
        sa.Column("relevance_reason", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_macro", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_news_items_ticker", "news_items", ['ticker'])
    safe_create_index("ix_news_items_published_at", "news_items", ['published_at'])

    # --- passkey_credentials ---
    op.create_table(
        "passkey_credentials",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transports_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('credential_id'),
    )

    # --- price_cache ---
    op.create_table(
        "price_cache",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(20, 6), nullable=True),
        sa.Column("high", sa.Numeric(20, 6), nullable=True),
        sa.Column("low", sa.Numeric(20, 6), nullable=True),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
        sa.Column("volume", sa.Numeric(24, 2), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticker', 'date', name="uq_price_cache_ticker_date"),
    )
    safe_create_index("ix_price_cache_ticker", "price_cache", ['ticker'])
    safe_create_index("ix_price_cache_date", "price_cache", ['date'])

    # --- subscriptions ---
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("billing_cycle", sa.String(20), nullable=False),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=True),
        sa.Column("payment_method", sa.String(80), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_subscriptions_user_id", "subscriptions", ['user_id'])

    # --- mc_runs ---
    op.create_table(
        "mc_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("results_json", sa.Text(), nullable=False),
        sa.Column("paths_summary_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_mc_runs_user_id", "mc_runs", ['user_id'])
    safe_create_index("ix_mc_runs_created_at", "mc_runs", ['created_at'])

    # --- mc_path_samples ---
    op.create_table(
        "mc_path_samples",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False, autoincrement=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("path_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['mc_runs.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_mc_path_samples_ts", "mc_path_samples", ['ts'])
    safe_create_index("ix_mc_path_samples_run_id", "mc_path_samples", ['run_id'])

    # --- composites ---
    op.create_table(
        "composites",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_composites_user_id", "composites", ['user_id'])
    safe_create_index("ix_composites_created_at", "composites", ['created_at'])

    # --- attribution_runs ---
    op.create_table(
        "attribution_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("benchmark", sa.String(80), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_attribution_runs_portfolio_id", "attribution_runs", ['portfolio_id'])
    safe_create_index("ix_attribution_runs_created_at", "attribution_runs", ['created_at'])
    safe_create_index("ix_attribution_runs_user_id", "attribution_runs", ['user_id'])

    # --- performance_ledger_entries ---
    op.create_table(
        "performance_ledger_entries",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False, autoincrement=True),
        sa.Column("composite_id", sa.String(36), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("twr", sa.Numeric(20, 6), nullable=False),
        sa.Column("mwr", sa.Numeric(20, 6), nullable=False),
        sa.Column("dispersion", sa.Numeric(20, 6), nullable=True),
        sa.Column("ex_post_risk_json", sa.Text(), nullable=False),
        sa.Column("snapshot_meta_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['composite_id'], ['composites.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_performance_ledger_entries_as_of", "performance_ledger_entries", ['as_of'])
    safe_create_index("ix_performance_ledger_entries_composite_id", "performance_ledger_entries", ['composite_id'])

    # --- factors_library ---
    op.create_table(
        "factors_library",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("formula_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ic_summary_json", sa.Text(), nullable=False),
        sa.Column("regime_applicability", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_factors_library_retired_at", "factors_library", ['retired_at'])
    safe_create_index("ix_factors_library_name", "factors_library", ['name'])

    # --- screener_runs ---
    op.create_table(
        "screener_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime_label", sa.String(80), nullable=True),
        sa.Column("selected_factor_ids", sa.Text(), nullable=False),
        sa.Column("scores_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_screener_runs_ts", "screener_runs", ['ts'])

    # --- trader_backtests ---
    op.create_table(
        "trader_backtests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("screener_run_id", sa.String(36), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['screener_run_id'], ['screener_runs.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_trader_backtests_created_at", "trader_backtests", ['created_at'])
    safe_create_index("ix_trader_backtests_screener_run_id", "trader_backtests", ['screener_run_id'])

    # --- recommendation_dossiers ---
    op.create_table(
        "recommendation_dossiers",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conviction", sa.Float(), nullable=False),
        sa.Column("dossier_json", sa.Text(), nullable=False),
        sa.Column("attribution_run_id", sa.String(36), nullable=True),
        sa.Column("mc_run_id", sa.String(36), nullable=True),
        sa.Column("goal_ids", sa.Text(), nullable=True),
        sa.Column("aspect_ids", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(['mc_run_id'], ['mc_runs.id'], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(['attribution_run_id'], ['attribution_runs.id'], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_recommendation_dossiers_conviction", "recommendation_dossiers", ['conviction'])
    safe_create_index("ix_recommendation_dossiers_ts", "recommendation_dossiers", ['ts'])
    safe_create_index("ix_recommendation_dossiers_user_id", "recommendation_dossiers", ['user_id'])

    # --- shadow_positions ---
    op.create_table(
        "shadow_positions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False, autoincrement=True),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(20, 6), nullable=False),
        sa.Column("mtm", sa.Numeric(20, 6), nullable=False),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_shadow_positions_portfolio_id", "shadow_positions", ['portfolio_id'])
    safe_create_index("ix_shadow_positions_ts", "shadow_positions", ['ts'])
    safe_create_index("ix_shadow_positions_symbol", "shadow_positions", ['symbol'])

    # --- q_values ---
    op.create_table(
        "q_values",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("q_theta", sa.Float(), nullable=False),
        sa.Column("q_phi", sa.Float(), nullable=False),
        sa.Column("visited_count", sa.Integer(), nullable=False),
        sa.Column("reward", sa.Float(), nullable=True),
        sa.Column("next_state_hash", sa.String(64), nullable=True),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("regime_label", sa.String(24), nullable=True),
        sa.Column("crisis", sa.Boolean(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=True),
        sa.Column("next_state_json", sa.Text(), nullable=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=True),
        sa.Column("validation_score", sa.Float(), nullable=True),
        sa.Column("loss", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_q_values_state_hash", "q_values", ['state_hash'])
    safe_create_index("ix_q_values_ts", "q_values", ['ts'])
    safe_create_index("ix_q_values_action", "q_values", ['action'])
    safe_create_index("ix_q_values_regime_label", "q_values", ['regime_label'])
    safe_create_index("ix_q_values_next_state_hash", "q_values", ['next_state_hash'])

    # --- meta_policy_snapshots ---
    op.create_table(
        "meta_policy_snapshots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime_label", sa.String(24), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("support_set_meta_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_meta_policy_snapshots_ts", "meta_policy_snapshots", ['ts'])
    safe_create_index("ix_meta_policy_snapshots_regime_label", "meta_policy_snapshots", ['regime_label'])

    # --- nudges ---
    op.create_table(
        "nudges",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(24), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_nudges_ts", "nudges", ['ts'])
    safe_create_index("ix_nudges_source", "nudges", ['source'])
    safe_create_index("ix_nudges_user_id", "nudges", ['user_id'])

    # --- embeddings ---
    op.create_table(
        "embeddings",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("item_type", sa.String(64), nullable=False),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_embeddings_item_id", "embeddings", ['item_id'])
    safe_create_index("ix_embeddings_created_at", "embeddings", ['created_at'])
    safe_create_index("ix_embeddings_item_type", "embeddings", ['item_type'])

    # --- finagent_runs ---
    op.create_table(
        "finagent_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_finagent_runs_agent", "finagent_runs", ['agent'])
    safe_create_index("ix_finagent_runs_created_at", "finagent_runs", ['created_at'])

    # --- portfolio_snapshots ---
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("security_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("total_return_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('portfolio_id', 'date', 'source', name="uq_portfolio_snapshots_portfolio_date_source"),
        sa.UniqueConstraint('user_id', 'date', 'source', name="uq_portfolio_snapshots_user_date_source"),
    )
    safe_create_index("ix_portfolio_snapshots_user_id", "portfolio_snapshots", ['user_id'])
    safe_create_index("ix_portfolio_snapshots_date", "portfolio_snapshots", ['date'])
    safe_create_index("ix_portfolio_snapshots_portfolio_id", "portfolio_snapshots", ['portfolio_id'])
    safe_create_index("ix_portfolio_snapshots_portfolio_date", "portfolio_snapshots", ['portfolio_id', 'date'])

    # --- discovery_config ---
    op.create_table(
        "discovery_config",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("config_type", sa.String(32), nullable=False),
        sa.Column("version_label", sa.String(64), nullable=False),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("parent_config_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("champion_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['parent_config_id'], ['discovery_config.id'], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_discovery_config_type_status", "discovery_config", ['config_type', 'status'])
    safe_create_index("ix_discovery_config_config_type", "discovery_config", ['config_type'])

    # --- alphacrafter_job_runs ---
    op.create_table(
        "alphacrafter_job_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("progress_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("shared_memory", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_alphacrafter_job_runs_status", "alphacrafter_job_runs", ['status'])
    safe_create_index("ix_alphacrafter_job_runs_user_id", "alphacrafter_job_runs", ['user_id'])

    # --- composite_membership ---
    op.create_table(
        "composite_membership",
        sa.Column("composite_id", sa.String(36), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(['composite_id'], ['composites.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('composite_id', 'portfolio_id', 'valid_from'),
    )

    # --- alpha_signals ---
    op.create_table(
        "alpha_signals",
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("factor_id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Numeric(12, 6), nullable=False),
        sa.Column("ic_window_value", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['factor_id'], ['factors_library.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('symbol', 'factor_id', 'ts'),
    )
    safe_create_index("ix_alpha_signals_factor_id", "alpha_signals", ['factor_id'])
    safe_create_index("ix_alpha_signals_symbol", "alpha_signals", ['symbol'])
    safe_create_index("ix_alpha_signals_ts", "alpha_signals", ['ts'])

    # --- tax_residency_periods ---
    op.create_table(
        "tax_residency_periods",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint('id'),
    )
    safe_create_index("ix_tax_residency_periods_user_id_country", "tax_residency_periods", ['user_id', 'country'])
    safe_create_index("ix_tax_residency_periods_user_id_valid_from", "tax_residency_periods", ['user_id'])


def downgrade() -> None:
    # Reverse dependency order.
    for table in [
        "tax_residency_periods",
        "alpha_signals",
        "composite_membership",
        "alphacrafter_job_runs",
        "discovery_config",
        "portfolio_snapshots",
        "finagent_runs",
        "embeddings",
        "nudges",
        "meta_policy_snapshots",
        "q_values",
        "shadow_positions",
        "recommendation_dossiers",
        "trader_backtests",
        "screener_runs",
        "factors_library",
        "performance_ledger_entries",
        "attribution_runs",
        "composites",
        "mc_path_samples",
        "mc_runs",
        "subscriptions",
        "price_cache",
        "passkey_credentials",
        "news_items",
        "invoices",
        "fundamentals",
        "executive_summaries",
        "dkb_transactions",
        "dkb_positions",
        "dkb_accounts",
        "chat_messages",
        "benchmarks",
        "backtest_results",
        "api_keys",
        "app_settings",
        "quant_runs",
        "recommendations",
        "watchlist",
        "expenses",
        "transactions_log",
        "holdings",
        "portfolios",
        "categories",
        "users",
    ]:
        op.drop_table(table)
