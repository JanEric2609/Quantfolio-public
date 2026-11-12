# LLM choice and configuration for Quantfolio (research dated 2026-11-12)

Citation key: [n] refers to the numbered URL list at the end. "READ" = page fetched and read; "SNIPPET" = only a search-result summary was seen (lower confidence). Web search budget ran out before every planned query was done (see section 9).

## 0. Bottom line

1. Do NOT switch the main model just because a newer one exists. The observed failures (calling a 23% "not-own" FCF yield exceptional, unit/base errors, identical confidence across tickers, 4% runaway generations) are mostly a prompt/architecture/config problem, and the literature says even frontier models make 10-20% errors on chained numeric reasoning over filings [21]. Qwen3.5-9B is still the 8 GB consensus pick in mid-2026 guides [73 SNIPPET], and Qwen has published no 9B-class successor (Qwen3.6 has only 27B and 35B-A3B) [95 SNIPPET].
2. Fix the runaways: set `n_predict`/max_tokens, send `response_format`/`json_schema` (llama.cpp turns it into a grammar) [10][22], and verify thinking is really off per request: `enable_thinking:false` has been reported ignored by Qwen3.5-9B in llama.cpp (issue closed unresolved as stale) [34]. A 120 s timeout at ~55 t/s is ~6,600 tokens, consistent with a thinking leak or a loop.
3. Run sentiment on an encoder (FinBERT-class, ms per call) or keep the LLM only with a 1-token enum schema; 3B-class LLMs only tie FinBERT-Tone on PhraseBank [68 SNIPPET], and reasoning does not help sentiment [49].
4. For the 7-section reports and verdicts, move all numbers, units, bases and labels (e.g. "FCF yield: peer-derived, not company FCF; band = normal/high") into deterministic code, give the LLM only prose, and run a regex/number-check pass after generation. Replace self-stated "confidence" with a computed one (sample agreement or token probability) [20][39][50].
5. Cloud: for public/non-private workloads, Claude Haiku 5.5 at $0.10/$0.50 per MTok would cost about $2/month for the whole stated load [12][27] (calc in section 6). Anthropic offers no EU data residency (only `us`/`global`) [23]. Keep anything with real portfolio data local.
6. Hardware upgrade is the only route to a clearly better local model class (14B-30B). A 5060 Ti 16 GB costs EUR 617 lowest (Amazon.de, 12 Nov 2026) [29]; used 3090 24 GB is roughly EUR 680-1,185 (undated listings) [86-87 SNIPPET]. Not required for the current workloads.
7. Honest model-quality caveat: on Vectara's summarisation-faithfulness board the Qwen3.5 family looks WORSE than older models: qwen3.5-35b-a3b 10.5%, qwen3.5-27b 12.1% vs qwen3-8b 4.8%, gemma-3-12b 4.4%, Phi-4 3.7% [2]. Qwen3.5-9B itself is not on the board, so the 9B's grounded-summary hallucination rate is unmeasured by Vectara. This justifies an A/B test of Gemma 4 E4B / Gemma 3 12B / Phi-4 on YOUR prompts, not a blind switch.

## 1. Candidate open-weight models (8 GB VRAM, or 8 GB + RAM offload)

### 1.1 Facts per family (every number has its source)

- Qwen3.5-9B (current): Apache-2.0, 262k native context; MMLU-Pro 82.5, IFEval 91.5, IFBench 64.5, MultiChallenge 54.5, GPQA-D 81.7, AA-LCR 63.0 [1]. Thinking is ON by default in the card; no /think soft switch [1][51]. Unsloth says reasoning is off by default for 0.8B/2B/4B/9B GGUF builds [3] (the model card says the opposite for API use; both are reported, hence the leak risk). Qwen3.5-4B: IFEval 89.8, MMLU-Pro 79.1 [51]. Release 2 Mar 2026 [95 SNIPPET].
- Qwen3.5-35B-A3B (MoE): Apache-2.0, IFEval 91.9, IFBench 70.2, MMLU-Pro 85.3, 40 layers, 256 experts [66]. Q4_K_XL is 19.17 GB (KLD table) [32] so it needs expert offload and ~21 GB RAM; on a 4060 8 GB with 32 GB RAM it ran 8.2-8.6 t/s using 30.8 GB RAM [19], too much for a 31 GB shared host.
- Gemma 4 (Google, Apache-2.0): E2B, E4B (128k ctx), 12B Unified (256k), 26B A4B MoE (3.8B active), 31B dense [4]. E4B: MMLU-Pro 69.4, GPQA-D 58.6, MRCR-128k 25.4; 12B: MMLU-Pro 77.2, MRCR 43.4 [4][55]. No IFEval or finance numbers in the card [4]. GGUF sizes E4B: Q4_K_M 4.98 GB, Q6_K 7.07, Q8_0 8.19 [35]. Gemma 4 12B Q4_K_M listed ~7.4 GB (repo listing, SNIPPET) so it barely fits 8 GB with no headroom [93-94 SNIPPET]. Recommended sampling temp 1.0, top_p 0.95, top_k 64 [55][35].
- Phi-4 14B (MIT): 16k context, MMLU 84.8, MATH 80.4, SimpleQA 3.0 (i.e. poor factual recall), no IFEval reported [43]. Needs ~9 GB at Q4, so partial offload on 8 GB. Vectara hallucination 3.7% [2] (answer rate reportedly 80.7%, from a search summary, unverified). Phi-4-mini: 23.5% [2]. No Phi-5 as of June 2026 per a low-authority post [97 SNIPPET].
- Ministral 3 (3B/8B/14B, Apache-2.0, 256k, native JSON/function calling; temp below 0.1 recommended) [41]. 8B instruct: Arena-Hard 0.509, MATH 0.876 [41]. No FinBench/Vectara entry found for it.
- Granite 4.0-H-Small (32B total/9B active MoE, Apache-2.0, 128k; German supported): IFEval 87.55 avg, GSM8K 87.27, BFCL v3 64.69 [42]; Vectara 5.2% [2]. Too big for 8 GB without MoE offload.
- gpt-oss-20b (21B/3.6B active, Apache-2.0, ~16 GB in MXFP4, native structured outputs, 3 reasoning levels) [59]; 63 t/s at 4k ctx on a 4060 Ti 16 GB [30]. Does not fit 8 GB without CPU-MoE offload.
- Tiny: LFM2.5-1.2B (IFEval 86.23, "recommended for data extraction/RAG", not for knowledge; sampling temp 0.1, rep-pen 1.05; licence "lfm1.0") [64]; SmolLM3-3B (Apache-2.0, IFEval 76.7 no-think) [65]. Candidates for a cheap extractor tier only.
- Qwen3 family still in Vectara: qwen3-8b 4.8%, 14b 5.4%, 4b 5.7%, 32b 5.9% [2].
- Finance-tuned: no open finance LLM in 2026 beat general models in anything I could verify. Evidence found: a LoRA-tuned Qwen3.5-4B reached 82.35% ConvFinQA execution accuracy vs 69.56% base (self-reported, English 10-K only) [54]; TAT-LLM (LLaMA-2 fine-tune, 2024) beat GPT-4 on FinQA/TAT-QA [40]. The Open FinLLM Leaderboard exists [46] but I found no 2026 standings. FinBen: 36 datasets/24 tasks, LLMs strong on IE/textual analysis, weaker on advanced reasoning [62]. FinanceBench: GPT-4-Turbo with retrieval wrongly answered or refused 81% [63] (2023, old).
- Finance agent benchmarks (Vals Finance Agent v2.1, 9 Nov 2026) are led by Claude Fable 5.2 69.47%, Opus 5.5 67.85% [52]; open models listed are far above 8 GB (e.g. Qwen3.8 27B 48.6% on v2) [5]. No <=14B model has a published finance-agent score.

### 1.2 Benchmarks you asked for, and the gaps
- Vectara HHEM (grounded summarisation, updated 22 Oct 2026): numbers above [2]. Caveat: summarisation of short passages, not numeric tables.
- openweightbench (12 false-premise questions, German prompts, n=12, steps of 8%): gemma-4-e4b gguf 8-bit 88%, 4-bit 71%; qwen3.5-9b gguf 8-bit 83%, mlx 4-bit 71%, gguf 4-bit 62%; phi absent; granite-4-h-tiny 54% [6]. Tiny sample, low authority, but it hints Q4 may hurt the 9B on that task.
- FAITH (masked numbers in S&P 500 10-K tables, 2,406 spans): Claude Sonnet 4 95.6%, Gemini 2.5 Pro 91.9%, GPT-4.1 89.2%, GPT-4.1-mini 88.2%, Llama-3.1-8B 47.5%, Qwen3-8B 30.6%; dominant errors: scale (drops "million"), chained calculation, ignoring table rows [21]. This is the closest published match to your failure mode (units/bases). Qwen3.5-9B, Gemma 4, Phi-4 not evaluated.
- FACTS Grounding, FinQA/TAT-QA/ConvFinQA for current small models, JSONSchemaBench per-model numbers, Artificial Analysis for the 9B non-reasoning variant: no usable numbers obtained [56][26][36][53].
- Artificial Analysis lists Qwen3.5-9B (reasoning) at $0.14/$0.20 median provider price and 81 t/s, Intelligence Index not available [26].

### 1.3 Comparison table (best candidates for this host)

| # | Model | Size / quant that fits 8 GB | Licence | Key numbers (source) | Fit / risk |
|---|---|---|---|---|---|
| 1 | Qwen3.5-9B (current) | UD-Q4_K_XL 5.97 GB; Q5_K_M 6.58; UD-Q5_K_XL 6.74; Q6_K 7.46 (too big) [25] | Apache-2.0 | IFEval 91.5, MMLU-Pro 82.5 [1]; Vectara: not listed (family 10.5-12.1%) [2]; openweightbench 62% Q4 vs 83% Q8 (n=12) [6] | Strong IF; thinking-leak and loop risk [34]; grounded-summary rate unmeasured |
| 2 | Qwen3.5-4B | Q8 ~4.5 GB est. (size not verified) | Apache-2.0 | IFEval 89.8, MMLU-Pro 79.1 [51]; ConvFinQA 69.6 base, 82.35 LoRA [54] | Cheap tier for tone/extraction; leaves VRAM for parallel slots |
| 3 | Gemma 4 E4B | Q4_K_M 4.98, Q6_K 7.07 GB [35] | Apache-2.0 | MMLU-Pro 69.4 [4]; openweightbench 88% (Q8) / 71% (Q4) [6]; Vectara not listed (26b-a4b 5.2%, 31b 7.4%) [2] | Best-looking "grounded/false-premise" behaviour in a small model I could find, unproven on tables |
| 4 | Gemma 4 12B | Q4_K_M ~7.4 GB (SNIPPET) [93] | Apache-2.0 | MMLU-Pro 77.2, GPQA 78.8, MRCR 43.4 [4]; openweightbench 71% (QAT Q4) [6] | Barely fits; ~4 t/s if it spills (estimate) [SNIPPET]; better on 16 GB card |
| 5 | Gemma 3 12B | Q4 ~7 GB | Gemma licence (not Apache; not re-verified) | Vectara 4.4% [2] | Older; good faithfulness number |
| 6 | Phi-4 14B | Q4 ~9 GB -> partial offload | MIT | MMLU 84.8, MATH 80.4, SimpleQA 3.0 [43]; Vectara 3.7% [2] | Best Vectara score among sub-15B but 16k ctx and weak factual recall |
| 7 | Qwen3-8B | Q4 ~5 GB | Apache-2.0 | Vectara 4.8% [2]; FAITH 30.6% [21]; non-thinking temp 0.7 [57] | Older, steadier on faithfulness board than Qwen3.5 |
| 8 | Ministral 3 8B/14B | 8B Q4 ~5 GB | Apache-2.0 | Arena-Hard 0.509 / MATH 0.876 (8B) [41] | Native JSON; temp <0.1 recommended; no grounding benchmark found |
| 9 | Granite 4.0-H-Small | 32B/9B-active, ~19 GB Q4 (needs MoE offload) | Apache-2.0 | IFEval 87.55, Vectara 5.2% [42][2] | Interesting for enterprise-style RAG, too big for current GPU |
| 10 | Qwen3.5-35B-A3B | Q4_K_XL 19.2 GB, expert offload | Apache-2.0 | IFEval 91.9, MMLU-Pro 85.3 [66] | RAM 21-31 GB on a shared 31 GB host: risky |
| 11 | gpt-oss-20b | MXFP4 ~13-16 GB | Apache-2.0 | GPQA-D 58.6-66 [59]; 63 t/s on 16 GB card [30] | Needs 16 GB GPU or MoE offload |
| 12 | LFM2.5-1.2B | <2.5 GB | lfm1.0 | IFEval 86.23 [64] | Sentiment/extraction only |

## 2. Quantisation

- Weight sizes of the current model: Q4_K_M 5.68 GB, UD-Q4_K_XL 5.97, Q5_K_M 6.58, UD-Q5_K_XL 6.74, Q6_K 7.46, UD-Q6_K_XL 8.76, Q8_0 9.53, BF16 17.9 [25]. Your 6.9 GB observed total at 32k ctx implies ~0.9 GB for KV, compute buffers and CUDA context.
- Evidence that Q4 is nearly lossless: Llama-3.1-8B GSM8K 77.63 (F16) vs 77.41 (Q4_K_M), IFEval 78.93 vs 79.06, TruthfulQA 54.79 vs 54.49; standard error ~1 point [7]. Qwen3.6-27B: all >=4-bit quants within noise on AIME; KLD <0.05 tracks BF16, >0.08 degrades [9]. Kaitchup on Qwen3.5: "Q4 overall is very safe" except where memory forces Q3/Q2; drops show up on MMLU-Pro-style reasoning more than Math500; error shifts within +/-10% are noise [8].
- Evidence for going up: on Qwen3.5-35B-A3B, UD-Q4_K_XL -> UD-Q5_K_XL adds ~4 GB and halves mean KLD (0.0137 -> 0.0069) [32]; UD-Q4_K_XL beats bartowski Q4_K_M at similar size (0.0137 vs 0.0182) [32]. Unsloth warns lower KLD does not always mean better task scores [32]. No KLD/accuracy table for the 9B was readable (an image only) [32][17].
- openweightbench (n=12) is the only Qwen3.5-9B-specific datapoint: Q4 62% vs Q8 83% [6]; far too small to act on, but cheap to replicate with your own 30-case set.
- Practical: UD-Q5_K_XL (6.74 GB) is the largest realistic upgrade on 8 GB; it needs a smaller context (see below). Q6_K (7.46 GB) will not leave room for KV/compute buffers on an 8 GB card (my arithmetic).
- "Smaller model at higher precision vs larger at Q4": no controlled study found. The only related statement (Kaitchup) says that if Q3 or lower would be needed, use a smaller model instead [8].
- Context cost: Qwen3.5-9B is a hybrid (24 Gated DeltaNet + 8 full-attention layers, 4 KV heads, head dim 256) so only 8 layers keep a growing KV cache: ~32 KiB/token in bf16, i.e. ~1 GiB at 32k per sequence (a search-summary calculation, not a measured figure) [SNIPPET: search on Gated DeltaNet KV; architecture partially corroborated by 66 and 18]. GDN state is fixed and small ("tens of MB") [18]. So dropping ctx from 32768 to 16384 frees roughly 0.5 GiB; with `--parallel N` the shared cache must hold N x T [60]. Quantised KV (q8_0) would halve it; Unsloth notes bf16 KV cache fixed gibberish at too-low context [3].

## 3. Sampler and llama.cpp settings

### 3.1 Vendor recommendations (Qwen3.5-9B card, mirrored by Unsloth)
- Thinking, general: temp 1.0, top_p 0.95, top_k 20, min_p 0, presence 1.5, rep 1.0.
- Non-thinking, general: temp 0.7, top_p 0.8, top_k 20, min_p 0, presence 1.5, rep 1.0 (exactly your current setting).
- Non-thinking, reasoning tasks: sections disagree (top_p 0.95/top_k 20/presence 1.5 vs top_p 1.0/top_k 40/presence 2.0) [1][3].
- Qwen3-8B card: do NOT use greedy decoding ("endless repetitions"); presence_penalty 0-2 to reduce repetition but "higher values may cause language mixing and a slight drop in performance" [57]. This is why temp 0 is risky for Qwen-family models; use a low but non-zero temperature plus grammar/n_predict caps.
- Ministral 3: temp below 0.1 for production [41]. LFM2.5: temp 0.1 [64]. Gemma 4: temp 1.0/top_p 0.95/top_k 64 for all uses [55].

### 3.2 Evidence on temperature and penalties
- Temperature 0.0-1.0 had no statistically significant effect on multiple-choice problem solving across nine LLMs [45] (not about open-ended generation or JSON stability).
- Presence/repetition penalties harm structured output in theory (penalise braces, quotes, repeated keys and digits) but the only support is one anecdotal blog [47 low authority]; llama.cpp defaults are presence 0, repeat 1.0, temp 0.8 [48]. A grammar guarantees syntax but not correct values [47]. I found no controlled study of digits corruption.
- Guidance from a practitioner write-up: for strict grounded output use temp ~0-0.3, min_p 0.05-0.1, no repeat penalty, DRY sampler for loops, fixed seed, cap tokens, use `--json-schema` [48].
- Reasoning on/off: for financial sentiment, chain-of-thought/reasoning models did NOT improve (GPT-4o without CoT best) [49]. For numeric fidelity of a 9B writing from a table I found no evidence either way; at 9B, thinking used 8,095 of 8,192 context tokens on one task [19]. Treat as untested.
- Verbalised confidence: see section 7.

### 3.3 llama.cpp mechanics (from the official server README and grammar README)
- `n_predict` default -1 (unlimited): this is why runaway calls hit your 120 s timeout. Set it per request (e.g. 160 for tone JSON, 1,400-1,800 for a section report) [10].
- `json_schema` and `response_format` (OpenAI wrapper) constrain sampling; the schema is NOT injected into the prompt, you must describe fields in the prompt; unsupported keywords are silently skipped; `additionalProperties` defaults false; number min/max apply only to integers; use `x{0,N}` not `x? x?` for speed; properties are emitted in listed order [10][22]. Put numbers-as-strings and enums in the schema, and `maxLength` to bound free text.
- `cache_prompt` defaults true (reuses KV for the shared prefix); may cause tiny non-determinism across batch sizes [10]. Put the fixed instructions first and the headlines last to get prefix reuse.
- `--parallel` / continuous batching: on by default; slot save/restore via `--slot-save-path`; `-c` is one shared KV pool (size it as P x T) and attention is computed over the whole cache then masked, so many long slots waste compute [10][60]. No measured aggregate-throughput figure for 2-4 slots on an 8 GB 3070 was found; since decoding is bandwidth-bound it should help for 530 independent short calls, but test it (unverified).
- `--chat-template-kwargs '{"enable_thinking":false}'` is the documented server-wide switch [10]; issue #20182 shows it failing for Qwen3.5-9B for some users, `--reasoning-budget 0` did not fix it for several, a hard-coded empty think block in the Jinja template did [34]. Verify by checking that responses contain no `<think>` and that completion_tokens is small.
- JSONSchemaBench evaluates llama.cpp's grammar module among six constrained-decoding engines, finding constrained decoding can speed generation by up to 50% [36 abs; 36 snippet]. Per-engine coverage numbers were not retrieved.
- Known pitfall: Ollama may ignore presence_penalty set through the API (Ollama issue) [76 SNIPPET]; irrelevant on llama-server but relevant if the app ever sits behind Ollama.

## 4. Sentiment

- Encoder: ProsusAI/FinBERT is ~0.1B, fine-tuned on Financial PhraseBank, licence CC BY-NC-SA 4.0 (non-commercial) [44]. Fine for a personal app; a licence problem if commercialised.
- PhraseBank all-agree subset (n=2,264), 2026 paper: FinBERT 97.2% (partly trained on the data), FinBERT-Tone 91.7%, Qwen2.5-3B 92.1%, Mistral-7B 88.7%, Llama-3.2-3B 82.3%, Gemma-3-1B 73.0% [68 SNIPPET; PDF could not be parsed]. So small LLMs tie the weaker encoder, not the stronger one.
- Fine-tuned LLMs (Qwen3-8B, Llama3-8B, DeepSeek-7B) reach 0.93 on PhraseBank, 0.84 on FiQA; FinBERT excluded on those (trained on them) and scored 0.56-0.68 on two other sets after fine-tuning [11]. The table has an apparent transcription duplication in the paper [11].
- ModernBERT-based: ModernFinBERT-base 82.32% vs FinBERT-Tone 79.18% (model card) [70 SNIPPET]; a thesis found FinBERT (91.49%) beat ModernBERT-base (87.93%) and large (88.82%) on news [71 SNIPPET]. No German/French financial-encoder benchmark found (unverified).
- GPT-4 few-shot approaches fine-tuned FinBERT on PhraseBank [69 SNIPPET].
- Return predictability: Lopez-Lira and Tang: GPT-4 headline scores predict next-day returns and drift, ability rises with model size, and returns decline as LLM adoption rises [37]. Kirtac and Germano: fine-tuned OPT-2.7B directional accuracy 74.4% vs BERT 72.5%, FinBERT 72.2%, Loughran-McDonald 50.1%; long-short Sharpe 3.05 vs 2.11 and 2.07 after 10 bps costs, single 2021-23 window, no significance test, data not reproducible [28]. Conclusion: LLM scores may add a bit over encoders, but the gain is small, in-sample-ish, and shrinking; the encoder is the cheap baseline to beat. Note OPT here was fine-tuned on return-sign labels, not zero-shot prompted.
- Cost/throughput: FinBERT on CPU is milliseconds per headline vs ~7 s per call now; 530 calls would take well under a minute. (Latency figure is my engineering estimate, not sourced.)

## 5. Cloud option: prices (USD per million tokens)

| Provider/model | Input | Output | Batch | Cache read | Source |
|---|---|---|---|---|---|
| Claude Haiku 5.5 (<=100k prompt) | 0.10 | 0.50 | -50% | 0.01 | [12][27] |
| Claude Sonnet 5.5 | 2 | 10 | -50% | 0.10 | [12] |
| Claude Haiku 4.5 | 1 | 5 | -50% | 0.10 | [12] |
| OpenAI gpt-5-nano | 0.05 | 0.40 | -50% | 0.005 | [15] |
| OpenAI gpt-5.4-nano | 0.20 | 1.25 | -50% | 0.02 | [15] |
| OpenAI gpt-5-mini | 0.25 | 2.00 | -50% | 0.025 | [15] |
| OpenAI gpt-5.4-mini | 0.75 | 4.50 | -50% | 0.075 | [15] |
| Gemini 3.1 Flash-Lite | 0.25 | 1.50 | 0.125/0.75 | 0.025 | [14] |
| Gemini 3.5 Flash-Lite | 0.30 | 2.50 | 0.15/1.25 | 0.03 | [14] |
| Gemini 3.6/3.8 Flash (to 31 Dec 2026; doubles 1 Jan 2027) | 0.75 | 3.75 | -50% | 0.075 | [14] |
| DeepSeek V4.1 Flash (peak / off-peak) | 0.30 / 0.15 miss | 1.20 / 0.60 | n/a | 0.006 / 0.003 | [13] |
| Mistral Ministral 3 8B / 14B | 0.15 / 0.20 | 0.15 / 0.20 | -50% (page) | up to -90% | [16][106 SNIPPET] |
| Mistral Small 4 | 0.10-0.15 | 0.30-0.60 (sources disagree) | | | [104-105 SNIPPET] |
| DeepInfra Qwen3.5-9B (listed "zero retention") | 0.10 | 0.15 | | | [61] |
| gpt-oss-20b via Groq/Together/OpenRouter | 0.05-0.075 | 0.20-0.30 (OpenRouter 0.018-0.03 / 0.09-0.13) | | | [100-103 SNIPPET aggregators] |

Notes: Haiku 5.5 uses the newer tokenizer (~30% more tokens for the same text than Haiku 4.5) [12][27]; my token counts below do not inflate for this. Gemini paid tier does not use content to improve products; the free tier may [14]. Gemini Flash-Lite output includes thinking tokens, so real cost can be higher [14].

### Estimated monthly cost (assumptions mine: tone call ~600 in / ~100 out tokens x 530 x 30 nights = 9.5M in / 1.6M out; reports 20/week x 4.35 = 87/month x 6k in / 1.5k out = 0.52M in / 0.13M out; total ~10.1M in / ~1.7M out)

| Model | Cost per month |
|---|---|
| Claude Haiku 5.5 | ~$1.9 (batch ~$0.9) |
| Claude Sonnet 5.5, reports only (0.52M/0.13M) | ~$2.3 (batch ~$1.2) |
| gpt-5-nano | ~$1.2 |
| gpt-5.4-nano | ~$4.2 |
| gpt-5-mini | ~$5.9 (reasoning tokens extra) |
| Gemini 3.1 Flash-Lite | ~$5.1 |
| Gemini 3.5 Flash-Lite | ~$7.3 |
| DeepSeek V4.1 Flash | ~$5.1 peak / ~$2.5 off-peak |
| Ministral 3 8B | ~$1.8 |
| DeepInfra Qwen3.5-9B | ~$1.3 |
| Local | electricity only; no extra cost |

### Privacy and residency
- Anthropic API: conversation content "not retained by default", retained data never used for training without permission; Covered Models (Fable/Mythos 5.x) require 30-day retention and no ZDR; Haiku 5.5 is not listed as Covered [24]. Data residency: only `inference_geo` of `us` or `global`, workspace geo only `us`; NO EU option [23].
- Gemini: paid tier content not used to improve products [14]. OpenAI/Mistral/Together/Groq retention terms and Mistral EU regional inference: not verified (Mistral's page links a "Regional inference" page I could not read) [58].
- DeepInfra lists Qwen3.5-9B as "Zero retention" (provider claim) [61].
- Recommendation: use cloud only for public-data jobs (news tone from public headlines; reports built from public fundamentals). Anything containing holdings, cash, or tax data stays local.

## 6. Hardware (EU, late 2026)

- RTX 5060 Ti 16 GB: lowest EUR 617 (Amazon.de, updated 12 Nov 2026), 12-month low EUR 429, median EUR 569 [29]; launch list price EUR 449 [84 SNIPPET]; reports of rising prices to ~EUR 573 earlier [85 SNIPPET].
- Used RTX 3090 24 GB: eBay.de listings roughly EUR 680-1,185, mostly undated; one aggregator ~EUR 843; UK low GBP 975.70 on 21 Oct 2026 [86-87 SNIPPET]. No used-4060-Ti-16GB price found.
- Speed: 4060 Ti 16 GB, Q4: Qwen3 8B 45.8/34.3/25.5 t/s at 4k/16k/32k ctx, Qwen3 14B 27.4/22.4/17.9, gpt-oss-20B 63.2/57.8/51.5 [30]. 5060 Ti about 1.4-1.5x the 4060 Ti on 14B Q4 (32-33 vs 22 t/s at ~16k, from search summaries; attribution to a single page unclear) [88 SNIPPET]. Your 3070 8 GB runs the 9B at 54-58 t/s [73 SNIPPET] and ~33-37 t/s on a 4060 8 GB [19].
- What 16 GB unlocks: 12-14B at Q5/Q6 (Phi-4, Gemma 4 12B, Ministral 3 14B), gpt-oss-20b, higher-quant Qwen3.5-9B with long context and parallel slots. 24 GB unlocks 27-31B dense at Q4 (Gemma 4 31B, Qwen3.6-27B); sizes of these GGUFs were not verified.
- CPU-only: bandwidth-bound; dual-channel DDR4-3200 14B Q4 ~2-5 t/s (guide estimate) [89 SNIPPET]; DDR4 desktop 13B ~4 t/s, DDR5 ~7 t/s [31]. A 5900 would therefore give ~5 t/s at best for a 9 GB model (arithmetic). No Ryzen 9 5900 benchmark and no 30B-A3B CPU benchmark was found. The one 8 GB-GPU MoE-offload figure was a Medium post (8.7 -> 40+ t/s with `-ncmoe`, on Qwen3.6-35B-A3B, 3070 Ti) which I could not open (403) [SNIPPET, unverified].

## 7. Counter-evidence and structure over model size

- FAITH: even Claude Sonnet 4 and Gemini 2.5 Pro show roughly 10-20% error on multi-step numerical reasoning; the paper's own examples show frontier models skipping a table row and scale errors (dropping "million"); adding a value-type hint ("percentage or absolute") restored answer uniqueness [21]. Lesson: units/base errors persist at scale; put units/bases in structured fields.
- FinanceBench: GPT-4-Turbo with retrieval answered wrongly or refused 81%, hallucinations limited enterprise use [63] (2023).
- Faithfulness is not monotone in size: Intel's 7B Neural Chat had 2.8% vs GPT-4 3% on HHEM [33]; Qwen3.5-35B/27B (10.5%/12.1%) are worse than Qwen3-8B (4.8%) [2]. FaithJudge shows leaderboard changes when judged by human-annotated examples [38].
- Verbalised confidence: models are overconfident, calibration improves with scale, no single elicitation method wins, all struggle on professional-knowledge tasks [39]. Small models: gemma1.1-2b scores nearly independent of accuracy (via a summary of arXiv 2412.14737 [20]; the abstract I read only says prompting matters, the size claim is from a search summary); Gemma 3 4B gave >95% confidence with near-chance AUROC (0.554) [50]; ten-sample self-consistency reached AUROC 0.999 vs 0.701 for single-pass logit entropy on TriviaQA [50]; small VLMs' verbalised confidence unobtainable while token probabilities separated errors [90 SNIPPET]. Your "identical numbers across tickers" is exactly the binary-confidence failure. Compute confidence outside the model.
- A blog claims RAG cut error ~40% and self-verification helped numerics [91 low authority, unreviewed]; no template-verification study was found. This is an engineering recommendation, not a cited result.

## 8. Recommended configuration per workload

Common: keep llama-server; add `--chat-template-kwargs '{"enable_thinking":false}'` AND per-request `chat_template_kwargs`; check for `<think>` in outputs; log completion_tokens; update llama.cpp (the thinking bug was build-dependent) [34].

1. Nightly news tone (530 calls): preferred = FinBERT-class encoder as primary score, LLM only for non-English or ambiguous headlines. If LLM retained: `response_format` json_schema with enum label + number fields, `max_tokens` 160, temp 0.2, top_p 0.8, top_k 20, presence_penalty 0 (the 1.5 card value is untested for JSON; keep as the A/B arm), `--parallel 3-4` with `-c 4*2048` (small headline prompts), headlines last in prompt for `cache_prompt` reuse. Expected: no 120 s runaways; run time shrinks from ~62 min sequential. Cloud fallback for public headlines: Haiku 5.5 batch (~$0.9/month).
2. 7-section reports: compute every figure, unit, base, peer-vs-own flag and qualitative band (e.g. "23% = unusually high, PEER-DERIVED, do not call exceptional") in Python and hand the model a rendered fact sheet; one schema'd call per section; temp 0.3-0.5 (Qwen card minimum 0.7 for non-thinking; trade-off untested), `max_tokens` ~1,000/section, presence 0 to 0.5; post-check: regex all numbers in the output against the fact sheet, reject/regen on mismatch or on banned words when the flag is set. Context 16k is enough (6k in + 1.5k out). Consider UD-Q5_K_XL.
3. Verdicts (multi-horizon BUY/HOLD/SELL): enum schema; ask for verdict only; derive confidence from agreement across 5 samples (temp 0.7) and/or the probability of the verdict token (`n_probs`), then calibrate against realised outcomes in the existing track-record tables. Do not store the self-stated number.
4. Factor ideas (AlphaCrafter): temp 0.7-1.0, thinking ON with a budget is plausible here (reasoning helps open-ended search; untested), outputs validated by the existing backtest/ledger gate, not by the LLM. Offline, so speed is not critical.
5. Portfolio-advisor reasoning/reflection: stays local (private data). If quality is the problem, the 16 GB GPU path (Gemma 4 12B / Phi-4 / Ministral 14B at Q5-Q6) is the upgrade; before buying, run the A/B harness on a rented-by-the-hour 16 GB GPU or on Gemma 4 E4B/12B locally.
6. A/B harness before any model change: 30-50 frozen prompts with known-correct labels (units, own-vs-peer FCF, JSON validity, runaway rate, sample-agreement), score Qwen3.5-9B Q4 vs UD-Q5_K_XL vs Gemma 4 E4B Q6_K vs Gemma 3 12B / Phi-4 (partial offload). Evidence I found cannot rank them on grounded numeric writing.

## 9. Claims I could not verify / gaps

- Qwen3.5-9B on Vectara HHEM, FACTS, FinQA/TAT-QA/ConvFinQA, FAITH, JSONSchemaBench: no numbers found [2][56][36].
- Gemma 4 E4B/12B IFEval and finance scores: not published in the card [4]; benchlm/datalearner figures were aggregator-only.
- Qwen3.5-9B KV size of ~1 GiB at 32k (derived from a search-summary of layer counts, not measured) and the claim that Q6_K will not fit (my arithmetic).
- Quant effect on the 9B for numeric tasks: only n=12 data [6]; the Benjamin Marie 9B GGUF evals are an image [32].
- Aggregate throughput with `--parallel` on the 3070: no source.
- Whether presence_penalty 1.5 corrupts digits in constrained JSON: only an anecdotal blog [47].
- Whether thinking improves numeric fidelity at 9B: no source.
- Claude Haiku 5.5 quality vs Qwen3.5-9B on your tasks: no benchmark retrieved; price/limits only [27].
- Mistral/OpenAI/Groq/Together retention terms, Mistral Small 4 price (sources disagree 0.10-0.15 / 0.30-0.60), Mistral EU hosting, Scaleway EU prices (page 403).
- Used 3090/4060 Ti EUR prices (undated listings), Intel Arc/AMD 16 GB options: not researched (search budget ended).
- FinBERT-class multilingual (German/French) benchmark, ModernBERT financial model comparisons beyond one model card and one thesis.
- Search-budget note: the tool's web-search quota ran out mid-session; planned queries on Fin-R1, FACTS leaderboard numbers, gpt-oss-20b 8 GB offload speeds and EU providers were not run.
- Llama 4, DeepSeek small distills, SmolLM beyond 3B, NVIDIA Nemotron nano, LFM2 larger variants: only incidental mentions; no data gathered. openweightbench lists nemotron-3-nano-4b 50%, granite-4-h-tiny 54%, ministral-3-14b-reasoning 58% [6].
- Model/benchmark names in some aggregator snippets (e.g. "GPT-6 Astra") could not be matched to a primary source and were not used.

## 10. Source list (107 distinct URLs; 67 READ, 40 SNIPPET-only)

Fetch attempts that failed and are NOT counted: openai.com/api/pricing (403), knightli.com Gemma 4 12B article (404), scaleway.com pricing (403), bestvaluegpu.com (429), medium.com ncmoe article (403), github llama.cpp discussions via first attempt OK, tomshardware 5060 Ti vs 4060 Ti (title only).

READ pages
1. https://huggingface.co/Qwen/Qwen3.5-9B - READ: specs, benchmarks, sampling, thinking switch
2. https://github.com/vectara/hallucination-leaderboard - READ: HHEM table updated 22 Oct 2026
3. https://unsloth.ai/docs/models/qwen3.5 - READ: small-model settings, quant advice, llama.cpp cmd, known issues
4. https://ai.google.dev/gemma/docs/core/model_card_4 - READ: Gemma 4 sizes, ctx, licence, benchmarks
5. https://sec-api.io/resources/best-open-source-models-for-financial-analysis - READ: open-weight finance ranking; no <=14B
6. https://openweightbench.pages.dev/benchmarks/hallucination.html - READ: 12-question false-premise bench by quant (low authority, n=12)
7. https://arxiv.org/html/2601.14277v1 - READ: llama.cpp quant study on Llama-3.1-8B
8. https://kaitchup.substack.com/p/summary-of-qwen35-gguf-evaluations - READ: Qwen3.5 GGUF eval summary
9. https://quesma.com/blog/qwen-quantization-quality/ - READ: Qwen3.6-27B quant quality
10. https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md - READ (first 100k chars): n_predict, cache_prompt, schema, parallel, slots, reasoning flags
11. https://arxiv.org/html/2512.00946v1 - READ: FinBERT vs Qwen3/Llama3/DeepSeek fine-tuned sentiment
12. https://platform.claude.com/docs/en/about-claude/pricing - READ: Claude prices, batch, caching
13. https://api-docs.deepseek.com/quick_start/pricing - READ: DeepSeek V4.1 Flash prices
14. https://aistudio.google.com/docs/pricing - READ (first 100k chars): Gemini prices, batch, data use
15. https://developers.openai.com/api/docs/pricing - READ: OpenAI mini/nano prices
16. https://mistral.ai/pricing - READ: only batch/cache terms, no small-model prices
17. https://cmh-qwen3-5-9b-gguf-quant-drift.static.hf.space/index.html - READ: method only, table not rendered
18. https://trilogyai.substack.com/p/deep-dive-qwen-35-brings-native-multimodality - READ: Qwen3.5 hybrid architecture, small GDN state
19. https://dev.to/plasmon_imp/i-pitted-3-qwen35-models-against-each-other-on-an-rtx-4060-8gb-what-spec-sheets-dont-tell-you-ko0 - READ: 9B/27B/35B-A3B speeds on 8 GB (practitioner post, single runs)
20. https://arxiv.org/abs/2412.14737 - READ: verbalised confidence abstract (size claim only via search summary)
21. https://arxiv.org/html/2508.05201 - READ: FAITH tabular hallucination benchmark
22. https://raw.githubusercontent.com/ggml-org/llama.cpp/master/grammars/README.md - READ: JSON-schema-to-grammar limits
23. https://platform.claude.com/docs/en/manage-claude/data-residency - READ: only us/global geos
24. https://platform.claude.com/docs/en/manage-claude/api-and-data-retention - READ (via saved file, grep): retention/ZDR/Covered Models
25. https://huggingface.co/unsloth/Qwen3.5-9B-GGUF - READ: GGUF sizes per quant
26. https://artificialanalysis.ai/models/qwen3-5-9b - READ: price/speed; no index
27. https://platform.claude.com/docs/en/models/haiku-5-5/overview - READ: Haiku 5.5 specs/prices
28. https://arxiv.org/html/2412.19245v1 - READ: Kirtac and Germano method/results/caveats
29. https://gpuprix.com/de/germany/gpus/geforce-rtx-5060-ti-16gb - READ: EUR 617 low, 12 Nov 2026
30. https://www.hardware-corner.net/gpu-llm-benchmarks/rtx-4060-ti-16gb/ - READ: 4060 Ti 16GB t/s by ctx
31. https://insiderllm.com/guides/cpu-only-llms-what-actually-works/ - READ: CPU-only t/s
32. https://unsloth.ai/docs/de/modelle/qwen3.5/gguf-benchmarks - READ: KLD table for 35B-A3B
33. https://www.intel.com/content/www/us/en/developer/articles/technical/do-smaller-models-hallucinate-more.html - READ: HHEM size vs hallucination (older)
34. https://github.com/ggml-org/llama.cpp/issues/20182 - READ: enable_thinking:false ignored issue
35. https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF - READ: Gemma 4 E4B GGUF sizes
36. https://arxiv.org/abs/2501.10868 - READ: JSONSchemaBench abstract, no per-engine numbers
37. https://arxiv.org/abs/2304.07619 - READ: Lopez-Lira and Tang abstract
38. https://arxiv.org/abs/2505.04847 - READ: Vectara FaithJudge abstract
39. https://arxiv.org/abs/2306.13063 - READ: LLM uncertainty verbalisation abstract
40. https://arxiv.org/abs/2401.13223 - READ: TAT-LLM abstract
41. https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512 - READ: Ministral 3 8B card
42. https://huggingface.co/ibm-granite/granite-4.0-h-small - READ: Granite card
43. https://huggingface.co/microsoft/phi-4 - READ: Phi-4 card
44. https://huggingface.co/ProsusAI/finbert - READ: FinBERT size/licence/labels
45. https://arxiv.org/abs/2402.05201 - READ: temperature effect abstract
46. https://arxiv.org/abs/2501.10963 - READ: Open FinLLM Leaderboard abstract
47. https://dev.to/ji_ai/why-repetition-penalty-breaks-json-and-code-generation-4bbk - READ: repetition-penalty/JSON argument (LOW authority, anecdotal)
48. https://blog.alexewerlof.com/p/sampling-args-in-llama-server - READ: llama-server sampler defaults and extraction advice (blog)
49. https://www.arxiv.org/abs/2506.04574 - READ: reasoning does not help financial sentiment
50. https://www.alphaxiv.org/abs/2604.24070 - READ: Gemma 3 4B verbal confidence
51. https://huggingface.co/Qwen/Qwen3.5-4B - READ: 4B benchmarks
52. https://www.vals.ai/benchmarks/fabv2-1 - READ: Finance Agent v2.1 leaders
53. https://artificialanalysis.ai/models/comparisons/gemma-4-26b-a4b-vs-qwen3-5-122b-a10b - READ: no useful numbers
54. https://featherless.ai/models/sharick008/convfinqa-qwen3.5-4b - READ: LoRA ConvFinQA (self-reported)
55. https://huggingface.co/google/gemma-4-E4B-it - READ: E4B card
56. https://www.kaggle.com/benchmarks/google/facts-grounding - READ: no scores returned
57. https://huggingface.co/Qwen/Qwen3-8B - READ: greedy warning, presence penalty note
58. https://docs.mistral.ai/getting-started/models/models_overview/ - READ: Mistral small models list; no prices/EU text
59. https://huggingface.co/openai/gpt-oss-20b - READ: gpt-oss-20b card
60. https://github.com/ggml-org/llama.cpp/discussions/4130 - READ: shared KV across parallel slots
61. https://deepinfra.com/Qwen/Qwen3.5-9B - READ: price, zero-retention label
62. https://arxiv.org/abs/2402.12659 - READ: FinBen abstract
63. https://arxiv.org/abs/2311.11944 - READ: FinanceBench abstract
64. https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct - READ: LFM2.5 card
65. https://huggingface.co/HuggingFaceTB/SmolLM3-3B - READ: SmolLM3 card
66. https://huggingface.co/Qwen/Qwen3.5-35B-A3B - READ: 35B-A3B card
67. https://llm-stats.com/benchmarks/category/finance - READ: nothing for <=14B

SNIPPET-only (search summaries)
68. https://arxiv.org/pdf/2609.20198 - SNIPPET (PDF not parseable): PhraseBank FinBERT vs small LLMs
69. https://arxiv.org/pdf/2305.05862 - SNIPPET: GPT-4 few-shot vs FinBERT
70. https://huggingface.co/neoyipeng/ModernFinBERT-base/commit/2a9520cc3d3f5201899a9ab72b3a08f2635c5080 - SNIPPET: ModernFinBERT accuracy
71. https://norma.ncirl.ie/9572/ - SNIPPET: thesis FinBERT vs ModernBERT
72. https://themoonlight.io/en/review/pre-trained-large-language-models-for-financial-sentiment-analysis - SNIPPET: Llama-2-7B SFT 0.90 vs FinBERT 0.86 (secondary)
73. https://www.microcenter.com/site/mc-news/article/best-local-llms-8gb-16gb-32gb-memory-guide.aspx - SNIPPET: 8 GB guide, 3070 54-58 t/s claim came from another search-listed guide (attribution uncertain)
74. https://github.com/ggml-org/llama.cpp/issues/20837 - SNIPPET: ROCm Qwen3.5-9B UD-Q4_K_XL issue
75. https://forum.level1techs.com/t/llama-cpp-qwen-devolves-into-repeating-s/254357 - SNIPPET (forum, LOW)
76. https://github.com/ollama/ollama/issues/14493 - SNIPPET: Ollama ignores presence_penalty
77. https://arxiv.org/pdf/2504.17550 - SNIPPET: HalluLens older small-model rates
78. https://benchmarklist.com/benchmarks/vectara_hhem/ - SNIPPET (garbled; not relied on)
79. https://cdn04132025.gitlink.org.cn/replica/llama.cpp/commit/8a308354f6520df6bea851b435bd8054ee5617b4 - SNIPPET (mirror): response_format support commit
80. https://docs.liquid.ai/deployment/on-device/llama-cpp/structured-output - SNIPPET: llama.cpp structured output usage
81. https://inventivehq.com/blog/gguf-quantization-quality-benchmark - SNIPPET: blog quant tests
82. https://docs.unsloth.ai/basics/unsloth-dynamic-2.0-ggufs - SNIPPET: Dynamic 2.0 MMLU Q4_K_M 71.23 vs XL 71.47 (older model)
83. https://hn.svelte.dev/item/47192505 - SNIPPET (HN comments, LOW)
84. https://www.pcgameshardware.de/Geforce-RTX-5060-Ti-16GB-Grafikkarte-281131/News/Release-Preise-Shops-Verfuegbarkeit-wo-die-neuen-GPUs-kaufen-1470629/ - SNIPPET: EUR 449 launch
85. https://www.notebookcheck.com/GPU-Preisexplosion-RTX-5060-Ti-16GB-jetzt-fast-so-teuer-wie-die-RTX-5070.1224735.0.html - SNIPPET: ~EUR 573
86. https://gpudojo.com/uk/rtx-3090 - SNIPPET: GBP 975.70 low 21 Oct 2026
87. https://bestvaluegpu.com/en-eu/history/new-and-used-rtx-3090-price-history-and-specs/ - SNIPPET (fetch 429): ~EUR 843 used
88. https://insiderllm.com/guides/rtx-5060-ti-local-ai-benchmarks/ - SNIPPET: 5060 Ti ~1.4-1.5x 4060 Ti (figure attribution unclear)
89. https://bestllmfor.com/guides/phi-4-14b-cpu-only-inference-speed/ - SNIPPET: 2-5 t/s on DDR4 for 14B
90. https://www.alphaxiv.org/abs/2607.22034 - SNIPPET: small VLM verbalised confidence
91. https://hackernoon.com/when-ai-makes-things-up-the-hallucination-problem-in-financial-reporting - SNIPPET (LOW, unreviewed blog)
92. https://llm-stats.com/leaderboards/best-ai-for-finance - SNIPPET: Qwen top of finance board (conflicts with 67)
93. https://www.analyticsvidhya.com/blog/2026/06/google-gemma-4-12b/ - SNIPPET: Gemma 4 12B launch 3 Jun 2026, size
94. https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/ - SNIPPET: Gemma 4 12B announcement
95. https://rits.shanghai.nyu.edu/ai/qwen-3-5-small-models-9b-parameters-that-beat-120b/ - SNIPPET: Qwen3.5 small release 2 Mar 2026; Qwen3.6 27B/35B only
96. https://www.ibm.com/new/announcements/ibm-granite-4-0-hyper-efficient-high-performance-hybrid-models - SNIPPET: Granite 4.0 sizes
97. https://frankx.ai/blog/phi-analysis-2026 - SNIPPET (LOW): no Phi-5 as of June 2026
98. https://www.deeplearning.ai/the-batch/mistral-uses-cascade-distillation-on-mistral-3-to-build-ministral-family - SNIPPET: Ministral 3 origin
99. https://arxiv.org/html/2601.08584v1 - SNIPPET: Ministral 3 paper
100. https://cloudprice.net/models/openai-gpt-oss-20b - SNIPPET (aggregator): Groq price
101. https://cloudprice.net/models/gpt-oss-20b - SNIPPET (aggregator): Together price
102. https://www.getmaxim.ai/bifrost/llm-cost-calculator/provider/groq/model/gpt-oss-20b.md - SNIPPET (aggregator)
103. https://computeprices.com/providers/openrouter/models/gpt-oss-20b - SNIPPET (aggregator): OpenRouter price
104. https://www.aipricing.guru/mistral-pricing/ - SNIPPET (aggregator): Mistral Small 4 price
105. https://developer.puter.com/tutorials/mistral-api-pricing/ - SNIPPET (aggregator, Oct 2026)
106. https://www.developersdigest.tech/models/ministral-3-14b - SNIPPET (aggregator): Ministral 3 14B price
107. https://privacy.claude.com/en/articles/8956058-i-have-a-zero-data-retention-agreement-with-anthropic-what-products-does-it-apply-to - SNIPPET: ZDR scope
