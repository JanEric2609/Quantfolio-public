# 0014 — Analytics Hypertable Writes, Regime Coherence, and Dossier Grounding

**Status:** Accepted (implemented)
**Date:** 2026-09-01
**Context source:** Production forensics on the live deployment (Postgres via
read-only MCP, `journalctl` on LXC 111 via SSH to the Proxmox host), triggered
by a user report that Discover dossiers and the sidebar regime chip were
producing figures a third-party LLM (Gemini) judged fabricated. Four parallel
Haiku mapping agents over `discover/`, `regime/`, `providers/`, and the ETF
metadata modules, plus two research agents (arXiv/web) whose load-bearing
citations were re-fetched and verified directly. Memory:
`project_quantfolio_2026-09-01_analytics_hypertable_regression.md`,
`project_quantfolio_2026-09-01_discover_er_and_data_sources.md`.
**Consumed by:** `backend/alembic/versions/`,
`backend/app/services/data_backbone/ingest.py`,
`backend/app/services/regime/macro_snapshot.py`,
`frontend/src/components/regime/RegimeChip.tsx`,
`backend/app/services/discover/dossier_writer.py`,
`backend/app/services/discover/blm.py`,
`backend/app/services/providers/registry.py`

## Context

Five Discover dossiers (84X0.DE, ERNX.DE, IS3S.DE, XAIX.DE, QDVI.DE) were
reviewed externally and pronounced "structurally hallucinating": every one
carried `Insufficient comparison panel`, `Market Regime: unknown`,
`Snapshot confidence: 50%`, and `ml_signal_unavailable`, while the headline
"Model estimate (Black-Litterman)" read ~+0.0% against a stated
`expected_return_anchor_pct` of 26–33%. One dossier described IS3S.DE — a MSCI
World **Value** Factor ETF — as "structural exposure to undervalued large-cap
growth". The external diagnosis was "a broken data ingestion pipeline,
disconnected from its long-term historical database", with a recommendation to
re-architect around a paid European data vendor.

That diagnosis identified real symptoms and the wrong mechanism in every case.
The actual causes are three, and only the third is about the LLM.

### 1. The analytics hypertables have never accepted a write

`2ce7981ac5c6_recreate_analytics` recreates `bar_prices`,
`factor_loadings_daily`, `regime_snapshots`, and `provider_health_history` under
an `if not inspector.has_table(...)` guard, using plain
`sa.Column("id", sa.BIGINT(), nullable=False)` and non-unique indexes
(`ix_bar_prices_symbol_ts`, line 90). It thereby discards two earlier fixes that
are marked applied and therefore never re-run:

- `0073_hypertable_id_defaults` — attaches identity sequences to the composite-PK
  `id` columns. Its own docstring records the identical failure: *"every INSERT
  that omits id (all of them: DataIngester, BarStore) failed with
  NotNullViolation. bar_prices had 0 rows in production as a result."*
- `0075_bar_prices_unique_index` — creates `uq_bar_prices_symbol_ts` and
  `uq_factor_loadings_symbol_factor_ts`, the targets the ingester's
  `ON CONFLICT (symbol, ts) DO UPDATE` upsert names.

**This is therefore a regression of a bug already diagnosed and fixed once.**

Verified against the live database and logs on 2026-09-01:

| Check | Result |
|---|---|
| `id` column on all four tables | `BIGINT NOT NULL`, `column_default` NULL, no sequence in `information_schema.sequences` |
| `uq_bar_prices_symbol_ts` | absent; only the non-unique `ix_bar_prices_symbol_ts` exists |
| `bar_prices` / `regime_snapshots` / `provider_health_history` / `factor_loadings_daily` | **0 rows each** |
| `price_cache` | 458,730 rows across 315 tickers |
| `quantfolio-api` journal, last 4000 lines | 155× `psycopg2.errors.InvalidColumnReference: there is no unique or exclusion constraint matching the ON CONFLICT specification`; 181× NotNullViolation |
| `alembic_version` | `2ce7981ac5c7_bar_prices_caggs` — at head; 0073 and 0075 both ancestors |

The failure is silent because `DataIngester.ingest_bar_prices` wraps the insert
in a bare `try/except` that records the error to `provider_health_history` —
which is itself one of the four broken tables. **The observability channel and
the thing it observes failed together**, which is why this survived undetected.

`market.history` masks the outage for most callers: it reads `BarStore` first,
finds nothing, and falls through to `PriceCache`, which is healthy.

This single defect produces four of the five disputed caveats:

- **`Insufficient comparison panel`** — `alphacrafter/panel.py:build_panel` reads
  `BarStore` *only*, and `pipeline.py:579` requires ≥3 of
  `{candidate, SPY, URTH, VGK}`. It gets 0, on 100% of candidates, forever.
  Meanwhile `trails URTH by 12.6%/yr` in the same dossier computed correctly,
  because that path uses `market_history` and its `PriceCache` fallback. The two
  disagreeing signals in one dossier are the fingerprint of the split store.
- **`regime: unknown` / `risk-on: no` / `confidence 50%`** — `regime_snapshots`
  is empty, so `discover/regime_gate.py:99-108` returns its unknown default. The
  HMM classifier also reads bars via `BarStore`, so it can neither fit nor
  classify.
- **`ml_signal_unavailable`** — zero `quant_ml_models` rows with
  `status='completed'`; `pipeline.py:1428` returns neutral.

Note what is **not** broken: `macro_indicators` holds 1,937 rows of genuine FRED
data (VIXCLS, T10Y2Y, BAA10Y) current to 2026-08-31. An early hypothesis that
the FRED key was unconfigured was checked and disproved.

Also **not** broken: the `short_history` caveat. It fired on 84X0.DE (data from
2023-11-03) and ERNX.DE (2022-04-29) — both genuinely short of the 5-year
`_HISTORY_REQUEST_DAYS` window — and correctly did *not* fire on
IS3S/QDVI/XAIX. The external review's claim that it fires on every fund
regardless of age is false. The defect is narrative, not arithmetic: the LLM
restates a *data-coverage* flag as a claim about *fund age*.

### 2. The regime chip displays a correct series under the wrong label

`regime/macro_snapshot.py:41-48` computes `^TNX − ^IRX`, i.e. 10-year minus
**3-month**, and its docstring says so explicitly. `RegimeChip.tsx:25` hardcodes
the label `10Y-2Y`. On 2026-09-01 the chip showed `+1.016%`; live `^TNX − ^IRX`
was 1.024, and the true `T10Y2Y` — already present in this app's own
`macro_indicators` table — was **+0.41%**. The number is right; the label is
wrong; and the authoritative series is sitting unread in the database.

Compounding this, the app runs two regime producers that can contradict each
other in the same UI: the rule-based `macro_snapshot.py` behind the chip (live,
working, showing "Bull" at 90% confidence) and the HMM `regime/classifier.py`
behind the dossiers (dead, per §1, showing "unknown" at 50%).

### 3. The LLM is asked for facts it cannot have, and for numbers it cannot know

Two distinct defects, both genuine:

**3a. `er_mode = "bl"` is live in production.** `discover/blm.py:365` asks the
local Qwen3.5-9B-Q4 model, 25 times: *"What is the expected 2-week return for
{symbol}? Answer with a single number."* The mean becomes the Black-Litterman
view `q`; the sample variance becomes the view confidence. When all 25 queries
fail, `blm.py:472` returns `(0.0, 1.0)` silently. This is why every observed
headline reads +0.0%/+0.1% while the thesis text argues about a 26–33% anchor:
the two figures come from disconnected pipelines and the forward one is
collapsing to a failure sentinel that is indistinguishable from a real estimate.

Literature (load-bearing citations re-fetched and verified directly, not taken
from agent summaries):

- **arXiv:2409.11540** (Chen, Green, Gulen & Zhou, 2024) — LLMs "over-extrapolate,
  placing excessive weight on recent performance", and forecasts are optimistic
  relative to realized returns. Asking a model for a return after a 50%+ rally
  is the precise setup this paper warns about.
- **arXiv:2504.14345** (Lee et al., 2025) — the one paper that genuinely drives
  Black-Litterman from LLM output. It translates return forecasts *and explicit
  predictive uncertainty* into views and confidence, and reports that each
  model's idiosyncratic "style" dominates results. Raw point-averaging with
  sampled variance as confidence is a cruder method than the published one.
- **arXiv:2304.07619** (Lopez-Lira & Tang, 2023) — such skill as exists is found
  when the model reasons over *supplied news*, not when queried from parametric
  memory.

The calibration literature is uniformly against reading decoding variance as
confidence; a 4-bit 9B model sampled 25× at temperature 0.2 is measuring
decoding stochasticity, not uncertainty about the world.

**3b. The dossier prompt contains no instrument facts.**
`dossier_writer.py:379-395` passes symbol, name, ISIN, composite scores,
fundamentals and sentiment — and **no holdings, no tracked index, no inception
date, no sector, no factor mandate**. `get_etf_profile()` and
`registry.get_etf_holdings()` exist and work, but are never called from this
path. The "Value ETF described as growth" error is therefore not a broken
connector: the model was asked to name a style given a ticker and a large
trailing return, and it did what the extrapolation literature predicts.

### Data sources: measured, not assumed

The external recommendation was to adopt EODHD (~€20/mo, later ~$60/mo for the
tier that actually carries holdings). Direct testing contradicts the premise
that free sources fail on European instruments:

| Source | Holdings (`.DE`) | Inception date | Equities | Cost |
|---|---|---|---|---|
| `yfinance.funds_data.top_holdings` | **works** (top 10) | unreliable¹ | yes | free |
| Self-hosted OpenBB `etf/info` (LXC 100) | no² | **works** | yes | free, already deployed |
| justETF universe cache | top-10, sector/country, ISIN-keyed | yes | no | free, already refreshed daily |
| EODHD | 403 | 403 | — | ~$60/mo tier required |

¹ Reports 84X0.DE inception 2024-05-10 though prices exist from 2023-11-03.
² `etf/holdings` accepts only `fmp`, `intrinio`, `tmx` — no European coverage.

The configured EODHD key was tested against `/api/fundamentals` for `IS3S.DE`,
`IS3S.XETRA` and `XAIX.XETRA`: all return
`403 "Only EOD data allowed for free users"`. EOD *prices* work on this key;
holdings and fund valuations do not.

Empirically, `yfinance` top-10 holdings for the disputed tickers are
unambiguous: IS3S.DE → Micron, Cisco, Verizon, Toyota, AT&T (Value);
XAIX.DE → Microsoft, Amazon, Apple, NVIDIA, Alphabet (Growth). A deterministic
classifier over these would have labelled all five correctly.

Two further findings constrain the design: `assets` contains **1 row**, so no
instrument metadata is DB-queryable and the justETF universe lives only as a
local JSON file; and `provider_chain_json` excludes `yfinance` while
`openbb_default_provider` is unset, so OpenBB proxies to Yahoo regardless —
Yahoo data arrives laundered through OpenBB today.

### Scope

The Discover universe is not German-ETF-only: 327 `.DE`, 206 unsuffixed (US),
12 `.PA`, plus `.MC/.AS/.MI/.HE/.L`, spanning ETFs and single equities. The
binding user constraint is tradeability, already enforced by
`discover/tradeability.py` (196 candidates rejected as "Not likely tradeable at
DKB"). Any grounding work must therefore be asset-class-generic and keyed on
`instrument_type`, not fund-specific.

## Decision

**1. Restore the analytics hypertable write path (fix forward).**
A new migration re-attaches identity defaults to `id` on all four tables and
recreates the business-key UNIQUE indexes (`uq_bar_prices_symbol_ts`,
`uq_factor_loadings_symbol_factor_ts`), dedupe-first, using
`0075_bar_prices_unique_index` as the template. `bar_prices` is then backfilled
from the 458k healthy `PriceCache` rows.

**2. Make ingest failure loud.** `DataIngester.ingest_bar_prices` must not
swallow a write failure whose only report channel is a table in the same broken
set. Schema-shape errors (`InvalidColumnReference`, `NotNullViolation`) are
logged at ERROR and surfaced in the ingest result, distinctly from ordinary
provider outages.

**3. Pin the invariant with a test.** A regression test asserts, on Postgres,
that each of the four tables has an `id` default and that the business-key
unique indexes exist. Guarded `create_table` in a later migration must never
again silently drop a constraint added by an earlier one.

**4. Correct the regime chip and unify the producers.** The chip reads the real
`T10Y2Y` from `macro_indicators`, labelled `10Y-2Y`; the existing 10Y-3M value
is retained as a separate, correctly-labelled row (it is the stronger recession
indicator, per Estrella–Mishkin, and is worth keeping — but not under another
series' name). Chip and dossier are reconciled onto one regime source so they
cannot contradict.

**5. `er_mode` returns to `"trailing"`, and view failure becomes explicit.**
The headline reverts to the shrunk trailing anchor, which is at least coherent
with the thesis text that discusses it. `blm.py`'s all-queries-failed path must
surface "no estimate" rather than `(0.0, 1.0)`; a sentinel that is
indistinguishable from a genuine near-zero forecast is the specific defect. The
BL code is retained but dormant, pending a grounded view source (§Consequences).

**6. Ground the dossier prompt in facts, for both asset classes.** `assets` is
populated from the justETF cache plus OpenBB `etf/info` so instrument metadata
is DB-queryable and ISIN-keyed. The dossier context then branches on
`instrument_type`: funds receive top-10 holdings, tracked index, and inception
date; equities receive sector, industry, and fundamentals. **Style and factor
mandate are classified deterministically in Python from holdings and index
name, and passed to the LLM as an established fact — never inferred by the
model.** The history caveat is reworded to state data coverage rather than fund
age.

**7. Route providers by purpose.** `openbb_default_provider` is set explicitly
rather than defaulting to Yahoo implicitly. Price history routes to the
already-keyed quant-grade sources (Tiingo, TwelveData, Databento, EODHD EOD);
Yahoo is retained for reference metadata, where the failure mode is "missing"
and is cross-checkable against the issuer. This extends the per-purpose routing
already established in [[ADR 0010]].

**No new paid data subscription is adopted.** The free, already-deployed stack
covers holdings, inception, and style for the instruments in the universe.

## Consequences

- Restoring `bar_prices` should clear `Insufficient comparison panel`,
  `regime: unknown`, `confidence 50%`, and `ml_signal_unavailable` together.
  Because four caveats share one cause, the fix is verifiable in one pass: if
  they do not all clear, the remaining ones have independent causes and must be
  re-diagnosed rather than assumed fixed.
- `provider_health_history` becoming writable restores the observability channel
  whose absence hid this defect. Expect a burst of historical provider-failure
  rows once writes succeed.
- ML signals stay unavailable until models are actually trained; restoring bars
  is necessary but not sufficient. `ml_signal_unavailable` should be expected to
  persist after Phase 1 and is not evidence the fix failed.
- Reverting `er_mode` to `"trailing"` makes headlines coherent but does not make
  them *good*: a shrunk trailing anchor is still backward-looking, as
  `expected_return.py` documents. The building-blocks (`"blocks"`) mode remains
  the intended destination, and arXiv:2504.14345 remains the reference design
  should a grounded BL view be revisited. This ADR deliberately does not adopt
  it now.
- Deterministic style classification introduces a maintenance surface: the
  growth/value proxy lists and index-name rules need review as holdings drift.
  This is accepted over LLM inference because it is inspectable and testable.
- `yfinance` remains in the stack for reference metadata despite known
  weaknesses; this is a considered split, not an endorsement. If Yahoo's holdings
  coverage degrades, the EODHD Fundamentals tier (~$60/mo) is the identified
  upgrade path and the provider abstraction already accommodates it.
- Two unrelated issues surfaced during forensics and are recorded here rather
  than lost: the `alpaca` API secret is stored **in cleartext** in
  `api_keys.meta_json` and should be rotated; and the worker logs
  `macro backfill: VIXCLS unavailable: Internal Server Error`.

## Alternatives considered

**Adopt EODHD (or FMP) as the primary provider, as externally recommended.**
Rejected on evidence. The premise — that free providers return nothing for
Xetra ETF constituents — was tested and is false. FMP's holdings derive from SEC
NPORT-P filings and therefore cover US-domiciled funds only, making it
structurally unable to serve UCITS ETFs at any tier. EODHD would work but costs
~$60/mo for data the deployed stack already returns, and adopting it would not
have fixed any of the four caveats, which are schema failures.

**Re-run migrations 0073/0075 by editing `alembic_version`.** Rejected: mutating
applied-revision state to force a re-run is fragile and leaves no audit trail. A
forward migration is idempotent, reviewable, and safe to re-apply.

**Repoint `build_panel` at `PriceCache` instead of fixing `bar_prices`.**
Rejected: it would paper over the write failure while leaving the regime
classifier, ML training, and factor loadings broken, and would entrench the
split-store confusion that made this defect hard to see.

**Delete the LLM-view BL path entirely.** Considered and deferred. The path is
sound in structure — the defect is the view *source* and the silent failure
sentinel, not Black-Litterman. Retaining it dormant preserves the option to
supply a grounded view later at no ongoing cost.

**Let the LLM infer style from holdings passed in the prompt.** Rejected. Style
is a deterministic function of holdings and mandate; computing it in Python
makes it testable and removes the exact inference step that produced the
"Value ETF described as growth" error.
