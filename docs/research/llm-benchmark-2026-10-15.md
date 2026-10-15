# LLM benchmark, 2026-10-15 (plan 2026-10-14, D4.3, D4.5 and D5)

**Run on:** the production LLM container (LXC 112, RTX 3070 8 GB, 24 GB RAM), outside
Discover's window, with the app's own client (`_local_llm_sync`, schema-constrained JSON,
thinking off). Script: `backend/scripts/llm_batch_bench.py`; it writes nothing to the database.

## Verdicts

| Item | Result | Decision |
|---|---|---|
| **D4.3** EDGAR numbers vs XBRL, Qwen3.5-9B | 98.8 % of answered numbers correct (162/164; 95 % CI 95.7-99.7 %) | Clears the plan's 98 % bar. The two "wrong" answers are definition mismatches (below). |
| **D4.5** sampling, extraction half | Temperatures 0.7, 0.3 and 0.0 give identical answers on all 44 filings | Keep the current preset; no change. |
| **D4.5** sampling, dossier half | Not measured: capturing past Discover inputs was not allowed on prod | Measure from the D4.1 audit trail (#349) after deploy |
| **D5** Qwen3.6-35B-A3B vs Qwen3.5-9B | Same accuracy (98.8 %), 4.5× slower (13.0 s vs 2.9 s per filing) | **Keep the 9B.** The 35B file was deleted from LXC 112 on 2026-10-15. |

The look-ahead probe (D4.2) has its own page: [llm-lookahead-probe.md](llm-lookahead-probe.md).

## D4.3: EDGAR extraction checked against XBRL

- **Set:** the latest 10-K and 10-Q of 25 US large caps (AAPL ... DIS), fetched with the app's
  SEC user agent.
  - 49 filings were fetched. In 44 of them the income statement was found in the text; the
    other 5 are left out.
  - The model receives the income-statement window of the filing text. It returns revenue, net
    income, diluted EPS and weighted diluted shares.
  - Ground truth is the filer's own XBRL value (`companyfacts`) for the same accession and
    period. A number counts as correct within 0.5 % (EPS within half a cent).
- **Guidance** (in the plan's list) was left out: XBRL has no truth for it.

| | Qwen3.5-9B (t = 0.7, 0.3, 0.0) | Qwen3.6-35B-A3B (t = 0.0) |
|---|---|---|
| Correct | 162 | 161 |
| Wrong | 2 | 2 |
| Missing (model gave null) | 6 | 7 |
| No XBRL truth | 4 | 4 |
| XBRL itself wrong | 2 | 2 |
| **Accuracy on answered numbers** | **98.8 %** (162/164) | **98.8 %** (161/163) |
| Median latency per filing (p90) | 2.86 s (2.95 s) | 13.03 s (20.68 s) |
| Completion speed | 28 tokens/s | 5.5 tokens/s |

**The misses:**
- **Wrong, 9B:** TSLA and CVX net income. The model gave net income including minority
  interests (3,855 vs 3,794 and 12,485 vs 12,299 USD m); XBRL's `NetIncomeLoss` is the amount
  attributable to shareholders.
- **Wrong, 35B:** CVX revenue, twice. It gave "sales and other operating revenues", where XBRL
  has total revenues.
- **Missing:** mostly diluted shares (GOOGL, PG, MRK, CVX). These statements put the share
  count in a separate note, outside the window the model sees.
- **XBRL wrong:** MCD tags its diluted shares in millions (716.4) as if they were units. The
  model's 716,400,000 is right, so these two rows are left out of the accuracy.

**Production rule** (plan, D4.3): the LLM may fill a field only if its accuracy on numbers is
≥ 98 %. The 9B meets that for revenue, EPS and diluted shares as read. Net income needs the
prompt to ask for the amount "attributable to" shareholders; that is a prompt change to test,
not a model change. No production path uses this extraction yet.

## D4.5: sampling

- **Extraction:** temperature makes no difference here. Every field had the same outcome at
  0.7, 0.3 and 0.0, and latency and speed were unchanged. A short JSON answer under a schema
  leaves the sampler almost no room.
- **Dossiers:** not measured. The plan wanted the validator's rejection rate (truncation,
  numbers, contradiction) at each temperature. Replaying past Discover inputs needs a capture
  step on the app container, which was not allowed. After deploy, #349's audit trail records
  the sampling, outcome (ok, schema retry, fallback) and prompt hash of every call. The
  rejection rate at the current preset can be read from it without a replay. A change of
  temperature is then a pre-registered A/B, not a flip.

## D5: the 35B MoE

**Setup:**
- **Model:** Qwen3.6-35B-A3B UD-Q4_K_M (22.1 GB).
- **Unit:** a transient systemd unit, with `--n-cpu-moe 34` (expert layers in RAM, shared
  layers in VRAM: 5.9 GB used), `--ctx-size 16384` and `MemoryMax=22G`.
- **Safety:** a timer restored the 9B unit after the run.
- **Prod:** the 9B was back at 09:50 UTC, before the advisor's 10:00 and 10:30 cycles. Both
  cycles ran on the 9B.

**Measured:**
- **EDGAR:** the same accuracy as the 9B (above).
- **Speed:** 5.5 tokens/s against the 9B's 28 on the EDGAR prompts (about 3,300 tokens in);
  9.8 against 41.8 on short prompts.
- **Look-ahead probe:** a weak sign of recall before the cutoff (AUC 0.557, CI 0.505-0.611);
  the 9B shows none.
- **Not measured:**
  - the dossier rejection rate (as in D4.5);
  - a full Discover run. At 4.5× the latency per call, its LLM time would grow about 4.5×.

**Adoption rule** (plan, D5): adopt only if accuracy is ≥ 3 pp better *and* a Discover run
stays under 20 minutes. Accuracy is 0 pp better, so the rule fails on its first condition.
The 9B stays.

**Cleanup:**
- The 35B GGUF was deleted on 2026-10-15 (the owner's OK), freeing 22 GB on LXC 112.
- Only one llama-server fits the GPU, so the two models can never run side by side anyway.
- LXC 112 keeps its 24 GB RAM limit. An LXC's limit is a cap, not a reservation, so the unused
  part is not taken from the host.

## Reproduce

```bash
cd /opt/quantfolio/backend   # on LXC 111, as the quantfolio user, with the app's .env
.venv/bin/python scripts/llm_batch_bench.py edgar-build --out edgar_set.json
.venv/bin/python scripts/llm_batch_bench.py run --task edgar --set edgar_set.json \
    --temps 0.7,0.3,0.0 --label 9b --out r_edgar_9b.json
.venv/bin/python scripts/llm_batch_bench.py summarize r_edgar_9b.json
```

The results file records the model file the endpoint served, read from `/v1/models`. On
2026-10-15 the script still recorded the configured name instead, so the 35B result files
are labelled `35b` but name the 9B file.
