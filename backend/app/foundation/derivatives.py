"""Warrants held at a broker: terms, value, implied volatility, Greeks, exposure (ADR 0026).

Scalable sells bank-issued warrants (Optionsscheine) and knock-outs; it does
not let a retail account write options. The holdings feed types them
DERIVATIVE and names them ("Anthropic Put 100,00 VON") but carries no expiry,
ratio or strike currency, so the owner enters those once
(:class:`~app.foundation.models.entities.DerivativeTerms`). From them and the
underlying's close this module works out what the price implies:

* the implied volatility (the volatility a model price needs to match the
  broker's mid), from Black-Scholes for a European warrant and a
  Leisen-Reimer tree for an American one;
* delta, gamma, theta and vega by finite differences on the same pricer;
* the stock-equivalent exposure in EUR (``delta x ratio x quantity x price``;
  negative for a put), the time decay per day, the break-even at expiry and
  the value at expiry for a few moves of the underlying.

Everything is an estimate: the risk-free rate is the EUR one for every
currency (it moves a one-month price by well under 1 %), the dividend yield is
taken as zero, and a warrant's price carries the issuer's margin, which is
what :mod:`app.lab.options_lab` measures. Nothing here trades; the brokers
stay read-only.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Broker security types that are options on something else.
DERIVATIVE_TYPES = frozenset({"DERIVATIVE", "WARRANT"})

#: Issuer short codes as Scalable writes them at the end of a warrant's name.
ISSUERS = {
    "VON": "Vontobel", "SG": "Société Générale", "HSBC": "HSBC", "UBS": "UBS", "BNP": "BNP Paribas",
    "MS": "Morgan Stanley", "GS": "Goldman Sachs", "DZ": "DZ Bank", "JPM": "J.P. Morgan", "CITI": "Citigroup",
    "HVB": "UniCredit (HVB)", "UC": "UniCredit", "DB": "Deutsche Bank", "COBA": "Commerzbank", "ING": "ING",
}

_NAME = re.compile(
    r"^(?P<underlying>.+?)\s+(?P<kind>put|call)\s+(?P<strike>\d[\d.]*(?:,\d+)?)(?:\s+(?P<issuer>[A-Za-z]{2,5}))?\s*$",
    re.I,
)

#: An expiry this close is flagged in the Control Center.
EXPIRY_WARNING_DAYS = 7
_TREE_STEPS = 200
#: Moves of the underlying the value-at-expiry table shows.
SCENARIO_MOVES = (-0.5, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.5)


def _german_number(text: str) -> float | None:
    """``"1.234,50"`` -> 1234.5, ``"100,00"`` -> 100.0, ``"95"`` -> 95.0."""
    cleaned = text.replace(".", "").replace(",", ".") if "," in text else text
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_name(name: str | None) -> dict[str, Any] | None:
    """Underlying name, put/call, strike and issuer from a broker's warrant name, or None.

    ``"Anthropic Put 100,00 VON"`` -> ``{"underlying_name": "Anthropic", "kind": "put",
    "strike": 100.0, "issuer": "Vontobel"}``. The strike's currency is not in the name.
    """
    m = _NAME.match((name or "").strip())
    if m is None:
        return None
    strike = _german_number(m.group("strike"))
    if strike is None or strike <= 0:
        return None
    code = (m.group("issuer") or "").upper()
    return {
        "underlying_name": m.group("underlying").strip(),
        "kind": m.group("kind").lower(),
        "strike": strike,
        "issuer": ISSUERS.get(code, code or None),
    }


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _intrinsic(kind: str, spot: float, strike: float) -> float:
    return max(strike - spot, 0.0) if kind == "put" else max(spot - strike, 0.0)


def black_scholes(kind: str, spot: float, strike: float, years: float, rate: float, vol: float) -> float:
    """European option price per share (no dividends)."""
    if years <= 0 or vol <= 0:
        return _intrinsic(kind, spot, strike)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-rate * years)
    if kind == "put":
        return disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return spot * _norm_cdf(d1) - disc * _norm_cdf(d2)


def _lr_inversion(z: float, n: int) -> float:
    """Peizer-Pratt inversion (method 2) used by the Leisen-Reimer tree."""
    inner = 0.25 - 0.25 * math.exp(-((z / (n + 1.0 / 3.0 + 0.1 / (n + 1.0))) ** 2) * (n + 1.0 / 6.0))
    return 0.5 + math.copysign(math.sqrt(max(inner, 0.0)), z)


def binomial_american(
    kind: str, spot: float, strike: float, years: float, rate: float, vol: float, steps: int = _TREE_STEPS,
) -> float:
    """American option price per share on a Leisen-Reimer tree (no dividends).

    Leisen & Reimer (1996) centre the tree on the strike, so the price converges smoothly in the spot.
    A Cox-Ross-Rubinstein tree converges in a sawtooth, and a second difference of it (gamma) was off
    by up to 60 % at 200 steps. *steps* is made odd, as the method needs.
    """
    if years <= 0 or vol <= 0:
        return _intrinsic(kind, spot, strike)
    n = steps if steps % 2 == 1 else steps + 1
    dt = years / n
    grow = math.exp(rate * dt)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    p = _lr_inversion(d2, n)
    p_star = _lr_inversion(d1, n)
    if not (0.0 < p < 1.0 and 0.0 < p_star < 1.0):
        # The inversion saturates for a short-dated or low-vol option far from the strike.
        return max(black_scholes(kind, spot, strike, years, rate, vol), _intrinsic(kind, spot, strike))
    up = grow * p_star / p
    down = (grow - p * up) / (1.0 - p)
    if not 0.0 < down < up:
        return max(black_scholes(kind, spot, strike, years, rate, vol), _intrinsic(kind, spot, strike))
    disc = 1.0 / grow
    values = [_intrinsic(kind, spot * up ** (n - i) * down ** i, strike) for i in range(n + 1)]
    for step in range(n - 1, -1, -1):
        for i in range(step + 1):
            held = disc * (p * values[i] + (1.0 - p) * values[i + 1])
            values[i] = max(held, _intrinsic(kind, spot * up ** (step - i) * down ** i, strike))
    return values[0]


def option_price(
    kind: str, style: str, spot: float, strike: float, years: float, rate: float, vol: float,
) -> float:
    if style == "american":
        return binomial_american(kind, spot, strike, years, rate, vol)
    return black_scholes(kind, spot, strike, years, rate, vol)


def implied_vol(
    price: float, kind: str, style: str, spot: float, strike: float, years: float, rate: float,
) -> float | None:
    """The volatility at which the model price equals *price* (per share), or None.

    None when the price is below what exercise is worth now (or the European
    lower bound), or above any price a volatility up to 1000 % gives.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    lo, hi = 0.005, 10.0
    f_lo = option_price(kind, style, spot, strike, years, rate, lo) - price
    f_hi = option_price(kind, style, spot, strike, years, rate, hi) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if option_price(kind, style, spot, strike, years, rate, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)


def greeks(
    kind: str, style: str, spot: float, strike: float, years: float, rate: float, vol: float,
) -> dict[str, float]:
    """Delta, gamma (per unit of the underlying), theta (per calendar day) and vega (per vol point)."""
    def price(s: float = spot, t: float = years, v: float = vol) -> float:
        return option_price(kind, style, s, strike, t, rate, v)

    h = spot * 0.01
    base, up, down = price(), price(s=spot + h), price(s=spot - h)
    day = 1.0 / 365.0
    v_low = max(vol - 0.01, 1e-4)  # vega is divided by the actual width: near the floor it is one-sided
    theta = (price(t=years - day) - base) if years > day else (_intrinsic(kind, spot, strike) - base)
    return {
        "delta": (up - down) / (2 * h),
        "gamma": (up - 2 * base + down) / (h * h),
        "theta_per_day": theta,
        "vega_per_point": (price(v=vol + 0.01) - price(v=v_low)) / (vol + 0.01 - v_low) * 0.01,
    }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def _latest_close(db: Session, symbol: str) -> tuple[float | None, date | None]:
    from sqlalchemy import text

    try:
        row = db.execute(
            text("SELECT close, ts FROM bar_prices WHERE symbol = :s ORDER BY ts DESC LIMIT 1"), {"s": symbol},
        ).first()
    except Exception:  # noqa: BLE001 - no bar store (a bare test database)
        db.rollback()
        return None, None
    if row is None or row[0] is None:
        return None, None
    ts = row[1]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return float(row[0]), ts.date() if isinstance(ts, datetime) else ts


def _eur_per_unit(db: Session, currency: str, on: date) -> float | None:
    from app.foundation.ecb_fx import eur_per_unit

    try:
        rate, _source = eur_per_unit(db, currency, on)
    except Exception:  # noqa: BLE001 - FX is optional here; the view says what is missing
        logger.debug("derivatives: no %s/EUR rate", currency, exc_info=True)
        return None
    return float(rate) if rate else None


def _rate(db: Session) -> float:
    from app.foundation.settings import get_risk_free_rate

    try:
        return float(get_risk_free_rate(db))
    except Exception:  # noqa: BLE001
        return 0.02


def terms_dict(terms: Any) -> dict[str, Any]:
    return {
        "kind": terms.kind,
        "style": terms.style,
        "strike": float(terms.strike),
        "strike_currency": terms.strike_currency,
        "ratio": float(terms.ratio),
        "expiry": terms.expiry.isoformat(),
        "underlying_symbol": terms.underlying_symbol,
        "issuer": terms.issuer,
        "notes": terms.notes,
    }


def analyse(
    *,
    kind: str,
    style: str,
    strike: float,
    ratio: float,
    expiry: date,
    quantity: float,
    price_eur: float | None,
    spot: float | None,
    eur_per_unit: float | None,
    rate: float,
    today: date,
) -> dict[str, Any]:
    """What one warrant position's price implies. Prices are per warrant in EUR unless named per share."""
    days = (expiry - today).days
    out: dict[str, Any] = {"days_to_expiry": days, "expired": days < 0, "missing": []}
    if spot is None:
        out["missing"].append("the underlying's price")
    if eur_per_unit is None:
        out["missing"].append("an exchange rate for the strike currency")
    if price_eur is None or price_eur <= 0:
        out["missing"].append("the warrant's price")
    if out["missing"] or spot is None or eur_per_unit is None or price_eur is None:
        return out
    fx = eur_per_unit
    shares = ratio * quantity
    out["spot"] = spot
    out["intrinsic_eur"] = _intrinsic(kind, spot, strike) * ratio * fx
    out["time_value_eur"] = price_eur - out["intrinsic_eur"]
    per_share = price_eur / fx / ratio
    out["break_even"] = strike - per_share if kind == "put" else strike + per_share
    out["break_even_move"] = out["break_even"] / spot - 1.0
    out["scenarios"] = [
        {
            "move": move,
            "underlying": spot * (1.0 + move),
            "value_eur": _intrinsic(kind, spot * (1.0 + move), strike) * shares * fx,
        }
        for move in SCENARIO_MOVES
    ]
    years = days / 365.0
    if years <= 0:
        return out
    iv = implied_vol(per_share, kind, style, spot, strike, years, rate)
    out["implied_vol"] = iv
    if iv is None:
        out["missing"].append("an implied volatility (the price is outside what a model gives)")
        return out
    g = greeks(kind, style, spot, strike, years, rate, iv)
    out["delta"] = g["delta"]
    out["gamma"] = g["gamma"]
    out["vega_eur_per_point"] = g["vega_per_point"] * shares * fx
    out["theta_eur_per_day"] = g["theta_per_day"] * shares * fx
    # The position moves like this many EUR of the underlying (negative: a put gains when it falls).
    out["exposure_eur"] = g["delta"] * shares * spot * fx
    # Probability of finishing in the money under the same model (risk-neutral, an estimate).
    sq = iv * math.sqrt(years)
    d2 = (math.log(spot / strike) + (rate - 0.5 * iv * iv) * years) / sq
    out["prob_in_the_money"] = _norm_cdf(-d2) if kind == "put" else _norm_cdf(d2)
    return out


def derivative_positions(db: Session, user_id: str, *, today: date | None = None) -> list[dict[str, Any]]:
    """Every warrant the user's brokers report, with its terms and what its price implies."""
    from app.foundation.live_positions import combined_positions
    from app.foundation.models.entities import DerivativeQuote, DerivativeTerms

    clock = today or datetime.now(UTC).date()
    positions = [
        p for p in combined_positions(db, user_id)
        if (p.security_type or "").strip().upper() in DERIVATIVE_TYPES and p.quantity > 0
    ]
    if not positions:
        return []
    terms_by_isin = {
        t.isin: t for t in db.query(DerivativeTerms).filter(DerivativeTerms.user_id == user_id).all()
    }
    rate = _rate(db)
    out: list[dict[str, Any]] = []
    for pos in positions:
        quantity = float(pos.quantity)
        price = float(pos.current_price) if pos.current_price is not None else None
        cost = float(pos.avg_buy_price) * quantity if pos.avg_buy_price is not None else None
        value = float(pos.value)
        row: dict[str, Any] = {
            "isin": pos.isin,
            "name": pos.name,
            "brokers": list(pos.sources),
            "quantity": quantity,
            "price_eur": price,
            "value_eur": value,
            "cost_eur": cost,
            "pnl_eur": value - cost if cost is not None else None,
            "parsed": parse_name(pos.name),
            "terms": None,
            "analysis": None,
            "listed": None,
            "estimate": True,
        }
        terms = terms_by_isin.get(pos.isin)
        if terms is not None:
            row["terms"] = terms_dict(terms)
            spot, spot_day = _latest_close(db, terms.underlying_symbol)
            fx = 1.0 if terms.strike_currency.upper() == "EUR" else _eur_per_unit(db, terms.strike_currency, clock)
            analysis = analyse(
                kind=terms.kind, style=terms.style, strike=float(terms.strike), ratio=float(terms.ratio),
                expiry=terms.expiry, quantity=quantity, price_eur=price, spot=spot, eur_per_unit=fx,
                rate=rate, today=clock,
            )
            analysis["spot_day"] = spot_day.isoformat() if spot_day else None
            # The implied volatility moves ~3 vol points for a 1 % spot error (ADR 0026 review, 2026-11-25):
            # say how old the underlying's close is, and that the warrant's price is the broker's, not a mid.
            analysis["spot_stale_days"] = (clock - spot_day).days if spot_day else None
            analysis["price_basis"] = "broker price (not a bid/ask mid)"
            row["analysis"] = analysis
            quote = (
                db.query(DerivativeQuote)
                .filter(DerivativeQuote.isin == pos.isin, DerivativeQuote.listed_iv.isnot(None))
                .order_by(DerivativeQuote.day.desc())
                .first()
            )
            if quote is not None:
                row["listed"] = {
                    "day": quote.day.isoformat(),
                    "implied_vol": quote.listed_iv,
                    "strike": quote.listed_strike,
                    "expiry": quote.listed_expiry.isoformat() if quote.listed_expiry else None,
                    "warrant_implied_vol": quote.warrant_iv,
                }
        out.append(row)
    return out


def upsert_terms(db: Session, user_id: str, isin: str, payload: dict[str, Any]) -> Any:
    """Create or replace the terms of *isin* for *user_id*; the caller commits."""
    from app.foundation.models.entities import DerivativeTerms

    row = (
        db.query(DerivativeTerms)
        .filter(DerivativeTerms.user_id == user_id, DerivativeTerms.isin == isin)
        .one_or_none()
    )
    if row is None:
        row = DerivativeTerms(user_id=user_id, isin=isin)
        db.add(row)
    for key in ("kind", "style", "strike", "strike_currency", "ratio", "expiry", "underlying_symbol", "issuer", "notes"):
        if key in payload:
            setattr(row, key, payload[key])
    row.strike_currency = (row.strike_currency or "USD").upper()
    row.underlying_symbol = row.underlying_symbol.strip().upper()
    return row


# ---------------------------------------------------------------------------
# Control Center
# ---------------------------------------------------------------------------


def attention(db: Session, *, today: date | None = None) -> list[dict[str, str]]:
    """``[{id, severity, title, detail}]`` for warrants without terms, near expiry, or past it."""
    from app.foundation.models.entities import User

    clock = today or datetime.now(UTC).date()
    items: list[dict[str, str]] = []
    for (user_id,) in db.query(User.id).all():
        try:
            rows = derivative_positions(db, user_id, today=clock)
        except Exception:  # noqa: BLE001 - one user's data never hides the rest of the list
            logger.debug("derivatives: attention failed for %s", user_id, exc_info=True)
            continue
        for row in rows:
            if row["terms"] is None:
                items.append({
                    "id": f"derivative-terms-{row['isin']}", "severity": "warning",
                    "title": f"Enter the terms of {row['name']}",
                    "detail": "Its expiry, ratio and strike currency are not in the broker's feed. Without them "
                              "its time decay, break-even and expiry warning are not shown.",
                })
                continue
            days = (date.fromisoformat(row["terms"]["expiry"]) - clock).days
            if days < 0:
                items.append({
                    "id": f"derivative-expired-{row['isin']}", "severity": "info",
                    "title": f"{row['name']} expired {-days} day{'s' if days != -1 else ''} ago",
                    "detail": "The broker still lists it. The issuer pays out what it was worth at expiry, "
                              "usually within a few days; a worthless one is booked out.",
                })
            elif days <= EXPIRY_WARNING_DAYS:
                value = row["value_eur"]
                items.append({
                    "id": f"derivative-expiry-{row['isin']}", "severity": "warning",
                    "title": f"{row['name']} expires in {days} day{'s' if days != 1 else ''}",
                    "detail": f"Worth about {value:.2f} EUR now; what is left of its time value goes to zero by "
                              f"{row['terms']['expiry']}. Sell it or let it run, but decide before then.",
                })
    return items


# ---------------------------------------------------------------------------
# Daily snapshot (trade_check_daily slot, 22:30 UTC)
# ---------------------------------------------------------------------------


def listed_implied_vol(symbol: str, kind: str, strike: float, expiry: date) -> dict[str, Any] | None:
    """Yahoo's implied volatility of the listed option nearest in expiry, then strike, or None."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        expiries = [date.fromisoformat(e) for e in (ticker.options or ())]
    except Exception:  # noqa: BLE001 - no listed options (yet), or Yahoo is down
        logger.debug("derivatives: no option expiries for %s", symbol, exc_info=True)
        return None
    if not expiries:
        return None
    nearest = min(expiries, key=lambda e: abs((e - expiry).days))
    try:
        chain = ticker.option_chain(nearest.isoformat())
        frame = chain.puts if kind == "put" else chain.calls
    except Exception:  # noqa: BLE001
        logger.debug("derivatives: option chain failed for %s %s", symbol, nearest, exc_info=True)
        return None
    if frame is None or frame.empty or "strike" not in frame or "impliedVolatility" not in frame:
        return None
    frame = frame[frame["impliedVolatility"] > 0.01]
    if frame.empty:
        return None
    row = frame.iloc[(frame["strike"] - strike).abs().argsort().iloc[0]]
    return {"implied_vol": float(row["impliedVolatility"]), "strike": float(row["strike"]), "expiry": nearest}


def snapshot_quotes(
    db: Session,
    *,
    today: date | None = None,
    ingest: Any = None,
    listed: Any = listed_implied_vol,
) -> dict[str, Any]:
    """Fetch each warrant underlying's bars and keep one quote row per warrant for today.

    ``ingest(symbol, start)`` and ``listed(symbol, kind, strike, expiry)`` are
    injectable for tests. Returns ``{warrants, quotes, listed}``.
    """
    from app.foundation.models.entities import DerivativeQuote, DerivativeTerms, User

    clock = today or datetime.now(UTC).date()
    if ingest is None:
        from app.foundation.data_backbone.ingest import DataIngester

        ingester = DataIngester(db)

        def _ingest(symbol: str, start: str) -> Any:
            return ingester.ingest_bar_prices(symbol, start_date=start)

        ingest = _ingest
    report = {"warrants": 0, "quotes": 0, "listed": 0}
    fetched: set[str] = set()
    for (user_id,) in db.query(User.id).all():
        terms_by_isin = {
            t.isin: t for t in db.query(DerivativeTerms).filter(DerivativeTerms.user_id == user_id).all()
        }
        if not terms_by_isin:
            continue
        for row in derivative_positions(db, user_id, today=clock):
            terms = terms_by_isin.get(row["isin"])
            if terms is None or row["price_eur"] is None or (terms.expiry - clock).days < 0:
                continue
            report["warrants"] += 1
            symbol = terms.underlying_symbol
            if symbol not in fetched:
                fetched.add(symbol)
                try:
                    ingest(symbol, (clock - timedelta(days=30)).isoformat())
                except Exception as exc:  # noqa: BLE001 - the quote is kept without a fresh close
                    logger.warning("derivatives: bars for %s failed: %s", symbol, exc)
                    db.rollback()
                # Re-read with the new close.
                row = next(
                    (r for r in derivative_positions(db, user_id, today=clock) if r["isin"] == row["isin"]), row,
                )
            analysis = row.get("analysis") or {}
            got = listed(symbol, terms.kind, float(terms.strike), terms.expiry) if listed else None
            quote = (
                db.query(DerivativeQuote)
                .filter(DerivativeQuote.isin == row["isin"], DerivativeQuote.day == clock)
                .one_or_none()
            )
            if quote is None:
                quote = DerivativeQuote(isin=row["isin"], day=clock)
                db.add(quote)
            quote.underlying_symbol = symbol
            quote.warrant_price_eur = row["price_eur"]
            quote.underlying_close = analysis.get("spot")
            quote.eur_per_unit = (
                1.0 if terms.strike_currency.upper() == "EUR" else _eur_per_unit(db, terms.strike_currency, clock)
            )
            quote.days_to_expiry = analysis.get("days_to_expiry")
            quote.warrant_iv = analysis.get("implied_vol")
            quote.listed_iv = got["implied_vol"] if got else None
            quote.listed_strike = got["strike"] if got else None
            quote.listed_expiry = got["expiry"] if got else None
            quote.source = "broker+yahoo" if got else "broker"
            report["quotes"] += 1
            report["listed"] += 1 if got else 0
    db.commit()
    return report
