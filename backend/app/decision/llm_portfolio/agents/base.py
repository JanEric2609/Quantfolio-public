"""Base agent for the Multi-Agent Council.

All 5 council agents (Analyst, Risk, Macro, Manager, Debate) inherit
from BaseAgent. Provides shared LLM calling, JSON parsing, structured
output validation, and retry logic.

Design:
- LLM calls are async (httpx.AsyncClient)
- System prompt is agent-specific, built from AgentConfig
- Tool execution is synchronous (all arithmetic in deterministic Python)
- Output is always a validated Pydantic model
- Reasoning is disabled per Machine Spirits mitigation
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.foundation.llm.sampling import QWEN_SAMPLING
from app.foundation.settings import llm_auth_headers
from app.decision.llm_portfolio.agents.models import AgentConfig
from app.foundation.providers.rate_limiter import get_shared_async_limiter

logger = logging.getLogger(__name__)

# Characters-per-token estimate for prompt budgeting
_CHAR_PER_TOKEN = 4


def _json_default(obj: Any) -> Any:
    """Serialize non-JSON-native objects for json.dumps.

    Prefers Pydantic v2 ``model_dump()``, falls back to ``dict()``,
    and uses ``str()`` only as a last resort with a warning.
    """
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return obj.model_dump()
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    logger.warning(
        "Falling back to str() for JSON serialization of %s",
        type(obj).__name__,
    )
    return str(obj)


class AgentError(Exception):
    """Non-retryable agent error."""


class LLMCallError(AgentError):
    """LLM call failed (network, timeout, etc.)."""


class ParseError(AgentError):
    """LLM output could not be parsed as valid JSON/Pydantic model."""


class BaseAgent:
    """Shared foundation for all council agents.

    Subclasses override:
    - system_prompt: str (class attribute or property)
    - _build_user_message(...) → str
    - output_model: type[BaseModel] (Pydantic model class for response parsing)
    """

    system_prompt: str = ""
    output_model: type[BaseModel] = BaseModel
    # Corrective round-trips after an invalid reply. The local 9B model
    # returns an out-of-schema RiskReport about once a day (9 RiskAgent
    # failures on prod 2026-09-10..25; 3 of 3 valid on a replay), and
    # without a retry one bad reply discarded the whole agent report.
    max_parse_retries: int = 1

    def __init__(self, config: AgentConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, **context: Any) -> BaseModel:
        """Execute the agent: build prompt → call LLM → parse → validate.

        Args:
            **context: Agent-specific keyword arguments forwarded to
                _build_user_message().

        Returns:
            A validated Pydantic model of type output_model.

        Raises:
            LLMCallError: LLM endpoint unreachable or returned error.
            ParseError: LLM output invalid after all retries.
        """
        messages = self._build_messages(**context)
        raw = await self._call_llm(messages)
        for attempt in range(self.max_parse_retries + 1):
            try:
                return self._parse_output(raw)
            except ParseError as exc:
                if attempt >= self.max_parse_retries:
                    raise
                logger.info("%s: invalid reply, asking for a correction: %s", type(self).__name__, exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": self._correction_message(exc)},
                ]
                raw = await self._call_llm(messages)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # Message building (override in subclasses)
    # ------------------------------------------------------------------

    def _build_system_message(self) -> dict[str, str]:
        """Build the system prompt message."""
        prompt = self.system_prompt
        if not prompt:
            raise AgentError(f"{type(self).__name__}: system_prompt must be set")
        return {"role": "system", "content": prompt}

    def _build_user_message(self, **context: Any) -> str:
        """Build the user message from agent-specific context.

        Subclasses MUST override this. The returned string becomes the
        single user message sent to the LLM.
        """
        raise NotImplementedError("Subclasses must implement _build_user_message")

    def _build_messages(self, **context: Any) -> list[dict[str, str]]:
        """Assemble the full message list (system + user)."""
        user_msg = self._build_user_message(**context)
        char_count = len(user_msg)
        token_estimate = char_count // _CHAR_PER_TOKEN
        logger.debug(
            "%s prompt: %d chars, ~%d tokens",
            type(self).__name__,
            char_count,
            token_estimate,
        )
        return [
            self._build_system_message(),
            {"role": "user", "content": user_msg},
        ]

    # ------------------------------------------------------------------
    # LLM calling
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        timeout_s: float = 300.0,
    ) -> str:
        """Call the LLM and return the raw text response.

        Uses httpx.AsyncClient for async HTTP calls to the llama.cpp
        OpenAI-compatible endpoint.
        """
        base_url = self.config.llm_endpoint.rstrip("/")
        model = self.config.llm_model

        payload = {
            "model": model,
            "messages": messages,
            "temperature": QWEN_SAMPLING["temperature"],
            "top_p": QWEN_SAMPLING["top_p"],
            "top_k": QWEN_SAMPLING["top_k"],
            "max_tokens": self.config.max_tokens,
        }

        client = self._client or httpx.AsyncClient(timeout=timeout_s)
        try:
            url = f"{base_url}/chat/completions"
            logger.debug("LLM call to %s (model=%s)", base_url, model)
            # No db session here, so only LLM_API_KEY from the environment applies.
            auth = llm_auth_headers()
            auth_kwargs: dict[str, Any] = {"headers": auth} if auth else {}
            async with get_shared_async_limiter(f"council:{self.config.llm_endpoint}"):
                response = await client.post(url, json=payload, **auth_kwargs)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices")
            if not choices:
                logger.error("LLM returned no choices: %s", data)
                return self._default_response()
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                raise LLMCallError("LLM returned empty response")
            return content
        except httpx.HTTPStatusError as exc:
            raise LLMCallError(f"LLM HTTP {exc.response.status_code}: {exc.response.text[:500]}") from exc
        except httpx.TimeoutException as exc:
            raise LLMCallError(f"LLM timeout after {timeout_s}s") from exc
        except httpx.RequestError as exc:
            raise LLMCallError(f"LLM request failed: {exc}") from exc
        finally:
            if self._client is None and client is not None:
                await client.aclose()

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    def _extract_json(self, raw: str) -> dict[str, Any]:
        """Best-effort JSON extraction from LLM output.

        Handles:
        - Pure JSON: {"key": "value"}
        - Markdown-fenced: ```json\n{...}\n```
        - Malformed: trailing commas, unquoted keys (basic)
        """
        raw = raw.strip()

        # Strip markdown code fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            # Remove opening fence (may have language specifier)
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        # Find the first { and last } for cases where LLM adds extra text
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and start < end:
            raw = raw[start : end + 1]

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Last resort: try stripping trailing commas (common LLM error)
        try:
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Failed to parse LLM output as JSON: {exc}\nRaw output:\n{raw[:1000]}") from exc

    def _parse_output(self, raw: str) -> BaseModel:
        """Parse raw LLM output into a validated Pydantic model.

        Single attempt; :meth:`run` owns the corrective retry. The
        ``ParseError`` message carries the reason (JSON error or the failing
        fields), so the retry prompt and the log can say what was wrong.

        Raises:
            ParseError: Output is not JSON or fails model validation.
        """
        name = self.output_model.__name__
        try:
            data = self._extract_json(raw)
        except ParseError as exc:
            raise ParseError(
                f"Failed to parse/validate LLM output for {name}: {str(exc).splitlines()[0]}"
            ) from exc
        try:
            # Pydantic v2: model_validate raises ValidationError
            return self.output_model.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            raise ParseError(f"Failed to parse/validate LLM output for {name}: {problems}") from exc
        except Exception as exc:
            raise ParseError(f"Failed to parse/validate LLM output for {name}: {exc}") from exc

    def _correction_message(self, exc: ParseError) -> str:
        """Follow-up user turn asking the model to fix its previous reply."""
        return (
            f"Your previous reply could not be used. {exc}\n"
            f"Return ONLY a single corrected JSON object matching the {self.output_model.__name__} "
            "schema from the instructions above, with every numeric field inside its stated range."
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _default_response(self) -> str:
        """Return an empty response placeholder when the LLM gives no content."""
        return ""

    def _format_holdings(self, holdings: dict[str, Any]) -> str:
        """Format holdings dict into a human-readable table string."""
        if not holdings:
            return "No current holdings."
        lines = ["Current Holdings:", ""]
        for asset_id, h in holdings.items():
            if isinstance(h, dict):
                lines.append(
                    f"  {asset_id}: {h.get('quantity', 0)} @ {h.get('avg_price', 0)}"
                )
            else:
                lines.append(f"  {asset_id}: {h}")
        return "\n".join(lines)

    def _format_cash(self, cash: dict[str, Any]) -> str:
        """Format cash dict into a human-readable string."""
        if not cash:
            return "No cash balances."
        lines = ["Cash Balances:", ""]
        for currency, c in cash.items():
            if isinstance(c, dict):
                lines.append(f"  {currency}: {c.get('amount', 0)}")
            else:
                lines.append(f"  {currency}: {c}")
        return "\n".join(lines)

    def _json_block(self, data: Any) -> str:
        """Serialize data as a JSON block with indentation."""
        return json.dumps(data, indent=2, default=_json_default)
