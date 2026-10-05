# 0010 — OpenBB Per-Purpose Routing

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-08-31_implementation_decisions.md`
(Topic 5 of the consolidated implementation plan)
**Consumed by:** `backend/app/services/providers/openbb_provider.py`,
`backend/app/services/providers/registry.py`,
`backend/app/services/providers/rate_limiter.py`,
`backend/app/services/settings.py`

## Context

The audit found `OpenBBProvider` bundled every market-data purpose (quotes,
history, fundamentals, news, analyst estimates, FX, macro) behind one
`openbb_enabled` flag and one position in the provider fallback chain, with
no distinction between "OpenBB as a quote/history fallback" and "OpenBB as
the macro/rates data path." Three specific problems followed from that:

1. **`get_macro_indicators` was a documented near-no-op.** It called
   `/api/v1/economy/calendar` (economic *events*, not indicator time series)
   because the real per-series route needs a `symbol` param the generic
   capability doesn't take — the code comment explicitly says this is
   "intentionally a near-no-op" relying on the registry chain falling
   through to `FredProvider`. So "route macro through OpenBB's fred
   extension" had no real implementation to route to.
2. **Enabling OpenBB for macro would have silently rerouted quotes too.**
   `_DEFAULT_CHAIN` placed `openbb` *before* `yfinance`, and `OpenBBProvider`
   declares `get_quote`/`get_history`/`get_fundamentals` capabilities. Since
   provider selection is chain-order-first-match (`first_success`), turning
   on `openbb_enabled` to get real macro data would have made OpenBB the de
   facto primary source for quotes as a side effect — contradicting "quotes
   stay yfinance."
3. **No client-side usage guardrail existed for OpenBB.**
   `PROVIDER_RATE_LIMITS["openbb"] = []`, commented "throttled server-side by
   OpenBB deployment" — a reasonable stance when OpenBB was disabled by
   default and rarely exercised, less so once it starts doing real
   scheduled-job-adjacent work.

The ECB risk-free-rate fetch (`ecb_provider.py`, ADR 0006) and the
Fama-French factor download (`quant_factors.py`, ADR 0007) were both
recently hardened as direct HTTP/download paths; the plan explicitly keeps
both that way rather than routing them through OpenBB's `ecb` extension or
an OpenBB-mediated Dartmouth fetch.

## Decision

**1. `yfinance` moved ahead of `openbb` in `_DEFAULT_CHAIN`.** Quotes,
history, and fundamentals now resolve via yfinance first regardless of
`openbb_enabled` — OpenBB only sees that traffic as a fallback if yfinance
itself fails, which satisfies "quotes stay yfinance" even once OpenBB is
turned on for macro purposes. `DEFAULT_PUBLIC_SETTINGS["provider_chain_json"]`
and `registry.py`'s `_DEFAULT_CHAIN` were both updated to keep the documented
sync between them; `tests/test_provider_chain.py`'s existing chain-order
test (which had pinned the *previous* ordering, `openbb` immediately before
`yfinance`) was updated to pin the new one instead.

**2. A real fred-extension method, additive not a replacement.**
`OpenBBProvider.get_fred_series(series_id, start=None, end=None)` calls
`/api/v1/economy/fred_series` with `provider="fred"` **forced explicitly**
— not left to `_api_request`'s generic default-provider injection, which
would otherwise silently send `provider=yfinance` (the ODP default for
everything else) to an endpoint yfinance cannot serve at all. This is the
actual "macro/rates via OpenBB's fred extension" path the plan asked for.
`get_macro_indicators` (the calendar near-no-op) is left exactly as-is — it
has its own pinned test and its own documented reason to exist; the new
method is a second, more capable path for callers that want real per-series
values, not a fix-in-place of the old one.

**3. ECB stays fully outside OpenBB — no `get_ecb_rate` equivalent added.**
Per the plan's explicit carve-out, and because `ecb_provider.py`'s direct
SDMX-JSON path was just bug-fixed and test-covered (ADR 0006) — routing it
through OpenBB's `ecb` extension now would both violate the plan's own
instruction and re-introduce exactly the kind of indirection whose failure
mode (silent wrong-shape parsing) triggered ADR 0006 in the first place.

**4. `quant_factors.py` is untouched.** Fama-French Europe factors
(ADR 0007) stay on direct Dartmouth download — no OpenBB routing was added
or considered for it, matching "factors stay direct-download."

**5. No production job was rewired to call OpenBB.** `register_macro_refresh_job`
(`worker.py`) keeps calling `FredProvider` directly via
`DataIngester.ingest_macro_indicators(series_ids=...)`, and
`register_risk_free_rate_refresh_job` keeps calling `EcbProvider` directly.
Both already have documented reasons to bypass generic provider fallover for
their specific series codes (`ingest_macro_indicators`'s own comment).
`get_fred_series` exists as an available, correctly-routed capability for
any future caller that wants OpenBB-mediated FRED data — it was not forced
into the two jobs whose isolation from generic fallover predates this ADR
and is independently justified.

**6. Self-imposed rate-limit guardrail added.** `PROVIDER_RATE_LIMITS["openbb"]`
changes from `[]` to `[(30, 60, "OpenBB self-hosted LXC: 30/min self-imposed
guardrail")]`. Unlike the other entries in that table (published free-tier
API quotas), this isn't rate-limiting against a vendor's terms — it protects
a single self-hosted LXC container from being hammered by scheduled jobs and
interactive fallback traffic combined, now that OpenBB does more than serve
as a rarely-enabled quote fallback. It is enforced automatically by the
existing `first_success` → `create_limiters`/`acquire_all` mechanism
(`registry.py:105-109`) for every OpenBB call, generic-chain or explicit
`get_fred_series`, with no new call-site plumbing needed.

**7. No infrastructure change.** Unused OpenBB extensions stay installed on
the LXC (nothing here uninstalls anything), and the LXC itself is untouched
— this ADR is a routing/software change only.

## Alternatives considered

**Split `OpenBBProvider` into separate quote/macro provider classes with
independent `enabled` flags.** Rejected as more invasive than necessary: the
chain-reorder (Decision 1) achieves "quotes stay yfinance" with a one-line
change and no new class hierarchy, and a single `openbb_enabled` flag
matches how the rest of the app already reasons about "is the OpenBB LXC
reachable" as one yes/no fact.

**Reroute the ECB risk-free-rate job or the FRED macro-refresh job through
OpenBB now that a real fred-extension method exists.** Rejected per the
plan's explicit scope and for the reasons in Decision 5 — both jobs'
isolation from generic provider fallover is deliberate and predates this
ADR; folding them into the newly-capable OpenBB path is a separate decision
this session did not make.

**Leave `PROVIDER_RATE_LIMITS["openbb"]` empty since OpenBB is still disabled
by default.** Rejected: the guardrail should exist before OpenBB use grows
(macro/rates is exactly the kind of use that grows once `get_fred_series`
exists), not be added reactively after a real overload incident on a
personal-scale LXC with no other load-shedding in front of it.

## Consequences

- `_DEFAULT_CHAIN` order changed: `yfinance` now precedes `openbb`. Any
  deployment relying on an explicit `provider_chain_json` override (rather
  than the default) is unaffected — this only changes the fresh-install
  default, consistent with how ADR 0006 treated the `regime_index_symbol`
  default cutover.
- `OpenBBProvider` gains one new public method (`get_fred_series`) and one
  new capability string (`"get_fred_series"`); `ProviderRegistry` gains a
  matching `get_fred_series(series_id, start=None, end=None)` dispatch
  method. Neither is called from any production code path yet — it is
  available infrastructure, not a behavior change for existing jobs.
- OpenBB calls (of any capability) are now throttled client-side at 30/min
  in addition to the existing per-capability dead-provider cooldown.
- Regression coverage:
  `tests/test_openbb_provider.py` (`get_fred_series` endpoint routing, forced
  `provider=fred`, symbol/date-range params, disabled-provider error path,
  capability membership); `tests/test_provider_chain.py` (yfinance-before-openbb
  chain-order assertion, `get_fred_series` registry dispatch, existing
  chain-order test updated to the new pinned order).
