# LLM A/B for the stock report (WP15.3, decides D8)

**Arm A run 2026-11-12 on the app host; arms B and C not run.** The set and the scorer are in `backend/scripts/llm_batch_bench.py` (task `report`); each result row now keeps the model's text, so a run can be re-scored offline.

**Set.** `report-build` writes the 40 seeded fact tables of the WP9 golden set (`scripts/report_golden_set.py`, committed as `backend/tests/fixtures/report_golden_set.json`; growth basis, period, drawdown, volatility, FCF proxy, news or none). The same tables carry 313 planted-error and clean variants that the prose check judges correctly (`tests/test_report_golden_set.py`). Each
asks for the four prose sections of the report with the production prompt. No network, no database.

**Score.** Each section goes through the production checks (`check_prose`: figures, periods, judgement words, news
claims; `foreign_companies`). The statistic is the rejection rate with a Wilson 95 % interval, plus tokens per
second and latency.

**Arms.** A: the current model. B: the same model at Q5 with a 16k context. C: Gemma 3 12B or Phi-4.

```bash
# on the app host, one arm at a time, at night
python scripts/llm_batch_bench.py report-build --out report_set.json
python scripts/llm_batch_bench.py run --task report --set report_set.json --temps 0.7 --label A --out A.json
python scripts/llm_batch_bench.py summarize A.json B.json C.json
```

**Decision rule (set before looking).** Switch only if an arm's rejection interval lies entirely below A's
(no overlap) and latency stays under 60 s per section. Otherwise keep the current model (D8).

## Arm A (2026-11-12, Qwen3.5-9B-UD-Q4_K_XL, temperature 0.7, 40 tables, 146 sections)

No errors; median latency 1.1 s per section (p90 1.6 s), 53 completion tokens per second.

The first run rejected 54 % of sections (95 % 46-62 %), almost all of them for "a number not in the fact table".
Reading the texts showed the checker, not the model: the golden set's company names carry digits ("Company J9"),
and the check counted the company's own name as a figure. The same would hit real names (3M, 1&1, 3i). It also
rejected "a bearish market backdrop" (the regime label it was given), "market sentiment cannot be judged without news"
and "the last quarter" for the 3-month return. The check now leaves out the company name, the ticker and the given
headlines, accepts the regime word for the market and a sentence saying sentiment cannot be judged, and accepts
"quarter" or "half-year" beside a price fact of the same span unless the sentence is about revenue or earnings. The
golden set has a clean text and a still-caught counterpart for each (all 15 classes as expected).

Re-run with the fixed check: 6 of 146 rejected (4.1 %, 95 % 1.9-8.7 %); re-scored offline with the final check: 3 of 146
(2.1 %), each with "quarter" in a sentence that also names revenue; the check cannot tell which measure the period
belongs to, so these stay rejected. A rejected section falls
back to the template sentence, so a reader never sees the rejected text.

Arms B and C need other model files loaded on the LLM host (an RTX 3070 with 8 GB; arm A uses about 6.9 GB), so they are
the owner's call. Until one runs, D8 stays: keep the current model.
