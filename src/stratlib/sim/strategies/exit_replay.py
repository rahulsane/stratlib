"""Replay recorded entries (signal, entry session, entry price, initial stop) with other exit rules.

Entries come from a finished test, period by period, so every exit rule sees the
same trades. The initial stop always stays in place as the floor.

Exit kinds (params["exit"]):
- time: initial stop only; sell at the close of the `days`-th session after entry.
- sma: initial stop; sell at the first close below the `n`-day SMA, from the entry day's close on.
- partial_day: sell `fraction` at the close of session `day` after entry and move the stop to the
  entry fill; from the next session, sell the rest at the first close below the `n`-day SMA.
  With `require_profit`, the sale (and the move to breakeven) happens only if that close is above the
  entry fill; otherwise the original stop stays and the whole position trails from the next session.
- time_then_sma: initial stop only until session `days` after entry; from that close on, sell at the
  first close below the `n`-day SMA.
- partial_r: a limit order sells `fraction` at entry + `r` x initial risk (from the session after
  entry; at the open if it gaps above). At that close the stop moves to the entry fill, and from
  that close on the rest is sold at the first close below the `n`-day SMA. Until the target is
  reached only the initial stop applies.
- partial_pct: as partial_r, with the target at entry + `pct`%; the stop moves to the entry fill only
  if `breakeven` is true.
- chandelier: stop = highest high since entry (entry day included) minus `mult` x the `atr`-day
  Wilder ATR, the distance fixed in price at entry (ATR as of the session before entry). Updated
  at each close, effective the next session, never below the initial stop.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..engine import Position, Rules, Strategy, simulate


@dataclass(frozen=True)
class Entry:
    j: int
    signal_day: int
    entry_day: int
    price: float  # before slippage
    stop: float
    note: str


def recorded_entries(panel, trades) -> list[Entry]:
    return [Entry(j=panel.index[t.ticker], signal_day=panel.day[t.signal_date], entry_day=panel.day[t.entry_date],
                  price=t.entry_price_raw, stop=t.stop, note=t.entry_note) for t in trades]


def entries_from_test(panel, make_strategy, params: dict, periods: dict, rules: Rules | None = None) -> dict:
    """Re-run a finished test (it is deterministic) and collect its entries for each period."""
    result = {}
    for period, (start, end) in periods.items():
        strategy = make_strategy()
        strategy.setup(panel, params)
        result[period] = recorded_entries(panel, simulate(panel, strategy, start, end, rules or Rules())["trades"])
    return result


def wilder_atr(panel, n: int) -> np.ndarray:
    prev = pd.DataFrame(panel.close).ffill(limit=5).shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        tr = np.fmax(panel.high - panel.low, np.fmax(np.abs(panel.high - prev), np.abs(panel.low - prev)))
    return pd.DataFrame(tr).ewm(alpha=1 / n, adjust=False, min_periods=n, ignore_na=True).mean().to_numpy()


class ExitRules(Strategy):
    """The exit rules above, for any strategy: call setup_exits() in setup(); params["exit"] holds the rule."""

    def setup_exits(self, panel, rule: dict) -> None:
        rolling = lambda n: pd.DataFrame(panel.close).rolling(n, min_periods=n).mean().to_numpy()  # noqa: E731
        self.sma = {rule["n"]: rolling(rule["n"])} if "n" in rule else {}
        self.atr = wilder_atr(panel, rule["atr"]) if rule["kind"] == "chandelier" else None

    def below_sma(self, pos: Position, t: int) -> bool:
        n = self.params["exit"]["n"]
        sma = self.sma[n][t, pos.j]
        return bool(np.isfinite(sma) and self.panel.close[t, pos.j] < sma)

    def on_close(self, pos: Position, t):
        return exit_decision(self, pos, t)


class ExitReplay(ExitRules):
    name = "exit_replay"
    entry = "scheduled"

    def __init__(self, entries_by_period: dict[str, list[Entry]]):
        self.entries_by_period = entries_by_period
        self.by_day, self.stops = {}, {}

    def setup(self, panel, params):
        super().setup(panel, params)
        self.setup_exits(panel, params["exit"])

    def begin_period(self, period: str) -> None:
        self.by_day, self.stops = defaultdict(list), {}
        for e in self.entries_by_period[period]:
            self.by_day[e.entry_day].append((e.j, e.signal_day, e.price, e.note))
            self.stops[(e.j, e.entry_day)] = e.stop

    def scheduled(self, t):
        return self.by_day.get(t, [])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return self.stops[(j, t_entry)]


def exit_decision(self: ExitRules, pos: Position, t: int):
    """The engine's on_close decision for params["exit"]."""
    rule = self.params["exit"]
    kind, age = rule["kind"], t - pos.entry_day
    if kind == "time":
        return ("close", f"time exit, day {rule['days']}") if age >= rule["days"] else None
    if kind == "sma":
        return ("close", f"close below {rule['n']}-day SMA") if self.below_sma(pos, t) else None
    if kind == "partial_day":
        if "partial_at" not in pos.state:
            if age >= rule["day"]:
                pos.state["partial_at"] = t
                if rule.get("require_profit") and not self.panel.close[t, pos.j] > pos.entry_fill:
                    return None  # below entry: no sale, original stop kept, trail from the next session
                pos.stop = max(pos.stop, pos.entry_fill)
                return ("partial", f"day-{rule['day']} sale of a third", rule["fraction"])
            return None
        if t > pos.state["partial_at"] and self.below_sma(pos, t):
            return ("close", f"close below {rule['n']}-day SMA")
        return None
    if kind == "time_then_sma":
        if age >= rule["days"] and self.below_sma(pos, t):
            return ("close", f"close below {rule['n']}-day SMA after day {rule['days']}")
        return None
    if kind == "partial_r":
        if "armed" not in pos.state:
            pos.state["armed"] = True
            pos.target = pos.entry_fill + rule["r"] * (pos.entry_fill - pos.initial_stop)
            pos.target_fraction, pos.target_label = rule["fraction"], f"half at +{rule['r']:g}R"
        if pos.partials and "breakeven" not in pos.state:
            pos.state["breakeven"] = True
            pos.stop = max(pos.stop, pos.entry_fill)
        if "breakeven" in pos.state and self.below_sma(pos, t):
            return ("close", f"close below {rule['n']}-day SMA")
        return None
    if kind == "partial_pct":
        if "armed" not in pos.state:
            pos.state["armed"] = True
            pos.target = pos.entry_fill * (1 + rule["pct"] / 100)
            pos.target_fraction, pos.target_label = rule["fraction"], f"half at +{rule['pct']:g}%"
        if pos.partials and "after_partial" not in pos.state:
            pos.state["after_partial"] = True
            if rule.get("breakeven"):
                pos.stop = max(pos.stop, pos.entry_fill)
        if "after_partial" in pos.state and self.below_sma(pos, t):
            return ("close", f"close below {rule['n']}-day SMA")
        return None
    if kind == "chandelier":
        if "distance" not in pos.state:
            atr = self.atr[:pos.entry_day, pos.j]
            valid = np.flatnonzero(np.isfinite(atr))
            pos.state["distance"] = rule["mult"] * atr[valid[-1]] if len(valid) else np.inf
        high = self.panel.high[t, pos.j]
        if np.isfinite(high):
            pos.state["highest"] = max(pos.state.get("highest", -np.inf), high)
        if "highest" in pos.state:
            pos.stop = max(pos.stop, pos.state["highest"] - pos.state["distance"])
        return None
    raise ValueError(f"unknown exit kind {kind}")
