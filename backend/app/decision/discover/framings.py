"""Style framings for Discovery prompt perturbation (P3c).

Single source of truth for the per-style prompt framing lines appended to a
base system prompt when generating challenger configs. Lives in its own
module so that ``advisor.strategy`` can reuse the framings through this
sanctioned public surface instead of reaching into ``config_perturb``'s
privates (see ADR-0003 decision 8 and the Wave refactor roadmap).
"""
from __future__ import annotations

STYLE_FRAMINGS: dict[str, str] = {
    "conservative": "Be conservative in your estimates. Prefer neutral over conviction.",
    "aggressive": "Be bold when signals align. Prefer conviction over neutrality.",
    "fundamentals": "Focus on fundamental analysis: valuation, financial health, and competitive advantage.",
    "momentum": "Focus on momentum and technical factors: trend strength, relative strength, and volume patterns.",
}
