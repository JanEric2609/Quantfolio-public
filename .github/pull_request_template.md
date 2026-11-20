## What and why

<!-- What this changes and the reason for it. Link the issue or ADR. -->

## How it was tested

<!-- Commands run and their results; screenshots for UI changes (with made-up data only). -->

## Checklist

- [ ] No real account data, IBANs, tokens, keys or personal information in code, tests, docs or screenshots
- [ ] Banks and brokers stay read-only (no new write call, command or path)
- [ ] `ruff`, `pyright`, `lint-imports`, the cycle and broad-except checks and `pytest` pass
- [ ] `npm run build` and `npx vitest run` pass (frontend changes)
- [ ] `docs/api/openapi.json` regenerated (endpoint or response-model changes)
- [ ] Migration guarded with `has_table`; upgrade → downgrade → upgrade tested (migration changes)
- [ ] Tax outputs keep `estimate: true` and `not_tax_advice: true`
- [ ] Docs updated where behaviour changed
