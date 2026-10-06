"""Nick Radge's Weekend Trend Trader (Weekend Trend Trader, 2012; research/run_wtt.py).

Weekly bars: a week's close is the close of its last session. Decisions are made at that close and
traded at the next session's open.
Entry: the index (SPY) closes above the mean of its last 10 weekly closes, the stock's weekly close is
at least the highest of the previous 20 weekly closes, and its 20-week rate of change is at least 30%.
Only common stocks (Radge's universe is the Russell 3000), within the engine's liquidity floor.
Size: params["position_pct"] of equity per position (5%, so at most 20 positions).
Stop: checked on weekly closes only, never intraday. It trails the highest weekly close since the
signal week by params["stop_up_pct"] (40%) while the index is above its average and by
params["stop_down_pct"] (10%) once it closes below. A close under the stop sells at the next open.
params["stop_rule"]:
  "ratchet"    the stop never moves down; after a week below the average it stays at the 10% level
               until 40% below a new high climbs past it (the rules as described).
  "recompute"  the stop is recomputed from the highest close with the current week's percentage, so it
               loosens back to 40% when the index recovers (how some coded versions behave).
The engine's own intraday and gap stops are switched off (pos.stop = -inf); the initial stop passed to
the engine, 40% below the signal close, only sets 1R for the trade statistics.
params["universe"]: "all" common stocks, or "us" without the companies in `WeekendTrend.foreign` (the
runner fills it from FMP's country field; the Russell 3000 holds US companies only), or index members at
the signal week's close: "sp900" (S&P 500 or MidCap 400) or any key of `WeekendTrend.members` ("sp500",
"sp400", "r3000"), boolean (sessions x columns) masks the runner fills (research/wtt_universe.py,
research/wtt_russell.py).
Positions are kept after a stock leaves its index; only entries need membership.
params["rank"], for more signals than free slots (the book gives no rule): "ret63", the ground rules'
63-session return; "roc", the 20-week rate of change; "mom12_1", the return from 252 to 21 sessions before the
signal (12-month momentum skipping the latest month); "random", a random order fixed by params["seed"].
params["entry_delay_weeks"]: act on each week's signals that many weeks later (a robustness check; the stock must
still be eligible then).
"""

from __future__ import annotations

import numpy as np

from ..engine import Position, Strategy

DEFAULTS = {"index": "SPY", "index_weeks": 10, "high_weeks": 20, "roc_weeks": 20, "roc_pct": 30.0,
            "stop_up_pct": 40.0, "stop_down_pct": 10.0, "position_pct": 5.0, "stop_rule": "ratchet",
            "universe": "all", "rank": "ret63", "seed": 0, "entry_delay_weeks": 0}


def week_ends(dates: np.ndarray) -> np.ndarray:
    """Sessions that close a Monday-to-Sunday week. The panel's last session counts only on a Friday."""
    days = np.array(dates, dtype="datetime64[D]").astype(np.int64)
    week = (days + 3) // 7  # 1970-01-01 was a Thursday; +3 starts weeks on Monday
    ends = np.zeros(len(days), dtype=bool)
    ends[:-1] = week[:-1] != week[1:]
    ends[-1] = (days[-1] + 3) % 7 == 4
    return ends


class WeekendTrend(Strategy):
    name = "weekend_trend_trader"
    entry = "open"
    foreign: frozenset = frozenset()  # symbols of non-US companies, left out when params["universe"] == "us"
    members: dict = {}  # "sp500" / "sp400" -> boolean (sessions x columns) membership, for the index universes

    def setup(self, panel, params):
        super().setup(panel, {**DEFAULTS, **params})
        q, p = self.params, panel
        self.week_end = week_ends(p.dates)
        rows = np.flatnonzero(self.week_end)
        weekly = p.close_ff[rows]
        k = np.arange(len(rows))

        index = weekly[:, p.index[q["index"]]]
        n = q["index_weeks"]
        sma = np.full(len(rows), np.nan)
        sma[n - 1:] = np.convolve(index, np.ones(n) / n, mode="valid")
        up = np.zeros(len(p.dates), dtype=bool)
        up[rows] = index > sma
        self.market_up = up

        h, r = q["high_weeks"], q["roc_weeks"]
        with np.errstate(invalid="ignore", divide="ignore"):
            prior = np.full_like(weekly, np.nan)
            for i in k[h:]:
                window = weekly[i - h:i]
                prior[i] = np.where(np.isfinite(window).all(axis=0), window.max(axis=0), np.nan)
            base = np.full_like(weekly, np.nan)
            base[r:] = weekly[:-r]
            roc = 100 * (weekly / base - 1)
            signal = (weekly >= prior) & (roc >= q["roc_pct"]) & up[rows][:, None]
        delay = int(q["entry_delay_weeks"])
        acts = {int(rows[i]): int(rows[i + delay]) for i in range(len(rows) - delay)}   # signal week -> action week
        self.signals = {acts[int(t)]: np.flatnonzero(signal[i]) for i, t in enumerate(rows)
                        if signal[i].any() and int(t) in acts}
        self.roc = {acts[t]: dict(zip(js.tolist(), roc[i, js].tolist())) for i, t in enumerate(rows.tolist())
                    if t in acts and (js := self.signals.get(acts[t])) is not None}
        stocks = p.kind == "stock"
        if q["universe"] == "us":
            stocks &= ~np.isin(p.symbols, sorted(self.foreign))
        self.eligible_mask = p.eligible & stocks[None, :]
        if q["universe"] == "sp900":
            self.eligible_mask &= self.members["sp500"] | self.members["sp400"]
        elif q["universe"] not in ("all", "us"):
            self.eligible_mask &= self.members[q["universe"]]
        self.events = []  # (session, column, event) for the report: "tightened", "loosened"

    def candidates(self, t):
        return self.signals.get(t, np.zeros(0, dtype=int))

    def order_key(self, j, t_signal):
        """Competing orders are taken in ascending key order."""
        rank = self.params["rank"]
        if rank == "random":
            return float(np.random.default_rng((self.params["seed"], t_signal, j)).random())
        if rank == "roc":
            return -self.roc[t_signal][j]
        if rank == "mom12_1":
            c = self.panel.close_ff
            if t_signal < 252:
                return np.inf
            value = c[t_signal - 21, j] / c[t_signal - 252, j] - 1
            return -float(value) if np.isfinite(value) else np.inf
        return -float(np.nan_to_num(self.panel.ret63[t_signal, j], nan=-np.inf))

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return float(self.panel.close[t_signal, j] * (1 - self.params["stop_up_pct"] / 100))

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        return self.params["position_pct"] / 100 * equity / fill

    def on_entry(self, pos: Position, t):
        signal_close = float(self.panel.close[pos.signal_day, pos.j])
        pos.state.update(high=signal_close, stop=pos.initial_stop, level="40%", tight=False)
        pos.stop = -np.inf

    def on_close(self, pos: Position, t):
        if not self.week_end[t]:
            return None
        q, s = self.params, pos.state
        close = float(self.panel.close[t, pos.j])
        s["high"] = max(s["high"], close)
        up = bool(self.market_up[t])
        pct = q["stop_up_pct"] if up else q["stop_down_pct"]
        level = f"{pct:g}%"
        candidate = s["high"] * (1 - pct / 100)
        if not up and not s["tight"]:
            self.events.append((t, pos.j, "tightened"))
        s["tight"] = not up
        if q["stop_rule"] == "ratchet":
            if candidate > s["stop"]:
                s["stop"], s["level"] = candidate, level
        else:
            if candidate < s["stop"]:
                self.events.append((t, pos.j, "loosened"))
            s["stop"], s["level"] = candidate, level
        if close < s["stop"]:
            return ("open", f"weekly close under the {s['level']} stop")
        return None
