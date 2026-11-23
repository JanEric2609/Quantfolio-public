"""The warrant search reaches the delta band with few sc reads (ADR 0027, day-1 fix 2026-11-23)."""
from datetime import timedelta

from desk_helpers import DAX, NOW, FakeSc, standard_handler

from app.decision.desk.limits import LIMITS
from app.decision.desk.products import MAX_CANDIDATES, WARRANT_ENOUGH, WARRANT_MAX_READS, _search_warrants
from app.foundation.scalable import market

EPOCH = (NOW.date() + timedelta(days=60) - (NOW.date().replace(year=1970, month=1, day=1))).days


def _universe(n=2500, sign=1, bad_first_page=0, with_total=True):
    deltas = [sign * (0.98 - 0.97 * i / (n - 1)) for i in range(n)]
    rows = [{"strategy": "call" if sign > 0 else "put", "issuer": "X", "omega": 8, "implied_volatility": 0.2,
             "underlying_isin": DAX, "isin": f"DE000WB{i:05d}", "delta": d, "expiry_date": {"epoch_day": EPOCH}}
            for i, d in enumerate(deltas)]
    for i in range(bad_first_page):  # items the parser drops
        rows[i * 13 % 100].pop("isin")
    calls = []

    def handler(args):
        if args[:3] == ["broker", "derivatives", "search"]:
            calls.append(args)
            off = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
            lim = int(args[args.index("--limit") + 1])
            page = rows[off:off + lim]
            out = {"count": len(page), "items": page}
            if with_total:
                out["total_available"] = len(rows)
            return {"result": out}
        return standard_handler({}, {})(args)
    return handler, calls


def _search(handler, strategy):
    return _search_warrants(FakeSc(handler).cli(), DAX, strategy, NOW.date())


def _in_band(rows):
    return all(LIMITS.warrant_min_abs_delta <= abs(r.delta) <= LIMITS.warrant_max_abs_delta for r in rows)


def test_call_universe_bisects_into_the_band():
    handler, calls = _universe()
    rows = _search(handler, "call")
    assert len(rows) >= WARRANT_ENOUGH and _in_band(rows) and len(calls) < WARRANT_MAX_READS


def test_put_universe_uses_negative_deltas():
    handler, calls = _universe(sign=-1)
    rows = _search(handler, "put")
    assert len(rows) >= WARRANT_ENOUGH and _in_band(rows) and len(calls) < WARRANT_MAX_READS
    assert calls[0][calls[0].index("--sort-order") + 1] == "asc"


def test_short_parse_does_not_stop_the_search():
    """11-23: a page of 100 raw items with 7 unparseable ones is not a last page."""
    handler, calls = _universe(bad_first_page=7)
    rows = _search(handler, "call")
    assert rows and _in_band(rows)


def test_without_total_it_pages_sequentially_and_never_exceeds_the_cap():
    handler, calls = _universe(with_total=False)
    _search(handler, "call")
    assert len(calls) <= WARRANT_MAX_READS


def test_band_on_page_one_reads_one_page():
    deltas = [0.6] * 100
    epoch = EPOCH
    rows = [{"strategy": "call", "issuer": "X", "omega": 8, "underlying_isin": DAX, "isin": f"DE000WC{i:05d}",
             "delta": d, "expiry_date": {"epoch_day": epoch}} for i, d in enumerate(deltas)]
    calls = []

    def handler(args):
        calls.append(args)
        return {"result": {"total_available": 100, "count": 100, "items": rows}}
    assert len(_search(handler, "call")) >= 2 * MAX_CANDIDATES and len(calls) == 1


def test_universe_below_the_band_stops_after_page_one():
    handler, calls = _universe(n=100)
    # shrink: all deltas below the band
    def low(args):
        out = handler(args)
        for it in out["result"]["items"]:
            it["delta"] = 0.05
        return out
    assert _search(low, "call") == [] and len(calls) == 1


def test_page_function_reports_raw_count_and_total():
    handler, _ = _universe(bad_first_page=3)
    page = market.search_derivatives_page(FakeSc(handler).cli(), underlying=DAX, product_type="warrant",
                                          strategy="call", limit=100)
    assert page.raw_count == 100 and len(page.rows) < 100 and page.total_available == 2500
