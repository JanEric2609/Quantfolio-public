# Cleanup: partial Alpaca bars stored before WP6 (2026-11-12)

Decision (owner delegated, 2026-11-12): do not delete anything. After the WP6 deploy the
re-pull replaces a partial bar the next time a symbol is fetched, and the 60-day gap cut
stops a reused ticker from joining two companies. Old rows only matter for symbols nobody
fetches again, so a one-off sweep is optional. Nothing here runs automatically.

## 1. Look first (read-only)

```sql
-- bar_prices columns on prod (checked 2026-11-12): id, symbol, ts, open, high, low, close, volume, currency, provider
SELECT symbol, max(ts)::date AS last_bar, count(*) AS bars
FROM bar_prices WHERE provider ILIKE 'alpaca%'
GROUP BY symbol HAVING max(ts) < current_date - 60 ORDER BY last_bar;
```

Run on prod 2026-11-12 (read-only): 141 Alpaca symbols, none older than 60 days, so there is nothing to sweep.

A symbol whose last Alpaca bar is more than 60 days old is a delisted or reused ticker:
its bars cannot be re-pulled into the right company. Check the list before any delete.

## 2. Only if the list is clean

Delete just those symbols' Alpaca rows in a transaction, compare the count with step 1,
then COMMIT or ROLLBACK. Do not touch `price_cache`, which refills itself.
