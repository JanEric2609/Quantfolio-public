"""The nightly forward-test lab (plan 2026-11-09).

Each night, after the universe prefetch has stored the previous day's bars:

1. **Score.** Every signal in :mod:`.signals` scores every MSCI World name with
   a bar on the last completed trading day (``as_of``), in EUR. The scores and
   two portfolios built from them are frozen in ``forward_lab_snapshots``
   before the next day's prices exist:

   * ``rank``: centred ranks, long leg +1, short leg -1 (the textbook sort);
   * ``efficient``: the least-variance long-short mix with the same exposure
     to the signal (Ledoit, Wolf & Zhao), on a Ledoit-Wolf covariance of the
     last year; the trust page's ``ranking_ef`` design, which measured ~1.3
     times faster to a verdict at this breadth.

2. **Resolve.** Each earlier snapshot earns the next trading day's EUR return
   of the names priced on both days (each leg rescaled to +1 / -1 over them);
   that day's value and the Spearman correlation of scores and returns go to
   ``forward_lab_daily``. Daily rebalanced, so consecutive days do not overlap.

3. **Judge** (:func:`summary`, on read). Per signal: the anytime-valid
   e-values for a positive and a negative mean of the ``efficient`` series,
   e-BH across all signals at 1 %, and the robustness checks of
   :mod:`.robustness`.

Nothing here feeds Discover, a pick or a trade: it is a record. Returns are
before trading costs, which a daily-rebalanced long-short would pay heavily;
the question it answers is whether the signal still sorts tomorrow's winners
from losers, not whether trading it daily pays.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation import eur_prices
from app.foundation.evidence_weights import centred_rank_weights, efficient_weights, ledoit_wolf_cov
from app.foundation.index_universe import universe_symbols
from app.foundation.models.entities import ForwardLabDaily, ForwardLabSnapshot, NewsToneScore
import app.lab.forward_lab.robustness as rb
from app.lab.forward_lab.signals import BY_NAME, HISTORY_DAYS, NEWS_SIGNAL, PRICE_SIGNALS, SIGNAL_NAMES

logger = logging.getLogger(__name__)

#: The first day whose snapshot counts: everything is forward from deployment.
START = date(2026, 11, 10)
#: A signal is scored only over at least this many names.
MIN_NAMES = 30
#: Days a missing close may be carried in the covariance panel.
FFILL_LIMIT = 3
#: Next-day stock returns are clipped here before they enter a portfolio: one
#: bad print at +500 % would otherwise move the whole day.
RETURN_CLIP = 0.4
#: Nights of news tone averaged into one score.
NEWS_NIGHTS = 7
#: The e-value at which a signal counts as having evidence on its own (e-BH decides across signals).
THRESHOLD = 1.0 / rb.ALPHA


# --- panel ----------------------------------------------------------------------------


def _load_panel(db: Session, symbols: list[str], bench: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """(closes carried at most FFILL_LIMIT days, real-bar mask, benchmark closes) on the benchmark's days."""
    rate_cache: dict[str, dict[str, float] | None] = {}
    bench_closes = eur_prices.eur_closes(db, bench, days=HISTORY_DAYS, allow_live=False, rate_cache=rate_cache)
    if not bench_closes:
        return pd.DataFrame(), pd.DataFrame(), pd.Series(dtype=float)
    days = sorted(bench_closes)
    raw: dict[str, pd.Series] = {}
    for sym in symbols:
        closes = eur_prices.eur_closes(db, sym, days=HISTORY_DAYS, allow_live=False, rate_cache=rate_cache)
        if closes:
            raw[sym] = pd.Series(closes, dtype=float)
    frame = pd.DataFrame(raw).reindex(days)
    real = frame.notna()
    return frame.ffill(limit=FFILL_LIMIT), real, pd.Series(bench_closes, dtype=float).reindex(days)


def _previous_weekday(today: date) -> date:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _as_of(days: list[str], today: date) -> str | None:
    """The previous weekday when the benchmark has its bar, else None (nothing is scored).

    Only that day: after a missed night or a benchmark holiday an older day
    would be scored when the next day's prices already exist, and a snapshot
    must come before its outcome.
    """
    want = _previous_weekday(today).isoformat()
    return want if want in days else None


# --- scoring --------------------------------------------------------------------------


def _price_scores(panel: pd.DataFrame, real: pd.DataFrame, bench: pd.Series, as_of: str) -> dict[str, dict[str, float]]:
    upto = panel.loc[:as_of]
    bench_upto = bench.loc[:as_of].ffill()
    dates = list(upto.index)
    out: dict[str, dict[str, float]] = {s.name: {} for s in PRICE_SIGNALS}
    for sym in upto.columns:
        if not bool(real.at[as_of, sym]):
            continue
        series = upto[sym]
        ok = series.notna() & bench_upto.notna()
        closes = series[ok].to_numpy()
        if len(closes) < 130:
            continue
        b = bench_upto[ok].to_numpy()
        d = [dates[i] for i in np.flatnonzero(ok.to_numpy())]
        for sig in PRICE_SIGNALS:
            assert sig.score is not None
            try:
                value = sig.score(closes, b, d)
            except Exception:  # noqa: BLE001 - one bad series must not stop the night
                logger.debug("forward_lab: %s failed for %s", sig.name, sym, exc_info=True)
                value = None
            if value is not None and np.isfinite(value):
                out[sig.name][sym] = float(value)
    return out


def _news_scores(db: Session, as_of: str, symbols: set[str]) -> dict[str, float]:
    """Mean tone over :data:`NEWS_NIGHTS` nights before *as_of*; names without news are left out.

    A night's row is dated the morning it is written (UTC). Only rows dated
    strictly before *as_of* count, so even an Asian close on the morning of the
    reading cannot have come before the headlines were rated.
    """
    day = date.fromisoformat(as_of)
    rows = (
        db.query(NewsToneScore)
        .filter(NewsToneScore.day < day, NewsToneScore.day >= day - timedelta(days=NEWS_NIGHTS))
        .all()
    )
    tones: dict[str, list[float]] = {}
    for row in rows:
        if row.symbol in symbols and row.n_headlines > 0:
            tones.setdefault(row.symbol, []).append(float(row.tone))
    return {sym: float(np.mean(v)) for sym, v in tones.items()}


def _legs(weights: np.ndarray) -> np.ndarray:
    """Long leg scaled to +1 and short leg to -1."""
    w = np.asarray(weights, dtype=float)
    long_total = w[w > 0].sum()
    short_total = -w[w < 0].sum()
    if long_total <= 0 or short_total <= 0:
        return np.zeros_like(w)
    return np.where(w > 0, w / long_total, w / short_total)


def portfolios(
    scores: dict[str, float], panel: pd.DataFrame, as_of: str,
) -> dict[str, dict[str, float]]:
    """``{"rank": {sym: w}, "efficient": {sym: w}}`` from *scores*, using only prices up to *as_of*."""
    syms = sorted(scores)
    s = np.array([scores[x] for x in syms])
    rank_w = _legs(centred_rank_weights(s))
    out = {"rank": {x: round(float(w), 7) for x, w in zip(syms, rank_w, strict=True) if w != 0}}
    hist = panel.loc[:as_of, syms].iloc[-253:]
    rets = hist.pct_change().iloc[1:]
    # Names with a full year enter the covariance; the rest keep their rank weight only.
    full = [x for x in syms if rets[x].notna().mean() >= 0.95]
    if len(full) >= MIN_NAMES:
        r = rets[full].fillna(0.0).to_numpy()
        signal = centred_rank_weights(np.array([scores[x] for x in full]))
        signal = signal / (signal.std() or 1.0)
        try:
            ef = _legs(efficient_weights(signal, ledoit_wolf_cov(r), float(signal @ signal)))
            out["efficient"] = {x: round(float(w), 7) for x, w in zip(full, ef, strict=True) if w != 0}
        except np.linalg.LinAlgError:
            logger.warning("forward_lab: covariance singular on %s", as_of)
    return out


def score_day(db: Session, panel: pd.DataFrame, real: pd.DataFrame, bench: pd.Series, as_of: str) -> int:
    """Freeze every signal's snapshot for *as_of*; returns the number written."""
    day = date.fromisoformat(as_of)
    existing = {s for (s,) in db.query(ForwardLabSnapshot.signal).filter(ForwardLabSnapshot.as_of == day)}
    all_scores = _price_scores(panel, real, bench, as_of)
    priced = {sym for sym in panel.columns if bool(real.at[as_of, sym])}
    all_scores[NEWS_SIGNAL.name] = _news_scores(db, as_of, priced)
    written = 0
    for name in SIGNAL_NAMES:
        scores = all_scores.get(name) or {}
        if name in existing or len(scores) < MIN_NAMES:
            continue
        db.add(ForwardLabSnapshot(
            as_of=day, signal=name,
            scores_json={k: round(v, 6) for k, v in scores.items()},
            weights_json=portfolios(scores, panel, as_of), n=len(scores),
        ))
        written += 1
    return written


# --- resolving ------------------------------------------------------------------------


def _leg_return(weights: dict[str, float], rets: pd.Series) -> float | None:
    names = [s for s in weights if s in rets.index and np.isfinite(rets[s])]
    if not names:
        return None
    w = _legs(np.array([weights[s] for s in names]))
    if not np.any(w):
        return None
    return float(w @ np.array([rets[s] for s in names], dtype=float))


def resolve(db: Session, panel: pd.DataFrame, real: pd.DataFrame) -> int:
    """Score each unresolved snapshot on the next trading day's returns; returns the number resolved."""
    done = {(s, d) for s, d in db.query(ForwardLabDaily.signal, ForwardLabDaily.as_of)}
    days = list(panel.index)
    written = 0
    oldest = date.fromisoformat(days[0]) if days else date.today()
    pending = (
        db.query(ForwardLabSnapshot)
        .filter(ForwardLabSnapshot.as_of >= oldest)
        .order_by(ForwardLabSnapshot.as_of)
        .all()
    )
    for snap in pending:
        key = (snap.signal, snap.as_of)
        as_of = snap.as_of.isoformat()
        if key in done or as_of not in days:
            continue
        i = days.index(as_of)
        if i + 1 >= len(days):
            continue
        nxt = days[i + 1]
        both = (real.loc[as_of] & real.loc[nxt]).to_numpy(dtype=bool)
        raw = panel.loc[nxt].to_numpy(dtype=float) / panel.loc[as_of].to_numpy(dtype=float) - 1.0
        rets = pd.Series(raw[both], index=panel.columns[both], dtype=float).clip(-RETURN_CLIP, RETURN_CLIP)
        weights = snap.weights_json or {}
        scores = pd.Series(snap.scores_json or {}, dtype=float)
        common = scores.index.intersection(rets.index)
        ic = None
        if len(common) >= MIN_NAMES:
            a = pd.Series(scores.reindex(common).to_numpy(dtype=float)).rank()
            b = pd.Series(rets.reindex(common).to_numpy(dtype=float)).rank()
            ic = float(a.corr(b))
        db.add(ForwardLabDaily(
            signal=snap.signal, as_of=snap.as_of, day=date.fromisoformat(nxt),
            value_rank=_leg_return(weights.get("rank") or {}, rets),
            value_efficient=_leg_return(weights.get("efficient") or {}, rets),
            rank_ic=ic, n=int(len(common)),
        ))
        written += 1
    return written


def run_nightly(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """Score yesterday, resolve what can be resolved, commit. Safe to run twice."""
    today = today or datetime.now(UTC).date()
    symbols = sorted({m.symbol for m in universe_symbols(db) if m.symbol})
    bench = eur_prices.benchmark_ticker(db)
    panel, real, bench_closes = _load_panel(db, symbols, bench)
    if panel.empty:
        return {"status": "no_benchmark_bars", "symbols": len(symbols)}
    as_of = _as_of(list(panel.index), today)
    last = str(panel.index[-1])
    scored = 0
    if as_of and date.fromisoformat(as_of) >= START:
        scored = score_day(db, panel, real, bench_closes, as_of)
        db.flush()
    resolved = resolve(db, panel, real)
    db.flush()
    store_summary(db)
    db.commit()
    return {
        "status": "ok", "as_of": as_of, "symbols": len(symbols),
        "priced": int(real.loc[as_of].sum()) if as_of else 0, "last_bar": last,
        "snapshots": scored, "resolved": resolved,
    }


# --- judging --------------------------------------------------------------------------


def _ci90(values: np.ndarray) -> tuple[float, float] | None:
    from app.foundation.forecast_verification import mean_ci_bootstrap

    return mean_ci_bootstrap(values.tolist(), 0.9)


def summary(db: Session, *, with_robustness: bool = True, draws: int = rb.BOOTSTRAP_DRAWS) -> dict[str, Any]:
    """Per signal: days, mean, 90 % interval, e-values, e-BH verdict and robustness.

    The page reads the copy :func:`store_summary` keeps from the last night
    (10,000 resamples); without one it computes this live with fewer.
    """
    rows = db.query(ForwardLabDaily).order_by(ForwardLabDaily.day).all()
    by_signal: dict[str, list[ForwardLabDaily]] = {}
    for r in rows:
        by_signal.setdefault(r.signal, []).append(r)
    skill: dict[str, float] = {}
    harm: dict[str, float] = {}
    out: list[dict[str, Any]] = []
    for name in SIGNAL_NAMES:
        series = by_signal.get(name, [])
        values = np.array([r.value_efficient for r in series if r.value_efficient is not None], dtype=float)
        ics = np.array([r.rank_ic for r in series if r.rank_ic is not None], dtype=float)
        e_skill, e_harm = rb.e_values(values) if len(values) else (1.0, 1.0)
        skill[name], harm[name] = e_skill, e_harm
        sig = BY_NAME[name]
        item: dict[str, Any] = {
            "signal": name, "label": sig.label, "source": sig.source,
            "days": int(len(values)),
            "first_day": series[0].day.isoformat() if series else None,
            "last_day": series[-1].day.isoformat() if series else None,
            "mean_daily": float(values.mean()) if len(values) else None,
            "ci90_daily": _ci90(values) if len(values) else None,
            "annual_ir": (
                float(values.mean() / values.std(ddof=1) * np.sqrt(252)) if len(values) > 2 and values.std() > 0 else None
            ),
            "mean_rank_ic": float(ics.mean()) if len(ics) else None,
            "e_skill": e_skill, "e_harm": e_harm,
            "names": int(series[-1].n) if series else 0,
        }
        if with_robustness and len(values) > rb.MIN_OBS + 5:
            item["robustness"] = rb.robustness(values, THRESHOLD, seed=len(values), draws=draws).as_dict()
        out.append(item)
    proven = rb.e_bh(skill)
    harmful = rb.e_bh(harm)
    for item in out:
        name = item["signal"]
        item["state"] = (
            "evidence_of_skill" if name in proven
            else "evidence_of_harm" if name in harmful
            else "collecting" if item["days"] else "not_started"
        )
    return {
        "start": START.isoformat(), "alpha": rb.ALPHA, "threshold_single": THRESHOLD,
        "signals": out,
        "note": "Before trading costs; daily rebalanced; recorded forward only.",
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "resamples": draws if with_robustness else 0,
    }


#: Internal state (no Control Center entry): the last night's :func:`summary`.
SUMMARY_SETTING_KEY = "forward_lab_summary_json"


def store_summary(db: Session) -> None:
    """Keep the night's summary with :data:`robustness.NIGHTLY_DRAWS` resamples; the caller commits."""
    from app.foundation.settings import upsert_public_settings

    upsert_public_settings(db, {SUMMARY_SETTING_KEY: summary(db, draws=rb.NIGHTLY_DRAWS)})


def stored_summary(db: Session) -> dict[str, Any]:
    """The last night's summary when it covers every signal and every resolved day, else a live one."""
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, SUMMARY_SETTING_KEY)
    if isinstance(stored, dict):
        names = {s.get("signal") for s in stored.get("signals") or []}
        days = sum(int(s.get("days") or 0) for s in stored.get("signals") or [])
        if names == set(SIGNAL_NAMES) and days == db.query(ForwardLabDaily).filter(
            ForwardLabDaily.value_efficient.isnot(None)
        ).count():
            return stored
    return summary(db)


__all__ = [
    "START", "SUMMARY_SETTING_KEY", "portfolios", "resolve", "run_nightly", "score_day", "store_summary",
    "stored_summary", "summary",
]
