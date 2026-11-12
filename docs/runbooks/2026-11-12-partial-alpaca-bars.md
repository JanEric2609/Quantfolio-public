# Cleanup: partial Alpaca bars stored before WP6 (2026-11-12)

Decision (owner delegated, 2026-11-12): do not delete anything. After the WP6 deploy the
re-pull replaces a partial bar the next time a symbol is fetched, and the 60-day gap cut
stops a reused ticker from joining two companies. Old rows only matter for symbols nobody
fetches again, so a one-off sweep is optional. Nothing here runs automatically.

## 1. Look first (read-only)

```sql
SELECT column_name FROM information_schema.columns WHERE table_name = 'bar_prices';
-- then, with the column names that query shows (symbol, date, source):
SELECT symbol, max(date) AS last_bar, count(*) AS bars
FROM bar_prices WHERE source ILIKE 'alpaca%'
GROUP BY symbol HAVING max(date) < current_date - 60 ORDER BY last_bar;
```

A symbol whose last Alpaca bar is more than 60 days old is a delisted or reused ticker:
its bars cannot be re-pulled into the right company. Check the list before any delete.

## 2. Only if the list is clean

Delete just those symbols' Alpaca rows in a transaction, compare the count with step 1,
then COMMIT or ROLLBACK. Do not touch `price_cache`, which refills itself.
