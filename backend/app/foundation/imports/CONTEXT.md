# Context: Imports

## Responsibility

Broker CSV upload pipeline: parse uploaded files into candidate
transactions (parse), flag likely duplicates against existing rows
(dedupe), validate shape and required fields (validate), and commit
accepted transactions into the ledger (commit).

## Public surface (facade `app.foundation.imports`)

`commit_import`, `detect_duplicates`, `parse_csv`, `validate_csv`.

## Key collaborators

- In: `app.interface.api.imports`.
- Out: none cross-context.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Not covered by the Phase-F facade-only bans (not a decision-loop context).

## Owner-wave notes

Parsers read quantity, unit price and fees (explicit columns, else labelled text
such as "10 Stück ... Kurs 80,00"; price derived from the cash amount when only a
quantity is known) and `commit_import` stores them on the `ActivityLedgerEntry`,
which is what `tax_cockpit.seed_lots_from_activity` needs to open a lot. The
comdirect / Trade Republic layouts are still the assumed ones (no real exports to
verify against), and `validate_csv` still detects only DKB, so those two formats
are not reachable through `/api/imports/commit` until detection is pinned.

Facade already conformed pre-Phase-F (no changes made). Self-contained
context with no seeded debt.
