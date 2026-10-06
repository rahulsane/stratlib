"""Causal signal timing and portfolio behavior for the D10 entry gates."""

import numpy as np
import pandas as pd
import pytest

from engine import Rules, simulate
from panel import Panel
from strategies.tt_checklist_trend import TrendGatedChecklist, prior_session_trend


def make_panel():
    dates = pd.bdate_range("2015-01-01", periods=270).strftime("%Y-%m-%d").to_numpy()
    close = np.repeat(np.linspace(90, 110, len(dates))[:, None], 13, axis=1)
    symbols = np.array([f"S{i}" for i in range(12)] + ["SPY"])
    return Panel(dates=dates, symbols=symbols, kind=np.array(["stock"] * 12 + ["etf"]),
                 until=np.array([""] * 13), open=close.copy(), high=close * 1.01,
                 low=close * .99, close=close, volume=np.full_like(close, 1e6),
                 factor=np.ones_like(close))


def setup(panel, passes, market=False, stock=False):
    panel.ret63[:] = np.arange(13) / 100
    strategy = TrendGatedChecklist()
    strategy.passes = passes
    strategy.setup(panel, {"stop_pct": 20.0, "equal_weight": True, "top_n": 10,
                           "trend_sessions": 200, "market_gate": market, "stock_gate": stock})
    return strategy


def run(panel, strategy):
    return simulate(panel, strategy, str(panel.dates[210]), None,
                    Rules(risk_pct=1, max_positions=10, max_position_pct=100 / 3))


def test_gate_uses_only_previous_session_and_requires_complete_history():
    close = np.arange(1, 241, dtype=float)[:, None]
    before = prior_session_trend(close)
    assert not before[2][:200].any()
    assert before[0][200, 0] == 200
    assert before[1][200, 0] == pytest.approx(100.5)
    assert before[2][200, 0]
    close[200:] = 1
    after = prior_session_trend(close)
    for a, b in zip(before, after):
        np.testing.assert_array_equal(a[:201], b[:201])
    close[100] = np.nan
    assert not prior_session_trend(close)[2][200, 0]
    assert not prior_session_trend(np.ones((240, 1)))[2].any()  # strict above


def test_stock_gate_does_not_backfill_and_preserves_cash_split_sizing():
    panel = make_panel()
    panel.close[209, 11] = 80
    strategy = setup(panel, {210: frozenset(range(12))}, stock=True)
    assert strategy.passes[210] == frozenset(range(2, 12))
    assert strategy.entry_passes[210] == frozenset(range(2, 11))
    result = run(panel, strategy)
    assert {t.ticker for t in result["trades"]} == {f"S{i}" for i in range(2, 11)}
    assert sum(t.position_value for t in result["trades"]) == pytest.approx(100_000)
    assert all(t.position_value == pytest.approx(100_000 / 9) for t in result["trades"])
    assert result["counts"]["skipped_no_slot"] == 0


def test_market_gate_blocks_entries_without_exiting_retained_holdings():
    panel = make_panel()
    panel.close[229, panel.index["SPY"]] = 80
    strategy = setup(panel, {210: frozenset(range(12)), 230: frozenset(range(12))}, market=True)
    # Stock 0 rises into the quarterly top ten, replacing 2. Its replacement
    # purchase is blocked, while the original rank-based exit still happens.
    panel.ret63[230, 0] = 1
    strategy.passes = strategy.screen_passes
    strategy.setup(panel, strategy.params)
    assert not strategy.entry_passes[230]
    result = run(panel, strategy)
    by = {t.ticker: t for t in result["trades"]}
    assert "S0" not in by
    assert by["S2"].exit_date == str(panel.dates[230])
    assert by["S2"].exit_reason == "fell outside top 10 at a rebalance"
    assert all(by[f"S{j}"].exit_reason == "end of test" for j in range(3, 12))
    assert len(by) == 10


def test_stock_gate_failure_does_not_exit_existing_position_or_move_stop():
    panel = make_panel()
    panel.close[229, 11] = 95  # below average, but above the existing 20% stop
    strategy = setup(panel, {210: frozenset(range(12)), 230: frozenset(range(12))}, stock=True)
    assert 11 not in strategy.entry_passes[230] and 11 in strategy.passes[230]
    result = run(panel, strategy)
    trade, = [t for t in result["trades"] if t.ticker == "S11"]
    assert trade.exit_reason == "end of test"
    assert trade.entry_date == str(panel.dates[210])
    assert trade.stop == pytest.approx(panel.close[210, 11] * .8)


def test_combined_gate_is_intersection_and_waits_until_next_quarter():
    panel = make_panel()
    panel.close[209, 12] = 80
    panel.close[229, 11] = 80
    passes = {210: frozenset(range(12)), 230: frozenset(range(12))}
    both = setup(panel, passes, market=True, stock=True)
    market = setup(panel, passes, market=True)
    stock = setup(panel, passes, stock=True)
    for t in passes:
        assert both.entry_passes[t] == market.entry_passes[t] & stock.entry_passes[t]
    result = run(panel, both)
    assert not result["held"][:20].any()
    assert all(t.entry_date == str(panel.dates[230]) for t in result["trades"])
    assert len(result["trades"]) == 9


def test_stop_exit_still_operates_between_rebalances():
    panel = make_panel()
    panel.low[220, 11] = 70
    strategy = setup(panel, {210: frozenset(range(12))}, market=True, stock=True)
    result = run(panel, strategy)
    trade, = [t for t in result["trades"] if t.ticker == "S11"]
    assert trade.exit_reason == "stop"
    assert trade.exit_date == str(panel.dates[220])
