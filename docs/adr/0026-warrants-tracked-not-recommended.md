# 0026 — Warrants and options: tracked and measured, not recommended

**Status:** Accepted 2026-11-20 ("track + Quantlab", terms entered by hand)
**Date:** 2026-11-20
**Consumed by:** `foundation/derivatives.py`, `foundation/live_positions.py` (`DERIVATIVE` → `derivative`),
`foundation/broker_sync.py`, `foundation/etf_lookthrough.py`, `foundation/control_center.py`,
`interface/api/derivatives.py`, `lab/options_lab/`, `decision/trade_check.py` (`trade_check_daily` slot),
`frontend/src/pages/portfolio/components/DerivativesCard.tsx`, migration `0156_derivatives`,
`backend/scripts/options_lab_report.py`

## Context

Scalable lists bank-issued warrants such as Vontobel's puts on Anthropic (Nasdaq: ANT, listed 2026-11-19 at an
$83 IPO price), e.g. `DE000BE4QXD6` "Anthropic Put 100,00 VON", and syncs them like any other position. Scalable's holdings feed types them `DERIVATIVE`. That type was missing from
the broker-type map, so the mirror stored both as `etf` holdings. The tax cockpit then estimated them a
Vorabpauschale, and the look-through would have counted a put as a long holding of its underlying.

What Scalable offers, as far as sources show (2026-11): bank-issued warrants (Optionsscheine), knock-outs and
certificates from several issuers, traded on gettex and the Hanover exchange's EIX. A retail account can
buy them; it cannot write options. Exchange-listed ANT options start at Cboe on 2026-11-23. Scalable sells
only the issuers' warrants on them.

The `sc` holdings feed carries a warrant's ISIN, name and type, and nothing else. The name gives the
underlying, put or call, the strike and the issuer. It does not give the expiry, the ratio or the strike
currency. The read-only `sc` allow-list is not widened to fetch more (standing safety rule).

## Research

- **Retail option buyers lose on average.** de Silva, So & Smith, "Losing is Optional: Retail Option
  Trading and Expected Announcement Volatility" (Review of Finance, 2025): average retail losses of 5–9 %,
  and 10–14 % around high-volatility earnings announcements. The losses come from overpaying implied
  volatility and wide spreads, and from holding positions after the event.
- **Protective puts are an expensive hedge.** Israelov, "Pathetic Protection: The Elusive Benefits of
  Protective Puts" (Journal of Alternative Investments, Winter 2019). Rolled puts gave a lower Sharpe ratio
  and deeper peak-to-trough falls than simply holding less equity at the same expected return. The
  protection fails when a fall does not line up with the expiry cycle.
- **Issuers price in a margin.** Baule & Blonski (Journal of Futures Markets, 2015): buyers pick the
  cheapest of similar warrants but do not respond to the general margin level. An industry response to
  BaFin (FIA EPTA, 2025, advocacy, not peer-reviewed) reports retail warrant buyers paying 2–5 times the
  premium over mid on single stocks. A figure of "up to 30 %" overpricing against listed options (EUWAX
  data) was only seen in a search snippet, so it is not relied on here. The lab measures the owner's
  warrants itself.
- **German tax (§ 20 EStG).** A warrant is a capital claim (§ 20 (1) Nr. 7 / (2) EStG); it is not a fund.
  It owes no Vorabpauschale and gets no Teilfreistellung. JStG 2024 (published 2024-12-05) removed the
  €20,000 loss-offset cap for derivatives (Termingeschäfte) for all open years. Losses therefore fall in the
  general bucket. Most sources say JStG 2024 also removed the cap on total losses (a warrant expiring
  worthless). These are secondary sources (VLH, CMS, Lohnsteuer kompakt), not the statute: an estimate,
  not tax advice.
- **Data.** Yahoo serves ANT bars and, from 2026-11-23, listed option chains with implied volatility.
  Issuer and exchange pages for the two puts returned 404 on 2026-11-20 (new products are indexed late).

### What the Quantlab replay shows (S&P 500 with VIX, 1990-01-02 to 2026-11-19, 2.5 % cash)

Monthly (21-day) puts, priced with Black-Scholes at VIX (no skew, no issuer margin: a floor on the cost).

| Strike | Premium per year | Protected: return / Sharpe / worst fall | Same mean return without puts: equity share / return / Sharpe / worst fall |
|---|---|---|---|
| at the money | 25.5 % | −0.5 % / −0.31 / −61.2 % | 0 % (all cash) |
| 5 % below | 6.8 % | 5.5 % / 0.29 / −53.4 % | 53 % / 6.0 % / 0.46 / −32.1 % |
| 10 % below | 1.6 % | 7.9 % / 0.43 / −52.0 % | 89 % / 8.0 % / 0.46 / −49.9 % |

The unhedged index: 8.6 %/yr, Sharpe 0.46, worst fall −54.5 %. Priced 3 vol points above VIX (skew), the
5 %-below put leaves 2.9 %/yr and a −63 % worst fall. In every case, holding less index did better than
buying protection, as Israelov found.

## Decision

1. **Track every warrant correctly.** `DERIVATIVE` and `WARRANT` map to asset type `derivative`. Derivatives
   are not funds for the tax import, get no Vorabpauschale, and are left out of the ETF look-through.
2. **The owner enters the terms once** (expiry, ratio, strike currency, American or European). The form is
   prefilled from the name. No scraper is used: new products return 404, and scrapers break.
3. **Show what the price implies.** The implied volatility comes from a Leisen-Reimer tree for American warrants (smooth in the spot, so gamma is stable; CRR was a sawtooth) and
   Black-Scholes for European ones. Also shown: delta, theta per day, vega, the stock-equivalent exposure
   in EUR (negative for a put), the break-even at expiry and the value at expiry for moves of −50 % to +50 %.
   All of it is labelled an estimate.
   Amendment 2026-11-25: the price is built with a continuous dividend yield (read from Yahoo each evening and
   kept in the internal `derivatives_inputs_json` setting; zero until read) and, for a non-EUR strike, that
   currency's short rate (default 4 %, the same key) instead of the EUR rate. The listed-option smirk signal
   ignores quotes with no bid, a crossed or wider-than-half-the-mid spread, or an IV outside 3-200 %.
4. **Warn before expiry.** The Control Center (and so the 06:00 Telegram self-check) lists a warrant
   without terms, one expiring within 7 days, and one past its expiry.
5. **Measure in the Quantlab.** Each evening (22:30 UTC, in the `trade_check_daily` slot) the worker stores one row per warrant:
   the warrant's implied volatility and Yahoo's listed-option volatility nearest in expiry and strike. It
   also keeps the underlying's bars fresh. `lab/options_lab` reports the issuer margin and the
   protective-put replay (`scripts/options_lab_report.py`).
6. **No recommendations.** Discover, the advisor and the paper sleeves do not suggest options. Reopening
   that needs evidence the lab does not show today: a measured edge after the issuer's margin.

## Consequences

- After deploy, the next Scalable sync re-types held warrants as `derivative`. Their terms must be entered
  once on Portfolio → Holdings; until then the Control Center asks for them.
- Assumed until entered: a put's ratio, expiry and strike currency. A sister product (Vontobel call
  `DE000BE4Q614`, strike 170 USD) is American-style, cash-settled, ratio 0.1, last trading day 2026-12-23.
  With those terms, a 100-strike put quoted at 1.725 EUR implies about 150 % volatility and a delta of −0.43
  (ANT at 97.17 USD on 2026-11-19).
- The risk-free rate is the EUR one for every currency, and dividends are taken as zero. The listed and
  warrant expiries differ, so the margin is an estimate.
- The broker's price is used (the holdings feed's price, not a bid/ask mid; the analysis says how old the underlying's close is); the spread paid on entry and exit is not modelled.
