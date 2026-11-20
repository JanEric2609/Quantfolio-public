"""ADR-0004 spike harness: RegimeHMM vs JumpRegimeModel on synthetic regimes.

Runs the time-boxed, evidence-producing comparison behind
docs/adr/0004-regime-jump-model-spike.md. Pure offline script — no DB, no
API, no live path. Usage:

    cd backend && .venv/bin/python scripts/jump_spike_eval.py

Scenarios: three synthetic price series composed of blocks with distinct
drift/volature profiles (trending-up low-vol, choppy mid-vol, high-vol
crash with drawdown), plus a sliding-window refit-stability probe that
measures how often consecutive refits disagree on overlapping rows.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from app.lab.regime.hmm_model import RegimeHMM
from app.lab.regime.jump_model import JumpRegimeModel

SEED = 42
N_INIT_ROWS = 63  # feature warmup dropped before fitting (vol/drawdown windows)

# (name, n_rows, daily drift, daily vol) per block; concatenated per scenario.
SCENARIOS: dict[str, list[tuple[str, int, float, float]]] = {
    "A_bull_chop_crash": [
        ("bull", 400, 0.0015, 0.005),
        ("sideways", 300, 0.0000, 0.010),
        ("bear", 300, -0.0035, 0.025),
    ],
    "B_v_shaped_recovery": [
        ("bear", 350, -0.0030, 0.022),
        ("sideways", 250, 0.0002, 0.011),
        ("bull", 400, 0.0020, 0.006),
    ],
    "C_grind_up_then_break": [
        ("bull", 500, 0.0008, 0.006),
        ("sideways", 350, 0.0000, 0.012),
        ("bear", 250, -0.0045, 0.030),
    ],
}


def build_scenario(
    blocks: list[tuple[str, int, float, float]], seed: int
) -> tuple[pd.DataFrame, pd.Series]:
    """Return production-shaped features and ground-truth labels for a scenario."""
    rng = np.random.default_rng(seed)
    rets, truth = [], []
    for name, n, drift, vol in blocks:
        rets.append(rng.normal(drift, vol, n))
        truth.extend([name] * n)

    close = 100.0 * np.cumprod(1.0 + np.concatenate(rets))
    df = pd.DataFrame({"close": close})
    df["ret"] = df["close"].pct_change()
    df["vol"] = df["ret"].rolling(21).std()
    df["drawdown"] = df["close"] / df["close"].rolling(63, min_periods=1).max() - 1
    df = df.dropna().reset_index(drop=True)

    truth = truth[len(truth) - len(df):]
    return df[["ret", "vol", "drawdown"]], pd.Series(truth)


def fit_predict(model: object, features: pd.DataFrame) -> tuple[list[str], float]:
    """Fit on the full frame; return (labels, wall-clock seconds)."""
    t0 = time.perf_counter()
    model.fit(features)  # type: ignore[attr-defined]
    elapsed = time.perf_counter() - t0
    return model.predict(features), elapsed  # type: ignore[attr-defined]


def refit_flip_rate(model_factory: object, features: pd.DataFrame) -> float:
    """Mean label flip rate between consecutive sliding-window refits.

    Windows cover 60% of the frame, advanced in 4 equal steps; each pair of
    consecutive windows overlaps by 3/4 of the window. The flip rate is the
    fraction of overlapping rows whose predicted label changes between the
    two refits — a direct measure of how much a scheduled refit churns the
    historical regime series.
    """
    n = len(features)
    window = int(n * 0.60)
    starts = list(range(0, n - window + 1, max((n - window) // 4, 1)))
    label_seqs = []
    for start in starts:
        sub = features.iloc[start : start + window]
        model = model_factory()  # type: ignore[operator]
        model.fit(sub)  # type: ignore[attr-defined]
        label_seqs.append(np.array(model.predict(sub)))  # type: ignore[attr-defined]

    flips, total = 0, 0
    for prev, curr in zip(label_seqs, label_seqs[1:]):
        overlap = min(len(prev), len(curr))
        flips += int((prev[-overlap:] != curr[-overlap:]).sum())
        total += overlap
    return flips / total if total else float("nan")


def main() -> None:
    models = {
        "HMM": lambda: RegimeHMM(random_state=SEED),
        "Jump": lambda: JumpRegimeModel(random_state=SEED),
    }
    header = (
        f"{'scenario':<22} {'model':<6} {'block_acc':>9} {'bear_recall':>11} "
        f"{'refit_flips':>11} {'fit_s':>7}"
    )
    print(header)
    print("-" * len(header))

    for scenario_name, blocks in SCENARIOS.items():
        features, truth = build_scenario(blocks, seed=SEED)
        truth_arr = truth.to_numpy()
        bear_mask = truth_arr == "bear"

        for model_name, factory in models.items():
            labels, fit_s = fit_predict(factory(), features)
            labels_arr = np.array(labels)
            block_acc = float((labels_arr == truth_arr).mean())
            bear_recall = float((labels_arr[bear_mask] == "bear").mean())
            flip_rate = refit_flip_rate(factory, features)
            print(
                f"{scenario_name:<22} {model_name:<6} {block_acc:>9.3f} "
                f"{bear_recall:>11.3f} {flip_rate:>11.3f} {fit_s:>7.3f}"
            )

    # Determinism spot-check across both engines (same seed → same labels).
    features, _ = build_scenario(SCENARIOS["A_bull_chop_crash"], seed=SEED)
    for model_name, factory in models.items():
        a = factory().fit(features).predict(features)  # type: ignore[attr-defined]
        b = factory().fit(features).predict(features)  # type: ignore[attr-defined]
        print(f"determinism[{model_name}]: {'OK' if a == b else 'MISMATCH'}")

    print(f"\nrows/scenario after warmup drop ({N_INIT_ROWS}-row warmup): "
          f"{ {k: len(build_scenario(v, SEED)[0]) for k, v in SCENARIOS.items()} }")
    print("jumpmodels pin: jumpmodels==0.1.1 | jump_penalty=10.0, n_init=10 "
          "(untuned spike defaults)")


if __name__ == "__main__":
    main()
