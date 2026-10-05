"""Unified evidence-based German fund classifier (§20 InvStG Teilfreistellung).

THE RULE (audit-fixes-2026-08 todo 22, resolving audit §6.2 aggravator 2 /
Metis B2): a holding's fund class may come ONLY from verifiable positive
evidence:

1. ``holdings.fund_class`` explicitly set by the user  → source ``"user"``;
2. an etf_universe ISIN/composition match              → source ``"etf_universe"``
   (the former ``etf_universe._derive_tf_class`` keyword logic is reused
   INSIDE this match path only, demoted to a hint).

NO name-keyword guessing is applied as a primary source: keywords are never
consulted for holdings that lack composition/ISIN evidence.

If neither evidence source yields a class → ``"other"`` (0 % Teilfreistellung).
This conservative floor can only OVERSTATE the estimated tax, never understate
it — understatement would mean a surprise tax liability, while overstatement
is the safe estimate direction for a German private investor. Consequently no
code path here may silently LOWER Vorabpauschale without positive evidence,
and statutory rates (``TEILFREISTELLUNG_RATES``) are never touched.

This module is PURE: no DB, network, or filesystem access. Callers perform any
etf_universe lookup themselves and embed the matched record under the
``etf_universe_match`` key of the ``holding`` mapping.
"""
from __future__ import annotations

from typing import Any, Literal, Mapping

FundClass = Literal["aktien", "misch", "immobilien", "other"]
ClassificationSource = Literal["user", "etf_universe", "default_other"]

#: The closed set of classes this classifier may emit.
VALID_FUND_CLASSES: frozenset[str] = frozenset(
    {"aktien", "misch", "immobilien", "other"}
)

# ---------------------------------------------------------------------------
# Keyword hint vocabulary (canonical home; etf_universe re-imports these).
# ---------------------------------------------------------------------------

# Overnight-rate / deposit-swap funds (e.g. XEON.DE tracking EUR €STR) are
# cash equivalents, not equities or duration-bearing bonds — a distinct set
# from BOND_KEYWORDS because they need to be excluded from equity-style
# scoring/prompting even though the tax treatment (both land outside
# "aktien") is the same as a bond fund. Keywords must not include "swap"
# alone (equity index swap-UCITS ETFs also use synthetic replication).
MONEY_MARKET_KEYWORDS: frozenset[str] = frozenset(
    {
        "overnight rate", "overnight index", "overnight deposit",
        "eonia", "estr", "€str", "sonia", "sofr", "ester",
    }
)

BOND_KEYWORDS: frozenset[str] = frozenset(
    {
        "bond", "anleihe", "fixed income", "treasury", "government bond",
        "corporate bond", "aggregate bond", "inflationsindexiert",
        "pfandbrief", "kurzlauf", "short term",
    }
)

# Both bond and money-market funds get the "other" (non-"aktien") German tax
# treatment, so the tf_class hint below checks the union; instrument-type
# classification checks each set separately, since a bond fund and a
# money-market fund are distinct InstrumentTypes with different scoring
# treatment (see instrument_taxonomy.py). "money market" itself stays out of
# BOND_KEYWORDS so is_bond_fund/is_money_market_fund remain disjoint.
KNOWN_BOND_KEYWORDS: frozenset[str] = (
    BOND_KEYWORDS | {"money market"} | MONEY_MARKET_KEYWORDS
)


def normalize_fund_class(value: Any) -> FundClass | None:
    """Return the canonical class for a user-set value, else ``None``.

    Only values in :data:`VALID_FUND_CLASSES` count as verifiable evidence;
    anything else (empty, unknown spelling) is discarded so classification
    falls through to the next evidence source rather than guessing.
    """
    if not value:
        return None
    candidate = str(value).strip().lower()
    if candidate in VALID_FUND_CLASSES:
        return candidate  # type: ignore[return-value]
    return None


def class_from_etf_universe_hint(name: Any, strategy: Any = None) -> FundClass:
    """Derive a class from a MATCHED etf_universe record's metadata.

    This is the former ``etf_universe._derive_tf_class`` logic, verbatim in
    semantics: bond/money-market keywords in the fund name negate to
    ``"other"``, otherwise the record defaults to ``"aktien"`` (30 %
    Teilfreistellung). It is only meaningful INSIDE an etf_universe match —
    i.e. for records whose ISIN/composition membership in the UCITS universe
    is already established. Never apply it to holdings without such evidence.
    """
    name_lower = str(name or "").lower()
    if any(kw in name_lower for kw in KNOWN_BOND_KEYWORDS):
        return "other"
    return "aktien"


def classify_fund_class_with_source(
    holding: Mapping[str, Any],
) -> tuple[FundClass, ClassificationSource]:
    """Classify *holding* and report WHERE the class came from.

    ``holding`` is a mapping with optional keys:

    - ``fund_class``: user-set class string (evidence 1);
    - ``etf_universe_match``: mapping of the etf_universe record matched on
      ISIN/composition by the caller (evidence 2); ``None``/absent means no
      match;
    - ``isin`` / ``name``: informational only — never used to guess.
    """
    # Evidence 1: explicit user classification wins unconditionally.
    user_class = normalize_fund_class(holding.get("fund_class"))
    if user_class is not None:
        return user_class, "user"

    # Evidence 2: caller-verified etf_universe ISIN/composition match.
    match = holding.get("etf_universe_match")
    if isinstance(match, Mapping) and match:
        return (
            class_from_etf_universe_hint(match.get("name"), match.get("strategy")),
            "etf_universe",
        )

    # No evidence → conservative floor (0 % Teilfreistellung).
    return "other", "default_other"


def classify_fund_class(holding: Mapping[str, Any]) -> FundClass:
    """Classify *holding* per THE RULE; see :func:`classify_fund_class_with_source`."""
    return classify_fund_class_with_source(holding)[0]
