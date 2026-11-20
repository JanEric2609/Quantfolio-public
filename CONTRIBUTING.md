# Contributing

Thanks for taking a look. Quantfolio is a personal project published as-is, so answers can take a few days, but bug reports, fixes and well-argued ideas are welcome.

## Before you start

- **Bugs:** open an issue with the bug template. Never paste real account data, IBANs, tokens, API keys or broker exports into an issue; replace them with made-up values.
- **Security problems:** do not open an issue. Report them privately as [SECURITY.md](SECURITY.md) describes.
- **Larger changes:** open an issue first. Decisions that shape the app are written down as ADRs in [docs/adr/](docs/adr/); a change that contradicts one needs a new ADR.

## Ground rules (not negotiable)

- **Read-only at every bank and broker.** No payment initiation at DKB, no order or write command for Scalable (`ALLOWED_COMMANDS` and the root-owned wrapper), only GET requests to the fixed `ALLOWED_PATHS` for Trading 212. A pull request that widens any of these is closed.
- **No trading on anyone's behalf.** Recommendations record a decision; the user places the order.
- **Tax outputs are estimates.** Every tax response carries `estimate: true` and `not_tax_advice: true`.
- **Secrets** go through `foundation/settings.py` (`set_secret`/`get_secret`, encrypted with Fernet). Never log them, never store them in plain settings.
- **Errors sent to the browser** say what failed, not the exception's text (`foundation/public_errors.py`).

## Setup

See the quick start in the [README](README.md). Development runs on SQLite; nothing needs a bank account.

## Checks a pull request must pass

CI runs these; run them locally first (the full list and their order is in [AGENTS.md](AGENTS.md#running-the-full-gate-locally)):

```bash
cd backend
.venv/bin/ruff check app/
.venv/bin/pyright app/
.venv/bin/lint-imports                               # architecture contracts
.venv/bin/python scripts/check_no_new_cycles.py
.venv/bin/python scripts/check_broad_excepts.py
.venv/bin/pytest -n 6

cd ../frontend
npm run build && npx vitest run
```

- **New endpoint or response model:** regenerate `docs/api/openapi.json` (`DATABASE_URL="sqlite:///:memory:" .venv/bin/python scripts/export_openapi.py`) and commit it; breaking changes fail the contract check.
- **Migration:** guard with `has_table`, keep the revision ID at 32 characters or fewer, and test `upgrade`, `downgrade` and `upgrade` again.
- **Tests** use an in-memory SQLite database (`_memory_db()`), never mock the database layer, and never reach the network.

## Style

- Python: type hints, `ruff`, and a docstring that says *why* when the code alone cannot.
- Commits and pull requests: say what changed and why, and how you tested it.
- Write plainly; the app explains money to people, and so do its docs.

## Licence

By contributing you agree that your contribution is published under the repository's [licence](LICENSE).
