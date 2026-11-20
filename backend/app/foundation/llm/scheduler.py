"""In-process asyncio priority queue scheduler for LLM tasks."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel


TaskType = Literal["interactive", "batch_research", "routine"]

# Priority mapping: lower number = higher priority
TASK_PRIORITIES = {
    "interactive": 0,      # user-facing, immediate
    "batch_research": 50,  # overnight Miner, dossier synthesis
    "routine": 100,        # sentiment, summarization, accepts latency
}


class QueuedTask(BaseModel):
    """A task queued for LLM processing."""
    task_id: str
    task_type: TaskType
    priority: int
    queued_at: datetime
    messages: list[dict[str, str]]
    structured_output: type[BaseModel] | None = None
    timeout_s: float = 60.0

    def __lt__(self, other: "QueuedTask") -> bool:
        # Lower priority number = higher precedence; break ties by FIFO (queued_at)
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.queued_at < other.queued_at


class LLMScheduler:
    """In-process asyncio priority queue for LLM task scheduling."""

    def __init__(self):
        self.queue: asyncio.PriorityQueue[QueuedTask] = asyncio.PriorityQueue()
        self.completed_count = 0
        self.in_flight: dict[str, QueuedTask] = {}
        # In-flight accounting for the router. router.call() executes via its own
        # work queue, so it only needs to register/deregister tasks for metrics —
        # it must NOT feed self.queue (which is never drained and would grow
        # unbounded, leaking every prompt for the process lifetime).
        self.pending: dict[str, TaskType] = {}

    async def enqueue(
        self,
        task_id: str,
        task_type: TaskType,
        messages: list[dict[str, str]],
        *,
        structured_output: type[BaseModel] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        """Enqueue a task for LLM processing.

        Args:
            task_id: Unique task identifier
            task_type: One of ("interactive", "batch_research", "routine")
            messages: Chat messages list
            structured_output: Optional output schema
            timeout_s: Request timeout
        """
        task = QueuedTask(
            task_id=task_id,
            task_type=task_type,
            priority=TASK_PRIORITIES[task_type],
            queued_at=datetime.now(UTC),
            messages=messages,
            structured_output=structured_output,
            timeout_s=timeout_s,
        )
        await self.queue.put(task)

    async def dequeue(self) -> QueuedTask:
        """Dequeue the next highest-priority task.

        Returns:
            QueuedTask ready for processing
        """
        task = await self.queue.get()
        self.in_flight[task.task_id] = task
        return task

    def mark_pending(self, task_id: str, task_type: TaskType) -> None:
        """Register a task the router has started (in-flight), for metrics only."""
        self.pending[task_id] = task_type

    def mark_done(self, task_id: str) -> None:
        """Mark a task as completed.

        Args:
            task_id: The completed task's ID
        """
        self.pending.pop(task_id, None)
        self.in_flight.pop(task_id, None)
        self.completed_count += 1

    def queue_depth(self) -> dict[TaskType, int]:
        """Return current queue depth by task type.

        Returns:
            Dict mapping task type to pending count
        """
        depth: dict[TaskType, int] = {"interactive": 0, "batch_research": 0, "routine": 0}
        for task_type in self.pending.values():
            depth[task_type] += 1
        return depth

    def queue_status(self) -> dict:
        """Return queue status for diagnostics.

        Returns:
            Dict with queue depth, in-flight count, completed count
        """
        depth = self.queue_depth()
        return {
            "pending_interactive": depth["interactive"],
            "pending_batch_research": depth["batch_research"],
            "pending_routine": depth["routine"],
            "in_flight": len(self.pending),
            "completed": self.completed_count,
        }


# Global scheduler instance (singleton)
_scheduler: LLMScheduler | None = None


def get_scheduler() -> LLMScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = LLMScheduler()
    return _scheduler
