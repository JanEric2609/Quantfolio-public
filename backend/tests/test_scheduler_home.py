from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # backend/

def _read(rel):
    return (ROOT / rel).read_text()

def test_api_does_not_register_cron():
    src = _read("app/main.py")
    for name in ("register_alphacrafter_daily_job", "register_verification_daily_job", "register_meta_learner_weekly_job", "register_regime_change_detector_job"):
        assert name not in src, f"{name} should not be in main.py"

def test_worker_registers_cron():
    src = _read("app/worker.py")
    assert "register_alphacrafter_daily_job" in src
    assert "register_regime_daily_job" in src
    assert "register_regime_refit_job" in src

def test_worker_registers_alphacrafter_job_unconditionally():
    """Gate retirement: alphacrafter_daily must be registered UNCONDITIONALLY
    in the worker — no experimental flag remains. (verification_refresh was
    removed with the confidence score it computed.)"""
    src = _read("app/worker.py")
    assert "is_experimental_enabled" not in src, "experimental gate must be gone from worker.py"
    for name in ("register_alphacrafter_daily_job",):
        # Exact single-indent match proves the call sits outside any conditional.
        call_line = f"    {name}(scheduler)"
        assert call_line in src.splitlines(), f"{name} must be called unconditionally in build_scheduler"


def test_worker_registers_price_backfill_daily_job():
    """Regression test: ``register_price_backfill_daily_job`` was defined
    (2026-08-26, Phase E decision-loop independence) to keep
    ``regime_index_symbol``'s price bars fed daily, but the call into
    ``build_scheduler`` was never added — the job existed and was unit-tested
    in isolation (test_jobs_registration_snapshot.py drives every registrar
    directly against a stub scheduler) but nothing asserted build_scheduler
    itself wires it up, so it silently never ran in production. Bars for the
    configured index symbol went stale for weeks with no operator-visible
    signal, and the regime classifier kept scoring off an old snapshot."""
    src = _read("app/worker.py")
    call_line = "    register_price_backfill_daily_job(scheduler)"
    assert call_line in src.splitlines(), (
        "register_price_backfill_daily_job must be called in build_scheduler"
    )
