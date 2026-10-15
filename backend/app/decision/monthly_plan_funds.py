"""One suggested fund per sleeve, with a one-line reason (ADR 0019 §2).

Suggest-only: this module never places orders and never moves money. It ranks
curated justETF-listed UCITS candidates that track the sleeve's index by
yearly fee, tracking difference against same-index peers, fund size,
replication, Xetra volume, savings-plan availability and domicile (ADR 0019,
amended 2026-10-14). ``plan_tilt_isins`` stays a manual override: a set override
always wins and the why-line says so.

Figures are curated in code (there is no justETF API) and go stale — confirm
every ISIN on justETF before buying. Changing a candidate or a weight is a
deliberate code change a reviewer can see.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FundCandidate:
    sleeve: str  # "core" | "tilt" | "bond_short" | "bond_mid" | "bond_aggregate" | "cash" | "gold" | "universe"
    isin: str
    name: str
    index: str
    ter_pct: float  # for an ETC: its running custody cost
    size_eur_bn: float  # 0 when unknown
    replication: str  # "full" | "sampled" | "synthetic" | "physical"
    xetra_volume_eur_m: float  # average daily Xetra turnover; 0 when unknown
    dkb_savings_plan: bool
    scalable_savings_plan: bool
    note: str = ""
    #: The Xetra (or, failing that, EUR) listing whose closes measure tracking difference; "" when none.
    ticker: str = ""


# Curated; TER, size and replication checked against justETF on 2026-10-12.
# Mostly accumulating; see ACC_DIST_NOTE for why. The 2026-10 audit found two
# wrong ISINs here: IE00BP3QZ825 is the Momentum fund (not Value), and
# IE00BF4RF909 does not exist (Small Cap is IE00BF4RFH31).
CANDIDATES: tuple[FundCandidate, ...] = (
    FundCandidate("core", "IE00B4L5Y983", "iShares Core MSCI World (Acc)", "MSCI World",
                  0.20, 133.2, "sampled", 250.0, True, True, ticker="EUNL.DE"),
    FundCandidate("core", "IE00BK5BQT80", "Vanguard FTSE All-World (Acc)", "FTSE All-World",
                  0.14, 55.4, "sampled", 120.0, True, True, ticker="VWCE.DE"),
    FundCandidate("core", "IE00BJ0KDQ92", "Xtrackers MSCI World 1C", "MSCI World",
                  0.12, 22.0, "sampled", 90.0, True, True, ticker="XDWD.DE"),
    FundCandidate("tilt", "IE00BP3QZB59", "iShares Edge MSCI World Value Factor", "MSCI World Value",
                  0.25, 6.7, "full", 8.0, True, True, ticker="IS3S.DE"),
    FundCandidate("tilt", "IE00BP3QZ825", "iShares Edge MSCI World Momentum Factor (Acc)", "MSCI World Momentum",
                  0.25, 5.7, "sampled", 8.0, True, True, ticker="IS3R.DE"),
    FundCandidate("tilt", "IE00BL25JP72", "Xtrackers MSCI World Momentum 1C", "MSCI World Momentum",
                  0.25, 2.1, "full", 5.0, True, True, ticker="XDEM.DE"),
    FundCandidate("tilt", "IE00BL25JL35", "Xtrackers MSCI World Quality 1C", "MSCI World Quality",
                  0.25, 2.6, "full", 4.0, True, True, ticker="XDEQ.DE"),
    FundCandidate("tilt", "IE00BF4RFH31", "iShares MSCI World Small Cap (Acc)", "MSCI World Small Cap",
                  0.35, 7.9, "sampled", 10.0, True, True, ticker="IUSN.DE"),
    # Asset-class lines (plan 2026-10-12, D4); used only when the owner sets a target.
    FundCandidate("bond_short", "IE00B3VTMJ91", "iShares € Govt Bond 1-3yr (Acc)", "Euro Govt 1-3Y",
                  0.10, 2.5, "sampled", 0.0, False, False,
                  note="No Xetra listing; trades in EUR on the LSE (CBE3) and Euronext Amsterdam."),
    FundCandidate("bond_mid", "IE00B4WXJJ64", "iShares Core € Govt Bond (Dist)", "Euro Govt",
                  0.07, 5.4, "sampled", 0.0, False, False, note="Distributing; all maturities (about 7y duration)."),
    FundCandidate("bond_aggregate", "IE00BDBRDM35", "iShares Core Global Aggregate Bond EUR Hedged (Acc)",
                  "Bloomberg Global Aggregate (EUR hedged)", 0.10, 2.5, "sampled", 0.0, False, False),
    FundCandidate("cash", "LU0290358497", "Xtrackers II EUR Overnight Rate Swap 1C", "€STR",
                  0.10, 23.1, "synthetic", 0.0, False, False,
                  note="Swap-based by design: it pays €STR minus its fee."),
    FundCandidate("gold", "DE000A0S9GB0", "Xetra-Gold (ETC)", "Gold",
                  0.36, 0.0, "physical", 0.0, False, False,
                  note=("An ETC (a bond backed by physical gold, deliverable on request), not a fund. No TER; "
                        "Clearstream's custody fee of about 0.30 % a year plus VAT is priced in. Confirm with "
                        "your broker.")),
)

#: Sleeves that are asset-class lines rather than equity sleeves.
ASSET_CLASS_SLEEVES = ("bond_short", "bond_mid", "bond_aggregate", "cash", "gold")

#: ADR 0019 §2 weights, as amended 2026-10-14 (plan C4). Higher is better; each component scores 0-1.
W_TER = 0.25
W_TD = 0.20
W_SIZE = 0.15
W_REPLICATION = 0.10
W_VOLUME = 0.15
W_PLAN = 0.10
#: Every curated equity fund is Irish today, so this only reorders the
#: info-only best-per-index list; it ranks a curated suggestion once a non-Irish
#: US-heavy fund is curated.
W_DOMICILE = 0.05

#: Tracking difference against peers, in % a year, that scores 0 or 1; unknown scores 0.5.
TD_FULL_SCALE_PCT = 0.15
#: Index words that mean at least 30 % US equity, where an Irish fund pays about 14 bp less US withholding.
_US_HEAVY_WORDS = ("world", "all-world", "acwi", "s&p 500", "usa", "nasdaq", "north america", "us equity")

_REPLICATION_SCORE = {"full": 1.0, "physical": 1.0, "sampled": 0.5, "synthetic": 0.0}


def replication_kind(raw: str | None) -> str:
    """justETF's replication text as ``full`` / ``sampled`` / ``synthetic``."""
    r = (raw or "").lower()
    if "swap" in r or "synthetic" in r:
        return "synthetic"
    if "sampl" in r or "optimi" in r:
        return "sampled"
    if "full" in r or "physical" in r:
        return "full"
    return "sampled"


def candidate_from_universe(record: dict[str, Any], *, index: str, sleeve: str = "universe") -> FundCandidate:
    """A justETF universe row as a scorable candidate (D4).

    TER, size and replication come from justETF; it has no Xetra volume, so
    that component scores 0 for every row alike. The savings-plan flag comes
    from the curated table when the ISIN is there, else False.
    """
    isin = str(record.get("isin") or "").upper()
    curated = next((c for c in CANDIDATES if c.isin == isin), None)
    size_m = record.get("size_eur_m")
    try:
        size_bn = float(size_m) / 1000.0 if size_m is not None else 0.0
    except (TypeError, ValueError):
        size_bn = 0.0
    try:
        ter = float(record.get("ter") or 0.0)
    except (TypeError, ValueError):
        ter = 0.0
    return FundCandidate(
        sleeve=sleeve, isin=isin, name=str(record.get("name") or isin), index=index,
        ter_pct=ter, size_eur_bn=size_bn, replication=replication_kind(record.get("replication")),
        xetra_volume_eur_m=0.0,
        dkb_savings_plan=curated.dkb_savings_plan if curated else False,
        scalable_savings_plan=curated.scalable_savings_plan if curated else False,
    )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def us_withholding_drag(c: FundCandidate) -> bool:
    """True for a non-Irish equity fund with at least 30 % US equity (about 14 bp a year more US withholding)."""
    if c.sleeve in ASSET_CLASS_SLEEVES or c.isin.startswith("IE"):
        return False
    index = c.index.lower()
    if "ex us" in index or "ex-us" in index:
        return False
    return any(w in index for w in _US_HEAVY_WORDS)


def score_fund(c: FundCandidate, peer_td: Mapping[str, float] | None = None) -> float:
    """Deterministic 0-1 score; the ranking the suggestion shows its work from.

    *peer_td* maps ISIN to the fund's tracking difference against its
    same-index peers in % a year (:mod:`app.decision.fund_tracking`); a fund
    without one scores the neutral 0.5 on that line.
    """
    ter = _clamp01((0.50 - c.ter_pct) / (0.50 - 0.05))
    td_pct = (peer_td or {}).get(c.isin)
    td = 0.5 if td_pct is None else _clamp01(0.5 + 0.5 * td_pct / TD_FULL_SCALE_PCT)
    size = _clamp01(math.log10(c.size_eur_bn + 1.0) / math.log10(11.0))
    replication = _REPLICATION_SCORE.get(c.replication, 0.0)
    volume = _clamp01(math.log10(c.xetra_volume_eur_m + 1.0) / math.log10(101.0))
    plan = 1.0 if (c.dkb_savings_plan or c.scalable_savings_plan) else 0.0
    domicile = 0.0 if us_withholding_drag(c) else 1.0
    return (W_TER * ter + W_TD * td + W_SIZE * size + W_REPLICATION * replication + W_VOLUME * volume
            + W_PLAN * plan + W_DOMICILE * domicile)


def _why(c: FundCandidate, score: float, override: bool = False, td_pct: float | None = None) -> str:
    plans = "DKB + Scalable savings plans" if (c.dkb_savings_plan and c.scalable_savings_plan) \
        else "DKB savings plan" if c.dkb_savings_plan \
        else "Scalable savings plan" if c.scalable_savings_plan else "no savings plan"
    cost = f"running cost about {c.ter_pct:.2f} %" if c.sleeve == "gold" else f"TER {c.ter_pct:.2f} %"
    size = f"€{c.size_eur_bn:g}bn" if c.size_eur_bn > 0 else "size n/a"
    line = f"{c.name} ({c.isin}): {cost}, {size}, {c.replication} replication, {plans}."
    if td_pct is not None:
        line += f" Over 3 years it returned {td_pct:+.2f} % a year against the median of its same-index peers."
    if us_withholding_drag(c):
        line += " Not Irish-domiciled: about 0.14 % a year more US withholding tax than an Irish fund."
    if c.note:
        line += f" {c.note}"
    if override:
        line += " Chosen by hand (plan_tilt_isins) — the manual override wins over the ranking."
    return line


@dataclass(frozen=True)
class FundSuggestion:
    sleeve: str
    isin: str | None
    name: str
    why: str
    overridden: bool = False


ACC_DIST_NOTE = (
    "Accumulating vs. distributing: while your NV-Bescheinigung (tax exemption "
    "certificate) covers the gains it hardly matters — nothing is taxed either "
    "way. Once it no longer covers you, accumulating funds defer tax through "
    "the yearly Vorabpauschale instead of paying out distributions, so "
    "accumulating is the default. Check the certificate is on file with both "
    "DKB and Scalable, and note its renewal date."
)


def _rank(sleeve: str, peer_td: Mapping[str, float] | None) -> list[FundCandidate]:
    """The sleeve's candidates, best first.

    Tracking difference is measured against funds of the same index, so it
    ranks within an index only. Indexes are ordered by their best fund's score
    without it (core mixes MSCI World and FTSE All-World, a tilt sleeve several
    factor indexes); within an index, tracking difference then picks the fund.
    """
    pool = [c for c in CANDIDATES if c.sleeve == sleeve]
    by_index: dict[str, list[FundCandidate]] = {}
    for c in pool:
        by_index.setdefault(c.index, []).append(c)
    best = {idx: max(score_fund(m) for m in members) for idx, members in by_index.items()}
    order = {idx: i for i, idx in enumerate(sorted(best, key=lambda idx: (-best[idx], idx)))}
    return sorted(pool, key=lambda c: (order[c.index], -score_fund(c, peer_td), c.isin))


def suggest_fund(sleeve: str, override_isins: set[str] | None = None,
                 peer_td: Mapping[str, float] | None = None) -> FundSuggestion:
    """The suggested fund for *sleeve* with its one-line reason; *peer_td* as in :func:`score_fund`."""
    if sleeve == "satellite":
        return FundSuggestion(
            sleeve="satellite", isin=None, name="Single stocks",
            why="No fund: the satellite holds single stocks, unlocked only by the evidence gate.",
        )
    ranked = _rank(sleeve, peer_td)
    td = peer_td or {}
    ordered = sorted((override_isins or set()))
    if ordered:
        shared = f" Tilt money is shared equally across: {', '.join(ordered)}." if len(ordered) > 1 else ""
        first = ordered[0]
        known = next((c for c in CANDIDATES if c.isin == first and c.sleeve == sleeve), None)
        if known is not None:
            return FundSuggestion(sleeve, known.isin, known.name,
                                  _why(known, score_fund(known, peer_td), override=True,
                                       td_pct=td.get(known.isin)) + shared,
                                  overridden=True)
        return FundSuggestion(
            sleeve, first, "Hand-picked fund",
            f"{first}: chosen by hand (plan settings) — the manual override wins over the ranking.{shared} "
            "Confirm it tracks the sleeve's index before buying.",
            overridden=True,
        )
    if not ranked:
        return FundSuggestion(sleeve, None, "No candidate",
                              f"No curated candidate for the {sleeve} sleeve yet.")
    best = ranked[0]
    return FundSuggestion(sleeve, best.isin, best.name,
                          _why(best, score_fund(best, peer_td), td_pct=td.get(best.isin)))


def suggest_all(tilt_isins: set[str] | None = None, peer_td: Mapping[str, float] | None = None) -> list[dict]:
    """Suggestions for every sleeve, as plain dicts for the plan response."""
    return [asdict(suggest_fund(s, tilt_isins if s == "tilt" else None, peer_td))
            for s in ("core", "tilt", "satellite")]
