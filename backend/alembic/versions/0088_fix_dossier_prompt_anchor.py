"""Repair the seeded discovery_config prompt_template — the active row
predates the expected_return-anchor instruction, so the LLM had no guidance
to compute a per-candidate value and instead echoed the schema example's
literal "12.5" on every dossier. Code-level fix (config.py's
DEFAULT_PROMPT_TEMPLATE) only affects fresh seeds; an already-active DB row
needs this one-time data repair. Promotes the stale active row to champion
and activates a fresh row carrying the corrected prompt, mirroring
config.py:activate_config's champion/retire semantics.

Revision ID: 0088_fix_dossier_prompt_anchor
Revises: 0087_paper_trade_fee
Create Date: 2026-08-11
"""
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0088_fix_dossier_prompt_anchor"
down_revision = "0087_paper_trade_fee"
branch_labels = None
depends_on = None

_ANCHOR_MARKER = "expected_return_anchor_pct"

_NEW_SYSTEM_PROMPT = (
    "You are a quantitative equity research analyst. Your task is to produce "
    "a research hypothesis as structured JSON — no tools, no chain-of-thought, no markdown.\n"
    "The input includes signal_breakdown.expected_return_anchor_pct, a quant-derived "
    "trailing-return estimate (percent). Anchor your expected_return on it and adjust "
    "up or down based on the thesis; only diverge sharply from it if you state why in "
    "the thesis. expected_return is required — if you have no other basis, echo the "
    "anchor value (or 0 if the anchor itself is null).\n"
    "Output exactly one JSON object matching this schema. The numbers below are "
    "illustrative placeholders only — compute your own value for each candidate, "
    "never copy them verbatim:\n"
    '{"direction": "long"|"short"|"neutral", '
    '"conviction": <float 0-1>, '
    '"horizon_months": <int 1-12>, '
    '"expected_return": <float, anchored on signal_breakdown.expected_return_anchor_pct>, '
    '"expected_return_range": [<float>, <float>]|null, '
    '"thesis": "1-3 sentence narrative", '
    '"key_risks": ["risk1", "risk2"]}\n'
    "Do not include any text outside the JSON object."
)

_NEW_CONFIG_JSON = {
    "system_prompt": _NEW_SYSTEM_PROMPT,
    "temperature": 0.2,
    "max_tokens": 4096,
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_config"):
        return

    discovery_config = sa.table(
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
    )

    active_row = bind.execute(
        sa.select(discovery_config.c.id, discovery_config.c.config_json).where(
            discovery_config.c.config_type == "prompt_template",
            discovery_config.c.status == "active",
        )
    ).first()

    if active_row is None:
        return

    raw_config = active_row.config_json
    stored_config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    system_prompt = (stored_config or {}).get("system_prompt") or ""
    if _ANCHOR_MARKER in system_prompt:
        return  # already carries the anchor instruction — nothing to repair

    now_val = datetime.now(UTC)

    bind.execute(
        discovery_config.update()
        .where(discovery_config.c.id == active_row.id)
        .values(status="champion", champion_at=now_val)
    )

    version_label = hashlib.sha256(
        json.dumps(_NEW_CONFIG_JSON, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]

    bind.execute(
        discovery_config.insert().values(
            id=str(uuid4()),
            config_type="prompt_template",
            version_label=version_label,
            description="System-default prompt fix: anchor expected_return on quant estimate (was echoing schema example literal)",
            config_json=_NEW_CONFIG_JSON,
            status="active",
            source="system",
            parent_config_id=active_row.id,
            created_at=now_val,
            champion_at=None,
        )
    )


def downgrade() -> None:
    # Data repair only — not reversible (and not desirable to revert to the
    # bugged prompt).
    pass
