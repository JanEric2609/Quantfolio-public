# Research: engineering, frontend, Learn tab, compute (2026-11-12)

## READ THIS FIRST: limits of this report
- Method: WebSearch only (standard mode). I never opened a page with WebFetch. Every "[n]" claim rests on a search-result snippet and the search tool's summary, not on a full-page read. In the URL list each entry is marked S (snippet only). No entry is marked R.
- The search quota ran out ("5-hour limit") partway through the last round. These topics were NOT searched and are marked UNVERIFIED where I mention them from memory: ruff BLE001/blind-except, pytest skip/xfail/--strict-markers/-rs, Nielsen Norman empty/error states, financial chart UX (log scale, adjusted prices, candlesticks), FSRS, Sandpack, Sweller/Kalyuga, regression-to-the-mean misconception research, eslint-plugin-tailwindcss/stylelint, responsive tables, Ilmanen and Grinold-Kahn, Hypothesis-for-finance properties (the properties below are my own design, not sourced), Timescale tuning, Ray/Dask, GPU sharing, Airflow-lite specifics for Huey/RQ/systemd timers.
- 106 distinct URLs are listed (target was 70+), but none was page-read, and about 25 are low-authority. Treat the count as "surfaced", not "verified".

---
## 1. CI without hosted minutes

Findings
- GitHub Free private repos: 2,000 min/month, 500 MB artifacts, 10 GB cache; spending limit defaults to $0 so jobs stop at the cap; larger runners are always billed [6]. A news item says GitHub planned a $0.002/min platform charge for self-hosted runners on private repos; I could not confirm date or status [7] (low authority; check GitHub's pricing page before relying on self-hosted being free).
- Self-hosted runners: GitHub's own security text (quoted by a blog) says they carry no clean-VM guarantee and can be persistently compromised by untrusted workflow code; public repos are the main warning [5]. JIT/ephemeral runners deregister after one job and avoid long-lived registration tokens [1]. Reused hardware still needs a clean environment per job [1][2][4]. Practitioner guidance: least privilege, short-lived tokens, block metadata/egress, protect workflow files with branch protection [2][4][5].
- For a one-person private repo the realistic threat is a malicious or compromised dependency or workflow edit, not a stranger's PR. A runner in an unprivileged LXC with no access to prod secrets, no route to the other Quantfolio LXCs and the DKB/broker credentials, reset from a snapshot per run, covers that.
- Forgejo Actions: runner installed separately from the Forgejo instance (docs advise not on the same machine), supports an `lxc` label type, needs passwordless sudo for `lxc-*` on a Debian host [8][9]. Docs versions disagree on default release (bullseye vs bookworm) [9].
- Woodpecker: Forgejo support documented but flagged experimental; needs OAuth app and a webhook allow-list if co-hosted [10].
- Local gates: lefthook supports `pre-push`, `parallel: true`, `lefthook-local.yml`, tags to skip, `lefthook validate` [11][12]. `act` runs workflows in Docker but is not faithful for secrets, cache, services, permissions [13][14][15].
- Note: the repo already uses a public mirror for CI (your memory file). That is free and unlimited for public repos [6]. It stays the simplest option.

Recommended setup (simplest safe)
1. Keep public-mirror CI as the authoritative gate (0 h).
2. Add a local pre-push gate with lefthook (2-3 h): `ruff check app/`, `lint-imports`, `check_no_new_cycles.py`, `pytest -n 6 -x --lf` on changed areas, `tsc`/`vitest related`. Full ~10-min suite in a `make ci-local` or `just ci` recipe, run before merge. Snippet:
```yaml
# lefthook.yml
pre-push:
  parallel: true
  jobs:
    - name: ruff
      run: cd backend && .venv/bin/ruff check app/
    - name: imports
      run: cd backend && .venv/bin/lint-imports && .venv/bin/python scripts/check_no_new_cycles.py
    - name: tsc
      run: cd frontend && npx tsc --noEmit
```
3. Only if you want PR-triggered private CI: a single ephemeral JIT-runner LXC (unprivileged, snapshot rollback per job, no secrets, firewall to internet only) (6-10 h); or Forgejo + runner as a second option (8-12 h) [8]. Not recommended before step 2 proves insufficient.
Benefit: catches the gates AGENTS.md lists before a push; removes the dependence on minutes. Expected: 2-3 h for 80% of the value.
Counter-arguments: hooks are bypassable (`--no-verify`) and drift from CI; act is not CI-equivalent [13]; self-hosted runner adds a persistent attack surface next to a host that holds financial credentials [5].

## 2. Test quality

Findings
- coverage.py has a single `fail_under` (exit code 2) and branch coverage is opt-in via `[run] branch = True`; there is no separate branch threshold (a quiz page claiming `fail_under_branch` is unsupported) [25].
- diff-cover: `diff-cover coverage.xml --compare-branch=origin/main --fail-under=80`; CI needs the base ref fetched [21][22]. A diff floor is more practical than a global floor on a 5,000-test suite.
- pytest-randomly reshuffles order and reseeds; with xdist the master generates one seed and sends it to workers (since 3.3.0); fix a failing order with `-p randomly --randomly-seed=N` [24].
- ruff supports the flake8-pytest-style `PT` family via `select = ["PT"]`; PT004/PT005 were removed in ruff 0.8 [23]. I did not retrieve the list of PT rules; no PT rule detects assertion-free tests (my understanding, UNVERIFIED). A custom AST check ("test function without assert/raises/approx/mock assert") in 1-2 h is the realistic route. Also UNVERIFIED: pytest `--strict-markers`, `-rs`, `xfail_strict`, `--runxfail`; use the pytest docs.
- Mutation testing: mutmut 3 (config in `[tool.mutmut]`, `paths_to_mutate`, `tests_dir`, resumable runs, glob selection, `# pragma: no mutate`; code outside functions is not mutated) [16][17]. StrykerJS has `--incremental` and a Vitest runner (needs Vitest >=2 per a mirrored changelog) [18][19]. Cost estimate for a 24-thread host is my inference: cost = (#mutants) x (test time for relevant tests); target only pure kernels (`tax_calc`, `quant_metrics`, `allocation`, `eur_prices`) not the whole 5k suite. Expect hours per module; run nightly in the idle slot and keep a surviving-mutant ledger.
- Hypothesis: `hypothesis.extra.numpy` (`arrays`, `array_shapes`, dtype strategies), designed for small data; `np.float` is gone since NumPy 1.24 [20]. Concrete properties (my design, not from a source):
  - returns: log-returns additive over time; simple returns of a constant series are 0; scaling prices by c leaves returns unchanged; EUR restatement with FX=1 is identity.
  - covariance: symmetric, PSD (min eigenvalue >= -1e-10), invariant to row permutation, diag equals variance; Ledoit-Wolf shrinkage between 0 and 1.
  - tax: tax >= 0, monotone in gain, zero below Sparerpauschbetrag, Teilfreistellung 30% on equities equals taxable x 0.7 (assert `estimate` and `not_tax_advice` are True).
  - optimisers: weights sum to 1, within bounds, permuting assets permutes weights, scaling returns by positive c does not change the min-variance portfolio.
- Metamorphic testing addresses the oracle problem; surveys: Segura et al. TSE 2016, Chen et al. ACM CSUR [92][93][94]. Use for pipelines where no exact answer exists (backtests: shuffling asset order, adding a constant-cash asset, doubling capital).
- Golden tests self-reference problem: the project already hit this (goldens captured cold). Rule: a golden must be (a) generated from an independent oracle (vectorbt harness, closed-form, or a hand-computed fixture), (b) accompanied by at least one invariant property, (c) regenerated only with a diff review, (d) with all network/cache stubbed (AGENTS.md "Tests that reach the network").
- Contract testing: Schemathesis stateful testing infers links from response schemas and `Location` headers; with typed responses there is more to infer; FastAPI typed `response_model`s feed OpenAPI, so 37% untyped responses are the gap (my inference about linking: UNVERIFIED) [51]. `openapi-typescript` generates `paths`/`components` types, static, runtime-free, pairs with `openapi-fetch` [52]. Recommended: generate `frontend/src/lib/api-types.ts` from `docs/api/openapi.json` and make a CI check that it is up to date.
- Time-dependent code: time-machine is faster than freezegun, mocks everywhere (C-level), CPython only, `travel()` replaces `freeze_time()` [90][91].
- Playwright: `toHaveScreenshot()` options (`maxDiffPixels`, `animations: 'disabled'`, `--update-snapshots`); baselines per browser/OS; flaky with dynamic data [88]. For the smoke suite, content assertions (a number is shown, no "NaN", no empty table) beat pixel diffs; use screenshots only for 3-4 stable pages with stubbed data.

Recommended order (hours / benefit)
1. diff-cover at 80% on changed lines in the public CI (2 h) - blocks new untested code.
2. AST check for assertion-free tests, run once to list offenders (2 h) - finds the real weak tests.
3. pytest-randomly in a nightly job only first (1 h) - exposes order dependence.
4. Hypothesis suite for tax_calc / quant_metrics / allocation (8-12 h).
5. mutmut on 3-4 pure modules, nightly (6 h setup).
6. openapi-typescript codegen + response_model typing sprint (10-16 h).
7. Playwright content assertions (3 h).
Counter-arguments: mutation score is noisy on numerical code (float tolerances produce equivalent mutants); global coverage floors incentivise assertion-free tests; Hypothesis is slow on large arrays [20]; xdist + randomly reseeding needs care [24].

## 3. Reliability patterns

Findings
- SRE guidance: four golden signals (latency, traffic, errors, saturation); error budget = allowed shortfall against an SLO [57] (secondary summaries in the snippet; the book itself is the primary text).
- Logging: structlog guide: JSON in prod, console in dev, bind context, canonical log lines, structured tracebacks, keep INFO meaningful [58].
- Error tracking: GlitchTip is Sentry-SDK compatible, lightweight (web, worker, Postgres, Redis), MIT licensed; sources are hosting-vendor pages (low authority) and hardware figures are unverified [53][106]. The project already has `app_errors` (fingerprint table) so a second system is probably unnecessary; fix coverage first.
- Scheduler: APScheduler 3.x `max_instances`, `coalesce`, `misfire_grace_time` are per-job or via `job_defaults` [54][55]; a third-party guide says defaults are 1 s grace, `max_instances=1`, coalesce on [56] (low authority; verify in the 3.x guide, I could not read the "missed job executions" section).
- Trading calendar: pandas-market-calendars ships calendars in the package, not live; updated hours require upgrading; mirrors exchange_calendars [59].
- Stale/negative caches, partial-failure flags, broad `except`: I did not find sources. My recommendations (UNVERIFIED as sourced practice):
  - Job result contract: `{status: ok|degraded|failed, processed, skipped, errors[], partial: bool}`; `_track_job` sets "degraded" if skipped/errors exceed a threshold and Control Center turns it amber, `self_check` pages on 2 consecutive degraded runs.
  - 300+ `except Exception`: do not blanket-fix. Add a ruff `BLE` (blind-except) baseline (UNVERIFIED rule name, check ruff docs) and ratchet like the import cycles golden; for each handler choose: narrow exception, log with `logger.exception`, or record to `app_errors`; ban `except Exception: pass`.
  - Negative caching: store `miss` rows with TTL (the migration 0154 for dead symbols is the right idea) and a `retry_after`; positive TTL keyed on last completed bar (already done in #396).
  - Time zones: keep UTC in DB, convert with exchange calendar sessions, never `date.today()`; test with time-machine [90].
Effort: job contract 6-8 h; blind-except ratchet 3 h + incremental cleanup; partial flags per endpoint 4-6 h.
Limits: more alerting without a degraded-vs-ok definition just adds noise; single-owner systems should page rarely.

## 4. Frontend quality

Findings
- WCAG 2.2 SC 1.4.3: 4.5:1 for normal text, 3:1 for large (18 pt or 14 pt bold); no dark-mode exemption [26]. The #6b6b6b on near-black claim: contrast depends on the background. Compute with the WCAG formula; for a near-black of #0a0a0a, luminance of #6b6b6b is about 0.147 giving about 4.0:1 (my calculation, check with a tool); #8a8a8a gives roughly 6:1 on #0a0a0a. Use computed tokens, not guesses. Add a unit test that walks the token pairs and asserts >= 4.5.
- APCA is a candidate for WCAG 3, not adopted; its advocate argues WCAG 2 misjudges dark mode [27]; critics: conversion to ratings unclear, values changed between versions, size-dependent [28][29]. Use WCAG 2 for conformance and APCA only as a second opinion.
- Tooltips: APG pattern is "work in progress" without task-force consensus; `role="tooltip"`, trigger has `aria-describedby`, tooltip not focusable, dismiss on blur/mouse-out/Escape; interactive content belongs in a non-modal dialog [30]. Screen-reader support for `aria-describedby` is uneven; test it.
- Localisation: `Intl.NumberFormat('de-DE', {style:'currency', currency:'EUR'})` gives "123.456,79 EUR" with a trailing space; percent style multiplies by 100 and rounds to integers unless `maximumFractionDigits` is set; example of "74 %" with a (narrow) space is from a third party [31][32]. Create one formatter module (`formatEur`, `formatPct`, `formatDate`) and ban `toFixed` in components via an ESLint `no-restricted-syntax` rule (my suggestion).
- Uncertainty display: expected frequencies ("of 100 people like you") improve understanding; evidence is mixed (ratio bias, "10 out of 100" perceived higher than "1 of 10") [33][34][35][36][37]. Hypothetical Outcome Plots (animated draws) outperform error bars and violin plots for judging reliability of ordering across two or three quantities, similar for single quantities [38][39]. Use a fan chart for the headline, HOPs on demand in Learn; never show forecast numbers with more digits than their uncertainty.
- Not searched (UNVERIFIED, from memory): NN/g loading/empty/error patterns; range selectors (1M, 6M, YTD, 1Y, 5Y, Max), log-scale toggle, label "price return" vs "total return (adjusted)"; line chart default for retail, candlestick as option; responsive tables (card layout under 640 px or horizontal scroll with sticky first column); `eslint-plugin-tailwindcss` `no-arbitrary-value` and a custom rule or `stylelint-declaration-strict-value` against raw colours.
Recommended: contrast token test (2 h), formatter module + lint rule (3-4 h), shared `<AsyncState>` component rendering loading/empty/error with retry and wired into every `useQuery` (4-6 h), glossary popovers (3-5 h).
Limits: contrast ratio is necessary, not sufficient; tooltips hide information from touch users, so definitions should also appear inline on first use.

## 5. The Learn tab

Evidence base
- Dunlosky et al. 2013 rates practice testing and distributed practice "high utility"; interleaving, self-explanation, elaborative interrogation "moderate"; highlighting, rereading, summarising low [41][42].
- Testing effect: restudy wins at 5 min but tested students retain more at 2 days and 1 week [81]. Pretesting: unsuccessful retrieval attempts before reading improve later learning, tested mainly on text facts [80].
- Interleaving: pre-registered cluster RCT, 54 Grade 7 classes, 61% vs 38%, d = 0.83, but worksheets in one district [49][50][105].
- Productive failure: meta-analysis of 53 studies, 166 comparisons, Hedges g = 0.36 favouring problem-solving-first; bigger when design follows PF principles; for simple procedures instruction-first does as well; weaker for young learners [43][44].
- Worked examples and fading: backward fading, prompted self-explanation, adaptive fading [45][46]. Expertise reversal and cognitive load theory not searched (UNVERIFIED).
- Interactive articles: Distill/Hohman et al. review design space and affordances [40]; Bret Victor: explorables must guide attention to a phenomenon, not dump a sandbox [79]; Seeing Theory (archived, D3, six chapters) as a model [78].
- Running code: Pyodide (CPython on WebAssembly with NumPy/pandas/matplotlib) [76]; marimo exports WASM notebooks (not every package works, e.g. torch; needs internet for Pyodide) [77]. Sandpack not searched (UNVERIFIED).
- Statistics education: GAISE College Report 2016 six recommendations (teach statistical thinking, focus on conceptual understanding, integrate real data, foster active learning, use technology, use assessments) [47][48] (list of six from memory of the report; snippet confirmed there are six); ASA statement on p-values six principles [95].
- Multiple testing: Harvey-Liu-Zhu: new factors need t > 3.0 [75]; Deflated Sharpe Ratio [74]; purged k-fold, embargo, CPCV in AFML chapters 7 and 12 [104][103].
- Curricula to align: Stock-Watson Part II regression chapters 4-7 [96]; Hanck et al. free R companion to Stock-Watson [67][68]; Huntington-Klein The Effect [70]; Cunningham Mixtape (free online, second edition in progress) [69]; Shiller Open Yale ECON 252 [71]; MIT 15.401 (Lo) [72] and 15.450 [73]; Pedersen Efficiently Inefficient [97][98]. Grinold-Kahn and Ilmanen not searched.

Design principles
1. Prediction first: show a chart, ask "what will the Sharpe on held-out data be?", then reveal (pretest/productive failure).
2. Worked example from the user's own portfolio, then faded versions (compute step 3 yourself, then steps 2-3, then all).
3. Show formula, plain-language sentence and the exact app function side by side; link to source line.
4. Retrieval and spacing: 3-question quiz at lesson end, resurfaced by a simple scheduler (Leitner or FSRS; FSRS not verified) after 2 d, 1 wk, 3 wk.
5. Interleave: every third exercise mixes two earlier topics; misconception cards (p-value, regression to the mean, overfitting) hit the ASA list.
6. Every number is estimated with uncertainty (bootstrap/HOP), and every lesson ends with "what this cannot tell you".

Lesson table (12-16 lessons)

| # | Lesson | Econometrics topic (first year) | App function it explains | Interactive element | Exercise | Reading |
|---|---|---|---|---|---|---|
| 1 | Returns, log vs simple | Descriptive stats, Stock-Watson ch.2-3 | `eur_prices`, `quant_metrics` returns | Slider: compounding path | Predict total return of +50%/-50% | [96] ch.2 |
| 2 | Mean, variance, Sharpe | Estimators, sampling distribution | Sharpe/Sortino in `quant_metrics` | Resample returns, watch Sharpe histogram | Guess CI width | [67] ch.2-3; [78] |
| 3 | Confidence intervals and the standard error | Ch.3 | bootstrap CI on metrics | HOP of bootstrap draws | Predict then see if a CI contains 0 | [38][96] |
| 4 | Hypothesis tests and p-values | Ch.3 | skill/harm e-process gates | Simulate null many times, count p<0.05 | Fix 3 misconception cards | [95] |
| 5 | Simple regression and OLS | Ch.4 | beta/alpha in `quant_metrics` | Drag points, see OLS line | Fit beta vs MSCI World by hand | [96] ch.4; [67] |
| 6 | Inference in regression | Ch.5 | alpha t-stat | Toggle HAC SE | Does alpha differ from 0? | [67] ch.5 |
| 7 | Multiple regression, omitted variable bias | Ch.6-7 | factor exposures (`quant_factors`) | Add FF factors, watch alpha shrink | Predict alpha change | [96] ch.6 |
| 8 | Heteroskedasticity, autocorrelation | Ch.5, 15 | HAC/Newey-West use | Residual plot toggles | Spot clustering | [67] |
| 9 | Covariance, diversification, shrinkage | multivariate stats | `allocation` Ledoit-Wolf | Slider: shrinkage intensity | Predict min-variance weights | [97][72] |
| 10 | Overfitting and out-of-sample | Ch.9 internal/external validity | walk-forward, purged CV | Fit polynomial degree vs OOS error | Pick degree before reveal | [104] |
| 11 | Multiple testing and the deflated Sharpe | Ch.7 joint tests (Bonferroni) | `resolve_n_trials`, DSR, `evidence_gate` | Run N random strategies, show best Sharpe | Predict best-of-N Sharpe | [74][75] |
| 12 | Diebold-Mariano and forecast comparison | time series Ch.15-16 | `diebold_mariano_hln`, advisor promotion | Two forecast series, loss differential plot | Who wins? | [96] |
| 13 | Forecast calibration (Mincer-Zarnowitz) | regression of outcome on forecast | `compute_mincer_zarnowitz` | Calibration plot | Is slope 1? | [96] |
| 14 | Regression to the mean and selection | Ch.4 intuition | "best recent performers" screens, Discover ranking | Pick top decile, watch next-period | Predict next-period rank | [78] |
| 15 | Causal inference vs prediction | The Effect / Mixtape | why IC and paper benchmark differ | DAG toggle: confounder | Draw DAG for "momentum works" | [70][69] |
| 16 | Taxes and costs as a regression-free calculation | none (applied) | `tax_cockpit`, `trade_check` | Edit trade, see net edge | Break-even edge | AGENTS docs (tax cockpit) |

Mastery tracking: per-lesson 3-item quiz, store correct/total and next-due date in a `learn_progress` table; do not gamify beyond a "due today" count.
Effort: framework + first 4 lessons 20-30 h; each further lesson 4-6 h. Pyodide in-browser run of a Python snippet: 8-10 h (first use). Alternative: pre-render outputs server-side, avoiding a 10+ MB runtime download.
Counter-arguments: effect sizes come mostly from school or lab settings, not self-directed adults; PF gains are for conceptual, not procedural, skills [43]; interleaving studied on maths worksheets [49]; interactivity without guidance does not explain [79].

## 6. Idle compute

Findings (sources thin; the rest is design reasoning)
- Proxmox LXC: `cores` limits visible CPUs (default all); `cpulimit` caps usage (0 = unlimited); `cpuunits` sets relative priority under contention; memory limits enforced through cgroups and OOM-kill inside the container [82]. Set `cpulimit` per container so research jobs cannot starve the web LXC; give the web/DB LXC higher `cpuunits`.
- joblib: loky default, auto-memmaps numpy arrays above `max_nbytes='1M'`, `mmap_mode='r'`, `temp_folder` / `JOBLIB_TEMP_FOLDER`, `parallel_config` for libraries [84][85]. Use `n_jobs` <= 12 and pass big panels as memmaps.
- PostgreSQL: each parallel worker takes its own `work_mem`; default `max_parallel_workers_per_gather` is 2; community rule: (max_connections + max_parallel_workers) x work_mem < 25% RAM (not official) [86][87].
- DuckDB: `ATTACH ... (TYPE postgres, READ_ONLY)` and `postgres_query` allow analytics on the live DB without writing [83]; the repo already uses duckdb for PIT joins.
- Orchestration: Prefect lighter than Dagster for function-based flows; for simple scheduling APScheduler/Celery Beat suffice [89] (low-authority comparison). Conclusion: keep APScheduler; add a `job_runs` table with result contract (Section 3); systemd timers only for jobs that must run outside the worker (as `self_check` already does).
- Research uses of idle compute (design reasoning, UNVERIFIED as "what groups spend compute on"): simulation-based calibration of your own tests (simulate under the null, check false-positive rate of the gates), bootstrap CIs for every headline metric, placebo/null runs (random-signal strategies feed the trial ledger and the DSR), walk-forward re-estimation, data-reconciliation jobs (two vendors, flag differences), hyper-parameter search counted in `n_trials` [74][75][103].
- GPU/second small model: not searched; keep embedding model on the 3070 only if VRAM is free after the LLM; UNVERIFIED.
Recommended: 1) null-simulation harness for evidence gate (10-16 h, high value), 2) cpulimit/cpuunits review (1 h), 3) bootstrap CI everywhere (6 h).
Limits: more nightly compute increases the number of trials; every extra search must be logged to the ledger or it raises false discovery.

## 7. Dependency hygiene

- npm: `npm audit --omit=dev` restricts to production deps; overrides are in package.json (since npm 8.3 from memory) and may not apply to optional deps; re-audit after install and check the lockfile [60]. Rule: dev-only advisories (build tooling not shipped) are acknowledged in a documented allow-list with expiry date; prod ones fixed or overridden.
- pip-audit (PyPA) reads requirements and pyproject; can use PyPI or OSV as service; check Pipfile.lock claim (conflicting) [61][62]. OSV-Scanner is Google's cross-language alternative (no doc retrieved).
- Renovate: `packageRules` with `groupName`; Dependabot has groups; GA timing of cross-ecosystem grouping disputed [63]; PR-volume claim (40-60 vs 8-15) is anecdotal.
- uv: `uv lock`, `uv sync --locked` (assert up to date), `--frozen` (no update), lock prefers earlier versions [64]. Use `--locked` in CI.
- Version caps: Schreiner argues against upper caps generally (esp. Python), caps are not overridable by users; counter-view exists [65][66]. For an application (not a library) caps with a dated reason plus a scheduled review are reasonable: the AGENTS.md table already documents reasons. Add a monthly job "try to lift caps in a scratch venv, run suite" (4 h).

## 8. Operational

- Demo/test account: the standard approach is a separate role/schema with synthetic data, not masked prod rows (reasoning). Postgres: `pg_read_all_data` (PG14+) gives read access but does not block writes; `default_transaction_read_only` is only a default, the user can switch it off; PG15+ removed CREATE on `public` from PUBLIC [99][100]. So: a diagnostic DB role with SELECT on views that exclude broker/tax tables, plus app-level `demo` user bound to a seeded SQLite/Postgres schema (`seed_smoke_db.py` already exists). Never expose a login that can read `live_positions`.
- Backup verification: `pg_restore --list` plus checksum, then a scratch restore with row counts; pg_dump tests do not cover PITR/WAL; quarterly drill; a blog claims pgBackRest was archived (April 2026) which I could not confirm [101][102]. Low-authority sources only; take procedure from PostgreSQL docs.
- Runbooks: one page per alert in Control Center (symptom, check, fix, who). 6-8 h.

---
## Top recommendations (value per effort)
See final message.

## Claims I could not verify
- GitHub's $0.002/min self-hosted charge [7].
- mutmut and Stryker runtime on this machine (no benchmark found).
- Any PT ruff rule that detects assertion-free tests.
- Contrast numbers for #6b6b6b (computed by me, not sourced).
- GlitchTip hardware needs [53][106].
- pgBackRest archival claim [101].
- APScheduler default values [56].
- Existence of a `fail_under_branch` option (contradicted).
- Everything marked UNVERIFIED above.

## URL list (S = search snippet only; R = page read). No page was read.
1 S https://github.blog/changelog/2023-06-02-github-actions-just-in-time-self-hosted-runners/ - JIT runners changelog (primary)
2 S https://appsec.equinor.com/guidelines/gh-actions-runners - Equinor AppSec runner guideline (org guidance)
3 S https://livebook.manning.com/book/github-actions-in-action/chapter-6 - book chapter on self-hosted runners
4 S https://tenki.cloud/blog/self-hosted-runners-arc-security-guide - ARC security (vendor, low)
5 S https://dev.to/tildalice/github-actions-self-hosted-runners-3-security-risks-36oj - 3 risks (low)
6 S https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions - Actions billing (primary)
7 S https://itbrief.co.uk/story/github-cuts-actions-runner-prices-adds-new-usage-fee - news on runner pricing (low)
8 S https://forgejo.org/docs/v14.0/admin/actions/runner-installation/ - Forgejo runner install (primary)
9 S https://forgejo.org/docs/latest/admin/actions/configuration/ - Forgejo runner config/labels (primary)
10 S https://woodpecker-ci.org/docs/administration/configuration/forges/forgejo - Woodpecker Forgejo (primary)
11 S https://lefthook.dev/usage/commands.html - lefthook commands (primary)
12 S https://lefthook.dev/usage/features.html - lefthook features (primary)
13 S https://oneuptime.com/blog/post/2026-02-08-how-to-run-act-to-test-github-actions-locally-in-docker/view - act guide (low)
14 S https://contribute.sonatype.com/docs/how-to/testing-github-actions-locally/ - Sonatype on act
15 S https://man.archlinux.org/man/act.1.en.txt - act man page
16 S https://pypi.org/project/mutmut/ - mutmut PyPI
17 S https://github.com/Otto-AA/mutmut - mutmut repo mirror (verify upstream boxed/mutmut)
18 S https://stryker-mutator.io/docs/stryker-js/incremental/ - Stryker incremental (primary)
19 S https://stryker-mutator.io/docs/stryker-js/vitest-runner/ - Stryker vitest runner (primary)
20 S https://hypothesis.readthedocs.io/en/latest/numpy.html - Hypothesis numpy (primary)
21 S https://sources.debian.org/src/diff-cover/10.2.0-1/README.rst - diff-cover README
22 S https://manpages.ubuntu.com/manpages/questing/man1/diff-cover.1.html - diff-cover man
23 S https://codspeed.io/astral-sh/ruff/branches/alex/remove-pt-rules - ruff PT004/5 removal (low)
24 S https://pypi.org/project/pytest-randomly - pytest-randomly
25 S https://coverage.readthedocs.io/en/6.5.0/config.html - coverage.py config (older version)
26 S https://w3c.github.io/wcag/understanding/contrast-minimum.html - WCAG 1.4.3 (standard)
27 S https://www.github.com/Myndex/SAPC-APCA/blob/master/documentation/WhyAPCA.md - APCA advocacy
28 S https://www.yatil.net/blog/wcag-3-is-not-ready-yet - individual opinion
29 S https://www.w3.org/WAI/GL/task-forces/silver/wiki/Visual_Contrast_of_Text_Subgroup/Conformance - W3C wiki
30 S https://www.w3.org/WAI/ARIA/apg/patterns/tooltip - APG tooltip (standard)
31 S https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/NumberFormat/NumberFormat - MDN NumberFormat
32 S https://frontendchecklist.io/rules/i18n/currency-formatting - checklist (low)
33 S https://core.ac.uk/download/267566522.pdf - Spiegelhalter & Gage paper
34 S https://icots.info/icots/9/data/ICOTS9_PL2_SPIEGELHALTER.pdf - ICOTS9 plenary
35 S https://www.efsa.europa.eu:443/sites/default/files/event/180918-conference/presentations/18-0_04_Spiegelhalter.pdf - EFSA slides
36 S https://plus.maths.org/issue50/risk - Plus magazine
37 S https://www.annfammed.org/lookup/volpage/6/196 - Ann Fam Med commentary
38 S https://idl.uw.edu/papers/hops - HOPs paper page
39 S https://mucollective.northwestern.edu/project/hops - HOPs lab page
40 S https://distill.pub/2020/communicating-with-interactive-articles/ - Distill article
41 S https://stafforini.com/works/dunlosky-2013-improving-students-learning/ - Dunlosky 2013 record
42 S https://www.psychologicalscience.org/news/releases/which-study-strategies-make-the-grade.html - APS release
43 S https://phys.org/news/2021-09-productively-wiser.html - press on meta-analysis (low)
44 S https://www.x-mol.com/paper/1406031499744456704 - Sinha & Kapur record
45 S https://link.springer.com/doi/10.1007/s11251-009-9107-8 - adaptive fading study
46 S https://en.wikipedia.org/wiki/Worked-example_effect - Wikipedia (low)
47 S https://www.amstat.org/gaise - ASA GAISE
48 S https://amstat.org/asa/files/pdfs/GAISE/GAISECollege_Full.pdf - GAISE 2016 PDF
49 S https://link.springer.com/doi/10.1007/s10648-020-09516-2 - 2020 interleaving review
50 S https://ies.ed.gov/use-work/awards/systematic-replication-study-interleaved-mathematics-practice - IES replication
51 S https://schemathesis.readthedocs.io/en/stable/guides/stateful-testing/ - Schemathesis stateful
52 S https://openapi-ts.dev/about - openapi-typescript
53 S https://railway.com/deploy/glitchtip-error-tracking.md - GlitchTip template (low)
54 S https://apscheduler.readthedocs.io/en/3.x/userguide.html - APScheduler 3 guide
55 S https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/base.html - add_job params
56 S https://cronuru.com/guides/apscheduler - third-party guide (low)
57 S https://oreilly.com/ideas/monitoring-distributed-systems - SRE monitoring chapter
58 S https://www.structlog.org/en/stable/logging-best-practices.html - structlog
59 S https://pandas-market-calendars.readthedocs.io/en/latest/index.html - market calendars
60 S https://docs.npmjs.com/cli/v11/commands/npm-audit/ - npm audit
61 S https://github.com/pypa/gh-action-pip-audit - pip-audit action
62 S https://pypi.org/p/pip-audit - pip-audit
63 S https://docs.renovatebot.com/bot-comparison/ - Renovate comparison (vendor)
64 S https://docs.astral.sh/uv/concepts/projects/sync/ - uv sync
65 S https://prefix.dev/blog/the_python_packaging_debate - upper-bound debate (vendor blog)
66 S https://simonwillison.net/2022/Sep/5/upper-bound-version-constraints/ - link blog to Schreiner essay (secondary)
67 S https://bookdown.org/machar1991/ITER/ - Intro to Econometrics with R
68 S https://openeconomics.zbw.eu/en/2024/05/open-textbook-introduction-to-econometrics-with-r/ - ZBW announcement
69 S https://mixtape.scunning.com/ - Causal Inference: The Mixtape
70 S https://newbooksnetwork.com/the-effect - The Effect (interview page; free-online URL not verified)
71 S https://oyc.yale.edu/economics/econ-252-11/lecture-1 - Shiller ECON 252
72 S https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/resources - MIT 15.401
73 S https://ocw.mit.edu/courses/15-450-analytics-of-finance-fall-2010/ - MIT 15.450
74 S https://papers.ssrn.com/abstract=2460551 - Deflated Sharpe Ratio
75 S https://www.nber.org/papers/w20592 - Harvey-Liu-Zhu
76 S https://cdn.jsdelivr.net/gh/pyodide/pyodide@main/README.md - Pyodide README
77 S https://docs.marimo.io/guides/wasm/ - marimo WASM
78 S https://seeing-theory.brown.edu/ - Seeing Theory (archived)
79 S https://worrydream.com/ExplorableExplanations/ - Bret Victor
80 S https://learninglab.uchicago.edu/Pre-Testing_files/RichlandKornellKao.pdf - pretesting paper
81 S https://pubmed.ncbi.nlm.nih.gov/16507066/ - Roediger & Karpicke 2006
82 S https://pve.proxmox.com/pve-docs/pct.1.html - pct manual
83 S https://duckdb.org/docs/current/core_extensions/postgres/overview - DuckDB postgres
84 S https://joblib.readthedocs.io/en/stable/generated/joblib.Parallel.html - joblib Parallel
85 S https://joblib.readthedocs.io/en/latest/auto_examples/parallel_memmap.html - joblib memmap
86 S https://postgresqlco.nf/doc/en/param/max_parallel_workers_per_gather/ - param reference
87 S https://www.crunchydata.com/blog/parallel-queries-in-postgres - Crunchy blog
88 S https://playwright.dev/docs/api/class-snapshotassertions - Playwright snapshots
89 S https://dzone.com/articles/airflow-vs-dagster-vs-prefect-which-scheduler-fits - orchestrator comparison (low)
90 S https://time-machine.readthedocs.io/en/latest/comparison.html - time-machine vs freezegun
91 S https://adamj.eu/tech/2021/02/19/freezegun-versus-time-machine/ - benchmark post
92 S https://eprints.whiterose.ac.uk/110335/1/segura16-tse.pdf - Segura MT survey
93 S https://research.nottingham.edu.cn:443/files/31448151/51607.pdf - Chen MT review
94 S https://arxiv.org/pdf/1907.09427 - ML testing mapping study
95 S https://www.sciencedaily.com/releases/2016/03/160307092305.htm - ASA p-value statement news (primary statement not opened)
96 S https://stock.scholars.harvard.edu/pages/introduction-econometrics.md - Stock-Watson page
97 S https://blogs.cfainstitute.org/investor/2017/06/15/book-review-efficiently-inefficient - review of Pedersen
98 S https://www.lhpedersen.com/books - Pedersen books page
99 S https://www.crunchydata.com/blog/creating-a-read-only-postgres-user - read-only user
100 S https://postgresqlco.nf/doc/en/param/default_transaction_read_only/ - parameter reference
101 S https://oneuptime.com/blog/post/2026-01-21-postgresql-backup-testing/view - backup testing (low)
102 S https://dev.to/piteradyson/postgresql-backup-verification-how-to-test-and-validate-your-postgresql-backups-b2f - backup verification (low)
103 S https://reasonabledeviations.com/notes/adv_fin_ml - AFML notes (low)
104 S https://www.lehmanns.de/isbn/978-1-119-48208-6 - AFML table of contents (publisher listing)
105 S https://zendy.io/pdf-viewer/10.1037/edu0000367 - Rohrer et al. 2020 record
106 S https://skills.sh/lu1sdv/skillsmd/glitchtip - GlitchTip listing (low)
