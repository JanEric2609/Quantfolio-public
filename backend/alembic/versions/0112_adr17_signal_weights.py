"""Carry ADR 0017's rebalanced signal weights to the seeded prod config row.

``config.DEFAULT_SIGNAL_WEIGHTS`` only seeds fresh databases; production keeps
reading the ``signal_weights`` row seeded on 2026-09-07. The ADR moves 0.10
of weight from the two trailing-return signals (momentum 0.20 -> 0.15,
benchmark 0.10 -> 0.05) to fundamentals (0.05 -> 0.15). As 0089 did for the
prompt, this retires the active row to ``champion`` and inserts the new
default as the active row, but only when the active row still holds the
exact previous code default. A config someone customised is left alone
(it will show the stale badge, and "sync to default" remains available).

The new row starts a fresh config_id cohort for the track-record gate. That
is intended: the old cohort's outcomes were produced by different scoring.

Revision ID: 0112_adr17_signal_weights
Revises: 0111_evidence_gate_runs
"""
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0112_adr17_signal_weights"
down_revision = "0111_evidence_gate_runs"
branch_labels = None
depends_on = None

_OLD_CONFIG_JSON = {
    "weights": {
        "ic_icir": 0.10, "regime": 0.05, "analyst": 0.05, "sentiment": 0.05,
        "portfolio": 0.15, "fundamentals": 0.05, "momentum": 0.20, "risk": 0.10,
        "benchmark": 0.10, "ml_signal": 0.05, "estimate_revision": 0.05,
        "insider_signal": 0.05,
    },
    "threshold_ic_obs": 20,
}

_NEW_CONFIG_JSON = {
    "weights": {
        **_OLD_CONFIG_JSON["weights"],
        "fundamentals": 0.15,
        "momentum": 0.15,
        "benchmark": 0.05,
    },
    "threshold_ic_obs": 20,
}

_DESCRIPTION = (
    "ADR 0017: trailing-return signals cut to 0.20 combined (momentum 0.15, "
    "benchmark 0.05); fundamentals 0.15"
)


def _hash(config_json: dict) -> str:
    # Same digest as app.decision.discover.config._auto_version.
    return hashlib.sha256(json.dumps(config_json, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def _table() -> sa.TableClause:
    return sa.table(
        "discovery_config",
        sa.column("id", sa.String),
        sa.column("config_type", sa.String),
        sa.column("version_label", sa.String),
        sa.column("description", sa.String),
        sa.column("config_json", sa.JSON),
        sa.column("status", sa.String),
        sa.column("source", sa.String),
        sa.column("parent_config_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("champion_at", sa.DateTime(timezone=True)),
        sa.column("code_default_hash", sa.String),
    )


def _as_dict(raw: object) -> dict:
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return parsed if isinstance(parsed, dict) else {}


def _same_weights(stored: dict, expected: dict) -> bool:
    weights = stored.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(expected["weights"]):
        return False
    return all(abs(float(weights[k]) - v) < 1e-9 for k, v in expected["weights"].items())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_config"):
        return
    columns = {c["name"] for c in inspector.get_columns("discovery_config")}
    if "code_default_hash" not in columns:
        return

    table = _table()
    active = bind.execute(
        sa.select(table.c.id, table.c.config_json, table.c.source).where(
            table.c.config_type == "signal_weights",
            table.c.status == "active",
        )
    ).first()
    if active is None or active.source != "system":
        return
    if not _same_weights(_as_dict(active.config_json), _OLD_CONFIG_JSON):
        return  # customised, or already migrated

    now = datetime.now(UTC)
    bind.execute(
        table.update().where(table.c.id == active.id).values(status="champion", champion_at=now)
    )
    new_hash = _hash(_NEW_CONFIG_JSON)
    bind.execute(
        table.insert().values(
            id=str(uuid4()),
            config_type="signal_weights",
            version_label=new_hash,
            description=_DESCRIPTION,
            config_json=_NEW_CONFIG_JSON,
            status="active",
            source="system",
            parent_config_id=active.id,
            created_at=now,
            champion_at=None,
            code_default_hash=new_hash,
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_config"):
        return
    table = _table()
    row = bind.execute(
        sa.select(table.c.id, table.c.parent_config_id).where(
            table.c.config_type == "signal_weights",
            table.c.status == "active",
            table.c.description == _DESCRIPTION,
        )
    ).first()
    if row is None:
        return
    bind.execute(table.delete().where(table.c.id == row.id))
    if row.parent_config_id:
        bind.execute(
            table.update()
            .where(table.c.id == row.parent_config_id)
            .values(status="active", champion_at=None)
        )
