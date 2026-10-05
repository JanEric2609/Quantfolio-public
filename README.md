# Quantfolio

A self-hosted personal finance app for one household in Germany: it reads your accounts at DKB and Scalable Capital, shows the combined book, estimates the German tax on it, plans the monthly contribution, and runs a research loop that has to prove its ideas against a plain MSCI World ETF before it is trusted.

It is a personal project, built for one owner's setup and published as-is. Nothing in it is financial or tax advice.

## What it does

- **One book across brokers.** DKB (FinTS, read-only) and Scalable Capital (the official `sc` CLI behind a read-only wrapper) are synced into one view, with the split per broker, cash, and a time-weighted return against MSCI World in EUR.
- **This month.** How much to contribute and where: target weights from covariance-only allocation (equal, inverse-volatility, minimum variance, risk parity, HRP with a Ledoit-Wolf covariance) and ±5 pp rebalancing bands, so contributions fill the underweights and a sale only happens on a breach.
- **German tax estimates.** FIFO lots per depot, Teilfreistellung by fund class, Vorabpauschale, loss pots, allowances and NV certificates per bank, foreign trades at the ECB rate of their day. Every number is marked as an estimate; the broker's statement is the source of truth.
- **Wealth projection.** Ten thousand paths in today's euros from a published long-run return estimate, with fat tails and uncertainty about the return itself.
- **Ideas, measured.** Discover screens a universe and writes dossiers with a local LLM; an advisor loop and two LLM mandates trade them on paper next to a passive MSCI World book. "Can I trust it?" scores every call after its horizon, with the rebalance date as the unit, exact and Newey-West intervals and anytime-valid evidence corrected for multiple looks (e-BH). Backtests report the deflated Sharpe ratio and the probability of backtest overfitting, and say "insufficient evidence" when that is the answer.
- **Everything else.** A budget with envelopes and subscriptions, a watchlist, news from RSS and providers, a Quant Lab, local LLM chat, Telegram alerts and a Control Center for settings, connections and diagnostics.

## Safety

- **Read-only at every bank and broker.** No payment initiation at DKB; the FinTS TAN is always approved by you in the DKB app. At Scalable, `sc` runs only through a root-owned wrapper with a read-only login and an empty trade allow-list ([docs/scalable.md](docs/scalable.md)).
- **No trading.** Accepting a recommendation records your decision and links to the security at your broker; you place the order yourself, and the next sync notices it.
- **Secrets are encrypted** at rest (Fernet), never logged, and never stored in plain settings.

## Quick start (development)

Requirements: Python 3.11+, Node 22, and optionally Redis. SQLite is fine for development; production uses PostgreSQL 16 with TimescaleDB and pgvector.

```bash
cp .env.example .env          # defaults work for local development

cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload          # http://localhost:8000

cd ../frontend                                    # in a second terminal
npm install
npm run dev                                       # http://localhost:5173
```

The first account you register becomes the admin. It needs the one-time setup token, which the backend logs at WARNING on start ("FIRST-RUN SETUP") and writes to `DATA_DIR/setup_token` (or set `SETUP_TOKEN` in `.env`).

Market data works out of the box through yfinance. The LLM features need an OpenAI-compatible endpoint (llama.cpp locally, or a cloud key in the Control Center); DKB needs your online-banking login; Scalable needs the `sc` CLI on the server. Each degrades on its own when missing.

## Tests and checks

```bash
cd backend
.venv/bin/ruff check app/
.venv/bin/lint-imports                            # the architecture contracts
.venv/bin/pytest -n 6                             # ~4,000 tests

cd ../frontend
npm run build && npx vitest run
```

[AGENTS.md](AGENTS.md) lists the full gate (migration dry-run, import cycles, the OpenAPI contract) and the conventions.

## Layout

```
backend/app/
  interface/    FastAPI routers
  decision/     the live loop: Discover, advisor, mandates, paper portfolios, verification
  lab/          research and measurement: backtest engine, trial ledger, regime model, ML
  foundation/   data, providers, brokers, tax, quant kernels, persistence
frontend/       React, TypeScript, Vite
infra/          Proxmox LXC manifests, systemd units, Caddy, update scripts
docs/           feature and operations docs, ADRs, the API contract
```

Each layer may import only the layers below it; import-linter enforces it. Each package has a `CONTEXT.md` describing its job and public surface.

## Deployment

The reference deployment is four Proxmox LXCs (database, API and scheduler, LLM, web with Caddy), reached over Tailscale for HTTPS and passkeys. See [docs/deployment.md](docs/deployment.md); updates run through `quantfolio-update` on the host.

## Documentation

[docs/README.md](docs/README.md) is the index: Scalable, the tax cockpit, the Quant Lab, research, deployment, runbooks and the architecture decision records.

## Licence

[MIT](LICENSE). Security issues: see [SECURITY.md](SECURITY.md).
