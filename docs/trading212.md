# Trading 212 (read-only)

Quantfolio reads a Trading 212 Invest account through Trading 212's public REST API (v0, beta).
It reads positions, cash, the order, dividend and cash-transaction history, and the pies.
Nothing in Quantfolio can place, change or cancel an order, or change a pie.

## Setup

1. In the Trading 212 app, go to **Settings › API (Beta)** and generate a key for the **live** account.
   - Enable **Account data**, **Portfolio**, **History**, **Pies** and **Metadata**.
   - Leave **Orders** off. Quantfolio never needs it.
   - Restrict the key to the server's outbound IP address.
2. In **Control Center › Banks & brokers › Trading 212**, paste the key and the secret and switch
   **Sync Trading 212** on. Press **Test connection**. If a step fails, the probe says which
   permission is missing.
3. Press **Sync Trading 212** once (Portfolio › Accounts). This first sync reads the whole history,
   which can take a few minutes at the API's rate limits. After that, the worker syncs every
   `trading212_sync_hours` (default 6).
4. On the tax page, enter the Freistellungsauftrag on file at Trading 212 (app: tax overview). If
   one is on file, also say whether Trading 212 has a copy of your NV certificate.
5. After the first sync, compare one dividend and the cash balance with the Trading 212 app (see
   *Dividends* below).

The key and the secret are stored encrypted in `api_keys` (`set_secret("trading212", key,
meta={"api_secret": …})`; the secret is a sensitive meta field and never reaches `meta_json`).
The key belongs to the app user who ran the first sync. Nobody else can sync or test it.

## What the client may call

`foundation/trading212/client.py` holds `ALLOWED_PATHS`, the only endpoints a request can be built
for, and every one is a GET:

| Endpoint | Documented limit | Used for |
|---|---|---|
| `/equity/account/summary` | 1 / 5 s | cash (free, in pies, reserved), total value, account id |
| `/equity/positions` | 1 / 1 s | positions with ISIN, quantity, value and cost in EUR |
| `/equity/history/orders` | 20 / min | fills: price, quantity, FX rate, net value, fees |
| `/equity/history/dividends` | 20 / min | dividends: EUR credit, gross per share |
| `/equity/history/transactions` | 20 / min | deposits, withdrawals, fees, interest on cash |
| `/equity/pies`, `/equity/pies/{id}` | 1 / 30 s, 1 / 5 s | pies (fail soft, see below) |
| `/equity/metadata/instruments` | 1 / 50 s | instrument type (ETF, STOCK) for ISINs not seen before |

Any other path raises `Trading212ReadOnlyViolation` before a request exists. That also covers a
`nextPagePath` on another host or feed. The sync then switches itself off until someone has looked.
The client has no POST or DELETE method. `tests/test_trading212.py` pins the list.

Rate limits are per account. The client reads `x-ratelimit-remaining` and `x-ratelimit-reset` on
every response, waits for the reset when a window is used up, and retries a 429 after it. Calls are
sequential.

## What a sync writes

- **Positions** go to `broker_positions` (`source = "trading212"`), one row per ISIN. Value, price
  and cost are per unit in EUR, from Trading 212's own valuation. T212's ticker (`CMG_US_EQ`) is
  never used as an app ticker; the ISIN resolver sets the ticker afterwards. Positions go through
  `live_positions` and `combined_positions` like Scalable's, so they count in net worth, allocation
  and the advisor's fit.
- **Cash** goes to a `connected_accounts` row of type `cash`: available plus reserved plus the cash
  held in pies. The monthly plan counts it as savings (it earns interest), never as buying power.
  New money still goes to Scalable.
- **History** goes to `activity_ledger_entries`, deduped on `trading212:<account>:<kind>:<id>`.
  - **Fills** are `buy` or `sell` in EUR. The price uses the fill's own FX rate, and fees come
    from `taxes[]`.
  - A **share movement without a trade** (split, spin-off) is a `corporate_action` marked for
    review. It is not in the tax lots.
  - **Dividends** and **interest on cash** are `dividend` and `interest` rows.
  - **Deposits** before the first sync are history (`external_pre_sync`). The first sync books the
    account's total value once as the opening balance, the same as Scalable.
  - A later sync stops paging a feed at the first page that holds a stored row.
- **Tax** goes to the tax cockpit through `broker_sync.ingest_broker_tax_events` (`source =
  "trading212_sync"`). Buys become FIFO lots per depot and sells consume them. Dividends and
  interest become events at Trading 212's bank, so they use its Freistellungsauftrag.
- **Measured interest:** the interest paid in the last 30 days, annualised over today's cash, is
  shown as "cash interest … p.a. (measured)". The tax plan uses it. The API does not expose the
  advertised rate.

### Dividends

The gross of a dividend (gross per share × quantity, at the ECB rate of the pay day) is booked as
the income. The credit itself is what Trading 212 paid out. The API does not say whether that
credit is before or after US withholding, or after the German tax Trading 212 withholds, so each
dividend row carries `withholding_unverified`. Compare the first one with the app before you rely
on the tax cockpit's dividend figures. The Steuerbescheinigung stays the source of truth.

## Pies (fail soft)

Trading 212 marks the pie endpoints deprecated. Each sync reads the list and then each pie's detail
into `broker_pies`, replacing the last read. If the pie read fails, the last good read stays with
its date, the sync log says `pies unavailable`, and the sync still succeeds. The Control Center
shows an item once the pies have been unreadable for more than 7 days. The Accounts page shows each
pie's value, result, cash, dividend setting and goal, and target vs actual share per holding.

## Control Center items

- `trading212-key-missing`: sync is on, but no key is saved.
- `trading212-key`: the key was refused (401).
- `trading212-scope`: the key lacks a permission, or the IP is not allowed (403).
- `trading212-failed`: the last sync failed.
- `trading212-stale`: no successful sync for two sync intervals.
- `trading212-pies`: the pies have been unreadable for more than 7 days.
