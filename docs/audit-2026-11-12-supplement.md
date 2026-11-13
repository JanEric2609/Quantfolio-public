# Audit supplement, 2026-11-12: research on fixes, the LLM question, and the rendered-UI review

Supplements `docs/audit-2026-11-12.md`. Nothing in the code was changed. Two things were done on prod at the owner's request: the news retag (section 8) and a read-only look at the UI through a two-hour session for the owner's account (section 7).

## 0. What the research is, and how far to trust it

Five research sweeps ran on separate themes. Their full write-ups, with numbered source lists, are in `docs/research/2026-11-12-audit-sources/`.

| File | Theme | Sources listed | Pages actually opened |
|---|---|---|---|
| `research_llm.md` | Which model, which settings, cloud, hardware | 107 | 67 |
| `research_data.md` | Fundamentals, news, bars, tickers, F-score | 130 | 4 |
| `research_stats.md` | Promotion test, ladder, IC gate, confidence | 86 | 1 in full, most as abstracts or records |
| `research_grounding.md` | Making LLM output reliable | 87 | about half at abstract or doc level, 3 in full |
| `research_eng.md` | CI, tests, UX, Learn tab, compute | 106 | 0 (search snippets only) |

That is 516 listed sources, on top of 122 web sources and about 46 papers in the first round. **Most were read as search snippets or abstracts, not full texts.** Treat a figure below as "reported by the source as summarised", and open the source before relying on it. Where I checked something myself against prod or the code, it says so.

Things the sweeps could not find are listed in section 9; they matter as much as what was found.

---

## 1. Should Quantfolio switch to a different LLM?

**No, not now.** Three independent lines point the same way.

1. **Your own benchmark** (`docs/research/llm-benchmark-2026-10-15.md`): Qwen3.5-9B and Qwen3.6-35B-A3B both got 98.8 % of extracted filing numbers right; the 35B was 4.5 times slower. Temperature 0.7, 0.3 and 0.0 gave identical extraction answers.
2. **The failures are not capability failures.** Calling a 23 % FCF yield "exceptional", reading H1 as a quarter, identical confidences: each comes from what the model is shown and asked, and each has been reproduced with far larger models in the literature (section 4).
3. **Nothing clearly better fits 8 GB.** The sweep found no newer 9B-class Qwen (3.6 exists only at 27B and 35B). No published grounding, FinQA or JSON-schema score exists for Qwen3.5-9B itself, so no candidate can be shown to beat it on paper.

Two cautions from the sweep, both as reported:

- On Vectara's grounded-summary hallucination board the larger Qwen3.5 models score worse (10.5-12.1 %) than Qwen3-8B (4.8 %), Gemma-3-12B (4.4 %) and Phi-4 (3.7 %). The 9B is not listed. This is a reason to test alternatives for the report writer, not a reason to switch blind.
- On FAITH (masked numbers in 10-K tables) 8B-class models score 31-48 % against 96 % for a frontier model. Small models are weak at numeric table reasoning in general, which is the argument for taking numbers out of the model's hands.

### What to do instead, per workload

| Workload | Recommendation |
|---|---|
| News tone | Lower the token cap for this call from 4,096 to about 200, pass a JSON schema with an enum for tone, log the partial output on timeout. Consider FinBERT (already installed) as the primary score: one study has a 3B LLM only tying FinBERT-Tone (92.1 % vs 91.7 %, snippet only), and reasoning does not help financial sentiment |
| Stock report | Keep the model, change the design (section 4). Optionally A/B the same model at UD-Q5_K_XL (6.74 GB) with context cut to 16k |
| Verdicts | Remove self-stated confidence. One study: a 4B model stated over 95 % confidence at near-chance discrimination (AUROC 0.55) |
| Factor ideas | Local, unchanged; the gate does the judging |
| Advisor reasoning | Local (private data) |
| Public-data jobs | A cloud model is cheap if ever wanted: the sweep's estimate for the whole nightly load is about 1-5 USD a month across the small tiers. Anthropic has no EU inference region. Not needed today |

### Before any switch: a 30-50 prompt A/B

Freeze 30-50 real inputs (reports and tone calls), score with code checks (unsupported numbers, period mismatches, schema failures), and compare: current model, same model at Q5, Gemma 3 12B or Phi-4, Gemma 4 E4B. Run it at night on the idle GPU. Count it in the trial ledger as a tooling decision, not a strategy trial.

### What I checked myself

- Server flags on LXC 112: temperature 0.7, top-p 0.8, top-k 20, presence penalty 1.5, `--parallel 1`, context 32,768, thinking off, 6.9 of 8.2 GB VRAM in use.
- The client sets `max_tokens = 4096` on every call (`llm/local_llama.py:21`). At the 28 tokens/s your benchmark measured, 4,096 tokens take about 146 s, longer than the tone call's 120 s timeout. So the cap never bites before the timeout.
- All 528 stored tone responses from last night are short, clean JSON (average 589 characters, none with thinking output). The 20 timed-out calls left no record, so runaway generation is still the likely cause, not a proven one.
- The sweep's claim that presence penalty corrupts digits rests on one blog; the Qwen model card itself recommends 1.5 with thinking off and warns against greedy decoding. Your benchmark saw no effect of temperature on extraction. So: do not change the sampler on belief; test it in the A/B.

### Hardware

A 16 GB card is the only route to a clearly better local model class (RTX 5060 Ti 16 GB about 617 EUR, lowest listing on 11-12, as reported). Your workloads do not need it yet.

---

## 2. Fixing the data defects

### 2.1 Free-cash-flow yield

- Stop using Yahoo's `info["freeCashflow"]`. Build it from the cash-flow statement: operating cash flow − capital expenditure − lease principal repaid.
- Convert market cap into the reporting currency (`financialCurrency`) before dividing; divide GBp quotes by 100.
- For groups with large minorities (Deutsche Telekom), either use an enterprise-value yield with minorities included in EV, or scale cash flow by the ownership share.
- Exclude banks and insurers.
- When any of these cannot be done, show "n/a". Do not hand the model a proxy with a caveat (section 4 explains why caveats fail).

Not found: Yahoo's own definition of `freeCashflow`. The fix avoids needing it.

### 2.2 Net debt / EBITDA

Show one clearly labelled figure: "including leases" for IFRS reporters. Rating agencies include leases in debt, so 3.1× for Deutsche Telekom is a legitimate number, only an unlabelled one. The 3.0 threshold then needs to be sector-aware (telecoms and utilities carry more); the sweep found no sourced sector thresholds, so any table would be a rule of thumb and should say so.

### 2.3 Half-year reporters

- Infer the period length from the gap between statement dates (about 90, 180 or 365 days) and store `period_months` and `period_end` with every growth figure.
- Compute trailing twelve months as full year + latest H1 − prior-year H1, and compare like with like.
- Label from the data: "half-year to 30 June 2026", never the fixed word "quarter".

### 2.4 Company news outside the US

The hard finding: there is **no free machine-readable feed** for the regulatory news that matters.

| Source | Status as reported |
|---|---|
| London RNS | Licence about 6,500 GBP a year |
| Oslo NewsWeb | Paid feed |
| EQS / DGAP (Germany) | No public API or RSS found |
| TDnet (Japan) | 31 days free on the site; API about 70,000 JPY a month |
| GDELT | Free; 250 records per query; English-heavy |
| Marketaux | Free tier exists |
| EODHD | News included in its fundamentals plan |

Practical route, cheapest first:

1. Make the report read what is already stored: the GDELT mentions and the Finnhub company news that the tone job fetches. Today it reads only `news_items`.
2. Fetch on demand when a report is requested for a stock that is not held.
3. Say "no news source covers this company" when nothing is found, instead of "no recent news".
4. Add company investor-relations RSS feeds for the names you hold or shortlist. That is a few dozen feeds, not 1,400.
5. Map a release to a ticker by the ISIN in its text, OpenFIGI, and the GLEIF ISIN-to-LEI file.

### 2.5 Daily bars

- Before storing, drop any bar dated after the exchange's last completed session. `exchange_calendars` gives each exchange's session close; add a settle buffer.
- Re-pull the previous session's bar after its close.
- 52-week high and low from unadjusted highs and lows (`auto_adjust=False`), split-adjusted only. Keep adjusted closes for returns and drawdowns, and label drawdown "including dividends".
- Verify on a known split whether Yahoo's unadjusted close is already split-adjusted; the sweep could not confirm it.

I confirmed the partial-bar problem on prod (today's Tokyo bars at about a third of normal volume).

### 2.6 Unknown tickers

- Validate at the door: quote type present, recent history non-empty, currency as expected. Otherwise answer "not found" and store nothing.
- Normalise the `.US` suffix (used by EODHD, Stooq and Trading 212) to Yahoo's bare symbol.
- For Alpaca, key on the asset `id`, not the symbol; symbols are reused. That is the likely story for TEVA and PARA (not confirmed with Alpaca).

### 2.7 The strength score

Either implement the real nine Piotroski signals or rename the score.

- The real signals: ROA > 0, operating cash flow > 0, ROA rising, cash flow above net income, leverage falling, current ratio rising, no new shares, gross margin rising, asset turnover rising. They need two consecutive annual statements; Yahoo's annual statements usually cover four years (not verified for European names).
- Evidence as reported: positive in European and other non-US markets after 2000; one unrefereed claim of decay in the US.
- Cheapest alternative with good evidence: Novy-Marx gross profitability (gross profit over assets), one number.
- A missing input must read "not available", not "fail".

### 2.8 Paid data: test before buying

| Option | Price as reported | What it would fix |
|---|---|---|
| EODHD fundamentals | about 60 EUR a month | European fundamentals and news; end-of-day prices are a separate plan |
| Twelve Data Grow | 29-79 USD a month, personal use | Fundamentals, a second price source |
| filings.xbrl.org, SEC companyfacts, EDINET, Stooq, OpenFIGI, GLEIF | Free | Cross-checks; ESEF is annual only and Germany and Ireland are missing |

No independent accuracy study of any vendor's European fundamentals was found. So: hand-key ten European issuers from their own reports (include one half-year reporter and one with large minorities), pay for one month, score the vendor against that set, then decide.

### 2.9 Validation at ingestion

A schema per provider payload with range checks (pandera fits a pandas codebase), currency assertions, and a quarantine table for outliers, as already exists for bars. The 10 rows with FCF yield above 30 % and the 7 with dividend yield above 12 % would all have been caught by a range check.

---

## 3. Fixing the statistics

### 3.1 The promotion test

The sweep's reading confirms the audit's simulation and goes further: with a 21-day horizon, 40 dates contain about 2 independent blocks and 170 contain about 8. No variance correction turns that into strong evidence.

Options, in order of preference:

1. **Replace it with an anytime-valid test.** Split forecast dates into 21 residue classes so forecasts within a class do not overlap, run a betting e-process per class, average them. Sources: Henzi-Ziegel 2022; Choe-Ramdas; the 2026 "Promote" paper. The averaging step is the sweep's own reasoning, not a published result, so it should be simulated first.
2. **Short term:** fixed-smoothing inference. Equal-weighted cosine variance with ν = ⌊0.4·T^(2/3)⌋ and a t(ν) reference (Lazarus-Lewis-Stock-Watson 2018; Coroneo-Iacone 2020). No library function for this was found; it is about 20 lines.
3. Drop the Pocock boundary either way: its looks are not independent increments here.

Whatever is chosen, calibrate it by simulation under the null before use. This is the first job for the idle CPU.

### 3.2 The capital ladder

Your design is close to the published "Promote" design. Three changes:

1. Bet on return minus β̂·benchmark, with β̂ estimated from past data only, not on raw excess return.
2. State that the tested quantity is the clipped score, and keep the unclipped P&L beside it. For heavy tails, sign-based or Catoni-style scores are the published alternatives.
3. Give the skill and harm processes half the error budget each.

**How long promotion takes.** By the sweep's approximation (not a simulation of your setup), years to reach evidence 10 are about 4.6 / IR², where IR is the yearly information ratio of the excess return:

| Information ratio | Years to evidence 10 | Years to evidence 40 |
|---|---|---|
| 1.0 | 4.6 | 7.4 |
| 0.75 | 8.2 | 13 |
| 0.5 | 18 | 30 |
| 0.25 | 74 | 118 |

An information ratio of 0.5 is what good professional managers achieve. So a strategy with real, ordinary skill would not be promoted within a decade. That is a property of honest sequential testing on daily returns, and it deserves a decision from you: accept it, or define "tuition" money as a learning expense that is not waiting for proof.

### 3.3 The IC gate

- t ≥ 3 (Harvey-Liu-Zhu) is a hurdle for factors found by searching. A single hypothesis written down in advance needs the ordinary 1.96-2.58.
- With about 42 independent periods, the sweep estimates the gate's power at about 9 % for IC 0.025 and 47 % for IC 0.045 (assuming a per-date IC standard deviation of 0.10, which is an assumption). About 13 years of data would give 80 % power at IC 0.03.
- Better than a hard gate: an empirical-Bayes shrunk IC with its interval (Jensen-Kelly-Pedersen; Chen-Zimmermann).

This is contested in the literature: whether t > 3 overstates false discoveries is an open argument.

### 3.4 Confidence shown to you

Do not show a model's stated confidence. Show the measured hit rate of past verdicts with its interval and the number of cases, and nothing until there are enough cases. Recalibrate (isotonic or Platt) only once data allows. ESMA's rule that past performance is not a forecast applies to the wording.

### 3.5 Smaller items

Drawdown: count the starting value as the first peak. Quantiles: state the Hyndman-Fan type. Hit rates on overlapping picks: divide the count by the overlap or cluster by date. The 125 open overlapping picks found on prod are this case.

---

## 4. Making the LLM output reliable

The strongest single piece of evidence (opened in full by the sweep): in one study, replacing free-form numbers with a typed fact ledger plus code execution cut hallucination from 4.1 % to 1.2 %. In the same study, giving the model *more* context made it slightly worse. A second full-text study found that even after fine-tuning an 8B model, 83 % of the remaining errors were omitted or wrong time periods. That is your "latest quarter" error exactly.

### Design for the stock report

1. Code writes every sentence that carries a number, a unit, a period or a judgement word, from a fact table.
2. The model writes only the connecting prose, one section per call, and may not type a digit.
3. Never show the model a figure it should not use. A caveat in the prompt is weak: negation and "ignore this" instructions are unreliable.
4. Judgement words ("high", "cheap") come from code: a percentile against sector peers or a fixed threshold, passed in as a label. (No paper tested this exact rule; it is a recommendation.)
5. After generation, in code: every number in the text must be in the fact table within 0.5-1 %; period words must match the fact's period label; sentiment words require non-empty news.
6. On failure: regenerate once, then fall back to the plain template. Drop a section whose inputs are missing.

### Design for the tone call

- Skip the call when there are no headlines and store `no_news`.
- Deduplicate and cap at 10 headlines.
- JSON schema with an enum label; describe the schema in the prompt too, because the server does not inject it.
- Token cap about 150-200, client timeout 30 s, one retry, then `unknown`.
- Log the raw output of every failed call.

### Measuring it

A golden set of 30-50 fact tables, each with 3-5 variants carrying one planted error (wrong period, proxy figure, adjective without a label, a claim from missing news, a number off by 10 %, a unit swap), scored by code. Track nightly: unsupported-number rate, period mismatches, schema failures, timeouts, share of `no_news` and `unknown`, fallback rate.

### LLM factor mining

Two 2026 preprints (as reported): a deliberately leaky strategy with a Sharpe of 35 passed deflated-Sharpe and PBO checks, and every LLM-discovered strategy was rejected under honest evaluation; in an audit, 252 of 923 LLM-proposed expressions had negative rank IC. Keep the rule you already have: the model proposes, a frozen statistical procedure judges, and every proposal is a counted trial.

### Contested

Whether constrained decoding lowers answer quality is disputed (one paper says yes; the rebuttals come from vendors of constrained-decoding tools). For a three-way tone label it does not matter.

---

## 5. Tests, CI and reliability

| # | Change | Effort (sweep's estimate) |
|---|---|---|
| 1 | Keep public-mirror CI as the gate; add a local pre-push hook (ruff, import contracts, cycles, tsc, targeted pytest) | 2-3 h |
| 2 | Coverage floor on changed lines (diff-cover at 80 %) | 2 h |
| 3 | A small AST check that fails on tests without an assertion | 2 h |
| 4 | Report skips and fail on unexpected ones | 1 h |
| 5 | Job result contract: ok / degraded / failed plus counts, and `self_check` alerts on repeated "degraded". This fixes the news-tone "success with 20 timeouts" class | 6-8 h |
| 6 | Ratchet the 304 broad `except Exception` handlers with a golden count that may only shrink, like the import-cycle ledger | 3 h |
| 7 | Null-simulation harness for every gate | 10-16 h |
| 8 | Property tests (Hypothesis) on `tax_calc`, `quant_metrics`, `allocation` | 8-12 h |
| 9 | Generate TypeScript types from `openapi.json`; type the 142 untyped responses so the fuzzer can check them | 10-16 h |
| 10 | Mutation testing nightly on the pure maths modules only | set up once |

A self-hosted GitHub runner would use the idle server, but GitHub's own guidance warns that such runners have no clean-machine guarantee, and it would sit beside a host holding broker credentials. Not recommended now; if ever, an ephemeral runner in an unprivileged container with no secrets.

The effort figures and the property list are the sweep's own estimates, not sourced.

A side finding from today: a worker restart resets interval-job timers. After the 08:57 and 10:04 restarts the four-hourly news refresh skipped its 09:09 run. Interval jobs should be anchored to wall-clock times.

---

## 6. Learn tab

Principles with the best evidence (mostly from school and lab studies, not self-taught adults):

1. **Predict first, then reveal.** Your current stations already do this; keep it.
2. **Worked example on your own data, then fade the steps** so you fill in more each time.
3. **Formula, plain sentence and the app's function side by side**, with a link to the source line.
4. **Short quiz at the end of each lesson, resurfaced after 2 days, 1 week, 3 weeks.**
5. **Aim each lesson at a known misconception**: what a p-value is not, overfitting, regression to the mean, testing many things.
6. **Uncertainty as "x out of 100"** and as fan charts, never as false precision.

The sweep's 16-lesson table (lesson, econometrics topic, app function, interactive element, exercise, reading) is in `research_eng.md`. It lines up with Stock-Watson, Hanck et al. "Introduction to Econometrics with R" (free online), Huntington-Klein "The Effect", Cunningham "Mixtape", Yale ECON 252 and MIT 15.401.

Two technical choices to make before building: whether formulas render with KaTeX, and whether code is shown read-only from the repo or runnable in the browser (Pyodide is the option for the latter; heavier).

---

## 7. Rendered-UI review

**How:** at the owner's choice, no test account was created (every nightly job loops over all users, and the trial ledger has no user column, so a second user would have added load and trials). Instead a two-hour session for the owner's account was minted with the app's own token function, the frontend was built locally at prod's commit, and its API calls were forwarded to prod. All non-GET requests were blocked in the browser; none was attempted. The token file, screenshots and helper servers were deleted afterwards.

**Result of the pass:** 30 pages on desktop (1440 px) and 8 on phone (390 px). No script errors, no 5xx, no sideways scrolling, exactly one h1 on every page.

What I saw on the six pages I looked at closely:

| Page | Observation |
|---|---|
| Stock detail | The page title reads "Watchlist" with the ticker as a second heading; on a phone there are three stacked titles ("Stock Detail", "Watchlist", "DTE.DE") |
| Stock detail | "model confidence 72 %" sits directly under every verdict, six times, in the most prominent card on the page |
| Stock detail | Chart grid lines are pure white and brighter than the candles; the range buttons (1W-5Y) are there and clear |
| Stock detail | Every stock page requests `/api/etf/{ticker}/profile` and gets a 404 |
| Stock detail, phone | The tab row is cut off ("Fundamen…") with no hint that it scrolls |
| Discover | The same two paragraphs ("Rank N of 965…", "Worth acting on? Not yet…") repeat on all 11 cards |
| Discover | Each card says "no edge is shown" beside a green "Ret. est. +4,2 %" badge. The two contradict each other |
| Discover | Raw flags in the UI: `revenue_growth_unreliable` |
| Discover | Three Chinese government-bond ETFs on one shortlist of eleven; a stock flagged "max drawdown 40.3 % exceeds 30 %" is still shortlisted |
| Portfolio → Risk | The currency chart lists 35 currencies, about 25 of them at 0.0 %, and stretches the page to 2,900 px with an empty column beside it |
| Portfolio → Risk | "Extreme" concentration in red directly above "Well diversified" in green |
| Portfolio → Risk | The asset-type donut is tiny and its labels overlap |
| Learn | Sliders are blue; the rest of the app's accent is orange. A slider at the far left looks like an answer of zero |
| Learn | Renders under the "Strategy lab" header and tab row, although the menu puts it under "My money" |
| Home | Clean and clear |

Overall: consistent, calm, readable on a phone. The problems are contradictions and repetition in the text, a few charts that waste space, and the confidence figure.

---

## 8. Actions taken on prod today

1. Dumped `news_items` and `user_news_relevance` to `/var/backups/quantfolio/news_before_retag_2026-11-12.dump` on LXC 110.
2. Ran `scripts/retag_news.py` as the service user: 656 items re-attributed to their feed ticker, 335 tickers cleared where the article does not name the company, all relevance tags cleared.
3. Ran the documented SQL: 714 keyword sentiment labels set to NULL.
4. Verified: 0 items left to move, 0 stale tags, 183 FinBERT labels untouched.
5. The worker will rescore and retag at its next news run, about 14:04 UTC (its timers were reset by the 10:04 restart).

The two-hour session token expires by itself at about 13:05 UTC. To end it sooner, log out once in the UI: that raises the session version and invalidates it.

---

## 9. What the research could not find or verify

- Yahoo's definition of `freeCashflow`, and whether its unadjusted close is split-adjusted.
- Any independent accuracy comparison of fundamentals vendors for European companies.
- A machine-readable feed for EQS, Euronext, Borsa Italiana, BME or SIX company news.
- Any benchmark score for Qwen3.5-9B on grounding, financial QA or schema adherence.
- Whether thinking mode improves numeric accuracy at 9B; the throughput gain from parallel slots on one 8 GB card.
- A published sequential test for a regression alpha or a Sharpe ratio using e-values.
- A library implementation of the fixed-smoothing Diebold-Mariano test.
- Sourced sector thresholds for net debt/EBITDA.
- Studies of chart range selectors or "learn" content with retail investors.
- Several statistical formulas in `research_stats.md` are written from memory and marked so; check them against the papers before coding.
- `research_eng.md` rests entirely on search snippets.

## 10. Revised order of work

Unchanged from the audit's section 4, with these additions and one change of emphasis:

1. **First, the cheap honesty fixes** (audit A1-A7), now with the concrete recipes in section 2 here.
2. **Then the report writer redesign** (section 4): it removes a whole class of errors and makes the model question moot.
3. **Then the null-calibration harness** (3.1, 5 #7): it validates every gate, including the two the audit found wrong, and is the best use of the idle CPU.
4. **A decision for you:** given 3.2, is the ladder's timescale acceptable?
5. **Before paying for data:** the ten-issuer truth set (2.8).
6. **Before changing the model:** the 30-50 prompt A/B (section 1).
