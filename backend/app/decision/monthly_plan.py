"""This month's plan: where the monthly contribution goes, and whether anything else changes.

The answer to the one question the owner actually has each month. The book is
split into three sleeves (docs/archive/audits/2026-09-24-why-it-does-not-work §6-§7):

* **core** - broad global equity ETFs (the passive core). The benchmark; needs
  no evidence.
* **tilt** - documented factor premia via cheap UCITS ETFs, capped by the
  tracking-error budget ("tight" = 15 %). Allowed only once a prior-informed
  backtest on the JKP panel passes (Phase 3).
* **satellite** - single stocks, capped at 10 %. Allowed only once a strategy
  passes the hard DSR/PBO gate; bought in lumps large enough that DKB's flat
  order fee stays small.

How new money reaches the book is the owner's choice (``plan_contribution_mode``,
``plan_broker``): a standing savings plan, or orders placed by hand each month,
at DKB or Scalable Capital. Every action names its broker and that broker's fee;
"auto" picks the cheapest synced depot for each buy, and a sale is made at the
depot that holds the position (FIFO runs per depot). Positions at both brokers,
and those entered by hand under Portfolio -> Holdings, all count toward the
sleeves.

The plan also reads what already runs: the savings plans synced from Scalable
(sorted into sleeves, so the plan never asks for a plan that exists) and the
cash above the owner's fixed emergency reserve (``emergency_reserve_eur``),
offered as an optional one-off buy. Giro money is never counted.

Allocation is contribution-first: new money goes to whichever sleeve is below
target. A sale is proposed only when an unlocked sleeve drifts more than the
band (``plan_drift_band_pp``) above its target and a year of contributions
would not bring it back, because every sale is a taxable event under German
law. Until a sleeve is unlocked its target is 0 %, so the usual answer is "the
savings plan runs as usual, nothing else to do" (or "buy the core", without
one). Existing positions in a
locked sleeve are left alone, never sold.

Only evidence sets a target (report Phase 4): the tilt from a passing
factor-premia card, the satellite from a passing evidence-gate run. Nothing
else, the LLM loop included, emits target weights for the real book.

Advisory only. Nothing here places orders.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.allocation import allocate_contribution, drift_sales
from app.foundation.broker_fees import SAVINGS_PLAN_FEE_EUR, order_fee_eur
from app.foundation.broker_status import load_raw
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION, latest_evidence_gate, latest_factor_evidence_cards
from app.foundation.fx_rates import convert as fx_convert
from app.foundation.live_positions import BROKER_SOURCES, live_positions, manual_holdings_filter
from app.foundation.models.entities import BrokerSyncLog, ConnectedAccount, DkbAccount, Holding, Portfolio
from app.foundation.portfolio_utils import holding_market_value
from app.foundation.settings import get_public_settings
from app.decision.monthly_plan_funds import ACC_DIST_NOTE, suggest_all

# Broad, market-cap-weighted global equity ETFs, and the emerging-markets funds
# that complete a world ETF (MSCI World + EM is the ACWI). Any of these counts
# as core: holding the "wrong" world ETF is not worth a taxable switch.
CORE_ETF_ISINS: dict[str, str] = {
    "IE00B4L5Y983": "iShares Core MSCI World (Acc)",
    "IE00B0M62Q58": "iShares MSCI World (Dist)",
    "IE00BJ0KDQ92": "Xtrackers MSCI World 1C",
    "IE00BFY0GT14": "SPDR MSCI World",
    "IE00B6R52259": "iShares MSCI ACWI (Acc)",
    "IE00B3YLTY66": "SPDR MSCI ACWI IMI",
    "IE00BK5BQT80": "Vanguard FTSE All-World (Acc)",
    "IE00B3RBWM25": "Vanguard FTSE All-World (Dist)",
    "IE00BKM4GZ66": "iShares Core MSCI Emerging Markets IMI (Acc)",
    "IE00B4L5YC18": "iShares MSCI Emerging Markets (Acc)",
    "IE00BTJRMP35": "Xtrackers MSCI Emerging Markets 1C",
    "IE00BK5BR733": "Vanguard FTSE Emerging Markets (Acc)",
    "IE00B3VVMM84": "Vanguard FTSE Emerging Markets (Dist)",
}

# Look-through: the emerging-markets share of each core fund, approximate.
# World funds hold none, the all-world indices about a tenth, EM funds all.
CORE_EM_SHARE: dict[str, float] = {
    "IE00B4L5Y983": 0.0, "IE00B0M62Q58": 0.0, "IE00BJ0KDQ92": 0.0, "IE00BFY0GT14": 0.0,
    "IE00B6R52259": 0.10, "IE00B3YLTY66": 0.10, "IE00BK5BQT80": 0.10, "IE00B3RBWM25": 0.10,
    "IE00BKM4GZ66": 1.0, "IE00B4L5YC18": 1.0, "IE00BTJRMP35": 1.0, "IE00BK5BR733": 1.0, "IE00B3VVMM84": 1.0,
}
# Emerging markets' weight in the world market (MSCI ACWI, FTSE All-World).
WORLD_EM_SHARE_PCT = 10.0

SLEEVES = ("core", "tilt", "satellite")
SLEEVE_LABELS = {"core": "Core", "tilt": "Factor tilt", "satellite": "Stock picks"}

# Where and how the monthly contribution is invested (plan settings).
CONTRIBUTION_MODES = ("savings_plan", "manual_orders")
BROKERS = ("dkb", "scalable")
BROKER_CHOICES = ("auto", *BROKERS)
BROKER_LABELS = {"dkb": "DKB", "scalable": "Scalable Capital"}

# Savings-plan frequencies as Scalable reports them, in executions per month.
FREQUENCY_PER_MONTH = {
    "WEEKLY": 52 / 12,
    "BIWEEKLY": 26 / 12,
    "EVERY_TWO_WEEKS": 26 / 12,
    "MONTHLY": 1.0,
    "EVERY_TWO_MONTHS": 0.5,
    "BIMONTHLY": 0.5,
    "QUARTERLY": 1 / 3,
    "HALF_YEARLY": 1 / 6,
    "SEMI_ANNUALLY": 1 / 6,
    "YEARLY": 1 / 12,
    "ANNUALLY": 1 / 12,
}

# Fees live in foundation/broker_fees.py (SAVINGS_PLAN_FEE_EUR, order_fee_eur).
SATELLITE_BROKER_LABELS = {"dkb": "DKB", "scalable": "Scalable Capital"}

# A plan whose next execution lies further back than this is treated as not
# running (sc sends no paused flag): listed, but left out of the totals.
OVERDUE_PLAN_DAYS = 3


@dataclass
class SleeveState:
    key: str
    label: str
    current_eur: float
    current_pct: float
    target_pct: float
    max_pct: float
    unlocked: bool
    status: str
    contribution_eur: float = 0.0
    after_eur: float = 0.0
    after_pct: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    # Proposed only past the drift band; the proceeds go to the other sleeves.
    sale_eur: float = 0.0
    reinvest_eur: float = 0.0


@dataclass
class PlanAction:
    sleeve: str
    kind: str  # "savings_plan" | "order" | "sale"
    amount_eur: float
    instrument: str
    ticker: str | None
    isin: str | None
    note: str
    # Where to place it: a buy at the plan's broker, a sale at the depot that
    # holds the position (None for a holding entered by hand).
    broker: str | None = None
    broker_label: str | None = None
    # The depot a sale comes from (several depots at one broker).
    account_id: str | None = None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _isin_list(value: Any) -> set[str]:
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return {str(i).strip().upper() for i in items if str(i).strip()}


def plan_settings(db: Session) -> dict[str, Any]:
    """Resolve the plan's inputs from public settings (with defaults)."""
    s = get_public_settings(db)
    core_isin = str(s.get("passive_core_isin") or "IE00B4L5Y983").upper()
    core_isins = set(CORE_ETF_ISINS) | {core_isin} | _isin_list(s.get("plan_core_isins"))
    mode = str(s.get("plan_contribution_mode") or "savings_plan")
    broker = str(s.get("plan_broker") or "auto").lower()
    return {
        "contribution_mode": mode if mode in CONTRIBUTION_MODES else "savings_plan",
        "broker": broker if broker in BROKER_CHOICES else "auto",
        "emergency_reserve_eur": max(0.0, _as_float(s.get("emergency_reserve_eur"), 0.0)),
        "contribution_eur": max(0.0, _as_float(s.get("monthly_contribution_eur"), 1000.0)),
        "tilt_max_pct": min(100.0, max(0.0, _as_float(s.get("plan_tilt_max_pct"), 15.0))),
        "satellite_max_pct": min(100.0, max(0.0, _as_float(s.get("plan_satellite_max_pct"), 10.0))),
        "min_order_eur": max(0.0, _as_float(s.get("plan_min_order_eur"), 1000.0)),
        "drift_band_pp": min(100.0, max(0.0, _as_float(s.get("plan_drift_band_pp"), 5.0))),
        "core_ticker": str(s.get("passive_core_ticker") or "EUNL.DE").upper(),
        "core_isin": core_isin,
        "core_isins": core_isins,
        "tilt_isins": _isin_list(s.get("plan_tilt_isins")) - core_isins,
    }


def _pct(value: float) -> str:
    return f"{value * 100:+.1f} %"


def _tilt_evidence(db: Session, cfg: dict[str, Any]) -> tuple[bool, str, bool]:
    """Whether the tilt may receive money, from the latest world factor-premia run.

    Returns (unlocked, reason, evidence_passed): a pass without a chosen fund is
    "passed but not unlocked". Needs both a passing evidence card and a factor ETF the owner chose
    (``plan_tilt_isins``): the evidence says a premium exists, not which fund
    to buy, and no money moves into an instrument nobody picked.
    """
    cards = latest_factor_evidence_cards(db, TILT_EVIDENCE_REGION)
    if not cards:
        return False, (
            f"No factor tilt has been tested on {TILT_EVIDENCE_REGION} data yet. Run the "
            "factor-premia study on the JKP data (python -m app.lab.factor_premia) to grade "
            "value, momentum and the rest."
        ), False
    passing = sorted(
        (c for c in cards if c.get("passed")),
        key=lambda c: c.get("net_expected_annual") or 0.0,
        reverse=True,
    )
    if not passing:
        return False, (
            f"None of the {len(cards)} pre-registered factor strategies passed on "
            f"{cards[0].get('region', 'the JKP')} data, so the tilt stays at 0 %."
        ), False
    best = passing[0]
    names = ", ".join(c.get("label", c.get("strategy", "?")) for c in passing)
    net = best.get("net_expected_annual")
    expected = f" (expected {_pct(net)} a year over the core after decay, costs and tax)" if net is not None else ""
    if not cfg["tilt_isins"]:
        return False, (
            f"{names} passed the evidence check{expected}, but no tilt fund is chosen, so the target "
            f"stays at 0 %. Set one in Plan settings to use it: add the ISIN of {best.get('etf_hint', 'a factor ETF')} "
            "(comma-separated for more than one; the tilt money is shared equally)."
        ), True
    return True, f"Unlocked: {names} passed the evidence check{expected}.", True


def _tilt_instrument(held: list[dict[str, Any]], isin: str | None) -> str:
    for h in held:
        if isin and h["isin"] == isin and h.get("name"):
            return str(h["name"])
    return "Factor ETF"


def _split_tilt(
    held: list[dict[str, Any]], tilt_isins: set[str], amount: float,
) -> list[tuple[str | None, float]]:
    """Share the tilt money equally across the chosen factor ETFs.

    A combined strategy such as value + momentum means one ETF per leg, half
    each. Money goes first to whichever ETF is furthest below its equal share,
    so the legs converge without selling. Returns (isin, amount) pairs with a
    positive amount, in ISIN order.
    """
    if not tilt_isins:
        return [(None, amount)]
    current = {isin: 0.0 for isin in tilt_isins}
    for h in held:
        if h["isin"] in current:
            current[h["isin"]] += h["value_eur"]
    equal = {isin: 100.0 / len(current) for isin in current}
    split = allocate_contribution(current, equal, amount)
    return [(isin, split[isin]) for isin in sorted(split) if split[isin] > 0]


EVIDENCE_STALE_DAYS = 14


def gate_freshness(gate: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """When the evidence gate last ran, over how many trials, and whether that is stale."""
    if gate is None:
        return None
    computed = gate.get("computed_at")
    stale = False
    day: str | None = None
    if computed:
        try:
            ts = datetime.fromisoformat(str(computed))
            ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            stale = (now - ts).days > EVIDENCE_STALE_DAYS
            day = ts.date().isoformat()
        except ValueError:
            pass
    return {"computed_at": computed, "as_of": day, "n_trials": gate["n_trials"], "stale": stale}


def _satellite_evidence(db: Session, now: datetime) -> tuple[bool, str, str | None, dict[str, Any] | None]:
    """Whether the satellite may receive money, from the latest evidence-gate run.

    Returns (unlocked, reason, broker, freshness). The gate (``app.lab.evidence_gate``)
    grades every mined score's after-tax satellite by Deflated Sharpe and PBO;
    only a passing mined score unlocks it, and its broker variant sets the fee.
    """
    gate = latest_evidence_gate(db, TILT_EVIDENCE_REGION)
    if gate is None:
        return False, (
            "No stock-picking strategy has been through the evidence gate yet. Run the "
            "pooled model, the satellite simulation and the gate on the JKP data "
            "(python -m app.lab.pooled_model, app.lab.satellite, app.lab.evidence_gate)."
        ), None, None
    fresh = gate_freshness(gate, now)
    assert fresh is not None
    when = f" (as of {fresh['as_of']}{', out of date: it is re-checked weekly' if fresh['stale'] else ''})" if fresh["as_of"] else ""
    if not gate["satellite_unlocked"] or not gate["unlocked_by"]:
        return False, (
            "No stock-picking strategy passed the evidence gate (Deflated Sharpe against "
            f"{gate['n_trials']} trials, then the backtest-overfitting test){when}: {gate['reason']}"
        ), None, fresh
    best = str(gate["unlocked_by"][0])
    broker = best.split(":", 1)[0]
    return True, f"Unlocked{when}: {gate['reason']}", broker, fresh


def evidence_state(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Which sleeves may receive money, and why.

    The tilt reads the latest factor-premia evidence cards (Phase 3), the
    satellite the latest evidence-gate run (Phase 4). This is the single seam
    evidence is wired into: nothing else may set a target weight for the
    real book.
    """
    tilt_unlocked, tilt_reason, tilt_passed = _tilt_evidence(db, plan_settings(db))
    satellite_unlocked, satellite_reason, satellite_broker, gate = _satellite_evidence(db, now or datetime.now(UTC))
    return {
        "tilt_unlocked": tilt_unlocked,
        "tilt_reason": tilt_reason,
        "tilt_evidence_passed": tilt_passed,
        "satellite_unlocked": satellite_unlocked,
        "satellite_reason": satellite_reason,
        "satellite_broker": satellite_broker,
        "satellite_gate": gate,
    }


def _holdings(
    db: Session, user_id: str, cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """The real book: synced broker positions (DKB, Scalable) plus holdings entered by hand.

    A hand-entered holding (any ``Holding`` not mirrored from a broker sync) is
    valued like the wealth summary values it:
    latest cached price, else cost. One without an ISIN is matched to the core
    by the core ticker.
    """
    out: list[dict[str, Any]] = []
    synced: datetime | None = None
    for p in live_positions(db, user_id):
        out.append({
            "isin": (p.isin or "").upper(),
            "ticker": p.ticker,
            "name": p.name,
            "value_eur": float(p.value),
            "cost_eur": float(p.avg_buy_price * p.quantity) if p.avg_buy_price is not None else None,
            "broker": p.source,
            "account_id": p.account_id,
        })
        if p.last_synced is not None:
            ts = p.last_synced if p.last_synced.tzinfo else p.last_synced.replace(tzinfo=UTC)
            synced = ts if synced is None or ts > synced else synced

    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
    manual = (
        db.query(Holding)
        .filter(Holding.portfolio_id == portfolio.id, manual_holdings_filter())
        .all()
        if portfolio is not None else []
    )
    for h in manual:
        isin = (h.isin or "").upper()
        if not isin and cfg and (h.ticker or "").upper() == cfg["core_ticker"]:
            isin = cfg["core_isin"]
        out.append({
            "isin": isin,
            "ticker": h.ticker,
            "name": h.name,
            "value_eur": float(holding_market_value(db, h)),
            # Booked in the holding's own currency; in EUR like its value, so
            # the gain share compares like with like.
            "cost_eur": (
                fx_convert(float(h.avg_buy_price * h.quantity), (h.currency or "EUR"), "EUR", db)
                if h.avg_buy_price is not None else None
            ),
            "broker": None,
            "account_id": None,
        })
    return out, synced


def connected_brokers(db: Session, user_id: str) -> list[str]:
    """Brokers with a synced depot, in ``BROKERS`` order."""
    out: list[str] = []
    if db.query(DkbAccount.id).filter(DkbAccount.user_id == user_id, DkbAccount.type == "depot").first():
        out.append("dkb")
    if _broker_depots(db, user_id):
        out.append("scalable")
    return out


def _broker_depots(db: Session, user_id: str) -> list[ConnectedAccount]:
    return (
        db.query(ConnectedAccount)
        .filter(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.source.in_(BROKER_SOURCES),
            ConnectedAccount.account_type == "depot",
        )
        .order_by(ConnectedAccount.created_at)
        .all()
    )


def depot_label(db: Session, broker: str, account_id: str) -> str:
    """Name one depot when a broker holds the position in more than one."""
    if broker == "dkb":
        dkb = db.get(DkbAccount, account_id)
        iban = (dkb.iban or "") if dkb is not None else ""
        return f"DKB depot …{iban[-4:]}" if iban else "DKB depot"
    account = db.get(ConnectedAccount, account_id)
    return account.name if account is not None and account.name else BROKER_LABELS.get(broker, broker)


def buy_broker(
    cfg: dict[str, Any], connected: list[str], kind: str, amount: float, name: str | None = None,
) -> str:
    """The broker a buy goes to: the owner's choice, or the cheapest synced depot."""
    if cfg["broker"] in BROKERS:
        return cfg["broker"]
    candidates = connected or ["dkb"]

    def fee(broker: str) -> float:
        return SAVINGS_PLAN_FEE_EUR[broker] if kind == "savings_plan" else order_fee_eur(broker, amount, name)

    return min(candidates, key=lambda b: (fee(b), BROKERS.index(b)))


def _plan_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def running_savings_plans(
    db: Session, user_id: str, cfg: dict[str, Any], *, today: date | None = None,
) -> list[dict[str, Any]]:
    """The savings plans each synced broker runs, with their sleeve and monthly amount.

    ``monthly_eur`` is None for a frequency the plan does not know; such a plan
    is listed but left out of the totals. A plan is ``running`` unless sc marks
    it paused or its next execution date is more than ``OVERDUE_PLAN_DAYS`` in
    the past; a plan that is not running is listed with its reason but also
    left out of the totals.
    """
    today = today or datetime.now(UTC).date()
    out: list[dict[str, Any]] = []
    for account in _broker_depots(db, user_id):
        for item in load_raw(account).get("savings_plans") or []:
            if not isinstance(item, dict):
                continue
            amount = _as_float(item.get("amount"), 0.0)
            if amount <= 0:
                continue
            frequency = str(item.get("frequency") or "MONTHLY").upper()
            per_month = FREQUENCY_PER_MONTH.get(frequency)
            isin = str(item.get("isin") or "").upper()
            next_date = _plan_date(item.get("next_execution_date"))
            not_running: str | None = None
            if item.get("paused") is True:
                not_running = "paused"
            elif next_date is not None and (today - next_date).days > OVERDUE_PLAN_DAYS:
                not_running = "overdue"
            rate = _decimal_or_none(item.get("dynamization_rate"))
            out.append({
                "broker": account.source,
                "broker_label": BROKER_LABELS.get(account.source, account.institution or account.source),
                "name": str(item.get("name") or isin or "Savings plan"),
                "isin": isin or None,
                "sleeve": classify(isin, cfg),
                "amount_eur": _round_eur(amount),
                "frequency": frequency,
                "monthly_eur": _round_eur(amount * per_month) if per_month is not None else None,
                "next_execution_date": item.get("next_execution_date"),
                "dynamization_rate": rate if rate else None,
                "running": not_running is None,
                "not_running_reason": not_running,
            })
    return out


def plans_freshness(db: Session, user_id: str) -> dict[str, Any]:
    """When the savings plans were last really fetched, and whether the latest fetch failed.

    A failed fetch keeps the old plans silently, so the page needs both facts to
    say how far to trust the list.
    """
    stamps = [load_raw(a).get("plans_synced_at") for a in _broker_depots(db, user_id)]
    stamps = [s for s in stamps if isinstance(s, str) and s]
    last = (
        db.query(BrokerSyncLog)
        .filter(
            BrokerSyncLog.user_id == user_id, BrokerSyncLog.source.in_(BROKER_SOURCES), BrokerSyncLog.state.in_(("success", "warning")),
        )
        .order_by(BrokerSyncLog.started_at.desc())
        .first()
    )
    failed = False
    if last is not None:
        try:
            failed = bool(json.loads(last.counts_json or "{}").get("plans_unavailable"))
        except ValueError:
            failed = False
    return {"synced_at": min(stamps) if stamps else None, "last_fetch_failed": failed}


def _decimal_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cash_state(db: Session, user_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Savings and free broker cash, and what is left above the emergency reserve.

    Counts DKB Tagesgeld, broker savings accounts (Scalable's overnight
    account) and each broker's buying power (cash not already earmarked for
    savings plans or open orders). The giro account is spending money and never
    counts. With no reserve set nothing is investable: the plan does not guess
    how much cash the owner needs.
    """
    savings = sum(
        float(a.balance or 0)
        for a in db.query(DkbAccount).filter(DkbAccount.user_id == user_id, DkbAccount.type == "tagesgeld")
    )
    broker_accounts = (
        db.query(ConnectedAccount)
        .filter(ConnectedAccount.user_id == user_id, ConnectedAccount.source.in_(BROKER_SOURCES))
        .all()
    )
    savings += sum(float(a.balance or 0) for a in broker_accounts if a.account_type == "savings")
    broker_cash: dict[str, float] = {}
    cash_accounts = {a.external_id: a for a in broker_accounts if a.account_type == "cash" and a.external_id}
    depots = [a for a in broker_accounts if a.account_type == "depot"]
    # A depot's own cash account is that depot's (its buying power already
    # covers it); the pooled fallback sums only the cash accounts no depot owns.
    owned = {f"{d.external_id}:cash" for d in depots if d.external_id}
    pooled_fallback: set[str] = set()
    for depot in depots:
        cash = load_raw(depot).get("cash")
        free = _decimal_or_none(cash.get("buying_power")) if isinstance(cash, dict) else None
        if free is None:
            # No buying power read yet: the depot's own cash account stands in
            # (the sync names it "<portfolio>:cash"); without one, the broker's
            # unowned cash accounts together, counted once per broker.
            own = cash_accounts.get(f"{depot.external_id}:cash") if depot.external_id else None
            if own is not None:
                free = float(own.balance or 0)
            elif depot.source not in pooled_fallback:
                pooled_fallback.add(depot.source)
                free = sum(
                    float(a.balance or 0) for a in broker_accounts
                    if a.account_type == "cash" and a.source == depot.source and a.external_id not in owned
                )
            else:
                free = 0.0
        broker_cash[depot.source] = broker_cash.get(depot.source, 0.0) + max(0.0, free)
    reserve = cfg["emergency_reserve_eur"]
    total = savings + sum(broker_cash.values())
    return {
        "savings_eur": _round_eur(savings),
        "broker_cash_eur": _round_eur(sum(broker_cash.values())),
        "broker_cash_by_broker": {k: _round_eur(v) for k, v in broker_cash.items()},
        "emergency_reserve_eur": _round_eur(reserve),
        "reserve_set": reserve > 0,
        "investable_eur": _round_eur(max(0.0, total - reserve)) if reserve > 0 else 0.0,
    }


def core_look_through(positions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The emerging-markets share of the core, looking through the funds.

    Only funds with a known split count; a core ETF added by hand without one
    is reported as ``unknown_eur``. With no known split at all ``em_pct`` is
    None. None when the core is empty.
    """
    known = em = unknown = 0.0
    for p in positions:
        share = CORE_EM_SHARE.get(p["isin"])
        if share is None:
            unknown += p["value_eur"]
            continue
        known += p["value_eur"]
        em += p["value_eur"] * share
    if known <= 0 and unknown <= 0:
        return None
    return {
        "em_eur": _round_eur(em),
        "em_pct": round(100.0 * em / known, 1) if known > 0 else None,
        "world_em_pct": WORLD_EM_SHARE_PCT,
        "unknown_eur": _round_eur(unknown),
    }


def classify(isin: str, cfg: dict[str, Any]) -> str:
    if isin in cfg["core_isins"]:
        return "core"
    if isin in cfg["tilt_isins"]:
        return "tilt"
    return "satellite"


def _gain_share(position: dict[str, Any]) -> float:
    """The share of a position's value that is unrealised gain; unknown cost sorts last."""
    cost = position.get("cost_eur")
    if cost is None or position["value_eur"] <= 0:
        return float("inf")
    return 1.0 - cost / position["value_eur"]


def _sale_split(held: list[dict[str, Any]], amount: float) -> list[tuple[dict[str, Any], float]]:
    """Take *amount* from the positions with the least gain per euro first, larger ones on a tie.

    The same fund at DKB and at Scalable is two positions with their own FIFO
    chains (§ 20 Abs. 4 S. 7 EStG), so where to sell is a choice: the depot
    whose position carries the smallest gain per euro realises the least
    taxable gain now.
    """
    out: list[tuple[dict[str, Any], float]] = []
    left = amount
    for h in sorted(held, key=lambda p: (_gain_share(p), -p["value_eur"])):
        if left <= 0.005:
            break
        take = min(left, h["value_eur"])
        if take > 0:
            out.append((h, take))
            left -= take
    return out


def _fee_eur(fee: float) -> str:
    return eur(fee) if float(fee).is_integer() else f"{fee:.2f}".replace(".", ",") + " €"


def eur(amount: float) -> str:
    """German-style whole-euro amount ("1.000 €"), matching the frontend's de-DE formatting."""
    return f"{amount:,.0f}".replace(",", ".") + " €"


def tracking_error_label(tilt_max_pct: float) -> str:
    """Name the tracking-error budget implied by the tilt cap (report §7, Q2)."""
    if tilt_max_pct <= 15.0:
        return "Tight"
    if tilt_max_pct <= 35.0:
        return "Moderate"
    return "Loose"


def _round_eur(x: float) -> float:
    return round(x, 2)


def sleeve_targets_from(cfg: dict[str, Any], evidence: dict[str, Any]) -> tuple[dict[str, float], dict[str, bool]]:
    """Target percent and unlocked flag per sleeve: a sleeve without passing evidence is held at 0 %."""
    unlocked = {
        "core": True,
        "tilt": bool(evidence["tilt_unlocked"]),
        "satellite": bool(evidence["satellite_unlocked"]),
    }
    targets = {
        "tilt": cfg["tilt_max_pct"] if unlocked["tilt"] else 0.0,
        "satellite": cfg["satellite_max_pct"] if unlocked["satellite"] else 0.0,
    }
    targets["core"] = max(0.0, 100.0 - targets["tilt"] - targets["satellite"])
    return targets, unlocked


def sleeve_plan(db: Session, user_id: str) -> dict[str, Any]:
    """The plan's sleeve targets for the rebalance view.

    Targets, unlocked flags, band, an ISIN classifier, the chosen tilt ISINs
    and ``other_budget_eur``: running savings plans into a locked sleeve (stock
    picks) are the owner's own budget and come out of the monthly contribution
    before the core is planned, exactly as in ``build_monthly_plan``.
    """
    cfg = plan_settings(db)
    targets, unlocked = sleeve_targets_from(cfg, evidence_state(db))
    other_budget = 0.0
    if cfg["contribution_mode"] != "manual_orders":
        holdings, _ = _holdings(db, user_id, cfg)
        current = {k: 0.0 for k in SLEEVES}
        for h in holdings:
            current[classify(h["isin"], cfg)] += h["value_eur"]
        split = allocate_contribution(current, targets, cfg["contribution_eur"])
        if 0 < split["satellite"] < cfg["min_order_eur"]:
            split["core"] += split["satellite"]
        locked = sum(
            p["monthly_eur"] for p in running_savings_plans(db, user_id, cfg)
            if p["running"] and p["monthly_eur"] is not None
            and p["sleeve"] in ("tilt", "satellite") and not unlocked[p["sleeve"]]
        )
        other_budget = min(locked, split["core"])
    return {
        "targets": targets,
        "unlocked": unlocked,
        "labels": dict(SLEEVE_LABELS),
        "band_pp": cfg["drift_band_pp"],
        "tilt_isins": sorted(cfg["tilt_isins"]),
        "other_budget_eur": other_budget,
        "classify": lambda isin: classify((isin or "").upper(), cfg),
    }


def build_monthly_plan(db: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Compute this month's plan for *user_id*. Read-only."""
    now = now or datetime.now(UTC)
    cfg = plan_settings(db)
    evidence = evidence_state(db)
    holdings, synced = _holdings(db, user_id, cfg)

    current = {k: 0.0 for k in SLEEVES}
    positions: dict[str, list[dict[str, Any]]] = {k: [] for k in SLEEVES}
    for h in holdings:
        sleeve = classify(h["isin"], cfg)
        current[sleeve] += h["value_eur"]
        positions[sleeve].append(h)
    book = sum(current.values())

    targets, unlocked = sleeve_targets_from(cfg, evidence)

    contribution = cfg["contribution_eur"]
    split = allocate_contribution(current, targets, contribution)

    # A stock order below the minimum lump is not worth DKB's flat fee:
    # route that money to the core instead of placing a small order.
    if 0 < split["satellite"] < cfg["min_order_eur"]:
        split["core"] += split["satellite"]
        split["satellite"] = 0.0

    # Drift band: an unlocked sleeve far above target that contributions
    # cannot fix is sold down, and the proceeds go to the other sleeves as
    # single orders. Sales smaller than the minimum order are not worth it.
    band = cfg["drift_band_pp"]
    sales = {
        k: v for k, v in drift_sales(current, targets, unlocked, contribution, band).items()
        if v >= cfg["min_order_eur"]
    }
    reinvest = {k: 0.0 for k in SLEEVES}
    if sales:
        after_split = {k: current[k] + split[k] - sales.get(k, 0.0) for k in SLEEVES}
        reinvest_targets = {k: 0.0 if k in sales else targets[k] for k in SLEEVES}
        reinvest = allocate_contribution(after_split, reinvest_targets, sum(sales.values()))
        if 0 < reinvest["satellite"] < cfg["min_order_eur"]:
            # Too small for a stock order: keep it in the core, by selling
            # that much less of the core when the core is what is sold.
            if "core" in sales:
                sales["core"] -= reinvest["satellite"]
            else:
                reinvest["core"] += reinvest["satellite"]
            reinvest["satellite"] = 0.0

    total_after = book + contribution
    manual_mode = cfg["contribution_mode"] == "manual_orders"
    contribution_kind = "order" if manual_mode else "savings_plan"
    connected = connected_brokers(db, user_id)
    broker = buy_broker(cfg, connected, contribution_kind, contribution)
    broker_label = BROKER_LABELS[broker]

    # What already runs: the brokers' savings plans, per sleeve. A plan that is
    # not running (paused, or overdue) is listed but never counted.
    all_plans = running_savings_plans(db, user_id, cfg, today=now.date())
    plans = [p for p in all_plans if p["running"]]
    stopped_plans = [p for p in all_plans if not p["running"]]
    plans_by_sleeve = {k: 0.0 for k in SLEEVES}
    for plan in plans:
        if plan["monthly_eur"] is not None:
            plans_by_sleeve[plan["sleeve"]] += plan["monthly_eur"]
    plans_total = sum(plans_by_sleeve.values())
    plan_brokers = " and ".join(sorted({p["broker_label"] for p in plans}))
    locked_plans = {k: plans_by_sleeve[k] for k in ("tilt", "satellite") if plans_by_sleeve[k] > 0 and not unlocked[k]}
    # Running plans into a sleeve that has no target are the owner's own budget
    # (e.g. stock picks): they are taken out of the monthly contribution and the
    # core plans only need the rest. The sleeve caps are unchanged.
    other_budget = 0.0 if manual_mode else min(sum(locked_plans.values()), split["core"])
    split["core"] = max(0.0, split["core"] - other_budget)
    split_shown = dict(split)
    if other_budget > 0:
        scale = other_budget / sum(locked_plans.values())
        for k, v in locked_plans.items():
            split_shown[k] = v * scale
    core_covered = bool(plans) and plans_by_sleeve["core"] >= split["core"] - 0.5

    statuses = {
        "core": (
            "Receives the monthly savings plan." if not manual_mode
            else "Receives this month's contribution; you place the order."
        ),
        "tilt": evidence["tilt_reason"] or "Unlocked by a passing backtest.",
        "satellite": (
            evidence["satellite_reason"] if not unlocked["satellite"]
            else "Unlocked by a strategy that passed the evidence gate."
        ),
    }
    sleeves: list[SleeveState] = []
    for k in SLEEVES:
        after = current[k] + split_shown[k] + reinvest[k] - sales.get(k, 0.0)
        status = statuses[k]
        if k != "core" and current[k] > 0 and not unlocked[k]:
            status += " Existing positions stay as they are; selling would be a taxable event."
        if k in sales:
            status += (
                f" It is more than {band:g} points above its target and a year of contributions "
                "would not bring it back, so part of it is sold."
            )
        sleeves.append(SleeveState(
            key=k,
            label=SLEEVE_LABELS[k],
            current_eur=_round_eur(current[k]),
            current_pct=round(100.0 * current[k] / book, 2) if book else 0.0,
            target_pct=round(targets[k], 2),
            max_pct=100.0 if k == "core" else (cfg["tilt_max_pct"] if k == "tilt" else cfg["satellite_max_pct"]),
            unlocked=unlocked[k],
            status=status,
            contribution_eur=_round_eur(split_shown[k]),
            after_eur=_round_eur(after),
            after_pct=round(100.0 * after / total_after, 2) if total_after else 0.0,
            positions=sorted(positions[k], key=lambda p: -p["value_eur"]),
            sale_eur=_round_eur(sales.get(k, 0.0)),
            reinvest_eur=_round_eur(reinvest[k]),
        ))

    satellite_broker = evidence.get("satellite_broker") or "dkb"

    def satellite_fee(amount: float) -> float:
        return order_fee_eur(satellite_broker, amount)

    def satellite_order(amount: float, source: str, kind: str = "order") -> PlanAction:
        return PlanAction(
            sleeve="satellite", kind=kind, amount_eur=_round_eur(amount),
            instrument="Stock pick", ticker=None, isin=None,
            note=(
                f"{source} At {SATELLITE_BROKER_LABELS.get(satellite_broker, satellite_broker)}, as tested; the "
                f"{_fee_eur(satellite_fee(amount))} fee is {100.0 * satellite_fee(amount) / amount:.1f} % of it."
            ),
            broker=satellite_broker, broker_label=SATELLITE_BROKER_LABELS.get(satellite_broker),
        )

    def order_note(source: str, amount: float, at: str, name: str | None = None) -> str:
        fee = order_fee_eur(at, amount, name)
        if fee == 0:
            return f"{source} at {BROKER_LABELS[at]}, free of charge for this ETF."
        return f"{source} at {BROKER_LABELS[at]}; the {_fee_eur(fee)} fee is {100.0 * fee / amount:.1f} % of it."

    def savings_plan_note(what: str, amount: float) -> str:
        fee = SAVINGS_PLAN_FEE_EUR[broker]
        cost = (
            "free of charge" if fee == 0
            else f"{_fee_eur(fee)} per execution ({100.0 * fee / amount:.2f} %), "
            "nothing if it is one of DKB's promotional ETFs"
        )
        return f"{what} at {broker_label}, {cost}."

    def buy(sleeve: str, kind: str, amount: float, instrument: str, ticker: str | None,
            isin: str | None, note: str, at: str) -> PlanAction:
        return PlanAction(
            sleeve=sleeve, kind=kind, amount_eur=_round_eur(amount), instrument=instrument,
            ticker=ticker, isin=isin, note=note, broker=at, broker_label=BROKER_LABELS[at],
        )

    core_instrument = CORE_ETF_ISINS.get(cfg["core_isin"], "Passive core ETF")

    def order_at(amount: float, name: str | None = None) -> str:
        return buy_broker(cfg, connected, "order", amount, name)

    def core_contribution_note(amount: float) -> str:
        if manual_mode:
            return order_note("This month's contribution: one order", amount, broker, core_instrument)
        running = plans_by_sleeve["core"]
        if core_covered:
            return f"Already covered: your savings plans at {plan_brokers} put {eur(running)} a month into the core."
        if running > 0:
            return savings_plan_note(
                f"Your core savings plans run {eur(running)} a month; raise them by {eur(amount - running)}",
                amount,
            )
        return savings_plan_note("Your savings plan", amount)

    actions: list[PlanAction] = []
    for k in SLEEVES:
        held_at = {h.get("broker") for h in positions[k]}
        for h, amount in _sale_split(positions[k], sales.get(k, 0.0)):
            depot = h.get("broker")
            where = f" at {BROKER_LABELS.get(depot, depot)}" if depot else ""
            same_broker = {
                p.get("account_id") for p in positions[k] if p.get("broker") == depot and p["isin"] == h["isin"]
            }
            if depot and h.get("account_id") and len(same_broker) > 1:
                where = f" from your {depot_label(db, depot, h['account_id'])}"
            per_depot = (
                " The position with the smallest gain per euro is sold first; FIFO runs per depot."
                if len(held_at) > 1 else ""
            )
            actions.append(PlanAction(
                sleeve=k, kind="sale", amount_eur=_round_eur(amount),
                instrument=h.get("name") or h["isin"], ticker=h.get("ticker"), isin=h["isin"] or None,
                note=(
                    f"Sells{where} back to the {targets[k]:g} % target. A sale realises gains: they are taxed "
                    "unless the Sparer-Pauschbetrag or an NV certificate covers them "
                    f"(Tax → Overview shows the tax-free room).{per_depot}"
                ),
                broker=depot, broker_label=BROKER_LABELS.get(depot) if depot else None,
                account_id=h.get("account_id"),
            ))
    if split["core"] > 0:
        actions.append(buy(
            "core", contribution_kind, split["core"], core_instrument, cfg["core_ticker"], cfg["core_isin"],
            core_contribution_note(split["core"]), broker,
        ))
    if reinvest["core"] > 0:
        at = order_at(reinvest["core"], core_instrument)
        actions.append(buy(
            "core", "order", reinvest["core"], core_instrument, cfg["core_ticker"], cfg["core_isin"],
            order_note("One order from the sale proceeds", reinvest["core"], at, core_instrument), at,
        ))
    if split["tilt"] > 0:
        for tilt_isin, amount in _split_tilt(positions["tilt"], cfg["tilt_isins"], split["tilt"]):
            at = order_at(amount) if manual_mode else broker
            actions.append(buy(
                "tilt", contribution_kind, amount, _tilt_instrument(positions["tilt"], tilt_isin), None, tilt_isin,
                order_note("One order", amount, at) if manual_mode
                else savings_plan_note("Run as a separate savings plan", amount),
                at,
            ))
    if reinvest["tilt"] > 0:
        for tilt_isin, amount in _split_tilt(positions["tilt"], cfg["tilt_isins"], reinvest["tilt"]):
            at = order_at(amount)
            actions.append(buy(
                "tilt", "order", amount, _tilt_instrument(positions["tilt"], tilt_isin), None, tilt_isin,
                order_note("One order from the sale proceeds", amount, at), at,
            ))
    if split["satellite"] > 0:
        actions.append(satellite_order(split["satellite"], "One order."))
    if reinvest["satellite"] > 0:
        actions.append(satellite_order(reinvest["satellite"], "One order from the sale proceeds."))

    # Cash above the emergency reserve: an optional one-off, split like the
    # contribution, only when one order's fee stays at or below 1 %.
    cash = cash_state(db, user_id, cfg)
    investable = cash["investable_eur"]
    one_off = {k: 0.0 for k in SLEEVES}
    notes: list[str] = []
    if investable > 0:
        at = order_at(investable, core_instrument)
        after_plan = {k: current[k] + split_shown[k] + reinvest[k] - sales.get(k, 0.0) for k in SLEEVES}
        planned = allocate_contribution(after_plan, targets, investable)
        if 0 < planned["satellite"] < cfg["min_order_eur"]:
            planned["core"] += planned["satellite"]
            planned["satellite"] = 0.0
        # Every order is checked against its own broker's fee: one that would
        # cost more than 1 % goes to the core order instead.
        orders: list[tuple[str, str | None, float, str]] = []  # sleeve, isin, amount, broker
        core_amount = planned["core"]
        if planned["tilt"] > 0:
            for tilt_isin, amount in _split_tilt(positions["tilt"], cfg["tilt_isins"], planned["tilt"]):
                if order_fee_eur(at, amount) <= 0.01 * amount:
                    orders.append(("tilt", tilt_isin, amount, at))
                else:
                    core_amount += amount
        if planned["satellite"] > 0:
            if satellite_fee(planned["satellite"]) <= 0.01 * planned["satellite"]:
                orders.append(("satellite", None, planned["satellite"], satellite_broker))
            else:
                core_amount += planned["satellite"]
        if core_amount > 0 and order_fee_eur(at, core_amount, core_instrument) <= 0.01 * core_amount:
            orders.insert(0, ("core", cfg["core_isin"], core_amount, at))
        if orders:
            source = f"Optional one-off from cash above your {eur(cash['emergency_reserve_eur'])} reserve: one order"
            need: dict[str, float] = {}
            for sleeve, isin, amount, broker_at in orders:
                one_off[sleeve] += amount
                need[broker_at] = need.get(broker_at, 0.0) + amount
                if sleeve == "satellite":
                    actions.append(satellite_order(amount, "Optional one-off from cash.", kind="one_off"))
                elif sleeve == "core":
                    actions.append(buy(
                        "core", "one_off", amount, core_instrument, cfg["core_ticker"], isin,
                        order_note(source, amount, broker_at, core_instrument), broker_at,
                    ))
                else:
                    actions.append(buy(
                        "tilt", "one_off", amount, _tilt_instrument(positions["tilt"], isin), None, isin,
                        order_note(source, amount, broker_at), broker_at,
                    ))
            for broker_at, amount in need.items():
                in_place = min(amount, cash["broker_cash_by_broker"].get(broker_at, 0.0))
                if amount - in_place >= 1.0:
                    account = "Scalable cash account" if broker_at == "scalable" else "DKB giro account"
                    notes.append(
                        f"For the one-off, move {eur(amount - in_place)} into your {account} first, from Tagesgeld "
                        f"or wherever the money sits; {eur(in_place)} is already free there."
                    )
        else:
            notes.append(
                f"{eur(investable)} sits above your emergency reserve: too little for one order at "
                f"{BROKER_LABELS[at]} with the fee at or below 1 %, so it stays in cash for now."
            )
    elif not cash["reserve_set"] and cash["savings_eur"] + cash["broker_cash_eur"] > 0:
        notes.append(
            f"You hold {eur(cash['savings_eur'] + cash['broker_cash_eur'])} in Tagesgeld and free broker cash. "
            "Set your emergency reserve under Plan settings to see how much of it could be invested."
        )

    if plans:
        if manual_mode:
            notes.append(
                f"Your savings plans at {plan_brokers} also buy {eur(plans_total)} a month on their own, "
                "on top of the orders above."
            )
        elif abs(plans_total - contribution) >= 1.0:
            notes.append(
                f"Your savings plans at {plan_brokers} total {eur(plans_total)} a month, but your monthly "
                f"contribution is set to {eur(contribution)}. The plan uses the setting; change it under "
                "Plan settings if the savings plans are what you invest."
            )
        if locked_plans and other_budget > 0:
            names = ", ".join(p["name"] for p in plans if p["sleeve"] in locked_plans)
            picks = sum(locked_plans.values())
            notes.append(
                f"{eur(picks)} of your {eur(contribution)} a month ({100.0 * picks / contribution:.0f} %) goes "
                f"into your own savings plans ({names}); the core gets the other {eur(split['core'])}."
            )
        else:
            for k, amount in locked_plans.items():
                names = ", ".join(p["name"] for p in plans if p["sleeve"] == k)
                notes.append(
                    f"{names}: {eur(amount)} a month goes into {SLEEVE_LABELS[k].lower()}, which no strategy has "
                    "unlocked yet (target 0 %). The plan would put that money into the core."
                )
        for p in plans:
            if p["monthly_eur"] is None:
                notes.append(
                    f"The {p['name']} savings plan runs {p['frequency'].lower()}, a schedule the plan does not "
                    "know, so it is left out of the totals."
                )
    for p in stopped_plans:
        why = (
            "is paused" if p["not_running_reason"] == "paused"
            else f"was due on {p['next_execution_date']} and has not run"
        )
        notes.append(f"The {p['name']} savings plan {why}, so it is not counted.")

    contribution_actions = [(a.sleeve, a.kind) for a in actions if a.kind != "one_off"]
    # Plans of your own that take the whole contribution leave nothing for the core to buy.
    plans_take_all = not contribution_actions and other_budget > 0 and core_covered
    only_core = contribution_actions == [("core", contribution_kind)] or plans_take_all
    locked_blocks = bool(locked_plans) and other_budget == 0
    no_change = only_core and not locked_blocks and (manual_mode or not plans or core_covered)
    picks_text = (
        f" and {eur(sum(locked_plans.values()))} into your own picks" if other_budget > 0 else ""
    )
    sold = sum(sales.values())
    if only_core and manual_mode:
        headline = f"Buy {eur(contribution)} of {cfg['core_ticker']} at {broker_label}." + (
            f" {eur(sum(locked_plans.values()))} a month of your savings plans goes into a sleeve that is "
            "still locked." if locked_plans else " Nothing else to do this month."
        )
    elif only_core and plans and core_covered and not locked_blocks:
        headline = (
            f"Let your savings plans run: {eur(plans_by_sleeve['core'])} a month into the core{picks_text}. "
            "Nothing else to do this month."
        )
    elif only_core and plans and core_covered:
        headline = (
            f"Your savings plans cover the core, and {eur(sum(locked_plans.values()))} a month of them goes "
            "into a sleeve that is still locked."
        )
    elif only_core and plans:
        headline = (
            f"Raise your core savings plans by {eur(split['core'] - plans_by_sleeve['core'])} to "
            f"{eur(split['core'])} a month (now {eur(plans_by_sleeve['core'])})."
        )
    elif only_core:
        headline = (
            f"Let your {eur(contribution)} savings plan run into {cfg['core_ticker']}. "
            "Nothing else to do this month."
        )
    elif not contribution_actions:
        headline = "No contribution is configured, so there is nothing to invest this month."
    elif sold > 0:
        headline = (
            f"Invest {eur(contribution)} and rebalance: sell {eur(sold)} and reinvest it, "
            "as listed below."
        )
    else:
        headline = f"Invest {eur(contribution)} as listed below."
    one_off_total = sum(one_off.values())
    if one_off_total > 0:
        headline += f" Optionally, invest {eur(one_off_total)} of cash above your reserve."

    return {
        "month": now.strftime("%Y-%m"),
        "generated_at": now.isoformat(),
        "headline": headline,
        "no_change": no_change,
        "contribution_eur": _round_eur(contribution),
        "book_eur": _round_eur(book),
        "holdings_synced_at": synced.isoformat() if synced else None,
        "has_holdings": bool(holdings),
        "tracking_error_budget": {
            "label": tracking_error_label(cfg["tilt_max_pct"]),
            "tilt_max_pct": cfg["tilt_max_pct"],
            "satellite_max_pct": cfg["satellite_max_pct"],
        },
        "min_order_eur": cfg["min_order_eur"],
        "drift_band_pp": band,
        "contribution_mode": cfg["contribution_mode"],
        "broker": broker,
        "broker_label": broker_label,
        "broker_choice": cfg["broker"],
        "brokers_connected": connected,
        "sleeves": [asdict(s) for s in sleeves],
        "evidence": {
            "tilt": {
                "passed": bool(evidence.get("tilt_evidence_passed", unlocked["tilt"])),
                "unlocked": unlocked["tilt"],
                "reason": evidence["tilt_reason"],
            },
            "satellite": {
                "unlocked": unlocked["satellite"],
                "reason": evidence["satellite_reason"],
                "gate": evidence.get("satellite_gate"),
            },
        },
        "actions": [asdict(a) for a in actions],
        "savings_plans": {
            "items": all_plans,
            "stopped": [p for p in all_plans if not p["running"]],
            **plans_freshness(db, user_id),
            "other_budget_eur": _round_eur(other_budget),
            "other_budget_pct": round(100.0 * other_budget / contribution, 1) if contribution > 0 else 0.0,
            "core_needed_eur": _round_eur(split["core"]),
            "monthly_eur": _round_eur(plans_total),
            "by_sleeve": {k: _round_eur(v) for k, v in plans_by_sleeve.items()},
        },
        "cash": cash,
        "core_look_through": core_look_through(positions["core"]),
        "notes": notes,
        "never_sells": not sales,
        "not_investment_advice": True,
        "suggested_funds": suggest_all(cfg["tilt_isins"]),
        "acc_dist_note": ACC_DIST_NOTE,
    }
