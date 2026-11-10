"""How many names could equally be on the shortlist (plan 2026-11-10, WP12b; shadow only).

A composite rank is not a return history, so the confidence-set method of
arXiv 2510.14988 does not apply as it stands. Its idea does: say honestly how
much of the list depends on the exact weights. Each draw perturbs every signal
weight within a band (default +-25 %), recomputes the composites from the stored
signals and keeps the top places of each lane. No data is fetched.

Each draw also shifts the 12-1 momentum window ends back by 0-5 trading days
(the same shift for every name, like a different run date) using the shifted
momentums the momentum stage stored; names without them keep their own rank.

Not re-applied: the sector and industry caps (the draws rank by composite alone).
"""
from __future__ import annotations

import random
from typing import Any

DRAWS = 200
MOMENTUM_SHIFT_DAYS = 5
WEIGHT_BAND = 0.25
COMPETE_SHARE = 0.20
STABLE_SHARE = 0.80


def _signals_and_weights(result: dict[str, Any]) -> list[tuple[str, float, float]]:
    raw = result.get("scores")
    rows = raw.get("composite_inputs") if isinstance(raw, dict) else None
    out: list[tuple[str, float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("score"), (int, float)) and isinstance(row.get("weight"), (int, float)):
            out.append((str(row["signal"]), float(row["score"]), float(row["weight"])))
    return out


def _shifted_ranks(rows: list[dict[str, Any]], max_shift: int) -> dict[int, dict[str, float]]:
    """Per shift 1..max_shift: mid-rank percentile of the shifted momentum among the lane's names that have it."""
    out: dict[int, dict[str, float]] = {}
    for k in range(1, max_shift + 1):
        values: dict[str, float] = {}
        for r in rows:
            mom = (r.get("scores") or {}).get("momentum_quality") if isinstance(r.get("scores"), dict) else None
            shifts = mom.get("momentum_12_1m_shifts") if isinstance(mom, dict) else None
            if isinstance(shifts, list) and len(shifts) >= k and isinstance(shifts[k - 1], (int, float)):
                values[str(r["symbol"])] = float(shifts[k - 1])
        if len(values) < 5:
            continue
        ordered = sorted(values, key=lambda x: values[x])
        n = len(ordered)
        ranks: dict[str, float] = {}
        i = 0
        while i < n:  # ties share the mean rank
            j = i
            while j + 1 < n and values[ordered[j + 1]] == values[ordered[i]]:
                j += 1
            for m in range(i, j + 1):
                ranks[ordered[m]] = ((i + j) / 2 + 0.5) / n
            i = j + 1
        out[k] = ranks
    return out


def stability(
    evaluable: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    draws: int = DRAWS,
    band: float = WEIGHT_BAND,
    seed: int = 20261110,
) -> dict[str, Any] | None:
    """Per lane: places, names competing, names on the list in at least 80 % of draws.

    ``evaluable`` are the scored candidates (``composite_inputs`` in their scores),
    ``selected`` the shortlist the run produced; the places of a lane are how many
    of it were selected. None without stored inputs.
    """
    rng = random.Random(seed)
    lanes: dict[str, list[dict[str, Any]]] = {}
    for r in evaluable:
        if _signals_and_weights(r):
            lanes.setdefault(str(r.get("lane") or "stock"), []).append(r)
    if not lanes:
        return None
    keys = sorted({s for rows in lanes.values() for r in rows for s, _, _ in _signals_and_weights(r)})
    out: dict[str, Any] = {"draws": draws, "weight_band": band, "lanes": {}}
    for lane, rows in lanes.items():
        places = sum(1 for r in selected if str(r.get("lane") or "stock") == lane)
        if places <= 0:
            continue
        inputs = {str(r["symbol"]): _signals_and_weights(r) for r in rows}
        counts: dict[str, int] = dict.fromkeys(inputs, 0)
        shifted = _shifted_ranks(rows, MOMENTUM_SHIFT_DAYS)
        for _ in range(draws):
            mult = {k: 1.0 + rng.uniform(-band, band) for k in keys}
            shift = rng.randint(0, MOMENTUM_SHIFT_DAYS)
            ranks = shifted.get(shift) if shift else None
            composite: dict[str, float] = {}
            for sym, signals in inputs.items():
                if ranks is not None and sym in ranks:
                    signals = [(s, ranks[sym] if s == "momentum" else v, w) for s, v, w in signals]
                total = sum(w * mult[s] for s, _, w in signals)
                composite[sym] = sum(v * w * mult[s] for s, v, w in signals) / total if total > 0 else 0.0
            for sym in sorted(composite, key=lambda x: (-composite[x], x))[:places]:
                counts[sym] += 1
        share = {sym: n / draws for sym, n in counts.items()}
        competing = sum(1 for v in share.values() if v >= COMPETE_SHARE)
        picked = [str(r["symbol"]) for r in selected if str(r.get("lane") or "stock") == lane]
        stable = sum(1 for sym in picked if share.get(sym, 0.0) >= STABLE_SHARE)
        out["lanes"][lane] = {
            "places": places,
            "competing": competing,
            "stable": stable,
            "text": (
                f"{competing} {'names' if competing != 1 else 'name'} compete for these {places} places; "
                f"{stable} of the {len(picked)} are on the list in at least {int(STABLE_SHARE * 100)} % of variations."
            ),
            "share": {sym: round(v, 3) for sym, v in share.items() if v > 0},
        }
    return out if out["lanes"] else None
