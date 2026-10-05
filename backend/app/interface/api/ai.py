import json
import logging
import uuid
from typing import Any, Generator

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.foundation.core.db import get_db
from app.foundation.core.security import limiter
from app.foundation.models.entities import (
    AnalysisReport,
    ChatMessage,
    User,
)
from app.foundation.schemas import (
    BacktestRequest,
    ChatConversationOut,
    ChatMessageOut,
    ChatStreamRequest,
    ObsidianQueryRequest,
    RecommendationGenerateRequest,
    RecommendationGenerateResult,
    RecommendationPayload,
    ReportCreateRequest,
    ReportOut,
)
from app.decision.ai import (
    build_analysis_context,
    configured_llm,
    generate_recommendations_for_user,
    persist_recommendation,
    record_recommendation_attempt,
    recommendation_messages,
    strip_unverifiable_evidence,
)
from app.foundation.auth import current_user
from app.foundation import obsidian as obsidian_service
from app.foundation.backtest_strategy import run_strategy_backtest_cached

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/analyse-report", response_model=RecommendationPayload)
def analyse_report(payload: BacktestRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RecommendationPayload:
    backtest = run_strategy_backtest_cached(db, payload.ticker, payload.strategy, payload.start, payload.end, payload.params)
    context = build_analysis_context(db, user.id, payload.ticker, backtest)
    try:
        recommendation = configured_llm(db).structured_chat_with_repair(
            recommendation_messages(payload.ticker, "mid", context),
            RecommendationPayload,
        )
    except Exception as exc:
        record_recommendation_attempt(
            db,
            user.id,
            ticker=payload.ticker,
            horizon="mid",
            status="failed",
            error_message=str(exc),
            debug={"source": "analyse-report", "strategy": payload.strategy},
        )
        raise _llm_recommendation_error(exc) from exc
    recommendation.evidence = strip_unverifiable_evidence(recommendation.evidence, context)
    persist_recommendation(db, user.id, recommendation, backtest)
    record_recommendation_attempt(db, user.id, ticker=payload.ticker, horizon="mid", status="saved")
    return recommendation


@router.post("/reports", response_model=ReportOut)
def create_report(payload: ReportCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ReportOut:
    report = AnalysisReport(user_id=user.id, **payload.model_dump())
    db.add(report)
    db.commit()
    db.refresh(report)
    return ReportOut.model_validate(report)


@router.post("/reports/upload", response_model=ReportOut)
async def upload_report(
    ticker: str | None = None,
    horizon: str = "mid",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ReportOut:
    if not file.filename or not file.filename.lower().endswith((".md", ".txt")):
        raise HTTPException(status_code=400, detail="Only .md and .txt TradingAgents reports are supported.")
    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Report upload is limited to 2 MB.")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Report must be UTF-8 text.") from exc
    payload = ReportCreateRequest(
        title=file.filename,
        ticker=ticker,
        horizon=horizon,  # type: ignore[arg-type]
        content=content,
        source="upload",
    )
    return create_report(payload, db, user)


@router.get("/reports", response_model=list[ReportOut])
def reports(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ReportOut]:
    rows = (
        db.query(AnalysisReport)
        .filter(AnalysisReport.user_id == user.id)
        .order_by(AnalysisReport.created_at.desc())
        .all()
    )
    return [ReportOut.model_validate(r) for r in rows]


@router.post("/reports/{report_id}/analyse", response_model=RecommendationPayload)
def analyse_saved_report(report_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RecommendationPayload:
    report = db.get(AnalysisReport, report_id)
    if report is None or report.user_id != user.id:
        raise HTTPException(status_code=404, detail="Report not found")
    ticker = report.ticker or _infer_ticker(report.content)
    backtest = run_strategy_backtest_cached(db, ticker, "buy_hold")
    context = build_analysis_context(db, user.id, ticker, backtest)
    try:
        recommendation = configured_llm(db).structured_chat_with_repair(
            recommendation_messages(ticker, report.horizon, context, report.content),
            RecommendationPayload,
        )
    except Exception as exc:
        record_recommendation_attempt(
            db,
            user.id,
            ticker=ticker,
            horizon=report.horizon,
            status="failed",
            error_message=str(exc),
            debug={"source": "saved-report", "report_id": report.id},
        )
        raise _llm_recommendation_error(exc) from exc
    recommendation.evidence = strip_unverifiable_evidence(recommendation.evidence, context)
    persist_recommendation(db, user.id, recommendation, backtest)
    record_recommendation_attempt(db, user.id, ticker=ticker, horizon=report.horizon, status="saved")
    return recommendation


@router.post("/recommendations/generate", response_model=RecommendationGenerateResult)
@limiter.limit("3/hour")
def generate_recommendations(
    request: Request,
    payload: RecommendationGenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RecommendationGenerateResult:
    result = generate_recommendations_for_user(
        db, user.id,
        universe=payload.universe,
        limit=payload.limit,
        mode=payload.mode or ("short_term" if payload.horizon == "short" else "long_term"),
        horizon=payload.horizon,
    )
    created = result.get("created", [])
    failed = result.get("failed", [])
    message = (
        f"Saved {len(created)} recommendation(s) for human review."
        if created
        else "No recommendations were saved because scoring could not build enough context for any candidate."
    )
    return RecommendationGenerateResult(created=created, failed=failed, message=message)


@router.post("/chat/stream")
@limiter.limit("10/minute")
def chat_stream(request: Request, payload: ChatStreamRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> StreamingResponse:
    conv_id = payload.conversation_id or str(uuid.uuid4())
    db.add(ChatMessage(user_id=user.id, role="user", content=payload.message, conversation_id=conv_id))
    db.commit()

    context = build_analysis_context(db, user.id, "EUNL.DE")
    system_content = f"Current finance context: {json.dumps(context, default=str)}"

    def generate() -> Generator[str, None, None]:
        full_answer = ""
        try:
            llm = configured_llm(db)
            tokens = llm.chat([
                {"role": "system", "content": system_content},
                {"role": "user", "content": payload.message},
            ])
            for token in tokens:
                full_answer += token
                yield f"data: {json.dumps({'delta': token})}\n\n"
        except Exception:
            fallback = "The local LLM endpoint is not available or returned invalid JSON."
            full_answer = fallback
            yield f"data: {json.dumps({'delta': fallback})}\n\n"

        msg = ChatMessage(user_id=user.id, role="assistant", content=full_answer, conversation_id=conv_id)
        db.add(msg)
        try:
            db.commit()
            db.refresh(msg)
            yield f"data: {json.dumps({'done': True, 'message_id': msg.id, 'conversation_id': conv_id})}\n\n"
        except Exception:
            db.rollback()
            yield f"data: {json.dumps({'done': True, 'error': 'Failed to persist assistant message'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/chat/conversations", response_model=list[ChatConversationOut])
def list_conversations(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ChatConversationOut]:
    first_win = (
        select(
            ChatMessage.conversation_id,
            ChatMessage.content,
            func.row_number()
            .over(partition_by=ChatMessage.conversation_id, order_by=ChatMessage.timestamp)
            .label("rn"),
        )
        .where(
            ChatMessage.user_id == user.id,
            ChatMessage.role == "user",
            ChatMessage.conversation_id.isnot(None),
        )
        .subquery()
    )
    stats = (
        select(
            ChatMessage.conversation_id,
            func.max(ChatMessage.timestamp).label("last_at"),
            func.count(ChatMessage.id).label("message_count"),
        )
        .where(
            ChatMessage.user_id == user.id,
            ChatMessage.conversation_id.isnot(None),
        )
        .group_by(ChatMessage.conversation_id)
        .subquery()
    )
    rows = (
        db.execute(
            select(
                first_win.c.conversation_id,
                first_win.c.content,
                stats.c.last_at,
                stats.c.message_count,
            )
            .join(stats, first_win.c.conversation_id == stats.c.conversation_id)
            .where(first_win.c.rn == 1)
            .order_by(stats.c.last_at.desc())
        )
        .all()
    )
    return [
        ChatConversationOut(
            conversation_id=row.conversation_id,
            title=((row.content or "")[:60] + "..." if len(row.content or "") > 60 else (row.content or "")),
            last_at=row.last_at.isoformat() if row.last_at else "",
            message_count=row.message_count,
        )
        for row in rows
    ]


@router.get("/chat/conversations/{conversation_id}", response_model=list[ChatMessageOut])
def get_conversation(conversation_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ChatMessageOut]:
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user.id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    return [
        ChatMessageOut(
            id=row.id,
            conversation_id=row.conversation_id,
            role=row.role,
            content=row.content,
            timestamp=row.timestamp.isoformat() if row.timestamp else "",
        )
        for row in rows
    ]


@router.delete("/chat/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, str]:
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user.id,
            ChatMessage.conversation_id == conversation_id,
        )
        .all()
    )
    for row in rows:
        db.delete(row)
    db.commit()
    return {"deleted": conversation_id}


@router.get("/context/obsidian/status")
def obsidian_status(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return obsidian_service.obsidian_status(db)


@router.post("/context/obsidian/query")
def obsidian_query(payload: ObsidianQueryRequest, db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return obsidian_service.query_obsidian(db, payload.query, payload.limit)


def _infer_ticker(content: str) -> str:
    words = [word.strip(" ,.;:()[]{}") for word in content.split()]
    for word in words:
        if 2 <= len(word) <= 12 and any(ch.isalpha() for ch in word) and word.upper() == word:
            return word
    return "EUNL.DE"


def _llm_recommendation_error(exc: Exception) -> HTTPException:
    # The cause (endpoint URL, provider response, schema-validation dump) stays on
    # the server: it is logged here and stored on the RecommendationAttempt row.
    logger.warning("LLM recommendation failed: %s", exc, exc_info=exc)
    return HTTPException(
        status_code=503,
        detail={
            "message": "The configured LLM could not return a valid recommendation. "
            "No fallback recommendation was saved. Details are in the server log.",
        },
    )
