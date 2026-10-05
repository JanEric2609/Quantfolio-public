# 0006 — ECB SDMX-JSON Parsing Fix, Regime Index Cutover, Job-Failure Alerting

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** `docs/archive/audits/2026-08-comprehensive-audit.md` (deep audit
2026-08-31), memory `project_quantfolio_2026-08-31_deep_audit.md` §"Bonus,
unprompted but verified live bug" and §"DE vs US scoping"
**Consumed by:** `backend/app/services/providers/ecb_provider.py`,
`backend/app/services/regime/classifier.py`, `backend/app/services/settings.py`,
`backend/app/services/jobs.py`, `backend/app/services/notify.py`

## Context

Three related, independently-discovered defects were bundled into one fix
because they share a root cause category (silent production failures with no
operator-visible signal) and one of them (the ECB parser) sits directly
upstream of the regime-index default:

1. **ECB SDW parser bug (live, silent, since ≥2026-08-24).**
   `ecb_provider.py::_fetch_sdw_series` read observations from
   `dataset["observations"]`. The real ECB Data Portal SDMX-JSON response
   nests them one level deeper: `dataset["series"]["<seriesKey>"]["observations"]`.
   The bug always returned zero observations and `ok=False` even though the
   underlying HTTP call succeeded (200, valid payload) — so
   `register_risk_free_rate_refresh_job` failed every day without ever
   writing `risk_free_rate_pct`, and every Sharpe/Sortino/alpha/Treynor/
   skfolio calculation silently ran on `FALLBACK_RISK_FREE_RATE=0.022`
   instead of the live ~2.19% €STR.
2. **`regime_index_symbol` defaulted to `SPY`.** A US index driving the
   crisis/regime classifier for a EUR/DE-scoped portfolio (`currency=EUR`,
   `tax_residency_country=DE`, §20 InvStG tax cockpit, DKB-FinTS-only
   integration) is a scoping mismatch independent of the ECB bug, but was
   surfaced by the same audit pass.
3. **No generic job-failure alerting.** Both of the above went unnoticed for
   days/permanently because `JobRun` rows recorded `status="error"` but
   nothing routed that failure anywhere a person would see it — the same gap
   would recur for the next silently-failing scheduled job.

## Decision

**1. Fix the parser, no historical backfill.** `_fetch_sdw_series` now
iterates `dataset["series"].values()` and reads each series' own
`observations` map. `risk_free_rate_pct` starts populating going forward
from real ECB data; no attempt is made to backfill the gap window, since the
fallback rate was close in value to the real rate for the affected period
and backfilling would require re-deriving values that were never actually
observed by the job.

**2. Hard cutover of `regime_index_symbol` default: `SPY` → `^STOXX50E`.**
Changed in the single source of truth (`services/settings.py`
`DEFAULT_PUBLIC_SETTINGS`) plus the two defensive `.get(..., "SPY")`
fallbacks in `classifier.py` that mirror it. Clean cutover, no dual-running,
no history recompute — any user who explicitly set `regime_index_symbol` in
their own settings is unaffected (DB AppSetting still wins over this
default); this only changes the out-of-the-box behavior for the fresh-DB
case that the audit found no persisted override for.

**3. Generic job-failure alerting in `services/jobs.py::_track_job`.** On
the `except Exception` branch (i.e. `run.status = "error"`), fan a bell
notification out to every user via the existing `Nudge` + `notify.py` bell
channel (same mechanism already used for `budget_threshold` and
`watchlist_buy_alert`), rather than building a bespoke alert path for this
one job. Best-effort and isolated: alerting failures are caught and logged,
never allowed to mask or overwrite the JobRun's own error status. This is
deliberately generic infra in `jobs.py` (not scoped to the ECB job) so the
next silently-failing scheduled job surfaces the same way.

## Alternatives considered

**Backfill `risk_free_rate_pct` for the outage window.** Rejected: no
record of what the real daily €STR was during the gap on our side (the
whole point of the bug is we never captured it), and reconstructing it from
ECB's published series after the fact adds complexity for a rate that was
already close to the fallback value in that window.

**Route the ECB fetch through OpenBB's `ecb` extension instead of fixing
the direct call.** Rejected here — that's Topic 5 (OpenBB wiring) scope,
which explicitly keeps the ECB fetch on direct-download per its own ADR.
Fixing the existing direct parser first is the smaller, faster, verifiable
change; OpenBB routing is evaluated separately.

**Job-specific alerting only for the risk-free-rate job.** Rejected: the
audit's finding was structural (no operator visibility into ANY scheduled
job failure), not specific to this one job. A bespoke alert would fix the
symptom the audit happened to find, not the class of bug.

**Recompute historical regime snapshots under the new index.** Rejected:
explicitly out of scope per the locked-in decision — the cutover is
forward-only, matching the no-backfill stance on the ECB rate fix.

## Consequences

- `risk_free_rate_pct` should begin populating on the next scheduled run
  after deploy; verify via prod smoke check (`journalctl` for
  `risk_free_rate_refresh`, or `GET` the relevant settings endpoint) rather
  than assuming from the code fix alone.
- Every quant metric consuming `get_risk_free_rate()` (Sharpe, Sortino,
  alpha, Treynor, skfolio optimisers) will shift slightly once live €STR
  starts flowing, tracking real rate moves instead of a frozen constant.
- Regime/crisis classification for fresh deployments now tracks Euro Stoxx
  50 instead of the S&P 500 — closer to the portfolio's actual currency and
  market exposure, at the cost of a discontinuity for any deployment that
  was implicitly relying on the SPY default without setting it explicitly
  (mitigated: any deployment that cared enough to notice already has bar
  history for whichever symbol it uses).
- Every scheduled job failure now produces a bell notification per user;
  this will surface latent failures the audit did not have time to find
  (a feature, not a regression) and adds one notification write per job
  failure — negligible volume for a personal-scale app.
- Regression coverage: `tests/test_ecb_provider.py` (real nested SDMX-JSON
  shape), `tests/test_jobs.py::TestTrackJob::test_failure_alerts_every_user_via_bell`
  / `test_success_does_not_alert`, plus the regime test fixtures
  (`test_regime_jobs.py`, `test_regime_hmm.py`, `test_regime_input_hygiene.py`,
  `test_regime_crisis_classifier.py`) updated to seed the new default symbol.
