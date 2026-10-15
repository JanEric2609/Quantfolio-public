# lab/risk_forecast

**Mandate.** Each trading day, forecast the 1-day and 21-day volatility, 95 % VaR and 97.5 % ES of:
- every paper sleeve;
- each user's real book;
- the benchmark.

Store the forecasts before the fact, and score them once the horizon has passed. It is the app's
fastest honest feedback: VaR coverage resolves in weeks (plan 2026-10-14, WP-D3).

**Public surface.** `run_forecasts(db)`, `summary(db, user_id)`, `forecast(returns, h)`, `HORIZONS`
and `MODEL_VERSION`.

**Models** (`models.py`, NumPy/SciPy only; `arch` is not in the lock):
- **Volatility:** the mean of a direct log-HAR (Duan smearing) and a GARCH(1,1)-t fitted by
  maximum likelihood.
- **Quantiles:** a split-conformal correction. The models are fitted on the first two thirds of
  about 3 years. The last third's non-overlapping h-day returns, each standardised by the forecast
  made before it, give the empirical quantile and tail mean. That set is centred on the first two
  thirds' mean only.
- **21 days:** 3 years leave about 12 non-overlapping 21-day windows, fewer than the 20 required,
  so the 21-day bands use the one-day standardised residuals scaled by the 21-day sigma.
- **Units:** an h-day return is the sum of the daily returns, in the forecast and in the score.
- **Versioning:** bump `MODEL_VERSION` with any change; it is stored on every row.

**Scoring** (`backtest.py`): the e-backtests of Wang, Wang & Ziegel (2022, arXiv 2209.00991).
- **VaR:** `1{loss > VaR} / 0.05`.
- **ES:** `(loss - VaR)+ / (0.025 (ES - VaR))`.
- **Combination:** a GREE betting product, with each λ chosen from past values only.
- **Overlap:** 21-day windows enter the product only when they do not overlap.

**Storage.** Two append-only tables (migration 0139): `risk_forecasts` (weights frozen per row)
and `risk_forecast_scores`.

**Collaborators.**
- Reads foundation only: entities, `eur_prices` (stored bars, `allow_live=False`),
  `live_positions`, `paper_cash` and `forecast_verification.coverage`.
- Runs inside the `trade_check_daily` job (22:30 UTC). The worker's pool is sized for the job
  count, so it has no job of its own.

**Invariants.**
- A forecast row is never updated.
- A score uses the weights frozen on the forecast day, not later trades.
- A symbol without stored closes is reported as `unpriced` and contributes 0 return, which
  understates risk. The API shows the unpriced share.
