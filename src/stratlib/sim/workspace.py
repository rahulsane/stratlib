"""The workspace strategies that have no research strategy class, on the shared engine, and the quarterly inputs of
the two statement-based strategies.

CANSLIM breakouts and Trend Leaders keep the rules of the app's earlier backtests (backtest.simulate and
simulate_leaders): the same signals, equal slots, the market's exposure ladder and the same exits. Nash's quality
screen rebalances quarterly into equal slots, as its live screen does. Like every strategy they trade under the
research ground rules (engine.py): slippage, the $5 and $20M liquidity floor on the signal day, stops filled
intraday, and SPY with dividends as the benchmark.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..fundamental_scans import (close_on_or_before, cyclical, latest_snapshot, load_statements, nash_clean_snapshots,
                                 nash_passes, nash_snapshots, quality_flags, quality_series, snapshot_index)
from ..sell_rules import below_trend_line
from .engine import Position, Strategy
from .panel import Panel


@dataclass(frozen=True)
class Breakout:
    """One session's buyable breakout: the panel column, pivot (split-adjusted), RS and the base's breakout date."""
    j: int
    symbol: str
    pivot: float
    rs: float
    breakout: str


def slots(max_holdings: int, exposure: float | None) -> int:
    """Holdings the market's allowed exposure permits, as in the app's backtests."""
    return math.floor(max_holdings * (exposure or 0) / 100 + 1e-9)


SLOT_TOLERANCE = 0.01


def equal_slot(max_holdings: int, fill: float, equity: float, available: float) -> float:
    """Shares for one equal slot, or none when the cash cannot fill it: the app's backtests never buy a part-sized
    position. Cash within 1% of a slot still buys it (the engine trims the order to the cash), since the engine sizes
    on the last close's equity where the app's backtests used the open's, and a sale before the buy pays slippage."""
    allocation = equity / max_holdings
    return allocation / fill if available + 1e-8 >= allocation * (1 - SLOT_TOLERANCE) and allocation > 0 else 0.0


class CanslimBreakouts(Strategy):
    """Buy a qualified breakout at the next open if it is within the buy zone; sell on the stop, at the open after a
    close at the profit target (unless the fast-gain hold applies), or to raise cash when exposure falls."""

    name = "canslim_breakouts"
    entry = "open"

    def __init__(self, signals: dict[int, list[Breakout]], exposure: np.ndarray):
        self.signals, self.exposure = signals, exposure       # by panel row: the close's breakouts and exposure

    def setup(self, panel: Panel, params: dict) -> None:
        super().setup(panel, params)
        self.info = {(t, b.j): b for t, items in self.signals.items() for b in items}
        self.bought: set[tuple[int, str]] = set()
        self.counts = {"open_above_buy_zone": 0}

    def candidates(self, t):
        return np.array([b.j for b in self.signals.get(t, []) if (b.j, b.breakout) not in self.bought], dtype=int)

    def order_key(self, j, t_signal):
        b = self.info[(t_signal, j)]
        return (-b.rs, b.symbol)

    def max_positions(self, t_signal):
        return slots(self.params["max_holdings"], self.exposure[t_signal])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        b = self.info[(t_signal, j)]
        if entry_price > b.pivot * (1 + self.params["buy_zone_max_pct"] / 100) + 1e-9:
            self.counts["open_above_buy_zone"] += 1
            return None
        return entry_price * (1 - self.params["stop_loss_pct"] / 100)

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        return equal_slot(self.params["max_holdings"], fill, equity, available)

    def on_entry(self, pos: Position, t):
        b = self.info[(pos.signal_day, pos.j)]
        self.bought.add((pos.j, b.breakout))
        pos.state.update(pivot=b.pivot, breakout=b.breakout, fast_gain=None)
        self._fast_gain(pos, pos.signal_day)       # the breakout close itself counts

    def _fast_gain(self, pos: Position, t: int):
        p = self.params
        day, breakout = str(self.panel.dates[t]), date.fromisoformat(pos.state["breakout"])
        until = (breakout + timedelta(weeks=p["fast_gain_weeks"])).isoformat()
        close = self.panel.close[t, pos.j]
        if (pos.state["fast_gain"] is None and pos.state["breakout"] <= day <= until and np.isfinite(close)
                and close + 1e-9 >= pos.state["pivot"] * (1 + p["fast_gain_pct"] / 100)):
            pos.state["fast_gain"] = day

    def on_close(self, pos: Position, t):
        self._fast_gain(pos, t)
        p, day = self.params, str(self.panel.dates[t])
        hold_until = (date.fromisoformat(pos.state["breakout"]) + timedelta(weeks=p["minimum_hold_weeks"])).isoformat()
        holding = pos.state["fast_gain"] is not None and day < hold_until
        if not holding and self.panel.close[t, pos.j] + 1e-9 >= pos.entry_price * (1 + p["profit_target_pct"] / 100):
            return ("open", "profit target")
        return None

    def reduce(self, t, positions):
        """Raise cash: above the limit the last close's exposure allows, sell the weakest since entry at the open."""
        capacity = slots(self.params["max_holdings"], self.exposure[t - 1])
        if not self.params["raise_cash"] or len(positions) <= capacity:
            return []
        close = self.panel.close_ff[t - 1]
        weakest = sorted(positions, key=lambda pos: (close[pos.j] / pos.entry_price, pos.symbol))
        reason = f"raise cash: market exposure {self.exposure[t - 1] or 0:g}%"
        return [(pos, reason) for pos in weakest[:len(positions) - capacity]]


class TrendLeaders(Strategy):
    """Buy the day's top-ranked trend leaders into the slots the market's exposure allows; sell at the open after a
    close below the long moving average, or on the loss cap. A sold stock is not rebought before the next month."""

    name = "trend_leaders"
    entry = "open"

    def __init__(self, ranking: dict[int, list[int]], exposure: np.ndarray):
        self.ranking, self.exposure = ranking, exposure       # by panel row: columns best first, and exposure

    def setup(self, panel: Panel, params: dict) -> None:
        super().setup(panel, params)
        self.rank = {(t, j): k for t, js in self.ranking.items() for k, j in enumerate(js)}
        self.lines: dict[int, np.ndarray] = {}
        self.stopped: set[int] = set()

    def candidates(self, t):
        dates = self.panel.dates
        if t + 1 < len(dates) and dates[t + 1][:7] != dates[t][:7]:
            self.stopped.clear()                       # the next open starts a new month
        return np.array([j for j in self.ranking.get(t, []) if j not in self.stopped], dtype=int)

    def order_key(self, j, t_signal):
        return self.rank[(t_signal, j)]

    def max_positions(self, t_signal):
        return slots(self.params["max_holdings"], self.exposure[t_signal])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        cap = self.params["loss_cap_pct"]
        return entry_price * (1 - cap / 100) if cap > 0 else 0.0

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        return equal_slot(self.params["max_holdings"], fill, equity, available)

    def line(self, j: int) -> np.ndarray:
        """The long moving average over the stock's own valid closes, as the app's trend exit measures it."""
        if j not in self.lines:
            close = self.panel.close[:, j]
            rows = np.flatnonzero(np.isfinite(close) & (close > 0))
            out = np.full(len(close), np.nan)
            out[rows] = pd.Series(close[rows]).rolling(self.params["long_ma_sessions"]).mean().to_numpy()
            self.lines[j] = out
        return self.lines[j]

    def on_close(self, pos: Position, t):
        if below_trend_line(float(self.panel.close[t, pos.j]), float(self.line(pos.j)[t])):
            return ("open", f"closed below the {self.params['long_ma_sessions']}-day line")
        return None

    def on_exit(self, pos: Position, t, reason):
        self.stopped.add(pos.j)


class NashQuality(Strategy):
    """Quarterly: keep holdings that still pass every rule, sell the rest, and fill the empty slots with the
    passers with the highest Rule of 40. Equal slots, no stop."""

    name = "nash_quality"
    entry = "close"

    def __init__(self, passers: dict[int, list[int]]):
        self.passers = passers                                # rebalance row -> passing columns, best first

    def setup(self, panel: Panel, params: dict) -> None:
        super().setup(panel, params)
        self.passing = {t: set(js) for t, js in self.passers.items()}
        self.rank = {(t, j): k for t, js in self.passers.items() for k, j in enumerate(js)}
        self.kept: dict[int, set[int]] = {}

    def on_close(self, pos: Position, t):
        if t in self.passing and t > pos.entry_day:
            if pos.j not in self.passing[t]:
                return ("close", "no longer passes at a quarterly rebalance")
            self.kept.setdefault(t, set()).add(pos.j)
        return None

    def candidates(self, t):
        if t not in self.passers:
            return np.zeros(0, dtype=int)
        kept = self.kept.get(t, set())
        fresh = [j for j in self.passers[t] if j not in kept]
        return np.array(fresh[:max(self.params["slots"] - len(kept), 0)], dtype=int)

    def order_key(self, j, t_signal):
        return self.rank[(t_signal, j)]

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return 0.0

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        return equity / self.params["slots"] / fill


# ----------------------------------------------------------------------------------------------
# Quarterly inputs: who passes at each rebalance, from statements public strictly before that session


def _statement_stocks(panel: Panel, rows: list[int], classes: dict, exclude, eligible: np.ndarray) -> list[int]:
    """Stock columns liquid on some rebalance session and not excluded by sector."""
    stock = np.flatnonzero(panel.kind == "stock")
    ever = stock[eligible[rows][:, stock].any(axis=0)] if rows else []
    return [int(j) for j in ever if not exclude(*classes.get(str(panel.symbols[j]), ("", "")))]


def checklist_passes(store, panel: Panel, p, rows: list[int], classes: dict,
                     eligible: np.ndarray | None = None) -> dict[int, frozenset]:
    """The Traveling Trader checklist's passers at each rebalance (research/tt_quality.py quality_rows), among the
    stocks liquid that session (``eligible``, the panel's floor by default)."""
    eligible = panel.eligible if eligible is None else eligible
    columns = _statement_stocks(panel, rows, classes, lambda sector, industry: sector == "Financial Services", eligible)
    series = {}
    for j in columns:
        doc = load_statements(store, str(panel.symbols[j]))
        series[j] = quality_series(doc) if doc else None
    passes = {}
    for i in rows:
        day, passing = str(panel.dates[i]), set()
        for j in columns:
            s = series[j]
            if s is None or not eligible[i, j]:
                continue
            k = snapshot_index(s, day, p.stale_days)
            if k is None:
                continue
            row = quality_flags(s, k, float(panel.close[i, j]), close_on_or_before(panel, j, s.dates[k]), p)
            if row["QUAL"] == 1:
                passing.add(j)
        passes[i] = frozenset(passing)
    return passes


def nash_passers(store, panel: Panel, p, rows: list[int], classes: dict,
                 eligible: np.ndarray | None = None) -> dict[int, list[int]]:
    """Nash's passers at each rebalance, best Rule of 40 first, as the live screen ranks them, among the stocks
    liquid that session (``eligible``, the panel's floor by default)."""
    eligible = panel.eligible if eligible is None else eligible
    columns = _statement_stocks(panel, rows, classes, lambda sector, industry: sector == "Financial Services" or (
        p.exclude_cyclicals and cyclical(sector, industry)), eligible)
    built = nash_clean_snapshots if p.data_guards else nash_snapshots
    snaps = {}
    for j in columns:
        doc = load_statements(store, str(panel.symbols[j]))
        snaps[j] = built(doc) if doc else None
    passers = {}
    for i in rows:
        day, passing = str(panel.dates[i]), []
        for j in columns:
            if snaps[j] is None or not eligible[i, j]:
                continue
            snap = latest_snapshot(snaps[j], day, p.stale_days)
            if snap is None or not nash_passes(snap[1], p):
                continue
            m = snap[1]
            if p.data_guards and m["rev0_usd"] < p.min_prior_revenue_m * 1e6:
                continue
            passing.append((j, m["r40"]))
        ranked = sorted(passing, key=lambda x: (-(x[1] if x[1] is not None else -9), str(panel.symbols[x[0]])))
        passers[i] = [j for j, _ in ranked]
    return passers
