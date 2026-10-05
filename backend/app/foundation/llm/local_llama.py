"""Local llama.cpp backend via OpenAI-compatible API."""

import json
import logging
from typing import Any, cast

from pydantic import BaseModel

from .base import Completion
from .sampling import QWEN_SAMPLING

logger = logging.getLogger(__name__)

_SCHEMA_REPAIR_INSTRUCTION = (
    "Return ONLY valid JSON conforming exactly to this JSON schema. "
    "No prose, no markdown code fences, no additional properties:\n{schema}"
)


def _build_completion_kwargs(structured_output: type[BaseModel] | None) -> dict[str, Any]:
    """Build chat.completions.create kwargs except messages/model/timeout.

    Spreads the Qwen non-thinking sampling preset. ``top_k`` is non-standard
    for the OpenAI SDK and must travel via ``extra_body`` (a bare kwarg is
    rejected). Structured requests add a json_object response format carrying
    the JSON schema — the shape llama.cpp's OpenAI-compatible handler has
    supported longest (mirrors ``review.py:_local_llm_sync``).
    """
    kwargs: dict[str, Any] = {
        "temperature": QWEN_SAMPLING["temperature"],
        "top_p": QWEN_SAMPLING["top_p"],
        "extra_body": {"top_k": QWEN_SAMPLING["top_k"]},
    }
    if structured_output is not None:
        kwargs["response_format"] = {
            "type": "json_object",
            "schema": structured_output.model_json_schema(),
        }
    return kwargs


def _is_schema_rejection(exc: BaseException) -> bool:
    """True when an API error looks like the server rejecting the constrained payload.

    llama.cpp builds predating grammar support answer a schema-carrying
    request with HTTP 4xx. ``APIStatusError`` subclasses carry ``status_code``
    directly; older shapes expose it only via ``response.status_code``. A
    missing status is treated conservatively as NOT a rejection (no retry),
    except for an explicit ``BadRequestError``.
    """
    from openai import BadRequestError

    if isinstance(exc, BadRequestError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


class LocalLlamaBackend:
    """OpenAI-compatible llama.cpp client."""

    def __init__(self, base_url: str, model: str = "qwen3.5-9b", api_key: str | None = None):
        """Initialize the local llama backend.

        Args:
            base_url: Base URL of the llama.cpp server (e.g., http://<llm-host>:8080/v1)
            model: Model name to use in requests
            api_key: Bearer key when llama-server runs with ``--api-key``; ``None``
                sends the placeholder an unauthenticated server ignores.
        """
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        """Lazily build and cache the AsyncOpenAI client so connection pooling
        works across calls instead of re-creating a client every request."""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key or "local")
        return self._client

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        structured_output: type[BaseModel] | None = None,
        timeout_s: float = 60.0,
    ) -> Completion:
        """Complete a chat request using local llama.cpp."""
        from openai import APIError
        from openai.types.chat import ChatCompletionMessageParam

        client = self._get_client()

        try:
            typed_messages = cast(list[ChatCompletionMessageParam], messages)
            response = await client.chat.completions.create(
                model=self.model,
                messages=typed_messages,
                timeout=timeout_s,
                **_build_completion_kwargs(structured_output),
            )
        except APIError as e:
            if structured_output is None or not _is_schema_rejection(e):
                raise RuntimeError(f"Local llama.cpp endpoint error: {e}") from e
            # Old llama.cpp builds reject the schema field outright (HTTP 4xx).
            # Retry exactly once without the constraint, appending a prompt-side
            # repair instruction carrying the schema. The model_validate gate
            # below stays the true correctness check either way: llama-server
            # can also fail OPEN on unparseable grammars (#19051).
            logger.warning(
                "LLM rejected schema-constrained request (%s); retrying unconstrained: %s",
                getattr(e, "status_code", "?"),
                str(e)[:200],
            )
            schema_json = json.dumps(structured_output.model_json_schema())
            retry_messages = [
                *messages,
                {
                    "role": "system",
                    "content": _SCHEMA_REPAIR_INSTRUCTION.format(schema=schema_json),
                },
            ]
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=cast(list[ChatCompletionMessageParam], retry_messages),
                    timeout=timeout_s,
                    **_build_completion_kwargs(None),
                )
            except APIError as retry_error:
                raise RuntimeError(
                    f"Local llama.cpp endpoint error: {retry_error}"
                ) from retry_error

        content = response.choices[0].message.content or ""

        if structured_output:
            try:
                data = json.loads(content)
                structured_output.model_validate(data)
            except Exception as e:
                raise ValueError(f"JSON schema validation failed: {e}") from e

        usage = response.usage
        return Completion(
            content=content,
            model=response.model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            backend="local_llama",
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using local llama.cpp."""
        client = self._get_client()

        try:
            response = await client.embeddings.create(
                model=self.model,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            raise RuntimeError(f"Local embedding failed: {e}") from e
