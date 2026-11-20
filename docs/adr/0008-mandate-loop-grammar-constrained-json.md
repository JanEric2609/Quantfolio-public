# 0008 — Mandate Loop: Grammar-Constrained JSON + Distinct Failure Status

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-08-31_implementation_decisions.md`
(Topic 3 of the consolidated implementation plan — "32% of mandate reviews
land on fallback_hold from unparseable JSON")
**Consumed by:** `backend/app/services/llm_portfolio/review.py`,
`backend/app/services/llm_portfolio/context.py`,
`backend/tests/llm_portfolio/test_review_fallback.py`,
`frontend/src/pages/mandates/JournalTab.tsx`

## Context

`run_mandate_review` (`review.py`) is the weekly mandate loop's bounded
tool-call/decision cycle. Unlike the advisor loop's decision step
(`advisor/llm_decision.py`), it called `_local_llm_sync` with no
`json_schema` — pure prompt-based JSON, parsed by a local `_try_parse_json`
that only stripped ``` fences and trailing commas before `json.loads`. A
JSON-parse failure just appended a "please reply with valid JSON" nudge and
consumed one of the loop's 8 turns; a *persistent* failure (a model stuck
producing prose, or repeatedly malformed output) silently ate the entire
turn budget before landing on `status="fallback_hold"` — indistinguishable
from a model that cooperated fully but simply ran out of turns exploring
tools. The audit's 32% figure was never traced to a stored sample, because
nothing on this path ever persisted the raw failing completion — a
`fallback_hold` row's `error` column is `NULL` today.

`advisor/llm_decision.py` had already solved the same failure class for its
own decision step: `build_decision_schema()` compiles a JSON Schema that
llama.cpp's `response_format` extension turns into a GBNF grammar,
constraining token sampling so malformed JSON or an out-of-enum value is not
representable. `app/services/llm/structured.py` extracted the
caller-agnostic half of that hardening (parsing with strict/embedded/salvage
fallback, retry-message shaping, the Qwen sampling preset, truncation
diagnostics) once `advisor/reflection.py` and `discover/dossier_writer.py`
needed the same defense — the mandate loop was the one caller left without
it.

## Decision

**1. Grammar-constrain both response shapes the mandate prompt protocol
describes.** `_MANDATE_RESPONSE_SCHEMA` (`review.py`) is a `oneOf` of the
tool-call shape (`{"tool": ..., "args": {...}}`, tool name pinned to an enum
of the four real tools) and the decision shape (`{"decision": {...}}`,
mirroring `context.py`'s `_PROMPT_PROTOCOL` field-for-field: `thesis`
(`minLength: 8`, `maxLength: 320` — same bound and same rationale as
advisor's `MAX_THESIS_CHARS`), `action` pinned to `buy|sell|hold`, `ticker`,
`quantity`, and the optional `alternatives_considered`/`key_risks`/
`expectation` fields, each bounded (`maxItems: 5`, `maxLength: 200`) for the
same reason advisor bounds its own unbounded string field: a grammar
constrains structure, not termination, so an unbounded array/string is where
a still-schema-legal completion can run to the token budget and leave the
object unclosed. Both `_local_llm_sync` calls in `run_mandate_review` (the
main tool loop and the shape-fix retry) now pass this schema plus
`STRUCTURED_JSON_SAMPLING` (the same Qwen near-greedy + `presence_penalty:
1.5` preset advisor uses).

The constant is duplicated rather than imported from `advisor/llm_decision.py`
— advisor and llm_portfolio are both decision-loop packages, and
import-linter's facade-only contracts forbid member-to-member imports
between them.

**2. Parse with the shared hardened extractor, not the local regex-based
one.** `_try_parse_json` (fence-strip + trailing-comma regex + bare
`json.loads`) is replaced by `parse_structured_completion` from
`app.services.llm.structured`, which additionally recovers JSON embedded
after a prose preamble ("Here is my decision: {...}") — a shape the old
parser could not recover at all — and reports *how* it parsed
(`strict`/`embedded`/`salvaged`/`unparseable`) rather than a bare
`None`/`dict`.

**3. One correction retry, then fail fast and distinctly — don't burn the
tool-call budget.** `_JSON_PARSE_MAX_CONSECUTIVE_RETRIES = 1`: the first
unparseable completion gets one retry via `build_retry_messages` (echoes the
rejected completion back, asks for corrected JSON matching the schema). A
*second* consecutive failure now breaks the loop immediately with a new
status, `review_failed`, distinct from `fallback_hold` — the latter now
means exclusively "the model cooperated with valid JSON throughout but never
committed to a decision within the turn budget." Previously these two
failure modes were the same status, which meant a genuinely broken JSON
round could not be told apart from ordinary tool exploration that simply ran
long.

**4. Persist the failing raw completion.** A `review_failed` row's `error`
column carries `unusable_reason()`'s diagnosis (truncation, empty
completion, schema-rejected, or generic) plus up to 2000 chars of the raw
completion itself. This is new: `fallback_hold`/`llm_unavailable` rows never
stored a failing completion before, which is why the audit's 32% claim could
not be traced back to an actual sample of failing prompts when this ADR was
written (see "Validation" below) — the fix closes that gap going forward
rather than attempting to reconstruct history that was never recorded.

**5. Frontend: one new badge.** `JournalTab.tsx`'s `STATUS_VARIANT` map gains
`review_failed: "danger"` — everything else about the journal rendering was
already generic (an unmapped status string falls through to a neutral badge
and prints as-is), so no other UI change was needed.

## Alternatives considered

**Increase `_MAX_TOOL_CALLS` or the JSON-nudge retry count instead of adding
a schema.** Rejected: more retries against an unconstrained prompt still
lets the model produce malformed JSON indefinitely; the grammar makes
malformed JSON structurally unrepresentable in the first place, which is a
stronger and cheaper fix than hoping more nudges help.

**Merge `review_failed` into `fallback_hold`.** Rejected per the plan's
explicit ask for a distinct status, and because the two causes call for
different operator responses: `fallback_hold` says "the model needs more
turns or a tighter prompt to decide faster"; `review_failed` says "the model
(or the endpoint) is producing broken JSON — check the raw completion now
stored in `error`."

**Backfill a 90-day sample of historical failing prompts to validate
against, as the plan specified.** Not possible: no raw completion was ever
persisted for a `fallback_hold` row (see Decision §4), and this repo's local
dev/test databases have no mandate-review history at all
(`llm_portfolio_decisions` is empty in every local SQLite file checked).
Querying the production database directly was considered and deliberately
not done in this session — it is a remote, shared-state action outside the
scope of a local code change and needs its own explicit go-ahead. Validation
here instead used a synthetic sample of the failure shapes the codebase
itself documents seeing (markdown-fenced JSON, prose-preamble JSON,
persistent non-JSON, one-retry recovery) — see Consequences.

## Consequences

- `run_mandate_review` now grammar-constrains generation the same way the
  advisor loop's decision step does; the mandate prompt protocol
  (`context.py:_PROMPT_PROTOCOL`) and the schema must be kept in sync by hand
  since nothing generates one from the other.
- A new `review_failed` status value exists alongside `completed` /
  `llm_unavailable` / `fallback_hold` / `invalid_action` /  `pending`. It is
  excluded from the journal block fed back to the LLM
  (`context.py:_build_journal_block`), same as the other fallback statuses.
- `fallback_hold`'s meaning narrows: it no longer captures "the model never
  produced valid JSON" cases, only genuine turn-budget exhaustion by an
  otherwise-cooperating model.
- Regression coverage added to
  `tests/llm_portfolio/test_review_fallback.py`: schema is actually passed to
  `_local_llm_sync`; markdown-fenced and prose-embedded JSON now parse where
  they previously would have nudged or failed; a single bad completion
  followed by a good one still completes normally (the retry works); two
  consecutive unparseable completions now produce `review_failed` with the
  raw completion in `error`; the pre-existing `fallback_hold` test was
  rewritten to exercise genuine turn exhaustion (valid tool-call JSON,
  8 turns, no decision) since persistent garbage now fails fast instead.
- Going forward, any future `review_failed` occurrence in production leaves
  an actual sample (raw completion + diagnosis) to inspect — the gap that
  made this session's own validation synthetic instead of empirical does not
  recur.
