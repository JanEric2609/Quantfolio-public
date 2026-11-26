# WRDS: the IBES analyst-consensus extract

How to refresh the "IBES analyst consensus" extract that Settings -> Status
shows as out of date. It cannot run by itself: WRDS is an institutional login
(your university account, with its own second factor), and the app never holds
that login. The exception to "nothing happens until you refresh" is built in:
while the extract is stale, Discover's estimate-revision signal reads Yahoo's
live consensus (the nightly `analyst_estimates_snapshot` job, 04:45 UTC). What
the stale extract costs you is the earnings-surprise figure (SUE) and the
dispersion, which only IBES has.

## When

The extract goes stale 92 days after its newest `statpers` month. IBES adds a
consensus snapshot every month, so a pull every quarter keeps it current. It is
a 10-minute job once WRDS is set up.

## The query

WRDS -> Get Data -> **IBES** -> **Summary History** -> **Summary Statistics**
(table `ibes.statsum_epsus`):

| Setting | Value |
|---|---|
| Date range | `1990-01` to the latest month offered (older rows are never read) |
| Measure | `EPS` |
| Forecast period indicator (FPI) | `1` (next fiscal year) |
| Output | comma-delimited CSV, gzip-compressed if offered; Date Format `YYMMDDs10.` (the loader reads any of the offered formats) |

Variables: `ticker cusip oftic statpers fpedats numest medest meanest stdev
highest lowest actual anndats_act curr usfirm`. The web form does not offer
`fpi` and `measure` as output columns; the loader stamps them for you (below).

### Same query in Python (alternative to the form)

From any machine with the `wrds` package and your WRDS login:

```python
import wrds

db = wrds.Connection()
df = db.raw_sql("""
    select ticker, cusip, oftic, statpers, fpedats, numest, medest, meanest,
           stdev, highest, lowest, actual, anndats_act, usfirm
    from ibes.statsum_epsus
    where measure = 'EPS' and fpi = '1' and statpers >= '1990-01-01'
""", date_cols=["statpers", "fpedats", "anndats_act"])
df.to_csv("ibes_eps_fy1.csv.gz", index=False, date_format="%Y-%m-%d")
```

The query leaves out `curr`, which is optional (only `ticker`, `statpers` and
`fpedats` are required). `db.describe_table("ibes", "statsum_epsus")` lists the
table's real column names if you want to add it.

## Loading it on LXC 111

```bash
cd /opt/quantfolio/backend && set -a && . ../.env && set +a
PANEL=$(.venv/bin/python -c "from app.foundation.data_engineering.paths import get_panel_dir; print(get_panel_dir())")

# 1. Dry run: validates and prints the row and ticker counts.
.venv/bin/python -m app.foundation.data_engineering.ibes_estimates_loader /path/to/ibes_eps_fy1.csv.gz --fpi 1 --measure EPS

# 2. Move the old extract aside (not deleted; move back to undo).
mv "$PANEL/ibes_estimates_pit" "$PANEL/ibes_estimates_pit.before-$(date +%F)"

# 3. Load.
.venv/bin/python -m app.foundation.data_engineering.ibes_estimates_loader /path/to/ibes_eps_fy1.csv.gz --fpi 1 --measure EPS --apply
```

Then Settings -> Status -> Research extracts shows the new newest date, and
the warning goes away on the next self-check (06:00 UTC).
