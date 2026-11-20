# Context: Quant RL

## Responsibility

RL Lab: FinRL portfolio-allocation/single-stock environments +
Stable-Baselines3 training/rollout. Enabled by default (`FINRL_ENABLED=true`,
Track D0, 2026-08-27 audit — fixed a `ModuleNotFoundError` in finrl's own
`__init__.py` chain and flipped the feature flag on; `/rl/train` used to be a
permanent no-op stub).

- `envs.py::build_env` — builds a FinRL gym environment
  (`StockPortfolioEnv` for `"portfolio_allocation"`,
  `StockTradingEnv` for `"single_stock"`) from an **already-fetched** OHLCV
  DataFrame (`ohlcv_df` parameter) — this module fetches no market data
  itself; see the Owner-wave notes below for why. Attaches `env.tickers =
  sorted(tickers)` (additive metadata, not read by FinRL itself) so
  `rollout.py` can map the env's per-step weight vectors back to ticker
  symbols.
- `train.py::train_policy` — trains a policy via Stable-Baselines3
  (PPO/A2C/DDPG/TD3), persisting a `QuantRlPolicy` row (`status`:
  `"training"` → `"completed"`/`"failed"`). Takes `ohlcv_df` as a
  keyword-only required argument, same reasoning as `build_env`.
- `rollout.py::run_rollout` — runs a trained policy for `n_steps`, reading
  FinRL's `asset_memory` for the true portfolio-value equity curve (not the
  raw step reward, which is a scaled value delta, not a return — a past bug).
  Also returns `final_weights` (per-ticker, from `StockPortfolioEnv`'s
  `actions_memory[-1]`) and `period_returns` (from
  `portfolio_return_memory[1:]`) — added in Track D2 so a caller can gate a
  policy on its own realised rollout Sharpe and surface a per-ticker
  advisory signal. `actions_memory`'s column order is always alphabetical
  by ticker (FinRL's own `_add_covariance_list` sorts by `["date", "tic"]`
  before fitting), which is why `envs.py` attaches `env.tickers =
  sorted(tickers)` rather than the caller's original order.

## Public surface (facade `app.lab.quant_rl`)

None re-exported at the package root (`__init__.py` is docstring-only) —
every caller imports submodules directly (`quant_rl.envs`, `quant_rl.train`,
`quant_rl.rollout`), the same practical-surface-vs-facade gap
`regime/CONTEXT.md` documents for that package.

## Key collaborators

- In: `app.decision.advisor.rl_training`
  (`train_and_validate_policy`, Track D2 — the scheduled quarterly
  `advisor_rl_training` job's real entry point; fetches OHLCV, trains, rolls
  out, and gates on rollout Sharpe via `quant_metrics.sharpe_ratio`, all
  *outside* this package — see Owner-wave notes).
- Out: `app.foundation.models.entities.QuantRlPolicy` — nothing else; no
  `app.decision.*` imports anywhere in this package (confirmed by
  inspection, and load-bearing — see below), only `finrl`/`stable_baselines3`/
  `pandas`.

## Contract invariants

- Not a member of any facade-only or decision-loop-independence contract —
  sits outside those lists. Lab-tier by physical placement (ADR 0015 Phase
  5), matching `quant_ml`: may import `app.foundation`, must never import
  `app.decision` or `app.interface`.
- Global layering (interface → decision → lab → foundation) holds by
  inspection and is enforced by the `Global layering` import-linter
  contract.

## Owner-wave notes

**The "no data-fetching / no cross-cutting imports inside this package"
discipline here is the one thing to protect before touching this package
again — this section predates ADR 0015 Phase 5 and is preserved as history;
the specific mechanism it describes (a flat `app.services` SCC blob) no
longer exists post-restructure, but the underlying discipline is now
formalised by the `Global layering` contract instead (see below).** Track D2
(wiring a validated RL signal into `advisor`'s paper-trading cycle)
originally had `envs.py` fetch its own OHLCV data via the (then-flat)
`app.services.market` and `advisor.rl_training` compute its Sharpe gate via
`app.services.quant_metrics` from *inside* `quant_rl` — both looked like
ordinary, harmless imports. `check_no_new_cycles.py` caught it: it
aggregated every flat module directly under `app.services` (e.g.
`market.py`, `quant_metrics.py`) into a single blob node, and that blob was
*already* part of the decision loop's accepted dense import-cycle SCC
(advisor/discover/alphacrafter/.../`app.services`). The instant `advisor`
(already inside that SCC) imported `quant_rl`, any edge from `quant_rl`
back into that blob — via *any* flat `app.services` module, not just ones
that looked decision-loop-related — closed the loop and merged `quant_rl`
into the SCC too.

The fix, still load-bearing today: `build_env`/`train_policy`/`run_rollout`
take already-fetched data (`ohlcv_df`) instead of a `db` session and fetch
nothing themselves; `app.foundation.market.ohlcv_frame` (not inside this
package) is what callers use to build that DataFrame; the Sharpe-gating
logic that decides whether a trained policy is trusted lives in
`app.decision.advisor.rl_training`, not here, specifically because it needs
`quant_metrics.sharpe_ratio`. Post-Phase-5, the sharpest form of the same
rule is simply the `Global layering` contract: `quant_rl` is `app.lab`, so
any `app.decision.*` import from inside it is a straightforward layers
violation, checked mechanically by `lint-imports` rather than discovered
after the fact by `check_no_new_cycles.py`. `app.foundation.*` imports
remain layers-legal, but this package still deliberately avoids fetching
its own data for the testability/decoupling reason above. Before adding
*any* new import to this package, check which layer it lives in. `quant_ml`
hit the same constraint independently (Track D1); see its `CONTEXT.md` for
the shorter version of the same story.

`Path("results").mkdir(...)` in `envs.py::build_env` exists because FinRL's
`StockPortfolioEnv`/`StockTradingEnv` unconditionally `plt.savefig()`/write
CSVs to a hardcoded `"results/"` path relative to cwd on the terminal step —
without it, training/rollout raises `FileNotFoundError` only once it
otherwise works, which makes the failure confusing if this line is ever
removed.

The manual RL Lab HTTP endpoints (`/api/quant/rl/{envs,policies,train,policies/{id}/rollout}`)
were removed (zero UI, package frozen); training runs only through the scheduled
`advisor_rl_training` job, so no RL training starts inside the API process.
