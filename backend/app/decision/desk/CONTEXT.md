# Context: Decision / desk (guarded derivatives desk, ADR 0027)

## Responsibility

Paper-only trading of knock-outs and warrants with real `sc` quotes and fake fills, run by strict
limits, until a pre-registered rule shows an edge after costs. **No order path exists**: no trading
user, no trade wrapper, no `trade` argv; the broker allow-lists stay read-only.

## Modules

| Module | Role |
|---|---|
| `limits.py` | Frozen `LIMITS` (code constants, pinned by a test, part of the pre-registration) |
| `calendar.py` | Trading days, 21:30 Berlin cut-offs, `MACRO_EVENTS_BY_YEAR` (maintain each year) |
| `guard.py` | Pure `check_entry` / `check_exit`; live-only caps vs reasons that stop a paper entry too |
| `sizing.py` | Units and stop from the 5 EUR budget (both fees included) |
| `ledger.py` | Append-only hash-chained `desk_events` (verification lives in `foundation/desk_chain.py`) |
| `products.py`, `underlyings.py` | Search, local filters, per-candidate quotes, ranking; the four index aliases |
| `signals/` | `owner_calls` (`/idea`, tap) and `index_momentum` (frozen rule) |
| `service.py` | Position lifecycle, halt flag (`desk_halt_json`), health (`desk_health_json`), live rehearsal |
| `prereg.py`, `scoring.py` | The pre-registered spec/hash, per-cell bootstrap, placebo, Holm |
| `news.py` | Guard and log only; news never opens or closes a position |
| `newswire.py` | The news wire: Alpaca stream (worker thread), Fed/ECB/SEC 8-K (job `desk_news_wire`), local-model rating, Telegram alerts; alert-only |
| `cfd.py` | The index rule as a manual Trading 212 CFD line, paper-scored in `index_momentum:cfd` |
| `instrument.py` | Names the exact product: issuer name, ISIN/WKN, leverage and barrier or omega and strike, expiry, order line |
| `telegram.py`, `jobs.py` | `/idea /desk /stop /close` + "Paper: take"; 1-minute monitor, hourly reconcile, 1-minute news wire |

## Public surface

The package root (`app.decision.desk`). A facade-only import-linter contract forbids deep imports from
every other context, with zero seeded exceptions.

## Invariants

- Limits are never settings and never raised from the UI or API.
- Every state change is a ledger event in the same transaction as the position update.
- `external` positions (the owner's own app trades) are logged and scored separately, never in a pass-rule cell.
- Phase 2 (real orders) is Proposed in ADR 0027 and not built.
- A CFD is never automated (`would_take_live` is always False); Trading 212 stays read-only.
- Nothing reads `news_wire_items` to open, close or block a position.
