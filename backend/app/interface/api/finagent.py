"""FinAgent API endpoints: Budget, Substitution, Explainer agents + RAG + LLM health."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import FinAgentRun, User
from app.foundation.auth import current_user
from app.foundation.finagent.budget_agent import BudgetAgent, BudgetInsight
from app.foundation.finagent.explainer_agent import ExplainerAgent, ExplanationResponse
from app.foundation.finagent.rag import RAGRetriever
from app.foundation.finagent.substitution_agent import SubstitutionAgent, SubstitutionAgentResponse
from app.foundation.llm.scheduler import get_scheduler

router = APIRouter(prefix="/api/finagent", tags=["finagent"])


class BudgetQueryRequest(BaseModel):
    query: str
    year: int
    month: int


class SubstitutionRequest(BaseModel):
    category: str
    current_spend: float


class ExplainerRequest(BaseModel):
    amount: float
    category: str
    description: str
    date: str
    historical_avg: float | None = None


class RAGRetrieveRequest(BaseModel):
    query: str
    top_k: int = 3
    item_type_filter: str | None = None


class LLMHealthResponse(BaseModel):
    local_llama_up: bool
    anthropic_reachable: bool
    openai_reachable: bool
    last_check: str


class QueueStatusResponse(BaseModel):
    pending_interactive: int
    pending_batch_research: int
    pending_routine: int
    in_flight: int
    completed: int


@router.post("/budget/query", response_model=BudgetInsight)
async def budget_query(
    request: BudgetQueryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BudgetInsight:
    """Analyze budget and answer natural-language questions."""
    agent = BudgetAgent(db, user.id)
    insight = await agent.query(request.query, request.year, request.month)

    # Record the run
    run = FinAgentRun(
        agent="budget",
        query=request.query,
        result_json=insight.model_dump_json(),
    )
    db.add(run)
    db.commit()

    return insight


@router.post("/substitutions", response_model=SubstitutionAgentResponse)
async def substitutions(
    request: SubstitutionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SubstitutionAgentResponse:
    """Suggest category substitutions to reduce spending."""
    agent = SubstitutionAgent(db)
    suggestions = await agent.suggest_for_category(request.category, request.current_spend)

    # Record the run
    run = FinAgentRun(
        agent="substitution",
        query=f"category:{request.category} spend:{request.current_spend}",
        result_json=suggestions.model_dump_json(),
    )
    db.add(run)
    db.commit()

    return suggestions


@router.post("/explain", response_model=ExplanationResponse)
async def explain(
    request: ExplainerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ExplanationResponse:
    """Explain a specific charge or anomaly."""
    agent = ExplainerAgent(db)
    explanation = await agent.explain(
        request.amount,
        request.category,
        request.description,
        request.date,
        request.historical_avg,
    )

    # Record the run
    run = FinAgentRun(
        agent="explainer",
        query=request.description,
        result_json=explanation.model_dump_json(),
    )
    db.add(run)
    db.commit()

    return explanation


_EMBED_MAX_TEXTS = 100
_EMBED_MAX_TEXT_LEN = 4_000  # chars per item


@router.post("/embed/index")
async def embed_index(
    texts: list[str],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, int]:
    """Index texts into pgvector embeddings (for corpus building).

    This endpoint is for admin use (e.g., indexing Obsidian vault notes).
    There is no separate admin-role concept in this single-user app, so a
    hard cap on batch size and per-text length is the practical mitigation
    against RAG-poisoning and embedding-DoS.
    """
    from app.foundation.llm.router import embed
    from app.foundation.models.entities import Embedding

    from fastapi import HTTPException
    from sqlalchemy import text

    if len(texts) > _EMBED_MAX_TEXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many texts: maximum {_EMBED_MAX_TEXTS} per request, got {len(texts)}.",
        )
    oversized = [i for i, t in enumerate(texts) if len(t) > _EMBED_MAX_TEXT_LEN]
    if oversized:
        raise HTTPException(
            status_code=400,
            detail=f"Text(s) at index {oversized} exceed the {_EMBED_MAX_TEXT_LEN}-character limit.",
        )

    embeddings = await embed(db, texts)

    if not embeddings:
        raise HTTPException(status_code=503, detail="embedding service unavailable")

    if db.bind is None:
        raise HTTPException(status_code=503, detail="Database connection not available")
    is_postgres = db.bind.dialect.name == "postgresql"

    for i, emb_vector in enumerate(embeddings):
        if not emb_vector:
            raise HTTPException(status_code=503, detail="embedding service unavailable")
        # The pgvector column is added by migration 0020 without a SQLAlchemy
        # mapped attribute on Embedding, so we INSERT the metadata via ORM and
        # set the vector via raw SQL afterwards (Postgres only).
        emb_record = Embedding(
            item_id=f"corpus_{i}",
            item_type="financial_corpus",
            meta_json='{}',
        )
        db.add(emb_record)
        db.flush()  # populate emb_record.id for the vector UPDATE
        if is_postgres:
            db.execute(
                text("UPDATE embeddings SET embedding = (:vec)::vector WHERE id = :id"),
                {"vec": list(emb_vector), "id": emb_record.id},
            )

    db.commit()
    return {"indexed": len(embeddings)}


@router.post("/rag/retrieve")
async def rag_retrieve(
    request: RAGRetrieveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """Retrieve relevant chunks from pgvector using similarity search."""
    retriever = RAGRetriever(db)
    results = await retriever.retrieve(
        request.query,
        top_k=request.top_k,
        item_type_filter=request.item_type_filter,
    )
    return results


@router.get("/llm/queue", response_model=QueueStatusResponse)
def llm_queue_status(user: User = Depends(current_user)) -> QueueStatusResponse:
    """Get current LLM task queue status and depth."""
    scheduler = get_scheduler()
    status = scheduler.queue_status()

    return QueueStatusResponse(
        pending_interactive=status["pending_interactive"],
        pending_batch_research=status["pending_batch_research"],
        pending_routine=status["pending_routine"],
        in_flight=status["in_flight"],
        completed=status["completed"],
    )


@router.get("/llm/health", response_model=LLMHealthResponse)
async def llm_health(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LLMHealthResponse:
    """Check health of all LLM backends (local, Anthropic, OpenAI)."""
    from app.foundation.llm.router import RouterConfig
    from app.foundation.settings import probe_root
    from datetime import UTC, datetime

    config = RouterConfig(db)

    # Check local llama.cpp
    local_up = False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{probe_root(config.llm_base_url)}/health")
            local_up = resp.status_code == 200
    except Exception:
        pass

    # Check Anthropic (just verify key presence; full check would need API call)
    anthropic_reachable = config.anthropic_key is not None

    # Check OpenAI
    openai_reachable = config.openai_key is not None

    return LLMHealthResponse(
        local_llama_up=local_up,
        anthropic_reachable=anthropic_reachable,
        openai_reachable=openai_reachable,
        last_check=datetime.now(UTC).isoformat(),
    )


@router.get("/llm/routing")
def llm_routing_info(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get routing decision info: which backend gets which task type."""
    from app.foundation.llm.router import RouterConfig

    config = RouterConfig(db)
    scheduler = get_scheduler()

    routing = {
        "interactive": "local",  # interactive always routes local per L24
        "batch_research": "anthropic" if config.anthropic_key else ("openai" if config.openai_key else "local_degraded"),
        "routine": "local",
        "queue_status": scheduler.queue_status(),
    }

    return routing
