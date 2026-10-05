# German private-investor tax cockpit

The tax cockpit computes Anlage-KAP-style estimates from the activity ledger,
manually entered tax events, and tax lots. **Every figure is an estimate.**
Broker statements (Steuerbescheinigung, Erträgnisaufstellung) remain the source
of truth. This is not tax advice.

## Scope

In:

- Dividends + foreign withholding tax (DBA 15% credit cap)
- Interest income
- Capital gains from sales via FIFO lot accounting per ISIN
- Vorabpauschale estimate (§18 InvStG 2018) per fund
- ETF/fund Teilfreistellung (Aktien 30%, Misch 15%, Immobilien 60%/80%)
- Freistellungsauftrag / Sparer-Pauschbetrag (€1 000 single, €2 000 spouse)
- Loss bucket separation (Aktien vs. Sonstige)
- Soli (5.5% on KESt)
- Church tax (8% BY/BW, 9% other)
- NV certificate (Nichtveranlagungsbescheinigung) and tax-free gain harvesting
  up to the Grundfreibetrag plus the Sparer-Pauschbetrag

Out (deferred to Phase B):

- Broker statement import (IBKR CSV, Trade Republic, Scalable PDF/CSV)
- Finanzamt lookup by Steuernummer
- Multi-currency FX history beyond per-event spot rates

## Data model

- `tax_lots` — FIFO lots per (user, ISIN, account_ref). Holds remaining
  quantity and cost basis. Sales decrement these in place.
- `tax_ledger_events` — canonical events (dividend, interest, sale,
  vorabpauschale, withholding, fee). Every row carries `confidence`,
  `source`, `source_ref` for provenance.
- `tax_year_summaries` — cached overview payload per (user, year).

## Calculators

Pure-function modules in `backend/app/foundation/tax_calc/`:

- `allowances.py` — Sparer-Pauschbetrag, Soli, church tax.
- `teilfreistellung.py` — §20 InvStG 2018 partial exemption rates.
- `fifo.py` — `fifo_consume(lots, qty, price)` → realised gain, basis,
  consumed-lots breakdown. Mutates lot dicts; caller persists.
- `vorabpauschale.py` — §18 InvStG; uses a `BASISZINS_BY_YEAR` table that
  must be updated each January from the BMF publication.
- `foreign_wht.py` — 15% DBA cap with creditable / excess split.
- `buckets.py` — Aktien vs. Sonstige loss bucket classification.
- `jurisdictions/de/gain_harvest.py` — `tax_free_room` (Grundfreibetrag +
  Sparer-Pauschbetrag − other income − capital income already booked, capped
  by the German Familienversicherung limit when that insurance is set) and
  `plan_gain_harvest` (sell-and-rebuy steps, FIFO as §20(4) sentence 7 EStG
  requires, best gain per euro traded first, kept only when the tax avoided
  later exceeds the order fees and spread).

## API endpoints

- `GET  /api/tax/overview?year=YYYY`
- `GET  /api/tax/reports/kap?year=YYYY`
- `GET  /api/tax/settings`
- `GET  /api/tax/basiszins`
- `PUT  /api/tax/settings`
- `GET  /api/tax/events?year=YYYY`
- `POST /api/tax/events`
- `DELETE /api/tax/events/{id}`
- `GET  /api/tax/lots`
- `POST /api/tax/lots`
- `DELETE /api/tax/lots/{id}`
- `POST /api/tax/lots/seed-from-activity` — best-effort seeding from
  `ActivityLedgerEntry` rows with `activity_type='buy'`.
- `POST /api/tax/sales/fifo` — record a sale, consume lots FIFO, write a
  `sale` ledger event.
- `POST /api/tax/vorabpauschale/estimate`
- `GET  /api/tax/harvest?year=YYYY` — tax-free gain harvesting: under an NV
  certificate (`mode: "nv"`) or, without one, within what each bank's
  Freistellungsauftrag still covers (`mode: "allowance"`). Prices come from the
  DKB and Scalable depot syncs, lots from `tax_lots` (an average-cost stand-in
  allowing only a whole-position sale when the lots do not match the depot).
  Each step names its bank; `bank_rooms` gives the cap per bank.
- `GET  /api/tax/allowances?year=YYYY` — Freistellungsauftrag and NV
  certificate per bank (see below).

## NV certificate and gain harvesting

With a Nichtveranlagungsbescheinigung (e.g. a student without other income)
the banks withhold no tax. Capital income stays untaxed while total income is
under the Grundfreibetrag plus the Sparer-Pauschbetrag, so realising gains in
that room and buying the same units straight back raises the cost basis for
free. The overview card lists the steps and warns about: an expiring or
expired certificate; the German Familienversicherung income limit (or, when
insured abroad, the insurer's own rules); BAföG; returning the certificate
when its conditions stop holding (e.g. starting work); and trading by
mid-December so the sale settles in the year. Settings: `tax_nv_certificate`,
`tax_nv_valid_until`, `tax_other_income_eur`, `tax_health_insurance`
(`unknown` / `de_family` / `de_own` / `foreign`), `tax_bafoeg`.

## Two banks: Freistellungsauftrag and NV certificate

The Sparer-Pauschbetrag is one yearly allowance (§ 20 Abs. 9 EStG: 1,000 €,
2,000 € jointly), but a bank only applies the Freistellungsauftrag (FSA) given
to *it* and withholds 25 % KESt plus Soli (and church tax) above that
(§ 44a Abs. 1, 2 EStG). One FSA covers every account and depot at a bank, so
DKB's covers the Girokonto, the Tagesgeld and the depot, and Scalable's the
depot and the Tagesgeld/overnight account (BMF 14.05.2025 Rn. 259; Scalable
help centre). An NV certificate stops withholding entirely, but only at the
banks that have a copy; it takes precedence over the FSA there, which applies
again when the certificate ends (Rn. 255).

`GET /api/tax/allowances` works out per bank (`foundation/tax_allowances.py`,
pure split in `tax_calc/jurisdictions/de/allowance_split.py`):

- **Booked**: the year's tax events with `institution = dkb | scalable`
  (synced ones get it from their source; DKB interest credits on the Tagesgeld,
  or with explicit interest wording elsewhere, are ingested as interest
  events), after Teilfreistellung and the bank's own loss pots (Rn. 230).
- **Expected for the rest of the year**: Tagesgeld interest (balance × rate:
  Scalable's from the sync, DKB's from `tax_interest_rate_dkb`, else the run
  rate so far), dividends of the same months last year, and January's
  Vorabpauschale while it is still ahead.
- **Withheld**: income above the bank's FSA when no NV copy covers it. Synced
  dividend and interest credits are booked net, so the part above the order is
  grossed up by the withholding rate (estimate).
- **Suggested split**: never below what a bank has already used (Rn. 258/259),
  then each bank's projected income, then a spare remainder where gains can
  still be harvested.
- **Actions** with deadlines: send the NV copy to a bank that doesn't have it
  (Scalable: copy only, upload link from support or post to Scalable Capital
  Bank GmbH, Seitzstraße 8e, 80538 München, by 15 December; DKB: file upload
  under Mein Profil › Steuerliche Angaben); change the split (until
  31 December); a Verlustbescheinigung when one bank holds losses and the
  other income (§ 43a Abs. 3 EStG, irrevocable request by 15 December);
  NV eligibility (other income plus the capital income above the
  Sparer-Pauschbetrag within the Grundfreibetrag; not issued with an assessed loss carry-forward
  or an application assessment, Rn. 252); hand the NV back when income gets
  too high (§ 44a Abs. 2 Satz 4); tax withheld although the allowance would
  have covered it comes back only through Anlage KAP (§ 32d Abs. 4).

Settings: `freistellungsauftrag_dkb_eur`, `freistellungsauftrag_scalable_eur`
(the orders on file; empty = not entered), `freistellungsauftrag_other_banks_eur`
(the total of the orders at other banks; at least `freistellungsauftrag_used`,
the allowance used there, which the year overview keeps using), `tax_nv_filed_dkb`, `tax_nv_filed_scalable` (unanswered, as in
older settings with only `tax_nv_certificate`: every bank is assumed to have a
copy and the cockpit asks; `false` means that bank has none),
`tax_interest_rate_dkb`. These per-bank values describe one person's accounts:
the user who first sets them owns them (`tax_bank_settings_user_id`), another
user can't change them (403) and their plans ignore them. An NV certificate
ends on 31 December (§ 44a Abs. 2 Satz 3); a date earlier in the year is
treated as not covering that year. Only for German tax residents (§ 44a Abs. 1:
unlimited tax liability).

The Vorabpauschale of a fund year counts on the first working day of the next
year (§ 18 Abs. 3 InvStG), so tax year *Y* carries the one computed for *Y − 1*
and it uses up *Y*'s allowance in January.

## Maintenance

When BMF publishes a new Basiszins (typically in January), update
`BASISZINS_BY_YEAR` in `tax_calc/vorabpauschale.py`. Existing year
estimates are tagged `basiszins_known: false` if the year is missing.

Each year also update `GRUNDFREIBETRAG_BY_YEAR` (§32a EStG) and
`FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR` (one seventh of the monthly
Bezugsgröße) in `tax_calc/jurisdictions/de/gain_harvest.py`. A missing year
falls back to the latest known value and the harvest card says so.
