"""Phase 5: Verification loop - dual-Q learning, shadow portfolio, nudges, audit

Revision ID: 0019_phase5_verification
Revises: 0018_phase4_alphacrafter
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_phase5_verification"
down_revision = "0018_phase4_alphacrafter"
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

    # Extend audit_logs with correlation_id and additional indexes
    if inspector.has_table("audit_logs"):
        columns = {c["name"] for c in inspector.get_columns("audit_logs")}
        if "correlation_id" not in columns:
            op.add_column(
                "audit_logs",
                sa.Column("correlation_id", sa.String(36), nullable=True),
            )
        # Use IF NOT EXISTS to avoid aborting the transaction on duplicate index
        bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_correlation_id ON audit_logs (correlation_id)"))
        bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_event_type_ts ON audit_logs (event_type, created_at DESC)"))

    # Create shadow_positions hypertable (7-day chunks)
    if not inspector.has_table("shadow_positions"):
        op.create_table(
            "shadow_positions",
            sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
            sa.Column("portfolio_id", sa.UUID(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("symbol", sa.String(length=12), nullable=False),
            sa.Column("qty", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("cost_basis", sa.Numeric(precision=20, scale=6), nullable=False),
            sa.Column("mtm", sa.Numeric(precision=20, scale=6), nullable=False),
            sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_shadow_positions_portfolio_ts", "shadow_positions",
                       ["portfolio_id", sa.desc("ts")])
        safe_create_index("ix_shadow_positions_symbol_ts", "shadow_positions",
                       ["symbol", sa.desc("ts")])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('shadow_positions', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))

    # Create q_values hypertable (7-day chunks)
    if not inspector.has_table("q_values"):
        op.create_table(
            "q_values",
            sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("action", sa.String(length=128), nullable=False),
            sa.Column("q_theta", sa.Float(), nullable=False),
            sa.Column("q_phi", sa.Float(), nullable=False),
            sa.Column("visited_count", sa.Integer(), nullable=False, server_default="1"),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_q_values_state_action_ts", "q_values",
                       ["state_hash", "action", sa.desc("ts")])
        safe_create_index("ix_q_values_ts", "q_values", [sa.desc("ts")])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('q_values', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))

    # Create meta_policy_snapshots table (non-hypertable, point-in-time snapshots)
    if not inspector.has_table("meta_policy_snapshots"):
        op.create_table(
            "meta_policy_snapshots",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("regime_label", sa.String(length=24), nullable=False),
            sa.Column("weights_json", sa.Text(), nullable=False),
            sa.Column("support_set_meta_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_meta_policy_snapshots_ts", "meta_policy_snapshots", [sa.desc("ts")])
        safe_create_index("ix_meta_policy_snapshots_regime_ts", "meta_policy_snapshots",
                       ["regime_label", sa.desc("ts")])

    # Create nudges table (point-in-time, not hypertable)
    if not inspector.has_table("nudges"):
        op.create_table(
            "nudges",
            sa.Column("id", sa.UUID(), nullable=False, server_default=sa.func.gen_random_uuid()),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("severity", sa.String(length=24), nullable=False, server_default="info"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        safe_create_index("ix_nudges_user_id_ack_ts", "nudges",
                       ["user_id", "ack_at", sa.desc("ts")])
        safe_create_index("ix_nudges_ts", "nudges", [sa.desc("ts")])
        safe_create_index("ix_nudges_source", "nudges", ["source"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop tables in reverse order of creation
    if inspector.has_table("nudges"):
        op.drop_table("nudges")

    if inspector.has_table("meta_policy_snapshots"):
        op.drop_table("meta_policy_snapshots")

    if inspector.has_table("q_values"):
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("SELECT drop_hypertable('q_values', if_exists => TRUE)"))
        op.drop_table("q_values")

    if inspector.has_table("shadow_positions"):
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("SELECT drop_hypertable('shadow_positions', if_exists => TRUE)"))
        op.drop_table("shadow_positions")

    # Remove correlation_id from audit_logs if it exists
    if inspector.has_table("audit_logs"):
        columns = {c["name"] for c in inspector.get_columns("audit_logs")}
        if "correlation_id" in columns:
            op.drop_column("audit_logs", "correlation_id")
