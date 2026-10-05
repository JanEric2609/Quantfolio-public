"""OpenAI backend via OpenAI SDK."""

import json
from typing import cast

from openai import APIError, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from .base import Completion


class OpenAIBackend:
    """OpenAI client using OpenAI SDK."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        """Initialize the OpenAI backend.

        Args:
            api_key: OpenAI API key
            model: Model name (e.g., gpt-4o, gpt-4o-mini, o1-mini)
        """
        self.api_key = api_key
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        structured_output: type[BaseModel] | None = None,
        timeout_s: float = 60.0,
    ) -> Completion:
        """Complete a chat request using OpenAI."""
        try:
            typed_messages = cast(list[ChatCompletionMessageParam], messages)
            if structured_output:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=typed_messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": structured_output.__name__,
                            "schema": structured_output.model_json_schema(),
                        },
                    },
                    temperature=0.2,
                    timeout=timeout_s,
                )
            else:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=typed_messages,
                    temperature=0.2,
                    timeout=timeout_s,
                )

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
                backend="openai",
            )
        except APIError as e:
            raise RuntimeError(f"OpenAI API error: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using OpenAI Embeddings API."""
        try:
            response = await self.client.embeddings.create(
                model="text-embedding-3-small",
                input=texts,
            )
            return [item.embedding for item in response.data]
        except APIError as e:
            raise RuntimeError(f"OpenAI embedding error: {e}") from e
