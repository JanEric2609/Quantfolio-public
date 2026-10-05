"""Context Builder for the Investment Recommendation Engine.

Gathers data from all existing Quantfolio services into a ContextBundle
that gets passed to the LLM for recommendation generation.

Each service call is wrapped in try/except — if a service fails, that
section is marked as unavailable and the engine continues with partial data.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.decision.recommendation_engine.models import (
    ContextBundle,
    DataHealth,
    PortfolioContext,
    RegimeContext,
    TickerEstimates,
    TickerFundamentals,
    TickerInsiderSignal,
    TickerMetrics,
    TickerSentiment,
    TickerTrackRecord,
    UserProfile,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _safe_float(value: object, default: float | None = None) -> float | None:
    """Safely convert a value to float, returning default on failure."""
    if value is None:
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
        return result if result == result else default  # NaN check
    except (TypeError, ValueError):
        return default


def build_context_bundle(
    db: Session,
    user_id: str,
    *,
    user_profile: UserProfile | None = None,
    candidate_tickers: list[str] | None = None,
) -> ContextBundle:
    """Gather data from all services into a ContextBundle.

    This is the main entry point for the context builder. Each service call
    is independently wrapped — failures don't prevent other sections from
    being populated.

    ``candidate_tickers``, when given, replaces the held-tickers list as the
    set to fetch per-ticker metrics/fundamentals/sentiment for — this is what
    lets the same context builder serve both "advise on what I hold" and
    "screen this candidate universe" callers. Portfolio context (holdings,
    allocation) is still built either way, since candidate scoring needs it
    for portfolio-fit.
    """
    available: list[str] = []
    failed: list[str] = []
    degraded: list[str] = []

    # --- 1. Portfolio ---
    portfolio_ctx = _build_portfolio_context(db, user_id, available, failed, degraded)

    # --- 2. Ticker list ---
    tickers = list(dict.fromkeys(candidate_tickers)) if candidate_tickers else _extract_tickers(portfolio_ctx)

    # --- 3. Regime ---
    regime_ctx = _build_regime_context(db, available, failed, degraded)

    # --- 4. Per-ticker data ---
    metrics_map = _build_ticker_metrics(db, tickers, available, failed, degraded)
    fundamentals_map = _build_ticker_fundamentals(db, tickers, available, failed, degraded)
    estimates_map = _build_ticker_estimates(db, tickers, available, failed, degraded)
    insider_signal_map = _build_ticker_insider_signal(db, tickers, available, failed, degraded)
    sentiment_map = _build_ticker_sentiment(db, tickers, available, failed, degraded)

    # --- 5. News and research availability ---
    _track_news_service(db, tickers, available, failed, degraded)
    _track_research_service(db, tickers, available, failed, degraded)

    # --- 5b. Track record (advisor + llm_portfolio decision history) ---
    track_record_map = _build_ticker_track_record(db, user_id, tickers, available, failed, degraded)

    # --- 6. Compute data health ---
    total_services = len(set(available + failed + degraded))
    completeness = len(available) / total_services if total_services > 0 else 0.0

    data_health = DataHealth(
        available=sorted(set(available)),
        failed=sorted(set(failed)),
        degraded=sorted(set(degraded)),
        completeness_score=round(completeness, 2),
    )

    return ContextBundle(
        portfolio=portfolio_ctx,
        metrics=metrics_map,
        fundamentals=fundamentals_map,
        estimates=estimates_map,
        insider_signal=insider_signal_map,
        sentiment=sentiment_map,
        track_record=track_record_map,
        regime=regime_ctx,
        user_profile=user_profile or UserProfile(),
        data_health=data_health,
    )


# ---------------------------------------------------------------------------
# Individual data gatherers
# ---------------------------------------------------------------------------


def _build_portfolio_context(
    db: Session,
    user_id: str,
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> PortfolioContext:
    """Build portfolio context from the user's holdings."""
    try:
        from app.foundation.portfolio_service import main_portfolio, allocation_by_asset_type

        portfolio = main_portfolio(db, user_id)
        holdings = portfolio.holdings if portfolio else []

        holdings_data = []
        total_value = 0.0
        for h in holdings:
            qty = _safe_float(h.quantity, 0.0) or 0.0
            avg_price = _safe_float(h.avg_buy_price, 0.0) or 0.0
            value = qty * avg_price
            total_value += value
            holdings_data.append({
                "ticker": h.ticker or h.isin or "UNKNOWN",
                "isin": h.isin,
                "name": h.name,
                "asset_type": h.asset_type,
                "quantity": qty,
                "avg_buy_price": avg_price,
                "current_value": value,
                "currency": h.currency,
            })

        allocation = allocation_by_asset_type(holdings) if holdings else {}

        # Normalize allocation to percentages
        allocation_pct = {k: float(v) for k, v in allocation.items()} if allocation else {}

        available.append("portfolio")
        return PortfolioContext(
            holdings=holdings_data,
            total_value=round(total_value, 2),
            currency="EUR",
            asset_allocation=allocation_pct,
        )
    except Exception as e:
        logger.warning("Portfolio context failed: %s", e)
        failed.append("portfolio")
        return PortfolioContext()


def _extract_tickers(portfolio: PortfolioContext) -> list[str]:
    """Extract unique ticker symbols from portfolio holdings."""
    tickers = []
    seen = set()
    for h in portfolio.holdings:
        ticker = h.get("ticker", "")
        if ticker and ticker not in seen and ticker != "UNKNOWN":
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def _build_regime_context(
    db: Session,
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> RegimeContext:
    """Build regime context from the latest regime snapshot."""
    try:
        from app.foundation.data_backbone.regime_store import RegimeStore

        store = RegimeStore(db)
        snapshot = store.get_latest_snapshot()

        if snapshot is None:
            available.append("regime")
            return RegimeContext(label="unknown", confidence=0.0)

        label = snapshot.get("label", "unknown")
        score = _safe_float(snapshot.get("score"), 0.0) or 0.0
        payload = snapshot.get("payload", {})

        # Extract features from payload
        features = payload.get("features", {})
        vix = _safe_float(features.get("vix"))

        available.append("regime")
        return RegimeContext(
            label=label,
            confidence=abs(score),
            vix=vix,
            macro_snapshot=features,
        )
    except Exception as e:
        logger.warning("Regime context failed: %s", e)
        failed.append("regime")
        return RegimeContext()


def _build_ticker_metrics(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerMetrics]:
    """Build per-ticker quant metrics from price history."""
    metrics_map: dict[str, TickerMetrics] = {}
    if not tickers:
        return metrics_map

    try:
        from app.foundation.market import history
        from app.foundation import quant_metrics

        for ticker in tickers:
            try:
                bars = history(db, ticker, days=252)
                if not bars or len(bars) < 30:
                    degraded.append(f"metrics.{ticker}")
                    metrics_map[ticker] = TickerMetrics()
                    continue

                # Compute daily returns from close prices
                closes = [b.get("close") for b in bars if b.get("close") is not None]
                if len(closes) < 30:
                    degraded.append(f"metrics.{ticker}")
                    metrics_map[ticker] = TickerMetrics()
                    continue

                def _to_float(v: object) -> float:
                    return float(v)  # type: ignore[arg-type]
                returns = [
                    (_to_float(closes[i]) - _to_float(closes[i - 1])) / _to_float(closes[i - 1])
                    for i in range(1, len(closes))
                    if _to_float(closes[i - 1]) != 0
                ]

                if len(returns) < 20:
                    degraded.append(f"metrics.{ticker}")
                    metrics_map[ticker] = TickerMetrics()
                    continue

                sortino = quant_metrics.sortino_ratio(returns)
                sharpe = quant_metrics.sharpe_ratio(returns)
                calmar = quant_metrics.calmar_ratio(returns)
                cvar = quant_metrics.historical_cvar(returns)
                dd = quant_metrics.max_drawdown(returns)
                ann_ret = quant_metrics.annualised_return(returns)
                ann_vol = quant_metrics.annualised_volatility(returns)

                metrics_map[ticker] = TickerMetrics(
                    sortino=round(sortino, 4) if sortino is not None else None,
                    sharpe=round(sharpe, 4) if sharpe is not None else None,
                    calmar=round(calmar, 4) if calmar is not None else None,
                    cvar_95=round(cvar, 4) if cvar is not None else None,
                    max_drawdown=round(dd.get("max_drawdown", 0.0), 4) if dd is not None else None,
                    annualised_return=round(ann_ret, 4) if ann_ret is not None else None,
                    annualised_volatility=round(ann_vol, 4) if ann_vol is not None else None,
                )
                available.append(f"metrics.{ticker}")
            except Exception as e:
                logger.warning("Metrics failed for %s: %s", ticker, e)
                failed.append(f"metrics.{ticker}")
                metrics_map[ticker] = TickerMetrics()

    except ImportError as e:
        logger.warning("Quant metrics unavailable: %s", e)
        failed.append("metrics")

    return metrics_map


def _build_ticker_fundamentals(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerFundamentals]:
    """Build per-ticker fundamental data."""
    fundamentals_map: dict[str, TickerFundamentals] = {}
    if not tickers:
        return fundamentals_map

    try:
        from app.foundation.piotroski import compute_piotroski

        for ticker in tickers:
            try:
                result = compute_piotroski(db, ticker)
                score = result.get("score") if result else None
                if score is not None:
                    # Extract individual Piotroski components if available
                    details = result.get("details", {})
                    fundamentals_map[ticker] = TickerFundamentals(
                        piotroski_score=int(score) if score is not None else None,
                        pe_ratio=_safe_float(details.get("pe_ratio")),
                        roe=_safe_float(details.get("roe")),
                        debt_equity=_safe_float(details.get("debt_equity")),
                        profit_margin=_safe_float(details.get("profit_margin")),
                    )
                    available.append(f"fundamentals.{ticker}")
                else:
                    degraded.append(f"fundamentals.{ticker}")
                    fundamentals_map[ticker] = TickerFundamentals()
            except Exception as e:
                logger.warning("Piotroski failed for %s: %s", ticker, e)
                failed.append(f"fundamentals.{ticker}")
                fundamentals_map[ticker] = TickerFundamentals()

    except ImportError as e:
        logger.warning("Piotroski service unavailable: %s", e)
        failed.append("fundamentals")

    return fundamentals_map


def _build_ticker_estimates(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerEstimates]:
    """Build per-ticker IBES forward-earnings estimate signal, as of now.

    Sibling of ``_build_ticker_fundamentals`` -- same per-ticker try/except
    shape -- but the underlying join is ticker-string matched, not
    gvkey-verified (see ``pit_panel_joins`` module docstring's "ticker
    trap"): a match can silently miss or collide, so ``data_confidence`` is
    always surfaced alongside the values for thesis text to caveat.
    """
    estimates_map: dict[str, TickerEstimates] = {}
    if not tickers:
        return estimates_map

    try:
        import pandas as pd

        from app.foundation.data_engineering.pit_panel_joins import (
            pit_ibes_estimate_signal_for_symbol,
        )

        as_of = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")])
        for ticker in tickers:
            try:
                joined = pit_ibes_estimate_signal_for_symbol(db, ticker, as_of)
                if joined is not None:
                    row = joined.iloc[0]
                    confidence = row.get("data_confidence")
                    estimates_map[ticker] = TickerEstimates(
                        sue=_safe_float(row.get("sue")),
                        revision_momentum=_safe_float(row.get("revision_momentum")),
                        dispersion=_safe_float(row.get("dispersion")),
                        data_confidence=str(confidence) if confidence is not None else None,
                    )
                    available.append(f"estimates.{ticker}")
                else:
                    degraded.append(f"estimates.{ticker}")
                    estimates_map[ticker] = TickerEstimates()
            except Exception as e:
                logger.warning("IBES estimate signal failed for %s: %s", ticker, e)
                failed.append(f"estimates.{ticker}")
                estimates_map[ticker] = TickerEstimates()

    except ImportError as e:
        logger.warning("IBES estimate service unavailable: %s", e)
        failed.append("estimates")

    return estimates_map


def _build_ticker_insider_signal(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerInsiderSignal]:
    """Build per-ticker SEC Form 4 insider-trading signal, as of now.

    Sibling of ``_build_ticker_fundamentals``. Same ticker-string-match
    caveat as :func:`_build_ticker_estimates` -- see that function's
    docstring and the ``pit_panel_joins`` module docstring.
    """
    insider_map: dict[str, TickerInsiderSignal] = {}
    if not tickers:
        return insider_map

    try:
        import pandas as pd

        from app.foundation.data_engineering.pit_panel_joins import (
            pit_insider_signal_for_symbol,
        )

        as_of = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")])
        for ticker in tickers:
            try:
                joined = pit_insider_signal_for_symbol(db, ticker, as_of)
                if joined is not None:
                    row = joined.iloc[0]
                    confidence = row.get("data_confidence")
                    cluster_score = row.get("cluster_buy_score")
                    insider_map[ticker] = TickerInsiderSignal(
                        cluster_buy_score=int(cluster_score) if cluster_score is not None else None,
                        net_insider_flow_usd=_safe_float(row.get("net_insider_flow_usd")),
                        data_confidence=str(confidence) if confidence is not None else None,
                    )
                    available.append(f"insider_signal.{ticker}")
                else:
                    degraded.append(f"insider_signal.{ticker}")
                    insider_map[ticker] = TickerInsiderSignal()
            except Exception as e:
                logger.warning("Insider-trading signal failed for %s: %s", ticker, e)
                failed.append(f"insider_signal.{ticker}")
                insider_map[ticker] = TickerInsiderSignal()

    except ImportError as e:
        logger.warning("Insider-trading service unavailable: %s", e)
        failed.append("insider_signal")

    return insider_map


def _build_ticker_sentiment(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerSentiment]:
    """Build per-ticker sentiment data."""
    sentiment_map: dict[str, TickerSentiment] = {}
    if not tickers:
        return sentiment_map

    try:
        from app.foundation.sentiment import aggregate_sentiment

        for ticker in tickers:
            try:
                result = aggregate_sentiment(db, ticker, days=21)
                if result:
                    score = _safe_float(result.get("finbert_score"))
                    news_count = int(result.get("news_count", 0) or 0)
                    headlines = result.get("headlines", []) or []

                    sentiment_map[ticker] = TickerSentiment(
                        finbert_score=round(score, 4) if score is not None else None,
                        news_count=news_count,
                        headlines=headlines[:5],  # Top 5 headlines
                    )
                    available.append(f"sentiment.{ticker}")
                else:
                    degraded.append(f"sentiment.{ticker}")
                    sentiment_map[ticker] = TickerSentiment()
            except Exception as e:
                logger.warning("Sentiment failed for %s: %s", ticker, e)
                failed.append(f"sentiment.{ticker}")
                sentiment_map[ticker] = TickerSentiment()

    except ImportError as e:
        logger.warning("Sentiment service unavailable: %s", e)
        failed.append("sentiment")

    return sentiment_map


def _build_ticker_track_record(
    db: Session,
    user_id: str,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> dict[str, TickerTrackRecord]:
    """Historical decision track record per ticker, from advisor + llm_portfolio.

    Cross-context reads go through those contexts' facades only
    (``app.decision.advisor``, ``app.decision.llm_portfolio``), never their
    submodules — enforced by the "decision-loop internals are facade-only"
    import-linter contracts, which list this package as a source module.
    Both calls are read-only aggregations; neither computes or persists
    anything.
    """
    track_record_map: dict[str, TickerTrackRecord] = {}
    if not tickers:
        return track_record_map

    advisor_composite: float | None = None
    advisor_n_resolved = 0
    try:
        from app.decision import advisor

        scorecard = advisor.get_scorecard_history(db, user_id)
        if scorecard is not None:
            advisor_composite = scorecard.get("composite_score")
            advisor_n_resolved = int(scorecard.get("n_resolved") or 0)
            available.append("track_record.advisor")
        else:
            degraded.append("track_record.advisor")
    except Exception as e:
        logger.warning("Advisor scorecard history failed: %s", e)
        failed.append("track_record.advisor")

    verdicts_by_ticker: dict[str, list[str]] = {t: [] for t in tickers}
    try:
        from app.decision import llm_portfolio

        for mandate in ("A", "B"):
            for entry in llm_portfolio.get_decision_verdicts(db, mandate, tickers=tickers):
                ticker = entry.get("ticker", "")
                if ticker in verdicts_by_ticker and entry.get("verdict"):
                    verdicts_by_ticker[ticker].append(str(entry["verdict"]))
    except Exception as e:
        logger.warning("llm_portfolio decision verdicts failed: %s", e)
        failed.append("track_record.llm_portfolio")

    for ticker in tickers:
        history = verdicts_by_ticker.get(ticker, [])
        win_rate = (
            sum(1 for v in history if v == "hit") / len(history) if history else None
        )
        track_record_map[ticker] = TickerTrackRecord(
            advisor_composite_score=advisor_composite,
            advisor_n_resolved=advisor_n_resolved,
            llm_verdict_history=history,
            llm_win_rate=round(win_rate, 4) if win_rate is not None else None,
        )
        if history:
            available.append(f"track_record.{ticker}")
        else:
            degraded.append(f"track_record.{ticker}")

    return track_record_map


def _track_news_service(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> None:
    """Track whether the news service has data for each ticker."""
    if not tickers:
        return
    try:
        from app.foundation.models.entities import NewsItem

        for ticker in tickers:
            try:
                count = db.query(NewsItem).filter(NewsItem.ticker == ticker).count()
                if count > 0:
                    available.append(f"news.{ticker}")
                else:
                    degraded.append(f"news.{ticker}")
            except Exception as e:
                logger.warning("News query failed for %s: %s", ticker, e)
                failed.append(f"news.{ticker}")
    except ImportError as e:
        logger.warning("News model unavailable: %s", e)
        failed.append("news")


def _track_research_service(
    db: Session,
    tickers: list[str],
    available: list[str],
    failed: list[str],
    degraded: list[str],
) -> None:
    """Track whether the research service has reports for each ticker."""
    if not tickers:
        return
    try:
        from app.foundation.models.entities import StockResearchReport

        for ticker in tickers:
            try:
                count = db.query(StockResearchReport).filter(
                    StockResearchReport.ticker == ticker
                ).count()
                if count > 0:
                    available.append(f"research.{ticker}")
                else:
                    degraded.append(f"research.{ticker}")
            except Exception as e:
                logger.warning("Research query failed for %s: %s", ticker, e)
                failed.append(f"research.{ticker}")
    except ImportError as e:
        logger.warning("Research model unavailable: %s", e)
        failed.append("research")
