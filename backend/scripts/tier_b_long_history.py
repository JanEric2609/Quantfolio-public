"""Tier B long-history harness (ADR 0020, plan 2026-10-12 WP12).

ADR 0020 pre-registers the strategies, periods, costs and pass bars; run this
only after it is accepted, and record every trial first (``register``).

Subcommands (run from backend/):

    .venv/bin/python scripts/tier_b_long_history.py register
        Record the six pre-registered trials in trial_ledger (context tier_b).
        Writes to the configured DATABASE_URL: on prod only with the owner's OK.

    .venv/bin/python scripts/tier_b_long_history.py fetch-crsp --out DIR
        CRSP monthly index (crsp.msi: date, vwretd) and 30-day T-bill
        (crsp.mcti: caldt, t30ret) from WRDS Postgres. Credentials come from
        WRDS_USERNAME and ~/.pgpass (or a getpass prompt); never stored.

    .venv/bin/python scripts/tier_b_long_history.py fetch-french --out DIR
        Ken French factor files (US 1926-, Developed ex US and Europe 1990-):
        market, SMB, HML, momentum, the risk-free rate, and the US portfolios
        formed on variance (B2). The second B2 region is a manual export of
        MSCI's end-of-day history (monthly, EUR, net): MSCI World Minimum
        Volatility and MSCI World, saved as DIR/msci_world_minvol.csv and
        DIR/msci_world.csv (date, level).

    .venv/bin/python scripts/tier_b_long_history.py run --data DIR
        Apply ADR 0020's pass bar and print two lines per trial: taxed (the
        decision) and without tax (NV-Bescheinigung). Writes
        DIR/tier_b_results.json. Data after 2025-12 are cut off, so the live
        period stays clean.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.lab import tier_b  # noqa: E402

WRDS_HOST = "wrds-pgdata.wharton.upenn.edu"
WRDS_PORT = 9737
FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FRENCH_FILES = {
    "us_ff3": "F-F_Research_Data_Factors_CSV.zip",
    "us_mom": "F-F_Momentum_Factor_CSV.zip",
    "developed_ex_us_ff3": "Developed_ex_US_3_Factors_CSV.zip",
    "developed_ex_us_mom": "Developed_ex_US_Mom_Factor_CSV.zip",
    "europe_ff3": "Europe_3_Factors_CSV.zip",
    "europe_mom": "Europe_Mom_Factor_CSV.zip",
    "us_var": "Portfolios_Formed_on_VAR_CSV.zip",
}
LAST_MONTH = "2025-12"


def cmd_register(_args: argparse.Namespace) -> int:
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        print(f"registered {tier_b.register_trials(db)} trials in trial_ledger (context tier_b)")
    finally:
        db.close()
    return 0


def cmd_fetch_crsp(args: argparse.Namespace) -> int:
    import psycopg

    user = os.environ.get("WRDS_USERNAME") or input("WRDS username: ")
    password = None if Path.home().joinpath(".pgpass").exists() else getpass.getpass("WRDS password: ")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # psycopg 3 (the app's driver); pandas reads it through a plain cursor.
    with psycopg.connect(host=WRDS_HOST, port=WRDS_PORT, dbname="wrds", user=user, password=password,
                         sslmode="require") as conn:
        def query(sql: str) -> pd.DataFrame:
            with conn.cursor() as cur:
                cur.execute(sql)
                return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description or []])
        msi = query("SELECT date, vwretd FROM crsp.msi WHERE date <= '2025-12-31' ORDER BY date")
        mcti = query("SELECT caldt, t30ret FROM crsp.mcti WHERE caldt <= '2025-12-31' ORDER BY caldt")
    msi.to_parquet(out / "crsp_msi.parquet")
    mcti.to_parquet(out / "crsp_mcti.parquet")
    print(f"crsp.msi {len(msi)} months, crsp.mcti {len(mcti)} months -> {out}")
    return 0


def _french_monthly(raw: bytes) -> pd.DataFrame:
    """The monthly block of a Ken French CSV zip, in fractions, indexed by month."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith(",") or ln.lower().startswith(",mkt"))
    rows = []
    for ln in lines[start + 1:]:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 2 or not parts[0].isdigit() or len(parts[0]) != 6:
            if rows:
                break
            continue
        rows.append(parts)
    header = [h.strip() for h in lines[start].split(",")][1:]
    df = pd.DataFrame([r[1:] for r in rows], columns=header, index=[r[0] for r in rows]).astype(float) / 100.0
    df.index = pd.PeriodIndex([f"{i[:4]}-{i[4:]}" for i in df.index], freq="M")
    return df


def cmd_fetch_french(args: argparse.Namespace) -> int:
    import httpx

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, filename in FRENCH_FILES.items():
        resp = httpx.get(FRENCH_BASE + filename, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        df = _french_monthly(resp.content)
        df.index = df.index.astype(str)
        df.to_parquet(out / f"french_{name}.parquet")
        print(f"{name}: {len(df)} months {df.index[0]}..{df.index[-1]}")
    return 0


def _load(data: Path, name: str) -> pd.DataFrame:
    df = pd.read_parquet(data / f"french_{name}.parquet")
    df.index = pd.PeriodIndex(df.index, freq="M")
    return df[df.index <= pd.Period(LAST_MONTH, "M")]


def _msci_returns(path: Path) -> pd.Series:
    """Monthly returns from an MSCI end-of-day history export saved as CSV.

    Takes the first column that parses as a date and the first numeric column
    after it (MSCI's header and footer lines are skipped), keeps the last
    level of each month and returns month-over-month changes.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if len(r) >= 2]
    raw = pd.DataFrame([r[:2] for r in rows], columns=["date", "level"])
    dates = pd.to_datetime(raw["date"].str.strip(), errors="coerce", format="mixed")
    levels = pd.to_numeric(raw["level"].str.replace(",", "", regex=False).str.strip(), errors="coerce")
    df = pd.DataFrame({"date": dates, "level": levels}).dropna()
    if len(df) < 24:
        raise SystemExit(f"{path}: fewer than 24 dated rows; save the MSCI export as CSV with date,level")
    monthly = df.sort_values("date").groupby(df["date"].dt.to_period("M"))["level"].last()
    rets = monthly.pct_change().dropna()
    return rets[rets.index <= pd.Period(LAST_MONTH, "M")]


def _line(v: tier_b.Verdict, label: str) -> str:
    return f"{v.trial:16} {label:7} {'PASS' if v.passed else 'no  '} {v.stats} {'; '.join(v.reasons)}"


def cmd_run(args: argparse.Namespace) -> int:
    data = Path(args.data)
    regions = {"us": "us", "developed_ex_us": "developed_ex_us", "europe": "europe"}
    ff = {r: _load(data, f"{key}_ff3") for r, key in regions.items()}
    mom = {r: _load(data, f"{key}_mom").iloc[:, 0] for r, key in regions.items()}
    market = {r: df["Mkt-RF"] + df["RF"] for r, df in ff.items()}
    # Each trial is decided on the taxed line; the NV line (no tax) is reported beside it.
    results: list[tuple[tier_b.Verdict, tier_b.Verdict]] = []

    crsp = data / "crsp_msi.parquet"
    if crsp.exists():
        msi = pd.read_parquet(crsp)
        msi.index = pd.PeriodIndex(pd.to_datetime(msi["date"]), freq="M")
        both = pd.concat([msi["vwretd"], market["us"]], axis=1, join="inner").dropna()
        print(f"cross-check: CRSP vwretd vs French US market, {len(both)} months, "
              f"correlation {both.corr().iloc[0, 1]:.4f}")

    # B1: trend overlay on the market (Mkt-RF + RF), cash = RF.
    for trial, months in (("B1_trend_10m", 10), ("B1_trend_200d", 9)):  # 200 trading days ~ 9.5 months
        pair = []
        for tax in (tier_b.GAIN_TAX_RATE, 0.0):
            oos: dict[str, pd.Series] = {}
            is_ex = pd.Series(dtype=float)
            b1_stats: dict[str, float] = {}
            for region, df in ff.items():
                res = tier_b.trend_overlay(market[region], df["RF"], window=months, gain_tax_rate=tax)
                excess = res["net"] - market[region].reindex(res.index)
                if region == "us":
                    is_ex = tier_b.window(excess, tier_b.IN_SAMPLE)
                    oos_net = tier_b.window(res["net"], tier_b.OUT_OF_SAMPLE)
                    oos_bh = tier_b.window(market[region].reindex(res.index), tier_b.OUT_OF_SAMPLE)
                    b1_stats = {"mdd": tier_b.max_drawdown(oos_net), "mdd_bh": tier_b.max_drawdown(oos_bh),
                                "cagr": tier_b.cagr(oos_net), "cagr_bh": tier_b.cagr(oos_bh)}
                oos[region] = tier_b.window(excess, tier_b.OUT_OF_SAMPLE)
            pair.append(tier_b.evaluate(trial, is_ex, oos, publication=tier_b.publication_year(trial), b1=b1_stats))
        results.append((pair[0], pair[1]))

    # B2-B5: a factor tilt's excess over the market, after implementation cost (and tax).
    factor: dict[str, dict[str, pd.Series]] = {
        "B3_size": {r: ff[r]["SMB"] for r in regions},
        "B4_value": {r: ff[r]["HML"] for r in regions},
        "B5_momentum": dict(mom),
    }
    var_file = data / "french_us_var.parquet"
    msci_mv, msci_w = data / "msci_world_minvol.csv", data / "msci_world.csv"
    if var_file.exists() and msci_mv.exists() and msci_w.exists():
        low_var = _load(data, "us_var")["Lo 20"]
        mv, w = _msci_returns(msci_mv), _msci_returns(msci_w)
        factor["B2_min_vol"] = {"us": low_var - market["us"].reindex(low_var.index),
                                "world_msci_eur": (mv - w.reindex(mv.index)).dropna()}
    else:
        print("B2_min_vol: not run; needs french_us_var.parquet (fetch-french) and "
              "msci_world_minvol.csv + msci_world.csv in the data directory.")
    for trial in ("B2_min_vol", "B3_size", "B4_value", "B5_momentum"):
        if trial not in factor:
            continue
        series = factor[trial]
        pair = []
        for taxed in (True, False):
            net = {r: tier_b.tilt_net(s, taxed=taxed) for r, s in series.items()}
            pair.append(tier_b.evaluate(
                trial, tier_b.window(net["us"], tier_b.IN_SAMPLE),
                {r: tier_b.window(s, tier_b.OUT_OF_SAMPLE) for r, s in net.items()},
                publication=tier_b.publication_year(trial),
            ))
        results.append((pair[0], pair[1]))

    for taxed_v, nv_v in results:
        print(_line(taxed_v, "taxed"))
        print(_line(nv_v, "no tax"))
    out = data / "tier_b_results.json"
    out.write_text(json.dumps([
        {"trial": t.trial, "passed": t.passed, "reasons": t.reasons, "stats": t.stats,
         "no_tax": {"passed": n.passed, "reasons": n.reasons, "stats": n.stats}}
        for t, n in results
    ], indent=2, default=float))
    print(f"stored: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("register")
    p = sub.add_parser("fetch-crsp")
    p.add_argument("--out", required=True)
    p = sub.add_parser("fetch-french")
    p.add_argument("--out", required=True)
    p = sub.add_parser("run")
    p.add_argument("--data", required=True)
    args = parser.parse_args()
    return {"register": cmd_register, "fetch-crsp": cmd_fetch_crsp, "fetch-french": cmd_fetch_french,
            "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
