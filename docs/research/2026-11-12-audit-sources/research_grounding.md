# Making a local 9B model reliable for numeric research text, news tone and formula proposals

Date: 2026-11-12. Research only; no code changed. Citations are [n] and match the source list at the end.
Depth key: FULL = full text read, REPORT = alphaXiv structured report of the paper, ABS = abstract page read, CARD = model card or docs page read, REC = only a search-result record or snippet (nothing opened).
Arithmetic like "X of Y" is mine; every reported number below was read in the cited source unless marked UNVERIFIED.

## Short answer

The evidence does not support asking a 9B model to write sentences that contain numbers and judgements. It supports splitting the job:
1. Code writes every sentence that carries a number, a unit, a period label or an evaluative adjective, from the fact table and labels computed in code.
2. The model only writes connective prose, in a closed set of slots, and is checked afterwards.
3. Output is constrained by schema (enum labels, bounded lengths, token cap) and validated.
4. Anything that fails a check is redacted or regenerated once, then replaced by the deterministic text.
5. Evaluation runs on a small golden set with injected errors, plus nightly rates.

## 1. Data-to-text for finance and numeric domains

Findings
- The classic data-to-text literature found neural end-to-end writers fluent but unfaithful. The RotoWire study (REC) introduced the data-to-document task and proposed metrics for content selection, ordering and relation accuracy, showing that neural text copied wrong records [15]. Planning-first systems (content selection and planning [10], macro planning [11], plan-then-generate [12], step-by-step planning vs realisation [16]) separate "what to say and in what order" from wording; I only saw abstracts or records, so I cannot give their error-rate numbers. UNVERIFIED numbers.
- A study of ToTTo found hallucination in table-to-text output can be traced partly to input problems (ToTTo input errors) [13], so cleaning the fact table matters as much as the writer. ABS/REC only.
- Open LLMs on data-to-text were analysed for behaviour and errors [14] (REC only; no numbers read).
- Finance-specific grounding papers (read in full or report):
  - FinGround [1] (REPORT, self-reported, from the authors' own FinHalu data): computational claims had the highest hallucination rate, 28.4%; hedged words ("approximately", "roughly") caused 52% of the detector's false positives; numeric claims had the lowest detection F1 (88.1%); the distilled 8B detector reached 91.4% F1 against 95.0% for GPT-4o; end-to-end hallucination rate 4.1% (78% relative reduction vs GPT-4o+CoT); recall dropped to 71.4% for wrong values within +-5% of the truth; computational claims were re-computed with a +-0.5% tolerance for rounding; a full regeneration was triggered at 3 or more repairs, and a "flag-only" mode removes rather than repairs claims.
  - VeNRA (arXiv 2603.04663, FULL) [2]: replaces free-form number generation by a typed fact ledger plus deterministic Python execution. Reported hallucination rate fell from 4.1% to 1.2% and exact match rose from 39.2% to 49.1% on a mix of TAT-QA/FinQA/FinanceBench questions. A counter-intuitive result: richer context (1,236 vs 680 prompt tokens) hurt the CoT generator (exact match 39.2% to 37.4%), i.e. more numbers in the prompt can make a model worse. Authors are a small vendor lab (FactAI); single-author preprint.
  - RLFKV (arXiv 2602.05723, FULL) [3]: Ant Group, trains Qwen3-8B/Llama-3.1-8B with a reward from atomic (entity, metric, value, timestamp) units. Faithfulness only improved 3.0 to 3.6 points on FDD; Qwen3-8B baseline faith 86.5 and 90.2 on the two sets. Residual errors on FDD-ANT: time omissions 55%, time inaccuracies 28% (relative dates, fiscal vs calendar year), numeric errors 17% (rounding). This is the closest published match to the "half-year called latest quarter" failure: period labels are the main residual error of an 8B model even after RL.
  - 2610.08675 (REC) [7] and NumTrace 2610.34887 (REC) [5] describe the "right number, wrong role" failure (same value repeated across periods or metrics; matching entity, concept, period, unit, scope). 2611.02258 [6] (REC) analyses hallucination types in financial QA; 2609.04806 [8] (REC) studies numeric hallucination in domain-adapted models; VeriFin [4] and BudgetVerify [9] (REC) are neurosymbolic / tiered verification. I did not read these beyond their search summaries.
- Published "numeric guardrails": an autonomous-research paper (SAGE, REC) [79] checks each table number against a registry of measured values with 1% relative tolerance and replaces unverified values with a redaction marker rather than regenerating. A practitioner source argues that source-matching misses derived numbers (a percentage that disagrees with its inputs) and says to recompute relations separately [80] (low authority, snippet only).

Implication
- The failures observed (an "exceptional" FCF yield, "latest quarter" for a half-year, sentiment from missing news) are not number-matching errors. A number-in-table check would pass all three. They are role, period and judgement errors, which the sources above list as the residual errors even for strong systems [3][5][7].

## 2. Claim-level verification and small checkers

What each method measured
- FActScore [36] (ABS): atomic-fact precision against a source; automated estimate error under 2%; ChatGPT scored 58% on biographies.
- SAFE [37] (ABS): a search-augmented LLM agrees with crowd annotators on 72% of about 16,000 facts and is right in 76% of 100 sampled disagreements; over 20x cheaper than humans. Needs a strong LLM.
- Chain-of-Verification [38] (ABS): the model drafts, plans check questions, answers them without seeing the draft, revises; abstract gives no numbers. Relies on the same model's knowledge, so it cannot see that a figure is not the company's own.
- SelfCheckGPT [39] (ABS): consistency over sampled answers, black-box; needs several samples per call, too costly for 530 nightly calls on a 9B model and detects invention, not misread caveats.
- RARR [40] (ABS): retrieve evidence and edit unsupported claims minimally; the "research and revise" shape is what a redaction step does.
- AlignScore [41] (ABS): 355M parameters, trained on 4.7M examples from 7 tasks, evaluated on 22 datasets (19 unseen).
- MiniCheck [32][35]: 770M Flan-T5-large variant reaches GPT-4-level accuracy on LLM-AggreFact at "400x lower cost" (authors' claim; abstract gives no table). Bespoke-MiniCheck-7B [34] is 8B on the card, CC BY-NC 4.0 (commercial use needs a licence), 32K context, over 500 docs/min on an A6000 GPU: too big for CPU, and a second 7-8B model would not fit an 8 GB GPU beside the generator.
- HHEM-2.1-Open [33]: 0.1B (Flan-T5-base), Apache-2.0, under 600 MB RAM, about 1.5 s for a 2k-token input on a modern x86 CPU, English only. Balanced accuracy 76.55% AggreFact-SOTA, 64.42% RAGTruth-Summ, 74.28% RAGTruth-QA. RAGTruth-Summ at 64% is close to a coin flip with a bias.
- Ragas faithfulness [64] and DeepEval faithfulness [65] both extract claims and check them, but the judge is an LLM; Ragas lists HHEM as a drop-in verifier; DeepEval warns smaller custom judges follow instructions less reliably.
- No source I read tests NLI/entailment checkers on numeric claims. FinGround itself says generic NLI was insufficient for ratios and margins even with the right evidence [1], and that is why it added formula re-computation.

Which checker fits next to a 9B generator on CPU
- HHEM-2.1-Open [33] or MiniCheck-Flan-T5-large [35] are the only candidates that run on CPU with sub-second to few-second cost. They are for textual entailment (is "investors are worried" supported by these headlines?), not for numbers. Expect weak numeric judgement. UNVERIFIED for this app; must be tested on the golden set (section 6).
- For numbers use code: extract numbers and units from the output, match to the fact table by (value, unit, rounding) and by role label, and recompute derived values [1][2][79].

## 3. Constrained and structured decoding

llama.cpp facts (official docs, read)
- The grammar README [25]: JSON schema is converted to GBNF; it supports a subset; unsupported features are skipped silently; `properties` cannot be mixed with anyOf/oneOf; prefixItems is broken; nested $ref is broken; remote $ref unsupported in C++; min/max only for integer, not number; patterns must start with ^ and end with $; additionalProperties defaults to false; the README warns of "performance gotchas" (use x{0,N} instead of repeated optionals); the schema "is not injected into the prompt", so the prompt must describe the structure too.
- Server README [26]: `n_predict` default -1 (infinite) is the cause of runaway generation when no limit is sent; `stop` takes strings; `temperature` default 0.8; `repeat_penalty` 1.1 default per the README (another guide says 1.0 [75]; check your build); `presence_penalty` 0 (off); DRY off by default; `timeout` is a server flag, not a request field; reasoning flags `--reasoning`, `--reasoning-budget` exist for thinking models.
- Bugs seen in search records (REC, not opened): invalid grammar used to be dropped silently, later fixed to return HTTP 400 (PR 24144) [83]; a json_schema response_format with no `schema` field fell back to an unconstrained empty schema (fixed upstream per a search snippet) [82]; very large maxLength can produce a repetition count above the grammar engine cap, and empty-properties objects fail (issue 25923, open as of July 2026) [82]. Action: pin the llama.cpp build, add a startup test that a deliberately violating sample is rejected.
- llguidance [30][31]: faster alternative engine; merged in llama.cpp build b4613 (2025-02-01); build with `-DLLAMA_LLGUIDANCE=ON` (needs Rust); self-reported mask time about 50 microseconds average and about 1.5 ms for a full mask on a 128k tokenizer. Self-reported on its own benchmark; for a 4-field schema the gain is irrelevant.
- Other libraries: Outlines [56] (JSON schema, regex, choice, CFG; llama.cpp backend), Guidance [62] (select, gen with regex, JSON), LMQL [60] (constraints as token masks; llama.cpp needs local install; maintenance status not stated), Instructor [57] (validate with Pydantic, retry `max_retries`, llama-cpp-python supported), Pydantic [61] (generates the JSON schema; Literal/enum map to `enum`; constraints map to minLength/maximum), BAML [58] (DSL; docs page read was the legacy v0 page), DSPy [59] (signatures, optimisers; its own page claims 0.41 to 0.63 F1 and 62% to 89% accuracy in examples, vendor-style claims). None is required: the llama.cpp server plus Pydantic validation does the job; Instructor adds retries.

Effect on quality
- "Let Me Speak Freely?" [17] (ABS): format restrictions reduced reasoning performance. Rebuttal by the constrained-decoding vendor dottxt [27]: unequal prompts and weak parsing; with matched prompts, Llama-3-8B structured vs unstructured: GSM8K 0.78 vs 0.77, Last Letter 0.77 vs 0.73, Shuffle Object 0.44 vs 0.41; the paper's own Last Letter parser scores 0.57 vs 0.61 for a flexible regex. Independent replication (blog) [29]: Llama-3-8B equal; GPT-4o-mini much worse structured on Shuffled Objects (about 62-70% vs 93-95%). The BAML vendor says constrained output hides errors, with one anecdote [28]. Both vendor posts have a commercial interest. Contested.
- JSONSchemaBench [18]: 10K real schemas, six frameworks (Guidance, Outlines, llama.cpp, XGrammar, OpenAI, Gemini); abstract only, coverage numbers not read.
- Small models: constrained decoding lifted schema validity from 78.6-92.9% to 100% on 0.6B-4B models but semantic accuracy still lagged [21] (ABS). Constrained decoding is not harmful when prompts are equivalent; harm came from non-equivalent prompts [19] (ABS, no numbers). Putting reasoning fields before the answer field matches free-form; answer-first drops sharply, especially for small models [20] (ABS). Records only: [22][23][24].
- Practical reading: for a classification label, the risk of constraining is low and the gain (no parse failure, no runaway) is high; for long reasoning, constrain only the final fields.

Sampler guidance
- A repetition penalty acts on digits and JSON punctuation as well; one blog argues it distorts structured output [75] (low authority, mechanism plausible, no benchmark). Another vendor doc uses 1.05 with grammar-constrained JSON [76] (REC). Recommendation: set repeat_penalty 1.0 and presence 0 for extraction calls; use temperature 0 to 0.2; add DRY only if loops appear. UNVERIFIED by any measurement; test on the golden set.
- A fine-tuned 2B finance sentiment model served locally with a 96-token cap and temperature 0 reports 100% valid JSON [77] (vendor card, low authority, shows that a short cap is normal).

## 4. Evaluative words, anchoring and prompt design

- No source I read measures "exceptional/cheap" adjectives directly. What exists: position and distraction effects. "Lost in the middle" [48]: accuracy is highest when relevant information is at the beginning or end of the context. Irrelevant context drops accuracy sharply (GSM-IC) and an "ignore irrelevant information" instruction helps partly [52] (ABS). Negated prompts get worse with scale (inverse scaling, tested up to 175B) [73] (REC); "do not use word X" can prime the word, 87.5% of violations in one 2026 study [74] (REC). So "do not call this exceptional" or "this figure is not the company's own" are weak guards; removing the figure or attaching a label in code is safer. This is an inference from [73][74][52], not a measurement on the Quantfolio prompt.
- VeNRA's context-size result [2] supports short, single-purpose prompts for small models.
- Design rule: a number never reaches the model without its qualifier in the same cell, e.g. "FCF yield (sector median proxy, NOT company's own): 23%". Better: do not show the proxy figure to the model at all, and let code print "Free cash flow yield: not available for this company" in the section.
- Labels: compute percentile or threshold labels in code (for example value-ranking vs the same universe) and pass the label as the only evaluative term; the writer may only use words from an allowed list per metric. This is a design recommendation; no paper tested it.
- Few-shot with counter-examples, system vs user placement: I found no solid small-model evidence in this session. UNVERIFIED.

## 5. Abstention and missing data

- The abstention survey [49] (ABS) organises the field by query, model, human values; no numbers. A study of hallucinated references finds the model often cannot hold consistent facts about things it invents [50] (ABS), which is why self-consistency is a weak abstention signal.
- Constrained schemas remove the model's ability to decline unless you add an explicit "unknown" value [28] (vendor, anecdotal). So the schema must include an enum value like "no_news".
- For missing news, the code should not call the model: if the headline set is empty, store "no_news" and skip the call (also saves about 4% timeouts' worth of load only if those were empty cases; UNVERIFIED which). For report sections, drop the section in code when its inputs are missing. FinGround's "flag-only" mode (remove rather than repair) [1] and SAGE's redaction marker [79] are published precedents.
- The RLFKV time omissions at 55% of residual errors [3] show that models drop temporal qualifiers, so ask for them in a structured field filled from code, not by the model.

## 6. Evaluation

- LLM-as-judge: strong judges reach over 80% agreement with humans, with position, verbosity and self-enhancement bias [54]; judges favour related models (preference leakage) [55]; DeepEval warns small judges follow instructions worse [65]. A 9B local judge grading a 9B local writer is the worst case for both. Use code checks first; use humans for the golden set; use the LLM judge only for style.
- Tools: promptfoo [63] (runs locally, caching, CI; its page did not list assertion types), Inspect [66] (tasks, scorers incl. model-graded, local providers hf/vLLM/SGLang; llama.cpp not mentioned), Ragas [64], DeepEval [65]; promptfoo sandboxed code-eval guide [85] (REC). Any of these works; the least dependency is a pytest file with golden fixtures.
- Golden set (design, not from a paper): 30-50 saved fact tables, each with the real report and 3-5 variants with one injected error (wrong period label, proxy figure presented as own, adjective without label, claim from missing news, number off by 10%, unit swap). Pass criteria are code checks. The injected-error method mirrors the sabotage training idea in VeNRA [2] and the same-number swaps in [7].
- Nightly metrics: unsupported-number rate; unlabelled-adjective rate; period-label mismatch rate; schema-failure rate; retry rate; timeout rate; fraction of reports falling back to deterministic text; tone label distribution drift. Thresholds are mine.
- Human spot check: I found no source giving a sample size for this. A sample of n gives roughly +-1/sqrt(n) at worst case; 100 reports bound an error rate to about +-10 points, so use it to catch rate shifts, not to estimate small rates. This is arithmetic, not a citation.

## 7. News retrieval

- Embeddings (model cards read): BGE-M3 [45] MIT, 8192 tokens, 1024 dims, "more than 100 languages", parameter count not on the page I got; multilingual-e5-small: page failed to load, size/licence UNVERIFIED (I recall about 118M but did not confirm); gte-multilingual-base [47] about 305M, Apache-2.0, 8192 tokens, over 70 languages, needs `trust_remote_code`; nomic-embed-text-v2-moe [46] 475M total / 305M active, Apache-2.0, about 100 languages, 512-token max; jina-embeddings-v3 [81] about 570M (sources say 559-572M) and CC BY-NC 4.0, so commercial use needs a licence (record only). For headlines (short), any of these runs on CPU; BGE-M3 or gte-multilingual-base are licence-clean.
- Dedup: a 2025 NAACL industry paper on near-duplicate detection with only headline and blurb combines PLM embeddings with metadata and community detection [86] (REC); a vendor describes embeddings plus Levenshtein refinement with a 0.95 cosine threshold [87] (low authority, vendor).
- Ticker linking and event extraction (buybacks, guidance): I found no usable source in this session (GLiNER search returned nothing). UNVERIFIED. Rule-based first (exact company aliases and ISIN/ticker maps, keyword patterns for "buyback", "guidance") is the safe default.
- Long-context survey [53] (ABS) is only background; it gave nothing specific.

## 8. LLM factor mining

- What survives honest evaluation (2608.27734) [44] (ABS): a leaky oracle with Sharpe 35 passed both the Deflated Sharpe and PBO tests; honest evaluation rejected every LLM-discovered strategy across 453 US stocks and 39 ETFs, with search budgets up to 100 candidates and five runs; every trial is logged and results deflated by trial count.
- Auditing LLM alpha mining (2610.29500) [43] (ABS): 67/20/11 "keep" decisions in three runs are not comparable because metric populations differ; a released catalog of 1,837 expressions excluded non-positive out-of-sample outcomes; in a re-analysis of 1,047 expressions, of 923 scorable 671 had positive and 252 negative mean rank-IC, and simple untrained reference models also showed positive associations.
- Propose, don't judge (2609.27051) [42] (ABS): a frozen statistical referee (anytime-valid, betting based) admits 5 to 11 times fewer sub-threshold factors than leaky referees; an admitted true factor waits about 500 trading days; the LLM matches a bandit in yield. Matches the app's design (model proposes, procedure judges). Author affiliations and review status unknown.
- FactorBench [70] (ABS): about 5,000 factors from nine methods across five markets; no paradigm consistently wins and mining gains do not reliably carry to portfolios.
- Framework papers with self-reported gains: AlphaAgent [67] (AST originality, complexity control, hypothesis alignment; abstract has no numbers), QuantaAlpha [71] (IC 0.0472, ARR 4.68%, MDD 11.8% with GPT-5.2 on CSI 300; self-reported), AlphaForge [72] (REC), AlphaDiverse [69] (local post-trained agents; no sizes in abstract). Alpha-GPT not located in this session: UNVERIFIED.
- Safe execution: QBX [68] (ABS) compiles proposals against a versioned block registry with types and an availability-effect check; valid proposals rose from 26.8% (free strings) and 36.4% (grammar only) to 97.7% (typed programs), zero registered-contract look-ahead violations in 23,342 candidates. Library notes (REC, tutorials): simpleeval blocks underscore attributes and some builtins; none of these stops CPU or stack exhaustion; recommended is a custom AST allow-list plus length, depth and time limits, or process isolation [84]. A GBNF grammar for the DSL gives validity but not safety or no-look-ahead; validation in code is still required.
- Small models: the literature uses frontier models for the headline results; the evidence that a 9B model finds real factors is thin. FactorBench and the honest-evaluation papers suggest expecting no gain. Treat the 9B as a hypothesis generator and count every proposed formula as a trial.

## 9. Verbalised confidence

- Xiong et al. [51]: verbalised confidence is overconfident; larger models calibrate better; consistency across samples helps; AUROC of black-box methods 0.522 to 0.605 (near chance) on five models including GPT-4 and LLaMA 2 Chat. Tested models were larger than 9B or older; for a 9B this is likely worse. UNVERIFIED for the app's model.
- Structure Tax [20] says reasoning-first schemas improve calibration (ABS).
- Display instead: coverage of the report (how many inputs were available), count of checks passed, label from code-computed evidence (such as agreement across headlines), and the model's token-probability of the label if the server returns it (`n_probs` exists [26]); never a self-reported percent.

## Recommended design

### Report writer
1. Build a fact table in code: each cell = value, unit, period label, source, own/proxy flag, rounding rule, and a code-computed evaluative label (percentile vs peers, threshold bucket). Drop cells with proxy flags from the model-visible table.
2. Render every numeric sentence from templates in code (per section, per metric). The model is not allowed to type a digit.
3. The model fills only closed slots: one connective sentence per section, schema `{"section": enum, "text": string maxLength 300}`; the prompt contains that section's facts only (one section per call, short).
4. Evaluative words only from the label supplied (allow-list per metric); any adjective from a banned list ("exceptional", "strong", "cheap", "worried", "investors") without a label causes failure.
5. Post-checks in code: (a) every digit sequence in the model slots must be in the fact table (after unit and rounding rules; tolerance 0.5% for rounded values as in [1], 1% as in [79]); stricter is better because the model should print none; (b) period words must match the cell's period label (half-year vs quarter); (c) sentiment words require non-empty news; (d) derived values recomputed [1][80].
6. On failure: regenerate that section once with the violation named; second failure: use the deterministic template alone. Remove sections whose inputs are missing [1][79].
7. Optionally run HHEM-2.1-Open or MiniCheck-Flan-T5-large on CPU for textual claims against the supplied headlines only [33][35]; log, do not block, until measured on the golden set.
8. Sampler: temperature 0 to 0.3, repeat_penalty 1.0, presence 0, `n_predict` 200 per slot, stop strings at the end-of-JSON; wall time cap below the 120 s limit.

### News tone call (530 per night)
1. Skip the call when there are no headlines: store `no_news` (code decision).
2. Pre-dedupe headlines (embedding cosine plus string match [86][87]) and cap at the top 10 by recency/relevance.
3. Schema via `response_format` json_schema [25], one object: `{"label": enum["positive","neutral","negative","mixed"], "evidence": array of integers (headline indices, max 3), "reason": string maxLength 120}`. Label first, no confidence field. Reasoning-first helps accuracy for hard cases [20], but a 3-way tone label on headlines is easy; if the golden set shows errors, add a 40-word `reason` before `label`.
4. `n_predict` 120 tokens (a JSON of this size needs far fewer; the 96-token example [77] is of the same order), temperature 0, repeat_penalty 1.0, per-request client timeout 30 s, one retry, then label `unknown`. Describe the structure in the prompt as well, since the schema is not injected [25].
5. Validate with Pydantic [61]; evidence indices must exist; a "positive" or "negative" label with empty evidence becomes `unknown`.
6. Confidence shown to the user = share of headlines whose evidence supports the label, plus count of distinct sources; no self-stated numbers [51].
7. Check the llama.cpp build for the json_schema bugs listed above [82][83]; add a startup self-test.
8. Metrics: schema-failure, timeout, unknown, no_news rates; label drift.

### Formula proposals
Keep the DSL grammar (GBNF) for syntax, a typed registry validator for operators/fields/windows and look-ahead [68], a length/depth/time limit, count every proposal in the trial ledger [44], and judge only with the frozen statistical procedure [42].

## Contested points
- Whether constrained decoding hurts quality: [17] vs [27] vs [29] vs [28]; dependent on model and prompt equivalence; small-model evidence [21] says validity gain, semantic gap remains.
- Whether LLM-based verification works for numbers: FinGround [1] and VeNRA [2] report big gains, but both are self-reported on their own data, and the first used GPT-4o as teacher; small CPU checkers have 64-77% balanced accuracy on RAG benchmarks [33].
- Repetition penalty effect on digits: only a blog [75]; llama.cpp default stated differently by two sources.
- Reported framework gains in alpha mining [71][67] vs audit results [43][44].

## Claims I could not verify
- That the app's loose-tolerance grounding check is too loose: I did not read that module (research task only); my numbers (0.5%/1%) come from [1][79].
- multilingual-e5 size/licence; nomic and gte quality on German/French/Japanese headlines; jina-v3 licence beyond a search record.
- GLiNER or any finance event-extraction model for buybacks/guidance; entity-linking methods.
- Alpha-GPT paper; AlphaForge details.
- Any benchmark of timeouts/truncation for small local models in news tone.
- JSONSchemaBench coverage numbers; "Let Me Speak Freely" effect sizes; ToTTo/RotoWire error rates; MiniCheck exact scores.
- Few-shot with counter-examples and system-vs-user placement for small models.
- Human spot-check sample sizes from literature.
- llama.cpp bug list is from search snippets (issue numbers 25923, 25895, PR 24144) and was not opened; confirm on GitHub.
- Search results were US-only and may be dated; sources with 2026 IDs were taken as published, I could not independently check peer review.

## Sources (87 distinct)

Papers and benchmarks
1. FinGround, arXiv 2604.23588, atomic claim verification in finance. REPORT (alphaXiv AI report; not raw PDF).
2. VeNRA, "Neuro-Symbolic Financial Reasoning via Deterministic Fact Ledgers", arXiv 2603.04663. FULL.
3. RLFKV, "Mitigating Hallucination in Financial RAG via Fine-grained Knowledge Verification", arXiv 2602.05723. FULL.
4. VeriFin, arXiv 2608.10213. REC.
5. NumTrace, arXiv 2610.34887. REC.
6. "Beyond Binary Detection: Type-Aware Hallucination Analysis...", arXiv 2611.02258. REC.
7. "Same-Number Citation Swaps", arXiv 2610.08675. REC.
8. "When Financial Fine-tuning Fails", arXiv 2609.04806. REC.
9. BudgetVerify, arXiv 2609.33052. REC.
10. "Data-to-Text Generation with Content Selection and Planning", arXiv 1809.00582. REC.
11. "Data-to-text Generation with Macro Planning", arXiv 2102.02723. REC.
12. "Plan-then-Generate", arXiv 2108.13740. REC.
13. "Improving Factual Accuracy of Neural Table-to-Text Output ... ToTTo", arXiv 2404.04103. REC.
14. "Beyond Traditional Benchmarks: Behaviors of Open LLMs on Data-to-Text", arXiv 2401.10186. REC.
15. "Challenges in Data-to-Document Generation" (RotoWire), arXiv 1707.08052. REC.
16. "Step-by-Step: Separating Planning from Realization", arXiv 1904.03396. REC.
17. "Let Me Speak Freely?", arXiv 2408.02442. ABS.
18. JSONSchemaBench, arXiv 2501.10868. ABS.
19. "Constrained Decoding: Harmful or Beneficial?", arXiv 2610.35328. ABS.
20. "Structure Tax", arXiv 2610.12056. ABS.
21. "Constrained Decoding Eliminates Structural Failures in Small LLMs...", arXiv 2609.23742. ABS.
22. "The Format Tax", arXiv 2604.03616. REC.
23. "The Constraint Tax", arXiv 2605.26128. REC.
24. "Schema-Key Wording as an Instruction Channel", arXiv 2604.14862. REC.
36. FActScore, arXiv 2305.14251. ABS.
37. SAFE / LongFact, arXiv 2403.18802. ABS.
38. Chain-of-Verification, arXiv 2309.11495. ABS.
39. SelfCheckGPT, arXiv 2303.08896. ABS.
40. RARR, arXiv 2210.08726. ABS.
41. AlignScore, arXiv 2305.16739. ABS.
32. MiniCheck, arXiv 2404.10774. ABS.
42. "Propose, Don't Judge", arXiv 2609.27051. ABS.
43. "Auditing LLM Alpha-Factor Mining", arXiv 2610.29500. ABS.
44. "What survives honest evaluation?", arXiv 2608.27734. ABS.
48. "Lost in the Middle", arXiv 2307.03172. ABS.
49. Abstention survey, arXiv 2407.18418. ABS.
50. "Do Language Models Know When They're Hallucinating References?", arXiv 2305.18248. ABS.
51. Xiong et al., "Can LLMs Express Their Uncertainty?", arXiv 2306.13063. ABS.
52. "LLMs Can Be Easily Distracted by Irrelevant Context", arXiv 2302.00093. ABS.
53. Long-context survey, arXiv 2503.17407. ABS (background only, low relevance).
54. MT-Bench / LLM-as-a-judge, arXiv 2306.05685. ABS.
55. Preference leakage, arXiv 2502.01534. ABS.
67. AlphaAgent, arXiv 2502.16789. ABS.
68. QBX, arXiv 2610.41689. ABS.
69. AlphaDiverse, arXiv 2609.29014. ABS.
70. FactorBench, arXiv 2610.06947. ABS.
71. QuantaAlpha, arXiv 2602.07085. ABS.
72. AlphaForge, arXiv 2406.18394. REC.
73. "Can LLMs Truly Understand Prompts? ... Negated Prompts", arXiv 2209.12711. REC.
74. "Semantic Gravity Wells", arXiv 2601.08070. REC.
78. "Fine-tuning of lightweight LLMs for sentiment classification on financial text", arXiv 2512.00946. REC.
79. SAGE self-correcting research agent, arXiv 2606.31478 (numeric table check). REC.
86. NDD-MAC near-duplicate news detection, NAACL 2025 industry paper 73. REC.

Official docs, model cards, repositories
25. llama.cpp grammars README (github.com/ggml-org/llama.cpp). CARD.
26. llama.cpp server README. CARD (page truncated after /slots).
30. llguidance README (github.com/guidance-ai/llguidance). CARD.
31. llama.cpp docs/llguidance.md via search results. REC.
33. HHEM-2.1-Open model card (huggingface vectara). CARD.
34. Bespoke-MiniCheck-7B model card. CARD.
35. MiniCheck GitHub README. CARD.
45. BGE-M3 model card. CARD (partial).
46. nomic-embed-text-v2-moe card. CARD.
47. gte-multilingual-base card. CARD.
56. Outlines docs. CARD.
57. Instructor docs. CARD.
58. BAML docs (legacy v0 page). CARD.
59. DSPy site. CARD.
60. LMQL docs. CARD.
61. Pydantic JSON-schema docs. CARD.
62. Guidance repository. CARD.
63. promptfoo intro docs. CARD.
64. Ragas faithfulness docs. CARD.
65. DeepEval faithfulness docs. CARD.
66. Inspect (UK AISI). CARD.
81. jina-embeddings-v3 model page and licence terms. REC.
82. llama.cpp issues 25923 / 25895 and related json_schema fixes. REC.
83. llama.cpp PR 24144 (grammar parse failure returns 400, build b9704) and related PRs 18851, 19612. REC.
84. simpleeval README and guides on eval sandboxing. REC (tutorials, low authority).
85. promptfoo "sandboxed code evals" guide. REC.
76. Liquid AI docs, llama.cpp structured output example. REC.

Blogs and vendor posts (low authority)
27. dottxt, "Say What You Mean" (vendor of Outlines; commercial interest). Opened.
28. Boundary (BAML vendor), "Structured outputs create false confidence". Opened; vendor, anecdotal.
29. Dylan Castillo, replication blog. Opened; individual blog.
75. DEV Community post on repetition penalty and JSON. REC; low authority.
77. Friendli model card, fine-tuned 2B financial sentiment. REC; vendor, low authority.
80. digitalapplied.com "derived numbers" article and other practitioner posts. REC; low authority.
87. Newscatcher API deduplication docs. REC; vendor.

Total: 87 distinct sources (numbers 1 to 87; 43 opened beyond a search record at abstract, model-card, report or full-text level, 3 at full text or near-full text, the remainder search records).
