# Dependabot triage, 2026-11-25

Recommendations only: nothing was merged, closed or changed in `pyproject.toml`, `package.json` or the lock files. Open bot PRs: 11 in the private repo, 10 in the public mirror (the public numbers repeat the private ones).

How the candidates were tested: in a scratch virtualenv built from `backend/requirements.lock`, one candidate version at a time, then restored. This is a smoke check on a few suites, not the full gate. A candidate that passes here still needs the full suite before the cap is lifted.

## Backend: keep the caps (sqlalchemy, pydantic, plotly), lift one (duckdb)

| Private / public PR | Change | Evidence on 2026-11-25 | Recommendation |
|---|---|---|---|
| #290 / #4 | sqlalchemy `<2.1` to `<2.2` | PyPI has 2.1.9. With 2.1.9 installed, `pyright app/` reports **43 errors** (the AGENTS.md note counted 29 on 2.1.1); the first ones are `Row` and `order_by` typing in `foundation/data_backbone/bars.py`, `regime_store.py`, `decision/trade_check.py`, `interface/api/evidence.py`, `portfolio_analysis.py`. The settings and tax tests (73) pass | **Close, keep the cap.** Lift deliberately: fix the typing in its own PR, bump the lock, run the full suite |
| #363 / none | pydantic `<2.14` to `<2.15` | PyPI has 2.14.4. With 2.14.4 the regenerated `docs/api/openapi.json` differs by 28 removed lines (the Decimal `pattern`), the same contract change AGENTS.md describes | **Close, keep the cap.** Lift only together with a lock bump and a regenerated snapshot that says so in the PR description |
| none / #6 | plotly `<7` to `<8` | PyPI has 7.2.0. With plotly 7.2.0 and the locked vectorbt 1.0.0, `tests/test_backtest_vbt_signals.py` fails 6 tests. vectorbt 1.2.1 (2026-11-11) is available and `Portfolio.plot()` ran with plotly 7.2.0, but a first signal test still failed with it, so the pair is not clean | **Close, keep the cap.** Revisit after vectorbt and plotly are bumped together and `tests/test_backtest_vbt_*.py` is green |
| #364 / #5 | duckdb `<2` to `<3` | PyPI has 2.0.1. With duckdb 2.0.1 all 47 tests in `tests/test_data_engineering_pit_*.py` pass (the PIT-join and trailing-window range-join paths) | **Candidate to lift** after the full suite and one nightly run on the owner's host. Bump the lock in the same PR; keep the explicit `memory_limit` and `threads` |
| #362 / #3 | ruff `<0.16` to `<0.18` | The cap exists because 0.16.0 pulled whole rule families into `extend-select` (1,500+ false positives). PyPI is now at 0.17.6; not tested | **Do not merge as is.** Test 0.17.6 with `ruff check app/`: if the rule-resolution problem is gone, raise the cap to `<0.18` and bump the lock; if not, close |
| #289 / #1 | quant group: pandas `<3` to `<4`, skfolio `<1` to `<2` | pandas 3.1.1 and skfolio 1.14.6 exist. The pandas cap is deliberate (vectorbt trade counting, `merge` dtype errors); the private PR lifts it | **Close.** Do not touch pandas until vectorbt is verified on 3.x. A skfolio 1.x move is a separate, tested change (the optimiser wrappers in `foundation/quant_optim.py`) |

Every one of these edits `pyproject.toml` only; the locked versions in `requirements.lock` (sqlalchemy 2.0.50, pydantic 2.13.4, plotly 6.8.0, duckdb 1.5.5) do not move. Merging any of them changes CI's floating install without changing prod, which is the drift AGENTS.md describes. An alternative that stops the noise: add `ignore` entries for these five packages in `.github/dependabot.yml`, with a comment pointing at AGENTS.md, so the PRs stop coming back.

## Frontend

| Private / public PR | Change | Risk | Recommendation |
|---|---|---|---|
| #416 / #2 | react group: react and react-dom 19.3.0 to 19.3.1, react-hook-form 7.89.0 to 7.92.0, react-hotkeys-hook 5.3.3 to 5.4.0, react-router-dom 7.18.4 to 7.18.6 | Patch and minor | **Merge candidate** after `npm run build` and `npx vitest run` |
| #329 / #7 | radix group, 14 packages, mostly minor (accordion 1.2.20 to 1.3.1, dialog 1.1.23 to 1.3.1, tooltip 1.2.16 to 1.4.1, slot 1.3.3 to 1.5.0) | Minor bumps across 14 components; the smoke test opens every page | **Merge candidate** after build, vitest and the browser smoke test |
| #331 / none | @vitejs/plugin-react 6.1.1 to 6.2.1 | Minor | Merge candidate with the react group |
| none / #9, #10 | @fontsource/inter-tight 5.2.7 to 5.3.0, @tailwindcss/postcss 4.3.3 to 4.3.5 | Minor and patch | Merge candidates; check the build and one visual page |
| #423 / #14 | testing group: vitest and @vitest/coverage-v8 4.1.12 to 5.1.1, jsdom 30.1.2 to 31.0.1, @testing-library/jest-dom 7.0.1 to 8.0.0, user-event 14.6.7 to 14.6.13 | Three majors; can break the whole test setup | Do separately, by hand, with the full vitest run. Not urgent |
| #372 / none | @tanstack/react-table 8.21.3 to 9.3.1 | Major; v9 changes the table API and the project wraps tables in `components/ui/table.tsx` | **Close** until a planned migration; add an `ignore` for major versions |

The front-end items were not run in this pass beyond installing the current lock (`npm ci`); the two merge-candidate groups have not been built against the new versions.

## Duplicates and staleness

The private PRs #289, #290, #329, #331, #362 to #364 and #372 are 3 to 8 weeks old and all of their public twins were opened on 2026-11-20. Closing the stale private copies and keeping the public ones (or the reverse) avoids running every bump twice.
