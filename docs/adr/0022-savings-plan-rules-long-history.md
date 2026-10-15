# 0022: Savings-Plan Asset-Mix Rules, Tested on 16 Countries Since 1870, Pre-Registered

**Status:** Accepted 2026-10-14 (pre-registration; run once after this file is committed)
**Date:** 2026-10-14
**Context source:** the 2026-10-14 audit plan (`docs/plan-audit-2026-10-14.md`, decision D2, WP-C)
**Extends:** [0015](0015-honest-measurement-rebuild.md) (the trial ledger),
[0019](0019-etf-fund-choice-and-stock-ranking.md) (the funds a passing rule is bought through),
[0020](0020-tier-b-long-history-preregistration.md) (overlays and tilts: all failed on 2026-10-14)
**Consumed by:** `backend/app/lab/long_history/`, `backend/scripts/plan_rules_long_history.py`

This ADR is a **pre-registration**. It fixes the rules, data, simulation, costs, metric and pass
bar *before* any rule is run.

**Observed when written:** the JST R.6 file had been downloaded and its columns and coverage
listed: 18 countries, 1870-2020, 16 of them with equity total returns. No return, mean or
statistic had been computed. The owner's plan settings were read: €100 a month, horizon
setting "medium (3-10 years)", the app's default.

## Context

The ETF engine recommends the owner's plan, so its rules must be right for a monthly saver over
decades. Live data cannot test that within a lifetime. Many countries' long histories can, once,
with the test fixed in advance.

ADR 0020 tested the overlays and tilts (trend, min-vol, size, value, momentum), and none passed.
This ADR tests what it did not: **how to split the plan between assets**.

The finding to beat is Anarkulova, Cederburg & O'Doherty (2024): for a lifetime saver, an
all-equity mix (domestic and international) beats a stock-bond or target-date mix. Bonds lower
the drawdown and cost saving power.

## Decision

### Data (local only)

- **JST Macrohistory R.6** (Jordà, Knoll, Kuvshinov, Schularick & Taylor), annual, 1870-2020.
  - The file is `data/long_history/jst_r6/JSTdatasetR6.dta`, SHA-256
    `b0ebb74a8d1b5b1bc9033fc46a6dcc578736afff8ce1e1086b7840f2649e79b3`.
  - The licence is CC BY-NC-SA. The file stays on the owner's machines; it is gitignored and
    excluded from the public export.
  - The countries are the 16 with `eq_tr`: Australia, Belgium, Denmark, Finland, France, Germany,
    Italy, Japan, Netherlands, Norway, Portugal, Spain, Sweden, Switzerland, UK and USA.
- **Variables used:**
  - `eq_tr` and `bond_tr`: nominal total returns, local currency;
  - `bill_rate`;
  - `cpi`;
  - `xrusd`: local currency per USD;
  - `gdp`: nominal, local currency.
- A country-year enters only with `eq_tr`, `bond_tr`, `bill_rate` and `cpi` all present, and the
  previous year's `cpi` and `xrusd`.

### Construction (for a saver at home in country *h*)

- **Real return:** (1 + r) / (1 + π) − 1, with π the home CPI change.
- **Foreign equity in home currency:** (1 + r_j) · FX_t / FX_{t−1} − 1, with FX = xrusd_h /
  xrusd_j (home units per unit of *j*).
- **Global equity:** the GDP-weighted mean of all available countries' equity (home included),
  in home currency.
  - The weights are the previous year's GDP in USD (gdp / xrusd), renormalised over the
    countries present that year.
  - GDP stands in for market capitalisation, which JST does not have before 1900 for most
    countries.
- **International equity:** the same, without the home country.
- **Bonds:** home government bonds (`bond_tr`). **Bills:** home `bill_rate`.

### The rules (one `trial_ledger` row each, context `plan_rules`)

| Rule | Mix, rebalanced once a year to target |
|---|---|
| **R0** | 100 % global equity (baseline: the current core) |
| **R1** | 50 % home equity, 50 % international equity |
| **R2** | 80 % global equity, 20 % home government bonds |
| **R3** | 60 % global equity, 40 % home government bonds |
| **R4** | Glide path: 100 % global equity until 10 years before the horizon, then linearly to 50 % equity / 50 % bonds at the horizon |
| **R5** | R0, except from 5 years before the horizon a home-bill sleeve holds a planned withdrawal of 24 monthly contributions (in real terms) due at the horizon, capped at wealth |

These six are the whole family. Any other mix, horizon rule or asset is a new trial. It is recorded
before it is run.

### Simulation

- **Plan:** a constant real contribution every year (12 × the monthly amount) at the start of
  the year, then the year's return.
  - With CRRA utility the result does not depend on the amount, so the owner's €100 a month
    only scales the plot.
- **Horizons:** **30 years (primary)**, with 10 and 20 years reported.
  - **Owner input:** the app's horizon setting is still its default. The pass bar is judged at
    30 years; the owner confirmed the 30-year horizon on 2026-10-15. A different primary horizon
    is a new amendment, made before any rerun.
- **Bootstrap:**
  - A stationary block bootstrap (Politis & Romano 1994) of whole country-years, so all assets
    of a year move together. The mean block is 10 years.
  - Each path keeps one home country.
  - There are 10,000 paths per country and horizon, with seed 20261014.
- **Costs:**
  - TER, as a yearly drag: equity 0.20 %, bonds 0.15 %, bills 0.10 %.
  - Rebalancing trades pay 0.10 % of the amount moved.
- **German tax:** on gains realised by rebalancing.
  - The rate is 26.375 %, with 30 % Teilfreistellung on equity funds.
  - The first €1,000 a year (real) is free, the Sparer-Pauschbetrag, scaled to the €100 plan.
  - **Sensitivity:** no tax (NV certificate).
- **Haircut:** every equity return is lowered by 30 % of that series' own historical mean real
  premium over bills (McLean & Pontiff), in the primary run. **Sensitivity:** no haircut.

### Metric

- **Equivalent savings change** Δ_rule = CE_rule / CE_R0 − 1. CE is the certainty equivalent of
  real terminal wealth under CRRA utility with γ = 5; γ = 3 and 10 are reported.
  - Δ = +5 % means a saver in R0 would have to save 5 % more to be as well off as with the rule.
- **Also reported:**
  - P(real terminal wealth < real contributions);
  - the median and 90th-percentile maximum drawdown of the plan's wealth;
  - for R5, P(the withdrawal cannot be paid in full).

### Pass bar (a rule is recommended over R0 only if all hold, at 30 years, γ = 5, primary costs)

1. The 95 % interval of Δ excludes 0, with Δ > 0. The interval comes from a bootstrap over the
   16 home countries' Δ, with 10,000 resamples.
2. Δ > 0 for at least 12 of 16 home countries.
3. Δ > 0 when the bootstrap draws only from 1870-1949 years, and again only from 1950-2020.
4. Δ > 0 after each of three drops: without Japan, without Germany, and without the war years
   1914-1918 and 1939-1945.
5. Without any single decade, Δ keeps at least half of its full value.

### What the recommender shows

- A rule that passes may be recommended.
- For every rule that fails, the plan page states its cost, not a forecast. For example: "a 20 %
  bond sleeve: about x % more savings needed for the same outcome, maximum drawdown −y pp".
- An owner target remains the owner's choice, made with that cost in view.

### Expected (stated now, not a pass condition)

- R0 holds. Rules with bonds lose on outcome and win on drawdown.
- R1 (home bias) costs most in the countries with a poor century.

## Consequences

- One `trial_ledger` row per rule, so n_trials rises by six.
- The results are written into this ADR's "Results" section with the code SHA. This file is not
  otherwise edited.
- Nothing runs on the live app. The lab code reads the local file, and the plan page reads the
  committed result.

## Results (run once, 2026-10-14)

- **Run:**
  - code `6466a69750442ceba860f62dba5ab073fd395944`;
  - data SHA-256 `b0ebb74a…49e79b3` (as above);
  - seed 20261014, 10,000 paths per country and horizon.
- The output file is `data/long_history/results/plan_rules_2026-10-14.json`. It is local only, like the data.
- Country coverage runs from 115 years (Spain, 1900-2020) to 150 (Sweden, UK).

### Verdict

**No rule passes. R0 (100 % global equity) stays the plan's rule.**
- No rule meets even the first check: every pooled Δ is negative.
- The six `trial_ledger` rows (context `plan_rules`: R0 `baseline_kept`, R1-R5 `failed`) were written on
  prod on 2026-10-15 with the owner's OK.

### Table (30 years, γ = 5, primary costs, pooled over the 16 home countries)

| Rule | Δ vs R0 | 95 % interval | countries Δ > 0 | 1870-1949 | 1950-2020 | no Japan / no Germany / no war years | no tax | no haircut | P(wealth < contributions) | max drawdown, median / 90th pct | passes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R0 | — | | | | | | | | 11.3 % | 48 % / 67 % | |
| R1 home 50 / intl 50 | −7.0 % | −11.0 % to +11.1 % | 8 | −23.1 % | −18.6 % | +3.8 % / −28.6 % / −4.4 % | −6.8 % | −0.8 % | 12.0 % | 47 % / 69 % | no |
| R2 80 / 20 bonds | −5.4 % | −6.6 % to +5.1 % | 11 | −11.7 % | +6.8 % | +0.9 % / −10.2 % / +10.3 % | −5.4 % | −12.2 % | 10.6 % | 39 % / 64 % | no |
| R3 60 / 40 bonds | −14.2 % | −15.8 % to +3.0 % | 9 | −24.3 % | +3.3 % | −5.1 % / −23.6 % / +11.2 % | −14.0 % | −25.7 % | 11.7 % | 31 % / 65 % | no |
| R4 glide path | −13.9 % | −15.7 % to +5.3 % | 11 | −19.6 % | +7.5 % | −3.1 % / −18.1 % / +10.6 % | −13.8 % | −21.3 % | 10.3 % | 46 % / 66 % | no |
| R5 bill sleeve | −13.1 % | −15.2 % to +5.5 % | 13 | −11.8 % | +2.3 % | +1.0 % / −10.3 % / +2.9 % | −13.1 % | −20.1 % | 11.2 % | 48 % / 67 % | no |

**Correction of 2026-10-15 (drawdowns only).** The drawdown index left out the year's trading
costs and tax, so a year whose return just offset them showed no fall. It now measures each year
from the wealth before those deductions. Re-run on the same code otherwise, seed and data
(`plan_rules_2026-10-15_dd_fix.json`, local): every Δ, interval, count and verdict is identical;
the drawdowns move by at most 0.2 points, and only R1's rounded figures change (46 % / 68 % before).

**Other reported quantities:**
- **10 and 20 years, γ = 5:**
  - R1: −13.7 % and −29.4 %.
  - R2: −4.2 % and −8.3 %.
  - R3: −10.2 % and −17.3 %.
  - R4: −11.6 % and −20.2 %.
  - R5: −9.1 % and −22.7 %.
- **γ = 3 and γ = 10 (30 years):**
  - R1: −3.2 % and −15.6 %.
  - R2: −1.1 % and −4.5 %.
  - R3: −6.8 % and −12.5 %.
  - R4: −2.8 % and −12.1 %.
  - R5: −1.7 % and −15.3 %.
- **Without any single decade:** Δ stays negative for every rule, except R2 (+2.1 %) and R5 (+0.3 %) without the 1940s.
- **R5:** P(the bill sleeve is below the planned withdrawal at the horizon) is 35 %.
  - This is mostly a sleeve worth slightly less than 24 contributions after a final year of negative real bill returns, not a sleeve that is empty.

### Reading (after the look; not part of the pass bar)

- **The pooled measure is decided by a handful of catastrophic paths.**
  - This was checked after the run on the same seed and paths.
  - Among the 160,000 primary paths (16 countries × 10,000), the worst 16 (0.01 %) supply about half of E[W^−4], and the worst 160 (0.1 %) supply 80-87 %.
  - Nearly all of them are paths of a Japanese saver through 1945-1949, with real terminal wealth near 5 % of contributions.
  - So the pooled Δ answers "which mix is least bad in the worst histories", which is what γ = 5 asks. That is why it moves so much between the decade and country drops.
  - By home country, Japan supplies about 80 % of R0's pooled E[W^−4]. Without Japan, R2 and R5 are each about +0.9 %:
    inside the noise, and both still fail checks 1, 3 and 5.
- **"Without Germany" drops Germany as a home country only.** German equity stays in the global mix.
- **In the typical country the picture differs.** The median of the 16 per-country Δ is:
  - R1: −1.2 %;
  - R2: +2.9 %;
  - R3: +0.7 %;
  - R4: +3.8 %;
  - R5: +1.7 %.
  - A bond sleeve helped a risk-averse saver a little in most countries and cost a lot where bonds were inflated away: Japan, Finland, Portugal, and Germany before 1950.
  - None of this meets the bar. It is the honest cost to show next to an owner target.
- **The expectations stated in advance partly held:**
  - R0 holds.
  - Bonds lowered the median drawdown (R3: 31 % against 48 %) and did not raise the outcome.
  - Home bias (R1) cost most where the home market had a poor century: Portugal −42 %, France −34 %.
- **What the plan page shows** follows "What the recommender shows":
  - for each rule, its pooled Δ and its per-country median Δ, side by side;
  - its median drawdown against R0.
  - Neither is a forecast.

## References

- Jordà, Knoll, Kuvshinov, Schularick & Taylor (2019), "The Rate of Return on Everything,
  1870-2015", QJE; JST Macrohistory Database R.6.
- Anarkulova, Cederburg & O'Doherty (2024), "Beyond the Status Quo: A Critical Assessment of
  Lifecycle Investment Advice".
- Politis & Romano (1994), "The Stationary Bootstrap", JASA.
- McLean & Pontiff (2016), "Does Academic Research Destroy Stock Return Predictability?", JF.
