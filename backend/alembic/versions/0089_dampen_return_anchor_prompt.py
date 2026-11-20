"""Repair the seeded discovery_config prompt_template a second time — the
0088 fix taught the LLM to anchor expected_return on
signal_breakdown.expected_return_anchor_pct, but that anchor is itself a
BACKWARD-looking trailing return (3y annualized CAGR, or 12-1m momentum as
fallback), not a forecast. The prompt told the model to "echo the anchor
value" when it had no other basis, so dossiers for stocks with a large
trailing rally (e.g. BBVA.MC, +60% trailing 3y CAGR) surfaced that figure
as a forward "Expected Return" on a 6-month horizon — technically correct
historical data, but misleadingly framed as a prediction (prod audit, Aug
2026). This migration repairs the already-active DB row the same way 0088
did; code-level fix (config.py's DEFAULT_PROMPT_TEMPLATE) only affects
fresh seeds.

Revision ID: 0089_dampen_return_anchor
Revises: 0088_fix_dossier_prompt_anchor
Create Date: 2026-08-11
"""
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0089_dampen_return_anchor"
down_revision = "0088_fix_dossier_prompt_anchor"
branch_labels = None
depends_on = None

_DAMPEN_MARKER = "BACKWARD-LOOKING"

_NEW_SYSTEM_PROMPT = (
    "You are a quantitative equity research analyst. Your task is to produce "
    "a research hypothesis as structured JSON — no tools, no chain-of-thought, no markdown.\n"
    "The input includes signal_breakdown.expected_return_anchor_pct, a BACKWARD-LOOKING "
    "trailing return (3-year annualized CAGR, or 12-1 month momentum as fallback) — it "
    "describes what already happened, not a forecast. Your expected_return must be a "
    "genuine FORWARD-looking estimate over horizon_months, not a copy of the anchor. "
    "Treat a large trailing anchor (e.g. >25% annualized) as a rally that is unlikely to "
    "repeat at the same pace and dampen it toward realistic near-term expectations "
    "(typically single-digit to low-teens percent over a few months) unless the thesis "
    "states a specific forward catalyst that justifies more. expected_return is required "
    "— if you have no other basis, use a conservative fraction of the anchor scaled to "
    "horizon_months rather than echoing it outright (0 if the anchor itself is null).\n"
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
    if _DAMPEN_MARKER in system_prompt:
        return  # already carries the dampening instruction — nothing to repair

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
            description=(
                "System-default prompt fix: expected_return anchor is explicitly "
                "backward-looking and must be dampened into a forward estimate, "
                "not echoed (was surfacing trailing 3y CAGR as a forward return)"
            ),
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
