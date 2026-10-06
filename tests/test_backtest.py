"""Synthetic execution paths isolate fill timing, portfolio accounting and bias."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from stratlib.backtest import BacktestError, Signal, performance, simulate
from stratlib.config import BacktestSettings, ConfigError, Thresholds
from stratlib.market_direction import CONFIRMED, CORRECTION
from stratlib.prices import Bar, parse_bars
from conftest import load_fixture

T = Thresholds()
P = BacktestSettings(initial_capital=1000, max_holdings=1)


def days(n=5):
    start = date(2025, 1, 6)
    result = []
    while len(result) < n:
        if start.weekday() < 5:
            result.append(start.isoformat())
        start += timedelta(days=1)
    return result


def bar(day, opening=100, close=None, low=None, high=None):
    close = opening if close is None else close
    return Bar(day, opening, high if high is not None else max(opening, close) + 1,
               low if low is not None else min(opening, close) - 1, close, 1_000_000)


def signal(day, symbol="AAA", pivot=100, rs=99):
    return Signal(symbol, day, pivot, rs, "Flat base", {"synthetic": True})


def run(prices, signals=None, *, t=T, p=P, states=None, **kwargs):
    calendar = [b.date for b in prices]
    histories = {"AAA": prices, "SPY": [bar(d) for d in calendar]}
    histories.update(kwargs.pop("extra", {}))
    signals = signals if signals is not None else {calendar[0]: [signal(calendar[0])]}
    return simulate(calendar, histories, states or dict.fromkeys(calendar, CONFIRMED),
                    lambda d: signals.get(d, []), t, p, **kwargs)


def test_signal_close_enters_next_open_and_final_mark_stays_open():
    d = days(3)
    result = run([bar(d[0], 10), bar(d[1], 102, 110), bar(d[2], 111)])
    assert result["equity_curve"][0]["holdings"] == 0
    holding = result["open_positions"][0]
    assert (holding["entry_date"], holding["entry_price"]) == (d[1], 102)
    assert holding["shares"] == pytest.approx(1000 / 102)
    assert result["metrics"]["ending_equity"] == pytest.approx(1000 * 111 / 102)
    assert result["metrics"]["win_rate_pct"] is None
    assert not result["trades"]


@pytest.mark.parametrize("opening,entered", [(105, True), (105.001, False), (94, True)])
def test_next_open_buy_zone_boundary(opening, entered):
    d = days(3)
    result = run([bar(d[0]), bar(d[1], opening), bar(d[2], opening)])
    assert bool(result["open_positions"]) is entered
    if not entered:
        assert result["skipped_entries"][0]["reason"] == "Open above maximum pivot distance"


def test_market_uses_signal_day_state_without_next_day_lookahead():
    d = days(3)
    prices = [bar(day) for day in d]
    result = run(prices, states={d[0]: CONFIRMED, d[1]: CORRECTION, d[2]: CORRECTION})
    assert len(result["open_positions"]) == 1
    result = run(prices, states={d[0]: CORRECTION, d[1]: CONFIRMED, d[2]: CONFIRMED})
    assert not result["open_positions"]


@pytest.mark.parametrize("opening,low,expected", [(100, 93, 93), (90, 89, 90)])
def test_daily_low_stop_and_worse_opening_gap(opening, low, expected):
    d = days(3)
    result = run([bar(d[0]), bar(d[1]), bar(d[2], opening, low=low)])
    assert result["trades"][0]["exit_price"] == expected
    assert result["metrics"]["ending_equity"] == pytest.approx(expected * 10)


def test_entry_day_stop_and_same_bar_high_cannot_override_it():
    d = days(3)
    result = run([bar(d[0]), bar(d[1], close=125, low=92, high=130), bar(d[2])])
    trade = result["trades"][0]
    assert trade["entry_date"] == trade["exit_date"] == d[1]
    assert trade["exit_price"] == 93
    assert trade["reason"] == "Stop loss"


def test_profit_target_is_close_confirmed_and_fills_next_open():
    d = days(4)
    t = replace(T, fast_gain_pct=50)
    result = run([bar(d[0]), bar(d[1], high=130), bar(d[2], close=120), bar(d[3], 118)], t=t)
    trade = result["trades"][0]
    assert (trade["exit_date"], trade["exit_price"]) == (d[3], 118)
    assert trade["return_pct"] == pytest.approx(18)


def test_gap_stop_takes_priority_over_pending_profit_order():
    d = days(4)
    result = run([bar(d[0]), bar(d[1]), bar(d[2], close=120), bar(d[3], 89)], t=replace(T, fast_gain_pct=50))
    assert result["trades"][0]["reason"] == "Gap through stop"
    assert result["trades"][0]["exit_price"] == 89


def test_fast_gain_hold_expires_eight_calendar_weeks_after_breakout():
    d = days(43)
    prices = [bar(day, 100 if i < 2 else 121) for i, day in enumerate(d)]
    result = run(prices)
    trade = result["trades"][0]
    expiry = (date.fromisoformat(d[0]) + timedelta(weeks=8)).isoformat()
    assert trade["fast_gain_date"] == d[2]
    assert trade["exit_date"] == d[d.index(expiry) + 1]


def test_fast_gain_boundary_inclusive_and_stop_overrides_active_hold():
    d = days(19)
    deadline = (date.fromisoformat(d[0]) + timedelta(weeks=3)).isoformat()
    prices = [bar(day, 100 if day < deadline else 120) for day in d]
    result = run(prices)
    assert result["open_positions"][0]["fast_gain_date"] == deadline
    prices[-1] = bar(d[-1], 90)
    result = run(prices)
    assert result["trades"][0]["exit_price"] == 90


def test_gain_first_reached_after_three_weeks_gets_normal_profit_exit():
    d = days(19)
    deadline = (date.fromisoformat(d[0]) + timedelta(weeks=3)).isoformat()
    prices = [bar(day, 100 if day <= deadline else 120) for day in d]
    result = run(prices)
    assert result["trades"][0]["fast_gain_date"] is None
    assert result["trades"][0]["reason"] == "Profit target, next open"


def test_equal_allocations_ranking_capacity_and_no_intraday_reuse_of_cash():
    d = days(4)
    prices = [bar(day) for day in d]
    signals = {d[0]: [signal(d[0], "CCC", rs=80), signal(d[0], "BBB", rs=90), signal(d[0], "AAA", rs=99)],
               d[1]: [signal(d[1], "CCC", rs=99)]}
    prices[2] = bar(d[2], low=90)
    result = run(prices, signals, p=replace(P, max_holdings=2), extra={"BBB": [bar(day, 50) for day in d],
                                                                   "CCC": [bar(day) for day in d]})
    assert result["trades"][0]["shares"] == 5
    assert result["open_positions"][0]["symbol"] == "BBB"
    assert result["open_positions"][0]["shares"] == 10
    assert len(result["skipped_entries"]) == 2
    assert result["equity_curve"][2]["cash"] == 465


def test_opening_exit_proceeds_are_available_to_other_scheduled_entries():
    d = days(4)
    signals = {d[0]: [signal(d[0])], d[1]: [signal(d[1], "BBB")]}
    result = run([bar(d[0]), bar(d[1], close=120), bar(d[2], 119), bar(d[3], 119)], signals,
                 extra={"BBB": [bar(day) for day in d]}, t=replace(T, fast_gain_pct=50))
    assert result["open_positions"][0]["symbol"] == "BBB"
    assert result["open_positions"][0]["value"] == pytest.approx(1190)


def test_missing_immediate_open_skips_instead_of_delaying_entry():
    d = days(4)
    prices = [bar(day) for day in d]
    result = run(prices, {d[0]: [signal(d[0], "BBB")]}, extra={"BBB": [bar(d[2]), bar(d[3])]})
    assert not result["trades"] and not result["open_positions"]
    assert "Missing next-session" in result["skipped_entries"][0]["reason"]


def test_missing_held_bar_or_unknown_delisting_cannot_create_optimistic_equity():
    d = days(4)
    prices = [bar(day) for day in d]
    with pytest.raises(BacktestError, match="held-position"):
        run(prices, {d[0]: [signal(d[0], "BBB")]}, extra={"BBB": [bar(d[1])]})
    with pytest.raises(BacktestError, match="delisting proceeds"):
        run(prices, delisted={"AAA": d[2]})
    result = run(prices, delisted={"AAA": d[2]}, settlements={("AAA", d[2]): 0})
    assert result["trades"][0]["return_pct"] == -100
    assert result["metrics"]["max_drawdown_pct"] == 100


def test_future_price_mutation_cannot_change_prior_equity_or_fills():
    d = days(7)
    prices = [bar(day) for day in d]
    before = run(prices[:5])
    prices[-1] = bar(d[-1], 2)
    after = run(prices)
    assert before["equity_curve"] == after["equity_curve"][:5]
    assert after["trades"][0]["entry_date"] == before["open_positions"][0]["entry_date"]


def test_metrics_drawdown_cagr_and_gain_loss_averages():
    curve = [{"equity": v} for v in [900, 1200, 960, 1100]]
    trades = [{"return_pct": v} for v in [10, 30, -5, -15, 0]]
    metrics = performance(curve, trades, 1000, "2024-01-01", "2025-01-01")
    assert metrics["max_drawdown_pct"] == pytest.approx(20)
    assert metrics["cagr_pct"] == pytest.approx(100 * (1.1 ** (365.25 / 366) - 1))
    assert metrics["win_rate_pct"] == 40
    assert metrics["average_gain_pct"] == 20
    assert metrics["average_loss_pct"] == -10
    assert metrics["gain_loss_ratio"] == 2


def test_recorded_spy_benchmark_matches_open_to_close_return():
    prices = [b for b in parse_bars(load_fixture("historical-price-eod_full_SPY_2026-08-01.json")) if b.date <= "2026-09-25"]
    calendar = [b.date for b in prices]
    result = simulate(calendar, {"SPY": prices}, dict.fromkeys(calendar, CORRECTION), lambda _: [], T, P)
    assert result["spy_metrics"]["ending_equity"] == pytest.approx(1000 * prices[-1].close / prices[0].open)
    assert result["metrics"]["total_return_pct"] == 0


@pytest.mark.parametrize("values", [{"max_holdings": 0}, {"max_holdings": True}, {"max_holdings": 1.5},
                                    {"initial_capital": float("nan")}, {"initial_capital": -1}, {"default_years": 0}])
def test_portfolio_options_validate(values):
    with pytest.raises(ConfigError):
        BacktestSettings(**values)


def test_exposure_limits_new_positions_to_its_share_of_holdings():
    d = days(3)
    p = BacktestSettings(initial_capital=1000, max_holdings=4)
    extra = {s: [bar(day) for day in d] for s in ("BBB", "CCC")}
    signals = {d[0]: [signal(d[0], s) for s in ("AAA", "BBB", "CCC")]}
    result = run([bar(day) for day in d], signals, p=p, extra=extra, exposure=dict.fromkeys(d, 50.0))
    assert sorted(h["symbol"] for h in result["open_positions"]) == ["AAA", "BBB"]
    assert result["skipped_entries"][0]["reason"] == "Market exposure limit (2 of 4 positions)"
    assert [point["exposure"] for point in result["equity_curve"]] == [50.0] * 3


def test_raise_cash_sells_the_weakest_holdings_at_the_next_open():
    d = days(4)
    p = BacktestSettings(initial_capital=1000, max_holdings=2, raise_cash=True)
    extra = {"BBB": [bar(d[0]), bar(d[1]), bar(d[2], 100, 98), bar(d[3], 97)]}
    signals = {d[0]: [signal(d[0]), signal(d[0], "BBB")]}
    prices = [bar(d[0]), bar(d[1]), bar(d[2], 100, 104), bar(d[3], 105)]
    exposure = {d[0]: 100.0, d[1]: 100.0, d[2]: 50.0, d[3]: 50.0}
    result = run(prices, signals, p=p, extra=extra, exposure=exposure)
    sold = result["trades"][0]
    assert (sold["symbol"], sold["exit_date"], sold["exit_price"]) == ("BBB", d[3], 97)   # weaker at the d[2] close
    assert sold["reason"] == "Raise cash: market exposure 50%"
    assert [h["symbol"] for h in result["open_positions"]] == ["AAA"]
    held = run(prices, signals, p=replace(p, raise_cash=False), extra=extra, exposure=exposure)
    assert not held["trades"] and len(held["open_positions"]) == 2


def test_default_exposure_is_the_confirmed_uptrend_gate():
    d = days(3)
    result = run([bar(day) for day in d], states={d[0]: CORRECTION, d[1]: CONFIRMED, d[2]: CONFIRMED})
    assert not result["open_positions"]
    assert [point["exposure"] for point in result["equity_curve"]] == [0.0, 100.0, 100.0]


def test_buy_zone_window_and_breakout_validity():
    from stratlib.technical import buyable
    base = {"breakout_date": "2025-03-03", "position_pass": True, "volume_pass": True}
    zone = replace(T, buy_zone_entry_weeks=3)
    assert buyable(base, "2025-03-03", T) and not buyable(base, "2025-03-04", T)      # spec: breakout day only
    assert buyable(base, "2025-03-24", zone) and not buyable(base, "2025-03-25", zone)
    assert not buyable({**base, "position_pass": False}, "2025-03-04", zone)         # extended or back below pivot
    assert not buyable({**base, "volume_pass": None}, "2025-03-03", zone)
    assert not buyable({**base, "breakout_date": None}, "2025-03-03", zone)


def test_a_failed_breakout_is_not_bought_twice_and_holds_count_from_breakout():
    d = days(6)
    prices = [bar(d[0]), bar(d[1], 100, 100, low=92), bar(d[2], 101), bar(d[3], 101), bar(d[4], 101), bar(d[5], 101)]
    first = Signal("AAA", d[0], 100, 99, "Flat base", {}, breakout_date=d[0])
    again = Signal("AAA", d[3], 100, 99, "Flat base", {}, breakout_date=d[0])
    result = run(prices, {d[0]: [first], d[3]: [again]})
    assert [t["reason"] for t in result["trades"]] == ["Stop loss"]
    assert result["skipped_entries"][0]["reason"] == "Already bought this breakout"
    fresh = Signal("AAA", d[3], 100, 99, "Flat base", {}, breakout_date=d[3])
    assert run(prices, {d[0]: [first], d[3]: [fresh]})["open_positions"][0]["entry_date"] == d[4]


def test_fast_gain_window_uses_the_breakout_date_for_a_late_entry():
    d = days(25)
    t = replace(T, fast_gain_weeks=1, minimum_hold_weeks=2, profit_target_pct=20, fast_gain_pct=20)
    late = Signal("AAA", d[5], 100, 99, "Flat base", {}, breakout_date=d[0])    # bought a week after breakout
    prices = [bar(day, 101) for day in d[:7]] + [bar(day, 125) for day in d[7:]]
    trade = run(prices, {d[5]: [late]}, t=t)["trades"][0]
    # Bought at 101 on d[6]. The 20% gain on d[7] came after the one-week window from the
    # breakout (d[0]), so no hold: sell at the next open. Counting from the signal would hold.
    assert (trade["entry_date"], trade["fast_gain_date"], trade["exit_date"]) == (d[6], None, d[8])
    assert trade["reason"] == "Profit target, next open"


def leaders(prices, rankings, *, exposure=None, p=None, exit_below=None):
    from stratlib.backtest import simulate_leaders
    calendar = [b.date for b in next(iter(prices.values()))]
    p = p or BacktestSettings(initial_capital=1000, max_holdings=2, leaders_rank_buffer=2)
    return simulate_leaders(calendar, {**prices, "SPY": [bar(d) for d in calendar]}, dict.fromkeys(calendar, CONFIRMED),
                            lambda d: rankings.get(d, rankings.get("*", [])), T, p,
                            exposure=exposure or dict.fromkeys(calendar, 100.0), exit_below=exit_below)


def monthly_days():
    return ["2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04"]


def test_leaders_fill_from_the_top_and_hold_winners_without_a_target():
    d = monthly_days()
    prices = {s: [bar(day, 100 + 30 * i) for i, day in enumerate(d)] for s in ("AAA", "BBB", "CCC")}
    result = leaders(prices, {"*": ["AAA", "BBB", "CCC"]})
    assert sorted(h["symbol"] for h in result["open_positions"]) == ["AAA", "BBB"]    # two slots, best two
    assert not result["trades"]                                                        # +100% and still held


def test_leaders_rebalance_sells_dropouts_and_stops_wait_for_the_next_rebalance():
    d = monthly_days()          # three January sessions, then two in February
    prices = {"AAA": [bar(day) for day in d], "BBB": [bar(day) for day in d],
              "CCC": [bar(d[0]), bar(d[1], 100, 100, low=90), bar(d[2]), bar(d[3]), bar(d[4])]}
    ranks = {d[0]: ["CCC", "AAA"], d[1]: ["CCC", "AAA", "BBB"], d[2]: ["CCC", "BBB"], d[3]: ["CCC", "BBB"]}
    # A spare slot, so cash after the stop still covers a full equal-weight position.
    result = leaders(prices, ranks, p=BacktestSettings(initial_capital=1000, max_holdings=3, leaders_rank_buffer=3))
    exits = [(t["symbol"], t["exit_date"], t["reason"]) for t in result["trades"]]
    assert exits == [("CCC", d[1], "Stop loss"), ("AAA", d[3], "Rebalance: no longer passes the Screen")]
    entries = {(h["symbol"], h["entry_date"]) for h in result["open_positions"]}
    # Stopped CCC is skipped in January (BBB fills the slot) and rebought at February's rebalance.
    assert entries == {("BBB", d[2]), ("CCC", d[3])}


def test_leaders_raise_cash_sells_the_lowest_ranked_first():
    d = monthly_days()[:4]
    prices = {s: [bar(day) for day in d] for s in ("AAA", "BBB")}
    p = BacktestSettings(initial_capital=1000, max_holdings=2, leaders_rank_buffer=2, raise_cash=True)
    result = leaders(prices, {"*": ["AAA", "BBB"]}, exposure={d[0]: 100.0, d[1]: 100.0, d[2]: 50.0, d[3]: 50.0}, p=p)
    sold = result["trades"][0]
    assert (sold["symbol"], sold["exit_date"], sold["reason"]) == ("BBB", d[3], "Raise cash: market exposure 50%")


def test_backtests_can_switch_the_stop_loss_off():
    d = days(3)
    prices = [bar(d[0]), bar(d[1], 100, 100), bar(d[2], 100, 80, low=79)]
    assert run(prices)["trades"][0]["reason"] == "Stop loss"
    held = run(prices, p=replace(P, stop_loss=False))
    assert not held["trades"] and held["open_positions"][0]["last_close"] == 80
    kept = leaders({"AAA": prices}, {"*": ["AAA"]},
                   p=BacktestSettings(initial_capital=1000, max_holdings=1, leaders_rank_buffer=1, stop_loss=False))
    assert not kept["trades"]


def test_trend_leaders_sell_only_on_a_trend_break_or_the_loss_cap():
    d = monthly_days()          # three January sessions, then two in February
    prices = {"AAA": [bar(day) for day in d], "BBB": [bar(d[0]), bar(d[1]), bar(d[2], 100, 88, low=84), bar(d[3]), bar(d[4])],
              "CCC": [bar(day) for day in d]}
    p = BacktestSettings(initial_capital=900, max_holdings=3, leaders_rank_buffer=3, raise_cash=True,
                         trend_loss_cap_pct=15)
    # Only the first close ranks anyone; the market then allows nothing.
    exposure = {d[0]: 100.0, d[1]: 100.0, d[2]: 0.0, d[3]: 0.0, d[4]: 0.0}
    result = leaders(prices, {d[0]: ["AAA", "BBB", "CCC"]}, exposure=exposure, p=p, exit_below={"AAA": {d[1]}})
    exits = [(t["symbol"], t["exit_date"], t["reason"], t["exit_price"]) for t in result["trades"]]
    assert exits == [("AAA", d[2], "Closed below the 200-day line", 100), ("BBB", d[2], "Stop loss", 85)]
    # CCC left the ranking and the market fell to 0%, but neither a rebalance nor raise cash sells it.
    assert [h["symbol"] for h in result["open_positions"]] == ["CCC"]
    uncapped = leaders(prices, {d[0]: ["AAA", "BBB", "CCC"]}, exposure=exposure, exit_below={},
                       p=replace(p, trend_loss_cap_pct=0))
    assert not uncapped["trades"]


def test_trend_settings_are_validated():
    with pytest.raises(ConfigError):
        BacktestSettings(trend_min_rs=100)
    with pytest.raises(ConfigError):
        BacktestSettings(trend_loss_cap_pct=100)
    with pytest.raises(ConfigError):
        BacktestSettings(trend_min_dollar_volume_m=-1)
