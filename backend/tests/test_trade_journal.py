"""Trade journal analysis (research only): payoff ratio and break-even hit rate from the owner's closed trades."""
from datetime import datetime

from app.lab.trade_journal import JournalTrade, analyse


def _t(res, hour=16, d="short", op=100.0, cl=99.0, mins=3):
    o = datetime(2026, 11, 25, hour, 0)
    return JournalTrade("USA Tech 100", d, o, datetime(2026, 11, 25, hour, mins), res, op, cl)


def test_payoff_ratio_and_break_even_hit_rate_from_the_screenshot_style_results():
    # invented numbers: wins about 1.75 EUR, losses about 4.20 EUR
    trades = [_t(1.10), _t(2.40), _t(-1.90), _t(-4.50), _t(-6.20)]
    rep = analyse(trades)
    o = rep["overall"]
    assert o["n"] == 5 and o["avg_win_eur"] == 1.75 and o["avg_loss_eur"] == 4.2
    assert o["payoff_loss_per_win"] == 2.4 and o["break_even_hit_rate"] == 0.706
    assert o["hit_rate"] == 0.4 and "break even" in rep["reading"]
    assert rep["by_hold_time"]["0-5 min"]["n"] == 5 and rep["longest_losing_streak"] == 3


def test_empty_and_single_side():
    assert analyse([]) == {"overall": {"n": 0}}
    assert analyse([_t(1.0)])["overall"]["payoff_loss_per_win"] is None


def _write(tmp_path, text):
    p = tmp_path / "export.csv"
    p.write_text(text)
    return p


def test_loader_reads_both_export_header_shapes_and_the_per_unit_spread(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import cfd_journal_report as rep

    real = ("Record Type,Date (UTC),Instrument,Direction,Units,Position ID,Order ID,Order type,Intent,Status,"
            "Date opened (UTC),Date closed (UTC),Average price (instrument currency),Close price (instrument currency),"
            "Exchange rate,Spread (account currency),Total result (account currency)\n"
            "Order,2026-11-25 16:11:24+00:00,USA Tech 100,Buy,0.09,POS1,O2,STOP LOSS,CLOSE,EXECUTED,,,,,,,\n"
            "Closed position,2026-11-25 16:11:24+00:00,USA Tech 100,Sell,0.09,POS1,O2,,,,"
            "2026-11-25 16:09:10+00:00,2026-11-25 16:11:24+00:00,20000.00,19980.00,0.91,1.5,1.82\n")
    trades, _ = rep.load(_write(tmp_path, real))
    t = trades[0]
    assert len(trades) == 1 and t.direction == "short" and t.exit_type == "STOP LOSS" and t.result_eur == 1.82
    assert round(t.spread_eur, 3) == 0.135 and 0.8 < t.spread_bp < 0.9  # 1.5 per unit x 0.09 units; about 0.82 bp
    help_centre = ("RecordType,Date (UTC),Instrument,Direction,Units,Position ID,Date opened (UTC),Date closed (UTC),"
                   "Average price,Close price,Exchange rate,Spread,Result,Total result\n"
                   "CLOSED_POSITION,2026-11-25 16:11:24,USA Tech 100,Sell,0.09,POS1,2026-11-25 16:09:10,"
                   "2026-11-25 16:11:24,20000.00,19980.00,0.91,1.5,1.83,1.82\n")
    assert len(rep.load(_write(tmp_path, help_centre))[0]) == 1


def test_loader_fails_loudly_instead_of_returning_an_empty_journal(tmp_path):
    import sys

    import pytest
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import cfd_journal_report as rep

    with pytest.raises(SystemExit):
        rep.load(_write(tmp_path, "RecordType,Instrument\nTRANSACTION,Cash\n"))
    with pytest.raises(SystemExit):
        rep.load(_write(tmp_path, "foo,bar\n1,2\n"))
