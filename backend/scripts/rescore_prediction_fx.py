"""One-time re-score of predictions resolved in their listing currency (plan 2026-10-14, M12).

Resolution looked up ``{ccy}EUR=X`` for a non-dollar currency, a series the
app never stores (its FX bars are ``USD{ccy}=X``), and fell back to the local
return without a flag. Pound, franc, krone and krona picks were therefore
scored in local currency against a EUR benchmark.

For every resolved or delisted row without ``outcome.fx_basis``, this restates
the stored local return in EUR over the stored entry and exit dates (crossing
through USD), recomputes the realised and excess return against the stored
benchmark return, keeps the old numbers under ``score_json.before_fx_fix`` and
sets ``fx_basis``. The row's ``hit`` and ``brier`` are recomputed from the new
excess; its ``rank_ic`` belongs to the whole cross-section and is left as it
was. Rows already in EUR (EUR or USD listings) only get ``fx_basis = "eur"``. ``discovery_prediction``'s outcome columns are not
issue-time columns, so the append-only trigger allows this.

Dry run by default. Writing to prod needs the owner's OK.

Usage (run from backend/):
    .venv/bin/python scripts/rescore_prediction_fx.py            # dry run
    .venv/bin/python scripts/rescore_prediction_fx.py --apply    # writes
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision.discover.resolution import _Series, load_closes  # noqa: E402
from app.foundation.core.db import SessionLocal  # noqa: E402
from app.foundation.models.entities import DiscoveryPrediction  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    args = parser.parse_args()

    db = SessionLocal()
    # Prices load in their own session: ``market.history`` commits what it
    # fetches, and on the main session that would also commit the re-scored
    # rows, dry run or not.
    price_db = SessionLocal()
    try:
        rows = (
            db.query(DiscoveryPrediction)
            .filter(DiscoveryPrediction.outcome_status.in_(("resolved", "delisted")))
            .all()
        )
        entries = [
            date.fromisoformat(e) for p in rows
            if (e := ((p.score_json or {}).get("outcome") or {}).get("entry_date"))
        ]
        # Reach back to the oldest entry, so no old row is flagged for want of loaded FX bars.
        days = max(365 * 2, (datetime.now(UTC).date() - min(entries)).days + 30) if entries else 365 * 2
        series = _Series(price_db, load_closes, days)
        changed = tagged = 0
        for pred in rows:
            score = dict(pred.score_json or {})
            outcome = dict(score.get("outcome") or {})
            if not outcome or "fx_basis" in outcome:
                continue
            if outcome.get("return_currency") == "EUR":
                outcome["fx_basis"] = "eur"
                score["outcome"] = outcome
                pred.score_json = score
                tagged += 1
                continue
            currency = str(outcome.get("currency") or "")
            local = outcome.get("local_return")
            entry, exit_ = outcome.get("entry_date"), outcome.get("exit_date")
            if local is None or not entry or not exit_:
                continue
            fx = series.eur_factor(currency, date.fromisoformat(entry), date.fromisoformat(exit_))
            if fx is None:
                outcome["fx_basis"] = "local_fallback"
                print(f"{pred.symbol:<10} {currency:<4} no rate: flagged local_fallback")
            else:
                sign = -1.0 if pred.direction in ("sell", "bear") else 1.0
                stock = (1.0 + float(local)) * fx - 1.0
                score["before_fx_fix"] = {
                    "realised_return": pred.realised_return, "excess_return": pred.excess_return,
                    "return_currency": outcome.get("return_currency"),
                }
                realised = sign * stock
                excess = sign * (stock - pred.benchmark_return) if pred.benchmark_return is not None else None
                print(
                    f"{pred.symbol:<10} {currency:<4} realised {pred.realised_return} -> {realised:.4f}, "
                    f"excess {pred.excess_return} -> {excess}"
                )
                pred.realised_return, pred.excess_return = realised, excess
                outcome["fx_basis"], outcome["return_currency"] = "eur", "EUR"
                judged = excess if excess is not None else realised
                hit = 1 if judged > 0 else 0
                score["before_fx_fix"].update(hit=score.get("hit"), brier=score.get("brier"))
                score["hit"] = hit
                probability = (
                    pred.conviction_calibrated if score.get("brier_basis") == "calibrated" else pred.conviction
                )
                if score.get("brier") is not None and probability is not None:
                    score["brier"] = (float(probability) - hit) ** 2
            score["outcome"] = outcome
            pred.score_json = score
            changed += 1
        if args.apply:
            db.commit()
            print(f"\nApplied: {changed} re-scored or flagged, {tagged} tagged as already in EUR.")
        else:
            db.rollback()
            print(f"\nDry run: {changed} would be re-scored or flagged, {tagged} tagged. Pass --apply to write.")
    finally:
        db.close()
        price_db.close()


if __name__ == "__main__":
    main()
