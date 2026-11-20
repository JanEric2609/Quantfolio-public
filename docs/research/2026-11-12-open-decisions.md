# Open decisions of the 2026-11-12 audit plan, settled by delegation

The owner asked for the best-researched option on each. Each can be reversed by editing one value.

| Decision | Choice | Why |
|---|---|---|
| Newey-West lag in the Learn lesson | Default 5 with the slider kept | A teaching default. For h-day overlapping returns the exact structure is h-1 lags; production uses that (promotion test) or non-overlapping dates. The lesson shows the t falling as lags grow. |
| Monte Carlo fan: discount the expected return | Keep the estimate as is, label it an estimate and show the zero-drift line beside it | A fixed haircut would be an unmeasured number. The pooled-ML and gate-calibration notes show estimates of this kind are noisy; showing the zero-drift case lets the reader discount it. |
| Pre-registration (pooled non-price study) | Approved as written; commit unchanged before any run; run not started | One hypothesis, t >= 3, power stated (only a large effect is detectable). Running it is a separate step. |
| Old partial Alpaca bars | No bulk delete; re-pull replaces them; optional sweep in `docs/runbooks/2026-11-12-partial-alpaca-bars.md` | A delete cannot repair a reused ticker, and an unfetched symbol is not read. |
| LLM A/B (D8) | Stay on the current model until the A/B has run on the app host | The harness is built; this checkout cannot reach a model endpoint. |
