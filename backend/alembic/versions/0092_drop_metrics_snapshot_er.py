"""Drop MetricsSnapshot.expected_return/_method/_horizon — dead RESERVED columns.

Unified Portfolio Engine Phase 3/4 follow-up (docs/archive/plans/unified-portfolio-
engine-implementation.md). These three columns were added in 0090 "reserved
for a future per-holding context" that was never scoped: nothing in app/
writes them, and nothing reads them either. Structurally they don't fit —
``portfolio_id`` is a mandatory FK to ``paper_portfolios``, but the one place
with a real single-instrument anchor to attach (Discover dossiers) is scoped
to a user and a symbol, not a portfolio. Per this project's own convention
of not carrying schema for unscoped future requirements, removed rather than
left NULL indefinitely. Discover's own anchor storage (dossier_json /
DiscoverCandidate) is unaffected.

Revision ID: 0092_drop_metrics_snapshot_er
Revises: 0091_config_default_hash
"""

from alembic import op
import sqlalchemy as sa

revision = "0092_drop_metrics_snapshot_er"
down_revision = "0091_config_default_hash"
branch_labels = None
depends_on = None

_COLUMNS = ("expected_return", "expected_return_method", "expected_return_horizon")
_TYPES = {
    "expected_return": sa.Float(),
    "expected_return_method": sa.String(48),
    "expected_return_horizon": sa.String(16),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("metrics_snapshots"):
        return
    columns = {col["name"] for col in inspector.get_columns("metrics_snapshots")}
    for name in _COLUMNS:
        if name in columns:
            op.drop_column("metrics_snapshots", name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("metrics_snapshots"):
        return
    columns = {col["name"] for col in inspector.get_columns("metrics_snapshots")}
    for name in _COLUMNS:
        if name not in columns:
            op.add_column("metrics_snapshots", sa.Column(name, _TYPES[name], nullable=True))
