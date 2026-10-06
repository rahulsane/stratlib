"""The app's own strategies on the shared engine (stratlib.sim.workspace), on small synthetic panels."""

import numpy as np
import pytest

from stratlib.sim.engine import Rules, set_up, simulate
from stratlib.sim.panel import Panel
from stratlib.sim import runs
from stratlib.sim.runs import json_safe, latest_results, trade_rows
from stratlib.store import Store
from stratlib.sim.workspace import Breakout, CanslimBreakouts, NashQuality, TrendLeaders, equal_slot

DAYS = 90
CANSLIM = {"max_holdings": 10, "raise_cash": True, "stop_loss_pct": 7.0, "profit_target_pct": 20.0,
           "fast_gain_pct": 20.0, "fast_gain_weeks": 3, "minimum_hold_weeks": 8, "buy_zone_max_pct": 5.0}


def dates(n=DAYS, start=(2016, 1)):
    """Weekday-like ISO dates, 20 sessions a month, so months and quarters turn predictably."""
    year, month = start
    out = []
    for i in range(n):
        out.append(f"{year:04d}-{month:02d}-{1 + i % 20:02d}")
        if i % 20 == 19:
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return np.array(out)


def panel(closes: dict[str, list[float]] | None = None, n=DAYS, start=(2016, 1)) -> Panel:
    """Flat $100 stocks (liquid: $100M a day) unless a path is given; opens equal the prior close, a 1% range."""
    symbols = ["SPY", *(closes or {})] or ["SPY"]
    close = np.full((n, len(symbols)), 100.0)
    for j, symbol in enumerate(symbols):
        if closes and symbol in closes:
            path = closes[symbol]
            close[:len(path), j] = path
            close[len(path):, j] = path[-1]
    open_ = np.vstack([close[:1], close[:-1]])
    return Panel(dates=dates(n, start), symbols=np.array(symbols), kind=np.array(["etf", *["stock"] * (len(symbols) - 1)]),
                 until=np.array([""] * len(symbols)), open=open_, high=np.maximum(open_, close) * 1.005,
                 low=np.minimum(open_, close) * 0.995, close=close, volume=np.full(close.shape, 1e6),
                 factor=np.ones(close.shape))


def run(p, strategy, params, *, start=None, max_positions=10):
    strategy = set_up(lambda: strategy, p, params)
    return simulate(p, strategy, start or str(p.dates[30]), None, Rules(max_positions=max_positions, max_position_pct=100.0))


def breakout(p, symbol, row, pivot=100.0, rs=95):
    return Breakout(int(p.index[symbol]), symbol, pivot, rs, str(p.dates[row]))


def test_a_breakout_is_bought_once_at_the_next_open_within_the_buy_zone_and_sized_by_its_slot():
    p = panel({"AAA": [100.0] * DAYS, "FAR": [100.0] * 40 + [110.0] * 50})
    signals = {40: [breakout(p, "AAA", 40), breakout(p, "FAR", 40)], 45: [breakout(p, "AAA", 40)]}
    result = run(p, CanslimBreakouts(signals, np.full(DAYS, 100.0)), CANSLIM)
    (trade,) = result["trades"]
    assert trade.ticker == "AAA" and trade.entry_date == str(p.dates[41])     # bought once; FAR opened 10% above
    assert trade.position_value == pytest.approx(10_000, rel=0.01)           # one slot of ten
    assert result["counts"]["skipped_stop_rule"] == 1



def test_a_slot_is_bought_whole_or_not_at_all():
    assert equal_slot(10, 50.0, 100_000, 10_000) == pytest.approx(200)
    assert equal_slot(10, 50.0, 100_000, 9_950) == pytest.approx(200)     # trimmed to the cash by the engine
    assert equal_slot(10, 50.0, 100_000, 9_800) == 0.0                    # the app's backtests skip, not shrink


def test_the_stop_profit_target_and_fast_gain_hold():
    # STOP falls 8% after entry; WIN closes 21% up two months after its breakout; FAST does so inside three weeks.
    stop = [100.0] * 42 + [92.0] * 48
    win = [100.0] * 80 + [121.0] * 10
    fast = [100.0] * 42 + [121.0] * 48
    p = panel({"STOP": stop, "WIN": win, "FAST": fast})
    signals = {40: [breakout(p, s, 40) for s in ("STOP", "WIN", "FAST")]}
    trades = {t.ticker: t for t in run(p, CanslimBreakouts(signals, np.full(DAYS, 100.0)), CANSLIM)["trades"]}
    assert trades["STOP"].exit_reason.startswith("stop") and trades["STOP"].exit_date == str(p.dates[42])
    assert trades["WIN"].exit_reason == "profit target" and trades["WIN"].exit_date == str(p.dates[81])
    # The fast gain holds the position for eight weeks from the 2016-03-01 breakout, so the target that met it at once
    # only sells after 2016-04-26: at the open after the first close past the hold.
    assert trades["FAST"].exit_reason == "profit target" and trades["FAST"].exit_date == "2016-05-02"


def test_exposure_limits_new_buys_and_raises_cash_from_the_weakest():
    p = panel({s: [100.0] * 50 + [100.0 + k] * 40 for k, s in enumerate(("A", "B", "C", "D"))})
    exposure = np.full(DAYS, 100.0)
    exposure[40] = 20.0                    # two of ten slots at the signal close
    exposure[60:] = 10.0                   # later only one
    signals = {40: [breakout(p, s, 40, rs=90 + k) for k, s in enumerate(("A", "B", "C", "D"))]}
    trades = run(p, CanslimBreakouts(signals, exposure), CANSLIM)["trades"]
    assert sorted(t.ticker for t in trades) == ["C", "D"]                  # the two highest RS
    sold = next(t for t in trades if t.exit_reason.startswith("raise cash"))
    assert sold.ticker == "C" and sold.exit_date == str(p.dates[61])         # the weaker since entry


def test_trend_leaders_sell_below_the_line_and_wait_for_the_next_month():
    long = 10
    line_break = [100.0] * 45 + [80.0] * 3 + [100.0] * 42
    p = panel({"LEAD": line_break, "NEXT": [100.0] * DAYS})
    ranking = {t: [int(p.index["LEAD"]), int(p.index["NEXT"])] for t in range(30, DAYS)}
    params = {"max_holdings": 1, "loss_cap_pct": 0.0, "long_ma_sessions": long}
    trades = run(p, TrendLeaders(ranking, np.full(DAYS, 100.0)), params, max_positions=1)["trades"]
    first = trades[0]
    assert first.ticker == "LEAD" and first.exit_date == str(p.dates[46])
    assert first.exit_reason == f"closed below the {long}-day line"
    # NEXT takes the slot at once; LEAD is not bought back before the next month (row 60 starts it).
    assert trades[1].ticker == "NEXT" and trades[1].entry_date == str(p.dates[46])
    assert all(t.ticker != "LEAD" or t.entry_date >= str(p.dates[60]) for t in trades[1:])


def test_nash_keeps_passers_sells_failures_and_fills_by_rule_of_40():
    p = panel({s: [100.0] * DAYS for s in ("A", "B", "C")}, n=150)
    j = {s: int(p.index[s]) for s in ("A", "B", "C")}
    first, second = 40, 100              # two quarterly rebalance rows
    passers = {first: [j["A"], j["B"]], second: [j["C"], j["B"]]}
    trades = run(p, NashQuality(passers), {"slots": 2}, max_positions=2)["trades"]
    held = {t.ticker: t for t in trades}
    assert held["A"].exit_date == str(p.dates[second]) and held["A"].exit_reason.startswith("no longer passes")
    assert held["B"].exit_reason == "end of test"                          # still passing: kept
    assert held["C"].entry_date == str(p.dates[second])
    assert held["B"].position_value == pytest.approx(50_000, rel=0.01)


def test_results_are_plain_json():
    assert json_safe({"a": np.float64(1.5), "b": float("nan"), "c": [np.int64(2), np.inf], "d": np.array([1.0])}) == \
        {"a": 1.5, "b": None, "c": [2, None], "d": [1.0]}


def saved_result(store, settings, monkeypatch, strategy_id="nash_quality", *, gain=1.2) -> dict:
    """A comparable backtest saved by run_strategy over a synthetic year, mid-2021 to mid-2022: SPY is flat and the one
    stock rises by ``gain``, held by Nash's quarterly rules whatever strategy the result is filed under."""
    n = 240
    p = panel({"AAA": list(np.linspace(100.0, 100.0 * gain, n))}, n=n, start=(2021, 7))
    store.save_document("research:spy_dividends", {"rows": {}})
    store.save_document("research:tbill3m", {"rows": {}})
    passers = {row: [int(p.index["AAA"])] for row in (60, 120, 180)}      # the first session of each later quarter
    monkeypatch.setattr(runs, "configure", lambda strategy_id, inputs: (
        lambda: NashQuality(passers), {"slots": 1}, Rules(max_positions=1, max_position_pct=100.0)))
    return runs.run_strategy(strategy_id, runs.Inputs(store, settings, str(p.dates[-1]), panel=p))


def test_a_saved_result_holds_every_period_and_reads_back_its_trades(settings, monkeypatch):
    store = Store(settings.data.db_path)
    try:
        doc = saved_result(store, settings, monkeypatch)
        assert latest_results(store) == {"nash_quality": doc}
        assert list(doc["results"]) == ["in_sample", "out_of_sample", "combined"]
        assert doc["universe"].startswith("US common stocks") and doc["data_through"] == "2022-06-20"
        combined = doc["results"]["combined"]
        assert combined["total_return"] > 0 and combined["spy"]["cagr"] == 0
        (trade,) = trade_rows(doc["trades"]["combined"])
        assert trade["ticker"] == "AAA" and trade["entry_date"] == "2021-10-01" and trade["exit_reason"] == "end of test"
        (late,) = trade_rows(doc["trades"]["out_of_sample"])                 # the second period starts afresh
        assert late["entry_date"] == "2022-01-01"
    finally:
        store.close()


def test_each_strategy_reads_its_own_liquidity_floor(settings):
    assert runs.floor("canslim", settings) == runs.floor("trend", settings) == runs.FLOOR
    p = settings.strategies["minervini"]
    assert runs.floor("minervini", settings) == (p.min_price, p.min_dollar_volume_m * 1e6)


def test_a_stricter_floor_narrows_what_a_strategy_may_buy():
    from dataclasses import replace
    p = panel({"BIG": [100.0] * DAYS, "THIN": [100.0] * DAYS})
    volume = p.volume.copy()
    volume[:, p.index["THIN"]] = 1e5                                      # $10M a day against BIG's $100M
    loose = replace(p, volume=volume, min_dollar_volume=5e6)
    strict = loose.liquid(5.0, 20e6)
    thin = int(loose.index["THIN"])
    assert loose.eligible[40, thin] and not strict[40, thin]
    signals = {40: [breakout(loose, "THIN", 40), breakout(loose, "BIG", 40, rs=90)]}
    strategy = set_up(lambda: CanslimBreakouts(signals, np.full(DAYS, 100.0)), loose, CANSLIM)
    strategy.eligible_mask = strict
    trades = simulate(loose, strategy, str(loose.dates[30]), None, Rules(max_positions=10, max_position_pct=100.0))["trades"]
    assert [t.ticker for t in trades] == ["BIG"]
