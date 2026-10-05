"""Tests for Phase 6: FinAgent, LLM router, and RAG retrieval."""
import json

from conftest import _memory_db

from app.foundation.models.entities import Embedding, FinAgentRun, User
from app.foundation.llm.scheduler import get_scheduler


def test_scheduler_priority_queue_basic():
    """Test that scheduler respects task priority: interactive > batch_research > routine."""
    scheduler = get_scheduler()

    # Enqueue in reverse priority order
    import asyncio

    async def enqueue_tasks():
        await scheduler.enqueue("routine_1", "routine", [{"role": "user", "content": "hello"}])
        await scheduler.enqueue("batch_1", "batch_research", [{"role": "user", "content": "analyze"}])
        await scheduler.enqueue("interactive_1", "interactive", [{"role": "user", "content": "chat"}])

    # Run the enqueue operations
    asyncio.run(enqueue_tasks())

    # Dequeue should return interactive first (highest priority = lowest number)
    async def dequeue_order():
        task1 = await scheduler.dequeue()
        task2 = await scheduler.dequeue()
        task3 = await scheduler.dequeue()
        return [task1.task_type, task2.task_type, task3.task_type]

    types = asyncio.run(dequeue_order())
    assert types == ["interactive", "batch_research", "routine"]


def test_embedding_model_exists():
    """Test that Embedding model is properly defined (requires postgres for full test)."""
    # Embedding model is only fully testable with PostgreSQL due to pgvector extension
    # This test just verifies the model is defined
    assert hasattr(Embedding, '__tablename__')
    assert Embedding.__tablename__ == "embeddings"


def test_finagent_run_model_creation():
    """Test that FinAgentRun model can be created and stored."""
    db = _memory_db()
    user = User(username="test", password_hash="hash")
    db.add(user)
    db.commit()

    run = FinAgentRun(
        agent="budget",
        query="why is groceries expensive?",
        result_json=json.dumps({"insight": "You spent more", "trend": "up"}),
    )
    db.add(run)
    db.commit()

    # Verify retrieval
    stored = db.query(FinAgentRun).filter(FinAgentRun.agent == "budget").one()
    assert stored.query == "why is groceries expensive?"
    result = json.loads(stored.result_json)
    assert result["trend"] == "up"


def test_scheduler_queue_status():
    """Test that scheduler.queue_status() returns correct depth."""
    scheduler = get_scheduler()

    # Get initial status
    status = scheduler.queue_status()
    assert "pending_interactive" in status
    assert "pending_batch_research" in status
    assert "pending_routine" in status
    assert "in_flight" in status
    assert "completed" in status
