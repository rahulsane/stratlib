"""Turtle simulator checks against the worked examples in "The Original Turtle Trading Rules" (2003).

Run: PYTHONPATH="src;research" .venv/Scripts/python -m pytest research/test_turtle.py -q
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pytest

import turtle_sim as ts

FREE = dict(slippage=0.0, slippage_low=0.0, cash_interest=False, borrow_fee=0.0, margin_spread=0.0)


@dataclass
class Bars:
    symbols: tuple
    days: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    traded: np.ndarray
    rate: np.ndarray


def bars(rows: list[tuple[float, float, float, float]], symbol: str = "X") -> Bars:
    """One market from (open, high, low, close) rows on consecutive weekdays starting on a Monday."""
    days, d = [], date(2024, 1, 1)
    while len(days) < len(rows):
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    a = np.array(rows, dtype=float)
    col = lambda k: a[:, k:k + 1]
    return Bars((symbol,), np.array(days), col(0), col(1), col(2), col(3), np.full((len(rows), 1), 50.0),
                np.zeros(len(rows)))


def test_true_range_and_n_match_the_heating_oil_table():
    # March 2003 heating oil, p. 14-15: high, low, close, true range, N
    table = [
        (0.7220, 0.7124, 0.7124, 0.0096, 0.0134), (0.7170, 0.7073, 0.7073, 0.0097, 0.0132),
        (0.7099, 0.6923, 0.6923, 0.0176, 0.0134), (0.6930, 0.6800, 0.6838, 0.0130, 0.0134),
        (0.6960, 0.6736, 0.6736, 0.0224, 0.0139), (0.6820, 0.6706, 0.6706, 0.0114, 0.0137),
        (0.6820, 0.6710, 0.6710, 0.0114, 0.0136), (0.6795, 0.6720, 0.6744, 0.0085, 0.0134),
        (0.6760, 0.6550, 0.6616, 0.0210, 0.0138), (0.6650, 0.6585, 0.6627, 0.0065, 0.0134),
        (0.6701, 0.6620, 0.6701, 0.0081, 0.0131), (0.6965, 0.6750, 0.6965, 0.0264, 0.0138),
        (0.7065, 0.6944, 0.6944, 0.0121, 0.0137), (0.7115, 0.6944, 0.7087, 0.0171, 0.0139),
        (0.7168, 0.7100, 0.7124, 0.0081, 0.0136), (0.7265, 0.7120, 0.7265, 0.0145, 0.0136),
        (0.7265, 0.7098, 0.7098, 0.0167, 0.0138), (0.7184, 0.7110, 0.7184, 0.0086, 0.0135),
        (0.7280, 0.7200, 0.7228, 0.0096, 0.0133), (0.7375, 0.7227, 0.7359, 0.0148, 0.0134),
        (0.7447, 0.7310, 0.7389, 0.0137, 0.0134), (0.7420, 0.7140, 0.7162, 0.0280, 0.0141),
    ]
    h, lo, c, tr_book, n_book = (np.array(x) for x in zip(*table))
    tr = ts.true_range(h, lo, c)
    assert np.allclose(tr[1:], tr_book[1:], atol=1e-9)
    n = n_book[0]
    for k in range(1, len(table)):
        n = (19 * n + tr[k]) / 20
        assert abs(n - n_book[k]) < 6e-5, (k, n, n_book[k])
    # The unit on 6 Dec 2002: N = 0.0141, $1,000,000 account, 42,000 dollars per point -> 16.88, so 16 contracts
    unit = 0.01 * 1_000_000 / (0.0141 * 42_000)
    assert int(unit * 100) / 100 == 16.88 and int(unit) == 16


def flat(n: int, close: float, half_range: float) -> list[tuple]:
    return [(close, close + half_range, close - half_range, close)] * n


def test_crude_oil_adds_and_stops():
    # p. 20-22: N = 1.20, 55-day breakout at 28.30; units at 28.30, 28.90, 29.50, 30.10, all stopped at 27.70
    rows = flat(60, 27.69, 0.60)                          # highs 28.29, so the breakout trigger is 28.30
    rows.append((28.00, 30.10, 27.95, 30.05))             # up day: open, low, high, close
    data = bars(rows)
    acct = ts.Account(ts.Config(system=2, capital=1_000_000, **FREE), data, ts.indicators(data))
    acct.run(59, 60, close_at_end=False)
    p = acct.positions[0]
    assert [round(u.fill, 2) for u in p.units] == [28.30, 28.90, 29.50, 30.10]
    assert [round(u.stop, 2) for u in p.units] == [27.70] * 4
    assert p.units[0].shares == pytest.approx(0.01 * 1_000_000 / 1.20)


def test_crude_oil_gap_on_the_fourth_unit():
    # p. 23: the market opens at 30.80; the fourth unit fills there with its stop at 28.40, the rest at 27.70
    rows = flat(60, 27.69, 0.60)
    rows.append((28.00, 29.50, 27.95, 29.40))
    rows.append((30.80, 30.90, 30.70, 30.85))
    data = bars(rows)
    acct = ts.Account(ts.Config(system=2, capital=1_000_000, **FREE), data, ts.indicators(data))
    acct.run(59, 61, close_at_end=False)
    p = acct.positions[0]
    assert [round(u.fill, 2) for u in p.units] == [28.30, 28.90, 29.50, 30.80]
    assert [round(u.stop, 2) for u in p.units] == [27.70, 27.70, 27.70, 28.40]


def test_short_side_mirrors_the_long_side():
    rows = flat(60, 30.00, 0.60)                          # lows 29.40, so the short trigger is 29.39
    rows.append((29.60, 29.65, 27.55, 27.60))             # down day: open, high, low, close
    data = bars(rows)
    acct = ts.Account(ts.Config(system=2, capital=1_000_000, **FREE), data, ts.indicators(data))
    acct.run(59, 60, close_at_end=False)
    p = acct.positions[0]
    assert p.d == -1
    assert [round(u.fill, 2) for u in p.units] == [29.39, 28.79, 28.19, 27.59]
    assert [round(u.stop, 2) for u in p.units] == [29.99] * 4
    assert acct.shares[0] == pytest.approx(-4 * 0.01 * 1_000_000 / 1.20)


def test_full_position_stopped_loses_five_n():
    # The four crude oil units stopped together at 27.70 lose 0.6 + 1.2 + 1.8 + 2.4 = 6.0 = 5N, i.e. 2.5R
    rows = flat(60, 27.69, 0.60)
    rows.append((28.00, 30.10, 27.95, 30.05))
    rows.append((29.00, 29.10, 27.20, 27.30))             # down day through the stops, above the 20-day exit
    data = bars(rows)
    acct = ts.Account(ts.Config(system=2, capital=1_000_000, **FREE), data, ts.indicators(data))
    out = acct.run(59, 61, close_at_end=False)
    assert not acct.positions
    t = out["trades"][0]
    assert t["units"] == 4 and t["exit_reason"] == "stop" and t["avg_exit"] == pytest.approx(27.70)
    assert t["r"] == pytest.approx(-2.5)
    assert out["equity"][-1] == pytest.approx(1_000_000 * (1 - 0.05))


def test_drawdown_rule_cuts_the_notional():
    # p. 17: $1,000,000 down 10% trades as $800,000; another 10% of that ($180,000 in all) as $640,000
    acct = ts.Account(ts.Config(), bars(flat(5, 10, 1)), None)
    acct.year_equity, acct.cuts = 1_000_000, 0
    expected = [(950_000, 1_000_000), (900_000, 800_000), (850_000, 800_000), (820_000, 640_000),
                (950_000, 640_000), (1_000_000, 1_000_000)]
    for equity, notional in expected:
        acct._drawdown_rule(equity)
        assert acct.notional == pytest.approx(notional), equity


def s1_scenario() -> Bars:
    rows = flat(60, 10.0, 0.5)                            # 20-day high 10.5: first breakout at 10.51
    c = 10.0
    for _ in range(10):                                   # rally to 15
        rows.append((c, c + 0.6, c - 0.1, c + 0.5))
        c += 0.5
    rows += flat(10, 15.0, 0.5)                           # 10-day low 14.5
    rows.append((15.0, 15.05, 13.9, 14.0))                # 10-day exit at 14.49: the breakout won
    rows += flat(25, 14.0, 0.5)                           # 20-day high 14.5, 55-day high 15.5
    rows.append((14.0, 15.0, 13.95, 14.9))                # crosses 14.51 only: skipped
    rows.append((14.9, 16.0, 14.85, 15.9))                # crosses the 55-day failsafe at 15.51
    return bars(rows)


def test_system1_skips_after_a_winner_and_takes_the_failsafe():
    data = s1_scenario()
    acct = ts.Account(ts.Config(system=1, capital=1_000_000, **FREE), data, ts.indicators(data))
    out = acct.run(59, len(data.days) - 1, close_at_end=False)
    first = out["trades"][0]
    assert first["entry"] == "20" and first["exit_reason"] == "exit" and first["pnl"] > 0
    assert len(out["trades"]) == 1
    p = acct.positions[0]
    assert p.kind == "failsafe 55" and p.entries[0][2] == pytest.approx(15.51)
    assert data.days[p.entry_t] == data.days[-1]


def test_system1_without_the_filter_takes_the_20_day_breakout():
    data = s1_scenario()
    acct = ts.Account(ts.Config(system=1, capital=1_000_000, s1_filter=False, **FREE), data, ts.indicators(data))
    acct.run(59, len(data.days) - 1, close_at_end=False)
    p = acct.positions[0]
    assert p.kind == "20" and p.entries[0][2] == pytest.approx(14.51)


def test_equity_change_equals_trade_profits_without_costs_or_interest():
    data = s1_scenario()
    out = ts.run(ts.Config(system=1, capital=1_000_000, **FREE), data, ts.indicators(data), 59, len(data.days) - 1)
    assert out["equity"][-1] - 1_000_000 == pytest.approx(sum(t["pnl"] for t in out["trades"]))


def test_unit_limits_across_correlated_markets():
    cfg = ts.Config()
    acct = ts.Account(cfg, Bars(("SPY", "QQQ", "GLD"), np.array(["2024-01-01"]), *(np.ones((1, 3)),) * 5,
                                np.zeros(1)), None)
    acct.positions = {0: ts.Position(0, 1, [ts.Unit(1, 1, 0)] * 4, 2, 0, "55", 1),
                      1: ts.Position(1, 1, [ts.Unit(1, 1, 0)] * 2, 2, 0, "55", 1)}
    assert not acct._room(0, 1)            # 4 units in one market
    assert not acct._room(1, 1)            # 6 units across SPY and QQQ
    assert acct._room(1, -1) and acct._room(2, 1)
    acct.positions[2] = ts.Position(2, 1, [ts.Unit(1, 1, 0)] * 4, 2, 0, "55", 1)
    acct.positions[3] = ts.Position(2, 1, [ts.Unit(1, 1, 0)] * 2, 2, 0, "55", 1)
    assert not acct._room(2, 1)            # 12 long units in all


def test_an_account_wiped_out_stops_trading():
    rows = flat(60, 27.69, 0.60)
    rows.append((28.00, 30.10, 27.95, 30.05))             # four units at 2% per N
    rows.append((1.00, 1.10, 0.90, 1.00))                 # gaps through every stop: equity below zero
    rows.append((1.00, 40.00, 1.00, 40.00))               # a later breakout is not traded
    data = bars(rows)
    out = ts.run(ts.Config(system=2, capital=1_000_000, risk_per_n=0.02, **FREE), data, ts.indicators(data), 59, 62)
    assert out["ruined"] == data.days[61]
    assert out["equity"][1] < 0 and out["equity"][2] == out["equity"][1]
    # the gap stops the long at the open and opens a short through the 55-day low; ruin closes that short
    assert [(t["side"], t["exit_reason"]) for t in out["trades"]] == [("long", "stop (gap)"), ("short", "ruin")]
