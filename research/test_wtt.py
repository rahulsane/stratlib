"""Weekend Trend Trader mechanics on a synthetic weekly path. Run: .venv\\Scripts\\python -m pytest research\\test_wtt.py -q"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import Rules, simulate  # noqa: E402
from panel import Panel  # noqa: E402
from strategies.weekend_trend import WeekendTrend, week_ends  # noqa: E402

WEEKS = 34
# Weekly closes (every session of a week trades at the week's value). The stock is flat at 50 for 21 weeks,
# closes at 70 in week 21 (a 20-week high, +40% on 20 weeks ago), runs to 100 by week 24, closes at 95 in
# week 25 (the index's dip week), 85 in weeks 26-29 (the index has recovered) and 55 from week 30.
STOCK = [50.0] * 21 + [70, 80, 90, 100, 95, 85, 85, 85, 85, 55, 55, 55, 55]
INDEX = [100.0 + w for w in range(WEEKS)]
INDEX[25] = 100  # below its 10-week average (118); week 26 (126) is back above it (119)


def make_panel():
    start = date(2015, 6, 1)  # a Monday
    days = [start + timedelta(days=7 * w + d) for w in range(WEEKS) for d in range(5)]
    n = len(days)
    week = np.repeat(np.arange(WEEKS), 5)
    close = np.column_stack([np.array(INDEX)[week], np.array(STOCK)[week]])
    low = close * 0.99
    low[5 * 23 + 2, 1] = 40.0  # an intraday dip far below the stop, recovered by the weekly close
    return Panel(dates=np.array([d.isoformat() for d in days]), symbols=np.array(["SPY", "S0"]),
                 kind=np.array(["etf", "stock"]), until=np.array(["", ""]), open=close.copy(), high=close * 1.01,
                 low=low, close=close, volume=np.full((n, 2), 1e6), factor=np.ones((n, 2)))


def run(params):
    panel = make_panel()
    strategy = WeekendTrend()
    strategy.setup(panel, params)
    result = simulate(panel, strategy, str(panel.dates[5 * 12]), None, Rules(max_positions=20, max_position_pct=5.0))
    return panel, strategy, result


def day(panel, week, weekday):
    return str(panel.dates[5 * week + weekday])


def test_week_ends_are_last_sessions_and_a_partial_final_week_is_left_out():
    dates = np.array(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"])
    assert week_ends(dates).tolist() == [False, True, False, False, False]
    assert week_ends(np.array(["2026-09-24", "2026-10-02"])).tolist() == [True, True]


def test_entry_size_and_initial_stop():
    panel, _, r = run({"stop_rule": "ratchet"})
    (tr,) = r["trades"]
    assert tr.signal_date == day(panel, 21, 4)
    assert tr.entry_date == day(panel, 22, 0)
    fill = 80 * 1.001
    assert tr.entry_price == pytest.approx(fill)
    assert tr.shares == pytest.approx(5_000 / fill)
    assert tr.stop == pytest.approx(42.0)  # 40% below the signal week's close of 70


def test_ratchet_keeps_the_tightened_stop_after_the_index_recovers():
    panel, strategy, r = run({"stop_rule": "ratchet"})
    (tr,) = r["trades"]
    # Stop 60 at the high of 100; 90 in the dip week; still 90 when the index recovers, so 85 sells.
    assert tr.exit_date == day(panel, 27, 0)
    assert tr.exit_price == pytest.approx(85 * 0.999)
    assert tr.exit_reason == "weekly close under the 10% stop"
    assert [e for _, _, e in strategy.events] == ["tightened"]


def test_recomputed_stop_loosens_back_to_forty_percent():
    panel, strategy, r = run({"stop_rule": "recompute"})
    (tr,) = r["trades"]
    assert tr.exit_date == day(panel, 31, 0)  # 85 holds above 60; 55 in week 30 does not
    assert tr.exit_price == pytest.approx(55 * 0.999)
    assert tr.exit_reason == "weekly close under the 40% stop"
    assert [e for _, _, e in strategy.events] == ["tightened", "loosened"]


def test_index_universe_needs_membership_at_the_signal_week_only():
    panel = make_panel()
    n = len(panel.dates)
    member = np.zeros((n, 2), dtype=bool)
    WeekendTrend.members = {"sp500": np.zeros((n, 2), dtype=bool), "sp400": member}
    try:
        _, _, r = run({"universe": "sp900"})
        assert r["trades"] == []  # never a member: no entry
        member[5 * 21 + 4, 1] = True  # a member at the signal close only, so the position is kept after it leaves
        _, _, r = run({"universe": "sp900"})
        assert len(r["trades"]) == 1 and r["trades"][0].exit_reason == "weekly close under the 10% stop"
        _, _, r = run({"universe": "sp500"})
        assert r["trades"] == []
        WeekendTrend.members["r3000"] = member
        panel, _, r = run({"universe": "r3000", "stop_down_pct": 40.0})
        # Never tightened: 40% below the high of 100 is 60, so the closes of 85 hold and 55 in week 30 sells.
        assert len(r["trades"]) == 1 and r["trades"][0].exit_date == day(panel, 31, 0)
    finally:
        WeekendTrend.members = {}


def test_foreign_companies_are_left_out_of_the_us_universe():
    WeekendTrend.foreign = frozenset({"S0"})
    try:
        _, _, r = run({"universe": "us"})
        assert r["trades"] == []
        _, _, r = run({"universe": "all"})
        assert len(r["trades"]) == 1
    finally:
        WeekendTrend.foreign = frozenset()


def test_entry_delay_acts_on_the_signal_a_week_later():
    panel, _, r = run({"stop_rule": "ratchet", "entry_delay_weeks": 1})
    (tr,) = r["trades"]
    assert tr.signal_date == day(panel, 22, 4)   # the following week's close
    assert tr.entry_date == day(panel, 23, 0)
