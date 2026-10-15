# LLM look-ahead probe (plan 2026-10-14, D4.2)

**Run:** 2026-10-15 on the production LLM container (LXC 112), outside Discover's window.
**Question:** does the local model "remember" market outcomes from before its training cutoff?
If it does, any replay of an LLM-touched step before that cutoff is contaminated.

**Answer: no sign of it for the served model (Qwen3.5-9B).** Its answers carry no
information about the outcome, before or after the cutoff (AUC 0.50 both). The 35B
candidate showed a weak hint (AUC 0.56 before, 0.52 after). It is not served (see
[the benchmark](llm-benchmark-2026-10-15.md)).

## Method

Based on the LAP idea (arXiv 2512.23847): ask the model a question whose answer it could
only know from its training data, and compare its skill before and after the training
cutoff. Real skill would look the same on both sides; memory shows up only before.

- **Question.** "Today is {date}. Will {name} ({ticker}) outperform the S&P 500 over the next
  month?" The model answers `outperform` (true/false) and a `confidence` of 0-100, in a
  JSON schema the server enforces. Temperature 0, thinking off.
- **Universe.** 100 US large caps. Each question covers one calendar month, from the first
  trading day to the first trading day of the next month. Total returns come from yfinance,
  and SPY is the benchmark.
- **Dates.** Monthly from January 2019 to September 2026, 3,840 questions in all:
  - January 2019 to June 2025: 30 names a month, drawn at random (seed 20261015);
  - from July 2025: every name every month, so the months around and after the cutoff are
    not thin;
  - this gives 3,040 questions before the cutoff and 800 after it (February to September 2026).
- **Cutoff.** 2026-02-01, taken as the training cutoff of both models. Neither model card
  states an exact date. If the true cutoff is earlier, part of the "before" sample is
  clean, which dilutes any memory signal but cannot create one.
- **Score.** Accuracy alone is misleading here: the 9B says "no" to almost everything, and
  more stocks lagged SPY after the cutoff. So the main figure is the **AUC** of the model's
  stated P(outperform): its confidence for a "yes", and 100 minus it for a "no". 0.5 means
  no information. The 95 % intervals come from a bootstrap with 2,000 resamples.

Script: `backend/scripts/llm_batch_bench.py` (`lap-build`, `run --task lap`, `summarize`).
It writes nothing to the database.

## Results

| Model | Set | Questions | AUC before cutoff | AUC after cutoff | Said "yes" (before / after) |
|---|---|---:|---|---|---|
| Qwen3.5-9B | full | 3,040 / 800 | **0.498** (0.483-0.513) | **0.501** (0.465-0.539) | 0.9 % / 0.0 % |
| Qwen3.5-9B | 800-question subset | 400 / 400 | 0.495 (0.454-0.535) | 0.520 (0.471-0.573) | — |
| Qwen3.6-35B-A3B | same subset | 400 / 400 | **0.557** (0.505-0.611) | 0.522 (0.499-0.544) | 13.8 % / 0.0 % |

The 35B ran only on a fixed subset: 400 questions before the cutoff and 400 after, drawn
with the same seed. At 5-10 tokens/s the full set would have taken about 8 hours. The 9B
row on the same subset is there for a like-for-like comparison.

Raw accuracy, for the record:
- **9B:** 51.1 % before and 57.9 % after. The 57.9 % is the base rate of "no" after the
  cutoff (42.1 % of stocks outperformed), not skill.
- **35B:** 50.2 % and 57.0 %, the same pattern.

## Reading

- **Qwen3.5-9B:** no measurable memory of one-month outcomes from 2019 to 2026. Its answers
  carry no information on either side of the cutoff. A replay of an LLM-touched step is
  therefore not contaminated by recall on this test. The model also shows no forecasting
  skill: an LLM answer is not a return signal.
- **Qwen3.6-35B-A3B:** its AUC before the cutoff is just above 0.5 at the 95 % level, and
  after it is not. That is a weak sign of recall. It is one more reason not to adopt the
  35B (see the benchmark), and it would need a larger set to confirm.
- **Limits:**
  - one question type (one-month relative return) and US large caps only;
  - the cutoff date is assumed;
  - a model can remember a famous month (March 2020) without that showing in a pooled AUC.
    The per-year accuracies show no such spike: for the 9B, 2020 is 56 % against 46-54 % in
    the other years.

## What this settles

The plan said a replay of an LLM-touched step "can be trusted not at all" until this was
measured. For the served 9B it now can, as far as recall of returns goes. Other limits of
a replay still apply: the prompt and the fact sheet must be rebuilt as of the date. This
probe does not cover earnings facts. The EDGAR check in the benchmark is about reading
accuracy, not recall.
