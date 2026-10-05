"""Anthropic Claude backend via Anthropic Messages API."""

import json
from typing import Any, cast

from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam, TextBlock
from pydantic import BaseModel

from .base import Completion


class AnthropicBackend:
    """Anthropic Claude client using Anthropic Messages API."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-8"):
        """Initialize the Anthropic backend.

        Args:
            api_key: Anthropic API key
            model: Model name (e.g., claude-opus-4-8, claude-sonnet-4-6)
        """
        self.api_key = api_key
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        structured_output: type[BaseModel] | None = None,
        timeout_s: float = 60.0,
    ) -> Completion:
        """Complete a chat request using Anthropic Claude."""
        try:
            # Anthropic doesn't support JSON mode via response_format,
            # but Claude naturally produces JSON in system prompts
            if structured_output:
                system = (
                    "Respond with valid JSON only, following this schema exactly. "
                    f"Schema: {structured_output.model_json_schema()}"
                )
            else:
                system = None

            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 4096,
                "messages": cast(list[MessageParam], messages),
                "timeout": timeout_s,
            }
            if system:
                kwargs["system"] = system
            response = await self.client.messages.create(**kwargs)

            first_block = response.content[0] if response.content else None
            content = first_block.text if isinstance(first_block, TextBlock) else ""

            if structured_output:
                try:
                    data = json.loads(content)
                    structured_output.model_validate(data)
                except Exception as e:
                    raise ValueError(f"JSON schema validation failed: {e}") from e

            return Completion(
                content=content,
                model=response.model,
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                backend="anthropic",
            )
        except APIError as e:
            raise RuntimeError(f"Anthropic API error: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embedding via Anthropic (uses an external embed model)."""
        raise NotImplementedError("Anthropic does not provide embedding via Messages API; use local model or pgvector embeddings.")
