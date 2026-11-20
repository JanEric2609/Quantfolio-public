# lab/plan_bands

**Mandate.** Each month, store a one-year 10/50/90 % band of the real book's real return. Do it
before the year starts, under two models:
- `planner`: the projection's parametric model;
- `long_history`: the JST bootstrap of `lab/long_history/plan_fan.py`.

Score each band once its year has passed (plan 2026-10-14, WP-D6).

**Public surface.** `run_plan_bands(db)` (publish, then score), `publish_due`, `score_due`,
`summary(db, user_id)`, `book_mix(positions)`, `MODELS`, `COVERAGE_LEVEL`.

**Scoring.**
- **Realised return:** the book's time-weighted unit value (`foundation/book_performance.py`) over
  the year, so deposits never count as growth.
- **Real terms:** deflated by euro-area HICP (`HICP_EA`, % on a year earlier) for the end month.
- **When a band waits:**
  - until that month's HICP is stored;
  - while either end of the year has no unit value within 7 days.
- **Giving up:** a band still unscored 75 days after its year ended stays unscored.

**Independence.** Monthly bands for one-year horizons overlap twelve-fold. The coverage interval
uses only bands whose years do not overlap.

**Where it runs.** Inside `trade_check_daily` (22:30 UTC), after the risk forecasts, because the
worker's job cap is reached.

**Data.** The JST file is CC BY-NC-SA. It is read from `data/long_history/jst_r6/JSTdatasetR6.dta`
(or `QUANTFOLIO_JST_PATH`) and never enters the repository or the public export. Without it only
the `planner` band is stored.
