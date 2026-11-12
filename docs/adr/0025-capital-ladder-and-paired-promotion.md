# 0025 — Capital ladder, paired promotion, trials per family, one advisor run a day

**Status:** Accepted 2026-11-10 (decided by the owner; parameters pre-registered here before any data was seen)
**Date:** 2026-11-10
**Amends:** [0015](0015-honest-measurement-rebuild.md) ruling #9 (`n_trials`); replaces the graduation gate
description of the PR2 D1/D2 work and of `decision/graduation/__init__.py`
**Plan:** `docs/plan-capital-ladder-2026-11-10.md`
**Consumed by:** `decision/advisor/{cycle,jobs,evolution,strategy,promotion,shadow}.py`,
`decision/graduation/ladder.py`, `lab/evidence_gate/{gate,admission}.py`, `lab/alphacrafter/{miner,trader,dossier}.py`,
`foundation/quant_metrics.py`, `decision/monthly_plan.py`, migration `0148_ladder_sleeves_cycle_claim`

## Context

Four problems, found in the 2026-11-09 research (notes B and E):

1. The advisor sometimes ran twice a day on one sleeve.
2. Every deflated-Sharpe gate used one global trial count, about 6,260 on prod, of which 6,217 were
   per-stock rows of the old Discover miner. A gate's N should be the trials its candidate was selected among.
3. All challengers shared one paper sleeve. After a promotion, champion and challenger would drive the
   same book. The promotion rule compared two differently sampled composites with a fixed 0.02 margin and
   no test.
4. The graduation gate asked whether the LLM had proven skill. No test can show a 2-4 %/yr edge at
   8-12 % tracking error within two years (25-220 years at 80 % power), so the gate could never pass.

### Step 0: what prod showed (read-only, 2026-11-10)

- **Cause of the double runs: the race (cause 1).** On 2-5 Nov the 10:00 `advisor_cycle` took 36-39 min
  (slow LLM). It was still running when the 10:30 `evolution_round` ran the champion again, which wrote a
  second `idle_no_trades` row at about 11:05. There was no failed-then-rerun day (cause 2), and no promotion
  has ever happened (cause 3 cannot have occurred yet).
- `llm_portfolio_decisions.status` values: `idle_no_trades` (67), `completed` (42), `idle_no_candidates` (4),
  `llm_unavailable` (1; written by the mandate review, `llm_portfolio/review.py`).
  Mandates: `advisor` (50), `advisor-challenger` (46), `A` (9), `B` (9). Mandate reviews never get a
  `cycle_date`, so the new index does not touch them.
- `trial_ledger` contexts: `discover_replay` 12, `factor_premia` 10, `tier_b` 6, `plan_rules` 6, `satellite` 4,
  `pooled_model` 2, `discover_pooled_ml` 2, `diagnostic` 1. `ac_trial_ledger`: 6,217 rows, **3** distinct `config_hash`.
- Advisor strategies: one champion and one challenger, both from 2026-09-07, on the `advisor` and the
  shared `advisor-challenger` sleeves.

## Decision

### 1. One advisor run per sleeve per day

- `llm_portfolio_decisions.cycle_date` (DATE), with a partial unique index `uq_llm_decision_cycle_slot` on
  `(portfolio_id, mandate, cycle_date)` where `cycle_date IS NOT NULL AND status IN ('running','completed',
  'idle_no_candidates','idle_no_trades')`. `failed` is outside the predicate, so a failed run frees the slot.
- `run_advisor_cycle` claims the day first: it inserts a `running` row and commits it at once. On
  `IntegrityError` it returns `already_ran_today`, or `in_progress` while the holder is still `running`. The
  claim row is finished in place. An exception marks it `failed`. The cap of 3 failed attempts a day stays.
- At worker start, a `running` claim older than 2 h, or from an earlier day, is marked `failed` with the error `worker restart`.
- The 10:00 `advisor_cycle` job is the one trigger for both sleeves (champion, then challenger). The evolution
  round runs no cycle. `POST /api/advisor/cycle/run` goes through the same claim.
- The migration's backfill gives `cycle_date` only to the earliest slot-holding row per (portfolio, mandate,
  UTC day) of advisor sleeves. Later duplicates keep NULL, so the backfill cannot violate the index.

### 2. Trials counted per family (amends ADR 0015 ruling #9)

A gate's N is the effective number of trials **among which the candidate under test was selected**, which
means the families on its lineage. `trial_ledger.context` is the family. Rows with the same `config_hash`
in one family count once (a daily re-test is not a new hypothesis). The global total stays on the Evidence
page as a conservative researcher-level number; no gate reads it.

| Family (`context`) | One trial = | Read by |
|---|---|---|
| `discover_legacy` (`ac_trial_ledger`) | one distinct `config_hash` | none (Evidence page only) |
| `alphacrafter_miner` (new) | one formula + horizon, first evaluation | factor admission |
| `alphacrafter_trader` (new) | one config key | AlphaCrafter dossier conviction |
| `alphacrafter_tuning` | one grid config per run | tuning's own deflated ICIR (unchanged: `n_trials=len(grid)`) |
| `advisor_champion`, `advisor_challenger` (new) | one strategy | advisor scorecard DSR (display only) |
| lab: `pooled_model`, `discover_pooled_ml`, `satellite`, `factor_premia`, `tier_b`, `discover_replay`, `backtest`, `plan_rules` | one pre-registered spec or grid config | evidence gate: sum of N_eff over these |

The plan named the lab studies by study (`ranking_replay`, `quant_lab_backtest`); the contexts they actually
write are `discover_replay` and `backtest`. `plan_rules` (ADR 0022) and `discover_pooled_ml` are lab studies too.

- `effective_n_trials(db, families)`: trials that store their return or IC series in `metadata_json["series"]`
  enter a Li-Ji (2005) eigenvalue count of their correlation matrix (aligned on their last common
  observations; eigenvalues rounded to 1e-9). Trials without one count raw, which is the conservative direction. Li-Ji
  jumps just below an integer eigenvalue (3.9999 counts about 2); accepted.
- Without families, `effective_n_trials` keeps its old meaning (distinct search families, a lower bound).
  The quant-lab backtest page shows that lower bound for its own family.
- **Factor admission** (miner): a factor enters the library at a deflated ICIR
  (`deflated_ic_sharpe(ic_series, n_trials=N_eff(alphacrafter_miner))`) of **≥ 0.95** and is retired only
  **below 0.80**. The IC series is signed by its own mean, since the Trader uses a negative-IC factor inverted;
  the sign choice is not counted as a separate trial. The daily IC series of a 5-day horizon overlaps, which
  the PSR-style z does not correct for, as in tuning. The flat IC/ICIR thresholds remain only as display.
- **Dossier conviction:** the winning trader config's Sharpe less the best-of-N luck hurdle
  (`dsr_hurdle_curve` at N = `alphacrafter_trader`, T = the backtest's days) feeds `compute_conviction`.

### 3. Separate challenger sleeves and paired promotion

- One sleeve per strategy, never reused: mandate `adv-<strategy id[:12]>`. The current champion keeps
  `advisor`. A new challenger's sleeve is a **clone of the champion's current book** (same holdings, same
  cash, same moment, costed at today's EUR quote so its return starts at 0). On promotion the champion pointer
  moves to the challenger's sleeve; the old champion's sleeve is archived in `paper_portfolio_archives`, kept
  in place and marked `managed_by = retired`. On retirement the same happens to the challenger's sleeve, and the next
  challenger is a fresh clone. Readers that looked the advisor sleeve up by the `advisor` mandate now follow the champion.
- `discovery_prediction.strategy_id`, set by the cycle. A strategy's scorecard uses only its own calls since
  its creation; the `user_legacy` fallback is gone for strategy scoring.
- **Shadow forecasts** (`advisor_shadow_forecasts`, unique per strategy, date and ticker): after both sleeves have run, each
  strategy gives, for every ticker on the day's Discover shortlist, `p_beat` (beats EUNL in EUR over 21 trading
  days) and a p5-p95 band. One batched LLM call per strategy per day, on the local model. They are resolved in
  the evolution round from EUR closes (`foundation/eur_prices`): `beat`, `brier = (p - beat)^2` and a three-bucket band RPS.
  (The discover resolution job is not used: calling the advisor from it would be a new forbidden import edge.)
- **Promotion rule (pre-registered).** It replaces `PROMOTION_MARGIN` and the composite comparison. The composite stays as a display.
  - Primary endpoint: for each matched (date, ticker), `d = Brier(champion) - Brier(challenger)`; `d_t` = mean over tickers within a date.
  - Test: one-sided Diebold-Mariano on `d_t` with a Newey-West (Bartlett) long-run variance at lag `h - 1 = 20`,
    the Harvey-Leybourne-Newbold factor `sqrt((T + 1 - 2h + h(h-1)/T)/T)` with `h = 21`, and t with `T - 1` df.
    HLN derived its factor for the rectangular kernel; Bartlett is used here so that the variance is never negative.
    `statsmodels` is not installed (the plan assumed it was), so the test is about 25 lines in `quant_metrics.diebold_mariano_hln`.
    It is checked exactly against `scipy.stats.ttest_1samp` (at h = 1 it is the one-sample t), and for h > 1 against a hand computation.
    No published DM example could be checked offline.
  - Overall one-sided α per challenger: **0.05 / 4 = 0.0125** (Bonferroni over about four challengers a year).
  - Planned looks: the first on the round when the minimums are first met, then one every **21 trading days**, at most **6**.
    Each look tests at the **Pocock** nominal level for 6 looks: boundary **c = 2.713**, nominal **p < 0.00333**
    (2,000,000-path simulation, seed 20261110; re-derived in `tests/test_advisor_promotion.py`). A round between looks spends nothing.
  - Minimums: **≥ 60 trading days** since the spawn, **≥ 3 non-overlapping horizons** resolved (forecast dates at least
    21 trading days apart, counted greedily), **≥ 200** resolved matched pairs.
  - Guardrails (either failing blocks a promotion): the lower 90 % bound of the challenger-minus-champion daily
    return difference (moving-block bootstrap, block 21, 2,000 draws, on the two sleeves' NAV returns on the same weekdays) must be
    **above -3 %/yr**, and the challenger's max drawdown may exceed the champion's by at most **5 pp**.
  - Time limit: a challenger not better at the 6th look (about 105 trading days after the minimums are met,
    within a 126-day look window) is retired.
  - Secondary, display only: an exact McNemar test on matched pairs where the two calls differ in direction.
- **Reset:** the first `advisor_cycle` after the deploy retires the current challenger and archives the shared
  `advisor-challenger` sleeve, then spawns a fresh clone of the champion's sleeve. It is marked by the app setting
  `advisor_sleeves_split_at`, so it runs once. The owner accepted that the advisor's paper comparison history restarts.

### 4. The capital ladder replaces the graduation gate

The ladder asks how much real money a candidate's picks may hold, given the evidence. It caps what an
unproven idea can cost; it is not a skill detector. In simulation it promotes almost nothing within five
years unless the edge is large, and the UI says so.

**Candidates.** There are two slots, each with π = ½: `llm` (the advisor champion lineage) and `rules` (Mandate B, the rules control).
Mandate A is not a candidate; its job is the A − B measurement. A candidate is a frozen configuration
hashed with SHA-256. For `llm` the hash covers the strategy config, the served model file and the prompt-template hash
from the cycle's provenance stamp (ADR 0018 §10), plus the universe, sizing, cost model and cadence. For `rules` it covers the mandate's config.
Any change starts a **new epoch** in the same slot. That includes a promotion, a model change and a paper reset (a later `inception_at`).
The `llm` birth is the later of the sleeve's inception and the champion's promotion; for `rules` it is the sleeve's inception.

**Error budget.** Epoch e gets π_e = ½ / e². Its thresholds are **10·e²** for Earned (1/(0.2·π_e)) and **40·e²** for Proven
(1/(0.05·π_e)): 10/40, then 40/160, then 90/360. By Ville's inequality, summed over every epoch of both
slots (Σ 1/e² ≈ 1.645), a no-skill candidate **ever** reaches Earned with probability at most
**0.2 × 1.645 ≈ 0.33** and Proven at most **0.05 × 1.645 ≈ 0.08**. For the first epochs of both slots together
the bounds are 0.2 and 0.05 (0.1 and 0.025 for one candidate). Keeping 10/40 for epoch 1 (the owner's choice)
costs exactly that much familywise slack.

Measured, not just bounded: with α = 0, TE 6 % and five years of daily data, the skill e-process crosses 10 on
about 5 % of paths (5.2 % ever, 4.6 % after day 126, over 2,000 paths); through the full stage machine, 4.8 % of
500 paths (seed 20261110) reach Earned. That meets the plan's 5 % target only narrowly; the guarantee is the
Ville bound of 10 % per candidate. 99.6 % of null paths reach Tuition, as designed: Tuition is the capped cost of
looking, not a verdict.

**Measurement (nightly, 06:45 UTC, after the 00:00 paper snapshots and the 06:30 price backfill).**
`x_t = r_sleeve − r_EUNL`, daily, in EUR, on identical dates. Paper sleeves have no external flows, so the NAV
return is time-weighted. Sleeve returns are after the fees and spreads the paper engine charges. **The tax drag
is left out**: the trade check (ADR 0021) keeps it per trade, not as a daily series. σ_t is an EWMA of x² (around
zero, the null) with a **126-day half-life, lagged one day**, after a 20-day burn-in during which the e-processes
do not bet. Only returns after the epoch's birth count. LLM backtests are never evidence.

**Evidence.**
- Skill: `Y_t = clip(x_t/σ_t, −1, 1)` and `E_t = mean over λ ∈ {0.025, 0.05, 0.10, 0.20, 0.35} of Π(1 + λ·Y_s)`.
- Harm: `H_t`, the same with −x.
- Validity needs `E[Y_t] ≤ 0` under the null. With a predictable σ and α = 0 that holds when x is symmetric
  around zero; the clip keeps fat tails from breaking it.
- Relative drawdown: the peak-to-trough of cumulative x, against the band `2.8·TE·√years`.
- Display only: a normal-mixture confidence sequence for α, and a Bayesian panel with prior N(−1 %, 3 %) per year,
  showing the posterior and the ¼-Kelly weight, which stays near 0 for years.

**Ladder** (a share of the monthly plan's book):

| Stage | Satellite | Entry | Exit |
|---|---|---|---|
| 0 Paper | 0 % | registered | — |
| 1 Tuition | w₁ = min(5 %, B/(\|α_bad\| + 1.645·TE)), B = 1 %/yr, α_bad = −3 %/yr | ≥ 126 trading days after birth; H < 10; cumulative x not below −1.645·TE·√t; Brier ≤ 0.25 on its resolved calls (a veto only; Mandate B makes none); no owner veto | H ≥ 10, or drawdown > band, or owner veto → stage 0 and a new epoch |
| 2 Earned | 10 % | E ≥ 10·e² | the stage-1 exits, or E < 1 → stage 1 |
| 3 Proven | `ladder_proven_weight_pct` (15 %, at most 20 %) | E ≥ 40·e² | as stage 2 |

Only one candidate funds the satellite: the highest stage, and on a tie the higher E. Authorisation expires 24 months after birth and is
renewed as a new epoch. **Stage changes apply at the next monthly plan.** `ladder_weight` reads the stage on the
last evidence day before the first of the current month.

**Monthly plan.** The satellite target is `ladder_weight × 100`, capped by `plan_satellite_max_pct`. That setting is now
the hard ceiling, and its default rises from 10 to 20. The picks are the funding candidate's paper holdings in the satellite sleeve,
scaled to that weight. The paper sleeves' own satellite budget stays at 10 %, so raising the real-book ceiling does not change the paper experiment.
The lab evidence gate no longer unlocks the satellite. Its DSR/PBO checks moved to `lab/evidence_gate/admission.py`
as the admission filter for backtest-selected candidates. That filter computes on **excess** returns with the family N_eff and the
observed cross-trial Sharpe variance, and it decides only whether a lab strategy may be **registered** as a candidate:
a future third slot, out of scope here. The graduation evaluator no longer computes DSR or PBO; its old criteria
are kept for display and marked `legacy`. Learning maturity is display only.

Stage changes are written as `AdvisorGraduationTransition` rows with a reason. `AdvisorGraduationState.graduated`
means "a candidate is at Earned or above now". `POST /api/graduation/recommendations` unlocks on that, never on
the legacy criteria: with DSR and PBO no longer blocking, the legacy verdict would otherwise pass more easily.
`GET /api/graduation/ladder` is new; `/status` keeps its shape, adds `ladder` and `legacy_graduated`, and sets
`graduated` from the ladder. The UI renames the page and the Strategy-lab tab to "Capital ladder".

Settings (Control Center, the plan page): `ladder_tuition_budget_pct` (1.0), `ladder_bad_alpha_pct` (−3.0),
`ladder_proven_weight_pct` (15.0, at most 20), `ladder_owner_veto` (false).

## Consequences

- One `llm_portfolio_decisions` row per sleeve per day.
- The advisor's paper comparison history restarts at the deploy.
- The satellite target is 0 % until a candidate has 126 trading days of evidence after its birth. For Mandate B
  that counts from its reset (planned with the #385/#386 deploy), so the earliest Tuition is about six months after it.
- Gates deflate by the searches they actually came from: the evidence gate by the lab families, the advisor by its own lineage (2-3 today).
- Nothing here places orders. Every new money-advice payload carries `estimate`, `not_tax_advice` and `not_financial_advice`.

## Out of scope

A third ladder slot for lab-admitted strategies; residualised (CUPED-style) excess returns, which would need
their own pre-registration; `pg_try_advisory_lock` in `_track_job`; the factor-ranking pipeline's own rank-IC e-process.

## Amendment 2026-11-10: the bets clip at 3 sigma, not 1

**Defect.** `e_processes` clipped `x / sigma` to [-1, 1]. A bet on a clipped value tests the *median* of the excess return, not its mean, so skew alone moved both e-processes. Simulated with the ladder's own functions (504 days, 10 % tracking error, Earned bound 20 %):

| Daily excess returns | Reaches Earned | Harm fires |
|---|---|---|
| Gaussian, zero alpha | 4.7 % | 4.1 % |
| Skew -0.30, zero alpha | 13.5 % | 1.7 % |
| Skew -0.85, zero alpha | 59 % | 0.3 % |
| Skew +0.85, alpha +3 %/yr | 0.5 % | 46 % |

A negatively skewed sleeve with no edge (the usual shape of a strategy that sells insurance) climbed the ladder; a positively skewed sleeve with a real edge was punished.

**Change.** `CLIP_SIGMAS = 3.0` (`foundation/capital_ladder.py`). `y = clip(x / sigma, -3, 3) / 3`, so `y` is again in [-1, 1], `1 + lambda * y` stays positive and `LAMBDAS` and every threshold are unchanged. With the clip at 3 sigma, skew -0.85 at zero alpha reaches Earned in 5.3 % of paths, with the same power under a real edge. `tests/test_capital_ladder.py` now simulates 2,000 paths each for skew 0, -0.3, -0.85 and +0.85 and fails when either rate exceeds its nominal bound plus three Monte Carlo standard errors (it fails at a clip of 1).

**Residual limit.** A 3-sigma clip still trims extreme days; the guarantee is approximate for very heavy tails. `ladder_evidence_daily` is deterministic from the paper snapshots: rows written before this change are recomputed by the next ladder run for the owner's candidates once `ladder_evidence_daily` is cleared (no rows existed on 2026-11-10).

**Also.** The ladder skips a paper snapshot day that is still the advisor cycle's provisional live-quote row (the rule the trust ledger already has). The Diebold-Mariano test in the paired promotion drops autocovariance pairs more than five trading days apart (matched forecasts are not contiguous). `candidate_gate.one_sided_t_e_value` documents that Wang and Ramdas, Theorem 4.11, is proved for mean = 0.

## Amendment 2026-11-12: a learning sleeve in euros, paid as tuition, not earned by evidence

**Status.** Decided by the owner on 2026-11-12. Not implemented; the work package is WP11 of `docs/plan-audit-fixes-2026-11-12.md`.

**Why.** The ladder is slow by construction. By the approximation in `docs/audit-2026-11-12-supplement.md` §3.2 (not a simulation of this setup), a candidate with a yearly information ratio of 0.5 needs about 18 years to reach Earned. The owner will not wait that long and accepts a small, fixed amount of real money as a learning expense.

The money buys three things: the gap between a paper fill and a real fill (spread, FX fee, fractional rounding, tax); an end-to-end run of plan → order → sync → tax ledger → performance on real positions; and the owner's own practice. It does **not** buy evidence of skill. Twelve purchases a year cannot separate a real edge from luck, and a return is the same number whether money was behind it or not. The paper record stays the only evidence.

**Decision.**

1. **Amount.** A new setting `ladder_learning_eur_per_month`, default `0` (off). The code caps it at `LEARNING_SLEEVE_MAX_EUR = 20`; a higher stored value is read as 20. Money not spent in a month does not carry over. There is no top-up after a loss.
2. **When it applies.** From the first monthly plan after the owner sets it, while the funding candidate is at stage 0 (Paper) or stage 1 (Tuition). It does not wait for the 126 trading days. At stage 1 the satellite gets the **smaller** of this amount and `w₁`. With the setting at 0, stage 1 behaves as before. Stages 2 and 3 are unchanged.
3. **Whose picks.** The `llm` slot (the advisor champion lineage) only. One purchase a month: the largest holding of that candidate's paper satellite sleeve on the plan day that Trading 212 lists by ISIN (`foundation/broker_instruments.py`) and that can be bought as a fraction. If there is none, nothing is bought that month.
4. **Sales.** The plan proposes a sale only when the paper sleeve no longer holds the name, and only in a monthly plan. Each proposal goes through the trade check (ADR 0021).
5. **Who trades.** The owner places every order by hand. Quantfolio stays read-only at every broker: no order endpoint, no pie write, no change to `ALLOWED_PATHS`. The plan shows the line; the next sync marks it executed (`foundation/recommendation_execution.py`).
6. **Stop.** The amount falls to 0 from the next monthly plan when the candidate's harm e-value reaches 10, its relative drawdown leaves the band, or `ladder_owner_veto` is set: the stage-1 exits, read from the paper record. Positions already bought are kept; nothing forces a sale. The authorisation ends after 12 monthly plans and the owner renews it by saving the setting again.
7. **Benchmark.** For every purchase the app records a shadow purchase of the same euro amount in `EUNL.DE` at that day's close. The page shows both money-weighted results, their difference, the number of purchases and, per purchase, the real fill against the paper engine's price in basis points.
8. **No feedback.** Results of the learning sleeve are never written to the lessons, a prompt, a training set, the trial ledger or `ladder_evidence_daily`. The ladder keeps measuring the paper sleeve. Changing the setting does not start a new epoch: it is not part of the candidate's hash.
9. **Wording.** Wherever the stage or the sleeve is shown: "A learning expense you chose. It is not evidence that the picks work." Every payload carries `estimate`, `not_tax_advice` and `not_financial_advice`.

**Cost.** At most 240 EUR a year is invested, not lost. If the picks keep trailing the index by the 4.7 % measured on the resolved paper BUY picks (2026-11-12, overlapping picks, not a forecast), the expected shortfall against the shadow is about 10 EUR a year.

**Tax.** Trading 212 holds the owner's Freistellungsauftrag and its sells and dividends already reach the tax cockpit through the sync (`docs/trading212.md`). Dividend rows there still carry `withholding_unverified`; that applies to this sleeve too.

**Supersedes.** In *Consequences*, "The satellite target is 0 % until a candidate has 126 trading days of evidence" now holds only while `ladder_learning_eur_per_month` is 0.

**Open for the implementer to confirm in code, not decided here.** How the real positions of the sleeve are told apart from the rest of the Trading 212 depot (preferred: one dedicated pie that the owner confirms, the mechanism of #374); whether a 20 EUR fractional order is possible for every listed instrument; how the euro amount is expressed in a plan that otherwise works in percent of the book.
