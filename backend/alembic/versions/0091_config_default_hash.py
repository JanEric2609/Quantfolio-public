"""Add discovery_config.code_default_hash — config source-of-truth staleness.

Unified Portfolio Engine Phase 5 (docs/archive/plans/unified-portfolio-engine-
implementation.md). Records the sha256[:12] hash of the code-level default
(DEFAULT_SIGNAL_WEIGHTS/DEFAULT_PROMPT_TEMPLATE/DEFAULT_UNIVERSE_CONFIG in
app.decision.discover.config) a row was seeded from, or last approved
against at activation — compared against the *current* code-default hash to
surface a "stale, review needed" badge instead of silently drifting.

Existing rows are backfilled best-effort, since their actual seed/approval
history predates this column: signal_weights/universe rows get the current
code-default hash (those constants are unchanged since seeding, so no
false-positive staleness badge appears); prompt_template rows get a hash of
their OWN stored content instead, since the prompt was rewritten in Phase 3
(see dossier_writer.py/config.py's DEFAULT_PROMPT_TEMPLATE) and a seeded row
predating that rewrite genuinely IS stale relative to the current code
default — this correctly surfaces the badge rather than masking it.

Revision ID: 0091_config_default_hash
Revises: 0090_metrics_snapshots
"""

import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0091_config_default_hash"
down_revision = "0090_metrics_snapshots"
branch_labels = None
depends_on = None


def _hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


# Frozen copies of the current code defaults — importing app.decision.discover.config
# here would tie this migration to future code changes; migrations must stay
# self-contained snapshots of intent at the time they were written.
_CODE_DEFAULTS = {
    "signal_weights": {
        "weights": {
            "ic_icir": 0.10, "regime": 0.05, "analyst": 0.05, "sentiment": 0.05,
            "portfolio": 0.15, "fundamentals": 0.10, "momentum": 0.20, "risk": 0.15, "benchmark": 0.15,
        },
        "threshold_ic_obs": 20,
    },
    "universe": {
        "universe_max_size": 200,
        "min_market_cap_eur": 250_000_000,
        "min_avg_volume": 100_000,
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_config"):
        return
    columns = {col["name"] for col in inspector.get_columns("discovery_config")}
    if "code_default_hash" not in columns:
        op.add_column("discovery_config", sa.Column("code_default_hash", sa.String(12), nullable=True))

    meta = sa.MetaData()
    table = sa.Table("discovery_config", meta, autoload_with=bind)
    # Only stamp rows that were genuinely reviewed against a code default:
    # system-seeded rows, and whatever is currently promoted. `is_config_stale`
    # treats a NULL hash as "never reviewed, therefore not stale", which is
    # exactly right for a hand-written challenger — stamping those too made a
    # custom prompt_template challenger render the Stale badge the moment this
    # migration ran (its own content hash can never equal the code default),
    # and conversely recorded a signal_weights challenger as approved against a
    # default nobody had reviewed it against.
    rows = bind.execute(
        sa.select(table.c.id, table.c.config_type, table.c.config_json).where(
            # Never overwrite an existing hash: a downgrade/upgrade cycle, or a
            # dev DB bootstrapped by create_all, would otherwise silently revert
            # every human re-approval recorded by activate_config().
            table.c.code_default_hash.is_(None),
            sa.or_(
                table.c.source == "system",
                table.c.status.in_(("active", "champion")),
            ),
        )
    ).fetchall()
    for row in rows:
        config_json = row.config_json if isinstance(row.config_json, dict) else json.loads(row.config_json)
        if row.config_type == "prompt_template":
            # The prompt template's exact wording has since been repaired by
            # migrations 0088/0089 and is not reproduced here; approximate
            # staleness detection for this type via the row's own content
            # hash instead of a frozen default snapshot.
            hashed = _hash(config_json)
        else:
            default = _CODE_DEFAULTS.get(row.config_type)
            hashed = _hash(default) if default is not None else _hash(config_json)
        bind.execute(
            table.update().where(table.c.id == row.id).values(code_default_hash=hashed)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_config"):
        return
    columns = {col["name"] for col in inspector.get_columns("discovery_config")}
    if "code_default_hash" in columns:
        op.drop_column("discovery_config", "code_default_hash")
