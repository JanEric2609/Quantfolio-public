# ADR 0023 — Verified reflection lessons, and Mandate B as a rules control

Date: 2026-11-09. Status: accepted.

## Context

The audit of 2026-11-09 found on prod:

- **Advisor confidence was worse than a coin flip.** Its stated confidence had a Brier score of 0.327, against a coin flip's 0.25, and its hit rate fell as stated confidence rose. The scorecard's calibration axis scored only the Monte Carlo band (RPS), so this never showed up in the composite.
- **Reflection lessons were not grounded.** They included false numbers ("over 70 % of buys gained"), contradicted one another, repeated themselves, and drew on trades from before the sleeve's last reset. They were injected into the prompt as a bare list.
- **Mandates A and B were the same experiment twice.** B's "momentum" existed only in its prompt; the two sleeves held 12 of the same 13 names and were 0.46 % apart.
- **The mandates read a random shortlist.** Their "latest Discover shortlist" was ten shortlisted rows from any run, ordered by a random UUID.

A research pass on the same day agreed with the measurements. Once memorisation is controlled, LLMs show essentially no monthly stock-picking skill (look-ahead studies, masked-ticker benchmarks). Verbal confidence is overconfident. Free-text reflections built on small samples mostly record the market's move, not the decision (ExpeL, MemTrial). The standard control for a discretionary sleeve is a rules sleeve on the same universe.

## Decision

### 1. Lessons are checked against computed numbers

`reflection.track_record_stats` computes the sleeve's resolved directional calls since its `inception_at`. They are grouped as all, buys, sells, high and low confidence (split at 0.6), equities and funds. Each group carries n, the hit rate against EUNL.DE over 21 trading days, the mean excess return, the mean stated confidence and the Brier score, plus calibration bins.

Each lesson must name one group (`stat`) and a `verdict`. It is stored only when all of these hold:

- the group has at least 20 trades (`MIN_LESSON_N`);
- the group's hit rate differs from 50 % with |z| ≥ 1.28;
- the verdict agrees with the group's sign: `working` needs excess above 0 and a hit rate of at least 50 %; `not_working` needs the reverse;
- every percentage or fraction the lesson quotes is one of that group's numbers, within 2 percentage points. Confidence figures count only when the lesson talks about confidence.

A newer lesson on the same group replaces the older one. At most 5 are active. Lessons that cite no stat, or that predate the last reset, are retired and never shown. With fewer than 20 resolved trades, reflection does not call the model at all.

### 2. The model sees its record

The decision prompt carries `your_track_record`, the same statistics as above. The system prompt now has:

- a calibration note: confidence is scored by Brier against EUNL.DE; lower it where your bins show you were overconfident;
- a lessons section, each lesson prefixed with `[group, n, verdict]`.

### 3. Calibration axis

The composite's calibration axis is now the mean of two parts: 1 − RPS for the Monte Carlo band, and 1 − Brier/0.25 for the stated confidence. Either is used alone when the other is missing.

### 4. Mandate B is the rules control

B runs the same weekly review on the same shortlist, under A's limits: at most 15 % in one stock, at least 40 % in ETFs, and A's sell gates (at most a third of a position per review and the trailing-window cap of ADR 0012/0013), so a dropped name is wound down over several reviews. It makes no LLM call; the frozen rules are in `llm_portfolio/rules.py`:

- **Signal:** 12-1 month momentum on EUR closes, positive momentum only.
- **Volatility filter:** with 4 or more eligible names, the most volatile quarter is dropped.
- **Selection:** the top 5 names. A holding stays while it ranks in the top 8.
- **Weights:** inverse volatility, capped at 15 % per name; the remainder goes to the core ETF.
- **Rebalancing:** a 2 % of NAV band and a €25 minimum order. Sells execute first.

Each review is journalled like A's, with a pre-registered expectation of beating EUNL.DE over 4 weeks.

A minus B is the value the LLM adds over a simple rule. An existing B portfolio adopts the rules config at its next review. Both mandates should be reset on the same day, so the comparison starts from one book.

### 5. One shortlist

All three mandate readers now go through `llm_portfolio/shortlist.py`. It returns the shortlist of the newest completed Discover run.

## Consequences

- There will usually be no active lesson for months: 20 resolved trades in one group, with a clear result, takes time. The diagnostics page reports this as expected rather than as a fault.
- B's turnover is bounded by the buffer and the band. Its decisions are reproducible from its journal (ranking, targets, orders).
- Retiring A's stock picking is a separate decision. It should rest on the pre-registered A − B excess after enough reviews, not on this ADR.
