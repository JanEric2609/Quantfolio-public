"""LLM router: selects backend by task type and available keys with priority-queue scheduling."""

import asyncio
import itertools
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.llm.audit import record_llm_call
from app.foundation.llm.base import Completion
from app.foundation.llm.cloud_anthropic import AnthropicBackend
from app.foundation.llm.cloud_openai import OpenAIBackend
from app.foundation.llm.local_llama import LocalLlamaBackend
from app.foundation.llm.scheduler import TaskType, get_scheduler
from app.foundation.settings import (
    get_public_settings,
    get_secret,
    resolve_llm_api_key,
    resolve_llm_base_url,
    resolve_llm_model,
    resolve_setting,
)

class QueueWaitTimeoutError(TimeoutError):
    """Raised when an LLM call exceeds its allocated queue-wait and processing budget."""


# ---------------------------------------------------------------------------
# Priority queue worker
# ---------------------------------------------------------------------------
# Priority mapping: lower number = higher priority (asyncio.PriorityQueue semantics)
_PRIORITY: dict[str, int] = {
    "interactive": 0,
    "routine": 1,
    "batch_research": 2,
}

# Module-level queue and worker task (created lazily on first call)
_queue: asyncio.PriorityQueue | None = None
_worker_task: asyncio.Task | None = None

# Monotonic tie-breaker so two items with identical priority never trigger the
# Future-vs-Future comparison that asyncio.PriorityQueue would otherwise attempt
# (Future does not implement __lt__, so the heap would raise TypeError).
_seq_counter = itertools.count()


def _get_queue() -> asyncio.PriorityQueue:
    global _queue
    if _queue is None:
        _queue = asyncio.PriorityQueue()
    return _queue


async def _worker() -> None:
    """Background coroutine that drains the priority queue in priority order."""
    queue = _get_queue()
    while True:
        priority, _seq, fut, db, backend, is_degraded, messages, opts, user_id = await queue.get()
        try:
            if fut.cancelled():
                continue
            completion = await backend.complete(
                messages,
                structured_output=opts.get("structured_output"),
                timeout_s=opts.get("timeout_s", 60.0),
            )
            # Bug 4 fix: record audit event with degraded_quality flag when falling back
            if is_degraded:
                try:
                    record_llm_call(
                        db,
                        backend="local_llama",
                        model=backend.model,
                        task_type=opts.get("task_type", "batch_research"),
                        tokens_in=completion.tokens_in,
                        tokens_out=completion.tokens_out,
                        is_degraded=True,
                        user_id=user_id,
                    )
                except Exception:
                    pass  # never let audit failure break the completion path
            if not fut.done():
                fut.set_result(completion)
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
        finally:
            queue.task_done()


def _ensure_worker() -> None:
    """Start the background worker if it is not already running.

    Must only be called from within a running event loop (i.e. from an async
    function).  get_running_loop() raises RuntimeError if there is no running
    loop, which is the correct signal to surface rather than silently handing
    back a potentially wrong / deprecated loop from get_event_loop().
    """
    global _worker_task
    if _worker_task is None or _worker_task.done():
        loop = asyncio.get_running_loop()
        _worker_task = loop.create_task(_worker())


class RouterConfig:
    """Configuration for the LLM router."""

    def __init__(self, db: Session):
        self.db = db
        self.llm_base_url = resolve_llm_base_url(db)
        self.llm_model = resolve_llm_model(db)
        self.llm_api_key = resolve_llm_api_key(db)

        # Load cloud API keys
        anthropic_secret = get_secret(db, "anthropic")
        openai_secret = get_secret(db, "openai")

        self.anthropic_key = anthropic_secret[0] if anthropic_secret else None
        self.openai_key = openai_secret[0] if openai_secret else None
        self.anthropic_model = resolve_setting(db, "anthropic_model", "ANTHROPIC_MODEL", "claude-opus-4-8")
        self.openai_model = resolve_setting(db, "openai_model", "OPENAI_MODEL", "gpt-4o")
        self.cloud_fallback = bool(get_public_settings(db).get("llm_cloud_fallback_enabled", False))

    def get_backend_for_task(
        self,
        task_type: TaskType,
        force_local: bool = False,
    ) -> tuple[LocalLlamaBackend | AnthropicBackend | OpenAIBackend, bool]:
        """Select backend for a task type.

        Args:
            task_type: One of ("interactive", "batch_research", "routine")
            force_local: Override cloud routing for privacy-sensitive paths

        Returns:
            Tuple of (backend, is_cloud_fallback)
            is_cloud_fallback indicates whether degraded_quality audit flag should be set
        """
        if force_local or task_type in ("interactive", "routine"):
            # Local only for interactive/routine
            return LocalLlamaBackend(self.llm_base_url, self.llm_model, self.llm_api_key), False

        # batch_research: cloud → local with degraded flag
        if self.cloud_fallback:
            if self.anthropic_key:
                return AnthropicBackend(self.anthropic_key, self.anthropic_model), False
            elif self.openai_key:
                return OpenAIBackend(self.openai_key, self.openai_model), False
        # Fallback to local with degraded quality flag
        return LocalLlamaBackend(self.llm_base_url, self.llm_model, self.llm_api_key), True


async def call(
    db: Session,
    task_type: TaskType,
    messages: list[dict[str, str]],
    *,
    force_local: bool = False,
    structured_output: type[BaseModel] | None = None,
    timeout_s: float = 60.0,
    user_id: str | None = None,
) -> Completion:
    """Route an LLM call through the appropriate backend.

    This is the single entry point all LLM-emitting code uses.

    Tasks are placed onto a priority queue and executed by a single background
    worker coroutine so that higher-priority tasks (interactive) can pre-empt
    lower-priority tasks (batch_research) in the queue.

    Args:
        db: Database session
        task_type: One of ("interactive", "batch_research", "routine")
        messages: Chat messages list
        force_local: Override cloud routing for privacy-sensitive paths
        structured_output: Optional output schema
        timeout_s: Request timeout

    Returns:
        Completion with content, tokens, and backend info

    Raises:
        RuntimeError: If backend call fails
        ValueError: If schema validation fails
    """
    task_id = str(uuid.uuid4())
    config = RouterConfig(db)

    # Register with the scheduler for queue-depth metrics. Use mark_pending (a
    # simple in-flight registry cleared by mark_done) rather than enqueue, which
    # would push into a PriorityQueue this call path never drains — an unbounded
    # leak of every prompt for the process lifetime.
    scheduler = get_scheduler()
    scheduler.mark_pending(task_id, task_type)

    # Select backend
    backend, is_degraded = config.get_backend_for_task(task_type, force_local)

    # Ensure the background worker is running
    _ensure_worker()

    # Build a Future that the worker will resolve.
    # get_running_loop() is the correct API inside async functions (Python 3.10+);
    # get_event_loop() is deprecated in this context and raises RuntimeError on
    # 3.12 when called from a thread that has no current event loop (e.g. the
    # APScheduler thread that drives scheduled LLM tasks).
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Completion] = loop.create_future()

    opts: dict[str, Any] = {
        "structured_output": structured_output,
        "timeout_s": timeout_s,
        "task_type": task_type,
    }
    priority = _PRIORITY.get(task_type, 99)

    await _get_queue().put((priority, next(_seq_counter), fut, db, backend, is_degraded, messages, opts, user_id))

    try:
        if timeout_s is not None:
            completion = await asyncio.wait_for(fut, timeout=timeout_s)
        else:
            completion = await fut
        scheduler.mark_done(task_id)
        return completion
    except (asyncio.TimeoutError, TimeoutError) as exc:
        scheduler.mark_done(task_id)
        if not fut.done():
            fut.cancel()
        raise QueueWaitTimeoutError(
            f"LLM task '{task_type}' timed out after {timeout_s:.1f}s (queue wait or generation)"
        ) from exc
    except Exception:
        scheduler.mark_done(task_id)
        raise


async def embed(
    db: Session,
    texts: list[str],
) -> list[list[float]]:
    """Embed texts using the configured backend.

    Uses local llama.cpp for embeddings (cloud backends are less suitable for bulk embedding).

    Args:
        db: Database session
        texts: List of text strings to embed

    Returns:
        List of embedding vectors
    """
    config = RouterConfig(db)
    backend = LocalLlamaBackend(config.llm_base_url, config.llm_model, config.llm_api_key)

    try:
        return await backend.embed(texts)
    except Exception as e:
        raise RuntimeError(f"Embedding failed: {e}") from e
