# Quant literature scan — August 2026

Scope: recent (2024–2026) work bearing on the five things this codebase actually
does — regime detection, composite factor scoring, ML return prediction, RL
allocation, and LLM-driven investment research.

**Provenance.** Every arXiv ID below was verified against the arXiv API on
2026-08-31: ID, title and first author match. The relevance notes are editorial
and were *not* verified against full texts — treat them as reading pointers, not
as claims about what each paper proves. Nothing here has been implemented; this
is a reading list, not a changelog.

> The alphaXiv MCP connector was not authenticated during this scan, so the
> search ran over the public arXiv API instead. Re-running with alphaXiv
> authorised would add citation-graph and code-repository context.

## 1. Market regime detection

Relevant to `services/regime/` and the `jumpmodels` entry on the AGENTS.md
dependency watch-list.

| Paper | ID | Why it matters here |
|---|---|---|
| Downside Risk Reduction Using Regime-Switching Signals: A Statistical Jump Model Approach — Shu et al., 2024 | [2402.05272](https://arxiv.org/abs/2402.05272) | The underlying method behind the `jumpmodels` library the watch-list tracks. Jump penalties enforce regime persistence, which plain HMMs lack. Read this before adopting or dropping that dependency. |
| Improving S&P 500 Volatility Forecasting through Regime-Switching Methods — Blake et al., 2025 | [2510.03236](https://arxiv.org/abs/2510.03236) | Argues *soft* regime assignment beats hard labels during uncertain periods. Our `regime` context emits a single hard label plus a confidence; this is the case for using the confidence as a weight rather than a display field. |
| Regime-Based Portfolio Allocation Using HMMs and Reinforcement Learning — Verma et al., 2026 | [2605.27848](https://arxiv.org/abs/2605.27848) | Directly mirrors our regime → allocation seam (`regime` feeding `advisor`). |
| Hybrid Hidden Markov Model for Equity Excess Growth Rate Dynamics — Alswaidan et al., 2026 | [2603.10202](https://arxiv.org/abs/2603.10202) | Scales regime estimation to hundreds of assets via transition counting instead of Baum-Welch. |
| Multi-Scale Markov Switching GARCH — Chaudhary et al., 2026 | [2606.06190](https://arxiv.org/abs/2606.06190) | Multi-timeframe volatility regimes; relevant if the single-scale regime signal proves too coarse. |
| Regime Discovery and Intra-Regime Return Dynamics in Global Equity Markets — Luwang et al., 2026 | [2601.08571](https://arxiv.org/abs/2601.08571) | Return behaviour *within* a regime, not just at transitions. |

## 2. Composite factor scoring & factor mining

Relevant to `services/discover/composite.py` and `services/alphacrafter/`.

| Paper | ID | Why it matters here |
|---|---|---|
| ML-Enhanced Multi-Factor Quantitative Trading with Bias Correction — Du et al., 2025 | [2507.07107](https://arxiv.org/abs/2507.07107) | **Most directly actionable.** Threads a tradability mask through every stage and reports a deflated Sharpe ratio. ADR 0003 already makes DSR a convention; this is a worked example of pairing it with a tradability guard. |
| FactorEngine: Program-level Knowledge-Infused Factor Mining — Lin et al., 2026 | [2603.16365](https://arxiv.org/abs/2603.16365) | Factors as code plus separated logic/parameter search — close in spirit to our `factor_dsl`. |
| Cognitive Alpha Mining via LLM-Driven Code-Based Evolution — Liu et al., 2026 | [2511.18850](https://arxiv.org/abs/2511.18850) | LLM-guided mutation of factor code; contrasts with brute-force genetic programming. |
| AgonAlpha: Autonomous Alpha Discovery — Ye et al., 2026 | [2608.11250](https://arxiv.org/abs/2608.11250) | Adversarial reviewer with veto power over LLM-proposed factors; a guardrail pattern for AlphaCrafter's bounded LLM proposals. |
| Harvesting the Volatility Risk Premium: A Learning-to-Rank Approach — Wysocki et al., 2026 | [2608.24786](https://arxiv.org/abs/2608.24786) | Ranking objective instead of score aggregation — an alternative framing for composite scoring. |

## 3. ML for return prediction

Relevant to `services/quant_ml/` and `compute_mincer_zarnowitz` in `quant_metrics.py`.

| Paper | ID | Why it matters here |
|---|---|---|
| The Uncertainty of Machine Learning Predictions in Asset Pricing — Liao et al., 2025 | [2503.00549](https://arxiv.org/abs/2503.00549) | Our ML signal is a point estimate fed into composite scoring with no error bars. This gives closed-form standard errors, which would let the discover pipeline weight a prediction by its own uncertainty. |
| ML-Based Bitcoin Trading Under Transaction Costs — Bysik et al., 2026 | [2606.00060](https://arxiv.org/abs/2606.00060) | **Cautionary.** Sign-based strategies stop working past ~10bp costs. Any walk-forward result we report without a cost model is optimistic. |
| Sequential Structure in Intraday Futures Data — Mesfin et al., 2026 | [2605.17724](https://arxiv.org/abs/2605.17724) | **Cautionary.** LSTM and gradient boosting both fail to beat baseline intraday; unstable feature importance across folds is diagnosed as noise-fitting. A useful null result to check our own walk-forward output against. |
| XGBoost with Walk-Forward Validation — Malla et al., 2026 | [2601.08896](https://arxiv.org/abs/2601.08896) | Expanding-window WFV protocol close to `walk_forward_cv`; sets realistic expectations for R². |
| Interpretable Deep Learning: Consensus-Bottleneck Asset Pricing — Kim et al., 2026 | [2512.16251](https://arxiv.org/abs/2512.16251) | Interpretability-by-design rather than post-hoc attribution. |

## 4. RL for portfolio allocation

Relevant to `services/quant_rl/` and the `finrl` watch-list entry.

| Paper | ID | Why it matters here |
|---|---|---|
| The Evolution of RL in Quantitative Finance: A Survey — Pippas et al., 2024 | [2408.10932](https://arxiv.org/abs/2408.10932) | Catalogues the standard failure modes: backtest overfitting, non-stationarity, unrealistic execution. Best single entry point before extending `quant_rl`. |
| DRL for Diversified Portfolio Management Across Global Equity Markets — Kashif et al., 2026 | [2605.17307](https://arxiv.org/abs/2605.17307) | **Cautionary and the most important one here.** Under walk-forward across three regions, SAC fails to outperform consistently. Directly tempers what our RL policy signal should be trusted to do. |
| DRL for Optimal Portfolio Allocation vs Mean-Variance — Sood et al., 2026 | [2602.17098](https://arxiv.org/abs/2602.17098) | Makes the case that RL must be benchmarked against MVO, not just a passive index. We have `quant_optim` (skfolio) available as exactly that baseline. |
| RL Portfolio Allocation with Dynamic Embedding of Market Information — He et al., 2025 | [2501.17992](https://arxiv.org/abs/2501.17992) | Autoencoder state compression plus online meta-learning for non-stationarity. |

## 5. LLM agents in investment research

Relevant to `services/discover/dossier_writer.py`, `services/llm/`, and `llm_portfolio`.

| Paper | ID | Why it matters here |
|---|---|---|
| Your AI, Not Your View: The Bias of LLMs in Investment Analysis — Lee et al., 2025 | [2507.20957](https://arxiv.org/abs/2507.20957) | **Most directly applicable.** Documents systematic large-cap/tech tilt, contrarian bias, and confirmation-bias escalation across iterative refinement — all three are live risks in dossier generation. |
| Agentic Trading: When LLM Agents Meet Financial Markets — Xia et al., 2026 | [2605.19337](https://arxiv.org/abs/2605.19337) | **Cautionary.** Surveys 19 empirical studies; almost none report transaction costs, survivorship handling, or reproducible protocols. A standard to hold our own claims to. |
| Toward Expert Investment Teams: Fine-Grained Trading Tasks — Miyazaki et al., 2026 | [2602.23330](https://arxiv.org/abs/2602.23330) | Decomposing analysis into subtasks beats monolithic generation — an argument for how `dossier_writer` structures its prompts. |
| AlphaAgents: LLM Multi-Agents for Equity Portfolio Construction — Zhao et al., 2025 | [2508.11152](https://arxiv.org/abs/2508.11152) | Role-based multi-agent equity analysis with explicit risk-tolerance conditioning. |
| Integrating LLMs in Financial Investments and Market Analysis: A Survey — Mahdavi et al., 2025 | [2507.01990](https://arxiv.org/abs/2507.01990) | Broad survey for orientation. |

## Themes worth acting on

1. **Uncertainty, not point estimates** ([2503.00549](https://arxiv.org/abs/2503.00549)) — the ML signal enters composite scoring as a bare number. Error bars would let it be weighted by its own reliability.
2. **Costs belong inside validation** ([2606.00060](https://arxiv.org/abs/2606.00060), [2605.19337](https://arxiv.org/abs/2605.19337)) — any walk-forward or backtest number reported without a cost model overstates the result.
3. **Temper the RL signal** ([2605.17307](https://arxiv.org/abs/2605.17307), [2408.10932](https://arxiv.org/abs/2408.10932)) — the literature does not support treating an RL policy as a reliable standalone edge; benchmark it against `quant_optim`'s MVO.
4. **LLM bias is measurable and ours is unmeasured** ([2507.20957](https://arxiv.org/abs/2507.20957)) — a bias audit over generated dossiers is a concrete, cheap next step.
