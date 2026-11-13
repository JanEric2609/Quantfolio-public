# LLM A/B for the stock report (WP15.3, decides D8)

**Built, not run.** The set and the scorer are in `backend/scripts/llm_batch_bench.py` (task `report`). The runs need
the model endpoint of the app host, which this checkout cannot reach, and the second and third arms need models to be
loaded there. Nothing below has a result yet.

**Set.** `report-build` writes 40 seeded fact tables (growth basis, period, drawdown, volatility, news or none). Each
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
