# Context: DKB

## Responsibility

Read-only DKB broker integration via a single FinTS adapter. Pure helpers
(utils: encoding safety, parsing, log capture, URL safety), data classes and
exception types (models), the FinTS protocol/TAN lifecycle/account reads
(adapter: `DkbFinTSAdapter`), and sync orchestration with persistence
(service: `DkbSyncService`, `dkb_sync_service`). Dividend ingestion routes
to the tax cockpit; positions/holdings land in DKB-scoped tables.

## Public surface (facade `app.foundation.dkb`)

`DKB_PUBLIC_TEST_PRODUCT_ID`, `DkbFinTSAdapter`, `DkbFinTSConnectionRejected`,
`DkbFinTSManualTanRequired`, `DkbFinTSTanTimeout`, `SyncSession`, `SyncState`,
`DkbSyncService`, `dkb_sync_service`, `dkb_transaction_hash`, plus the
pre-existing `_`-prefixed util re-exports (`_ascii_safe`, `_safe_fints_url`,
`_sanitize_fints_error`, …) kept for backward compatibility.

## Key collaborators

- In: `app.interface.api.dkb`.
- Out (all sanctioned independence-ledger seams): `etf_classification`,
  `tax_cockpit` (dividend ingestion), `verification.engine`,
  `portfolio_service`, `portfolio.name_resolver`, `price_backfill`, `jobs`.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence); every
  out-edge above is an explicit ignore row there.
- Safety: read-only AIS across all adapters — no payment initiation, no
  automated SCA; DKB-App decoupled push TAN only, user-initiated.
- Facade already conformed pre-Phase-F (no changes made).

## Owner-wave notes

fints (python-fints) is version-capped `<6` upstream-maintenance insurance
(see AGENTS.md dependency watch-list). Use
`POST /api/dkb/diagnostics/fints/selftest` before full syncs.
