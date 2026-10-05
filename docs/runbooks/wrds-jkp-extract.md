# WRDS: the JKP characteristics extract

What to pull from WRDS for the factor-premia evidence (Phase 3, step 1),
the cross-sectional model (step 2), the satellite simulation (step 3) and the
DSR/PBO gate (step 4), and
how to load it. The column list is
the one in `backend/app/foundation/data_engineering/wrds_factors_schema.py`
(`JKP_CHARACTERISTICS_BY_THEME`); keep the two in sync.

## Why this extract

The panel on prod today mixes two extracts. The first covers 1949–2025 but has
no momentum column. The second has momentum but starts in 2015. Step 1
therefore saw only 132 months of momentum, so Momentum and Value + momentum
failed the 20-year history check whatever their returns were. Step 2 needs
more than the four characteristics loaded so far. This extract fixes both: one
consistent pull with full history and 41 characteristics, two to five from
each of JKP's 13 themes.

## The query

WRDS → Get Data → Contributed Data Forms → **Global Factor Data** →
**Global Stock Returns and Characteristics** (the same form as before).

| Setting | Value |
|---|---|
| Date range | `1963-01` to the latest month offered |
| Company codes | Search the entire database |
| Countries (`excntry`) | `AUS AUT BEL CAN CHE DEU DNK ESP FIN FRA GBR HKG IRL ISR ITA JPN NLD NOR NZL PRT SGP SWE USA` (the 23 MSCI World markets) |
| Size groups (`size_grp`) | `mega`, `large`, `small` (drop `micro` and `nano`: the research code excludes them anyway, and this halves the file) |
| Security filters | primary security, common stock, main exchange, one observation per month (same as the last extract) |
| Output | comma-delimited CSV, **gzip-compressed** if offered, dates as `YYYY-MM-DD` |

Variables: tick exactly these 49. The first 8 are identifiers and the target;
the rest are the characteristics, by theme.

```
id,gvkey,eom,excntry,size_grp,me,ret_exc_lead1m,gics,oaccruals_at,taccruals_at,noa_at,debt_gr3,at_gr1,sale_gr1,capx_gr1,noa_gr1a,ppeinv_gr1a,at_be,cash_at,netdebt_me,beta_60m,betabab_1260d,ivol_capm_252d,rvol_21d,ret_12_1,ret_6_1,resff3_12_1,prc_highprc_252d,niq_su,saleq_su,niq_at_chg1,ope_be,ni_be,ocf_at,gp_at,op_at,cop_at,qmj,seas_2_5an,dbnetis_at,ret_1_0,rmax5_rvol_21d,dolvol_126d,ami_126d,be_me,bev_mev,ni_me,ocf_me,eqnpo_me
```

| JKP theme | Characteristics |
|---|---|
| Accruals | `oaccruals_at` `taccruals_at` |
| Debt issuance | `noa_at` `debt_gr3` |
| Investment | `at_gr1` `sale_gr1` `capx_gr1` `noa_gr1a` `ppeinv_gr1a` |
| Low leverage | `at_be` `cash_at` `netdebt_me` |
| Low risk | `beta_60m` `betabab_1260d` `ivol_capm_252d` `rvol_21d` |
| Momentum | `ret_12_1` `ret_6_1` `resff3_12_1` `prc_highprc_252d` |
| Profit growth | `niq_su` `saleq_su` `niq_at_chg1` |
| Profitability | `ope_be` `ni_be` `ocf_at` |
| Quality | `gp_at` `op_at` `cop_at` `qmj` |
| Seasonality | `seas_2_5an` `dbnetis_at` |
| Short-term reversal | `ret_1_0` `rmax5_rvol_21d` |
| Size | `dolvol_126d` `ami_126d` (plus `me`) |
| Value | `be_me` `bev_mev` `ni_me` `ocf_me` `eqnpo_me` |

The theme assignment is JKP's own (`GlobalFactors/Cluster Labels.csv` in
bkelly-lab/ReplicationCrisis). Two of them are surprising, but they are JKP's:
`gp_at` sits under Quality and `dbnetis_at` under Seasonality.

Rough size: about 3–4 million rows. That is roughly 1.5–2 GB as CSV, and a few
hundred MB gzipped.

### Same query in Python (alternative to the form)

If the form cannot filter by country or size, run this from any machine that
has the `wrds` package and your WRDS login. If the table name differs,
`db.list_tables(library="contrib")` shows it.

```python
import wrds

cols = "id,gvkey,eom,excntry,size_grp,me,ret_exc_lead1m,gics,oaccruals_at,taccruals_at,noa_at,debt_gr3,at_gr1,sale_gr1,capx_gr1,noa_gr1a,ppeinv_gr1a,at_be,cash_at,netdebt_me,beta_60m,betabab_1260d,ivol_capm_252d,rvol_21d,ret_12_1,ret_6_1,resff3_12_1,prc_highprc_252d,niq_su,saleq_su,niq_at_chg1,ope_be,ni_be,ocf_at,gp_at,op_at,cop_at,qmj,seas_2_5an,dbnetis_at,ret_1_0,rmax5_rvol_21d,dolvol_126d,ami_126d,be_me,bev_mev,ni_me,ocf_me,eqnpo_me"
countries = "','".join("AUS AUT BEL CAN CHE DEU DNK ESP FIN FRA GBR HKG IRL ISR ITA JPN NLD NOR NZL PRT SGP SWE USA".split())
db = wrds.Connection()
df = db.raw_sql(f"""
    select {cols} from contrib.global_factor
    where eom >= '1963-01-01'
      and excntry in ('{countries}')
      and size_grp in ('mega', 'large', 'small')
      and primary_sec = 1 and common = 1 and exch_main = 1 and obs_main = 1
""", date_cols=["eom"])
df.to_csv("jkp_world_wide.csv.gz", index=False, date_format="%Y-%m-%d")
```

## Loading it on LXC 111

```bash
cd /opt/quantfolio/backend && set -a && . ../.env && set +a
PANEL=$(.venv/bin/python -c "from app.foundation.data_engineering.paths import get_panel_dir; print(get_panel_dir())")
echo "$PANEL"

# 1. Dry run: validates and lists any characteristic the extract is missing.
.venv/bin/python -m app.foundation.data_engineering.wrds_factors_loader /path/to/jkp_world_wide.csv.gz

# 2. Move the two old extracts aside (not deleted; move back to undo).
mv "$PANEL/wrds_factor_characteristics_pit" "$PANEL/wrds_factor_characteristics_pit.pre-2026-09-24"

# 3. Load.
.venv/bin/python -m app.foundation.data_engineering.wrds_factors_loader /path/to/jkp_world_wide.csv.gz --apply

# 4. Re-run step 1 on the world markets (the default region; the one that
#    gates the tilt), then Europe for comparison. Add --dry-run to only print.
.venv/bin/python -m app.lab.factor_premia
.venv/bin/python -m app.lab.factor_premia --region europe

# 5. Phase 3 step 2: the pooled model (ridge + LightGBM) under walk-forward CV.
#    Writes a month-sorted copy of the universe to $PANEL/derived/ first:
#    up to half the size of wrds_factor_characteristics_pit, so check df -h.
#    Then 10-20 minutes. A new PANEL_VERSION rebuilds that copy by itself.
.venv/bin/python -m app.lab.pooled_model

# 6. Phase 3 step 3: the satellite those scores would pick (3 stocks at DKB,
#    10 at Scalable), net of the order fee, the spread and German tax. Needs step 5's scores file
#    (derived/pooled_model_world_scores.parquet) and one download of the US
#    T-bill rate from Ken French's library. A minute or two.
.venv/bin/python -m app.lab.satellite

# 7. Phase 3 step 4: Deflated Sharpe and PBO over what steps 5 and 6 stored.
#    Reads only (records no trials); a few seconds.
.venv/bin/python -m app.lab.evidence_gate
```

Step 1's dry run should warn `characteristic(s) not in the extract` only if a
box was left unticked. `gics` is the one name not taken from JKP's
characteristic list. If WRDS does not offer it, the loader stores it as null
and nothing else is affected.

Moving the old extracts aside (command 2) is deliberate. The loader
already lets the newest row win for each `(gvkey, eom)`, but the old extracts
also hold micro caps, emerging markets and pre-1963 US rows, none of which
carry the new columns. Keeping them would leave a patchwork panel. The only
thing lost is characteristics for emerging-market and micro-cap symbols in
Discover's per-symbol join, which then reports them as unavailable.
