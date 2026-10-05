"""Add metrics_snapshots table — unified risk/return snapshot schema.

Unified Portfolio Engine Phase 4 (docs/archive/plans/unified-portfolio-engine-
implementation.md). Additive only — no column drops. ``PaperSnapshot.sharpe``/
``.max_drawdown`` and ``AdvisorScorecard``'s risk columns are left in place,
unused going forward, per the plan's own "leaving them unused is lower-risk"
call — dropping columns other code paths might still transiently read is a
bigger blast radius than this refactor needs to take on.

Revision ID: 0090_metrics_snapshots
Revises: 0089_dampen_return_anchor
"""

from alembic import op
import sqlalchemy as sa

revision = "0090_metrics_snapshots"
down_revision = "0089_dampen_return_anchor"
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
    if not inspector.has_table("metrics_snapshots"):
        op.create_table(
            "metrics_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "portfolio_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("as_of", sa.Date(), nullable=False, index=True),
            sa.Column("context", sa.String(32), nullable=False),
            sa.Column("sharpe", sa.Float(), nullable=True),
            sa.Column("sortino", sa.Float(), nullable=True),
            sa.Column("calmar", sa.Float(), nullable=True),
            sa.Column("cvar_95", sa.Float(), nullable=True),
            sa.Column("max_drawdown", sa.Float(), nullable=True),
            sa.Column("volatility", sa.Float(), nullable=True),
            sa.Column("expected_return", sa.Float(), nullable=True),
            sa.Column("expected_return_method", sa.String(48), nullable=True),
            sa.Column("expected_return_horizon", sa.String(16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "portfolio_id", "as_of", "context", name="uq_metrics_snapshot_portfolio_asof_context"
            ),
        )
        safe_create_index(
            "ix_metrics_snapshots_portfolio_context",
            "metrics_snapshots",
            ["portfolio_id", "context"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("metrics_snapshots"):
        op.drop_table("metrics_snapshots")
