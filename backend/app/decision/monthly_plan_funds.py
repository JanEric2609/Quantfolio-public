"""One suggested fund per sleeve, with a one-line reason (ADR 0019 §2).

Suggest-only: this module never places orders and never moves money. It ranks
curated justETF-listed UCITS candidates that track the sleeve's index by
yearly fee, fund size, replication, Xetra volume and savings-plan
availability. ``plan_tilt_isins`` stays a manual override: a set override
always wins and the why-line says so.

Figures are curated in code (there is no justETF API) and go stale — confirm
every ISIN on justETF before buying. Changing a candidate or a weight is a
deliberate code change a reviewer can see.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FundCandidate:
    sleeve: str  # "core" | "tilt"
    isin: str
    name: str
    index: str
    ter_pct: float
    size_eur_bn: float
    replication: str  # "full" | "sampled" | "synthetic"
    xetra_volume_eur_m: float  # average daily Xetra turnover
    dkb_savings_plan: bool
    scalable_savings_plan: bool


# Curated October 2026. All accumulating; see ACC_DIST_NOTE for why.
CANDIDATES: tuple[FundCandidate, ...] = (
    FundCandidate("core", "IE00B4L5Y983", "iShares Core MSCI World (Acc)", "MSCI World",
                  0.20, 80.0, "full", 250.0, True, True),
    FundCandidate("core", "IE00BK5BQT80", "Vanguard FTSE All-World (Acc)", "FTSE All-World",
                  0.22, 20.0, "sampled", 120.0, True, True),
    FundCandidate("core", "IE00BJ0KDQ92", "Xtrackers MSCI World 1C", "MSCI World",
                  0.19, 12.0, "full", 90.0, True, True),
    FundCandidate("tilt", "IE00BP3QZ825", "iShares MSCI World Value Factor", "MSCI World Value",
                  0.30, 3.5, "full", 8.0, True, True),
    FundCandidate("tilt", "IE00BL25JP72", "Xtrackers MSCI World Momentum 1C", "MSCI World Momentum",
                  0.25, 1.8, "full", 5.0, True, True),
    FundCandidate("tilt", "IE00BL25JL35", "Xtrackers MSCI World Quality 1C", "MSCI World Quality",
                  0.25, 1.5, "sampled", 4.0, True, True),
    FundCandidate("tilt", "IE00BF4RF909", "iShares MSCI World Small Cap (Acc)", "MSCI World Small Cap",
                  0.35, 4.0, "sampled", 10.0, True, True),
)

#: ADR 0019 §2 weights. Higher is better; each component scores 0-1.
W_TER = 0.40
W_SIZE = 0.20
W_REPLICATION = 0.15
W_VOLUME = 0.15
W_PLAN = 0.10

_REPLICATION_SCORE = {"full": 1.0, "sampled": 0.5, "synthetic": 0.0}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_fund(c: FundCandidate) -> float:
    """Deterministic 0-1 score; the ranking the suggestion shows its work from."""
    import math

    ter = _clamp01((0.50 - c.ter_pct) / (0.50 - 0.05))
    size = _clamp01(math.log10(c.size_eur_bn + 1.0) / math.log10(11.0))
    replication = _REPLICATION_SCORE.get(c.replication, 0.0)
    volume = _clamp01(math.log10(c.xetra_volume_eur_m + 1.0) / math.log10(101.0))
    plan = 1.0 if (c.dkb_savings_plan or c.scalable_savings_plan) else 0.0
    return W_TER * ter + W_SIZE * size + W_REPLICATION * replication + W_VOLUME * volume + W_PLAN * plan


def _why(c: FundCandidate, score: float, override: bool = False) -> str:
    plans = "DKB + Scalable savings plans" if (c.dkb_savings_plan and c.scalable_savings_plan) \
        else "DKB savings plan" if c.dkb_savings_plan \
        else "Scalable savings plan" if c.scalable_savings_plan else "no savings plan"
    line = (
        f"{c.name} ({c.isin}): TER {c.ter_pct:.2f} %, €{c.size_eur_bn:g}bn, "
        f"{c.replication} replication, {plans}."
    )
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


def suggest_fund(sleeve: str, override_isins: set[str] | None = None) -> FundSuggestion:
    """The suggested fund for *sleeve* with its one-line reason."""
    if sleeve == "satellite":
        return FundSuggestion(
            sleeve="satellite", isin=None, name="Single stocks",
            why="No fund: the satellite holds single stocks, unlocked only by the evidence gate.",
        )
    ranked = sorted(
        (c for c in CANDIDATES if c.sleeve == sleeve),
        key=lambda c: (-score_fund(c), c.isin),
    )
    ordered = sorted((override_isins or set()))
    if ordered:
        shared = f" Tilt money is shared equally across: {', '.join(ordered)}." if len(ordered) > 1 else ""
        first = ordered[0]
        known = next((c for c in CANDIDATES if c.isin == first and c.sleeve == sleeve), None)
        if known is not None:
            return FundSuggestion(sleeve, known.isin, known.name,
                                  _why(known, score_fund(known), override=True) + shared,
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
    return FundSuggestion(sleeve, best.isin, best.name, _why(best, score_fund(best)))


def suggest_all(tilt_isins: set[str] | None = None) -> list[dict]:
    """Suggestions for every sleeve, as plain dicts for the plan response."""
    return [asdict(suggest_fund(s, tilt_isins if s == "tilt" else None))
            for s in ("core", "tilt", "satellite")]
