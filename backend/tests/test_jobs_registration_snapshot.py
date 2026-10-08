"""Scheduler registration snapshot for the quantfolio-worker (Wave 0, U3).

pytest never exercises job registration in production: jobs.py runs only in
the worker process via APScheduler. This test pins the worker's registration
contract — every public ``register_*_job`` function driven against a
recording scheduler stub and asserted against an explicit snapshot dict
literal below, so a regression shows up as a visible dict diff.

Characterization status at capture time (commit message records GREEN-first):
all triggers are static constants today — no registration reads settings or
the environment. If a future registration becomes settings-driven, pin the
setting with ``monkeypatch.setattr`` on the settings accessor exactly like
the advisor/verification tests do, and keep the snapshot literal as the
source of truth.

The stub never starts a real BackgroundScheduler (a scheduler is always
passed), so nothing is scheduled and no job body ever runs. Job bodies open
their own SessionLocal at execution time; registration only closes over
them.
"""
from app.foundation.core.db import Base
from app.foundation import jobs
from app.decision.advisor.jobs import (
    register_advisor_cycle_job,
    register_advisor_rl_training_job,
    register_evolution_job,
)
from app.lab.alphacrafter.jobs import (
    register_alphacrafter_daily_job,
    register_alphacrafter_tuning_job,
)
from app.decision.discover.jobs import (
    register_analyst_estimates_snapshot_job,
    register_discover_ml_training_job,
    register_discover_refresh_job,
    register_discovery_resolution_job,
    register_discovery_review_job,
)
from app.decision.llm_portfolio.jobs import (
    register_llm_portfolio_review_jobs,
    register_llm_review_scoring_job,
)
from app.lab.evidence_gate.jobs import register_evidence_gate_job
from app.decision.verification.jobs import (
    register_trust_daily_ledger_job,
    register_trust_weekly_ledger_job,
)
from app.lab.regime.jobs import (
    register_regime_daily_job,
    register_regime_refit_job,
)
from app.foundation.scalable.jobs import register_scalable_sync_job
from app.worker import (
    register_daily_position_snapshot_job,
    register_index_currency_weights_job,
    register_insider_trading_refresh_job,
    register_listing_currency_audit_job,
    register_macro_refresh_job,
    register_monthly_envelope_rollover_job,
    register_price_backfill_daily_job,
    register_provider_health_probe_job,
    register_risk_free_rate_refresh_job,
    register_rss_refresh_job,
    register_snapshot_paper_portfolios_job,
    register_telegram_daily_reminder_job,
    register_telegram_polling_job,
    register_watchlist_price_checker_job,
    send_telegram_daily_reminders,
)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class RecordingScheduler:
    """Minimal APScheduler stand-in capturing add_job calls."""

    def __init__(self):
        self.registered: dict[str, dict] = {}

    def add_job(self, fn, *, trigger, id, replace_existing, **trigger_fields):
        self.registered[id] = {"trigger": trigger, **trigger_fields}

        class _Handle:
            pass

        handle = _Handle()
        handle.id = id
        return handle


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# The worker's registration contract. Keys are APScheduler job ids; values
# are the exact trigger type and trigger fields passed to add_job.
EXPECTED_REGISTRATIONS = {
    "alphacrafter_daily": {"trigger": "cron", "hour": 8, "minute": 0},
    "alphacrafter_tuning": {
        "trigger": "cron",
        "month": "1,4,7,10",
        "day": "1",
        "hour": 6,
        "minute": 15,
    },
    "provider_health_probe": {"trigger": "cron", "hour": 5, "minute": 10},
    "advisor_cycle": {
        "trigger": "cron",
        "day_of_week": "mon-fri",
        "hour": 10,
        "minute": 0,
    },
    "evolution_round": {
        "trigger": "cron",
        "day_of_week": "mon-fri",
        "hour": 10,
        "minute": 30,
    },
    "macro_refresh": {"trigger": "cron", "hour": 7, "minute": 0},
    "risk_free_rate_refresh": {"trigger": "cron", "hour": 6, "minute": 30},
    "evidence_gate": {"trigger": "cron", "day_of_week": "sun", "hour": 5, "minute": 30},
    "regime_daily": {"trigger": "cron", "hour": 7, "minute": 15},
    "envelope_rollover": {"trigger": "cron", "day": "1", "hour": 0, "minute": 0},
    "index_currency_weights": {"trigger": "cron", "day": "3", "hour": 5, "minute": 15},
    "insider_trading_refresh": {"trigger": "cron", "hour": 4, "minute": 15},
    "analyst_estimates_snapshot": {"trigger": "cron", "hour": 4, "minute": 45},
    "telegram_daily_reminder": {"trigger": "cron", "hour": 20, "minute": 0},
    "telegram_polling": {"trigger": "interval", "minutes": 1},
    "regime_refit": {"trigger": "cron", "day_of_week": "sun", "hour": 6, "minute": 0},
    "watchlist_price_checker": {"trigger": "cron", "minute": "*/30"},
    "daily_position_snapshot": {"trigger": "cron", "hour": 20, "minute": 0},
    "rss_refresh": {"trigger": "cron", "minute": "*/30"},
    "snapshot_paper_portfolios": {"trigger": "cron", "hour": 0, "minute": 0},
    "price_backfill_daily": {"trigger": "cron", "hour": 6, "minute": 30},
    "scalable_sync": {"trigger": "interval", "hours": 1},
    "llm_mandate_a_review": {
        "trigger": "cron",
        "day_of_week": "tue",
        "hour": 18,
        "minute": 30,
    },
    "llm_mandate_b_review": {
        "trigger": "cron",
        "day_of_week": "fri",
        "hour": 18,
        "minute": 30,
    },
    "llm_review_scoring": {
        "trigger": "cron",
        "day_of_week": "sun",
        "hour": 7,
        "minute": 0,
    },
    "discovery_resolution": {"trigger": "cron", "hour": 2, "minute": 0},
    "trust_daily_ledger": {"trigger": "cron", "hour": 2, "minute": 40},
    "trust_weekly_ledger": {
        "trigger": "cron",
        "day_of_week": "sat",
        "hour": 3,
        "minute": 40,
    },
    "discover_refresh": {
        "trigger": "cron",
        "day_of_week": "mon",
        "hour": 5,
        "minute": 0,
    },
    "discovery_config_review": {
        "trigger": "cron",
        "day_of_week": "sun",
        "hour": 4,
        "minute": 0,
    },
    "discover_ml_training": {
        "trigger": "cron",
        "day_of_week": "sat",
        "hour": 3,
        "minute": 0,
    },
    "advisor_rl_training": {
        "trigger": "cron",
        "month": "2,5,8,11",
        "day": 2,
        "hour": 6,
        "minute": 0,
    },
}


def _drive_all_registrations(sched):
    """Call every public register_*_job against *sched*; return raw returns.

    Phase E relocation: registrars live in their owning contexts (package
    ``jobs.py`` modules) or the composition root (``app.worker``) instead of
    ``services/jobs.py``. The EXPECTED_REGISTRATIONS contract below is
    untouched — only the import paths here changed.
    """
    returns = {}
    returns["alphacrafter_daily"] = register_alphacrafter_daily_job(sched)
    returns["alphacrafter_tuning"] = register_alphacrafter_tuning_job(sched)
    returns["provider_health_probe"] = register_provider_health_probe_job(sched)
    returns["advisor_cycle"] = register_advisor_cycle_job(sched)
    returns["evolution_round"] = register_evolution_job(sched)
    returns["macro_refresh"] = register_macro_refresh_job(sched)
    returns["risk_free_rate_refresh"] = register_risk_free_rate_refresh_job(sched)
    returns["evidence_gate"] = register_evidence_gate_job(sched)
    returns["regime_daily"] = register_regime_daily_job(sched)
    returns["envelope_rollover"] = register_monthly_envelope_rollover_job(sched)
    returns["telegram_daily_reminder"] = register_telegram_daily_reminder_job(sched)
    returns["telegram_polling"] = register_telegram_polling_job(sched)
    returns["regime_refit"] = register_regime_refit_job(sched)
    returns["watchlist_price_checker"] = register_watchlist_price_checker_job(sched)
    returns["daily_position_snapshot"] = register_daily_position_snapshot_job(sched)
    returns["rss_refresh"] = register_rss_refresh_job(sched)
    returns["snapshot_paper_portfolios"] = register_snapshot_paper_portfolios_job(sched)
    returns["price_backfill_daily"] = register_price_backfill_daily_job(sched)
    returns["scalable_sync"] = register_scalable_sync_job(sched)
    returns["llm_portfolio_review_jobs"] = register_llm_portfolio_review_jobs(sched)
    returns["llm_review_scoring"] = register_llm_review_scoring_job(sched)
    returns["discovery_resolution"] = register_discovery_resolution_job(sched)
    returns["trust_daily_ledger"] = register_trust_daily_ledger_job(sched)
    returns["trust_weekly_ledger"] = register_trust_weekly_ledger_job(sched)
    returns["discover_refresh"] = register_discover_refresh_job(sched)
    returns["discovery_config_review"] = register_discovery_review_job(sched)
    returns["discover_ml_training"] = register_discover_ml_training_job(sched)
    returns["analyst_estimates_snapshot"] = register_analyst_estimates_snapshot_job(sched)
    returns["advisor_rl_training"] = register_advisor_rl_training_job(sched)
    returns["listing_currency_audit"] = register_listing_currency_audit_job(sched)
    returns["index_currency_weights"] = register_index_currency_weights_job(sched)
    returns["insider_trading_refresh"] = register_insider_trading_refresh_job(sched)
    return returns


def test_worker_registration_matches_snapshot():
    sched = RecordingScheduler()
    returns = _drive_all_registrations(sched)

    # Interval job with a first run at registration (worker start): its
    # next_run_time is "now", so it is checked apart from the literal.
    audit = sched.registered.pop("listing_currency_audit")
    assert audit.pop("next_run_time") is not None
    assert audit == {"trigger": "interval", "hours": 24}
    assert returns.pop("listing_currency_audit") == "listing_currency_audit"

    assert sched.registered == EXPECTED_REGISTRATIONS

    # Every registrar returns the id it registered (review jobs register a
    # pair and return both, comma-joined).
    assert returns["llm_portfolio_review_jobs"] == (
        "llm_mandate_a_review,llm_mandate_b_review"
    )
    for job_id in EXPECTED_REGISTRATIONS:
        if job_id.startswith("llm_mandate"):
            continue
        assert returns[job_id] == job_id


def test_generic_primitives_passthrough_triggers():
    sched = RecordingScheduler()

    def _noop() -> None:
        pass

    returned_cron = jobs.register_cron_job(
        "gen_cron", _noop, scheduler=sched,
        hour=1, minute=2, second=3, day_of_week="mon", day="1", month="2",
    )
    returned_interval = jobs.register_interval_job(
        "gen_interval", _noop, scheduler=sched, minutes=5,
    )
    returned_interval_hours = jobs.register_interval_job(
        "gen_interval_h", _noop, scheduler=sched, hours=4,
    )

    assert returned_cron == "gen_cron"
    assert returned_interval == "gen_interval"
    assert returned_interval_hours == "gen_interval_h"
    assert sched.registered == {
        "gen_cron": {
            "trigger": "cron",
            "hour": 1,
            "minute": 2,
            "second": 3,
            "day_of_week": "mon",
            "day": "1",
            "month": "2",
        },
        "gen_interval": {"trigger": "interval", "minutes": 5},
        "gen_interval_h": {"trigger": "interval", "hours": 4},
    }


def test_track_wrapping_semantics():
    """track=True wraps the callable (JobRun tracking); track=False passes it through."""

    class FnCapturingScheduler(RecordingScheduler):
        def __init__(self):
            super().__init__()
            self.fns = {}

        def add_job(self, fn, **kwargs):
            self.fns[kwargs["id"]] = fn
            return super().add_job(fn, **kwargs)

    sched = FnCapturingScheduler()

    def _bare() -> None:
        pass

    jobs.register_cron_job("tracked", _bare, scheduler=sched, hour=1)
    jobs.register_cron_job("untracked", _bare, scheduler=sched, hour=1, track=False)
    jobs.register_interval_job("itracked", _bare, scheduler=sched, minutes=1)
    jobs.register_interval_job("iuntracked", _bare, scheduler=sched, minutes=1, track=False)

    assert sched.fns["tracked"] is not _bare
    assert sched.fns["untracked"] is _bare
    assert sched.fns["itracked"] is not _bare
    assert sched.fns["iuntracked"] is _bare


def test_telegram_reminder_noop_on_empty_db():
    """send_telegram_daily_reminders over an empty DB reminds nobody (no network)."""
    db = _memory_db()
    assert send_telegram_daily_reminders(db) == {"reminded": 0}


# -- Scheduler defaults (audit D2) -------------------------------------------
# APScheduler's own defaults (misfire_grace_time=1 s, 10 threads) dropped runs
# whenever the pool was busy at a trigger minute. The worker's scheduler and the
# API's on-demand scheduler now share explicit defaults, pinned here.

EXPECTED_SCHEDULER_DEFAULTS = {"misfire_grace_time": 300, "coalesce": True, "max_instances": 1}


def test_scheduler_defaults_snapshot():
    assert jobs.SCHEDULER_JOB_DEFAULTS == EXPECTED_SCHEDULER_DEFAULTS
    assert jobs.WORKER_THREAD_POOL_SIZE == 16
    # A caller mutating the returned dict must not change the shared defaults.
    jobs.scheduler_job_defaults()["misfire_grace_time"] = 1
    assert jobs.SCHEDULER_JOB_DEFAULTS == EXPECTED_SCHEDULER_DEFAULTS


def test_worker_scheduler_uses_the_defaults_and_a_sized_pool():
    sched = jobs.build_worker_scheduler()

    assert sched._job_defaults == EXPECTED_SCHEDULER_DEFAULTS
    executor = sched._executors["default"]
    assert executor._pool._max_workers == jobs.WORKER_THREAD_POOL_SIZE
    assert str(sched.timezone) == "UTC"


def test_defaults_reach_the_jobs_of_a_running_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(job_defaults=jobs.scheduler_job_defaults())
    sched.start(paused=True)
    try:
        job = sched.add_job(lambda: None, trigger="interval", minutes=15, id="probe")
        assert job.misfire_grace_time == 300
        assert job.coalesce is True
        assert job.max_instances == 1
    finally:
        sched.shutdown(wait=False)


def test_worker_build_scheduler_registers_every_job_on_the_configured_scheduler():
    from app import worker

    sched = worker.build_scheduler()

    assert sched._job_defaults == EXPECTED_SCHEDULER_DEFAULTS
    registered = {job.id for job in sched.get_jobs()}
    # Every snapshot job is registered by the real composition root, and the
    # pool is sized for the job count (a thread per ~3 jobs). ``rss_refresh`` has
    # a registrar but the worker deliberately does not schedule it: it refreshes
    # a process-local in-memory feed cache that only the API process reads.
    assert set(EXPECTED_REGISTRATIONS) - {"rss_refresh"} <= registered
    assert "rss_refresh" not in registered
    assert len(registered) <= 3 * jobs.WORKER_THREAD_POOL_SIZE
