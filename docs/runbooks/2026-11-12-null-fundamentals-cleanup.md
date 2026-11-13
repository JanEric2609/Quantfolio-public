# Cleanup: stored fundamentals rows with no value (WP1, 2026-11-12)

After WP1 an unknown ticker stores nothing. Rows written before the fix can still
hold only `null` values. Run this by hand on prod after the deploy; nothing here runs
automatically. The prod database is `SQL_ASCII`, so the statements match the stored
text with a regular expression and never cast `data_json` to `json`.

The pattern matches an object whose every value is `null` (and `{}`):
`^\{(\s*"[^"]*"\s*:\s*null\s*,?)*\s*\}$`

## 1. Look first

```sql
SELECT ticker, source, fetched_at, left(data_json, 80)
FROM fundamentals
WHERE data_json ~ '^\{(\s*"[^"]*"\s*:\s*null\s*,?)*\s*\}$'
ORDER BY fetched_at DESC;
```

Check that no row you care about is in the list (a real company with a temporary
provider outage shows up here too; it is refetched on the next request).

## 2. Delete

```sql
BEGIN;
DELETE FROM fundamentals
WHERE data_json ~ '^\{(\s*"[^"]*"\s*:\s*null\s*,?)*\s*\}$';
-- compare the count with step 1, then COMMIT or ROLLBACK
COMMIT;
```

`fundamentals_snapshots` is append-only history for the ML work; do not touch it.
