"""Shared hardening for structured-JSON LLM calls.

Extracted from the pattern hardened in ``advisor/llm_decision.py`` (PRs
#195-197, prod incidents there documented) once ``advisor/reflection.py`` and
``discover/dossier_writer.py`` turned out to have the same failure class —
runaway completion -> "not valid JSON" -- without any of these defenses. A
grammar-constrained ``{"type": "string"}`` bounds *structure*, not
*termination*: inside an unbounded string field every repeated token is still
legal JSON, so a completion can run to the token limit and leave the object
unclosed. The pieces here are the generic, caller-agnostic half of the fix
(parsing, retry-message shape, sampling preset); schema ``maxLength`` bounds
and the system-prompt wording stay with each call site since those are
domain-specific.
"""
from __future__ import annotations

import json
from typing import Any

# Qwen's published non-thinking preset, in full. Near-greedy decoding is
# mode-seeking, which is what produced the narrow, repetitive completions this
# module exists to defend against.
#
# ``presence_penalty`` is part of that same published preset, and Qwen
# recommends 1.5 (up to 2.0) specifically for *quantized* checkpoints to
# suppress repetition — every caller of this preset runs against a quantized
# local model. It is restated here (rather than left to a server default)
# because the local endpoint zeroes its own ``--presence-penalty`` for every
# caller that doesn't pass one explicitly.
STRUCTURED_JSON_SAMPLING: dict[str, Any] = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "presence_penalty": 1.5,
}

# How much of a rejected completion is quoted back on a retry. Echoing the
# *whole* answer is how one runaway completion pushes the next attempt's
# prompt past the context window and guarantees the retry fails too (the
# local endpoint runs with ``--no-context-shift``).
DEFAULT_RETRY_EXCERPT_CHARS = 800


def _reject_constant(name: str) -> Any:
    """Refuse ``NaN``/``Infinity``, which Python's json accepts but JSON doesn't.

    A model that emits ``"conviction": NaN`` would otherwise have it parsed,
    stored in ``dossier_json``, re-serialised as a bare ``NaN`` token, and then
    crash the browser's ``JSON.parse`` on the way to the UI. Treating it as a
    parse failure routes it to the retry path instead.
    """
    raise ValueError(f"non-JSON constant in completion: {name}")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


def _loads(text: str) -> Any:
    """``json.loads`` that rejects NaN/Infinity, raising ``JSONDecodeError``-alike."""
    try:
        return _DECODER.decode(text)
    except ValueError as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise
        raise json.JSONDecodeError(str(exc), text, 0) from exc


def strip_code_fence(raw: str) -> str:
    """Return the contents of a ``` fenced block, wherever it appears.

    Models very often lead with a sentence ("Here's the analysis:") before the
    fence. Only stripping a fence anchored at position 0 — as this did before —
    left that extremely common shape unparseable, and the embedded-object
    fallback could not rescue it either because the trailing ``` is still
    attached. Falls back to the stripped input when there is no fence.
    """
    raw = raw.strip()
    open_at = raw.find("```")
    if open_at < 0:
        return raw
    # Skip the fence marker and its optional language tag ("```json\n").
    body_start = raw.find("\n", open_at)
    if body_start < 0:
        return raw
    close_at = raw.find("```", body_start)
    body = raw[body_start:close_at] if close_at >= 0 else raw[body_start:]
    return body.strip()


def salvage_truncated_array(raw: str, array_key: str) -> dict[str, Any] | None:
    """Recover the complete elements of a top-level array field cut off mid-object.

    A grammar-constrained answer that hits the token limit is a well-formed
    prefix of valid JSON — typically ``{"<array_key>": [ {…}, {…}, {"foo":``.
    Everything before the incomplete element is a real answer the model
    committed to, and discarding it entirely means a response the model *did*
    produce is thrown away just because its tail didn't fit the budget. This
    scans the array, keeps elements that closed, and drops the partial tail.
    Nothing is invented: a fragment with no complete element yields ``None``.

    Every occurrence of the key is scanned and the richest salvage wins, rather
    than the first. Completions routinely echo the schema example back before
    answering, and taking the first match meant salvaging the *example* — the
    advisor would then act on a placeholder ticker, reported as a success.
    """
    marker = f'"{array_key}"'
    best: dict[str, Any] | None = None
    search_from = 0
    while True:
        found = raw.find(marker, search_from)
        if found < 0:
            break
        search_from = found + len(marker)
        candidate = _salvage_one_array(raw, found, array_key)
        if candidate is not None and (
            best is None or len(candidate[array_key]) > len(best[array_key])
        ):
            best = candidate
    return best


def _salvage_one_array(raw: str, marker_at: int, array_key: str) -> dict[str, Any] | None:
    """Salvage the complete elements of the array opening after *marker_at*."""
    start = raw.find("[", marker_at)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    complete: list[str] = []
    element_start: int | None = None
    for i in range(start + 1, len(raw)):
        char = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                element_start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and element_start is not None:
                complete.append(raw[element_start : i + 1])
                element_start = None
        elif char == "]" and depth == 0:
            break

    if not complete:
        return None
    try:
        parsed = _loads("{" + f'"{array_key}": [' + ",".join(complete) + "]}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_structured_completion(raw: str, array_key: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a structured-JSON completion, reporting *how* it had to be read.

    Returns ``(parsed, how)`` with ``how`` in ``strict`` / ``embedded`` /
    ``salvaged`` / ``unparseable``. Callers need the distinction: a salvaged
    answer is usable but means the completion budget is too small, and
    reporting it as a clean parse hides the actual defect.
    """
    body = strip_code_fence(raw)
    try:
        parsed = _loads(body)
    except json.JSONDecodeError:
        parsed = None
    else:
        return (parsed, "strict") if isinstance(parsed, dict) else (None, "unparseable")

    # Prose wrapped around the object, or a preamble the grammar never
    # constrained because the server failed open on the schema. `raw_decode`
    # stops at the end of the first complete value, so this also covers an
    # object followed by a closing remark — which a plain `json.loads` on the
    # slice rejects. The `> 0` gate this replaces meant an object starting at
    # position 0 with *any* trailing text was never retried at all.
    brace = body.find("{")
    if brace >= 0:
        try:
            candidate, _end = _DECODER.raw_decode(body[brace:])
        except ValueError:
            # JSONDecodeError, or _reject_constant's bare ValueError on
            # NaN/Infinity — both mean "not usable JSON", both fall through.
            candidate = None
        if isinstance(candidate, dict):
            return candidate, "embedded"

    salvaged = salvage_truncated_array(body, array_key)
    return (salvaged, "salvaged") if salvaged is not None else (None, "unparseable")


def unusable_reason(raw: str, telemetry: dict[str, Any]) -> str:
    """Name the actual failure behind an unparseable completion.

    "not valid JSON" is technically true of every one of these but sends the
    reader to the prompt when the fix is usually the token budget — the
    telemetry dict (from ``_local_llm_sync``'s ``telemetry=`` kwarg) carries
    the endpoint's own account of what happened.
    """
    if not raw.strip():
        if telemetry.get("reasoning_leak") or telemetry.get("raw_content_chars"):
            return "LLM returned reasoning text with no answer content"
        return "LLM returned an empty completion"
    if telemetry.get("truncated"):
        return (
            f"LLM completion hit the {telemetry.get('max_tokens')}-token limit and was cut "
            "off mid-object (finish_reason=length) — no complete element survived"
        )
    if telemetry.get("schema_dropped"):
        return (
            "LLM response was not valid JSON and the server rejected the JSON schema "
            f"({telemetry.get('schema_reject_status')}), so generation was unconstrained"
        )
    return "LLM response was not valid JSON"


def build_retry_messages(
    base_messages: list[dict[str, str]],
    raw: str,
    instruction: str,
    *,
    excerpt_chars: int = DEFAULT_RETRY_EXCERPT_CHARS,
) -> list[dict[str, str]]:
    """Rebuild the message list from *base_messages* plus one correction turn.

    Only an excerpt of the rejected answer goes back. Rebuilding from the base
    each time (rather than appending to a growing transcript) is what keeps a
    single runaway completion from making every later retry fail too.
    """
    excerpt = raw.strip()[:excerpt_chars]
    if len(raw.strip()) > excerpt_chars:
        excerpt += "…[truncated]"
    return [
        *base_messages,
        {"role": "assistant", "content": excerpt},
        {"role": "user", "content": instruction},
    ]


def completion_budget(
    universe_size: int,
    *,
    tokens_per_item: int,
    min_tokens: int,
    max_tokens: int,
) -> int:
    """Completion tokens to allow for a response scaling with *universe_size*
    items, bounded to ``[min_tokens, max_tokens]``."""
    wanted = min_tokens + tokens_per_item * max(0, int(universe_size))
    return max(min_tokens, min(max_tokens, wanted))
