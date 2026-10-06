"""Engine mechanics on a synthetic panel. Run: .venv\\Scripts\\python -m pytest research\\test_engine.py -q"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import Rules, Strategy, simulate  # noqa: E402
from panel import Panel  # noqa: E402

N_DAYS = 100


def make_panel(n_symbols=3, price=100.0, volume=1e6, factor=1.0):
    dates = np.array([f"2016-{1 + d // 28:02d}-{1 + d % 28:02d}" for d in range(N_DAYS)])
    shape = (N_DAYS, n_symbols)
    close = np.full(shape, price, dtype=float)
    return dict(dates=dates, symbols=np.array([f"S{j}" for j in range(n_symbols)]),
                kind=np.array(["stock"] * n_symbols), until=np.array([""] * n_symbols),
                open=close.copy(), high=close * 1.01, low=close * 0.99, close=close,
                volume=np.full(shape, volume), factor=np.full(shape, factor))


class Scripted(Strategy):
    """Signals given as {session: [columns]}; stop a fixed distance below entry."""

    name = "scripted"

    def __init__(self, signals, stop_pct=5.0, entry="close", exit_at=None):
        self.signals, self.stop_pct, self.entry, self.exit_at = signals, stop_pct, entry, exit_at or {}

    def candidates(self, t):
        return np.array(self.signals.get(t, []), dtype=int)

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return entry_price * (1 - self.stop_pct / 100)

    def on_close(self, pos, t):
        return ("close", "rule") if self.exit_at.get(pos.j) == t else None


def run(arrays, strategy, start_day=30):
    panel = Panel(**arrays)
    strategy.setup(panel, {})
    return panel, simulate(panel, strategy, str(panel.dates[start_day]), None, Rules())


def test_sizing_risks_half_a_percent_and_costs_ten_basis_points():
    a = make_panel()
    panel, r = run(a, Scripted({30: [0]}, stop_pct=5, exit_at={0: 40}))
    (tr,) = r["trades"]
    fill = 100 * 1.001
    assert tr.entry_price == pytest.approx(fill)
    assert tr.shares == pytest.approx(500 / (fill - 95))
    assert tr.exit_price == pytest.approx(100 * 0.999)
    assert tr.r == pytest.approx((99.9 - fill) / (fill - 95))
    assert tr.exit_reason == "rule"


def test_position_capped_at_twenty_percent():
    a = make_panel()
    panel, r = run(a, Scripted({30: [0]}, stop_pct=0.5, exit_at={0: 35}))
    assert r["trades"][0].position_value == pytest.approx(20_000)


def test_gap_below_stop_exits_at_open_and_intraday_stop_exits_at_stop():
    a = make_panel(n_symbols=2)
    a["open"][32, 0], a["low"][32, 0], a["close"][32, 0] = 90, 89, 91
    a["low"][33, 1] = 94.0
    panel, r = run(a, Scripted({30: [0, 1]}, stop_pct=5))
    by = {t.ticker: t for t in r["trades"]}
    assert by["S0"].exit_reason == "stop (gap)" and by["S0"].exit_price == pytest.approx(90 * 0.999)
    stop = 100 * 0.95
    assert by["S1"].exit_reason == "stop" and by["S1"].exit_price == pytest.approx(stop * 0.999)
    assert by["S1"].r == pytest.approx((stop * 0.999 - 100.1) / (100.1 - stop))


def test_low_priced_trades_pay_quarter_percent_using_as_traded_price():
    # Adjusted price 100, but a later 1:10 reverse split means it traded at 10.
    a = make_panel(price=100.0, volume=1e6, factor=0.1)
    panel, r = run(a, Scripted({30: [0]}, exit_at={0: 40}))
    assert r["trades"][0].entry_price == pytest.approx(100 * 1.0025)


def test_five_dollar_floor_uses_as_traded_price():
    cheap = Panel(**make_panel(price=1.0, volume=1e8, factor=10.0))  # adjusted $1, traded at $10
    assert cheap.eligible[40, 0]
    dear = Panel(**make_panel(price=10.0, volume=1e7, factor=0.1))  # adjusted $10, traded at $1
    assert not dear.eligible[40, 0]


def test_ten_slots_filled_by_highest_three_month_return():
    a = make_panel(n_symbols=12)
    for j in range(12):  # later columns rose more over the last 63 sessions
        a["close"][:70, j] = 100 / (1 + 0.05 * j)
        a["open"][:70, j] = a["close"][:70, j]
    panel, r = run(a, Scripted({70: list(range(12))}, stop_pct=10), start_day=70)
    held = sorted(t.ticker for t in r["trades"])
    assert held == sorted(f"S{j}" for j in range(2, 12))
    assert r["counts"]["skipped_no_slot"] == 2


def test_cash_limits_positions_once_fully_invested():
    a = make_panel(n_symbols=8)
    panel, r = run(a, Scripted({30: list(range(8))}, stop_pct=3))
    values = sorted(t.position_value for t in r["trades"])
    assert len(values) == 7 and values[-1] == pytest.approx(500 / 3.1 * 100.1)
    assert sum(values) == pytest.approx(100_000, rel=1e-6)
    assert sum(t.cash_limited for t in r["trades"]) == 1
    assert r["counts"]["skipped_no_cash"] == 1


def test_delisted_security_exits_at_last_close():
    a = make_panel(n_symbols=2)
    for f in ("open", "high", "low", "close"):
        a[f][50:, 0] = np.nan
    panel, r = run(a, Scripted({30: [0]}))
    (tr,) = r["trades"]
    assert tr.exit_reason == "delisted" and tr.exit_date == str(panel.dates[49])


def test_next_open_entry_sets_stop_from_the_open():
    a = make_panel()
    a["open"][31, 0] = 102
    panel, r = run(a, Scripted({30: [0]}, stop_pct=5, entry="open", exit_at={0: 40}))
    (tr,) = r["trades"]
    assert tr.entry_date == str(panel.dates[31])
    assert tr.stop == pytest.approx(102 * 0.95)


def test_open_positions_close_at_end_of_test():
    a = make_panel()
    panel, r = run(a, Scripted({30: [0]}))
    assert r["trades"][0].exit_reason == "end of test"
    assert r["equity"][-1] == pytest.approx(100_000 - r["trades"][0].shares * 100 * 0.002)


class StopOrders(Strategy):
    """Buy-stop orders given as {session: {column: trigger}}; exits as {column: {session: decision}}."""

    name = "stop_orders"
    entry = "stop"

    def __init__(self, orders, exits=None, stop_pct=5.0):
        self.scripted, self.exits, self.stop_pct = orders, exits or {}, stop_pct

    def orders(self, t):
        return self.scripted.get(t, {})

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return entry_price * (1 - self.stop_pct / 100)

    def on_close(self, pos, t):
        decision = self.exits.get(pos.j, {}).get(t)
        if decision and decision[0] == "partial":
            pos.stop = pos.entry_fill
        return decision


def test_buy_stop_fills_at_trigger_or_at_the_open_on_a_gap():
    a = make_panel(n_symbols=2)
    a["high"][31, 0] = 102.0  # trades through 101 during the day
    a["open"][31, 1], a["high"][31, 1], a["close"][31, 1] = 103.0, 104.0, 103.5  # gaps above 101
    panel, r = run(a, StopOrders({30: {0: 101.0, 1: 101.0}}))
    by = {t.ticker: t for t in r["trades"]}
    assert by["S0"].entry_date == str(panel.dates[31]) and by["S0"].entry_price == pytest.approx(101 * 1.001)
    assert by["S0"].entry_note == "through trigger"
    assert by["S1"].entry_price == pytest.approx(103 * 1.001) and by["S1"].entry_note == "gap above trigger"
    assert r["counts"]["triggered"] == 2


def test_buy_stop_lives_for_five_sessions_after_the_signal():
    a = make_panel(n_symbols=2)
    a["high"][35, 0] = 106.0  # fifth session after the signal: filled
    a["high"][36, 1] = 106.0  # sixth session: expired
    panel, r = run(a, StopOrders({30: {0: 105.0, 1: 105.0}}))
    assert [t.ticker for t in r["trades"]] == ["S0"]
    assert r["trades"][0].entry_date == str(panel.dates[35])


def test_partial_sale_then_final_exit_blends_the_trade():
    a = make_panel()
    a["high"][31, 0] = 102.0
    for f, v in (("open", 110.0), ("high", 111.0), ("low", 109.0), ("close", 110.0)):
        a[f][32:, 0] = v
    a["close"][40, 0] = 120.0
    panel, r = run(a, StopOrders({30: {0: 101.0}}, exits={0: {34: ("partial", "day 3", 1 / 3), 40: ("close", "rule")}}))
    (tr,) = r["trades"]
    entry = 101 * 1.001
    average = (110 * 0.999) / 3 + (120 * 0.999) * 2 / 3
    assert tr.exit_price == pytest.approx(average)
    assert tr.r == pytest.approx((average - entry) / (entry - 101 * 0.95))
    assert tr.exit_reason == "day 3 + rule" and tr.partial_date == str(panel.dates[34])
    assert tr.pnl == pytest.approx(tr.shares * (average - entry))
    assert r["equity"][-1] == pytest.approx(100_000 + tr.pnl)


def test_breakeven_stop_after_partial_sale():
    a = make_panel()
    a["high"][31, 0] = 102.0
    for f, v in (("open", 108.0), ("high", 109.0), ("low", 107.0), ("close", 108.0)):
        a[f][32:, 0] = v
    a["low"][36, 0] = 100.0  # below the entry fill of 101.101
    panel, r = run(a, StopOrders({30: {0: 101.0}}, exits={0: {34: ("partial", "day 3", 1 / 3)}}))
    (tr,) = r["trades"]
    assert tr.exit_reason == "day 3 + stop (after partial)" and tr.exit_date == str(panel.dates[36])
    assert tr.final_exit_price == pytest.approx(101 * 1.001 * 0.999)


def test_buy_stop_can_be_stopped_on_its_entry_day():
    # A down day (open 100, close 99): open, high, low, close. Through 101, then below the 95.95 stop.
    a = make_panel()
    a["high"][31, 0], a["low"][31, 0], a["close"][31, 0] = 102.0, 95.0, 99.0
    panel, r = run(a, StopOrders({30: {0: 101.0}}))
    (tr,) = r["trades"]
    assert tr.exit_date == tr.entry_date and tr.exit_reason == "stop"
    assert r["counts"]["entry_day_stop_exits"] == 1


def test_up_day_breakout_is_not_stopped_by_a_low_that_came_first():
    # An up day (open 100, close 101.5): open, low, high, close. The 95 low preceded the breakout.
    a = make_panel()
    a["high"][31, 0], a["low"][31, 0], a["close"][31, 0] = 102.0, 95.0, 101.5
    panel, r = run(a, StopOrders({30: {0: 101.0}}))
    (tr,) = r["trades"]
    assert tr.exit_reason == "end of test" and r["counts"]["entry_day_stop_exits"] == 0
    panel = Panel(**a)
    s = StopOrders({30: {0: 101.0}})
    s.setup(panel, {})
    worst = simulate(panel, s, str(panel.dates[30]), None, Rules(entry_day_path="worst"))
    assert worst["trades"][0].exit_reason == "stop"


def replay(a, rule, entry_price=101.0, stop=96.0):
    from strategies.exit_replay import Entry, ExitReplay
    panel = Panel(**a)
    s = ExitReplay({"test": [Entry(j=0, signal_day=30, entry_day=31, price=entry_price, stop=stop, note="through trigger")]})
    s.setup(panel, {"exit": rule})
    s.begin_period("test")
    return panel, simulate(panel, s, str(panel.dates[30]), None, Rules())


def test_half_at_two_r_then_breakeven_stop():
    a = make_panel()
    for f, v in (("open", 105.0), ("high", 106.0), ("low", 104.0), ("close", 105.0)):
        a[f][32:, 0] = v
    a["high"][33, 0] = 112.0  # reaches the +2R target of 111.303
    a["low"][36, 0] = 100.0  # below the breakeven stop
    panel, r = replay(a, {"kind": "partial_r", "r": 2, "fraction": 0.5, "n": 20})
    (tr,) = r["trades"]
    fill = 101 * 1.001
    target = fill + 2 * (fill - 96)
    assert tr.partial_date == str(panel.dates[33]) and tr.partial_exit_price == pytest.approx(target * 0.999)
    assert tr.exit_reason == "half at +2R + stop (after partial)" and tr.exit_date == str(panel.dates[36])
    assert tr.exit_price == pytest.approx(0.5 * target * 0.999 + 0.5 * fill * 0.999)


def test_chandelier_trails_the_highest_high_by_three_atr():
    a = make_panel()  # true range 2 before entry, so the distance is 6
    for f, v in (("open", 108.0), ("high", 109.0), ("low", 107.0), ("close", 108.0)):
        a[f][32:, 0] = v
    a["high"][35, 0] = 110.0  # stop rises to 104 from the next session
    a["low"][36, 0] = 103.0
    panel, r = replay(a, {"kind": "chandelier", "mult": 3, "atr": 14})
    (tr,) = r["trades"]
    assert tr.exit_date == str(panel.dates[36]) and tr.exit_price == pytest.approx(104 * 0.999)


def test_time_exit_and_scheduled_entry_price():
    a = make_panel()
    panel, r = replay(a, {"kind": "time", "days": 5})
    (tr,) = r["trades"]
    assert tr.entry_price == pytest.approx(101 * 1.001) and tr.stop == 96.0
    assert tr.exit_date == str(panel.dates[36]) and tr.exit_reason == "time exit, day 5"


class ConfirmAtClose(Strategy):
    name = "confirm_at_close"
    entry = "close_confirm"

    def __init__(self, orders):
        self.scripted = orders

    def orders(self, t):
        return self.scripted.get(t, {})

    def confirm(self, j, t_signal, t, trigger):
        p = self.panel
        rng = p.high[t, j] - p.low[t, j]
        return p.close[t, j] > trigger and rng > 0 and (p.close[t, j] - p.low[t, j]) / rng >= 2 / 3

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return float(self.panel.low[t_entry, j])


def test_close_confirm_buys_strong_closes_and_drops_weak_breakouts():
    a = make_panel(n_symbols=2)
    a["high"][31, 0], a["low"][31, 0], a["close"][31, 0] = 103.0, 99.0, 102.5  # strong close above 101
    a["high"][31, 1], a["low"][31, 1], a["close"][31, 1] = 103.0, 99.0, 100.5  # closes back below 101
    a["high"][32, 1], a["low"][32, 1], a["close"][32, 1] = 103.0, 100.0, 102.9  # too late: order dropped
    panel, r = run(a, ConfirmAtClose({30: {0: 101.0, 1: 101.0}}))
    (tr,) = r["trades"]
    assert tr.ticker == "S0" and tr.entry_date == str(panel.dates[31])
    assert tr.entry_price == pytest.approx(102.5 * 1.001) and tr.stop == 99.0
    assert r["counts"]["triggered"] == 2 and r["counts"]["not_confirmed"] == 1


def test_earnings_day_mapping_covers_before_open_after_close_and_non_session_dates():
    from strategies.episodic_pivot import earnings_mask
    panel = Panel(**make_panel(n_symbols=1))
    mask = earnings_mask(panel, {"S0": ["2016-01-05", "2016-01-29"]})  # 01-29 is not a session here
    days = [str(d) for d in panel.dates[mask[:, 0]]]
    assert days == ["2016-01-05", "2016-01-06", "2016-02-01"]


def test_episodic_pivot_signal_needs_gap_volume_and_earnings():
    from strategies.episodic_pivot import DEFAULTS, earnings_mask, signal_mask, strong_close
    a = make_panel(n_symbols=3)
    for j, gap in ((0, 1.12), (1, 1.08), (2, 1.12)):
        a["open"][70, j], a["high"][70, j], a["low"][70, j], a["close"][70, j] = 100 * gap, 100 * gap + 3, 100 * gap - 1, 100 * gap + 2
        a["volume"][70, j] = 4e6  # 4x the 1e6 average
    panel = Panel(**a)
    earnings = earnings_mask(panel, {"S0": [str(panel.dates[69])], "S1": [str(panel.dates[69])]})  # S2 has none
    signal = signal_mask(panel, DEFAULTS, earnings)
    assert list(np.flatnonzero(signal[70])) == [0]
    assert signal_mask(panel, {**DEFAULTS, "earnings": False}, None)[70].tolist() == [True, False, True]
    assert strong_close(panel, 0.5)[70, 0]


def vcp_panel(final_depth=0.05, final_volume=5e5):
    """Uptrend to 120, then pullbacks of 20%, 10% and `final_depth`, then a bounce toward the pivot."""
    points = [(0, 80), (100, 120), (110, 96), (120, 119), (128, 107.1), (136, 118), (140, 118 * (1 - final_depth)),
              (143, 118 * (1 - final_depth) * 1.04), (160, 118 * (1 - final_depth) * 1.04)]
    days = np.arange(161)
    close = np.interp(days, [d for d, _ in points], [v for _, v in points])
    a = make_panel(n_symbols=1)
    n = len(days)
    a["dates"] = np.array([str(np.datetime64("2016-01-01") + np.timedelta64(int(d), "D")) for d in days])
    a["close"], a["open"] = close[:, None].copy(), close[:, None].copy()
    a["high"], a["low"] = close[:, None] * 1.004, close[:, None] * 0.996
    a["volume"] = np.full((n, 1), 1e6)
    a["volume"][137:144, 0] = final_volume
    a["factor"] = np.ones((n, 1))
    return Panel(**a)


def test_vcp_finds_contracting_pullbacks_and_the_pivot():
    from strategies.minervini import DEFAULTS, vcp_scan
    panel = vcp_panel()
    scan = vcp_scan(panel, np.ones(panel.close.shape, dtype=bool), DEFAULTS)
    t = 143
    assert scan["setup"][t, 0]
    assert scan["pivot"][t, 0] == pytest.approx(118 * 1.004)
    assert scan["peak_at"][t, 0] == 136


def test_vcp_rejects_a_deep_final_pullback_or_heavy_volume():
    from strategies.minervini import DEFAULTS, vcp_scan
    deep = vcp_panel(final_depth=0.12)  # 12% after 10%: not shallower, and over the 10% limit
    assert not vcp_scan(deep, np.ones(deep.close.shape, dtype=bool), DEFAULTS)["setup"][143, 0]
    heavy = vcp_panel(final_volume=2e6)
    assert not vcp_scan(heavy, np.ones(heavy.close.shape, dtype=bool), DEFAULTS)["setup"][143, 0]


def spy_panel(spy_path=None):
    a = make_panel(n_symbols=2)
    a["symbols"] = np.array(["SPY", "S1"])
    if spy_path is not None:
        for f in ("open", "high", "low", "close"):
            a[f][:, 0] = spy_path
    return Panel(**a)


def test_idle_cash_earns_the_tbill_rate_by_calendar_day():
    from datetime import date
    panel = spy_panel()
    s = Scripted({})
    s.setup(panel, {})
    market = {"cash_rate": np.full(len(panel.dates), 5.0), "spy_tr": panel.close[:, 0]}
    r = simulate(panel, s, str(panel.dates[30]), None, Rules(idle_cash_tbill=True), market)
    expected = 100_000.0
    for t in range(31, len(panel.dates)):
        days = (date.fromisoformat(panel.dates[t]) - date.fromisoformat(panel.dates[t - 1])).days
        expected *= 1 + 0.05 * days / 365
    assert r["equity"][-1] == pytest.approx(expected)


def test_overlay_holds_spy_when_idle_and_sells_it_to_fund_trades():
    path = np.linspace(100, 110, N_DAYS)  # SPY rises 10% over the panel
    panel = spy_panel(path)
    market = {"cash_rate": np.zeros(len(panel.dates)), "spy_tr": panel.close[:, 0]}
    idle = Scripted({})
    idle.setup(panel, {})
    r = simulate(panel, idle, str(panel.dates[30]), None, Rules(overlay_spy=True), market)
    assert r["equity"][-1] == pytest.approx(100_000 * 0.999 * path[-1] / path[30])
    trade = Scripted({40: [1]}, stop_pct=5, exit_at={1: 50})
    trade.setup(panel, {})
    r = simulate(panel, trade, str(panel.dates[30]), None, Rules(overlay_spy=True), market)
    (tr,) = r["trades"]
    held_out = tr.shares * tr.entry_price  # SPY sold at day 40 to pay for the trade, bought back at day 50
    units = 100_000 * 0.999 / path[30]
    units -= held_out / (path[40] * 0.999)
    units += (tr.shares * tr.exit_price) * 0.999 / path[50]
    assert r["equity"][-1] == pytest.approx(units * path[-1])


def test_prior_liquidity_ignores_the_signal_days_own_volume():
    from strategies.episodic_pivot import prior_liquidity
    a = make_panel(n_symbols=2, volume=1e5)  # $10M a day: below the floor
    a["volume"][70, 0] = 1e8  # the gap day alone would lift the 20-day average above $20M
    a["factor"][:50, 1] = 1 / 200.0  # a 1-for-200 reverse split later: excluded outright
    a["volume"][:, 1] = 1e6
    panel = Panel(**a)
    assert panel.eligible[70, 0]  # signal-day rule: the gap day counts
    mask = prior_liquidity(panel, {"split_window_start": "2016-01-01"})
    assert not mask[70, 0] and not mask[:, 1].any()


def test_day3_sale_only_above_entry_and_time_then_sma():
    a = make_panel()
    a["close"][34, 0] = 99.0  # day-3 close below the 100.1 entry: no sale, stop unchanged
    for f, v in (("open", 97.0), ("high", 97.97), ("low", 96.03), ("close", 97.0)):
        a[f][36:, 0] = v  # below the 10-day SMA and below breakeven, but above the 95 stop
    panel = Panel(**a)
    from strategies.exit_replay import Entry, ExitReplay
    s = ExitReplay({"t": [Entry(0, 30, 31, 100.0, 95.0, "through trigger")]})
    s.setup(panel, {"exit": {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10, "require_profit": True}})
    s.begin_period("t")
    r = simulate(panel, s, str(panel.dates[30]), None, Rules())
    (tr,) = r["trades"]
    assert tr.partial_date == "" and tr.exit_reason == "close below 10-day SMA"  # not stopped at breakeven
    assert tr.exit_date == str(panel.dates[36])
    s = ExitReplay({"t": [Entry(0, 30, 31, 100.0, 95.0, "through trigger")]})
    s.setup(panel, {"exit": {"kind": "time_then_sma", "days": 20, "n": 10}})
    s.begin_period("t")
    (tr,) = simulate(panel, s, str(panel.dates[30]), None, Rules())["trades"]
    assert tr.exit_reason == "end of test"  # the close never falls below the SMA again after day 20


def test_reserve_funds_trades_without_selling_spy():
    panel = spy_panel(np.full(N_DAYS, 100.0))
    market = {"cash_rate": np.zeros(len(panel.dates)), "spy_tr": panel.close[:, 0]}
    s = Scripted({40: [1]}, stop_pct=3, exit_at={1: 45})
    s.setup(panel, {})
    r = simulate(panel, s, str(panel.dates[30]), None, Rules(overlay_spy=True, reserve_pct=10), market)
    (tr,) = r["trades"]
    assert tr.position_value == pytest.approx(10_000)  # wanted about 16% of equity; the reserve holds 10%
    assert tr.cash_limited and r["skips"][0]["reason"] == "shrunk to the cash available"


class CountedSetup(Strategy):
    """Precomputes one array and counts its set-ups; on_close records into a per-run dict."""

    name = "counted"
    setups = 0

    def __init__(self, tag="plain"):
        self.tag, self.seen = tag, {}

    def setup(self, panel, params):
        super().setup(panel, params)
        CountedSetup.setups += 1
        self.signal = panel.close * params["scale"]


def test_an_identical_set_up_is_reused_with_shared_read_only_arrays_and_fresh_run_state():
    import engine
    engine._SETUPS.clear()
    CountedSetup.setups = 0
    panel = Panel(**make_panel())
    first = engine.set_up(CountedSetup, panel, {"scale": 2})
    first.seen["run one"] = True
    second = engine.set_up(CountedSetup, panel, {"scale": 2})
    assert CountedSetup.setups == 1
    assert second.signal is first.signal and not second.signal.flags.writeable
    assert second.seen == {} and second.panel is panel and second.params == {"scale": 2}
    with pytest.raises(ValueError):
        second.signal[0, 0] = 0
    # Other parameters, another factory or constructor state that is not plain data set up afresh.
    engine.set_up(CountedSetup, panel, {"scale": 3})
    engine.set_up(lambda: CountedSetup(), panel, {"scale": 2})
    engine.set_up(lambda: CountedSetup(tag=object()), panel, {"scale": 2})
    assert CountedSetup.setups == 4
    assert panel.close.flags.writeable   # the panel's own arrays are never frozen
