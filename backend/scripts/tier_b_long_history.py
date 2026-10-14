"""Tier B long-history harness (ADR 0020, plan 2026-10-12 WP12). NOT RUN YET.

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
        market, SMB, HML, momentum, and the risk-free rate. MSCI index levels
        are a manual export (MSCI end-of-day history) saved as
        DIR/msci_<region>.csv with columns month,level.

    .venv/bin/python scripts/tier_b_long_history.py run --data DIR
        Apply ADR 0020's pass bar and print one line per trial. Data after
        2025-12 are cut off, so the live period stays clean.
"""
from __future__ import annotations

import argparse
import getpass
import io
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
    import psycopg2

    user = os.environ.get("WRDS_USERNAME") or input("WRDS username: ")
    password = None if Path.home().joinpath(".pgpass").exists() else getpass.getpass("WRDS password: ")
    conn = psycopg2.connect(host=WRDS_HOST, port=WRDS_PORT, dbname="wrds", user=user, password=password,
                            sslmode="require")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        msi = pd.read_sql("SELECT date, vwretd FROM crsp.msi WHERE date <= '2025-12-31' ORDER BY date", conn)
        mcti = pd.read_sql("SELECT caldt, t30ret FROM crsp.mcti WHERE caldt <= '2025-12-31' ORDER BY caldt", conn)
    finally:
        conn.close()
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


def cmd_run(args: argparse.Namespace) -> int:
    data = Path(args.data)
    regions = {"us": "us", "developed_ex_us": "developed_ex_us", "europe": "europe"}
    ff = {r: _load(data, f"{key}_ff3") for r, key in regions.items()}
    mom = {r: _load(data, f"{key}_mom").iloc[:, 0] for r, key in regions.items()}
    verdicts = []

    # B1: trend overlay on the market (Mkt-RF + RF), cash = RF.
    for trial, months in (("B1_trend_10m", 10), ("B1_trend_200d", 9)):  # 200 trading days ~ 9.5 months
        oos: dict[str, pd.Series] = {}
        is_ex = pd.Series(dtype=float)
        b1_stats: dict[str, float] = {}
        for region, df in ff.items():
            market = df["Mkt-RF"] + df["RF"]
            res = tier_b.trend_overlay(market, df["RF"], window=months)
            excess = res["net"] - market.reindex(res.index)
            if region == "us":
                is_ex = tier_b.window(excess, tier_b.IN_SAMPLE)
                oos_net = tier_b.window(res["net"], tier_b.OUT_OF_SAMPLE)
                oos_bh = tier_b.window(market.reindex(res.index), tier_b.OUT_OF_SAMPLE)
                b1_stats = {"mdd": tier_b.max_drawdown(oos_net), "mdd_bh": tier_b.max_drawdown(oos_bh),
                            "cagr": tier_b.cagr(oos_net), "cagr_bh": tier_b.cagr(oos_bh)}
            oos[region] = tier_b.window(excess, tier_b.OUT_OF_SAMPLE)
        verdicts.append(tier_b.evaluate(trial, is_ex, oos, published=True, b1=b1_stats))

    # B3-B5: factor returns (long-short), out of sample in every region.
    for trial, col in (("B3_size", "SMB"), ("B4_value", "HML"), ("B5_momentum", None)):
        series = {r: (mom[r] if col is None else ff[r][col]) for r in regions}
        verdicts.append(tier_b.evaluate(
            trial, tier_b.window(series["us"], tier_b.IN_SAMPLE),
            {r: tier_b.window(s, tier_b.OUT_OF_SAMPLE) for r, s in series.items()}, published=True,
        ))
    print("B2_min_vol: needs low-volatility portfolio returns (French 'Portfolios formed on variance'); "
          "add the file and the column before running it.")

    for v in verdicts:
        print(f"{v.trial:16} {'PASS' if v.passed else 'no  '} {v.stats} {'; '.join(v.reasons)}")
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
