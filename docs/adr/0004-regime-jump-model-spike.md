# 0004 — Regime Jump-Model Spike (`jumpmodels`)

**Status:** Accepted (amended 2026-09-28: adopted, see below)
**Date:** 2026-08-25
**Context source:** `docs/archive/audits/2026-08-universe-regime-audit.md` §D2; DEC-C second-half
ratification ("fix HMM now — done; jumpmodels spike later"); AGENTS.md dependency
watch-list (jumpmodels: dormant since Jan 2025, pin if adopted)
**Consumed by:** `backend/app/services/regime/jump_model.py`,
`backend/tests/test_regime_jump_spike.py`, `backend/scripts/jump_spike_eval.py`

## Context

The D2 audit left the regime engine with a hardened Gaussian HMM
(`RegimeHMM`, n_states=3, deterministic return-rank labelling) and an open
question: the jump-model literature (Nystrup et al.; arXiv 2402.05272)
claims direct state-change penalties produce more persistent regimes and
better crisis separation than HMM transition matrices. DEC-C split the
work: harden the HMM immediately (shipped), evaluate `jumpmodels`
time-boxed afterwards (this spike).

Upstream reality checked before the spike: `jumpmodels==0.1.1` (PyPI,
Oct 2024) is a single-maintainer package, dormant upstream since Jan 2025,
with dependencies (numpy/pandas/scikit-learn) already in the backend venv.
That dormancy is the governing risk of this evaluation: any adoption
couples regime classification to unmaintained code, so the bar for
adoption is higher than "beats the incumbent on a benchmark".

Spike scope, as ratified: a pinned dev-only dependency, an
interface-parity prototype, a reproducible quantitative comparison, and
this ADR. Nothing was wired into `classifier.py`, jobs, settings, API
routers, or the frontend; `hmm_model.py` was read but not modified.

## What was built

- **Dependency:** `jumpmodels==0.1.1` pinned exact in the `[dev]`
  optional-dependencies group only. Dev-extra rather than main dependency
  because (a) no production module imports it, (b) a dormant package must
  never enter the prod closure until an adoption decision exists, and (c)
  the dev extra keeps CI/production installs unaffected when the pin goes
  stale. `requirements.lock` refresh deferred (no local `uv`; lock left
  untouched rather than hand-edited).
- **Prototype:** `JumpRegimeModel` mirrors `RegimeHMM`'s public surface —
  `fit` / `predict` / `predict_state_proba` / `classify_latest` /
  `save(model_id, base=None)` / `load(model_id)` / `feature_columns` /
  `converged` — with StandardScaler preprocessing, persistence through the
  shared quant-ml registry (joblib, same `_load_model_unvalidated` path as
  the incumbent), n_components=3, and the identical deterministic
  state→label rule (rank fitted centroids' return column: highest → bull,
  lowest → bear, middle → sideways). The ranking is applied to the final
  centroids, so it is invariant to upstream's internal state permutation.
  The import is lazy inside methods so the module imports cleanly when the
  package is absent; calling a method then raises a RuntimeError naming
  the dev extra.
- **Tests:** `tests/test_regime_jump_spike.py` — interface parity,
  save/load round-trip (tmp_path; persistence is filesystem joblib, no DB),
  seed determinism, contract parity against `RegimeHMM`, and an
  unconditional absence test (monkeypatches the lazy-import site;
  ImportError must surface as the clear RuntimeError regardless of
  installation state).
- **Harness:** `scripts/jump_spike_eval.py` — three synthetic scenarios
  (bull→chop→crash; V-shaped recovery; grind-up→break) built with
  production-shaped features (ret, rolling-21 vol, rolling-63-peak
  drawdown), plus a sliding-window refit-stability probe (60% windows,
  4 steps, label flip rate over overlaps). Both engines run identically.

## Measured comparison

Seed 42, ~980–1080 rows per scenario after warmup drop, jump defaults
`jump_penalty=10, n_init=10` (untuned):

| Scenario              | Model | Block acc | Bear recall | Refit flips | Fit s |
|-----------------------|-------|----------:|------------:|------------:|------:|
| A_bull_chop_crash     | HMM   |     0.987 |       0.983 |       0.511 | 0.333 |
| A_bull_chop_crash     | Jump  |     0.977 |       0.950 |       0.436 | 0.162 |
| B_v_shaped_recovery   | HMM   |     0.661 |       1.000 |       0.474 | 0.064 |
| B_v_shaped_recovery   | Jump  |     0.918 |       0.851 |       0.466 | 0.149 |
| C_grind_up_then_break | HMM   |     0.990 |       0.968 |       0.558 | 0.447 |
| C_grind_up_then_break | Jump  |     0.981 |       0.936 |       0.646 | 0.241 |

Determinism spot-check: both engines reproduce identical label sequences
under a fixed seed. Fits are sub-half-second at this scale for both.

Penalty sensitivity sweep (same scenarios, block acc / bear recall per
scenario in A, B, C order):

| jump_penalty | Block acc         | Bear recall       |
|-------------:|-------------------|-------------------|
|          2.0 | 0.478 / 0.878 / 0.389 | 0.560 / 0.714 / 0.280 |
|         10.0 | 0.977 / 0.918 / 0.981 | 0.950 / 0.851 / 0.936 |
|         30.0 | 0.977 / 0.918 / 0.981 | 0.950 / 0.851 / 0.936 |
|         60.0 | 0.977 / 0.967 / 0.981 | 0.950 / 1.000 / 0.936 |

Reading the numbers honestly:

1. **Scenario B is a structural HMM failure mode.** When the series opens
   in bear and recovers, the incumbent collapses to 0.661 block accuracy
   (bear recall 1.000 but massively over-inclusive — it swallows the
   sideways block). The jump model segments the same sequence at 0.918
   (0.967 tuned). This is the strongest pro-adoption signal in the data.
2. **At untuned defaults the jump model loses on crisis recall**
   (0.851–0.950 vs 0.968–1.000). For a portfolio-defense signal, missed
   crash rows are the expensive error class, so out-of-the-box the
   incumbent is safer. At `jump_penalty=60` the gap closes to near-parity
   (only scenario A remains at 0.950 vs 0.983).
3. **The literature's persistence advantage did not reproduce here.**
   Refit flip rates are a wash (each engine wins one scenario, 0.44–0.65
   band for both). The arXiv claim concerns decoded-sequence stickiness on
   real asset data; on our three-feature geometry with well-separated
   blocks, neither engine is meaningfully more refit-stable.
4. **Tuning burden is real.** `jump_penalty=2` is catastrophic (block acc
   down to 0.389); the usable plateau starts around 10 and improves again
   at 60. Any adoption ships with a validated penalty default, not the
   library default.

## Recommendation

**Adopt-later, behind a settings flag — do not adopt now.** The evidence
justifies keeping the option alive, not flipping anything today:

- For: scenario-B robustness where the incumbent structurally fails;
  parity-or-better everywhere once the penalty is tuned; identical public
  contract means the swap is mechanical; fit cost is comparable.
- Against adopting now: all evidence is synthetic with well-separated
  blocks; crisis recall regresses at library defaults; the penalty needs
  validation on real data; and the dependency is dormant upstream, so the
  adoption carries a permanent maintenance liability (exact pin, pickle
  artefacts that require the package to unpickle, fork-or-vendor escape
  hatch if upstream stays dead).

### Integration sketch (NOT implemented — future work)

1. `services/settings.py`: new public setting `regime.model_kind` ∈
   {`hmm`, `jump`} (DB AppSetting → env → default `hmm`).
2. `regime/classifier.py`: single dispatch point choosing
   `RegimeHMM()` or `JumpRegimeModel(jump_penalty=<validated>)`;
   persisted models already carry their class through the registry, so
   load-side dispatch keys off the stored artefact type.
3. Migration path: ship flag default-off → shadow-run both engines on
   live features for several weeks comparing crisis recall and refit
   churn on real data → flip default only with ADR amendment citing those
   numbers.
4. Rollback = git revert of the dispatch commit; flag-off restores the
   incumbent path with no artefact migration (both persist via the same
   registry layout).

## Alternatives considered

**Adopt now.** Rejected: synthetic-only evidence, a crisis-recall
regression at defaults, and an untuned hyperparameter on a dormant
dependency is not a basis for moving a live risk signal.

**Stay on HMM permanently.** Rejected as a final verdict: scenario B
demonstrates a real incumbent failure mode, and the tuned sweep shows the
model class matches or beats the incumbent elsewhere. Closing the door
would discard measured evidence in favour of the pre-spike status quo.

**Vendor/fork jumpmodels immediately.** Premature before the adoption
decision; revisit only if the shadow phase succeeds and upstream remains
dormant.

## Consequences

- The live regime path is untouched: `tests/test_regime_crisis_classifier.py`
  passes 20/20 unchanged, and no incumbent regime file was modified.
- `jumpmodels==0.1.1` lives in `[dev]` only; production installs never see
  it, and the prototype degrades to an actionable RuntimeError without it.
- The comparison is reproducible: `cd backend &&
  .venv/bin/python scripts/jump_spike_eval.py` regenerates every number in
  this ADR.
- Revisit triggers: (a) shadow-phase numbers on real portfolio features
  meeting or beating incumbent crisis recall at a validated penalty;
  (b) upstream activity resuming (would also warrant re-pinning);
  (c) another regime-engine proposal, which must cite this ADR's negative
  results (refit-stability wash, penalty sensitivity) rather than the
  literature prior alone.

## Amendment (2026-09-28): adopted on real-data evidence

**Status:** Accepted

**Trigger (a) met.** The Discover audit of 2026-09-28 found the live HMM
unusable.

The HMM gave different labels to the same features across refits. On
2026-09-26 it said bull (posterior 0.99999); after the Sunday refit, on
09-27, it said sideways (posterior 1.0), on identical features.

A `bear` snapshot blocks Discover (`MacroRegimeGate`), so this decided
whether runs happened at all.

The comparison below uses real data: ^STOXX50E from 2007 and FRED
VIX/BAA10Y/T10Y2Y. Models were refitted weekly from 2023-06 to 2026-09, and
each day of the following week was classified online.

- *Relabelled* is the share of overlapping days whose label changed between
  consecutive refits.
- *Flips/yr* counts label changes in the online sequence.

| Model | Relabelled | Flips/yr | Bull / sideways / bear |
|---|---:|---:|---|
| HMM as run in prod (≈260 rows, 1 start, mean-return labels) | 48% | 112 | 33 / 37 / 29 |
| HMM, 515 rows, 1 start, mean-return labels | 52% | 72 | 36 / 33 / 31 |
| HMM, 515 rows, 10 starts | 30% | 20.5 | 38 / 32 / 30 |
| HMM, 515 rows, 10 starts, **volatility labels** | 13% | 9.3 | 36 / 43 / 21 |
| HMM, 2,500 rows, 10 starts, volatility labels | 24% | 19.6 | 61 / 25 / 15 |
| Jump λ=50, return labels | 13.5% | 7.8 | 49 / 34 / 17 |
| **Jump λ=10, volatility labels** | **6.3%** | **5.7** | 49 / 32 / 19 |
| Jump λ=50, volatility labels | 3.4% | 3.6 | 67 / 11 / 22 |

In the April 2025 tariff sell-off the three candidates behaved differently:

- The prod HMM alternated bull and bear day by day (03-31 bull, 04-01 bear,
  04-02 bull, 04-03 bear, 04-04 bull), each at ≥0.99999.
- The best HMM labelled the crash week sideways and turned bear on 04-14.
- The jump model at λ=10 was bear from early March to mid-May without a
  flicker.

The spike's synthetic finding that refit flip rates were "a wash" did not
hold on real data. Its crisis-recall worry is answered by the April 2025
behaviour.

**Decision.**

- `JumpRegimeModel` (λ=10, 10 restarts) is the live regime model
  (`regime_model_kind`, default `jump`).
- `jumpmodels==0.1.1` moves to the main dependencies, pinned exact.
- Both models label states by the volatility feature: lowest → bull,
  highest → bear. Return means of three states on one index are close and
  noisy; their volatility levels are not. Shu, Yu & Mulvey (2024) separate
  HMM regimes by volatility for the same reason.
- The HMM keeps being refitted weekly (10 seeded restarts, best
  log-likelihood) as the fallback when the package or artefact is missing.
- Snapshots record `source` (`jump` | `hmm`).
- Classification is causal (`predict_online`). The score is
  `softmax(−V_t)` over the online DP values, because the discrete model's
  own probabilities are one-hot.
- Persistence is a versioned state dict, like `RegimeHMM`.
- Chose λ=10 over 50: 50 is stickier but left "sideways" at 11% of days,
  effectively a two-state model.

**Also fixed in the same pass (data):**

- Each FRED series is now carried forward on its own. A lagging VIX
  used to drop every newer price row, so the model classified 09-17 for a
  week.
- The FRED fetch retries on failure.
- The classifier refuses a feature row that lags the bars.
- The refit backfills a short ^STOXX50E history. Prod held only 282 bars.

**Deploy.**

- Install `jumpmodels==0.1.1`.
- Run `refit_regime_model` once. Until the first refit writes
  `regime_jump`, the classifier uses the HMM.

## Amendment 2 (2026-09-28): a calibrated score, and VIX from the index

**The score.** `softmax(−V_t)` sat at 1.00 on almost every day. While the
model stays in a regime, every other state's online DP value trails by
about the jump penalty, and e^−10 ≈ 4.5e−5. The score therefore said
nothing, and the research prompt read "confidence 100%".

The candidates were replayed on real data: 803 live days, weekly refits,
2023-06 to 2026-06. Each day's truth was the label the same fitted model
assigns once 63 more trading days are known. The online label matched that
truth on 95.1% of days.

| Score | Brier | Log loss | Mean score |
|---|---|---|---|
| DP softmax (as shipped) | 0.093 | 0.305 | 0.998 |
| Continuous jump model (Aydinhan, Kolm, Mulvey & Shu), λ=10 / 30 / 100 | 0.21 / 0.16 / 0.26 | 1.2 / 1.0 / 1.7 | ≈1.0 |
| **Label reliability** | **0.078** | **0.170** | 0.936 |

- **DP softmax:** days it scored 0.95–0.99 were right 36% of the time.
- **Continuous jump model:** its own argmax disagreed with the discrete
  label on 3–9% of days, and it was saturated as well. Rejected.
- **Label reliability:** at each refit the model counts, over its fit
  window, how often an online label kept its label in hindsight. The count
  is per online label and per bucket of days since the label last changed
  (0–4, 5–20, 21+), with Laplace smoothing. `classify_latest` returns that
  row as `probs` and today's label's entry as `score`. The payload's
  `score_basis` says which kind of score it is.

Most revisions (32 of 39 misses) are the model staying "bear" after
volatility has already calmed. An artefact saved before the table existed
loads and uses the softmax until its next weekly refit.

**VIX.** The 5 s and 30 s retries above did not help. Every 07:00 VIXCLS
request failed from at least 09-15 to 09-28, and the identical request
succeeded at 20:30. FRED's VIXCLS also lagged: last updated 09-23, newest
value 09-22, while T10Y2Y had 09-25. VIXCLS is the CBOE index close, so the
macro refresh now fills the days after FRED's newest value from the `^VIX`
bars (`source="cboe_index"`). On the 61 days both sources had, the values
were identical.
