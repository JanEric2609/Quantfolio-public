# Scalable Capital (read-only)

Quantfolio reads your Scalable Capital depot through Scalable's official
[`sc` CLI](https://github.com/ScalableCapital/scalable-cli): holdings, cash,
the overnight account, savings plans and the transaction history. **It never
trades or changes anything at Scalable.** Nothing in the app can place, change
or cancel an order or a savings plan, and no Scalable password or token is
stored in Quantfolio's database.

## How it stays read-only

Four independent layers, so no single mistake opens a write path:

1. **Read-only login.** The session is created with `sc login --local-read-only`;
   sc then refuses every GraphQL mutation except the login itself.
2. **Trade controls.** The CLI user's `config.toml` sets
   `[trade_controls] allowed_isins = []`, which refuses every order locally.
   Before each sync Quantfolio reads `sc capabilities` and stops if the list
   is not empty (`scalable_require_read_only_guard`, on by default).
3. **Allow-list in the app.** `backend/app/foundation/scalable/cli.py` runs only
   whole argv shapes on `ALLOWED_COMMANDS` (whoami, capabilities, broker
   context show / overview / cash-breakdown / holdings / savings-plans /
   transactions / transaction details, overnight, overnight transactions).
   Flag values are checked with strict patterns and may not start with `-`.
4. **Allow-list in a root-owned wrapper.** sudoers lets Quantfolio run only
   `/usr/local/libexec/quantfolio-sc-ro`, never `sc`. The wrapper checks the
   same list again before it starts `sc`.

The one command that is not a read is the login the Control Center starts
(`backend/app/foundation/scalable/login.py`): exactly `login --local-read-only`.
It is not on `ALLOWED_COMMANDS`; the wrapper accepts those two words and
nothing else (no plain `login`, no extra flag), so the only session the app
can create is a read-only one, and `allowed_isins = []` still refuses every
order. You approve the code in the Scalable app yourself.

If sc ever answers a read with a write-refusal code (`local_read_only`,
`trade_control_*`), the sync stops, the connection switches itself off and
the sync log says why. Turn it back on in the Control Center only after you
know what happened.

`tests/test_scalable.py` checks layers 3 and 4 against each other and greps
the service for any command that is not on the list.

## One-time server setup

1. In the Scalable **web** app: Profile › Security › Agentic Investing, enable
   Scalable CLI (sc refuses to log in until this is on).
2. On the app LXC, as root, from the deployed checkout:

   ```bash
   bash /opt/quantfolio/infra/scalable/install.sh
   ```

   It downloads the latest `sc` release (`SC_TAG=vX.Y.Z` pins one), verifies
   Scalable's minisign signature over the checksum file and the checksum of
   the archive, installs `sc` to `/usr/local/bin/sc`, creates
   `scalable-cli-user` (home `/var/lib/scalable-cli`, mode 0700), installs the
   wrapper, the sudoers rule (checked with `visudo`) and `config.toml` with
   `allowed_isins = []`, then checks as the `quantfolio` user, through sudo and
   the wrapper, that `sc capabilities` reports every order refused, and once
   more inside the API unit's systemd sandbox. Running it again is safe.

   sc needs glibc 2.38 or newer. Debian 12 has 2.36, so there the script also
   fetches Debian 13's `libc6` and `libgcc-s1` with apt, checked against
   Debian's archive signature, into `/usr/local/lib/quantfolio-sc-runtime`.
   Only the wrapper uses it, to start sc; the rest of the LXC keeps its own
   glibc, and sc stays the official binary, so the pinned SHA-256 still
   applies. Run the script again now and then to pick up Debian 13's glibc
   security updates; it waits for a running sync or login before it swaps the
   folder. Once the LXC itself has glibc 2.38 or newer, the next run removes
   the folder.

Then in the app (Control Center › Banks & brokers › Scalable Capital):

1. **Pin installed sc**: stores the SHA-256 of `/usr/local/bin/sc`; a swapped
   binary is refused from then on.
2. **Log in to Scalable**: the card shows a code and a link on
   scalable.capital. Approve it in the Scalable app (and the second factor on
   your linked device if asked). The same button logs in again when the
   session expires.
3. Turn **Sync** on and press **Test**. Each step (binary, sudo, login, trade
   guard, portfolio, overview) shows whether it passed. If you have more than
   one portfolio, set the portfolio ID.
4. Portfolio › **Accounts** › **Sync Scalable** for the first sync.

<details><summary>The same steps by hand</summary>

```bash
useradd --system --create-home --home-dir /var/lib/scalable-cli --shell /usr/sbin/nologin scalable-cli-user
chmod 0700 /var/lib/scalable-cli
# Verify the release (minisign + SHA-256, see the sc README), then:
install -o root -g root -m 0755 sc /usr/local/bin/sc
install -o root -g root -m 0755 infra/scalable/quantfolio-sc-ro /usr/local/libexec/quantfolio-sc-ro
install -o root -g root -m 0440 infra/scalable/sudoers.quantfolio-sc /etc/sudoers.d/quantfolio-sc
visudo -cf /etc/sudoers.d/quantfolio-sc
install -d -o scalable-cli-user -g scalable-cli-user -m 0700 /var/lib/scalable-cli/.config/scalable-cli
install -o scalable-cli-user -g scalable-cli-user -m 0600 infra/scalable/config.toml \
  /var/lib/scalable-cli/.config/scalable-cli/config.toml
sudo -u quantfolio sudo -n -H -u scalable-cli-user -- /usr/local/libexec/quantfolio-sc-ro capabilities --json
# Log in from a root shell instead of the Control Center:
sudo -u scalable-cli-user -H sc login --local-read-only
```

</details>

### systemd units

The API and worker call `sudo`, so their units must **not** set
`NoNewPrivileges=true` or `RestrictSUIDSGID=true` (sudo then can't become
another user; the login and the Test step **sudo** show `sandbox_blocks_sudo`),
nor `ProtectSystem=strict` or `ProtectHome=true`.

The same goes for every setting that installs a seccomp filter:
`ProtectKernelTunables`, `ProtectKernelModules`, `ProtectKernelLogs`,
`ProtectClock`, `ProtectHostname`, `PrivateDevices`, `LockPersonality`,
`MemoryDenyWriteExecute`, `RestrictRealtime`, `RestrictNamespaces`,
`RestrictAddressFamilies` and the `SystemCall*` settings. For a unit that runs
as a non-root `User=`, systemd turns `NoNewPrivileges` on by itself as soon as
one of them is set, so `systemctl show -p NoNewPrivileges` still says `no` while
sudo already fails. Units from before October 2026 set `ProtectKernelTunables`;
`quantfolio-update-app` (or `quantfolio-update` on the Proxmox host) installs
the current ones and restarts the services.

The shipped units use `ProtectSystem=full` and `ProtectControlGroups`, neither
of which touches sudo: `/etc` is only read, sudo's timestamps live in `/run`,
and the CLI user's home is under `/var`. `install.sh` checks this inside the
unit's sandbox and names any setting that blocks sudo, the Control Center Test
runs inside the real unit, and `tests/test_scalable.py` keeps these settings out
of the shipped units.

## What a sync does

The first successful sync binds the server's single sc login to the app user
who ran it. Another app user can never sync that portfolio into their own
book (the API answers 409 `owned_by_other_user`).

sc answers every broker and overnight query inside its own envelope:
`data = {account_id, portfolio_id, resolution | selection, result}`, with the
projection in `result` (`broker_result_envelope` in sc's
`src/broker_shared.rs`). `ScalableCli.run` returns that `result`; a query
without one, or a projection missing a key it always carries (`valuation`,
`cash_balance`, `items`, `balance`), stops the sync with `unexpected_shape`
before anything is written. Until 2026-10 the mapping read the wrapper itself,
so every value was empty and the sync stored zeros while reporting success.

The portfolio is the Control Center's **Portfolio ID**, else sc's saved broker
context, else the one sc picks itself when the account has a single
portfolio (it refuses to guess between several).

Each sync, in one database transaction:

- **Holdings** → `broker_positions` (one row per ISIN; positions you no longer
  hold are removed). They are mirrored into the portfolio's holdings, so every
  page that shows holdings sees them. An ISIN you also hold at DKB is one
  holding with the summed quantity.
- **Cash** and the **overnight account** → `connected_accounts` (cash, savings),
  counted in net worth.
- **Savings plans** are shown read-only on the Accounts page. If sc cannot
  read them on a run, the last synced list stays.
- **Transactions** → the activity ledger, deduplicated on the transaction ID.
  Buys and sells get price, fees and withheld tax from `transaction details`,
  dividends and interest their gross and withheld tax: at most 50 new rows per
  run. A row over that budget (or whose details are briefly unavailable) is
  not booked yet; the next run reads it again and books it complete.
- **Overnight interest** (`sc overnight transactions --type-filter INTEREST`)
  → the activity ledger as `interest` on the overnight account. A wrapper
  installed before this read was allowed refuses it; the sync goes on and
  its counts say `overnight_interest: unavailable` until `install.sh` is run
  again.
- **Sanity checks.** Holdings empty while the overview values the depot is an
  error (`holdings_unreadable`), nothing is written. Holdings that add up to
  more than 1 EUR and 2 % away from the overview's securities value, an
  unknown transaction status, or a follow-up step that failed make the sync a
  *warning* that names it.
- Only one sync runs at a time (a database constraint); a second request gets
  `busy`.

After the transaction commits, these steps run on their own, so one failing
never undoes the synced data: ticker resolution, the holdings mirror and
today's snapshot, dividend and interest events for the tax cockpit, and ETF
classification.

The worker wakes hourly and syncs once **Sync every (hours)** has passed since
the last successful sync (default 6). An expired login, a missing trade guard
or a missing install is not retried faster than that; the Control Center
shows an attention item instead.

### Returns stay correct

Time-weighted return treats only `cashflow` ledger rows as money crossing the
portfolio boundary:

- Deposits and withdrawals at Scalable are `cashflow`, so a transfer from the
  DKB giro nets against the DKB statement line.
- Dividends, interest, taxes and fees are return, not flows.
- On the **first sync that reads the depot** (the baseline, `baseline_at` in
  the depot's raw JSON) the whole Scalable value enters the tracked portfolio.
  It is booked once as an opening-balance `cashflow` dated that day, so TWR
  does not read it as a gain. It does not count as income in the 30-day
  cash-flow figure. A depot whose earlier syncs read nothing (before the
  `result` fix) has no opening balance yet and is baselined again.
- Deposits booked up to the baseline are inside the opening balance, so they
  are history (`external_pre_sync`), not contributions; a deposit first seen
  by a later sync is a contribution.

### Holdings you entered by hand

If a hand-entered holding has the same ISIN as a synced Scalable position,
the Scalable position is **not counted** until you decide, so nothing is ever
counted twice. Portfolio → Accounts → **Review** shows each match (a dry run
that changes nothing). Then per row:

- **Replace**: deletes the hand-entered row; the synced position counts.
- **Keep both**: both count, e.g. the same ETF also held at a third broker.

The decision is stored per ISIN and kept for later syncs.

### Where the Scalable depot shows up

Every reader of the real book goes through `app.foundation.live_positions`:
`live_positions` (one row per depot) or `combined_positions` (one row per
instrument, the depots summed; DKB's listing wins when both hold it), plus
`synced_cash` for the cash at every bank and broker (depots excluded). So the
Scalable depot counts in:

- net worth, the monthly plan, the tax cockpit and the holdings mirror;
- Quant Lab (holdings summary, the price matrix behind risk and optimisation);
- the advisor's real-vs-champion diff and the goal optimiser;
- the LLM portfolio (prices, divergence) and paper-portfolio seeding (holdings and cash);
- news relevance, the Discover exclusion list, the price backfill and the analysis report.

Only the syncs, their repair tools and the ticker resolver read
`dkb_positions` or `broker_positions` directly;
`tests/test_live_positions_readers.py` fails on any new direct reader.

### Telling the depots apart in the UI

Once positions from both brokers are synced, the Overview, Portfolio →
Accounts and Quant Lab → Holdings show an **All / DKB / Scalable** switch. It
is one setting shared by those pages (kept in the browser), and a page with
nothing from the chosen broker shows everything instead of an empty list.

- The switch narrows the position views: the Overview's allocation and
  largest positions, the accounts list, and the Quant Lab position list,
  donut and total. Net worth, risk metrics, factor exposures and the
  rebalancing suggestions stay whole-book; Quant Lab says so when filtered.
- Every synced position carries a **DKB** or **Scalable** badge, and a
  hand-entered one says **Manual**. Portfolio → Holdings sums each ISIN over
  the depots, so it has no switch: its *Held at* column lists every depot
  instead.
- Quant Lab's **Per depot** cards show each broker's value, share and
  unrealised P&L next to the total. The P&L counts only positions with a
  known purchase cost and says how much of the depot that covers (a
  transferred-in position often has none).
- `GET /api/quant/portfolio/real/holdings` keys `by_ticker` by ISIN (one row
  per fund across depots, with `depots` listing them) and adds `by_broker`.

### The monthly plan with two brokers

*This month* (`decision/monthly_plan.py`) works per depot:

- **Broker per action.** Each buy names its broker and that broker's fee.
  The default, *Automatic*, picks the cheapest synced depot: Scalable as soon
  as its depot is synced (orders 0,99 € on EIX, Prime-ETF buys from 250 € free,
  gettex/Xetra 1,99 €; savings plans free), DKB otherwise.
  A fixed choice in Plan settings wins. A sale is made at the depot that
  holds the position, and starts with the position with the smallest gain
  per euro: the same ETF at DKB and at Scalable is two FIFO chains, and the
  depot with the younger, cheaper lots realises the least taxable gain.
- **Savings plans that already run.** The synced Scalable savings plans are
  sorted into sleeves (emerging-markets ETFs count as core) and converted to
  a monthly amount, so the plan never asks for a plan that exists. It says
  when the plans fall short of the core money, when they differ from
  *Monthly contribution*, and when they feed a sleeve that is still locked
  (single stocks before the evidence gate passes). A plan on a schedule the
  plan does not know is listed but left out of the totals.
- **Cash above the emergency reserve.** *Emergency reserve* is a fixed amount
  in euros. DKB Tagesgeld, the Scalable overnight account and the broker's
  buying power (not the cash earmarked for savings plans) above it are
  offered as an optional one-off buy, split like the contribution, when one
  order's fee stays at or below 1 %. The giro account never counts. With no
  reserve set the plan suggests nothing from cash.
- **Look-through.** The core card shows the core's emerging-markets share
  looking through the funds (world ETFs 0 %, all-world about 10 %, EM ETFs
  100 %) against the world market's ~10 %. It is information, not a target.

### Taxes

After each sync (`ingest_tax_events`, EUR rows only, deduplicated on
`scalable:<transaction id>`):

- **Buys → FIFO lots** booked to the Scalable depot (`account_ref`), cost
  basis including the order fee. FIFO runs per depot (§ 20 Abs. 4 S. 7 EStG),
  so a Scalable sale never consumes the DKB lots of the same ETF, and the tax
  cockpit's harvest planner prices each depot with its own lots.
- **Sells → `sale` events**, FIFO over the Scalable lots bought on or before
  the sale. A share sale goes to the Aktien loss pot (§ 20 Abs. 6 S. 4 EStG),
  a fund sale to the general one with its Teilfreistellung. A sale with no
  lots to cover it is not guessed: the sync becomes a *warning* naming it.
- **Dividends and interest** (overnight interest included) → events at the
  gross from the transaction details; without a tax split the booked credit
  is used, and the allowance planner treats it as possibly net.
- **Tax Scalable withheld or refunded** on a trade or a credit → a
  `withholding` event, counted as KESt already paid.
- The fund class comes from evidence only (an etf_universe match); a share,
  bond or ETC is never a fund. Scalable's own security type also sets the
  holding's asset type, so a share is never estimated a Vorabpauschale.

Like every tax output in Quantfolio these are estimates; Scalable's
statements (Ertragsabrechnung, Jahressteuerbescheinigung) are the source of
truth. The Freistellungsauftrag is per bank: set the Scalable share under Tax
settings.

## When something goes wrong

| Status / sync log | What to do |
|---|---|
| `login_required` (`no_session`, `refresh_relogin_required`) | Control Center › Scalable › **Log in again** |
| `sandbox_blocks_sudo` | Install the current units: `quantfolio-update-app` on the app LXC, as root. Keep `NoNewPrivileges`, `RestrictSUIDSGID`, `ProtectKernelTunables` and the other seccomp settings out of any drop-in ([systemd units](#systemd-units)) |
| `wrapper_outdated` (login refused by the wrapper) | Re-run `infra/scalable/install.sh` to install the current wrapper |
| `guard_unattested` | Set `allowed_isins = []` in the CLI user's `config.toml`, then Test |
| `not_installed` / `sudo_not_configured` | Re-run `infra/scalable/install.sh` |
| install.sh stops with ``version `GLIBC_2.38' not found`` | The checkout predates the private glibc: update it (`quantfolio-update`) and run `install.sh` again |
| `binary_hash_mismatch` | sc changed: verify the new release, then update the pinned SHA-256 |
| `broker_context_missing` | sc could not pick a portfolio (several exist): set the portfolio ID in the Control Center |
| `unexpected_shape` | sc's output format changed: the sync wrote nothing. Check the sc version against `mapping.py` |
| `holdings_unreadable` | sc valued the depot but listed no holdings; the sync wrote nothing. Run Test, then sync again |
| *warning*: "no tax lots at Scalable cover the sale" | The position was bought before the history sc returns (or moved in from another depot): add its purchase as a tax lot |
| counts: `overnight_interest: unavailable` | Re-run `infra/scalable/install.sh` so the wrapper allows `overnight transactions` |
| Connection switched itself off after a write refusal | A read was refused as a write: check the sync log before turning it on |
| *warning*: "older ones were not imported" | The history is longer than 20,000 transactions; the watermark stays put, so raise `FIRST_SYNC_MAX_PAGES` in `scalable/service.py` |
| `binary_path_mismatch` | Only `/usr/local/bin/sc` is ever run by the wrapper; install sc there |

## Enums checked against a real account

The JSON shapes come from the CLI's source (`src/broker_projections.rs`,
`src/overnight_projections.rs`); `tests/test_scalable.py` feeds them in sc's
real wrapped form, one test with a depot shaped like the owner's (2026-10).
Seen on that account: statuses SETTLED, PENDING, CANCELLED; cash type
DEPOSIT; security types STOCK and ETF.

- The **status** values. `mapping.EXECUTED_STATUSES` lists the ones treated
  as executed. A trade needs one of them (no status is not enough); a cash row
  is held back only when it names another status. Held-back rows are not
  written, and a row that settles within the seven days every sync re-reads
  is imported then. `NOT_EXECUTED_STATUSES` (pending, cancelled, rejected, …)
  are held back quietly; any other status makes the sync a *warning* naming
  it.
- The **cash transaction types**. Deposits, withdrawals, dividends, interest,
  taxes and fees are recognised by keyword; anything else lands in the
  activity ledger as `other` with `needs_review`, never as a cash flow.

To pin them: run one sync, then read the sync log and the activity ledger
rows marked `needs_review`, and extend the two lists in
`backend/app/foundation/scalable/mapping.py` with a test.
