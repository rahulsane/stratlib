"""MA pullback mechanics on synthetic bars. Run: .venv\\Scripts\\python -m pytest research\\test_ma_pullback.py -q"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import Rules, simulate  # noqa: E402
from panel import Panel  # noqa: E402
from strategies import ma_pullback as M  # noqa: E402

WARM = 20  # clear sessions first, so the liquidity average exists before the pattern
# (high, low, close) with the area fixed at 99-101 (the 50-day EMA at 100, half an ATR of 2 either side).
PATH = [
    (106, 104, 105),      # clear above the area; swing high 106
    (103, 100.5, 102),    # pullback 1 (touch 1): its swing high is 106
    (104, 100, 103),
    (107, 103, 106),      # a new high: test 1 completed
    (104, 100.5, 101),    # in the area but not yet clear above it again: no new pullback
    (108, 102, 107),      # clear; swing high 108
    (104, 100.8, 101.5),  # pullback 2
    (109, 102, 108.5),    # test 2 completed
    (110, 103, 109),      # clear; swing high 110
    (101.2, 99.0, 101.1),  # pullback 3, a hammer: the signal
    (104, 101, 103.5),    # bought at the open (101.5)
    (109.8, 103, 109.6),  # reaches the target, 110 - 0.25 ATR = 109.5 (no new high yet: still pullback 3)
    (110, 99.5, 99.8),    # closes below the 50-day EMA (the trailing exit sells at the next open)
    (100, 98.6, 98.8),    # closes below the area: the count resets
    (103, 101.5, 102),    # clear again
    (102, 100, 101),      # touch 1 of a new count
]
OPENS = {9: 101.0, 10: 101.5, 13: 99.0}


def bars():
    n = WARM + len(PATH)
    h, lo, c = np.zeros(n), np.zeros(n), np.zeros(n)
    h[:WARM], lo[:WARM], c[:WARM] = 105, 103, 104
    for i, (a, b, x) in enumerate(PATH):
        h[WARM + i], lo[WARM + i], c[WARM + i] = a, b, x
    o = np.clip(np.concatenate([[c[0]], c[:-1]]), lo, h)   # the previous close, within the day's range, unless set
    for i, v in OPENS.items():
        o[WARM + i] = v
    return o, h, lo, c


def make_panel():
    o, h, lo, c = bars()
    n = len(c)
    start = date(2016, 1, 4)
    days = [(start + timedelta(days=k)).isoformat() for k in range(n)]
    two = lambda a, spy: np.column_stack([spy, a])  # noqa: E731
    spy = np.linspace(200, 210, n)
    return Panel(dates=np.array(days), symbols=np.array(["SPY", "S0"]), kind=np.array(["etf", "stock"]),
                 until=np.array(["", ""]), open=two(o, spy), high=two(h, spy), low=two(lo, spy), close=two(c, spy),
                 volume=np.full((n, 2), 1e6), factor=np.ones((n, 2)), min_price=1.0, min_dollar_volume=0.0)


def inject(panel, trend_rising=True):
    """Fixed indicators for the synthetic panel: the 50-day EMA at 100, ATR 2, the 200-day EMA at 90."""
    n, m = panel.close.shape
    area, a = np.full((n, m), 100.0), np.full((n, m), 2.0)
    trend = np.full((n, m), 90.0) + (np.arange(n)[:, None] * (0.1 if trend_rising else -0.1))
    q = M.DEFAULTS
    ind = {"ema_area": area, "ema_trend": trend, "atr": a, "near": panel.low <= area + 1.0,
           **M.touches(panel.high, panel.low, panel.close, area, q["band_atr"] * a),
           **M.candles(panel.open, panel.high, panel.low, panel.close)}
    M._CACHE.clear()
    M._CACHE[(id(panel), q["ema_area"], q["ema_trend"], q["atr_sessions"], q["band_atr"])] = ind
    return ind


def run(**params):
    panel = make_panel()
    inject(panel, trend_rising=params.pop("rising_200", True))
    s = M.MaPullback()
    s.setup(panel, {"universe": "all", **params})
    rules = Rules(risk_pct=1.0, max_position_pct=20.0, slippage_pct=0.0, slippage_low_price_pct=0.0)
    return panel, s, simulate(panel, s, str(panel.dates[0]), None, rules)


def test_touch_counting():
    p = make_panel()
    ind = inject(p)
    touch = ind["touch"][WARM:, 1].tolist()
    assert touch == [0, 1, 1, 0, 0, 0, 2, 0, 0, 3, 3, 3, 3, 0, 0, 1]
    assert ind["ref"][WARM + 1, 1] == 106 and ind["ref"][WARM + 6, 1] == 108 and ind["ref"][WARM + 9, 1] == 110
    assert ind["low"][WARM + 9, 1] == 99.0
    pid = ind["pid"][WARM:, 1]
    assert pid[1] == pid[2] and pid[6] == pid[1] + 1 and pid[9] == pid[6] + 1 and pid[15] == pid[9] + 1


def test_hammer_and_engulfing_follow_his_guide():
    col = lambda *xs: np.array(xs, dtype=float)[:, None]  # noqa: E731
    k = M.candles(col(10, 10), col(10.1, 10.5), col(9, 9), col(10.05, 10.05))
    assert k["hammer"][:, 0].tolist() == [True, False]     # the close must be in the top quarter of the range
    covers = M.candles(col(10, 9.5), col(10, 10), col(9.4, 9.4), col(9.5, 10.0))
    short = M.candles(col(10, 9.5), col(10, 10), col(9.4, 9.4), col(9.5, 9.9))
    assert bool(covers["engulfing"][1, 0]) and not bool(short["engulfing"][1, 0])


def test_third_touch_entry_target_and_stops():
    panel, s, r = run(exit="target", stop="swing")
    (tr,) = r["trades"]
    assert tr.signal_date == panel.dates[WARM + 9] and tr.entry_date == panel.dates[WARM + 10]
    assert tr.entry_price == pytest.approx(101.5)
    assert tr.stop == pytest.approx(99.0 - 2.0)              # 1 ATR below the pullback's low
    assert tr.exit_reason == "target" and tr.exit_price == pytest.approx(109.5)
    _, _, r = run(exit="target", stop="entry")
    assert r["trades"][0].stop == pytest.approx(101.5 - 4.0)  # 2 ATR below the entry


def test_trailing_exit_sells_at_the_open_after_a_close_below_the_ema():
    panel, _, r = run(exit="trail")
    (tr,) = r["trades"]
    assert tr.exit_reason == M.EXIT_REASON
    assert tr.exit_date == panel.dates[WARM + 13] and tr.exit_price == pytest.approx(99.0)


def test_trend_filters():
    _, s, r = run(trend="rising", rising_200=False)
    assert r["trades"] == [] and s.signal_count == 0      # a falling 200-day EMA blocks the rising filter
    _, s, r = run(trend="above", rising_200=False)
    assert len(r["trades"]) == 1                           # the close is still above it


def test_only_the_third_touch_and_no_target_room():
    _, s, _ = run(touch=2)
    assert s.signal_count == 0           # the second pullback had no trigger candle
    panel, s, r = run(exit="target", target_atr=4.5)  # target 110 - 9 = 101 is below the 101.5 entry
    assert r["trades"] == [] and r["counts"]["skipped_stop_rule"] == 1
