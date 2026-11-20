"""Add DiscoverRun.updated_at — bumped on every stage_json write.

The reap/staleness logic (reap_stale_discover_runs) previously could only
judge a "running" row by its overall age since created_at, which conflates
a pipeline that is genuinely still progressing (universe build + per-symbol
scoring + LLM dossier calls can legitimately take many minutes) with one
that was orphaned by a process restart mid-run. This column lets the reaper
tell those apart: a run whose stage_json hasn't moved in N minutes is stuck,
regardless of how old the run itself is.

Revision ID: 0093_discover_run_updated_at
Revises: 0092_drop_metrics_snapshot_er
"""

from alembic import op
import sqlalchemy as sa

revision = "0093_discover_run_updated_at"
down_revision = "0092_drop_metrics_snapshot_er"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discover_runs"):
        return
    columns = {col["name"] for col in inspector.get_columns("discover_runs")}
    if "updated_at" not in columns:
        op.add_column(
            "discover_runs",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discover_runs"):
        return
    columns = {col["name"] for col in inspector.get_columns("discover_runs")}
    if "updated_at" in columns:
        op.drop_column("discover_runs", "updated_at")
