"""Sector/style diagnostic of the ranking replay (plan 2026-10-14, WP-B1). Read-only.

Usage (on the app host, as the quantfolio user):
    python scripts/sector_style_diagnostic.py data/ranking_replay/exam-v2/composite.parquet

Prints one ``RESULT {json}`` line. Result of 2026-10-14 and its reading:
docs/research/ranking-replay/sector-style-diagnostic.md. Each run is one look
at the data and belongs in the trial ledger (context ``diagnostic``).
"""
import glob
import json
import sys
import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

comp = pd.read_parquet(sys.argv[1])
parts = comp["entity"].str.split(":")
comp["gvkey"] = parts.str[0]
comp["permno"] = pd.to_numeric(parts.str[1], errors="coerce")
comp["m"] = pd.to_datetime(comp["period"]).dt.to_period("M").dt.to_timestamp()
con = duckdb.connect(config={"memory_limit": "1500MB", "threads": "2"})
files = sorted(glob.glob("/opt/quantfolio/data/parquet_panel/wrds_factor_characteristics_pit/*.parquet"))
con.register("keys", comp[["gvkey", "m"]].drop_duplicates())
con.register("jkp", con.read_parquet(files))
chars = con.execute(
    "SELECT j.* FROM ("
    " SELECT gvkey, permno, date_trunc('month', eom) AS m, last(me ORDER BY ingested_at) me,"
    " last(be_me ORDER BY ingested_at) be_me, last(mom_12_1 ORDER BY ingested_at) mom_12_1,"
    " last(beta_60m ORDER BY ingested_at) beta_60m, last(gics ORDER BY ingested_at) gics"
    " FROM jkp WHERE excntry = 'USA' GROUP BY 1, 2, 3"
    ") j JOIN keys k ON k.gvkey = j.gvkey AND k.m = j.m"
).df()
cols = ["me", "be_me", "mom_12_1", "beta_60m", "gics"]
by_permno = chars.dropna(subset=["permno"]).drop_duplicates(["permno", "m"]).set_index(["permno", "m"])[cols]
by_gvkey = chars.drop_duplicates(["gvkey", "m"]).set_index(["gvkey", "m"])[cols]
a = comp.join(by_permno, on=["permno", "m"])
b = comp.join(by_gvkey, on=["gvkey", "m"])
df = a.copy()
for c in cols:
    df[c] = a[c].where(a[c].notna(), b[c])
out = {"months": 0, "rows": len(df), "matched_share": float(df["gics"].notna().mean())}
raw_ic, res_ic, ns = [], [], []
for period, g in df.groupby("period"):
    g = g.dropna(subset=["score", "forward", "gics", "me", "be_me", "mom_12_1", "beta_60m"])
    if len(g) < 50:
        continue
    X = pd.DataFrame({
        "size": np.log(g["me"].clip(lower=1e-6)), "value": g["be_me"], "mom": g["mom_12_1"], "beta": g["beta_60m"],
    })
    X = (X - X.mean()) / X.std(ddof=0).replace(0, 1)
    sector = (g["gics"] // 1000000).astype(int).astype(str)
    D = pd.get_dummies(sector, drop_first=False, dtype=float)
    A = np.column_stack([D.values, X.values])
    beta, *_ = np.linalg.lstsq(A, g["score"].values, rcond=None)
    resid = g["score"].values - A @ beta
    raw_ic.append(spearmanr(g["score"], g["forward"]).statistic)
    res_ic.append(spearmanr(resid, g["forward"]).statistic)
    ns.append(len(g))
raw, res = np.array(raw_ic), np.array(res_ic)
n_mean = float(np.mean(ns))
out.update({
    "months": len(raw), "stocks_per_month": n_mean,
    "raw_ic_mean": float(raw.mean()), "raw_ic_sd": float(raw.std(ddof=1)),
    "resid_ic_mean": float(res.mean()), "resid_ic_sd": float(res.std(ddof=1)),
    "sampling_only_sd": float(1 / np.sqrt(n_mean - 1)),
    "resid_ic_t": float(res.mean() / (res.std(ddof=1) / np.sqrt(len(res)))),
    "raw_ic_t": float(raw.mean() / (raw.std(ddof=1) / np.sqrt(len(raw)))),
    "gate_resid_sd_le_0_12": bool(res.std(ddof=1) <= 0.12),
})
print("RESULT " + json.dumps(out))
