"""LLM-powered dossier writer. Generates structured §5 JSON via single-pass LLM call with deterministic fallback."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.foundation.models.entities import NewsItem, Recommendation, RecommendationDossier
from app.decision.discover.candidate_gate import (
    _resolve_cohort_instrument_type,
    discover_verdict,
    evaluate_track_record,
)
from app.decision.discover.composite import ic_signal_usable
from app.decision.discover.config import DEFAULT_PROMPT_TEMPLATE
from app.foundation.expected_return import (
    EQUITY_PREMIUM_OVER_CASH,
    credibility_weight,
    equilibrium_prior,
    select_return_anchor,
)
from app.foundation.expected_return_blocks import building_block_er
from app.decision.discover import blm
from app.foundation.return_band import compute_return_band
from app.foundation.settings import (
    FALLBACK_RISK_FREE_RATE,
    get_public_settings,
    get_risk_free_rate,
    resolve_llm_base_url,
    resolve_llm_model,
)
from app.foundation.llm.structured import (
    STRUCTURED_JSON_SAMPLING,
    build_retry_messages,
    parse_structured_completion,
    unusable_reason,
)
from app.decision.llm_portfolio.review import _local_llm_sync
from app.foundation.text_safety import ascii_safe

logger = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 2

# Bounds the two unbounded fields in the schema. A grammar constrains
# *structure*, not *termination* — inside an unbounded `"thesis": "…"` every
# repeated token is still legal JSON, so without a maxLength a runaway
# completion burns the whole token budget and the JSON never closes (the same
# failure class fixed for the advisor decision call in PRs #195-197).
_MAX_THESIS_CHARS = 600
_MAX_RISK_CHARS = 160
_MAX_RISKS = 6

_COMPLETION_TOKENS = 1024

# How much of a rejected completion is quoted back on a retry, so echoing one
# runaway answer cannot push the next attempt past the context window.
_RETRY_EXCERPT_CHARS = 800

DOSSIER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["long", "short", "neutral"]},
        "conviction": {"type": "number", "minimum": 0, "maximum": 1},
        "horizon_months": {"type": "integer", "minimum": 1, "maximum": 12},
        # minLength closes the same gap fixed in advisor/llm_decision.py's
        # thesis field (audit-fixes-2026-08 todo 6): without it, the grammar
        # has no reason to refuse "" for a required string field.
        "thesis": {"type": "string", "minLength": 8, "maxLength": _MAX_THESIS_CHARS},
        "key_risks": {
            "type": "array",
            "maxItems": _MAX_RISKS,
            "items": {"type": "string", "minLength": 4, "maxLength": _MAX_RISK_CHARS},
        },
    },
    # Neither expected_return nor expected_return_range is part of the schema.
    #
    # expected_return_range was removed for the same reason, plus its own: the
    # LLM's range was rendered directly beside the anchored, 0.3-dampened,
    # +/-25-clamped expected_return, so the point estimate could sit *outside*
    # its own printed interval (a 20% 3y CAGR over 6 months prints "+3.0%
    # (8.0-15.0)"). Repairing the range would have meant inventing a band
    # width with nothing to calibrate it against, and the measured coverage of
    # LLM-produced intervals does not support keeping it: FermiEval (2025)
    # found nominal 99% intervals covering ~65% of the time, and
    # QuantSightBench (2026), on quantitative forecasting with prediction
    # intervals specifically, found no model reaching its target coverage.
    # If a band is wanted later, the honest source is the Monte Carlo p5/p95
    # already written by advisor/cycle.py, not the model's own guess.
    #
    # expected_return is computed
    # deterministically from signal_breakdown.expected_return_anchor_pct (see
    # write_dossier below), never trusted from the LLM. The model's job is to
    # justify or critique that anchor in `thesis`, not invent its own number
    # (prod incident: XEON.DE, the LLM echoed a 3-year CAGR nearly verbatim as
    # a 3-month forecast — see docs/archive/plans/unified-portfolio-engine-implementation.md
    # Phase 3).
    "required": [
        "direction", "conviction", "horizon_months", "thesis", "key_risks",
    ],
}


class DossierOutputModel(BaseModel):
    direction: Literal["long", "short", "neutral"]
    conviction: float  # 0-1
    horizon_months: int  # 1-12
    # min_length matches DOSSIER_JSON_SCHEMA's thesis/key_risks minLength
    # (audit-fixes-2026-08 todo 6): _coerce_dossier fills a genuinely-missing
    # thesis with "" so pydantic sees a value at all, and this constraint is
    # what turns that into a ValidationError -> retry/fallback instead of a
    # silently-persisted blank-justification dossier (see _try_parse_json's
    # docstring for the exact incident this was already guarding against at
    # the marker-field level).
    thesis: str = Field(min_length=8)
    key_risks: list[str]
    signal_breakdown: dict  # passed in input, echoed back
    estimate: bool = True
    not_financial_advice: bool = True


def _fetch_fundamentals(symbol: str, db: Session) -> dict[str, Any] | None:
    """Fetch latest fundamental metrics for *symbol* via the provider chain."""
    try:
        from app.foundation.market import fundamentals as market_fundamentals

        result = market_fundamentals(db, symbol)
        data = result.get("data") or {}
        if not isinstance(data, dict) or not data:
            logger.debug("_fetch_fundamentals: no valid dict data for %s", symbol)
            return None

        def _as_float(key: str) -> float | None:
            val = data.get(key)
            if val is None or val == "":
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        fundamentals = {
            "pe": _as_float("pe_ratio"),
            "pb": _as_float("pb_ratio"),
            "roe": _as_float("roe"),
            "de_ratio": _as_float("debt_equity"),
            "market_cap": _as_float("market_cap"),
            "revenue_growth": _as_float("revenue_growth"),
            # ADR 0014 §6: ground the equity branch of the dossier prompt in
            # sector/industry rather than leaving the LLM to infer them.
            "sector": data.get("sector"),
            "industry": data.get("industry"),
            "source": result.get("source", "unknown"),
            "stale": bool(result.get("stale", True)),
        }
        logger.debug("_fetch_fundamentals: fetched for %s from %s", symbol, fundamentals["source"])
        return fundamentals
    except Exception as exc:
        logger.warning("_fetch_fundamentals: failed for %s: %s", symbol, exc)
        return None


def _fetch_sentiment(symbol: str, db: Session) -> dict[str, Any]:
    """Return recent news sentiment metrics for *symbol* from the NewsItem table."""
    from datetime import UTC, datetime, timedelta

    neutral = {
        "news_sentiment": 0.5,
        "social_mentions": 0,
        "sentiment_trend": "stable",
    }

    try:
        since = datetime.now(UTC) - timedelta(days=30)
        rows = (
            db.query(NewsItem)
            .filter(NewsItem.ticker == symbol.upper(), NewsItem.published_at >= since)
            .order_by(NewsItem.published_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            logger.debug("_fetch_sentiment: no news rows for %s", symbol)
            return neutral

        scores = [float(r.sentiment_score) for r in rows if r.sentiment_score is not None]
        if not scores:
            return {**neutral, "social_mentions": len(rows)}

        avg = sum(scores) / len(scores)
        # Map [-1, 1] or [0, 1] to [0, 1].
        sentiment = avg if avg >= 0 else (avg + 1.0) / 2.0
        sentiment = max(0.0, min(1.0, sentiment))

        if len(scores) >= 2:
            first_half = sum(scores[: len(scores) // 2]) / max(1, len(scores) // 2)
            second_half = sum(scores[len(scores) // 2 :]) / max(1, len(scores) - len(scores) // 2)
            if first_half > second_half + 0.1:
                trend = "rising"
            elif second_half > first_half + 0.1:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "stable"

        logger.debug("_fetch_sentiment: %s mentions=%d sentiment=%.2f trend=%s", symbol, len(rows), sentiment, trend)
        return {
            "news_sentiment": round(sentiment, 4),
            "social_mentions": len(rows),
            "sentiment_trend": trend,
        }
    except Exception as exc:
        logger.warning("_fetch_sentiment: failed for %s: %s", symbol, exc)
        return neutral


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


_SHORT_HISTORY_PROMPT_REWORD = (
    "short_history (DATA COVERAGE gap: our price database's records for this "
    "ticker start later than our lookback window — this says nothing about "
    "when the underlying fund/company itself launched; do not state or imply "
    "an inception/founding date from this flag)"
)


def _reword_concerns_for_prompt(value: Any) -> Any:
    """Deep-copy *value*, rewording the bare ``"short_history"`` concern token.

    ADR 0014 §6: the raw ``scores`` dict (serialized verbatim into the
    prompt) carries a bare ``"short_history"`` string inside various nested
    ``concerns`` lists (app.decision.discover.pipeline's coverage/span/length
    gate). With no framing, the LLM free-associated "young fund" from that
    token alone — the exact defect behind IS3S.DE-style dossiers describing a
    data-coverage gap as if it were the fund's founding date. This walks the
    whole structure (concerns can appear at several nesting levels) rather
    than special-casing one known path, so it can't miss a location.
    """
    if isinstance(value, list):
        return [
            _SHORT_HISTORY_PROMPT_REWORD if v == "short_history" else _reword_concerns_for_prompt(v)
            for v in value
        ]
    if isinstance(value, dict):
        return {k: _reword_concerns_for_prompt(v) for k, v in value.items()}
    return value


def _gather_instrument_facts(
    db: Session, symbol: str, isin: str | None, instrument_type: str, fundamentals: dict[str, Any]
) -> dict[str, Any]:
    """Ground facts for the dossier prompt, branched on instrument_type.

    ADR 0014 §6: the prompt used to carry only symbol/ISIN/scores — no
    holdings, tracked index, inception date, sector, or factor mandate — so
    the LLM guessed style from a ticker and a trailing return (and guessed
    wrong: a Value ETF called "growth"). This gathers what's actually
    knowable and computes style deterministically; it never fabricates a
    placeholder, only omits what a best-effort lookup couldn't find.

    Best-effort throughout: any lookup failure degrades to an absent field,
    never raises — a data-source outage must not block dossier generation.
    """
    from app.decision.discover.style_classifier import classify_style
    from app.foundation.etf_lookup import get_etf_profile
    from app.foundation.providers.registry import build_provider_registry

    if instrument_type not in ("etf", "fund"):
        return {
            "instrument_type": instrument_type,
            "sector": fundamentals.get("sector"),
            "industry": fundamentals.get("industry"),
        }

    if isin:
        try:
            from app.foundation.asset_enrichment import upsert_asset_by_isin

            upsert_asset_by_isin(db, isin)
        except Exception as exc:
            logger.debug("_gather_instrument_facts: asset enrichment failed for %s: %s", isin, exc)

    holdings: list[dict[str, Any]] = []
    try:
        profile = get_etf_profile(db, symbol)
        if profile:
            holdings = profile.get("top_holdings") or []
    except Exception as exc:
        logger.debug("_gather_instrument_facts: get_etf_profile failed for %s: %s", symbol, exc)

    index_name: str | None = None
    inception_date: str | None = None
    try:
        info_result = build_provider_registry(db).get_etf_info(symbol)
        if info_result.get("ok"):
            info_data = info_result.get("data") or {}
            if isinstance(info_data, list) and info_data:
                info_data = info_data[0]
            if isinstance(info_data, dict):
                index_name = info_data.get("index") or info_data.get("tracked_index") or info_data.get("category")
                inception_date = info_data.get("inception_date") or info_data.get("fund_inception_date")
    except Exception as exc:
        logger.debug("_gather_instrument_facts: get_etf_info failed for %s: %s", symbol, exc)

    style = classify_style(instrument_type, holdings=holdings, index_name=index_name)

    return {
        "instrument_type": instrument_type,
        "top_holdings": holdings[:10],
        "tracked_index": index_name,
        "inception_date": inception_date,
        "style": style,
    }


_ANCHOR_KIND_TEXT: dict[str, str] = {
    "trailing_3y_annualized_return": "trailing 3-year annualized return",
    "momentum_12_1m": "12-1 month momentum (return from 12 months ago to 1 month ago)",
    "trailing_1m_annualized_return": "trailing 1-month annualized return",
    "building_blocks_credibility": "building-block estimate (dividend yield + trend earnings growth)",
    "bl_posterior": "Black-Litterman posterior",
}


def _pct(value: Any, digits: int = 1) -> str:
    return f"{float(value):+.{digits}f}%" if isinstance(value, (int, float)) else "n/a"


def _grounding_prompt_lines(scores: dict[str, Any], annual: dict[str, Any]) -> str:
    """Plain-language facts the thesis must not contradict (ADR 0017).

    BBVA.MC audit (2026-09-26): the prompt carried
    ``expected_return_anchor_pct=59.8`` without saying what kind of figure it
    was, so the model called the 3-year CAGR "12-1 month momentum" (which
    happened to be 60.9%). It cited a multi-factor regression loading (1.33)
    as the market beta, and it could not see which inputs drove the score.
    Each line here states one of those facts explicitly.
    """
    lines: list[str] = []
    mom = _as_dict(scores.get("momentum_quality"))
    components = _as_dict(annual.get("components"))
    kind = annual.get("anchor_kind")

    if kind in ("trailing_3y_annualized_return", "momentum_12_1m", "trailing_1m_annualized_return"):
        lines.append(
            f"Expected-return anchor: {_pct(components.get('anchor_pct'))}/yr is the "
            f"{_ANCHOR_KIND_TEXT[kind]} — backward-looking, not a forecast. Refer to it by "
            "exactly this name."
        )
        if kind != "momentum_12_1m" and isinstance(mom.get("momentum_12_1m"), (int, float)):
            lines.append(
                f"12-1 month momentum is a separate figure: {_pct(mom['momentum_12_1m'] * 100)}."
            )
        lines.append(
            "System forward estimate (annual, before scaling to horizon_months): "
            f"{_pct(components.get('er_annual_pct'))} = market-implied "
            f"{_pct(components.get('prior_pct'))} (cash {_pct(components.get('risk_free_pct'), 2)} + "
            f"adjusted beta {components.get('beta') if components.get('beta') is not None else 'n/a'} x "
            f"equity premium {_pct(components.get('equity_premium_pct'))}) + "
            f"{float(components.get('credibility_weight') or 0.0):.1%} of the anchor's gap to it. "
            "Critique this estimate, not the raw anchor."
        )
    elif kind in _ANCHOR_KIND_TEXT:
        lines.append(
            f"System forward estimate (annual): {_pct(components.get('er_annual_pct'))}, a "
            f"{_ANCHOR_KIND_TEXT[kind]}."
        )

    inputs = scores.get("composite_inputs")
    if isinstance(inputs, list) and inputs:
        drivers = "; ".join(
            f"{row.get('signal')} {float(row.get('score', 0.0)):.2f} x {float(row.get('weight', 0.0)):.2f}"
            f" = {float(row.get('contribution', 0.0)):.3f}"
            for row in inputs
            if isinstance(row, dict)
        )
        lines.append(f"Composite score drivers (signal score x applied weight = contribution): {drivers}.")

    quant = _as_dict(scores.get("quant_signals"))
    beta_weekly = quant.get("market_beta_weekly")
    beta_daily = quant.get("market_beta")
    benchmark = quant.get("benchmark") or "its benchmark"
    if isinstance(beta_weekly, (int, float)) or isinstance(beta_daily, (int, float)):
        beta_text = (
            f"{beta_weekly:.2f} (weekly returns vs {benchmark}, 2 years)"
            if isinstance(beta_weekly, (int, float))
            else f"{beta_daily:.2f} (daily returns vs {benchmark}, 1 year)"
        )
        lines.append(
            f"Market beta: {beta_text}. This is the only market beta; factor_betas are loadings "
            "from a multi-factor regression and must not be cited as market beta."
        )

    sf = _as_dict(scores.get("sentiment_fundamentals"))
    if sf.get("analyst_estimate_score") is not None:
        target_text = (
            f"median price target {sf['analyst_target']} ({_pct((sf.get('analyst_upside') or 0.0) * 100)} "
            "vs current price)"
            if sf.get("analyst_target") is not None
            else "no price target"
        )
        lines.append(
            f"Analyst consensus: {target_text}, rating {sf.get('analyst_recommendation') or 'n/a'}, "
            f"{sf.get('analyst_count') or 'unknown number of'} analysts."
        )
    elif "analyst_unavailable" in (sf.get("concerns") or []):
        lines.append("Analyst consensus: unavailable for this run — do not assume it is neutral or positive.")

    earnings_line = _earnings_text(sf)
    if earnings_line:
        lines.append(
            f"{earnings_line} The price usually moves several times a normal day's range on the report; "
            "name it as event risk in key_risks."
        )

    return "".join(f"{line}\n" for line in lines)


_EARNINGS_HOURS = {"bmo": "before the open", "amc": "after the close", "dmh": "during market hours"}


def _earnings_text(sf: dict[str, Any]) -> str | None:
    """The next-earnings sentence built from attach_earnings_calendar's fields, or None."""
    report_day = sf.get("next_earnings_date")
    days = sf.get("days_to_earnings")
    if not isinstance(report_day, str) or not isinstance(days, int) or days < 0:
        return None
    window_end = sf.get("earnings_date_to")
    when = f"{report_day} to {window_end} (not confirmed)" if isinstance(window_end, str) else report_day
    hour = _EARNINGS_HOURS.get(str(sf.get("earnings_hour")))
    return f"Next earnings report: {when}{', ' + hour if hour else ''}, {days} days from now."


def write_dossier(
    db: Session,
    candidate: dict[str, Any] | Any,
    profile: dict[str, Any],
    prompt_config: dict | None = None,
    skip_llm: bool = False,
    config_id: str | None = None,
    track_record_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate a recommendation dossier via a single-pass structured JSON LLM call.

    Args:
        db: Database session
        candidate: DiscoverCandidate as dict (symbol, isin, scores, tradeable)
        profile: User profile snapshot
        prompt_config: Optional dict with ``system_prompt``, ``temperature``,
            ``max_tokens``.  Falls back to the built-in hardcoded defaults.
        skip_llm: When True, go straight to the deterministic template
            (used by the orchestrator's circuit breaker after repeated
            LLM failures).
        config_id: The active ``signal_weights`` DiscoveryConfig.id for this
            run, recorded with the prediction.
        track_record_cache: Optional cache shared across a run's candidates,
            keyed by instrument_type (the track-record cohort), so a run
            queries the gate once per instrument type.

    Returns:
        Dict with dossier_id, recommendation_id, dossier, and generated_by
        ("llm" or "fallback") keys; fallback dossiers carry a
        ``fallback_reason``.
    """
    symbol = candidate["symbol"] if isinstance(candidate, dict) else getattr(candidate, "symbol", "")
    raw_scores = candidate.get("scores_json", "{}") if isinstance(candidate, dict) else getattr(candidate, "scores_json", "{}")
    if isinstance(raw_scores, str):
        try:
            scores = json.loads(raw_scores)
        except Exception:
            scores = {}
    elif isinstance(raw_scores, dict):
        scores = raw_scores
    else:
        scores = {}

    raw_tradeable = candidate.get("tradeable_json", "{}") if isinstance(candidate, dict) else getattr(candidate, "tradeable_json", "{}")
    if isinstance(raw_tradeable, str):
        try:
            tradeable = json.loads(raw_tradeable)
        except Exception:
            tradeable = {}
    elif isinstance(raw_tradeable, dict):
        tradeable = raw_tradeable
    else:
        tradeable = {}

    scores = _as_dict(scores)
    tradeable = _as_dict(tradeable)

    fundamentals = _fetch_fundamentals(symbol, db) or {}
    fundamentals = _as_dict(fundamentals)
    sentiment = _fetch_sentiment(symbol, db)
    sentiment = _as_dict(sentiment)

    composite_score = scores.get("composite", 0.0)
    alpha_miner_raw = _as_dict(scores.get("alpha_miner"))
    alpha_screener_raw = _as_dict(scores.get("alpha_screener"))
    portfolio_fit_raw = _as_dict(scores.get("portfolio_fit"))
    quant_signals_raw = _as_dict(scores.get("quant_signals"))
    backtest_raw = _as_dict(scores.get("backtest_vs_benchmark"))
    mom_raw = _as_dict(scores.get("momentum_quality"))
    sf_scores = _as_dict(scores.get("sentiment_fundamentals"))
    ml_scores = _as_dict(scores.get("ml_signal"))
    ml_regime_context = _as_dict(ml_scores.get("regime_context"))
    estimate_revision_scores = _as_dict(scores.get("estimate_revision_signal"))
    insider_scores = _as_dict(scores.get("insider_signal"))

    icir = alpha_miner_raw.get("icir")
    regime_state = alpha_screener_raw.get("regime_label")

    # Quant-derived point estimate the LLM's thesis must justify or critique,
    # rather than inventing its own expected_return from the qualitative
    # thesis alone (see select_return_anchor's docstring for why money-market
    # instruments prefer a short trailing window over the equity-shaped 3y
    # CAGR default — the XEON.DE incident). This is always a BACKWARD-looking
    # (realized) figure, not a forecast; the anchor_kind is surfaced to the
    # LLM prompt and the UI so it's never presented as a forward return for
    # the dossier's horizon_months without qualification.
    instrument_type = quant_signals_raw.get("instrument_type", "equity")

    # Track B1/B2: the cohort track-record gate. It withholds an "unproven"
    # cohort (Recommendation.approval_state) and is the only way to a BUY
    # verdict (candidate_gate.discover_verdict).
    if track_record_cache is not None and instrument_type in track_record_cache:
        track_record_gate = track_record_cache[instrument_type]
    else:
        track_record_gate = evaluate_track_record(db, instrument_type)
        if track_record_cache is not None:
            track_record_cache[instrument_type] = track_record_gate

    anchor = select_return_anchor(
        instrument_type,
        {
            "trailing_3y_annualized_return": backtest_raw.get("candidate_return_annual"),
            "trailing_1m_annualized_return": mom_raw.get("trailing_1m_annualized_return"),
            "momentum_12_1m": mom_raw.get("momentum_12_1m"),
        },
    )
    expected_return_anchor_pct = round(anchor["value"] * 100, 1) if anchor else None

    miner_scores = alpha_miner_raw

    # Mirror composite.py's derive_signals_from_scores availability checks
    # (docs/archive/audits/.../2026-08-28-discovery-pipeline-review.md Fix E): that
    # module already drops fundamentals/analyst/sentiment/ic_icir from the
    # weighted composite when there's no genuine underlying evidence, rather
    # than defaulting to a neutral 0.5/0.0 and weighting it in as if it were
    # real-but-uninformative. This dict must use the SAME availability tests
    # or the "unavailable" signal reappears here as a 0.50/0.00 placeholder
    # even though composite.py correctly ignored it.
    has_fundamentals_data = any(
        sf_scores.get(k) is not None
        for k in ("pe", "pb", "roe", "de_ratio", "market_cap", "revenue_growth")
    )
    has_sentiment_data = (sf_scores.get("social_mentions") or 0) > 0
    has_ic_data = ic_signal_usable(miner_scores)
    has_estimate_data = (
        estimate_revision_scores.get("sue") is not None
        or estimate_revision_scores.get("revision_momentum") is not None
    )
    has_insider_data = insider_scores.get("cluster_buy_score") is not None

    signal_breakdown = {
        "composite_score": composite_score,
        "regime_state": regime_state,
        "portfolio_context": portfolio_fit_raw.get("fit_score"),
        "expected_return_anchor_pct": expected_return_anchor_pct,
        # Persisted so a resolved DiscoveryPrediction row (features_json
        # mirrors this dict, see predictor.store_prediction) can later be
        # grouped by instrument_type for the per-class MZ-slope fitting path
        # (get_dynamic_shrinkage_factor, F1/F16 Workstream 7).
        "instrument_type": instrument_type,
    }
    recency_penalty = _as_dict(scores.get("recency_penalty"))
    if recency_penalty:
        # Transparency for the exponential-decay recency penalty (pipeline.py
        # _apply_recency_penalty): re-suggesting a ticker shortly after a
        # prior recommendation now costs composite score unless it's an ETF
        # or has improved since. Surfaced here rather than only
        # baked silently into composite_score so the dossier explains WHY a
        # familiar name scored lower than last time.
        signal_breakdown["recency_penalty_applied"] = recency_penalty.get("penalty_fraction", 0.0)
        signal_breakdown["days_since_last_recommended"] = recency_penalty.get("days_since_last")
        if recency_penalty.get("exempt_reason"):
            signal_breakdown["recency_penalty_exempt_reason"] = recency_penalty["exempt_reason"]
    if has_ic_data:
        signal_breakdown["factor_exposure"] = miner_scores.get("exposure_score")
        signal_breakdown["factor_exposures"] = miner_scores.get("factors")
        signal_breakdown["ic"] = miner_scores.get("ic")
        signal_breakdown["icir"] = icir
    if has_fundamentals_data:
        signal_breakdown["fundamentals"] = sf_scores.get("fundamentals_score")
    if sf_scores.get("analyst_estimate_score") is not None:
        # Same gate as composite.py: shown only when real consensus data
        # exists, never as a 0.50 placeholder.
        signal_breakdown["analyst_consensus"] = sf_scores.get("analyst_estimate_score")
    if quant_signals_raw.get("market_beta_weekly") is not None:
        signal_breakdown["market_beta_weekly"] = quant_signals_raw.get("market_beta_weekly")
    if has_sentiment_data:
        signal_breakdown["sentiment"] = sf_scores.get("sentiment_score")
    if ml_scores.get("prediction") is not None:
        signal_breakdown["ml_signal"] = ml_scores.get("prediction")
    if has_estimate_data:
        signal_breakdown["estimate_sue"] = estimate_revision_scores.get("sue")
        signal_breakdown["estimate_revision_momentum"] = estimate_revision_scores.get("revision_momentum")
        signal_breakdown["estimate_dispersion"] = estimate_revision_scores.get("dispersion")
    if has_insider_data:
        signal_breakdown["insider_cluster_buy_score"] = insider_scores.get("cluster_buy_score")
        signal_breakdown["insider_net_flow_usd"] = insider_scores.get("net_insider_flow_usd")

    annual_estimate = _resolve_annual_estimate(
        db,
        profile.get("user_id", "unknown"),
        instrument_type,
        scores,
        symbol,
        signal_breakdown=signal_breakdown,
    )
    if annual_estimate.get("anchor_kind"):
        signal_breakdown["expected_return_anchor_kind"] = annual_estimate["anchor_kind"]

    evaluated_signals = {k: v for k, v in signal_breakdown.items() if v is not None}
    grounding_lines = _grounding_prompt_lines(scores, annual_estimate)

    candidate_isin = candidate.get("isin") if isinstance(candidate, dict) else getattr(candidate, "isin", None)
    instrument_facts = _gather_instrument_facts(db, symbol, candidate_isin, instrument_type, fundamentals)

    instrument_facts_lines = ""
    if instrument_type in ("etf", "fund"):
        style = instrument_facts.get("style") or {}
        style_text = style.get("style") or "not established (insufficient evidence — do not guess)"
        instrument_facts_lines = (
            f"Holdings (top 10): {json.dumps(instrument_facts.get('top_holdings') or [])}\n"
            f"Tracked index: {instrument_facts.get('tracked_index') or 'N/A'}\n"
            f"Inception date: {instrument_facts.get('inception_date') or 'N/A'}\n"
            f"Style (established fact from holdings/mandate — do not re-derive or contradict): {style_text}\n"
        )
    else:
        instrument_facts_lines = (
            f"Sector: {instrument_facts.get('sector') or 'N/A'}\n"
            f"Industry: {instrument_facts.get('industry') or 'N/A'}\n"
        )

    system_prompt: str
    if prompt_config and "system_prompt" in prompt_config:
        system_prompt = prompt_config["system_prompt"]
    else:
        system_prompt = DEFAULT_PROMPT_TEMPLATE["system_prompt"]

    context = (
        f"Candidate: {symbol} ({candidate.get('name') or 'Unknown'})\n"
        f"ISIN: {candidate.get('isin') or 'N/A'}\n"
        f"Source: {candidate.get('source')}\n"
        f"Composite score: {composite_score}\n"
        f"ICIR: {icir}\n"
        f"Regime state: {regime_state}\n"
        f"ML signal regime context: {json.dumps(ml_regime_context)}\n"
        f"Portfolio context: {json.dumps(portfolio_fit_raw)}\n"
        f"Quantitative scores: {json.dumps(_reword_concerns_for_prompt(scores))}\n"
        f"{instrument_facts_lines}"
        f"Fundamentals: {json.dumps(fundamentals)}\n"
        f"Sentiment: {json.dumps(sentiment)}\n"
        f"Tradeability: {json.dumps(tradeable)}\n"
        f"Signal breakdown (evaluated signals): {json.dumps(evaluated_signals)}\n"
        f"{grounding_lines}"
        f"User profile: {json.dumps(profile)}\n"
        "\nGenerate a research hypothesis as structured JSON matching the schema."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    final_dossier: dict[str, Any] | None = None
    generated_by = "llm"
    fallback_reason: str | None = None

    if skip_llm:
        generated_by = "fallback"
        fallback_reason = "llm_skipped: circuit breaker open after repeated LLM failures"
        final_dossier = deterministic_template(
            candidate, scores, signal_breakdown, db, profile.get("user_id"), annual=annual_estimate,
        )
    else:
        base_messages = list(messages)
        attempt_messages = list(messages)
        last_error: str | None = None
        telemetry: dict[str, Any] = {}
        for attempt in range(_MAX_JSON_RETRIES + 1):
            telemetry.clear()
            try:
                raw = _local_llm_sync(
                    db, attempt_messages, timeout_s=120.0,
                    json_schema=DOSSIER_JSON_SCHEMA,
                    sampling=STRUCTURED_JSON_SAMPLING,
                    max_tokens=_COMPLETION_TOKENS,
                    telemetry=telemetry,
                )
            except Exception as exc:
                logger.warning("LLM call failed for %s: %s", symbol, exc)
                last_error = f"llm_error: {exc}"
                break

            parsed = _try_parse_json(raw)
            if parsed is not None:
                try:
                    validated = DossierOutputModel.model_validate(_coerce_dossier(parsed))
                    final_dossier = validated.model_dump()
                    break
                except ValidationError as exc:
                    logger.warning("Dossier validation failed for %s: %s", symbol, exc)
                    last_error = "llm_invalid: response failed schema validation"
            else:
                reason = unusable_reason(raw, telemetry)
                logger.warning("LLM returned unparseable JSON for %s: %s", symbol, reason)
                last_error = f"llm_unparseable: {reason}"

            if attempt < _MAX_JSON_RETRIES:
                # Rebuilt from the base, carrying only an excerpt of the
                # rejected completion — appending each attempt to a growing
                # transcript is how one runaway answer makes every remaining
                # retry fail too, since llama-server runs with
                # ``--no-context-shift``.
                instruction = (
                    f"That response was unusable: {last_error}. Reply with exactly "
                    "one JSON object, no markdown, no text outside the object. Keep "
                    f"thesis under {_MAX_THESIS_CHARS} characters so the object "
                    "closes inside the token budget."
                )
                attempt_messages = build_retry_messages(
                    base_messages, raw, instruction, excerpt_chars=_RETRY_EXCERPT_CHARS
                )

        if final_dossier is None:
            generated_by = "fallback"
            fallback_reason = last_error or "llm_invalid: response failed schema validation"
            final_dossier = deterministic_template(
            candidate, scores, signal_breakdown, db, profile.get("user_id"), annual=annual_estimate,
        )

    assert final_dossier is not None
    # expected_return is always computed here, from the quant anchor — never
    # trusted from the LLM (which no longer produces it at all; its schema
    # dropped the field, see DOSSIER_JSON_SCHEMA above). Both the LLM and
    # deterministic-fallback paths resolve it through the single dispatcher
    # below, so there is exactly one place expected_return (and its PRIIPs
    # uncertainty band) is derived.
    resolved = _resolve_expected_return(
        db,
        profile.get("user_id", "unknown"),
        instrument_type,
        scores,
        final_dossier.get("horizon_months", 6),
        symbol,
        signal_breakdown=signal_breakdown,
        annual=annual_estimate,
    )
    final_dossier["expected_return"] = resolved["expected_return_pct"]
    final_dossier["expected_return_components"] = resolved["components"]
    # Every composite input with its applied weight and contribution
    # (pipeline.run_candidate_pipeline), so the dossier shows what actually
    # drove the score rather than a hand-picked subset of inputs.
    composite_inputs = scores.get("composite_inputs")
    final_dossier["composite_breakdown"] = composite_inputs if isinstance(composite_inputs, list) else None
    final_dossier["signal_breakdown"] = signal_breakdown
    # Non-directional macro/volatility regime context (authoritative substitute
    # signal for passive ETFs whose directional ML model is unavailable by design).
    final_dossier["regime_context"] = ml_regime_context or None
    final_dossier["expected_return_anchor_kind"] = resolved["anchor_kind"]
    if resolved["band"] is not None:
        final_dossier["expected_return_band"] = resolved["band"]
    # Provenance: the UI must be able to distinguish a genuine LLM thesis
    # from the deterministic quant template.
    final_dossier["generated_by"] = generated_by
    if fallback_reason:
        final_dossier["fallback_reason"] = fallback_reason

    # Pipeline composite is the canonical conviction source — overwrite the LLM
    # value so dossier_json and the RecommendationDossier column are always in sync.
    scores_val = scores.get("composite", 0.0)
    conviction = float(scores_val) if isinstance(scores_val, (int, float)) else 0.0
    final_dossier["conviction"] = conviction

    final_dossier = _ascii_safe_dict(final_dossier)

    # ------------------------------------------------------------------
    # Persist RecommendationDossier + Recommendation
    # ------------------------------------------------------------------
    dossier_id = str(__import__("uuid").uuid4())
    rec_id = str(__import__("uuid").uuid4())

    try:
        profile_user_id = profile.get("user_id")
        dossier_row = RecommendationDossier(
            id=dossier_id,
            user_id=profile_user_id if profile_user_id and profile_user_id != "unknown" else None,
            conviction=conviction,
            dossier_json=json.dumps(final_dossier),
            attribution_run_id=None,
            mc_run_id=None,
            goal_ids=None,
            aspect_ids=None,
        )
        db.add(dossier_row)

        rec_row = Recommendation(
            id=rec_id,
            user_id=profile.get("user_id", "unknown"),
            ticker=symbol,
            # Evidence, not a score level: see candidate_gate.discover_verdict.
            verdict=discover_verdict(track_record_gate),
            confidence=__import__("decimal").Decimal(str(min(conviction, 1.0))),
            payload_json=json.dumps({
                "dossier_id": dossier_id,
                "candidate": candidate,
                "track_record_gate": track_record_gate,
            }),
            backtest_json="{}",
            mode="discover",
            data_quality_score=__import__("decimal").Decimal(str(_as_dict(scores.get("history_ingest")).get("coverage", 0.0))),
            risk_score=__import__("decimal").Decimal(str(_as_dict(scores.get("momentum_quality")).get("volatility_6m", 0.5))),
            portfolio_fit_score=__import__("decimal").Decimal(str(_as_dict(scores.get("portfolio_fit")).get("fit_score", 0.0))),
            # Track B2: auto-withhold. "unproven" (enough resolved history to
            # judge, and it doesn't show a significant edge) is the only
            # status that withholds — it stays "draft" instead of the default
            # "approved_candidate" fast path every other status takes,
            # including cold-start ("insufficient_data"), which fails open.
            approval_state=(
                "draft" if _as_dict(track_record_gate).get("status") == "unproven" else "approved_candidate"
            ),
        )
        db.add(rec_row)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "dossier_id": dossier_id,
        "recommendation_id": rec_id,
        "dossier": final_dossier,
        "generated_by": generated_by,
        "fallback_reason": fallback_reason,
        # The horizon-free estimate, so the prediction ledger can restate it
        # over its own horizon (see expected_return_over_trading_days).
        "annual_estimate": {
            k: annual_estimate.get(k) for k in ("prior_annual", "tilt_annual", "anchor_kind")
        },
    }


# Per-instrument_type shrinkage constants (F1/F16). A money-market anchor is
# a short-term policy rate (ESTR/EONIA-pegged) that persists near-
# mechanically month to month, so a shallow shrink preserves most of the
# signal; equity/etf/bond trailing returns exhibit mean reversion (a large
# trailing CAGR is a rally unlikely to repeat at the same pace) and momentum
# decay over a 6-12mo horizon (Moskowitz, Ooi & Pedersen, JFE 2012), so
# they're shrunk much harder. Hardcoded now per user decision;
# MZ_MIN_ROWS_BY_TYPE gates a dynamic per-class fit
# (get_dynamic_shrinkage_factor) that supersedes this once enough resolved
# data with expected_return exists for that class.
#
# Since ADR 0017 these are the CAP on the weight given to the anchor's gap
# to the market-implied prior, not a shrink toward zero. For non-cash
# classes the weight actually applied is usually the smaller,
# volatility-aware expected_return.credibility_weight.
SHRINKAGE_BY_INSTRUMENT_TYPE: dict[str, float] = {
    "money_market": 0.90,
    "equity": 0.10,
    "etf": 0.10,
    "bond": 0.10,
}
DEFAULT_SHRINKAGE = 0.10

# Minimum resolved-with-expected_return rows per instrument_type before the
# dynamic MZ-slope fit takes over from the hardcoded constant above. The
# cash-attractor audit's simulation found the standard error unacceptably
# wide even at n=22 pooled across all types — a per-class fit needs its own
# bar, kept equal to the pooled calibrator's min_rows=20 only until real
# per-class volume data justifies raising it independently per class.
MZ_MIN_ROWS_BY_TYPE: dict[str, int] = {
    "money_market": 20,
    "equity": 20,
    "etf": 20,
    "bond": 20,
}


def get_dynamic_shrinkage_factor(db: Session, user_id: str, instrument_type: str) -> float:
    """Per-instrument_type shrinkage factor: a dynamic MZ-slope fit against
    resolved predictions when enough data exists, else the hardcoded
    :data:`SHRINKAGE_BY_INSTRUMENT_TYPE` constant.

    Dormant by design (per user decision "hardcode now AND build the fitting
    path"): only ~1 of 22 resolved ``DiscoveryPrediction`` rows had
    ``expected_return`` set as of the cash-attractor audit (see Workstream 1's
    F3 fix, which is what makes this column start filling in going forward),
    so this essentially always returns the hardcoded fallback today. It
    activates automatically, with no further wiring needed, once a class
    accumulates ``MZ_MIN_ROWS_BY_TYPE`` resolved rows with ``expected_return``
    set — reuses :func:`app.foundation.quant_metrics.compute_mincer_zarnowitz`,
    the correct OLS regression (unlike ``discover/scoring.py:_compute_mincer``,
    which is Platt scaling on sign only, see F14).
    """
    from datetime import UTC, datetime, timedelta

    from app.foundation.models.entities import DiscoveryPrediction
    from app.foundation.quant_metrics import compute_mincer_zarnowitz

    fallback = SHRINKAGE_BY_INSTRUMENT_TYPE.get(instrument_type, DEFAULT_SHRINKAGE)
    min_rows = MZ_MIN_ROWS_BY_TYPE.get(instrument_type, 20)

    cutoff = datetime.now(UTC) - timedelta(days=365)
    rows = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.outcome_status == "resolved",
            DiscoveryPrediction.predicted_at >= cutoff,
            DiscoveryPrediction.expected_return.isnot(None),
            DiscoveryPrediction.realised_return.isnot(None),
        )
        .all()
    )
    # Only Discover's own rows: the advisor cycle writes to the same ledger,
    # but its expected_return is a Monte Carlo p50, a different estimator
    # whose calibration says nothing about this one. Discover rows carry
    # the type under signal_breakdown.quant_signals, not at the top level
    # this used to read, so no row ever matched and the fit never activated.
    matching = [
        r for r in rows
        if isinstance(_as_dict(_as_dict(r.features_json).get("signal_breakdown")).get("quant_signals"), dict)
        and _resolve_cohort_instrument_type(r.features_json) == instrument_type
    ]
    if len(matching) < min_rows:
        return fallback

    pairs = [
        (float(r.expected_return), float(r.realised_return))
        for r in matching
        if r.expected_return is not None and r.realised_return is not None
    ]
    slope, _r2 = compute_mincer_zarnowitz(pairs)
    if slope is None:
        return fallback
    # A fitted slope outside [0, 1] would mean an inverted or amplifying
    # relationship — clamp to preserve "shrink, never amplify" even from a
    # noisy dynamic fit. (The slope is fitted on final estimates, which since
    # ADR 0017 include the market-implied prior; using it as the cap on the
    # tilt is an approximation until enough outcomes resolve to fit the
    # tilt separately.)
    return max(0.0, min(1.0, slope))


def _dampen_trailing_return_for_forward_estimate(
    anchor_pct: float | None,
    horizon_months: int,
    anchor_kind: str | None = None,
    shrinkage_factor: float = DEFAULT_SHRINKAGE,
) -> float | None:
    """Convert a backward-looking trailing return into a conservative forward estimate.

    ``anchor_pct`` (3y annualized CAGR, or 12-1m momentum as fallback) is
    realized historical performance, not a forecast — echoing it verbatim
    produced dossiers claiming e.g. a +60% forward return on a 6-month
    horizon for a stock whose actual analyst consensus implied ~10% upside
    (BBVA.MC prod audit, Aug 2026). Shrink toward zero and scale to the
    horizon so the fallback template never restates a multi-year rally as a
    near-term forecast.

    ``shrinkage_factor`` replaces the old flat ``0.3`` constant — callers
    resolve it per instrument_type (:data:`SHRINKAGE_BY_INSTRUMENT_TYPE` or
    :func:`get_dynamic_shrinkage_factor`) since a money-market anchor and an
    equity anchor carry very different amounts of forward-looking signal.

    The two trailing-return anchors are already annualized rates, so scaling
    by ``horizon_months / 12`` is coherent — a longer horizon compounds more
    of the same rate. ``momentum_12_1m`` is different: Jegadeesh-Titman 12-1
    momentum predicts the *next one month*, not a rate that keeps compounding
    (Moskowitz, Ooi & Pedersen, JFE 2012, find the effect decays over
    roughly a year and can partially reverse beyond that). Scaling it up
    with a longer stated horizon get the sign of the horizon adjustment
    backwards, so its effective horizon is capped at one month regardless of
    the dossier's stated horizon (docs/archive/plans/momentum-anchor-horizon-issue.md).
    """
    if anchor_pct is None:
        return None
    if anchor_kind == "momentum_12_1m":
        horizon_fraction = 1.0 / 12.0
    else:
        horizon_fraction = max(horizon_months, 1) / 12.0
    dampened = anchor_pct * shrinkage_factor * horizon_fraction
    return round(max(-25.0, min(25.0, dampened)), 1)


# Registered values of the ``er_mode`` public-settings rollback key (M5 ladder).
# "blocks"/"bl" are FUTURE modes (Options 1/2) — registered so flipping the key
# rolls forward/back without a deploy; unknown or missing values normalize to
# "trailing".
_ER_MODES = ("trailing", "blocks", "bl")


def _try_building_block_er(
    db: Session, symbol: str, instrument_type: str, scores: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch building-block inputs and run the pure estimator; None on any failure.

    CAPE inputs are None until a macro feed exists (term degrades to 0.0 with a
    ``cape_unavailable`` concern). The money-market current yield uses the
    trailing_1m annualized reading as a documented proxy — the same
    short-window policy-rate signal the XEON.DE analysis validated
    (expected_return.py docstring).
    """
    try:
        from app.foundation.market import fundamentals as market_fundamentals

        data = (market_fundamentals(db, symbol) or {}).get("data") or {}
        result = building_block_er(
            instrument_type,
            data,
            cape_ratio=None,
            market_avg_cape=None,
            current_yield_annual=(scores.get("momentum_quality") or {}).get(
                "trailing_1m_annualized_return"
            ),
        )
        logger.debug("building-block ER for %s: %s", symbol, result)
        return result
    except Exception as exc:
        logger.warning("building-block ER failed for %s: %s", symbol, exc)
        return None


def _llm_view(db: Session, symbol: str) -> tuple[float, float] | None:
    """Sync seam over blm.multi_query_confidence: (mean view, variance) or None.

    None means "no usable view" — either no LLM endpoint configured, the query
    machinery failed, or every query failed. blm.multi_query_confidence itself
    returns None on all-queries-failed (ADR 0014 §5 — it used to return the
    sentinel ``(0.0, 1.0)``, indistinguishable from a genuine near-zero,
    high-variance forecast), so this just passes that through. Tests
    monkeypatch this function directly.
    """
    endpoint = resolve_llm_base_url(db)
    if not endpoint:
        return None
    try:
        result = asyncio.run(
            blm.multi_query_confidence(symbol, llm_endpoint=endpoint, llm_model=resolve_llm_model(db))
        )
    except Exception as exc:
        logger.warning("BLM view query failed for %s: %s", symbol, exc)
        return None
    if result is None:
        logger.info("no usable BLM view for %s (all queries failed)", symbol)
        return None
    return result


def _try_bl_er(
    db: Session, symbol: str, instrument_type: str, scores: dict[str, Any]
) -> dict[str, Any] | None:
    """Black-Litterman posterior annual ER for the candidate; None on any gap.

    Scope-locked 2-asset universe [candidate, region benchmark] with equal
    w_mkt (documented approximation absent market-cap data); Σ from the joint
    empirical covariance of both series (annualized, quant_optim pattern);
    one absolute view on the candidate: Q = clamp(mean, ±1)·0.25 (conservative
    mapping to the dossier's ±25pp display scale), confidence = 1 − variance.
    Any failure degrades the caller to the trailing anchor — an LLM outage must
    never break dossier generation.
    """
    try:
        view = _llm_view(db, symbol)
        if view is None:
            return None
        mean_view, variance = view

        from app.decision.discover.pipeline import _pick_benchmark

        benchmark = _pick_benchmark(symbol, instrument_type == "etf")
        days = 365 * 3 + 30

        import numpy as np
        import pandas as pd

        def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
            df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
            return df.assign(date=pd.to_datetime(df["date"]))

        cand_df = _frame(market_history_rows(db, symbol, days))
        bench_df = _frame(market_history_rows(db, benchmark, days))
        if cand_df.empty or bench_df.empty:
            return None
        merged = pd.merge(
            cand_df[["date", "close"]],
            bench_df[["date", "close"]],
            on="date",
            suffixes=("_c", "_b"),
        )
        if len(merged) < 250:
            return None
        # filter, not [["close_c","close_b"]]: pandas-stubs types list-getitem
        # as Series|DataFrame → Series.cov(other) looks required-arg. Columns
        # guaranteed by merge suffixes.
        rets = merged.filter(items=["close_c", "close_b"]).pct_change().dropna()
        sigma = rets.cov().values * 252.0

        w_mkt = np.array([0.5, 0.5])
        pi = blm.compute_equilibrium_returns(sigma, w_mkt)
        confidence = max(0.0, min(1.0, 1.0 - min(variance, 1.0)))
        q_annual = max(-1.0, min(1.0, mean_view)) * 0.25
        mu_posterior, _post_cov = blm.blm_posterior_proportional(
            pi, sigma, {0: q_annual}, {0: confidence}
        )
        return {"er_annual": float(mu_posterior[0]), "benchmark": benchmark}
    except Exception as exc:
        logger.warning("BL anchor failed for %s: %s", symbol, exc)
        return None


def market_history_rows(db: Session, symbol: str, days: int) -> list[dict[str, Any]]:
    """Cache-only price history rows (no synchronous provider fetch).

    Thin indirection so tests can stub price access without patching
    app.foundation.market internals; mirrors return_band.py's allow_live=False
    rationale for multi-symbol request paths.
    """
    from app.foundation.market import history as market_history

    return market_history(db, symbol, days=days, allow_live=False)


def _no_estimate(anchor_kind: str | None = None) -> dict[str, Any]:
    return {
        "anchor_kind": anchor_kind,
        "prior_annual": None,
        "tilt_annual": None,
        "band_mode": None,
        "components": None,
    }


def _resolve_annual_estimate(
    db: Session | None,
    user_id: str | None,
    instrument_type: str,
    scores: dict[str, Any],
    symbol: str,
    *,
    signal_breakdown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Horizon-independent half of the expected-return dispatcher.

    Resolved once per dossier, before the LLM call, so the prompt can state
    the anchor's kind and the system's forward estimate (the thesis used to
    call a 3-year CAGR "12-1 month momentum" because the prompt never said
    which it was — BBVA.MC audit, 2026-09-26). :func:`_resolve_expected_return`
    turns it into the horizon-specific point estimate and band.

    Returns ``{"anchor_kind", "prior_annual", "tilt_annual", "band_mode",
    "components"}``; the annual forward estimate is ``prior_annual +
    tilt_annual``. ``prior_annual`` is None when no anchor exists (no data is
    not a flat return).

    trailing mode (ADR 0017): ``prior`` is the market-implied return
    (:func:`equilibrium_prior`: cash rate + Blume-adjusted weekly beta x
    equity premium). The tilt is a :func:`credibility_weight` share of the
    anchor's gap to that prior, capped by the per-class shrinkage (or the
    fitted MZ slope). Trailing returns used to be shrunk toward 0%, which put
    low-return LONG picks below the cash rate. blocks/bl modes are already
    forward estimates: the whole value is the "prior", with no tilt.
    """
    er_mode = "trailing"
    if db is not None:
        raw_mode = get_public_settings(db).get("er_mode")
        if raw_mode in _ER_MODES:
            er_mode = raw_mode
    if er_mode != "trailing":
        logger.debug("er_mode=%s active for %s", er_mode, symbol)

    blocks = None
    if er_mode == "blocks" and db is not None:
        blocks = _try_building_block_er(db, symbol, instrument_type, scores)

    bl_result = None
    if blocks is None and er_mode == "bl" and db is not None:
        bl_result = _try_bl_er(db, symbol, instrument_type, scores)

    for kind, result in (("bl_posterior", bl_result), ("building_blocks_credibility", blocks)):
        if db is not None and result is not None and result.get("er_annual") is not None:
            er_annual = float(result["er_annual"])
            return {
                "anchor_kind": kind,
                "prior_annual": er_annual,
                "tilt_annual": 0.0,
                "band_mode": "annual",
                "components": {"er_annual_pct": round(er_annual * 100.0, 2)},
            }
    # blocks/bl failure (missing data, no usable LLM view) degrades to the
    # trailing path below rather than emitting an empty estimate.

    bt = _as_dict(scores.get("backtest_vs_benchmark"))
    mom = _as_dict(scores.get("momentum_quality"))
    anchor = select_return_anchor(
        instrument_type,
        {
            "trailing_3y_annualized_return": bt.get("candidate_return_annual"),
            "trailing_1m_annualized_return": mom.get("trailing_1m_annualized_return"),
            "momentum_12_1m": mom.get("momentum_12_1m"),
        },
    )
    if anchor is not None:
        anchor_value = float(anchor["value"])
        anchor_kind: str | None = anchor["method"]
    elif signal_breakdown is not None and signal_breakdown.get("expected_return_anchor_pct") is not None:
        anchor_value = float(signal_breakdown["expected_return_anchor_pct"]) / 100.0
        anchor_kind = signal_breakdown.get("expected_return_anchor_kind")
    else:
        return _no_estimate()

    if db is not None and user_id is not None:
        cap = get_dynamic_shrinkage_factor(db, user_id, instrument_type)
    else:
        cap = SHRINKAGE_BY_INSTRUMENT_TYPE.get(instrument_type, DEFAULT_SHRINKAGE)

    risk_free = get_risk_free_rate(db) if db is not None else FALLBACK_RISK_FREE_RATE
    quant = _as_dict(scores.get("quant_signals"))
    beta_raw, beta_source = None, None
    for key, source in (("market_beta_weekly", "weekly"), ("market_beta", "daily")):
        value = quant.get(key)
        if isinstance(value, (int, float)):
            beta_raw, beta_source = float(value), source
            break
    prior, beta_used = equilibrium_prior(instrument_type, risk_free, beta_raw)

    if instrument_type == "money_market":
        # A money-market anchor is the policy rate itself; its persistence
        # is mechanical, not a noisy mean estimate, so the class constant
        # applies directly.
        weight = cap
    elif anchor_kind == "momentum_12_1m":
        weight = credibility_weight(mom.get("volatility_6m"), 11.0 / 12.0, cap)
    elif anchor_kind == "trailing_3y_annualized_return":
        weight = credibility_weight(
            bt.get("candidate_volatility_annual"), bt.get("window_years") or 3.0, cap,
        )
    else:
        weight = cap
    tilt = weight * (anchor_value - prior)

    return {
        "anchor_kind": anchor_kind,
        "prior_annual": prior,
        "tilt_annual": tilt,
        "band_mode": "total",
        "components": {
            "anchor_pct": round(anchor_value * 100.0, 2),
            "risk_free_pct": round(risk_free * 100.0, 2),
            "beta": round(beta_used, 3) if beta_used is not None else None,
            "beta_raw": round(beta_raw, 3) if beta_raw is not None else None,
            "beta_source": beta_source,
            "equity_premium_pct": round(EQUITY_PREMIUM_OVER_CASH * 100.0, 2),
            "prior_pct": round(prior * 100.0, 2),
            "credibility_weight": round(weight, 4),
            "tilt_pct": round(tilt * 100.0, 2),
            "er_annual_pct": round((prior + tilt) * 100.0, 2),
        },
    }


def _expected_return_for_horizon(annual: dict[str, Any], horizon_months: int) -> float | None:
    """Percent point estimate over *horizon_months*, clamped to ±25, 1dp.

    Linear in the horizon, like the pre-ADR-0017 dampening (the
    characterization tests pin that). The prior compounds over the stated
    horizon; a momentum_12_1m tilt is capped at one month regardless of it
    (docs/archive/plans/momentum-anchor-horizon-issue.md).
    """
    prior = annual.get("prior_annual")
    tilt = annual.get("tilt_annual")
    if prior is None or tilt is None:
        return None
    horizon_fraction = max(horizon_months, 1) / 12.0
    tilt_fraction = 1.0 / 12.0 if annual.get("anchor_kind") == "momentum_12_1m" else horizon_fraction
    pct = 100.0 * (prior * horizon_fraction + tilt * tilt_fraction)
    return round(max(-25.0, min(25.0, pct)), 1)


def expected_return_over_trading_days(annual: dict[str, Any] | None, trading_days: int) -> float | None:
    """The annual estimate restated as a fraction over *trading_days*.

    This is the prediction ledger's unit: ``DiscoveryPrediction.realised_return``
    is the fraction the price moved over the ledger horizon (21 trading
    days by default), and the Mincer-Zarnowitz fit in
    :func:`get_dynamic_shrinkage_factor` regresses one on the other. The
    dossier's own ``expected_return`` is a percent over the LLM-chosen
    ``horizon_months`` (1-12), so it cannot go into the ledger as is.
    Scaling is linear and the momentum tilt is capped at one month, as in
    :func:`_expected_return_for_horizon`.
    """
    if not annual:
        return None
    prior = annual.get("prior_annual")
    tilt = annual.get("tilt_annual")
    if prior is None or tilt is None:
        return None
    years = max(trading_days, 1) / 252.0
    tilt_years = min(years, 1.0 / 12.0) if annual.get("anchor_kind") == "momentum_12_1m" else years
    return float(prior) * years + float(tilt) * tilt_years


def ledger_return_range(
    db: Session | None,
    symbol: str,
    expected_return: float | None,
    trading_days: int,
) -> tuple[float | None, float | None]:
    """P10/P90 of the total return over the ledger horizon, or ``(None, None)``.

    The dossier already prints a PRIIPs-style block-bootstrap band
    (:func:`compute_return_band`) over its own ``horizon_months``; the ledger
    needs the same band over *trading_days*, centred on the ledger's own point
    estimate (:func:`expected_return_over_trading_days`), so the range can later
    be scored for coverage ("7 of 10 outcomes inside the 80 % range").

    Only the block-bootstrap band is stored. The short-history GBM fallback is
    not centred on the estimate, so it would not be the range the estimate was
    stated with; such rows keep NULL and simply do not enter the coverage score.
    """
    if db is None or expected_return is None or expected_return <= -1.0:
        return None, None
    band = compute_return_band(
        db,
        symbol,
        1,
        0.0,
        horizon_steps=trading_days,
        target_total=float(expected_return),
    )
    if not band or band.get("method") != "block_bootstrap":
        return None, None
    return float(band["p10"]), float(band["p90"])


def _resolve_expected_return(
    db: Session | None,
    user_id: str | None,
    instrument_type: str,
    scores: dict[str, Any],
    horizon_months: int,
    symbol: str,
    *,
    signal_breakdown: dict[str, Any] | None = None,
    annual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single code path for the dossier expected-return point estimate + band.

    Both call sites (write_dossier's post-LLM overwrite and the
    deterministic_template fallback) MUST route through here so the LLM and
    fallback paths stay provably identical (M5 Option 0, discover-maths audit
    A8/F14). *annual* is the already-resolved :func:`_resolve_annual_estimate`
    result (write_dossier resolves it before the LLM call); when omitted it is
    resolved here.

    ``signal_breakdown`` carries the alternative anchor source for direct
    deterministic_template calls whose ``scores`` lack anchor candidates
    (characterization-pinned contract).

    Returns ``{"expected_return_pct": float|None, "anchor_kind": str|None,
    "band": dict|None, "components": dict|None}``; ``band`` is None whenever
    there is no db session, no anchor, or band computation failed open.
    """
    if annual is None:
        annual = _resolve_annual_estimate(
            db, user_id, instrument_type, scores, symbol, signal_breakdown=signal_breakdown,
        )
    expected_return_pct = _expected_return_for_horizon(annual, horizon_months)

    band = None
    if db is not None and expected_return_pct is not None:
        if annual.get("band_mode") == "annual":
            # blocks/bl: the estimate is already a forward annual rate.
            band = compute_return_band(db, symbol, horizon_months, float(annual["prior_annual"]))
        else:
            # O1 (discover-maths fix session): center the band on the point
            # estimate, not the raw backward-looking anchor. Invert the band's
            # geometric horizon conversion: find the annual rate whose h-month
            # TOTAL equals the estimate, so compute_return_band's
            # ``(1+anchor)^(h/12)-1`` recentering lands the median on it.
            total = expected_return_pct / 100.0
            assert total > -1.0  # the ±25pp clamp guarantees this
            h = max(horizon_months, 1)
            anchor_equiv = (1.0 + total) ** (12.0 / h) - 1.0
            band = compute_return_band(db, symbol, horizon_months, anchor_equiv)

    return {
        "expected_return_pct": expected_return_pct,
        "anchor_kind": annual.get("anchor_kind"),
        "band": band,
        "components": annual.get("components"),
    }


def deterministic_template(
    candidate: dict[str, Any],
    scores: dict[str, Any],
    signal_breakdown: dict[str, Any],
    db: Session | None = None,
    user_id: str | None = None,
    annual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a §5 dossier without LLM when LLM is unavailable or max retries exceeded.

    ``db``/``user_id`` are optional so this stays callable without a session
    (e.g. in tests); when omitted, the hardcoded
    :data:`SHRINKAGE_BY_INSTRUMENT_TYPE` constant is used directly instead of
    attempting the dynamic per-class fit.
    """
    composite = scores.get("composite", 0.0)
    symbol = candidate["symbol"]
    horizon_months = 6
    instrument_type = signal_breakdown.get("instrument_type", "equity")
    resolved = _resolve_expected_return(
        db,
        user_id,
        instrument_type,
        scores,
        horizon_months,
        symbol,
        signal_breakdown=signal_breakdown,
        annual=annual,
    )
    key_risks = [
        "Market risk: broad equity drawdowns affect the position.",
        "Currency risk: non-EUR denominated assets carry FX exposure.",
        "Concentration risk: single-name exposure may exceed diversification targets.",
    ]
    # Ticker-matched, not gvkey-verified (see pit_panel_joins module
    # docstring) -- the fallback template is the one place the LLM isn't
    # around to weigh that caveat itself, so it's spelled out inline.
    cluster_score = signal_breakdown.get("insider_cluster_buy_score")
    if cluster_score:
        key_risks.append(
            f"Insider activity: {int(cluster_score)} distinct insider(s) opportunistically bought in "
            "the trailing 30 days (SEC filings, best-effort ticker match)."
        )
    earnings_line = _earnings_text(_as_dict(scores.get("sentiment_fundamentals")))
    if earnings_line:
        key_risks.insert(0, f"Earnings event: {earnings_line}")
    sue = signal_breakdown.get("estimate_sue")
    if sue is not None and abs(sue) >= 1.0:
        direction = "beat" if sue > 0 else "missed"
        key_risks.append(
            f"Analyst estimates: most recent reported earnings {direction} consensus by "
            f"{abs(sue):.2f} standardized units (IBES, best-effort ticker match)."
        )

    dossier = {
        "direction": "long" if composite >= 0.5 else "neutral",
        "conviction": composite,
        "horizon_months": horizon_months,
        "expected_return": resolved["expected_return_pct"],
        "thesis": f"Quantitative assessment of {symbol} based on pipeline scores.",
        "key_risks": key_risks,
        "signal_breakdown": signal_breakdown,
        "estimate": True,
        "not_financial_advice": True,
    }
    if resolved["band"] is not None:
        dossier["expected_return_band"] = resolved["band"]
    return dossier


# A parsed object must carry at least one of these to count as an LLM dossier
# rather than an arbitrary object lifted out of surrounding prose. Deliberately
# the *substantive* fields only: `estimate`/`not_financial_advice` are boilerplate
# and `conviction` is overwritten from the pipeline composite regardless.
_DOSSIER_MARKER_FIELDS = frozenset({"thesis", "key_risks", "direction", "signal_breakdown"})


def _try_parse_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from raw LLM output.

    Handles a code-fenced object and, via the shared structured-completion
    parser, an object with prose wrapped around it (a preamble the grammar
    never constrained because the server failed open on the schema). There is
    no top-level array field to salvage a truncated tail from here — the
    dossier object's own fields (``thesis``, ``key_risks``) are what a cut-off
    completion leaves incomplete, not a repeated-element array — so a
    truncated completion still surfaces as unparseable and falls through to
    the deterministic template rather than a fabricated partial dossier.

    The parsed object must actually look like a dossier. The embedded-object
    extractor will happily lift the first ``{…}`` out of *any* prose, and
    ``_coerce_dossier`` then fills in every required field — so a refusal such
    as ``I cannot analyse this candidate. {"error": "insufficient data"}``
    validated cleanly and was persisted as ``generated_by="llm"`` with an empty
    thesis, no ``fallback_reason``, and an expected_return attached as if
    analysis backed it. Requiring at least one substantive dossier field keeps
    that on the deterministic-fallback path, where it belongs.
    """
    parsed, _how = parse_structured_completion(raw, "key_risks")
    if parsed is None:
        return None
    if not _DOSSIER_MARKER_FIELDS & parsed.keys():
        return None
    return parsed


def _coerce_dossier(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill missing dossier fields so pydantic validation may pass next round."""
    direction = str(raw.get("direction", "neutral")).lower()
    if direction not in {"long", "short", "neutral"}:
        direction = "neutral"
    raw["direction"] = direction
    raw.setdefault("conviction", 0.5)
    raw.setdefault("horizon_months", 6)
    raw.setdefault("thesis", "")
    raw.setdefault("key_risks", [])
    raw.setdefault("signal_breakdown", {})
    raw.setdefault("estimate", True)
    raw.setdefault("not_financial_advice", True)
    return raw


def _ascii_safe_dict(obj: Any) -> Any:
    """Recursively apply ascii_safe to all string values in a dict/list."""
    if isinstance(obj, dict):
        return {k: _ascii_safe_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ascii_safe_dict(v) for v in obj]
    if isinstance(obj, str):
        return ascii_safe(obj)
    return obj
