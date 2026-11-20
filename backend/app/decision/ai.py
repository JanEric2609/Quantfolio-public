import asyncio
import json
from datetime import UTC, datetime
from collections.abc import Generator
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.foundation.models.entities import Holding, Portfolio, Recommendation, RecommendationAttempt, WatchlistItem
from app.foundation.schemas import (
    RecommendationMode,
    RecommendationPayload,
    RecommendationPayloadV2,
)
from app.foundation import market as market_service
from app.foundation.budget import monthly_summary
from app.foundation.settings import resolve_llm_base_url, resolve_llm_model, get_secret
from app.foundation.llm.router import call as llm_call

T = TypeVar("T", bound=BaseModel)


DEFAULT_RECOMMENDATION_UNIVERSE = [
    {"ticker": "IWDA.AS", "name": "iShares Core MSCI World UCITS ETF", "type": "etf"},
    {"ticker": "EUNL.DE", "name": "iShares Core MSCI World UCITS ETF", "type": "etf"},
    {"ticker": "VWCE.DE", "name": "Vanguard FTSE All-World UCITS ETF", "type": "etf"},
    {"ticker": "SXR8.DE", "name": "iShares Core S&P 500 UCITS ETF", "type": "etf"},
    {"ticker": "EXS1.DE", "name": "iShares Core DAX UCITS ETF", "type": "etf"},
    {"ticker": "AAPL", "name": "Apple", "type": "stock"},
    {"ticker": "MSFT", "name": "Microsoft", "type": "stock"},
    {"ticker": "NVDA", "name": "NVIDIA", "type": "stock"},
    {"ticker": "ASML.AS", "name": "ASML", "type": "stock"},
    {"ticker": "SAP.DE", "name": "SAP", "type": "stock"},
]

DEFAULT_GLOBAL_UNIVERSE = [
    {"ticker": "IWDA.AS", "isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF", "type": "etf", "currency": "EUR", "ucits": True},
    {"ticker": "EUNL.DE", "isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF USD (Acc)", "type": "etf", "currency": "EUR", "ucits": True},
    {"ticker": "VWCE.DE", "isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World UCITS ETF", "type": "etf", "currency": "EUR", "ucits": True},
    {"ticker": "SXR8.DE", "isin": "IE00B5BMR087", "name": "iShares Core S&P 500 UCITS ETF", "type": "etf", "currency": "EUR", "ucits": True},
    {"ticker": "EXSA.DE", "isin": "DE0005933956", "name": "iShares STOXX Europe 600 UCITS ETF", "type": "etf", "currency": "EUR", "ucits": True},
    {"ticker": "EUNA.DE", "isin": "IE00BDBRDM35", "name": "iShares Core Global Aggregate Bond UCITS ETF", "type": "bond_etf", "currency": "EUR", "ucits": True},
    {"ticker": "IBGL.DE", "isin": "IE00B3VTN290", "name": "iShares Euro Government Bond 7-10yr UCITS ETF", "type": "bond_etf", "currency": "EUR", "ucits": True},
    {"ticker": "ASML.AS", "name": "ASML Holding", "type": "stock", "currency": "EUR"},
    {"ticker": "SAP.DE", "name": "SAP", "type": "stock", "currency": "EUR"},
    {"ticker": "MSFT", "name": "Microsoft", "type": "stock", "currency": "USD"},
    {"ticker": "AAPL", "name": "Apple", "type": "stock", "currency": "USD"},
]


def _default_universe(kind: str = "default_global") -> list[dict[str, Any]]:
    """Return a filtered universe of candidate assets."""
    rows = DEFAULT_GLOBAL_UNIVERSE
    if kind == "etfs":
        rows = [item for item in rows if item["type"] in {"etf", "bond_etf"}]
    elif kind == "stocks":
        rows = [item for item in rows if item["type"] == "stock"]
    return [item for item in rows if item.get("type") != "crypto"]


def validate_json_payload(schema: type[T], raw: str | dict[str, Any]) -> T:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return schema.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"AI output failed schema validation: {exc}") from exc


def _run_async(coro: Any, timeout_s: float | None = None) -> Any:
    """Run an async coroutine from synchronous code.

    Uses the running event loop's run_until_complete if available (e.g. inside
    a FastAPI sync endpoint called via threadpool), otherwise creates a new loop.

    If *timeout_s* is given the coroutine is wrapped with asyncio.wait_for so
    that a hung LLM backend cannot block a FastAPI worker thread indefinitely.
    asyncio.TimeoutError is raised if the deadline is exceeded.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if timeout_s is not None:
        coro = asyncio.wait_for(coro, timeout=timeout_s)
    return loop.run_until_complete(coro)


class LlmClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key or "local"
        # db is injected lazily; LlmClient callers that have a db session should
        # set this attribute before calling chat methods.
        self._db: Session | None = None

    # Wall-clock deadline applied on top of the backend HTTP timeout.  The
    # backend already enforces timeout_s=60 at the HTTP level; this outer guard
    # catches cases where the backend hangs in queue or bookkeeping code.
    # Note: LlmClient.chat() yields a single-chunk response fully bounded by
    # this wall timeout. An inter_token_timeout_s setting should be added if
    # and when stream_complete() lands on the LLM backends.
    _LLM_WALL_TIMEOUT_S: float = 90.0

    def _require_db(self) -> Session:
        if self._db is None:
            raise RuntimeError("LlmClient db session not set")
        return self._db

    def _chat_content(self, messages: list[dict[str, str]]) -> str:
        completion = _run_async(
            llm_call(
                self._require_db(),
                "interactive",
                messages,
                force_local=False,
            ),
            timeout_s=self._LLM_WALL_TIMEOUT_S,
        )
        return completion.content or "{}"

    def chat(self, messages: list[dict[str, str]]) -> Generator[str, None, None]:
        # The router does not expose a streaming interface; yield the full
        # response as a single chunk to preserve the generator contract.
        content = _run_async(
            llm_call(
                self._require_db(),
                "interactive",
                messages,
                force_local=False,
            ),
            timeout_s=self._LLM_WALL_TIMEOUT_S,
        ).content or ""
        yield content

    def structured_chat(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        return validate_json_payload(schema, self._chat_content(messages))

    def structured_chat_with_repair(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        raw = self._chat_content(messages)
        try:
            return validate_json_payload(schema, raw)
        except ValueError as first_error:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "The previous response failed schema validation. Return only corrected JSON for the "
                        f"same schema, with no markdown or commentary. Validation error: {first_error}"
                    ),
                },
            ]
            repaired = self._chat_content(repair_messages)
            return validate_json_payload(schema, repaired)


def configured_llm(db: Session) -> LlmClient:
    secret = get_secret(db, "llm")
    api_key = secret[0] if secret else "local"
    client = LlmClient(
        resolve_llm_base_url(db),
        resolve_llm_model(db),
        api_key,
    )
    client._db = db
    return client


def deterministic_recommendation(
    ticker: str | None,
    *,
    horizon: Literal["short", "mid", "long"] = "mid",
    context: dict[str, Any] | None = None,
    report_text: str | None = None,
) -> RecommendationPayload:
    context = context or {}
    backtest = context.get("backtest", {})
    metrics = backtest.get("metrics", {})
    fundamentals = context.get("fundamentals", {}).get("data", {})
    holdings = context.get("portfolio", {}).get("holdings", [])
    sharpe = float(metrics.get("sharpe") or 0)
    max_drawdown = float(metrics.get("max_drawdown") or 0)
    total_return = float(metrics.get("total_return") or 0)
    pe_ratio = fundamentals.get("pe_ratio")
    already_held = any((holding.get("ticker") or "").upper() == (ticker or "").upper() for holding in holdings)
    # Extended scoring: include Sortino, Calmar, CVaR when available
    sortino = float(metrics.get("sortino") or 0)
    calmar = float(metrics.get("calmar") or 0)
    cvar = float(metrics.get("historical_cvar") or 0)
    score = (
        30                              # base (credible floor for partial-data fallback)
        + sharpe * 10                    # risk-adjusted return
        + sortino * 8                    # downside risk-adjusted
        + calmar * 5                     # return per unit drawdown
        - max_drawdown * 20             # drawdown penalty
        - abs(cvar) * 5                 # tail risk penalty
        + total_return * 10             # absolute return component
    )
    if pe_ratio and isinstance(pe_ratio, (int, float)) and pe_ratio > 45:
        score -= 8
    if report_text and any(term in report_text.lower() for term in ("risk", "overvalued", "downgrade")):
        score -= 5
    score = max(20, min(82, score))
    verdict: Literal["BUY", "HOLD", "SELL", "AVOID", "WATCH"]
    if score >= 68:
        verdict = "BUY" if not already_held else "HOLD"
    elif score >= 52:
        verdict = "WATCH"
    elif already_held:
        verdict = "HOLD"
    else:
        verdict = "AVOID"
    backtest_summary = {
        "total_return": metrics.get("total_return", 0),
        "sharpe_ratio": metrics.get("sharpe", 0),
        "max_drawdown": metrics.get("max_drawdown", 0),
        "period": backtest.get("period", "latest available data"),
        "status": backtest.get("status", "unavailable"),
    }
    return RecommendationPayload(
        ticker=ticker,
        horizon=horizon,
        verdict=verdict,
        confidence=round(score, 1),
        thesis=(
            f"{ticker or 'Portfolio'} has a deterministic signal score of {score:.1f}/100 based on "
            "available price, risk, and portfolio context."
        ),
        reasoning=(
            "This fallback analysis uses market data, backtest metrics, and portfolio concentration "
            "because the configured LLM was unavailable or returned invalid JSON."
        ),
        pros=[
            "Uses the same stored portfolio and market context that is sent to the LLM.",
            f"Backtest Sharpe is {sharpe:.2f} with total return {total_return:.1%}.",
        ],
        cons=[
            "No live LLM narrative was accepted for this run.",
            "Fundamental and news coverage may be incomplete if provider keys are missing.",
        ],
        portfolio_fit=(
            "Already held; treat this as a rebalance/monitoring signal."
            if already_held
            else "Not currently held; cap any new satellite allocation until conviction improves."
        ),
        dkb_available=False,
        risk_notes=[
            f"Observed max drawdown is {max_drawdown:.1%}." if max_drawdown else "Drawdown data is limited.",
            "Do not treat this deterministic fallback as personalized financial advice.",
        ],
        sell_trigger="Reassess if drawdown worsens materially or the original thesis no longer holds.",
        backtest_summary=backtest_summary,
        macro_context=context.get("macro_context") or "Macro data was included when available.",
        generated_at=datetime.now(UTC),
    )


def persist_recommendation(
    db: Session,
    user_id: str,
    payload: RecommendationPayload,
    backtest: dict[str, Any] | None = None,
) -> Recommendation:
    row = Recommendation(
        user_id=user_id,
        ticker=payload.ticker,
        horizon=payload.horizon,
        verdict=payload.verdict,
        confidence=payload.confidence,
        payload_json=payload.model_dump_json(),
        backtest_json=json.dumps(backtest or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def payload_v2_to_legacy(payload: RecommendationPayloadV2) -> RecommendationPayload:
    verdict_map: dict[
        Literal[
            "STRONG_CANDIDATE",
            "CANDIDATE",
            "WATCH",
            "HOLD_EXISTING",
            "AVOID",
            "REJECTED_BY_RISK",
            "STALE",
        ],
        Literal["BUY", "HOLD", "SELL", "AVOID", "WATCH"],
    ] = {
        "STRONG_CANDIDATE": "BUY",
        "CANDIDATE": "WATCH",
        "WATCH": "WATCH",
        "HOLD_EXISTING": "HOLD",
        "AVOID": "AVOID",
        "REJECTED_BY_RISK": "AVOID",
        "STALE": "WATCH",
    }
    return RecommendationPayload(
        ticker=payload.asset.symbol,
        horizon="short" if payload.mode == "short_term" else "long",
        verdict=verdict_map[payload.verdict],
        confidence=payload.confidence,
        thesis=payload.thesis,
        reasoning=f"{payload.thesis} {payload.bear_case}",
        pros=[payload.bull_case],
        cons=payload.why_not_buy,
        portfolio_fit=f"Portfolio fit score {payload.portfolio_fit_score:.1f}/100.",
        dkb_available=False,
        risk_notes=payload.risk_notes,
        sell_trigger="Reassess if invalidation triggers are hit.",
        backtest_summary=payload.backtest_summary.model_dump(),
        macro_context="Macro fit is included when provider data is available.",
        generated_at=payload.generated_at,
    )


def persist_recommendation_v2(
    db: Session,
    user_id: str,
    payload: RecommendationPayloadV2,
    backtest: dict[str, Any] | None = None,
) -> Recommendation:
    legacy = payload_v2_to_legacy(payload)
    row = Recommendation(
        user_id=user_id,
        ticker=payload.asset.symbol,
        horizon=legacy.horizon,
        verdict=legacy.verdict,
        confidence=payload.confidence,
        payload_json=legacy.model_dump_json(),
        backtest_json=json.dumps(backtest or {}, default=str),
        recommendation_v2_payload_json=payload.model_dump_json(),
        recommendation_expiry=payload.expires_at,
        approval_state=payload.approval_state,
        mode=payload.mode,
        data_quality_score=payload.data_quality_score,
        risk_score=payload.risk_score,
        portfolio_fit_score=payload.portfolio_fit_score,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_recommendation_attempt(
    db: Session,
    user_id: str,
    *,
    ticker: str | None,
    horizon: str,
    status: str,
    error_message: str | None = None,
    debug: dict[str, Any] | None = None,
) -> RecommendationAttempt:
    row = RecommendationAttempt(
        user_id=user_id,
        ticker=ticker,
        horizon=horizon,
        status=status,
        error_message=error_message,
        debug_json=json.dumps(debug or {}, default=str),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def build_analysis_context(
    db: Session,
    user_id: str,
    ticker: str,
    backtest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    holdings = [
        {
            "ticker": row.ticker,
            "isin": row.isin,
            "name": row.name,
            "asset_type": row.asset_type,
            "quantity": float(row.quantity),
            "avg_buy_price": float(row.avg_buy_price) if row.avg_buy_price is not None else None,
            "currency": row.currency,
        }
        for row in db.query(Holding).join(Portfolio).filter(Portfolio.user_id == user_id).all()
    ]
    try:
        budget = monthly_summary(db, user_id)
    except Exception:
        budget = {}
    try:
        fundamentals = market_service.fundamentals(db, ticker)
    except Exception:
        fundamentals = {"ticker": ticker, "data": {}, "source": "unavailable", "stale": True}
    try:
        macro = market_service.macro_indicators(db)
    except Exception:
        macro = []
    return {
        "portfolio": {"holdings": holdings},
        "budget": budget,
        "fundamentals": fundamentals,
        "macro": macro,
        "macro_context": _macro_context(macro),
        "backtest": backtest or {},
    }


def recommendation_messages(
    ticker: str,
    horizon: str,
    context: dict[str, Any],
    report_text: str | None = None,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Return only JSON matching the recommendation schema. Include verdict, confidence from 0 to 100, "
                "reasoning, pros, cons, portfolio_fit, risk_notes, backtest_summary, macro_context, and sell_reasoning "
                "when verdict is SELL. Also include an 'evidence' list backing your reasoning: each item must be "
                "{source: a dot-path into the provided context that resolves to the cited value (e.g. "
                "'fundamentals.data.pe_ratio', 'backtest.metrics.sharpe', 'portfolio.holdings.0.ticker', 'macro.0.value'), "
                "value: the cited value, interpretation: a short note on why it matters}. Only cite paths that actually "
                "exist in the context you were given — claims backed by a source that doesn't resolve will be discarded."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ticker": ticker,
                    "horizon": horizon,
                    "context": context,
                    "tradingagents_report": report_text,
                },
                default=str,
            ),
        },
    ]


def _resolve_context_path(context: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve a dot-separated path (dict keys and/or list indices) against *context*.

    Returns (found, value). Mirrors the spirit of recommendation_engine.validator's
    evidence-stripping, but adapted to build_analysis_context's plain dict/list shape
    instead of the typed ContextBundle.
    """
    node: Any = context
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list):
            if not part.isdigit():
                return False, None
            idx = int(part)
            if idx < 0 or idx >= len(node):
                return False, None
            node = node[idx]
        else:
            return False, None
    return True, node


def strip_unverifiable_evidence(evidence: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop evidence items whose source path doesn't resolve against *context*."""
    verified = []
    for item in evidence:
        source = item.get("source") if isinstance(item, dict) else None
        if not source or not isinstance(source, str):
            continue
        found, _value = _resolve_context_path(context, source)
        if found:
            verified.append(item)
    return verified


def candidate_universe(db: Session, user_id: str, universe: str = "etfs_blue_chips") -> list[dict[str, Any]]:
    if universe == "portfolio":
        rows = db.query(Holding).join(Portfolio).filter(Portfolio.user_id == user_id).all()
        return [
            {"ticker": row.ticker, "name": row.name, "type": row.asset_type}
            for row in rows
            if row.ticker
        ]
    if universe == "watchlist":
        rows = db.query(WatchlistItem).filter(WatchlistItem.user_id == user_id).all()
        return [
            {"ticker": row.ticker, "name": row.name, "type": "watchlist", "horizon": row.horizon_tag}
            for row in rows
            if row.ticker
        ]
    if universe in {"default_global", "etfs", "stocks"}:
        return _default_universe(universe)
    return [item for item in DEFAULT_RECOMMENDATION_UNIVERSE if item.get("type") != "crypto"]


def _macro_context(macro: list[dict[str, Any]]) -> str:
    if not macro:
        return "No macro indicators are currently cached."
    return "; ".join(f"{item['name']}: {item['value']}" for item in macro[:3])


async def generate_recommendations_for_user_async(
    db: Session,
    user_id: str,
    *,
    universe: str = "etfs_blue_chips",
    limit: int = 5,
    mode: RecommendationMode | None = None,
    horizon: str = "mid",
) -> dict:
    """Generate validated, evidence-stripped recommendations for a candidate universe.

    Delegates context-building, LLM generation, and source-attribution
    validation to ``recommendation_engine``, then adapts each validated
    ``RecommendationItem`` to a persisted ``RecommendationPayloadV2`` row via
    ``v2_adapter`` (which reuses AlphaCrafter's real conviction/sizing math
    rather than inventing new heuristic scores — see the Phase 1 item 3
    write-up in docs/archive/plans/recommendation-discovery-consolidation-implementation.md).
    """
    from app.decision.recommendation_engine import (
        generate_recommendations,
        recommendation_item_to_v2,
    )

    _mode: RecommendationMode = mode or ("short_term" if horizon == "short" else "long_term")
    candidates = candidate_universe(db, user_id, universe)[:limit]
    candidates_by_ticker = {c["ticker"]: c for c in candidates if c.get("ticker")}

    created: list[dict] = []
    failed: list[dict] = []

    if not candidates_by_ticker:
        return {"created": created, "failed": failed}

    result = await generate_recommendations(
        db, user_id, candidate_tickers=list(candidates_by_ticker.keys())
    )
    report = result["report"]
    context = result["context"]

    seen_tickers: set[str] = set()
    for item in report.recommendations:
        candidate = candidates_by_ticker.get(item.ticker)
        if candidate is None:
            continue
        seen_tickers.add(item.ticker)
        try:
            recommendation_v2 = recommendation_item_to_v2(
                item, candidate=candidate, context=context, mode=_mode
            )
        except Exception as exc:
            record_recommendation_attempt(
                db, user_id, ticker=item.ticker, horizon=horizon, status="failed",
                error_message=str(exc), debug={"source": "recommendations-generate"},
            )
            failed.append({"ticker": item.ticker, "message": str(exc)})
            continue
        row = persist_recommendation_v2(db, user_id, recommendation_v2)
        record_recommendation_attempt(db, user_id, ticker=item.ticker, horizon=horizon, status="saved")
        created.append({
            "id": row.id,
            "ticker": row.ticker,
            "verdict": recommendation_v2.verdict,
            "approval_state": row.approval_state,
            "mode": row.mode,
            "expires_at": row.recommendation_expiry,
            "risk_score": float(row.risk_score or 0),
            "data_quality_score": float(row.data_quality_score or 0),
            "created_at": row.created_at,
        })

    for ticker in candidates_by_ticker:
        if ticker not in seen_tickers:
            record_recommendation_attempt(
                db, user_id, ticker=ticker, horizon=horizon, status="failed",
                error_message="No recommendation returned for this candidate.",
                debug={"source": "recommendations-generate"},
            )
            failed.append({"ticker": ticker, "message": "No recommendation returned for this candidate."})

    return {"created": created, "failed": failed}


def generate_recommendations_for_user(
    db: Session,
    user_id: str,
    *,
    universe: str = "etfs_blue_chips",
    limit: int = 5,
    mode: RecommendationMode | None = None,
    horizon: str = "mid",
) -> dict:
    """Sync wrapper around ``generate_recommendations_for_user_async`` for
    callers running outside an event loop (the scheduled ``quantfolio-worker``
    job)."""
    return _run_async(
        generate_recommendations_for_user_async(
            db, user_id, universe=universe, limit=limit, mode=mode, horizon=horizon
        )
    )
