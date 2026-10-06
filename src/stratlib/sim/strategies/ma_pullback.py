"""Rayner Teo's moving-average pullback ("MA bounce") on daily bars (research/run_ma_pullback.py). The rules were
fixed before any results, from his articles on tradingwithrayner.com (the report lists them).

Indicators, on split-adjusted daily bars: the 50- and 200-day EMAs of the close and the 20-day ATR (Wilder's
smoothing). The area of value is the 50-day EMA plus or minus params["band_atr"] (0.5) ATR, since he treats the
average as "an area, not a line".

Touches. One state machine per stock runs over the whole panel, so signals do not depend on the test period:
- A pullback starts on the first session whose low reaches the area (low <= EMA50 + band), once the stock has traded
  clear above it (a low above the area) since the last pullback or break.
- The pullback is a completed test, meaning the market respected the average, when a later high exceeds the swing
  high before it: the highest high since the stock last cleared the area (or since the previous test's new high).
- A close below the area (close < EMA50 - band) means the average was not respected: the count goes back to zero and
  restarts once a low clears the area again.
So the third touch is the third pullback since the count restarted, with two completed tests before it.

Entry: a signal at the close of session t, bought at the next open, when
- the stock is in its third pullback (exactly the third), which has neither closed below the area nor made a new
  high yet. params["touch_rule"] "at least" also takes later pullbacks (with params["touch"] = 1, every pullback;
  only for the index-ETF study's departures from the rule);
- params["trigger"] fires at t: "candle" (his preferred), a hammer or a bullish engulfing pattern as his candlestick
  guide defines them. Hammer: the close in the top quarter of the day's range and a lower shadow at least twice the
  body. Bullish engulfing: a down candle, then an up candle whose body covers it (opens at or below its close,
  closes at or above its open). "higher close" (his simplest version, a close above the previous close) is a
  robustness check. Session t or the one before it must reach the area;
- the trend filter holds at t, params["trend"]: "above" the close is above the 200-day EMA; "rising" also the
  200-day EMA is above its value params["slope_sessions"] (20) sessions earlier ("pointing higher");
- it is the first such session of this pullback (one entry per pullback).
Initial stop, params["stop"], with the signal session's ATR:
- "swing": params["swing_atr"] (1) ATR below the pullback's lowest low through the signal session;
- "entry": params["entry_atr"] (2) ATR below the entry price (the next open, before slippage).
Exit, params["exit"]:
- "target": a limit params["target_atr"] (0.25) ATR below the swing high before the pullback ("just before the
  recent swing high"); the stop stays where it is. A trade whose entry is at or above the target is skipped.
- "trail": sell at the next open after a close below the 50-day EMA ("only exit if this market closes below the
  50-period moving average"); the initial stop stays as a hard stop.
The engine buys at the next open, sells at the stop intraday (at the open on a gap) and at the target intraday (at
the open on a gap above it), stops before targets.
Universe: params["universe"], index members at the signal session (`MaPullback.members`, boolean sessions x columns
masks the runner fills: "sp900" = "sp500" | "sp400", or any key; "all" for every column), securities of
params["kind"] ("stock"; "etf" for the index-ETF study) within the panel's price floor.
Ranking, when more stocks signal than slots are free, params["rank"]: "mom12_1" (the return from 252 to 21 sessions
before the signal, highest first), "ret63" (63-session return), or "random" (an order fixed by params["seed"]).
params["entry_delay"]: act on each signal that many sessions later (robustness; prices still come from the signal
session, and the stock must still be eligible then).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..engine import Position, Strategy

DEFAULTS = {"ema_area": 50, "ema_trend": 200, "atr_sessions": 20, "band_atr": 0.5, "trend": "above",
            "slope_sessions": 20, "trigger": "candle", "stop": "swing", "swing_atr": 1.0, "entry_atr": 2.0,
            "exit": "target", "target_atr": 0.25, "touch": 3, "universe": "sp900", "rank": "mom12_1", "seed": 0,
            "entry_delay": 0, "kind": "stock", "touch_rule": "exact", "position_pct": None}
EXIT_REASON = "close below the 50-day EMA"

# Indicators and touch states per (panel, ema_area, ema_trend, atr_sessions, band_atr): about 3 s and 0.4 GB on the
# broad panel, shared by every variant that uses the same settings. The most recent CACHE_KEPT are kept.
_CACHE: dict = {}
CACHE_KEPT = 2


def ema(a: np.ndarray, span: int) -> np.ndarray:
    return pd.DataFrame(a).ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """Wilder's average true range; the previous close is the last valid one."""
    prev = pd.DataFrame(close).ffill(limit=5).shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))
    tr[~np.isfinite(high - low)] = np.nan
    return pd.DataFrame(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()


def touches(high: np.ndarray, low: np.ndarray, close: np.ndarray, area: np.ndarray, band: np.ndarray) -> dict:
    """The touch state machine (module docstring), for every column at once. Per session: `touch`, the number of the
    pullback live at the close (tests completed before it + 1; 0 outside a pullback), `ref` its swing high, `low`
    its lowest low so far, `pid` a running count of pullbacks per column (one entry per pullback)."""
    n, m = close.shape
    state = np.zeros(m, dtype=np.int8)       # 0 waiting to clear the area, 1 clear above it, 2 in a pullback
    tests = np.zeros(m, dtype=np.int16)
    swing = np.full(m, np.nan)
    ref = np.full(m, np.nan)
    pb_low = np.full(m, np.nan)
    pid = np.zeros(m, dtype=np.int32)
    out = {"touch": np.zeros((n, m), dtype=np.int8), "ref": np.full((n, m), np.nan, dtype=np.float32),
           "low": np.full((n, m), np.nan, dtype=np.float32), "pid": np.zeros((n, m), dtype=np.int32)}
    with np.errstate(invalid="ignore"):
        for t in range(n):
            h, lo, c = high[t], low[t], close[t]
            top, bottom = area[t] + band[t], area[t] - band[t]
            ok = np.isfinite(h) & np.isfinite(lo) & np.isfinite(c) & np.isfinite(top)
            near, clear, broken = ok & (lo <= top), ok & (lo > top), ok & (c < bottom)
            # 0 -> 1 when a low clears the area; the swing high is tracked while waiting too
            waiting = (state == 0) & ok
            swing[waiting] = np.fmax(swing[waiting], h[waiting])
            cleared = waiting & clear
            state[cleared] = 1
            # 1: a touch of the area starts a pullback
            was_clear = (state == 1) & ok & ~cleared
            start = was_clear & near
            stay = was_clear & ~near
            swing[stay] = np.fmax(swing[stay], h[stay])
            ref[start] = np.fmax(swing[start], h[start])
            pb_low[start] = lo[start]
            pid[start] += 1
            state[start] = 2
            # 2: a close below the area resets the count; a new high above the swing high completes a test
            live = (state == 2) & ok
            going = live & ~start
            pb_low[going] = np.fmin(pb_low[going], lo[going])
            brk = live & broken
            done = going & ~broken & (h > ref)
            state[brk] = 0
            tests[brk] = 0
            swing[brk] = np.nan
            state[done] = 0
            tests[done] += 1
            swing[done] = h[done]
            now = state == 2
            out["touch"][t, now] = np.minimum(tests[now] + 1, 127)
            out["ref"][t, now] = ref[now]
            out["low"][t, now] = pb_low[now]
            out["pid"][t] = pid
    return out


def candles(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> dict:
    po, pc = np.full_like(o, np.nan), np.full_like(c, np.nan)
    po[1:], pc[1:] = o[:-1], c[:-1]
    with np.errstate(invalid="ignore"):
        body = np.abs(c - o)
        rng = h - lo
        hammer = (rng > 0) & (np.fmin(o, c) - lo >= 2 * body) & (c - lo >= rng * 3 / 4)
        engulfing = (pc < po) & (c > o) & (o <= pc) & (c >= po)
        higher = c > pc
    return {"candle": hammer | engulfing, "higher close": higher, "hammer": hammer, "engulfing": engulfing}


def indicators(panel, q: dict) -> dict:
    key = (id(panel), q["ema_area"], q["ema_trend"], q["atr_sessions"], q["band_atr"])
    if key in _CACHE:
        _CACHE[key] = _CACHE.pop(key)     # the most recently used goes last
    else:
        while len(_CACHE) >= CACHE_KEPT:
            del _CACHE[next(iter(_CACHE))]
        p = panel
        e_area = ema(p.close, q["ema_area"]).astype(np.float32)
        e_trend = ema(p.close, q["ema_trend"]).astype(np.float32)
        a = atr(p.high, p.low, p.close, q["atr_sessions"]).astype(np.float32)
        band = q["band_atr"] * a
        with np.errstate(invalid="ignore"):
            near = p.low <= e_area + band
        _CACHE[key] = {"ema_area": e_area, "ema_trend": e_trend, "atr": a, "near": near,
                       **touches(p.high, p.low, p.close, e_area, band), **candles(p.open, p.high, p.low, p.close)}
    return _CACHE[key]


class MaPullback(Strategy):
    name = "ma_pullback"
    entry = "open"
    members: dict = {}   # "sp500" / "sp400" / "r3000" -> boolean (sessions x columns) membership

    def setup(self, panel, params):
        super().setup(panel, {**DEFAULTS, **params})
        q, p = self.params, panel
        ind = indicators(p, q)
        self.ind = ind
        c = p.close
        with np.errstate(invalid="ignore"):
            trend = c > ind["ema_trend"]
            if q["trend"] == "rising":
                k = q["slope_sessions"]
                lag = np.full_like(ind["ema_trend"], np.nan)
                lag[k:] = ind["ema_trend"][:-k]
                trend &= ind["ema_trend"] > lag
        prev_near = np.zeros_like(ind["near"])
        prev_near[1:] = ind["near"][:-1]
        touch = ind["touch"] == q["touch"] if q["touch_rule"] == "exact" else ind["touch"] >= q["touch"]
        raw = touch & ind[q["trigger"]] & (ind["near"] | prev_near) & trend
        # one entry per pullback: the first qualifying session
        first = np.zeros_like(raw)
        last_pid = np.zeros(raw.shape[1], dtype=np.int32)
        pid = ind["pid"]
        for t in np.flatnonzero(raw.any(axis=1)):
            js = np.flatnonzero(raw[t])
            new = pid[t, js] != last_pid[js]
            first[t, js[new]] = True
            last_pid[js[new]] = pid[t, js[new]]
        d = int(q["entry_delay"])
        if d:
            shifted = np.zeros_like(first)
            shifted[d:] = first[:-d]
            first = shifted
        self.delay = d
        self.signals = first
        self.signal_count = int(first.sum())
        mask = p.eligible & (p.kind == q["kind"])[None, :]
        u = q["universe"]
        if u == "sp900":
            mask &= self.members["sp500"] | self.members["sp400"]
        elif u != "all":
            mask &= self.members[u]
        self.eligible_mask = mask

    def candidates(self, t):
        return np.flatnonzero(self.signals[t])

    def order_key(self, j, t_signal):
        rank = self.params["rank"]
        if rank == "random":
            return float(np.random.default_rng((self.params["seed"], t_signal, j)).random())
        if rank == "mom12_1":
            c = self.panel.close_ff
            if t_signal < 252:
                return np.inf
            value = c[t_signal - 21, j] / c[t_signal - 252, j] - 1
            return -float(value) if np.isfinite(value) else np.inf
        return -float(np.nan_to_num(self.panel.ret63[t_signal, j], nan=-np.inf))

    def target_price(self, j, t_signal) -> float:
        s = t_signal - self.delay
        return float(self.ind["ref"][s, j] - self.params["target_atr"] * self.ind["atr"][s, j])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        q, s = self.params, t_signal - self.delay
        a = self.ind["atr"][s, j]
        if not np.isfinite(a):
            return None
        if q["stop"] == "swing":
            stop = float(self.ind["low"][s, j] - q["swing_atr"] * a)
        else:
            stop = float(entry_price - q["entry_atr"] * a)
        if q["exit"] == "target" and not self.target_price(j, t_signal) > entry_price:
            return None
        return stop if np.isfinite(stop) and stop < entry_price else None

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        """params["position_pct"]: a fixed share of equity per position instead of the 1% risk (robustness)."""
        pct = self.params["position_pct"]
        return None if pct is None else pct / 100 * equity / fill

    def initial_target(self, j, t_signal, entry_price):
        return self.target_price(j, t_signal) if self.params["exit"] == "target" else None

    def on_close(self, pos: Position, t):
        if self.params["exit"] != "trail":
            return None
        e, c = self.ind["ema_area"][t, pos.j], self.panel.close[t, pos.j]
        if np.isfinite(e) and np.isfinite(c) and c < e:
            return ("open", EXIT_REASON)
        return None
