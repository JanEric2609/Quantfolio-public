"""LLM-based factor proposal generation for AlphaCrafter Miner (Phase 8).

Uses Financial Chain-of-Thought prompting with RAG over wiki concepts to propose
novel factor candidates. Routes through ``llm/router.py`` with the
``batch_research`` task type (cloud fallback when configured).

Proposed formulas are expressed in the sandboxed factor DSL
(:mod:`app.lab.alphacrafter.factor_dsl`) and validated before they ever
reach the Miner — anything that fails to parse, or uses a non-whitelisted
construct, is discarded. The Miner then computes real IC and drops weak signals.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.lab.alphacrafter import factor_dsl
from app.foundation.finagent.rag import RAGRetriever
from app.foundation.llm.router import call as llm_call

logger = logging.getLogger(__name__)

# Surfaced to the model so it proposes formulas the sandbox can actually run.
_DSL_VARIABLES = "open, high, low, close, volume, pe_ratio, market_cap, roe"
_DSL_FUNCTIONS = (
    "rank(x), zscore(x), rolling_mean(x, n), rolling_std(x, n), rolling_sum(x, n), "
    "shift(x, n), delta(x, n), log(x), abs(x), ts_min(x, n), ts_max(x, n), corr(x, y, n); "
    "operators + - * / and comparisons"
)


@dataclass
class LLMFactorProposal:
    """An LLM-proposed factor (already validated against the DSL sandbox)."""

    name: str
    description: str
    dsl: str
    intuition: str
    expected_regime: str | None = None
    related_concepts: list[str] = field(default_factory=list)


def proposals_to_factor_defs(proposals: list[LLMFactorProposal]) -> list[dict]:
    """Map proposals to the Miner ``extra_factors`` format."""
    return [
        {"name": p.name, "formula": p.dsl, "source": "llm", "dsl": p.dsl}
        for p in proposals
    ]


async def propose_factors_via_llm(
    db: Session,
    universe_stats: dict,
    regime_label: str | None = None,
    force_local: bool = False,
) -> tuple[list[LLMFactorProposal], str]:
    """Use the LLM to propose novel factors via Financial CoT, validated by the DSL.

    Args:
        db: Database session (required by the LLM router and RAG).
        universe_stats: Stats about the universe (n_stocks, n_days, ...).
        regime_label: Current market regime, included in the prompt for context.
        force_local: Force the local LLM backend.

    Returns:
        (valid proposals, reasoning trace). Only proposals whose ``dsl`` passes
        :func:`factor_dsl.validate` are returned.
    """
    # Retrieve relevant wiki concepts for context.
    rag = RAGRetriever(db)
    try:
        wiki_chunks = await rag.retrieve(
            query="quantitative factors signal design factor engineering",
            top_k=5,
            item_type_filter="obsidian_note",
        )
    except Exception as exc:  # RAG is best-effort context
        logger.debug("Proposer: RAG retrieval failed: %s", exc)
        wiki_chunks = []

    context_parts = []
    for chunk in wiki_chunks:
        if isinstance(chunk, dict):
            context_parts.append(f"[{chunk.get('item_id', 'unknown')}] {chunk.get('meta_json', '')}")
    context = "\n".join(context_parts)

    regime_context = f"Current regime: {regime_label}. " if regime_label else ""
    universe_context = (
        f"Universe size: {universe_stats.get('n_stocks', 100)} stocks, "
        f"{universe_stats.get('n_days', 252)} days."
    )

    prompt = f"""You are a quantitative finance researcher designing novel equity factors.

{regime_context}{universe_context}

## Market Context and Concepts
{context}

## Factor expression language (STRICT)
Each factor's formula MUST be written in this restricted expression language and use
ONLY these variables and functions:
- Variables: {_DSL_VARIABLES}
- Functions/operators: {_DSL_FUNCTIONS}
Cross-sectional ops (rank, zscore) act across symbols per day; rolling/shift/delta/ts_*
act over time per symbol. No attribute access, indexing, imports, or other functions.

## Task
Propose 3-5 novel factors that:
1. Have economic intuition grounded in financial theory
2. Are computable from the variables above
3. Are distinct from one another and from plain Fama-French / momentum

For each factor respond with: name, description, formula (in the language above),
intuition, expected_regime.
Respond ONLY as a JSON array of objects with keys:
name, description, formula, intuition, expected_regime.
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        completion = await llm_call(
            db,
            task_type="batch_research",
            messages=messages,
            force_local=force_local,
            timeout_s=120.0,
        )
        response_text = completion.content
    except Exception as exc:
        return [], f"LLM call failed: {exc}"

    reasoning = response_text
    raw_items = _parse_json_array(response_text)
    if raw_items is None:
        return [], f"Failed to parse JSON response: {response_text[:200]}"

    proposals: list[LLMFactorProposal] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        formula = str(item.get("formula", "")).strip()
        if not formula:
            continue
        try:
            factor_dsl.validate(formula)
        except factor_dsl.FactorDSLError as exc:
            logger.info("Proposer: discarding invalid formula %r: %s", formula, exc)
            continue
        proposals.append(
            LLMFactorProposal(
                name=str(item.get("name", "")).strip() or f"llm_factor_{len(proposals)}",
                description=str(item.get("description", "")),
                dsl=formula,
                intuition=str(item.get("intuition", "")),
                expected_regime=item.get("expected_regime"),
                related_concepts=item.get("related_concepts") or [],
            )
        )

    return proposals, reasoning


def _parse_json_array(text: str) -> list | None:
    """Extract and parse the first JSON array found in ``text``."""
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None
