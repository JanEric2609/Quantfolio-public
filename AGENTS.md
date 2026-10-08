# AGENTS.md

This file is the single source of truth for AI coding assistants (Claude Code, opencode, Codex, Gemini CLI, etc.) working in this repository. Claude Code imports it via `CLAUDE.md`.

## Commands

### Backend
```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first-time setup
.venv/bin/uvicorn app.main:app --reload                      # dev server (port 8000)
.venv/bin/pytest -n 6                                        # all tests (parallel, 6 workers)
.venv/bin/pytest tests/test_tax_cockpit.py                   # single test file
.venv/bin/pytest tests/test_quant_metrics.py::test_beta_against_self_is_one  # single test
.venv/bin/alembic upgrade head                               # apply migrations
DATABASE_URL="sqlite:///test.db" .venv/bin/alembic upgrade head  # test migration on fresh sqlite
.venv/bin/ruff check app/                                    # lint
uv pip sync requirements.lock                                # reproducible install from lock file (uses .venv in current directory)
```


### Frontend
```bash
cd frontend
npm install
npm run dev      # Vite dev server (port 5173)
npm run build    # TypeScript + Vite production build (use to verify TS)
```

## Architecture

### Backend structure (`backend/app/`)

ADR 0015 Phase 5 restructured `backend/app/` into a **four-layer tree**. Each layer may import only the layers below it; the order is enforced by the `Global layering` import-linter contract (see *Architecture enforcement* below). Every layer has a top-level `CONTEXT.md` documenting its mandate, public surface, and seeded exceptions.

```
app/
├── interface/   FastAPI transport            (may import decision, lab, foundation)
├── decision/    live recommendation loop     (may import lab, foundation)
├── lab/         research & measurement       (may import foundation)
├── foundation/  infra, data, shared kernels  (imports nothing above it)
├── main.py      process entrypoint  ─┐ deliberately outside the layers contract:
└── worker.py    process entrypoint  ─┘ both import across all four layers by design
```

- **`interface/`** — `api/` holds the FastAPI routers, registered in `api/__init__.py` → `routers` list included by `main.py`. To add a domain: create `app/interface/api/mymodule.py` with `router = APIRouter(prefix="/api/mymodule")`, then append it to `api/__init__.py`. `api/quant/` groups the portfolio quant endpoints. `interface/envelope.py` is the response-envelope wrapper (do not confuse it with the unrelated budget-domain `foundation/envelope.py`). **Nothing below this layer may import it — that contract has zero ignore entries.**
- **`decision/`** — the live recommendation/execution loop and its own post-hoc scorekeeping. Sub-packages: `advisor`, `discover`, `graduation`, `llm_portfolio`, `portfolio_advisor`, `verification`, `recommendation_engine`, `engine/` (paper-trade execution). Flat modules: `paper_portfolio.py` (paper runs in EUR, seeded at market value; dividends credited through `paper_cash_flows`; a reset archives the run into `paper_portfolio_archives`; the same start held in MSCI World EUR is each run's passive benchmark), `regime_advisor.py` (MWU strategy reweighting — *not* the `lab/regime/` package), `ai.py`. Key entry points: `discover/pipeline.py` + `discover/dossier_writer.py` (recommendation pipeline & dossier generation). Six facade-only contracts require cross-context consumers to import the package root, not internal submodules.
- **`lab/`** — research/backtest/signal-search/measurement harness producing gated evidence and candidates. Sub-packages: `alphacrafter` (LLM-assisted factor search), `quant_lab` (custom backtest engine, trial ledger, CSCV/PBO), `backtest_vbt` (vectorbt harness, test oracle only), `quant_ml`, `quant_rl` (frozen), `regime`, `attribution`, `performance_ledger`. A `lab → decision` import is forbidden outright.
- **`foundation/`** — base infra, persistence, external data access, shared numerical/narration kernels.
  - `core/` — regular package. `db_base.py` holds ONLY the zero-dependency `Base`; `db.py` owns engine/SessionLocal side effects and re-exports Base; `security.py:SecretBox` — encrypt/decrypt secrets, always use this for credential storage; never roll custom crypto.
  - `models/entities/` — SQLAlchemy 2.0 `Mapped`-style model modules split by domain. Entities inherit `Base` from **`app.foundation.core.db_base`**. `entities/__init__.py` re-exports all symbols, so `from app.foundation.models.entities import X` works. Use `uuid_pk()` and `now_utc()` helpers; foreign keys use `ondelete="CASCADE"`.
  - `schemas/` — shared Pydantic request/response models.
  - Sub-packages: `portfolio`, `dkb`, `imports`, `tax_calc`, `providers`, `data_backbone`, `data_engineering`, `quant_mc`, `finagent`, `llm`.
  - Key flat modules: `dkb/adapter.py` (FinTS only, `DkbFinTSAdapter`), `tax_cockpit.py` (German tax estimates orchestrator), `expected_return.py` (unified return-anchor selector), `fund_class.py` (unified evidence-based German fund classifier — §20 InvStG Teilfreistellung), `market.py` (price history with bar/PriceCache layering + `latest_cached_close`), `quant.py` (VaR/frontier/MC), `quant_optim.py` (skfolio-powered optimisers), `quant_metrics.py` (Sortino/Calmar/CVaR/beta/alpha + `compute_mincer_zarnowitz` forecast calibration), `quant_proposal.py` (MC/skfolio trade-proposal builder), `settings.py` (public + encrypted secrets), `jobs.py` (**generic scheduler infra only** — `_track_job`, submit/cancel/status, cron/interval registration helpers; domain job registrars live in their owning context as `<context>/jobs.py`, one differently-named `register_<job>_job(scheduler)` function per job (e.g. `register_advisor_cycle_job`, `register_discovery_resolution_job`, `register_llm_portfolio_review_jobs`, `register_regime_daily_job` — 16 total across `advisor`, `alphacrafter`, `discover`, `llm_portfolio`, `regime`, `verification`); foundation-gated ones sit in `worker.py`), `telegram_bot.py` (leaf channel adapter) + `telegram_replies.py` (reply formatters above envelope/budget) + `telegram_updates.py` (the one inbound-update handler shared by the webhook and the worker's `getUpdates` polling job; never log a Bot API URL, the token is in it), `setup_token.py` (one-time token the first registration must present; see `docs/deployment.md`), `eur_prices.py` (every close restated in EUR via `ListingCurrency`; FX carried at most 5 days; `benchmark_ticker` defaults to `EUNL.DE`, MSCI World EUR) + `ecb_fx.py` (ECB reference rates by date, for tax conversions of foreign-currency trades), `book_performance.py` (time-weighted unit value and IRR of the real book from `book_position_snapshots`, one row per broker position per day), `allocation.py` (covariance-only targets with Ledoit-Wolf, ±5 pp band rebalancing) + `wealth_planner.py` (the real-terms projection), `recommendation_execution.py` (broker links for a recommendation; after each sync an accepted one is marked `executed` when the position moved), `live_positions.py` (synced broker positions from DKB and Scalable: `live_positions` per depot, `combined_positions` per ISIN, `synced_cash`; every real-book reader goes through it, never `DkbPosition`/`BrokerPosition` directly — `tests/test_live_positions_readers.py` keeps the list of direct readers from growing) + `broker_status.py` (Scalable connection status without running `sc`; the CLI adapter itself is the `scalable/` sub-package, see `docs/scalable.md`). AlphaCrafter runs unconditionally (the former `experimental_features_enabled` gate was retired end-to-end; rollback = git revert).
- **`alembic/versions/`** — Migrations use defensive `if not inspector.has_table(...)` guards.

One cross-layer exception is deliberate and seeded on the contract ledger, documented in `app/lab/CONTEXT.md`: `lab/regime/` is read by three `foundation` modules via lazy, function-local imports (`portfolio_analysis.py`, `research.py`, `llm_research.py`). `regime` is lab-tier because it imports `quant_ml.registry` at module level.

### Settings & secrets pattern
External endpoint resolvers (LLM, OpenBB, Obsidian, frontend origins) follow: **DB AppSetting → env var → built-in default**, implemented in `foundation/settings.py`. The Control Center "Test connection" performs real per-service probes.

```python
# Public settings (stored in app_settings table, visible in Control Center):
from app.foundation.settings import get_public_settings, upsert_public_settings
settings = get_public_settings(db)
upsert_public_settings(db, {"my_key": "value"})

# Encrypted secrets (stored in api_keys table, never logged):
from app.foundation.settings import get_secret, set_secret, update_secret_meta
set_secret(db, "dkb", password, meta={"username": user})   # service must be in SENSITIVE_INTEGRATIONS
secret, meta = get_secret(db, "dkb")
update_secret_meta(db, "dkb", {"extra_key": "val"})        # update meta without touching value
```
DKB credentials (PIN encrypted, username in meta) are stored under the `"dkb"` ApiKey entry.

**Control Center catalog.** `foundation/settings_catalog.py` is what the Control Center renders: every public key and credential sits on one of the pages in `PAGES` (`group`), under a `section`, with a plain-language `label`/`help`, optional `unit` (`fraction_pct` = stored as a fraction, shown as %), `option_labels`, and `advanced=True` for rarely touched keys. `CONNECTIONS` bundles a credential with the settings of the same service (rendered in one drawer). A new setting needs a `DEFAULT_PUBLIC_SETTINGS` entry **and** a catalog entry on an existing page — `tests/test_settings.py` enforces both. A connection meta field marked `sensitive` (Alpaca's secret key) is packed into the encrypted credential by `set_secret` and unpacked by `get_secret`; never store a credential in `meta_json`. `ENCRYPTED_META_KEYS` in the catalog lists meta stored the same encrypted way without the masked UI field (DKB `username`, Telegram `webhook_secret`); a plaintext copy is folded into the encrypted value on first `get_secret`. Internal state kept in `app_settings` (`worker_heartbeat_json`, `connection_tests_json`, `risk_free_rate_*`, `telegram_update_offset`) has no catalog entry and is written only through `upsert_public_settings` by its owning code — `PUT /api/settings` rejects keys outside `DEFAULT_PUBLIC_SETTINGS`.

### Migration pattern
```python
def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("my_table"):
        op.create_table("my_table", ...)
```
Always guard with `has_table`. Set `down_revision` to the previous migration ID.

**Revision ID length limit:** Alembic's `alembic_version.version_num` column is `VARCHAR(32)` by default. Revision IDs must be ≤32 characters. Use compact slugs (e.g. `0037_audit_log_user_nullable`, not `0037_make_audit_log_user_id_nullable`).

### Frontend routing
Routing uses **react-router-dom** defined in `frontend/src/lib/router.tsx` via `createBrowserRouter`, with page components lazy-loaded via dynamic imports.
`frontend/src/lib/store.ts` (Zustand) holds only **UI state** (e.g. viewMode, density, searchOpen) — not navigation.
To add a page:
1. Create `frontend/src/pages/MyPage.tsx` and export a component.
2. Add a `RouteObject` in `frontend/src/lib/router.tsx` using the helper functions `lazyRoute()` (for default exports) or `lazyNamed()` (for named exports), and add `handle: { title: "Page Title" }`:
   ```typescript
   { path: "/my-page", ...lazyNamed(() => import("../pages/MyPage"), "MyPage"), handle: { title: "Page Title" } }
   ```
3. Add the destination to `frontend/src/lib/routeManifest.ts` — the one list that feeds the sidebar, the phone "More" sheet, the bottom nav (`mobile: { slot, label }`), the command palette and breadcrumbs:
   ```typescript
   { id: "my-page", to: "/my-page", label: "My Page", icon: SomeLucideIcon, group: "money" }
   ```
   Optional: `end` (exact match), `match(pathname)` (an entry that owns several path trees), `researchTool: true` (collapsed behind the sidebar's "Show research tools" switch), `sidebar: false` (palette/bottom nav only), `keywords` (palette). A page that is only a tab or detail of an entry goes in `EXTRA_PAGES` (palette only). Tab rows use `components/composed/TabNav.tsx` (router children or `?tab=`).
4. Titles and the h1: give every route a `handle.title` (children included; it becomes `document.title`). `PageHeader` owns the page's one h1 (`level={2}` when nested under a layout that already renders one). A page with no `PageHeader` sets `handle: { noPageHeader: true }` and the shell supplies a visually hidden h1.

### API client (frontend)
```typescript
import { api } from "../lib/api";
const data = await api<MyResponseType>("/api/mymodule/endpoint");
const result = await api<T>("/api/x", { method: "POST", body: JSON.stringify(payload) });
```
All calls include `credentials: "include"` for cookie-based auth. Add new response types as exported TypeScript interfaces in `api.ts`.

### Database in tests
Each test file defines its own `_memory_db()` helper (SQLite in-memory):
```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.foundation.core.db import Base

def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()
```

### DKB integration strategy
**Single adapter: FinTS** (`foundation/dkb/adapter.py:DkbFinTSAdapter`) — read-only AIS, no payment initiation.
- Endpoint: `https://fints.dkb.de/fints`, BLZ `12030000` (both configurable in Settings).
- **No FinTS product ID registration required** — DKB does not validate it. The built-in default (`DKB_PUBLIC_TEST_PRODUCT_ID`) works for all users. fints (python-fints) v4+ enforces a product ID but any value works.
- SCA: DKB-App decoupled push TAN only. chipTAN/photoTAN/smsTAN raises `DkbFinTSManualTanRequired`.
- `fints` (PyPI: python-fints) is pinned to `>=5.0,<6` — the upstream maintainer noted limited maintenance capacity; the version cap insulates against a future breaking major.
- Use `POST /api/dkb/diagnostics/fints/selftest` to verify credentials and TAN mechanism discovery before a full sync.
- Enable **debug FinTS wire logging** in Control Center → Banks & brokers → Advanced settings to log raw FinTS messages at DEBUG level for diagnosing connection issues.

`DkbSyncLog`, `DkbDiagnosticRun`, and `ProviderHealth` provide observability.

### Tax cockpit
`foundation/tax_cockpit.py` orchestrates `foundation/tax_calc/` pure functions. All outputs carry `estimate: True` and `not_tax_advice: True`. Annual maintenance required:
- **Basiszins**: update `BASISZINS_BY_YEAR` in `foundation/tax_calc/jurisdictions/de/vorabpauschale.py:14` each January from BMF publication (§18 InvStG 2018). `foundation/tax_calc/vorabpauschale.py` is a backward-compat shim (`from .jurisdictions.de.vorabpauschale import *`) — do not edit it directly; a future `BASISZINS_BY_YEAR = {...}` reassignment there would create a disconnected local shadow copy.
- **Grundfreibetrag / Familienversicherung limit**: update `GRUNDFREIBETRAG_BY_YEAR` and `FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR` in `foundation/tax_calc/jurisdictions/de/gain_harvest.py` each year (they size the NV-certificate gain-harvest room; see `docs/tax-cockpit.md`).
- **Income-tax tariff (§ 32a EStG)**: add the year's zone boundaries and coefficients to `TARIFF_BY_YEAR` in `foundation/tax_calc/jurisdictions/de/income_tax.py` each January (the Günstigerprüfung compares the flat 25 % with it; a missing year uses the latest known tariff and is flagged as assumed).
- Teilfreistellung rates (§20 InvStG 2018) are statutory: aktien 30%, misch 15%, immobilien 60%.

### Quant
`foundation/quant.py` (existing — VaR, Monte Carlo, efficient frontier, factor exposures) is extended by `foundation/quant_metrics.py` (Sortino, Calmar, CVaR, beta/alpha/R²/Treynor, drawdown duration, skew, kurtosis). Skfolio-powered optimisation lives in `foundation/quant_optim.py`. These are flat foundation modules (not sub-packages); the portfolio API endpoints under `interface/api/quant/` import from all three. Do not rewrite `quant.py`; extend in place or in `quant_metrics.py`. Consolidation and convention decisions for these modules (canonical empirical VaR, Monte Carlo engine, Black-Litterman, deflated Sharpe Ratio, annualisation parameters, backtest-engine dispositions) are governed by `docs/adr/0003-quant-metrics-conventions.md`; read it before consolidating or adding any quant metric implementation.

### Neural Networks (quant_ml)
PyTorch is available as a dependency and used in `lab/quant_ml/` for neural network-based prediction models. Current state:
- **Active usage**: The `train_pipeline` and `walk_forward_cv` functions support gradient boosting (LightGBM, XGBoost) and a neural network MLP via scikit-learn's `MLPClassifier`. PyTorch is declared as a dependency but not directly used by current quant_ml code.
- **Limitations**: No GPU acceleration (CPU-only), limited to small-to-medium datasets, no distributed training
- **Future plans**: Potential integration with `quant_rl` for reinforcement learning agents, and expansion of `quant_ml` pipelines for time-series forecasting with PyTorch
- **Dependencies**: scikit-learn, lightgbm, xgboost, torch (all CPU-only)

## Safety constraints

- **Read-only DKB across all adapters.** No payment initiation (PIS) endpoints. No automated SCA flows.
- **Read-only Scalable Capital.** The `sc` CLI runs only through the root-owned `quantfolio-sc-ro` wrapper with a read-only login and `allowed_isins = []`; never add an order, savings-plan or any other write command to `ALLOWED_COMMANDS` (see `docs/scalable.md`). The only non-read argv anywhere is the Control Center login, exactly `login --local-read-only` (`scalable/login.py` and the wrapper); never widen it (no plain `login`, no other flag).
- **Lean integration removed** (was local-backtest-only); live trading remains prohibited.
- **Tax outputs must always carry `estimate: True` and `not_tax_advice: True`** in API responses. Broker statements are the source of truth.
- **FinTS SCA is always user-initiated** via DKB-App push TAN. The app never proxies DKB credentials.

## Test generation

When generating new test files, always use this boilerplate:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.foundation.core.db import Base

def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()
```

Rules: never mock the DB layer (use `_memory_db()`), create a fresh session per test, mock only external network calls (yfinance, finnhub, etc.), assert `estimate=True` and `not_tax_advice=True` on all tax outputs.

## Automations

This project is set up with several automations for Claude Code and OpenCode. Here's a reference to what's available:

### MCP Servers (`.mcp.json`)

| Server | Purpose |
|--------|---------|
| **context7** | Live documentation lookup for libraries (FastAPI, SQLAlchemy, React, PyTorch, etc.) |
| **alphaxiv** | Research paper discovery, full-text analysis, code repository inspection, and citation search via alphaXiv API |
| **github** | GitHub issues, PRs, commits, search operations |
| **postgres** | Direct PostgreSQL querying (reads DB schema, runs queries) |
| **playwright** | Browser automation for UI testing and visual QA |
| **memory** | Cross-session context persistence for architecture knowledge |
| **docker** | Container management (logs, restart, exec) for deployment debugging |
| **sentry** (optional) | Error investigation — requires `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` env vars |

### Hooks (`.claude/settings.json`)

All hooks auto-detect the project root via `git rev-parse` — works on any machine.

| When | What | Trigger |
|------|------|---------|
| PostToolUse | Ruff lint & auto-fix | Editing `.py` files |
| PostToolUse | TypeScript type-check | Editing `.ts`/`.tsx` files |
| PostToolUse | Alembic upgrade dry-run | Editing migration files |
| PostToolUse | Run related `pytest` | Editing `app/{foundation,lab,decision,interface}/*.py` or `tests/*.py` |
| PostToolUse | Run related `vitest` | Editing frontend `.tsx` component files |
| PreToolUse | Block `.env`/secrets edits | Editing sensitive files |

### Subagents (`.claude/agents/`)

| Agent | Invoke with | Purpose |
|-------|-------------|---------|
| `migration-validator` | `@migration-validator` | Reviews Alembic migrations for safety |
| `security-reviewer` | `@security-reviewer` | Audits auth/DKB/secrets code |
| `test-writer` | `@test-writer` | Generates pytest files following project conventions |
| `ci-runner` | `@ci-runner` | Runs backend + frontend test suites, reports failures |
| `code-reviewer` | `@code-reviewer` | Reviews code for correctness, architecture, security, and performance |
| `api-documenter` | `@api-documenter` | Generates structured endpoint references from FastAPI routers |

### Skills (`.claude/skills/`)

| Skill | Invoke via `/` | Purpose |
|-------|----------------|---------|
| `create-migration` | `/create-migration` | Generate a new Alembic migration with guards |
| `gen-test` | `/gen-test` | Generate pytest test file for a service/API module |
| `new-api-router` | `/new-api-router` | Add a new FastAPI domain router (3-file pattern) |
| `deployment-check` | `/deployment-check` | Pre-flight validation of multi-LXC Proxmox deployment |
| `performance-audit` | `/performance-audit` | Profile quant/ML endpoint latency and resource usage |
| `setup-dev` | `/setup-dev` | Check prerequisites and set up dev environment from scratch |
| `pr-check` | `/pr-check` | Run lint, type-check, tests, migration dry-run, and security review pre-PR |
| `alphaxiv` | `/alphaxiv` | Search and analyze quant finance, ML, and portfolio optimization papers on alphaXiv |

### CI Pipeline (`.github/workflows/`)

| Workflow | Trigger | What it does |
|----------|---------|-------------|
| `ci.yml` | PR to main/master | Ruff lint, Alembic migration dry-run, pytest (backend), tsc type-check, vitest (frontend), production build |
| `pullfrog.yml` | Manual dispatch | AI agent runner for review/automation tasks |

#### Running the full gate locally

GitHub Actions minutes are exhausted, so **CI does not run on push**. Every gate
must be reproduced locally before trusting a branch. The full set, in the order
CI would run it:

```bash
cd backend
.venv/bin/ruff check app/                 # lint (app/ only — tests/ is not linted)
PYRIGHT_PYTHON_FORCE_VERSION=1.1.414 .venv/bin/pyright app/   # type-check (CI's type-check-backend; pin = the version CI resolved, bump it with CI)
.venv/bin/lint-imports                    # 10 import-linter architecture contracts
.venv/bin/python scripts/check_no_new_cycles.py   # import-cycle golden ledger
DATABASE_URL="sqlite:////tmp/mig.db" .venv/bin/alembic upgrade head   # migration dry-run
DATABASE_URL="sqlite:///:memory:" .venv/bin/python scripts/export_openapi.py   && git diff --exit-code docs/api/openapi.json                       # API contract gate
.venv/bin/pytest -n 6                     # ~4000 tests, ~10 min

cd ../frontend && npm run build && npx vitest run
```

Also exercise the **downgrade** path when a migration changes — `alembic upgrade
head` alone will not catch a broken `downgrade()`, and the autogenerated ones
have shipped broken more than once:

```bash
cd backend && export DATABASE_URL="sqlite:////tmp/dg.db" && rm -f /tmp/dg.db
.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade 0105_add_fk_indexes \
  && .venv/bin/alembic upgrade head
```

Two migration traps that have both bitten already: `op.get_bind()` is **already
inside a transaction** (env.py opens it), so `bind.begin()` raises
`InvalidRequestError` — call `bind.execute(sa.text(...))` directly; and
autogenerated downgrades emit SQLite-flavoured `sa.DATETIME()`, which is **not a
valid Postgres type** — use `sa.DateTime(timezone=True)` in any `sa.Column(...)`.

#### Tests that reach the network

Two data sources are fetched live and memoised to machine-global paths, so a
test touching them passes or fails depending on cache state rather than on the
code. Both must be stubbed in deterministic/golden tests:

| Source | Where | Cache |
|---|---|---|
| Ken French Fama-French + momentum factors | `quant_factors._load_ff3` / `_load_momentum` | `/tmp/quantfolio_ff_cache` (override: `QUANT_FACTORS_FF_CACHE_DIR`) |
| Price history providers | `foundation/market.py:history` | DB `PriceCache` |

`tests/test_verification_risk.py` stubs the provider boundary (`market.history`)
and keeps everything above it real. The old `deterministic_factor_data` /
`deterministic_market_history` fixtures went away with verification's style-tilt
attribution (2026-10); the rule still stands for any new test that reaches
`quant_factors`: before that stub existed, style tilts silently read all-zero on
a cold machine and real betas on a warm one, and the goldens were captured cold
— so the parity test flipped on cache state alone.

## Dependency watch-list

The following dependencies warrant attention upstream (maintenance risk, dormancy, or recent reactivation). Consider replacing them in a future refactor where noted:

| Package | Status | Suggested replacement |
|---------|--------|----------------------|
| `vectorbt` (OSS) | ACTIVE again upstream (0.28.5 Mar 2026 → 1.0.0 Apr 2026 → 1.1.0 Jul 2026 per PyPI); re-evaluate before any migration away | None needed now; if ever required: `bt` (backtest) + `numpy`/`pandas` |
| `finrl` | Dormant upstream (last major release ~2022, 0.3.7); `FINRL_ENABLED` now defaults `True` (2026-08-27) — real training verified end-to-end (PPO, both envs). Two of finrl's own transitive deps (`alpaca_trade_api`, `wrds`) are **vendored stubs**, not real installs (`backend/alpaca_trade_api/`, `backend/wrds/__init__.py`) — the real `alpaca-trade-api` hard-pins `websockets<11`/`urllib3<2`, which breaks `yfinance` (needs `websockets>=13`) app-wide; `wrds` pins `pandas<2.3`, an unjustified downgrade for a data vendor this app never uses. `exchange_calendars`/`stockstats` (finrl's two other eager, otherwise-unused transitive deps) are real, safe installs. `quant_rl/envs.py` also carries two finrl/yfinance-version-drift compatibility shims: a monkeypatch-free `market.history()`-based OHLCV fetch (replaces finrl's own `YahooDownloader`, which is unconditionally broken against modern yfinance — removed `proxy` kwarg, changed MultiIndex return shape) and a `results/` dir creation (finrl's envs hardcode `plt.savefig("results/...")` relative to cwd on their terminal step) | Evaluate gymnasium-native envs or stable-baselines3 custom policies; re-evaluate the vendored stubs if finrl ever ships a release that drops the alpaca_trade_api/wrds eager imports |
| `fints` (python-fints) | Limited maintenance capacity; version-capped to `<6` | Monitor upstream; evaluate alternative FinTS libs |
| `plotly` | Version-capped to `<7`: vectorbt's bundled dark/light chart templates reference `scattermapbox`, which plotly 7 removed (`scattermap` replacement) — any vectorbt figure crashes under 7 | Revisit when vectorbt ships plotly-7-compatible templates; then migrate templates and lift the cap |
| `jumpmodels` (github.com/Yizhan-Oliver-Shu/jump-models, PyPI 0.1.1 Oct 2024) | Dormant since Jan 2025. **Adopted 2026-09-28** as the live regime model (ADR 0004 amendment): a main dependency pinned `==0.1.1`, loaded lazily. The classifier falls back to the HMM when it is missing. Uses `predict_online()` and the internal `do_E_step(..., return_value_mx=True)` | Vendor the ~300 lines of `jump.py`/`base.py` if a pinned install ever breaks |
| `duckdb` | ACTIVE; added 2026-09-23 (`>=1.5,<2`) for the PIT-join predicate-pushdown path (`foundation/data_engineering/_pit_duckdb.py`); `memory_limit` and `threads` are always set explicitly (DuckDB defaults to 80% of host RAM, which is wrong in a shared container) | None needed; monitor for major releases (Track B's insider trailing-window range join also runs on it since 2026-09-24) |
| `pandas` | Version-capped to `<3` (2026-09-05): pandas 3.0 changed the default datetime unit from `ns` to `s`, which broke `vectorbt` trade counting (`trades >= 1` assertions returned 0) and caused `pandas.errors.MergeError` on dtype-mismatched merge keys elsewhere; CI's `pip install -e ".[dev]"` has no lock file for that job, so it silently resolved 3.0.5 while the pinned `requirements.lock` (used everywhere else) stayed on 2.3.3 — a floating-version drift, not a deliberate upgrade | Revisit once vectorbt and the rest of the stack are verified against pandas 3.x; then lift the cap deliberately |
| `sqlalchemy` | Version-capped to `<2.1` (2026-09-25): CI's `pip install -e ".[dev]"` resolved the new 2.1.1 while `requirements.lock` and prod stay on 2.0.50, and 2.1's stricter typing (`Row` indexing, `order_by` argument types, nullable mapped columns) made `pyright app/` report 29 errors in untouched code — the same floating-version drift as pandas 3 | Lift the cap deliberately: bump the lock, fix the typing, run the full suite |

*Statuses cross-checked against PyPI/GitHub release history in Aug 2026; see `docs/archive/audits/2026-08-comprehensive-audit.md` §15.2 for the actionable deltas behind these corrections.*

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

All five canonical triage labels use the default strings. See `docs/agents/triage-labels.md`.

### Domain docs

Layout convention documented in `docs/agents/domain.md`; architecture decision records live under `docs/adr/`.

### API contract gate

`docs/api/openapi.json` is the committed API-contract snapshot. CI's `api-contract` job regenerates it from HEAD (`backend/scripts/export_openapi.py`) and runs oasdiff against the committed version, failing on breaking changes (ERR severity only; warnings/info are advisory).

**Ritual after any endpoint or response-model change:**

```bash
cd backend && DATABASE_URL="sqlite:///:memory:" .venv/bin/python scripts/export_openapi.py
git add docs/api/openapi.json   # review the diff — it IS the contract
```

If the gate fails, the change breaks existing clients: widen models instead of shrinking payloads (add optional fields, never remove/retype), or bump deliberately and call it out in the PR description with the snapshot diff attached.

Related Wave 0 gates: import-linter contracts + `scripts/check_no_new_cycles.py` golden ledger (see `backend/pyproject.toml [tool.importlinter]` and the ledger comments there).

### Architecture enforcement (post ADR 0015 Phase 5, 2026-09)

`backend/pyproject.toml [tool.importlinter]` enforces **10 contracts**, all CI-gated — new violations fail PRs; seeded debt may only shrink (`unmatched_ignore_imports_alerting = warn`):

| Contract | Rule |
|---|---|
| Global layering (`type = layers`) | `app.interface > app.decision > app.lab > app.foundation`. Subsumes the retired "Foundation services never import the decision loop" contract. Three seeded ignores: the three lazy `foundation → lab.regime` reads |
| Services never import the interface layer | `app.{foundation,lab,decision} → app.interface` forbidden, **zero ignore entries** |
| Decision-loop packages are independent | 7 members (`decision.{advisor,discover,graduation,llm_portfolio,paper_portfolio,portfolio_advisor}` + `foundation.portfolio`); member↔member chains banned except ledger-documented seams |
| Core never imports the models layer | `app.foundation.core → app.foundation.models`; single documented exception: `app.foundation.core.db -> app.foundation.models.entities` (create_all bootstrap) |
| Facade-only ×6 | decision-loop internals (advisor/discover/graduation/llm_portfolio/recommendation_engine/verification) importable only via package root from other contexts |

`app/main.py` and `app/worker.py` are process entrypoints at the `app` root, siblings of the four layers, and are deliberately outside the layers contract — both import across all four by design.

Additional invariants:
- **Import cycles**: `scripts/check_no_new_cycles.py` compares module/package SCCs against `scripts/import_cycles_golden.json` — zero module cycles exist; the golden may only shrink (regenerate deliberately with `--regenerate` when a refactor legitimately removes cycles).
- **Facades**: cross-context imports go through context `__init__.py` exports or documented public modules. Each context's `CONTEXT.md` documents responsibility, public surface, collaborators, and contract invariants.
- **Scheduler**: `foundation/jobs.py` is generic infra only. Domain job registrars live at `<context>/jobs.py`, one differently-named `register_<job>_job(scheduler)` function per job (e.g. `register_advisor_cycle_job`, `register_discovery_resolution_job`, `register_llm_portfolio_review_jobs`, `register_regime_daily_job` — 16 total across `advisor`, `alphacrafter`, `discover`, `llm_portfolio`, `regime`, `verification`); foundation-gated ones (`register_provider_health_probe_job`, `register_macro_refresh_job`, `register_risk_free_rate_refresh_job`, and others) sit in `worker.py`. Registration shape is pinned by `tests/test_jobs_registration_snapshot.py`.
- **API contract**: `docs/api/openapi.json` snapshot + oasdiff gate above — wire changes must be deliberate.
