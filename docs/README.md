# Documentation

New here? Start with the [README](../README.md) for what Quantfolio is and how to run it. If you are changing code (or are an AI coding assistant), [AGENTS.md](../AGENTS.md) holds the architecture, commands and conventions.

## Features

| Document | What it covers |
|---|---|
| [scalable.md](scalable.md) | Scalable Capital: the read-only `sc` CLI integration, its wrapper and guard, sync, tax events |
| [tax-cockpit.md](tax-cockpit.md) | German capital-gains estimates: FIFO lots, Teilfreistellung, Vorabpauschale, allowances, per-bank status |
| [quant-lab.md](quant-lab.md) | Quant Lab: holdings, risk, optimisation, wealth projection, backtests |
| [research-system.md](research-system.md) | The LLM research pipeline and its sources |
| [alphacrafter-shadow-gates.md](alphacrafter-shadow-gates.md) | Gates an LLM-proposed factor must pass before it counts |

## Operations

| Document | What it covers |
|---|---|
| [deployment.md](deployment.md) | Deploying on Proxmox LXCs (database, app, LLM, web), first-run setup token, updates |
| [runbooks/wrds-jkp-extract.md](runbooks/wrds-jkp-extract.md) | Refreshing the WRDS / JKP factor extract |
| [../infra/runbooks/](../infra/runbooks/) | Backup and restore, rollback, incident playbooks |

Addresses in these documents are placeholders (`<DB_HOST>`, `<APP_HOST>`, `<LLM_HOST>`, `<WEB_HOST>`, `<PROXMOX_HOST>`, `<TAILNET_HOST>`); substitute your own.

## Decisions and reference

| Path | What it is |
|---|---|
| [adr/](adr/) | Architecture Decision Records, numbered. A decision is superseded by a new record, never edited. |
| [api/openapi.json](api/openapi.json) | The committed API contract. Regenerate with `backend/scripts/export_openapi.py`; never edit by hand. |
| [research/](research/) | Literature scans behind some decisions (reading pointers, not implemented work). |
| [agents/](agents/) | Instructions for AI coding assistants (issue tracker, triage labels, domain layout). |
| `backend/app/*/CONTEXT.md` | Per-package mandate, public surface and contracts, next to the code. |

## Archive

[archive/](archive/) holds dated audits, implementation plans, specs and one-off runbooks from 2026-05 to 2026-10. They record what was true and intended at the time; they are not maintained, and the code and the documents above win wherever they disagree. The archive is not part of the public repository.
