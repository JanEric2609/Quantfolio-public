"""Base LLM backend protocol and data models."""

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class Completion(BaseModel):
    """Response from an LLM backend."""
    content: str
    model: str
    tokens_in: int
    tokens_out: int
    backend: Literal["local_llama", "anthropic", "openai"]


@runtime_checkable
class LLMBackend(Protocol):
    """Protocol for LLM backends (local llama.cpp, Anthropic, OpenAI)."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        structured_output: type[BaseModel] | None = None,
        timeout_s: float = 60.0,
    ) -> Completion:
        """Complete a chat request.

        Args:
            messages: List of {"role": "user"/"assistant", "content": "..."} dicts
            structured_output: Optional Pydantic model for JSON schema validation
            timeout_s: Request timeout in seconds

        Returns:
            Completion with content, model, token counts, and backend name
        """
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors.

        Args:
            texts: List of text strings

        Returns:
            List of embedding vectors (one per input text)
        """
        ...
