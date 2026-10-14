# 0021 — Trade check: does the edge cover the cost? In shadow first

**Status:** Accepted 2026-10-14 (shadow mode; enforcement is a later owner decision, see "Switching it on")
**Date:** 2026-10-14
**Extends:** [0018](0018-trust-evidence-pre-registration.md) (the staged calibrator that supplies `p`),
[0012](0012-advisor-holding-scoring-and-sell-cap.md) / [0013](0013-advisor-cumulative-sell-cap-and-core-floor.md) (the turnover limits it adds to)
**Consumed by:** `backend/app/decision/trade_check.py`, `GET /api/paper-portfolio/{id}/trade-check`,
the paper run card (`TradeCheckPanel`), the Discover shortlist's "Worth acting on?" line

## Context

Every paper sleeve has trailed MSCI World since it started, and the live evidence shows no stock-picking
skill yet (ADR 0018 calibration: no source has reached 100 issue dates with a usable spread). The
research the plan of 2026-10-14 collected agrees: costs are certain, skill is not (Gârleanu & Pedersen
2013 trade partially toward an aim; DeMiguel et al. 2020 show netting and turnover control are where
much of a multi-signal strategy's net value comes from; Gençay 2026 and KTD-FIN 2026 find no net
selection alpha in LLM strategies after honest evaluation). A trade with no shown edge has a negative
expected value equal to its cost.

The owner pays no tax at present (NV certificate), so for the real book the cost is fees and spread;
the paper books are a model of the system's judgement and are charged the same way.

## Decision

1. **Rule.** For each trade of `notional` euros:
   `edge = (2p − 1) · m · notional`, and the trade is **blocked when `edge < cost`**.
   `cost` = broker fee (as booked) + half spread (5 bp ETF, 10 bp stock) + tax on a sale's realised gain
   (zero under a valid NV certificate; otherwise flat KESt+Soli after Teilfreistellung, counted in full,
   which is an upper bound).
2. **Edge.** `p` = chance the call beats MSCI World over 21 trading days (a sell: that the sold name
   lags it). In order: the calibrated conviction (ADR 0018) of the call behind the trade, which the advisor
   stores in the same cycle just after booking it (a call more than an hour later is another cycle's); else the sleeve's own resolved trades,
   one vote per date, shrunk toward 0.5 by 20 pseudo-dates; else none (`p = 0.5`, no edge). `m` is the
   sleeve's mean |21-day excess return| (4 % before it has history). Only outcomes known on the trade
   day are used.
3. **Shadow.** The check never stops a paper trade. A daily job (22:30 UTC) writes one
   `paper_trade_checks` row per trade of every non-manual sleeve and marks it daily until its 21-day
   horizon ends (or, with no close on the horizon day, 7 days after it, on the last closes there are):
   `contribution = sign · notional · (r_asset − r_MSCI World) − cost`. A sale's tax uses the average cost
   of the position just before it, replayed from the run's start. Both returns cover the same dates (a
   series that starts more than 5 days late is not marked; one whose prices stop more than 5 days before
   the horizon is frozen after the grace without an outcome and counts nowhere), and a backfill settles
   earlier checks before it judges a later trade. The counterfactual is benchmark-relative, the calibrator's own hit definition: a blocked buy's money sits
   in MSCI World, a blocked sell's position is kept. **Value with the check on = NAV − Σ contribution
   of blocked trades.** It ignores what later trades did with the same shares; it is an estimate.
4. **Real money.** The Discover shortlist shows "Worth acting on?" per pick: "Possibly" only when the
   pick's score tier has beaten the ETF at the low end of its 90 % range, otherwise "Not yet", with the
   round-trip cost of a €500 order. Nothing is ordered (no write access to any broker).
5. A reset archives the run's checks with its trades.

## Switching it on

Start: the paper reset of 2026-10-14 (T0). Review no earlier than **2027-01-14** and only with at least
30 distinct trade dates resolved. Recommendation fixed now, before any result: enforce the check on the
advisor and mandate sleeves when the blocked trades' summed contribution is ≤ 0 (they did not, in total,
earn their cost); keep it in shadow otherwise and record why. Without shown edge, not trading is the
default; the burden of proof is on the trade. The decision and its date go into an amendment here.

## Consequences

- No change to any paper NAV, trade or prediction row; the check only adds rows.
- Until a source shows edge, nearly every trade reads "would stop". That is the finding, not a defect:
  the shadow result measures whether the system's trading beat doing nothing against the index.
- Estimates only, not tax advice: the API carries `estimate: true` and `not_tax_advice: true`.
