import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.core.db import SessionLocal
from app.foundation.models.entities import NewsItem, QuantExperiment
from app.decision.advisor.jobs import (
    register_advisor_cycle_job,
    register_advisor_rl_training_job,
    register_evolution_job,
)
from app.lab.alphacrafter.jobs import (
    register_alphacrafter_daily_job,
    register_alphacrafter_tuning_job,
)
from app.lab.alphacrafter.orchestrator import reap_stale_job_runs
from app.decision.discover.jobs import (
    register_analyst_estimates_snapshot_job,
    register_discover_ml_training_job,
    register_discover_refresh_job,
    register_discovery_resolution_job,
    register_discovery_review_job,
)
from app.decision.discover.orchestrator import reap_stale_discover_runs
from app.decision.verification.jobs import (
    register_trust_daily_ledger_job,
    register_trust_weekly_ledger_job,
)
from app.foundation.etf_lookthrough import portfolio_lookthrough
from app.foundation.jobs import (
    HEARTBEAT_INTERVAL_MINUTES,
    _track_job,
    build_worker_scheduler,
    publish_worker_heartbeat,
    reap_orphaned_job_runs,
    register_cron_job,
    register_interval_job,
)
from app.foundation.llm_audit import apply_prompt_retention_policy
from app.decision.llm_portfolio.jobs import (
    register_llm_portfolio_review_jobs,
    register_llm_review_scoring_job,
)
from app.lab.regime.jobs import (
    register_regime_daily_job,
    register_regime_refit_job,
)
from app.foundation.scalable.jobs import register_scalable_sync_job
from app.lab.evidence_gate.jobs import register_evidence_gate_job
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)


def cleanup_old_news() -> int:
    def _cleanup_old_news_inner() -> int:
        cutoff = datetime.now(UTC) - timedelta(days=21)
        with SessionLocal() as db:
            deleted = db.query(NewsItem).filter(NewsItem.published_at < cutoff).delete()
            db.commit()
            return int(deleted)
    result = [0]
    def _cleanup_old_news_tracked() -> None:
        result[0] = _cleanup_old_news_inner()
    _track_job("news_cleanup", _cleanup_old_news_tracked)
    return result[0]


def refresh_prices() -> None:
    def _refresh_prices_inner() -> None:
        """Backfill prices for all holdings + DKB positions across all users.

        Extended: includes DKB-position tickers, full 5y backfill for symbols
        with no data, incremental 30d otherwise.
        """
        from app.foundation.price_backfill import refresh_all_user_prices
        refresh_all_user_prices()

    _track_job("price_refresh", _refresh_prices_inner)


def refresh_news() -> dict:
    """Auto-refresh news for all users using the shared news service.

    Runs every 4 hours via the scheduler. Resolves each user's portfolio
    symbols and fetches news from the provider registry.
    """
    import logging

    from app.foundation.news import refresh_news_for_user

    def _refresh_news_inner() -> dict:
        _log = logging.getLogger(__name__)
        results: list[dict] = []
        with SessionLocal() as db:
            from app.foundation.models.entities import User
            users = db.query(User).all()
            for user in users:
                try:
                    result = refresh_news_for_user(db, user.id, max_symbols=12, news_per_symbol=8)
                    results.append(result)
                except Exception as exc:
                    _log.warning("News refresh failed for user %s: %s", user.id, exc)
        total_created = sum(r.get("created", 0) for r in results)
        _log.info("News refresh complete: %d items created across %d users", total_created, len(results))
        return {"total_created": total_created, "users": len(results)}

    result = [{}]
    def _refresh_news_tracked() -> None:
        result[0] = _refresh_news_inner()
    _track_job("news_refresh", _refresh_news_tracked)
    return result[0]


def bert_sentiment_enrichment() -> int:
    """Enrich recent news items with FinBERT sentiment scores.

    Runs as part of the 4h news refresh cycle.  Only touches items
    that lack a ``sentiment_label`` (provider did not supply one).
    """
    def _bert_sentiment_inner() -> int:
        try:
            from app.foundation.sentiment import enrich_news_items, is_available
            if not is_available():
                return 0
            with SessionLocal() as db:
                return enrich_news_items(db, limit=200)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("BERT enrichment failed: %s", exc)
            return 0

    result = [0]
    def _bert_sentiment_tracked() -> None:
        result[0] = _bert_sentiment_inner()
    _track_job("bert_sentiment", _bert_sentiment_tracked)
    return result[0]


def weekly_recommendation_refresh() -> int:
    def _weekly_recommendation_refresh_inner() -> int:
        with SessionLocal() as db:
            s = get_public_settings(db)
            if not s.get("scheduled_recommendation_refresh", False):
                return 0
            from app.foundation.models.entities import User
            from app.decision.ai import generate_recommendations_for_user
            users = db.query(User).all()
            total = 0
            for user in users:
                result = generate_recommendations_for_user(
                    db, user.id, universe="etfs_blue_chips", limit=5, mode="long_term", horizon="mid"
                )
                total += len(result.get("created", []))
            return total
    result = [0]
    def _weekly_recommendation_refresh_tracked() -> None:
        result[0] = _weekly_recommendation_refresh_inner()
    _track_job("weekly_recommendation_refresh", _weekly_recommendation_refresh_tracked)
    return result[0]


def weekly_active_experiment_run() -> int:
    def _weekly_active_experiment_run_inner() -> int:
        with SessionLocal() as db:
            s = get_public_settings(db)
            if not s.get("scheduled_quant_experiment_refresh", False):
                return 0
            from app.foundation.quant_experiments import run_experiment_once
            experiments = db.query(QuantExperiment).filter(QuantExperiment.active.is_(True)).all()
            count = 0
            for experiment in experiments:
                try:
                    run_experiment_once(db, experiment)
                    count += 1
                except Exception:
                    logging.getLogger(__name__).warning("Experiment %s failed: %s", experiment.id, exc_info=True)
            return count
    result = [0]
    def _weekly_active_experiment_run_tracked() -> None:
        result[0] = _weekly_active_experiment_run_inner()
    _track_job("weekly_active_experiment_run", _weekly_active_experiment_run_tracked)
    return result[0]


def cleanup_llm_audit_events() -> dict[str, int]:
    def _cleanup_llm_audit_events_inner() -> dict[str, int]:
        with SessionLocal() as db:
            s = get_public_settings(db)
            raw_hours = int(s.get("prompt_raw_retention_hours", 24))
            delete_days = int(s.get("prompt_delete_after_days", 14))
            return apply_prompt_retention_policy(db, raw_hours=raw_hours, delete_days=delete_days)
    result = [{}]
    def _cleanup_llm_audit_events_tracked() -> None:
        result[0] = _cleanup_llm_audit_events_inner()
    _track_job("llm_audit_cleanup", _cleanup_llm_audit_events_tracked)
    return result[0]


def weekly_advisor_pulse() -> None:
    def _weekly_advisor_pulse_inner() -> None:
        from app.foundation.models.entities import User
        from app.decision.portfolio_advisor.pulse import run_pulse_check
        from app.lab.regime.macro_snapshot import get_or_refresh_regime
        from app.decision.regime_advisor import get_regime_adjusted_weights
        with SessionLocal() as db:
            regime = get_or_refresh_regime(db)
            regime_label = regime.get("label", "unknown")
            regime_weights = get_regime_adjusted_weights(db, regime_label)
            for user in db.query(User).all():
                try:
                    holdings = _load_holdings_for_user(db, user.id)
                    lookthrough = portfolio_lookthrough(db, user.id)
                    run_pulse_check(holdings, regime, regime_weights, lookthrough)
                except Exception:
                    logging.getLogger(__name__).warning("Pulse check failed for user %s: %s", user.id, exc_info=True)
    _track_job("weekly_advisor_pulse", _weekly_advisor_pulse_inner)


def monthly_advisor_deep() -> None:
    def _monthly_advisor_deep_inner() -> None:
        from app.foundation.models.entities import User
        from app.decision.portfolio_advisor.deep import run_deep_analysis, make_llm_func
        from app.lab.regime.macro_snapshot import get_or_refresh_regime
        from app.decision.regime_advisor import get_regime_adjusted_weights
        with SessionLocal() as db:
            regime = get_or_refresh_regime(db)
            regime_label = regime.get("label", "unknown")
            regime_weights = get_regime_adjusted_weights(db, regime_label)
            llm = make_llm_func(db)
            for user in db.query(User).all():
                try:
                    holdings = _load_holdings_for_user(db, user.id)
                    lookthrough = portfolio_lookthrough(db, user.id)
                    run_deep_analysis(holdings, regime, llm_func=llm, regime_weights=regime_weights, lookthrough=lookthrough)
                except Exception:
                    logging.getLogger(__name__).warning("Deep analysis failed for user %s: %s", user.id, exc_info=True)
    _track_job("monthly_advisor_deep", _monthly_advisor_deep_inner)


def weekly_mwu_update() -> None:
    def _weekly_mwu_update_inner() -> None:
        from app.foundation.recommendation_outcomes import run_outcome_evaluation
        from app.decision.regime_advisor import run_mwu_update
        with SessionLocal() as db:
            # run_mwu_update reads RecommendationOutcome rows, which are only
            # populated by run_outcome_evaluation. That evaluation was
            # previously reachable only via a manual POST /evaluate endpoint
            # nothing ever called — meaning this job silently ran on empty
            # data every week. Evaluate first so there's something to weight.
            run_outcome_evaluation(db)
            run_mwu_update(db)
    _track_job("weekly_mwu_update", _weekly_mwu_update_inner)


def weekly_portfolio_analysis() -> int:
    """Generate portfolio-level LLM analysis for all users (weekly Sun 9:30)."""
    def _weekly_portfolio_analysis_inner() -> int:
        try:
            from app.foundation.portfolio_analysis import run_weekly_portfolio_analysis
            return run_weekly_portfolio_analysis()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Weekly portfolio analysis failed: %s", exc)
            return 0
    result = [0]
    def _weekly_portfolio_analysis_tracked() -> None:
        result[0] = _weekly_portfolio_analysis_inner()
    _track_job("weekly_portfolio_analysis", _weekly_portfolio_analysis_tracked)
    return result[0]


def _load_holdings_for_user(db, user_id: str) -> list[dict]:
    """Manual holdings plus every synced broker position, each counted once.

    Synced holdings (``dkb_sync``/``broker_sync``) mirror the live positions,
    so they are skipped here in favour of the positions themselves.
    """
    from sqlalchemy.orm import selectinload
    from app.foundation.live_positions import is_synced_holding, live_positions
    from app.foundation.models.entities import Portfolio
    holdings = []
    for p in (
        db.query(Portfolio)
        .options(selectinload(Portfolio.holdings))
        .filter(Portfolio.user_id == user_id)
        .all()
    ):
        for h in p.holdings:
            if is_synced_holding(h):
                continue
            holdings.append({
                "name": h.name,
                "current_value": float(h.quantity) * float(h.avg_buy_price) if h.avg_buy_price else 0,
                "asset_type": h.asset_type,
                "ticker": h.ticker,
                "quantity": float(h.quantity),
            })
    for p in live_positions(db, user_id):
        holdings.append({
            "name": p.name,
            "current_value": float(p.value),
            "asset_type": "etf",
            "ticker": p.ticker,
            "quantity": float(p.quantity or 0),
        })
    return holdings


_PAID_KEYED_PROVIDERS = frozenset(
    {"databento", "alpaca", "massive", "twelvedata", "tiingo"}
)


def _apply_provider_health_status(
    db: Any,
    status_payload: dict[str, Any],
    *,
    secret_present: bool = True,
) -> None:
    """UPSERT one ProviderHealth row (capability="status") from a probe payload.

    Semantics per audit §2.1: verified-available -> healthy, enabled-but-failing
    -> degraded, paid API without configured secret -> disabled (honest), and
    anything not enabled -> missing. Timestamps record the transition; a
    healthy probe clears the previous error stamp and vice versa.
    """
    from app.foundation.models.entities import ProviderHealth

    provider = str(status_payload.get("provider") or "unknown")
    enabled = bool(status_payload.get("enabled", False))
    available = bool(status_payload.get("available", False))
    message = str(status_payload.get("message") or "")[:500]
    now = datetime.now(UTC)

    if provider in _PAID_KEYED_PROVIDERS and not secret_present:
        status, configured, effective_available = "disabled", False, False
    elif enabled and available:
        status, configured, effective_available = "healthy", True, True
    elif enabled:
        status, configured, effective_available = "degraded", True, False
    else:
        status, configured, effective_available = "missing", enabled, False

    row = (
        db.query(ProviderHealth)
        .filter(ProviderHealth.provider == provider, ProviderHealth.capability == "status")
        .one_or_none()
    )
    if row is None:
        row = ProviderHealth(provider=provider, capability="status", message="")
        db.add(row)
    row.configured = configured
    row.available = effective_available
    row.status = status
    row.message = message
    row.updated_at = now
    if status == "healthy":
        row.last_success_at = now
        row.last_error_at = None
    elif status == "degraded":
        row.last_error_at = now
        row.last_success_at = None


def _provider_health_probe_once(db: Any | None = None) -> None:
    """One sequential sweep of registry statuses into ProviderHealth rows.

    Reuses the same machinery as GET /api/market/providers/health so nightly
    rows stay shape-compatible with what the Control Center reads. Honors each
    provider's own timeouts (registry _PER_PROVIDER_TIMEOUT_S <= 30s); never
    raises — per-provider failures are logged and skipped.
    """
    from app.foundation.market import provider_status
    from app.foundation.settings import get_secret

    owns_db = db is None
    if db is None:
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
    try:
        statuses = provider_status(db)
        for payload in statuses:
            provider = str(payload.get("provider") or "unknown")
            try:
                secret_present = True
                if provider in _PAID_KEYED_PROVIDERS:
                    stored = get_secret(db, provider)
                    secret_present = bool(stored[0]) if stored is not None else False
                _apply_provider_health_status(db, payload, secret_present=secret_present)
            except Exception as exc:
                logger.warning(
                    "provider_health_probe: %s status apply failed: %s", provider, exc
                )
        db.commit()
    except Exception as exc:
        logger.error("provider_health_probe failed: %s", exc, exc_info=True)
        if owns_db:
            db.rollback()
    finally:
        if owns_db:
            db.close()


def register_provider_health_probe_job(scheduler: Any | None = None) -> str:
    """Register the nightly real-probe job for market-data provider health.

    Runs at 05:10 UTC — before the data-heavy morning jobs — so stale rows
    (e.g. an openbb entry frozen pointing at a dead port) are repaired daily.
    """
    return register_cron_job(
        "provider_health_probe",
        _provider_health_probe_once,
        scheduler=scheduler,
        hour=5,
        minute=10,
    )

_REGIME_MACRO_SERIES = ["VIXCLS", "T10Y2Y", "BAA10Y"]


def register_macro_refresh_job(scheduler: Any | None = None) -> str:
    """Register the daily macro-indicator refresh job.

    Runs at 07:00 UTC every day, before the regime classify job at 07:15
    that consumes ``macro_indicators`` rows. Fetches exactly the FRED series
    the regime classifier maps (VIXCLS/T10Y2Y/BAA10Y — see
    ``app.lab.regime.classifier.MACRO_SERIES_MAP``) via the FRED
    provider specifically. Previously nothing ever called
    ``ingest_macro_indicators`` on a schedule (only a manual
    ``POST /api/data/macro/refresh``), so ``macro_indicators`` stayed empty
    and the regime classifier silently ran price-only.
    """

    def macro_refresh_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.data_backbone.ingest import DataIngester

        db = SessionLocal()
        try:
            result = DataIngester(db).ingest_macro_indicators(series_ids=_REGIME_MACRO_SERIES)
            logger.info("macro_refresh: %s", result)
        except Exception as exc:
            logger.error("macro_refresh failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "macro_refresh", macro_refresh_inner,
        scheduler=scheduler, hour=7, minute=0,
    )

def register_listing_currency_audit_job(scheduler: Any | None = None) -> str:
    """Register the listing-currency audit (daily, and once at worker start).

    Records each stored symbol's provider-reported quote currency in
    ``listing_currencies`` and relabels its ``bar_prices``/``price_cache``
    rows (429 of 780 symbols carried a guessed label on 2026-09-28; see
    ``data_backbone.listing_currency``). A symbol is re-probed only after 30
    days, so a daily run touches the new ones.
    """

    def listing_currency_audit_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.data_backbone.listing_currency import audit_listing_currencies

        with SessionLocal() as db:
            audit_listing_currencies(db)

    return register_interval_job(
        "listing_currency_audit", listing_currency_audit_inner,
        scheduler=scheduler, hours=24, run_immediately=True,
    )


def register_insider_trading_refresh_job(scheduler: Any | None = None) -> str:
    """Register the daily SEC EDGAR insider-trading refresh (04:15 UTC).

    Loads any quarterly Form 3/4/5 set the panel lacks (since 2024 Q2), then
    each day's Form 4 filings from EDGAR's daily index after the newest
    quarter (``data_engineering.sec_edgar_insider``). The panel was a one-off
    extract ending 2024-03-29, so Discover's insider signal had been
    "unknown" for every US name. The first run backfills ~9 quarters and
    ~3 months of days (about an hour at SEC's rate limit); later runs take a
    minute or two. Runs before the Monday 05:00 Discover refresh.
    """

    def insider_trading_refresh_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.data_engineering.sec_edgar_insider import refresh_insider_trading

        with SessionLocal() as db:
            refresh_insider_trading(db)

    return register_cron_job(
        "insider_trading_refresh", insider_trading_refresh_inner,
        scheduler=scheduler, hour=4, minute=15,
    )


def register_index_currency_weights_job(scheduler: Any | None = None) -> str:
    """Register the monthly refresh of index currency weights (3rd, 05:15 UTC).

    Reloads the currencies each index's constituents trade in from SPDR's
    daily holdings files into ``index_currency_weights``, which verification
    uses to look through index ETFs (``foundation.etf_currency``). Until the
    first run, the module's seed values from 2026-09-28 apply; weights older
    than six months raise a risk alert.
    """

    def index_currency_weights_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.etf_currency import refresh_index_currency_weights

        with SessionLocal() as db:
            refresh_index_currency_weights(db)

    return register_cron_job(
        "index_currency_weights", index_currency_weights_inner,
        scheduler=scheduler, day="3", hour=5, minute=15,
    )


def register_risk_free_rate_refresh_job(scheduler: Any | None = None) -> str:
    """Register the daily risk-free-rate cache refresh from ECB SDW (€STR).

    Runs at 06:30 UTC, before macro_refresh (07:00), so a fresh rate is in
    place before anything downstream (advisor cycle, discover scoring,
    verification risk) reads it that day. Fetches a trailing 10-day window
    rather than a single date since €STR has no observation on
    weekends/bank holidays; the most recent observation in the window wins.
    On any failure (network, empty response) this leaves the existing DB
    cache untouched and logs a warning — get_risk_free_rate()'s hardcoded
    fallback only matters if the cache has never been populated at all.
    """

    def risk_free_rate_refresh_inner() -> None:
        from datetime import timedelta

        from app.foundation.core.db import SessionLocal
        from app.foundation.providers.ecb_provider import EcbProvider
        from app.foundation.settings import upsert_public_settings

        db = SessionLocal()
        try:
            provider = EcbProvider()
            end = datetime.now(UTC).date()
            start = end - timedelta(days=10)
            result = provider.get_history(
                "EST.B.EU000A2X2A25.WT",
                start=start.isoformat(),
                end=end.isoformat(),
            )
            if not result.get("ok"):
                logger.warning("risk_free_rate_refresh: ECB SDW fetch failed: %s", result.get("error"))
                return
            observations = result.get("data") or []
            if not observations:
                logger.warning("risk_free_rate_refresh: ECB SDW returned no observations")
                return
            latest = observations[-1]
            rate_pct = float(latest["value"]) / 100.0
            upsert_public_settings(db, {
                "risk_free_rate_pct": rate_pct,
                "risk_free_rate_as_of": datetime.now(UTC).isoformat(),
            })
            logger.info("risk_free_rate_refresh: updated to %.4f%%", rate_pct * 100)
        except Exception as exc:
            logger.error("risk_free_rate_refresh failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "risk_free_rate_refresh", risk_free_rate_refresh_inner,
        scheduler=scheduler, hour=6, minute=30,
    )

def register_monthly_envelope_rollover_job(scheduler: Any | None = None) -> str:
    """Register the midnight-on-the-1st envelope rollover job (Money Phase 2).

    Seeds next month's EnvelopeBudget rows from the prior month's budgeted
    amounts. See run_monthly_envelope_rollover's docstring: the rollover
    *balance* itself is always derived at read time, independent of this job.
    """

    def envelope_rollover_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.envelope import run_monthly_envelope_rollover

        db = SessionLocal()
        try:
            result = run_monthly_envelope_rollover(db)
            logger.info("envelope_rollover: %s", result)
        except Exception as exc:
            logger.error("envelope_rollover failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "envelope_rollover", envelope_rollover_inner,
        scheduler=scheduler, day="1", hour=0, minute=0,
    )

def send_telegram_daily_reminders(db: Any) -> dict[str, int]:
    """Send the ~20:00 nudge to any linked user who hasn't logged anything today.

    Money Phase 3 (design doc §1.7): skip the reminder entirely for users
    who already logged something today — the point is to build the habit,
    not to nag once it's already happening.
    """
    from datetime import date

    from app.foundation.models.entities import Expense, TelegramAccount
    from app.foundation.telegram_bot import send_telegram_message

    today = date.today()
    reminded = 0
    for account in db.query(TelegramAccount).all():
        has_entry_today = (
            db.query(Expense)
            .filter(Expense.user_id == account.user_id, Expense.date == today)
            .first()
            is not None
        )
        if has_entry_today:
            continue
        send_telegram_message(
            db, account.chat_id, "No spending logged today. Send an amount + description to log one, or ignore this if there was nothing to log."
        )
        reminded += 1
    return {"reminded": reminded}


def register_telegram_daily_reminder_job(scheduler: Any | None = None) -> str:
    """Register the ~20:00 daily Telegram reminder job (Money Phase 3, §1.7)."""

    def daily_reminder_inner() -> None:
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            result = send_telegram_daily_reminders(db)
            logger.info("telegram_daily_reminder: %s", result)
        except Exception as exc:
            logger.error("telegram_daily_reminder failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "telegram_daily_reminder", daily_reminder_inner,
        scheduler=scheduler, hour=20, minute=0,
    )

def register_telegram_polling_job(scheduler: Any | None = None) -> str:
    """Register the Telegram ``getUpdates`` long-polling job.

    Runs every minute; each run long-polls for ~50 s (``poll_telegram_updates``),
    so the bot answers within seconds without a public webhook. The run is a
    no-op unless ``telegram_mode`` is ``polling`` and a bot token is saved, and
    the mode is read on every run, so switching modes needs no worker restart.
    Untracked: a JobRun row every minute would drown the job history.

    Never log the exception object here: httpx puts the request URL, which holds
    the bot token, in its messages.
    """

    def polling_inner() -> None:
        from app.foundation.core.db import SessionLocal
        from app.foundation.telegram_updates import poll_telegram_updates

        db = SessionLocal()
        try:
            poll_telegram_updates(db)
        except Exception as exc:
            logger.error("telegram_polling failed: %s", type(exc).__name__)
        finally:
            db.close()

    return register_interval_job(
        "telegram_polling", polling_inner,
        scheduler=scheduler, minutes=1, track=False,
    )


def register_watchlist_price_checker_job(scheduler: Any | None = None) -> str:
    """Register the watchlist buy-alert price checker job.

    Runs every 30 minutes.  Compares each watchlist item's target_price
    against the latest market quote and fires nudge notifications when
    the price drops to or below the target.
    """
    from app.foundation.buy_alerts import run_alert_check

    return register_cron_job(
        "watchlist_price_checker", run_alert_check,
        scheduler=scheduler, minute="*/30",
    )

def register_daily_position_snapshot_job(scheduler: Any | None = None) -> str:
    """Register the daily book position snapshot job (every broker).

    Runs daily at 20:00 UTC (after market close). Takes a point-in-time
    snapshot of the positions at every broker (book_position_snapshots), the
    holdings history the book's time-weighted return is built from.
    """
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import User

    def _take_snapshots_inner() -> None:
        db = SessionLocal()
        try:
            from app.foundation.portfolio_service import snapshot_book_positions

            user_ids = [r[0] for r in db.query(User.id).all()]
            total = 0
            for uid in user_ids:
                try:
                    result = snapshot_book_positions(db, uid)
                    total += result.get("snapshots_created", 0)
                except Exception as exc:
                    logger.error("position_snapshot failed for user %s: %s", uid, exc)
                    try:
                        db.rollback()
                    except Exception:
                        logger.warning("rollback failed after snapshot error for user %s", uid)
            logger.info("position_snapshot: created %d snapshots for %d users", total, len(user_ids))
        except Exception as exc:
            logger.error("position_snapshot job failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "daily_position_snapshot", _take_snapshots_inner,
        scheduler=scheduler, hour=20, minute=0,
    )

def register_rss_refresh_job(scheduler: Any | None = None) -> str:
    """Register the RSS feed refresh job.

    Runs every 30 minutes to fetch and cache RSS feeds from configured
    financial news sources. Updates the in-memory feed cache so the
    /api/news/rss endpoint returns fresh data.
    """

    def rss_refresh_inner() -> None:
        from app.foundation.rss_provider import fetch_all_feeds, invalidate_cache

        invalidate_cache()
        result = fetch_all_feeds()
        logger.info(
            "rss_refresh: items=%d sources=%d errors=%d",
            len(result["items"]),
            len(result["sources"]),
            len(result["errors"]),
        )

    return register_cron_job(
        "rss_refresh", rss_refresh_inner,
        scheduler=scheduler, minute="*/30",
    )

def register_snapshot_paper_portfolios_job(scheduler: Any | None = None) -> str:
    """Register the daily paper portfolio snapshot job.

    Runs daily at midnight UTC. Iterates all paper portfolios, creates
    a daily snapshot of holdings and cash, and computes risk metrics.
    """
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import PaperPortfolio

    def _snapshot_paper_inner() -> None:
        db = SessionLocal()
        succeeded = 0
        failed = 0
        try:
            from app.decision.paper_portfolio import compute_metrics, snapshot_paper_portfolio

            from app.foundation.models.entities._core import now_utc

            yesterday = now_utc().date() - timedelta(days=1)
            portfolios = db.query(PaperPortfolio).all()
            for p in portfolios:
                try:
                    # 00:00 UTC: the date that just ended, valued at its close.
                    snapshot_paper_portfolio(db, p.id, as_of=yesterday)
                    compute_metrics(db, p.id)
                    succeeded += 1
                except Exception as exc:
                    logger.error("paper_snapshot failed for portfolio %s: %s", p.id, exc)
                    failed += 1
                    try:
                        db.rollback()
                    except Exception:
                        logger.warning("rollback failed after paper snapshot error for portfolio %s", p.id)
            logger.info(
                "paper_snapshot: %d succeeded, %d failed of %d paper portfolios",
                succeeded, failed, len(portfolios),
            )
        finally:
            db.close()
        # Raised outside the try/finally so it reaches _track_job and lands in
        # JobRun.status. Previously this raise sat inside a try whose sibling
        # `except Exception` caught it, so the job reported success even when
        # every single portfolio failed to snapshot.
        if failed:
            raise RuntimeError(
                "paper_snapshot: %d failed, %d succeeded" % (failed, succeeded)
            )

    return register_cron_job(
        "snapshot_paper_portfolios", _snapshot_paper_inner,
        scheduler=scheduler, hour=0, minute=0,
    )

# Wave 3 (T3.E): window for the regime index-bar backfill. Mirrors
# classify_and_store's default lookback (services/regime/classifier.py) so the
# backfilled slice always covers what the classifier actually reads.
REGIME_INDEX_LOOKBACK_DAYS = 400


def _refresh_regime_index_bars(db: Session) -> dict[str, Any]:
    """Backfill bars for the configured regime index symbol (Wave 3, T3.E).

    Why: production evidence (audit D2,
    docs/archive/audits/2026-08-universe-regime-audit.md) showed regime snapshots
    frozen while nothing scheduled ever ingested ``regime_index_symbol`` — the
    Wave-1 freshness guard (classify/refit refuse stale bars) stayed
    permanently starved even though the classifier itself was fixed. This
    gives the index symbol the same daily ingestion discipline the holdings
    price backfill already gets.

    Reuses the ONE shared ingestion entrypoint the classifier's own stale-bar
    guard uses (``DataIngester.ingest_bar_prices``). The symbol comes from
    public settings (``regime_index_symbol``, ^STOXX50E fallback identical to
    ``classify_and_store`` — never hardcoded here). Skips the network when the
    newest stored bar is already within ``regime_max_bar_age_days``, so an
    already-fresh symbol is not re-ingested on every run.

    Never raises: any failure is logged and returned so one symbol failing can
    never kill the rest of the daily job.
    """
    from app.foundation.data_backbone.bars import BarStore
    from app.foundation.data_backbone.ingest import DataIngester
    from app.foundation.settings import get_public_settings

    outcome: dict[str, Any] = {"symbol": None, "skipped": False}
    try:
        settings = get_public_settings(db)
        symbol = str(settings.get("regime_index_symbol", "^STOXX50E"))
        max_age_days = float(settings.get("regime_max_bar_age_days", 7))
        outcome["symbol"] = symbol

        end = datetime.now(UTC)
        latest = BarStore(db).get_latest_bar(symbol)
        if latest is not None and latest.get("ts") is not None:
            last_ts = latest["ts"]
            if isinstance(last_ts, str):
                last_ts = datetime.fromisoformat(last_ts)
            # Normalise to naive UTC: SQLite returns naive timestamps while
            # Postgres TIMESTAMPTZ returns aware ones.
            if last_ts.tzinfo is not None:
                last_ts = last_ts.astimezone(UTC).replace(tzinfo=None)
            age_days = (end.replace(tzinfo=None) - last_ts).total_seconds() / 86400.0
            if age_days <= max_age_days:
                outcome.update(
                    skipped=True,
                    reason="bars_fresh",
                    last_bar_age_days=round(age_days, 2),
                )
                return outcome

        start_iso = (end - timedelta(days=REGIME_INDEX_LOOKBACK_DAYS)).date().isoformat()
        end_iso = end.date().isoformat()
        logger.info(
            "regime_index_backfill: refreshing %s bars (%s .. %s)",
            symbol, start_iso, end_iso,
        )
        result = DataIngester(db).ingest_bar_prices(
            symbol=symbol, start_date=start_iso, end_date=end_iso,
        )
        outcome.update(result)
    except Exception as exc:  # noqa: BLE001 — worker robustness: log-and-continue
        logger.error(
            "regime_index_backfill failed for %s: %s",
            outcome.get("symbol"), exc, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 — rollback is best-effort during failure handling
            logger.warning("regime_index_backfill: rollback after failure also failed")
        outcome["error"] = str(exc)[:200]
    return outcome


def register_price_backfill_daily_job(scheduler: Any | None = None) -> str:
    """Register the daily price backfill job.

    Runs at 06:30 UTC every day (before regime job at 07:15). Backfills
    price data for all users' holdings and DKB positions, then keeps the
    configured regime index symbol fed (Wave 3, T3.E — see
    ``_refresh_regime_index_bars``).
    """
    from app.foundation.price_backfill import refresh_all_user_prices

    def _price_backfill_inner() -> None:
        try:
            combined = refresh_all_user_prices()
            logger.info(
                "price_backfill_daily: %d/%d succeeded, %d failed across %d users",
                combined["succeeded"], combined["total"], combined["failed"],
                len(combined.get("user_results", {})),
            )
        except Exception as exc:
            logger.error("price_backfill_daily failed: %s", exc, exc_info=True)
            raise

        # Wave 3 (T3.E): after the holdings backfill, feed the regime index
        # symbol. The helper is log-and-continue by contract — an ingestion
        # failure here must never fail the rest of the daily job.
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            result = _refresh_regime_index_bars(db)
            logger.info("price_backfill_daily: regime index bars -> %s", result)
        finally:
            db.close()

    return register_cron_job(
        "price_backfill_daily", _price_backfill_inner,
        scheduler=scheduler, hour=6, minute=30,
    )


def build_scheduler() -> Any:
    scheduler = build_worker_scheduler()

    try:
        with SessionLocal() as db:
            reap_orphaned_job_runs(db)
    except Exception:
        logger.exception("Failed to reap orphaned job runs")

    try:
        with SessionLocal() as db:
            reap_stale_job_runs(db)
    except Exception:
        logger.exception("Failed to reap stale AlphaCrafter jobs")

    try:
        with SessionLocal() as db:
            reap_stale_discover_runs(db)
    except Exception:
        logger.exception("Failed to reap stale discover runs")

    def reap_discover_runs_periodic() -> None:
        try:
            with SessionLocal() as db:
                reap_stale_discover_runs(db)
        except Exception:
            logger.exception("Periodic discover-run reap failed")

    # Also reap periodically, not just at startup — a run orphaned by an API
    # process restart (e.g. mid-deploy) should self-heal within minutes
    # rather than sitting stuck until the next full process restart. A short
    # 2-minute interval (this is a cheap DB query) keeps the worst-case gap
    # between "past the dossier-complete grace window" and "next reap tick"
    # small — a run that stalls right after a restart shouldn't have to wait
    # out a long interval on top of the grace window itself.
    register_interval_job(
        "discover_reap_stale_runs", reap_discover_runs_periodic,
        scheduler=scheduler, minutes=2, track=False, run_immediately=True,
    )

    # Publishes the registered job list for the Control Center (schedules,
    # next runs, retired jobs, pending "needs worker restart" settings).
    worker_started_at = datetime.now(UTC)
    register_interval_job(
        "worker_heartbeat", lambda: publish_worker_heartbeat(scheduler, worker_started_at),
        scheduler=scheduler, minutes=HEARTBEAT_INTERVAL_MINUTES, track=False, run_immediately=True,
    )

    # DKB sync is intentionally manual-only. Jobs here avoid FinTS TAN prompts.
    register_interval_job("price_refresh", refresh_prices, scheduler=scheduler, minutes=15, track=False)
    register_interval_job("news_refresh", refresh_news, scheduler=scheduler, hours=4, track=False)
    register_interval_job("bert_sentiment", bert_sentiment_enrichment, scheduler=scheduler, hours=4, track=False)
    register_cron_job("news_cleanup", cleanup_old_news, scheduler=scheduler, hour=2, minute=0, track=False)
    register_cron_job("weekly_recommendation_refresh", weekly_recommendation_refresh, scheduler=scheduler, day_of_week="sun", hour=8, minute=30, track=False)
    register_cron_job("weekly_active_experiment_run", weekly_active_experiment_run, scheduler=scheduler, day_of_week="sun", hour=8, minute=45, track=False)
    register_cron_job("llm_audit_cleanup", cleanup_llm_audit_events, scheduler=scheduler, hour=2, minute=20, track=False)
    register_cron_job("weekly_advisor_pulse", weekly_advisor_pulse, scheduler=scheduler, day_of_week="sun", hour=9, minute=30, track=False)
    register_cron_job("monthly_advisor_deep", monthly_advisor_deep, scheduler=scheduler, day="1", hour=8, minute=0, track=False)
    register_cron_job("weekly_mwu_update", weekly_mwu_update, scheduler=scheduler, day_of_week="mon", hour=7, minute=30, track=False)
    register_cron_job("weekly_portfolio_analysis", weekly_portfolio_analysis, scheduler=scheduler, day_of_week="sun", hour=10, minute=0, track=False)
    register_risk_free_rate_refresh_job(scheduler)
    register_provider_health_probe_job(scheduler)
    register_macro_refresh_job(scheduler)
    register_listing_currency_audit_job(scheduler)
    register_index_currency_weights_job(scheduler)
    register_insider_trading_refresh_job(scheduler)
    register_price_backfill_daily_job(scheduler)
    # Read-only and TAN-free, unlike DKB; wakes hourly, syncs per scalable_sync_hours.
    register_scalable_sync_job(scheduler)
    register_evidence_gate_job(scheduler)
    register_regime_daily_job(scheduler)
    register_regime_refit_job(scheduler)
    register_watchlist_price_checker_job(scheduler)
    register_daily_position_snapshot_job(scheduler)
    register_snapshot_paper_portfolios_job(scheduler)
    register_llm_portfolio_review_jobs(scheduler)
    register_llm_review_scoring_job(scheduler)
    # Must precede the advisor cycle in the week: the cycle has no candidates
    # to trade without a fresh completed Discover run.
    register_discover_refresh_job(scheduler)
    register_advisor_cycle_job(scheduler)
    register_evolution_job(scheduler)
    register_discovery_resolution_job(scheduler)
    register_trust_daily_ledger_job(scheduler)
    register_trust_weekly_ledger_job(scheduler)
    register_discovery_review_job(scheduler)
    register_discover_ml_training_job(scheduler)
    register_analyst_estimates_snapshot_job(scheduler)
    register_monthly_envelope_rollover_job(scheduler)
    register_telegram_daily_reminder_job(scheduler)
    register_telegram_polling_job(scheduler)
    register_alphacrafter_daily_job(scheduler)
    register_alphacrafter_tuning_job(scheduler)
    register_advisor_rl_training_job(scheduler)

    return scheduler


def main() -> None:
    """Worker process entrypoint (``python -m app.worker``).

    Logging is configured first, exactly as the API does (same ``LOG_LEVEL``,
    same format, URL-logging libraries pinned to WARNING). Without it the root
    logger stays at WARNING and every INFO line the jobs emit is dropped.
    """
    from app.foundation.core.config import get_settings
    from app.foundation.core.logging_setup import configure_logging

    configure_logging(get_settings().log_level)
    build_scheduler().start()


if __name__ == "__main__":
    main()
