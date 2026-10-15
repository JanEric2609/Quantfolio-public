"""Run ADR 0022's pre-registered savings-plan rules on JST R.6 (plan 2026-10-14, WP-C).

Usage (from backend/, with the local data file):
    .venv/bin/python scripts/plan_rules_long_history.py ../data/long_history/jst_r6/JSTdatasetR6.dta \\
        --out ../data/long_history/results/plan_rules.json

Prints the results table for ADR 0022's "Results" section. Nothing is written to
the database; the six trial_ledger rows are recorded separately with the owner's OK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lab.long_history import COUNTRIES, RULES, PlanCosts, home_panel, load_jst  # noqa: E402
from app.lab.long_history.plan_bootstrap import (  # noqa: E402
    bootstrap_indices,
    certainty_equivalent,
    haircut,
    simulate_rule,
    utility_moment,
)

SEED = 20261014
N_PATHS = 10_000
GAMMAS = (3.0, 5.0, 10.0)
PRIMARY_GAMMA = 5.0
PRIMARY_HORIZON = 30
WAR_YEARS = set(range(1914, 1919)) | set(range(1939, 1946))


def run(panels: dict, *, horizon: int, costs: PlanCosts, cut: bool, countries: tuple[str, ...],
        keep_year=lambda y: True, n_paths: int = N_PATHS) -> dict:
    """Per country and rule: utility moments, shortfall, drawdowns (paired paths across rules)."""
    out: dict = {}
    for i, c in enumerate(countries):
        years, returns = panels[c]
        if cut:
            returns = haircut(returns)
        pool = np.array([k for k, y in enumerate(years) if keep_year(int(y))])
        if len(pool) < 20:
            continue
        rng = np.random.default_rng(SEED + 1000 * horizon + i)
        idx = bootstrap_indices(pool, n_paths, horizon, rng)
        contributed = 12.0 * horizon
        res = {}
        for rule in RULES:
            sim = simulate_rule(returns, idx, rule, horizon, costs)
            w = sim["wealth"]
            res[rule] = {
                "moments": {str(g): utility_moment(w, g) for g in GAMMAS},
                "ce": {str(g): certainty_equivalent(w, g) for g in GAMMAS},
                "p_shortfall": float(np.mean(w < contributed)),
                "median_wealth": float(np.median(w)),
                "max_dd_median": float(np.median(sim["max_dd"])),
                "max_dd_p90": float(np.quantile(sim["max_dd"], 0.9)),
            }
            if "withdrawal_short" in sim:
                res[rule]["p_withdrawal_short"] = float(np.mean(sim["withdrawal_short"]))
        out[c] = res
    return out


def pooled_delta(res: dict, rule: str, gamma: float, countries: list[str] | None = None) -> float:
    cs = countries if countries is not None else list(res)
    g = str(gamma)
    m_rule = np.mean([res[c][rule]["moments"][g] for c in cs])
    m_base = np.mean([res[c]["R0"]["moments"][g] for c in cs])
    p = 1.0 / (1.0 - gamma)
    return float(m_rule ** p / m_base ** p - 1.0)


def country_deltas(res: dict, rule: str, gamma: float) -> dict[str, float]:
    g = str(gamma)
    return {c: res[c][rule]["ce"][g] / res[c]["R0"]["ce"][g] - 1.0 for c in res}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data")
    parser.add_argument("--out", required=True)
    parser.add_argument("--paths", type=int, default=N_PATHS)
    args = parser.parse_args()

    digest = hashlib.sha256(Path(args.data).read_bytes()).hexdigest()
    raw = load_jst(args.data)
    panels = {}
    for c in COUNTRIES:
        p = home_panel(raw, c)
        panels[c] = (p.index.to_numpy(), p.to_numpy())
    primary = PlanCosts()
    common = {"costs": primary, "cut": True, "countries": COUNTRIES, "n_paths": args.paths}

    results: dict = {"data_sha256": digest, "seed": SEED, "paths": args.paths,
                     "years_per_country": {c: [int(panels[c][0].min()), int(panels[c][0].max()), len(panels[c][0])]
                                           for c in COUNTRIES}}
    try:
        results["code_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                             check=False).stdout.strip()
    except OSError:
        results["code_sha"] = None
    base = {h: run(panels, horizon=h, **common) for h in (10, 20, PRIMARY_HORIZON)}
    sens = {
        "no_tax": run(panels, horizon=PRIMARY_HORIZON, costs=PlanCosts(taxed=False), cut=True,
                      countries=COUNTRIES, n_paths=args.paths),
        "no_haircut": run(panels, horizon=PRIMARY_HORIZON, costs=primary, cut=False, countries=COUNTRIES,
                          n_paths=args.paths),
        "years_1870_1949": run(panels, horizon=PRIMARY_HORIZON, keep_year=lambda y: y <= 1949, **common),
        "years_1950_2020": run(panels, horizon=PRIMARY_HORIZON, keep_year=lambda y: y >= 1950, **common),
        "drop_japan": run(panels, horizon=PRIMARY_HORIZON, costs=primary, cut=True,
                          countries=tuple(c for c in COUNTRIES if c != "Japan"), n_paths=args.paths),
        "drop_germany": run(panels, horizon=PRIMARY_HORIZON, costs=primary, cut=True,
                            countries=tuple(c for c in COUNTRIES if c != "Germany"), n_paths=args.paths),
        "drop_war_years": run(panels, horizon=PRIMARY_HORIZON, keep_year=lambda y: y not in WAR_YEARS, **common),
    }
    decades = range(1870, 2021, 10)
    sens_decades = {
        d: run(panels, horizon=PRIMARY_HORIZON, keep_year=lambda y, d=d: not (d <= y < d + 10), **common)
        for d in decades
    }

    rng = np.random.default_rng(SEED)
    main_res = base[PRIMARY_HORIZON]
    summary = {}
    for rule in RULES[1:]:
        full = pooled_delta(main_res, rule, PRIMARY_GAMMA)
        per_country = country_deltas(main_res, rule, PRIMARY_GAMMA)
        names = list(main_res)
        boot = [pooled_delta(main_res, rule, PRIMARY_GAMMA, list(rng.choice(names, len(names), replace=True)))
                for _ in range(10_000)]
        lo, hi = float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))
        halves = {k: pooled_delta(sens[k], rule, PRIMARY_GAMMA) for k in ("years_1870_1949", "years_1950_2020")}
        drops = {k: pooled_delta(sens[k], rule, PRIMARY_GAMMA) for k in ("drop_japan", "drop_germany", "drop_war_years")}
        by_decade = {d: pooled_delta(sens_decades[d], rule, PRIMARY_GAMMA) for d in decades}
        checks = {
            "1_interval_excludes_0_and_positive": lo > 0.0,
            "2_positive_in_12_of_16": sum(v > 0 for v in per_country.values()) >= 12,
            "3_positive_in_both_halves": all(v > 0 for v in halves.values()),
            "4_survives_three_drops": all(v > 0 for v in drops.values()),
            "5_no_decade_supplies_half": full > 0 and all(v >= 0.5 * full for v in by_decade.values()),
        }
        summary[rule] = {
            "delta": full, "ci95": [lo, hi], "per_country": per_country,
            "n_positive": sum(v > 0 for v in per_country.values()),
            "halves": halves, "drops": drops, "by_decade": by_decade,
            "delta_gamma": {str(g): pooled_delta(main_res, rule, g) for g in GAMMAS},
            "delta_horizon": {str(h): pooled_delta(base[h], rule, PRIMARY_GAMMA) for h in base},
            "delta_no_tax": pooled_delta(sens["no_tax"], rule, PRIMARY_GAMMA),
            "delta_no_haircut": pooled_delta(sens["no_haircut"], rule, PRIMARY_GAMMA),
            "checks": checks, "passes": all(checks.values()),
        }

    def stat(res: dict, rule: str, key: str) -> float:
        return float(np.mean([res[c][rule][key] for c in res]))

    risk = {rule: {"p_shortfall": stat(main_res, rule, "p_shortfall"),
                   "max_dd_median": stat(main_res, rule, "max_dd_median"),
                   "max_dd_p90": stat(main_res, rule, "max_dd_p90")} for rule in RULES}
    risk["R5"]["p_withdrawal_short"] = stat(main_res, "R5", "p_withdrawal_short")
    results.update({"summary": summary, "risk_30y": risk})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=1, default=float))

    print(f"data {digest[:12]}  code {results['code_sha'][:10] if results['code_sha'] else '?'}  paths {args.paths}")
    print("| Rule | Δ vs R0 (γ 5, 30 y) | 95 % interval | countries > 0 | 1870-1949 | 1950-2020 | "
          "no JP / no DE / no war | no tax | no haircut | P(shortfall) | median max DD | passes |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    r0 = risk["R0"]
    print(f"| R0 | baseline | | | | | | | | {r0['p_shortfall']:.1%} | {r0['max_dd_median']:.0%} | |")
    for rule, s in summary.items():
        d = s["drops"]
        print(
            f"| {rule} | {s['delta']:+.1%} | {s['ci95'][0]:+.1%} to {s['ci95'][1]:+.1%} | {s['n_positive']}/16 | "
            f"{s['halves']['years_1870_1949']:+.1%} | {s['halves']['years_1950_2020']:+.1%} | "
            f"{d['drop_japan']:+.1%} / {d['drop_germany']:+.1%} / {d['drop_war_years']:+.1%} | "
            f"{s['delta_no_tax']:+.1%} | {s['delta_no_haircut']:+.1%} | {risk[rule]['p_shortfall']:.1%} | "
            f"{risk[rule]['max_dd_median']:.0%} | {'yes' if s['passes'] else 'no'} |"
        )


if __name__ == "__main__":
    main()
