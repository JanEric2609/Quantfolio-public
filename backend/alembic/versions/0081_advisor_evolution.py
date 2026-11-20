"""Advisor loop PR2 — strategies, lessons, graduation state, sleeve attribution.

Adds:
- advisor_strategies       — champion/challenger/retired strategy rows (C1).
- strategy_lessons         — bounded reflection memory (A1).
- advisor_graduation_state — the per-user graduation switch (D2).
- advisor_grad_transitions — audit trail of graduate/de-graduate flips (D2).
- discovery_prediction.portfolio_id — per-sleeve prediction attribution.

Additive only — no drops.

Revision ID: 0081_advisor_evolution
Revises: 0080_advisor_scorecard
"""

from alembic import op
import sqlalchemy as sa

revision = "0081_advisor_evolution"
down_revision = "0080_advisor_scorecard"
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

    if not inspector.has_table("advisor_strategies"):
        op.create_table(
            "advisor_strategies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("role", sa.String(16), nullable=False, server_default="champion", index=True),
            sa.Column(
                "portfolio_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("parent_strategy_id", sa.String(36), nullable=True),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not inspector.has_table("strategy_lessons"):
        op.create_table(
            "strategy_lessons",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "strategy_id",
                sa.String(36),
                sa.ForeignKey("advisor_strategies.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "portfolio_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("lesson_text", sa.Text(), nullable=False),
            sa.Column("tags_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column(
                "source_scorecard_id",
                sa.String(36),
                sa.ForeignKey("advisor_scorecards.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("rank", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not inspector.has_table("advisor_graduation_state"):
        op.create_table(
            "advisor_graduation_state",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "strategy_id",
                sa.String(36),
                sa.ForeignKey("advisor_strategies.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("graduated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("since", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reason", sa.Text(), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", name="uq_advisor_grad_state_user"),
        )

    if not inspector.has_table("advisor_grad_transitions"):
        op.create_table(
            "advisor_grad_transitions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("strategy_id", sa.String(36), nullable=True),
            sa.Column("from_graduated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("to_graduated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("criteria_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if inspector.has_table("discovery_prediction"):
        cols = {c["name"] for c in inspector.get_columns("discovery_prediction")}
        if "portfolio_id" not in cols:
            op.add_column(
                "discovery_prediction",
                sa.Column("portfolio_id", sa.String(36), nullable=True),
            )
            safe_create_index(
                "ix_discovery_prediction_portfolio_id",
                "discovery_prediction",
                ["portfolio_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("discovery_prediction"):
        cols = {c["name"] for c in inspector.get_columns("discovery_prediction")}
        if "portfolio_id" in cols:
            safe_drop_index("ix_discovery_prediction_portfolio_id", table_name="discovery_prediction")
            op.drop_column("discovery_prediction", "portfolio_id")
    for table in (
        "advisor_grad_transitions",
        "advisor_graduation_state",
        "strategy_lessons",
        "advisor_strategies",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
