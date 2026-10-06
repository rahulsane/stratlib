"""The Turtle Trading System on daily bars, as written in "The Original Turtle Trading Rules" (2003).

Rules (page numbers are the PDF's):
- N (p. 13): Wilder-style 20-day average of the true range, N = (19 x previous N + TR) / 20, seeded with the
  simple average of the first 20 true ranges. The unit sheet is refreshed each Monday (p. 15): units, stops
  and add-on spacing use the N at the previous close.
- Unit (p. 14): 1% of the notional account / N shares, so a 1N move in one unit is 1% of the account.
- Limits (p. 16): 4 units per market; 6 in one direction across closely correlated markets; 10 across loosely
  correlated ones; 12 in one direction overall.
- Notional account (p. 17): reset to equity each January; cut by 20% each time equity falls 10% of the current
  notional (1.0 -> 0.8 at -10%, -> 0.64 at -18%, ...), restored when equity regains the year's starting value.
- System 1 (p. 19): 20-day breakout by one tick; skipped if the last 20-day breakout, taken or not, would have
  won. That breakout is followed as a one-unit trade with a 2N stop and the 10-day exit; it won if it exited
  above its entry (below for a short). A skipped breakout leaves the 55-day breakout as the failsafe entry.
  While the last breakout is still open its outcome is unknown, so a flat account waits for the 55-day one.
- System 2 (p. 19): 55-day breakout by one tick, every signal.
- Adds (p. 19): one unit every 1/2 N beyond the previous fill, up to the limits.
- Stops (p. 22): 2N from each unit's fill; every add raises the earlier units' stops by 1/2 N.
- Exits (p. 26): System 1 on a 10-day breakout against the position, System 2 on a 20-day one.

Fills on daily bars: an order fills at its trigger, or at the open when the market opens beyond it. Within a
session the price is assumed to run open, low, high, close on an up day (close >= open) and open, high, low,
close on a down day; orders fire in the order the path reaches them, and orders created during the day can
fire later the same day. Slippage follows the research harness: 0.10% per side, 0.25% when the as-traded
price is under $20.

Money: idle cash and short-sale proceeds earn the 3-month T-bill yield; borrowed cash pays the T-bill yield
plus `margin_spread`; shorts pay `borrow_fee` a year. Prices are dividend-adjusted, so longs earn the
distributions and shorts pay them. `max_gross` caps gross exposure (long plus short value) at a multiple of the
previous close's equity: None is futures-style leverage, as the Turtles had; units that do not fit shrink to
the room left, and are skipped below `min_partial` of a unit. An account whose equity reaches zero at a close is
liquidated there and stops trading (a margin call).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

TICK = 0.01
EPS = 1e-7       # a bar that touches a trigger fills it, despite floating-point error in the levels
CUT_STEP, CUT_SIZE, MAX_CUTS = 0.10, 0.20, 60


@dataclass(frozen=True)
class Config:
    system: int = 2
    capital: float = 100_000.0
    risk_per_n: float = 0.01
    max_gross: float | None = None
    slippage: float = 0.001
    slippage_low: float = 0.0025
    low_price: float = 20.0
    borrow_fee: float = 0.005
    margin_spread: float = 0.005
    cash_interest: bool = True
    drawdown_rule: bool = True
    s1_filter: bool = True
    long_only: bool = False
    stop_n: float = 2.0
    add_n: float = 0.5
    max_units_market: int = 4
    max_units_close: int = 6
    max_units_loose: int = 10
    max_units_direction: int = 12
    close_groups: tuple[tuple[str, ...], ...] = (("SPY", "QQQ"), ("GLD", "SLV"))
    loose_groups: tuple[tuple[str, ...], ...] = ()
    min_partial: float = 0.1

    @property
    def entry_days(self) -> int:
        return 20 if self.system == 1 else 55

    @property
    def exit_days(self) -> int:
        return 10 if self.system == 1 else 20


def true_range(h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = np.full(c.shape, np.nan)
    tr[1:] = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(c[:-1] - lo[1:])])
    return tr


def wilder(tr: np.ndarray, period: int = 20) -> np.ndarray:
    n = np.full(tr.shape, np.nan)
    if len(tr) <= period:
        return n
    n[period] = np.mean(tr[1:period + 1], axis=0)
    for t in range(period + 1, len(tr)):
        n[t] = ((period - 1) * n[t - 1] + tr[t]) / period
    return n


def channel(x: np.ndarray, k: int, high: bool) -> np.ndarray:
    """Highest high (or lowest low) of the k sessions before each session."""
    out = np.full(x.shape, np.nan)
    if len(x) > k:
        w = sliding_window_view(x, k, axis=0)
        w = w.max(axis=-1) if high else w.min(axis=-1)
        out[k:] = w[:len(x) - k]
    return out


@dataclass
class Indicators:
    n: np.ndarray
    hi: dict[int, np.ndarray]
    lo: dict[int, np.ndarray]
    strength: np.ndarray      # (close 63 sessions back to the previous close) / N, known before the open


def indicators(data) -> Indicators:
    tr = true_range(data.high, data.low, data.close)
    n = wilder(tr)
    hi = {k: channel(data.high, k, True) for k in (10, 20, 55)}
    lo = {k: channel(data.low, k, False) for k in (10, 20, 55)}
    strength = np.full(data.close.shape, np.nan)
    strength[64:] = (data.close[63:-1] - data.close[:-64]) / n[63:-1]
    return Indicators(n, hi, lo, strength)


def first_ready(ind: Indicators) -> int:
    ok = np.isfinite(ind.n).all(1) & np.isfinite(ind.hi[55]).all(1)
    return int(np.flatnonzero(ok)[0])


@dataclass
class Unit:
    shares: float
    fill: float
    stop: float


@dataclass
class Position:
    j: int
    d: int
    units: list[Unit]
    next_add: float
    entry_t: int
    kind: str
    risk0: float
    entries: list = field(default_factory=list)   # (t, shares, fill)
    exits: list = field(default_factory=list)     # (t, shares, fill, reason)
    cost: float = 0.0
    peak_units: int = 1


class Account:
    def __init__(self, cfg: Config, data, ind: Indicators):
        self.cfg, self.data, self.ind = cfg, data, ind
        self.m = len(data.symbols)
        self.close_groups = [[data.symbols.index(s) for s in g if s in data.symbols] for g in cfg.close_groups]
        self.loose_groups = [[data.symbols.index(s) for s in g if s in data.symbols] for g in cfg.loose_groups]

    # ----- set-up and the daily loop -----

    def run(self, t0: int, t1: int, t_warm: int | None = None, close_at_end: bool = True) -> dict:
        """Trade sessions t0+1..t1 from capital at t0's close. The System 1 filter is followed from t_warm."""
        cfg, d = self.cfg, self.data
        t_warm = first_ready(self.ind) if t_warm is None else t_warm
        t_warm = min(t_warm, t0)
        self.cash, self.shares = cfg.capital, np.zeros(self.m)
        self.positions: dict[int, Position] = {}
        self.hypo: dict[int, tuple[int, float, float]] = {}
        self.last_win: dict[int, bool] = {}
        self.trades: list[dict] = []
        self.n_sheet = self.ind.n[t_warm - 1].copy()
        self.equity_prev = cfg.capital
        self.year_equity, self.cuts, self.notional = cfg.capital, 0, cfg.capital
        self.flows = {"interest": 0.0, "financing": 0.0, "borrow": 0.0, "slippage": 0.0}
        self.blocked = {"entry: limits": 0, "entry: gross cap": 0, "add: limits": 0, "add: gross cap": 0}
        rec = {k: [] for k in ("date", "equity", "gross", "net", "units_long", "units_short", "positions",
                               "notional", "cuts", "rate")}
        self.trading = False
        self.ruined = None
        for t in range(t_warm, t1 + 1):
            if t > t_warm and date.fromisoformat(d.days[t]).isocalendar()[:2] != \
                    date.fromisoformat(d.days[t - 1]).isocalendar()[:2]:
                self.n_sheet = self.ind.n[t - 1].copy()
            if t <= t0:
                for j in range(self.m):
                    self._market_day(t, j)
                continue
            if self.ruined:
                for k in rec:
                    rec[k].append(d.days[t] if k == "date" else float(d.rate[t]) if k == "rate" else
                                  rec[k][-1] if k == "equity" else 0)
                continue
            self.trading = True
            self._accrue(t)
            if t == t0 + 1 or d.days[t][:4] != d.days[t - 1][:4]:
                self.year_equity, self.cuts, self.notional = self.equity_prev, 0, self.equity_prev
            self.last_px = d.close[t - 1].copy()
            strength = self.ind.strength[t]
            for j in sorted(range(self.m), key=lambda j: -abs(strength[j]) if np.isfinite(strength[j]) else 0):
                self._market_day(t, j)
            if t == t1 and close_at_end:
                for j in list(self.positions):
                    self._exit_all(t, j, d.close[t, j], "end of test")
            eq = self.cash + float(self.shares @ d.close[t])
            if eq <= 0:          # wiped out: a margin call closes everything and the account stops
                for j in list(self.positions):
                    self._exit_all(t, j, d.close[t, j], "ruin")
                eq = self.cash
                self.ruined = d.days[t]
            self._drawdown_rule(eq)
            self.equity_prev = eq
            val = self.shares * d.close[t]
            rec["date"].append(d.days[t])
            rec["equity"].append(eq)
            rec["gross"].append(float(np.abs(val).sum()))
            rec["net"].append(float(val.sum()))
            rec["units_long"].append(sum(len(p.units) for p in self.positions.values() if p.d > 0))
            rec["units_short"].append(sum(len(p.units) for p in self.positions.values() if p.d < 0))
            rec["positions"].append(len(self.positions))
            rec["notional"].append(self.notional)
            rec["cuts"].append(self.cuts)
            rec["rate"].append(float(d.rate[t]))
        out = {k: np.array(v) for k, v in rec.items()}
        out.update(start=d.days[t0], start_equity=cfg.capital, trades=self.trades, flows=dict(self.flows),
                   blocked=dict(self.blocked), ruined=self.ruined)
        return out

    def _accrue(self, t: int) -> None:
        cfg, d = self.cfg, self.data
        days = (date.fromisoformat(d.days[t]) - date.fromisoformat(d.days[t - 1])).days
        rate = d.rate[t - 1] / 100
        if self.cash >= 0:
            x = self.cash * rate * days / 365 if cfg.cash_interest else 0.0
            self.flows["interest"] += x
        else:
            x = self.cash * (rate + cfg.margin_spread) * days / 365
            self.flows["financing"] += x
        short_value = float(np.clip(-self.shares, 0, None) @ d.close[t - 1])
        fee = short_value * cfg.borrow_fee * days / 365
        self.flows["borrow"] -= fee
        self.cash += x - fee

    def _drawdown_rule(self, eq: float) -> None:
        if not self.cfg.drawdown_rule:
            return
        y = self.year_equity
        if eq >= y:
            self.cuts = 0
        else:
            # cut k is reached after losing 10% of each successive notional: 10%, 18%, 24.4%, ... The thresholds
            # approach a 50% loss, so a gap below that leaves the notional near zero until January.
            while self.cuts < MAX_CUTS and eq <= y * (1 - CUT_STEP / CUT_SIZE * (1 - (1 - CUT_SIZE) ** (self.cuts + 1))):
                self.cuts += 1
        self.notional = y * (1 - CUT_SIZE) ** self.cuts

    # ----- one market, one session -----

    def _active(self, t: int, j: int) -> list[tuple]:
        """Live orders: (price, side, priority, kind, arg). Side +1 fires when the price rises to the level, -1 when
        it falls to it. At one price, exits fire before entries and adds, and those before the filter's bookkeeping."""
        cfg, ind = self.cfg, self.ind
        out = []
        if self.trading:
            p = self.positions.get(j)
            if p is not None:
                k = cfg.exit_days
                level = ind.lo[k][t, j] - TICK if p.d > 0 else ind.hi[k][t, j] + TICK
                for i, u in enumerate(p.units):        # listed first: a gap through both counts as a stop
                    out.append((u.stop, -p.d, 0, "stop", i))
                out.append((level, -p.d, 0, "exit", None))
                if len(p.units) < cfg.max_units_market:
                    out.append((p.next_add, p.d, 1, "add", None))
            else:
                k, kind = cfg.entry_days, str(cfg.entry_days)
                if cfg.system == 1 and cfg.s1_filter and (j in self.hypo or self.last_win.get(j)):
                    k, kind = 55, "failsafe 55"
                out.append((ind.hi[k][t, j] + TICK, 1, 1, "entry", kind))
                if not cfg.long_only:
                    out.append((ind.lo[k][t, j] - TICK, -1, 1, "entry", kind))
        if cfg.system == 1 and cfg.s1_filter:
            h = self.hypo.get(j)
            if h is None:
                out.append((ind.hi[20][t, j] + TICK, 1, 2, "hypo_start", None))
                out.append((ind.lo[20][t, j] - TICK, -1, 2, "hypo_start", None))
            else:
                hd, _, stop = h
                level = max(stop, ind.lo[10][t, j] - TICK) if hd > 0 else min(stop, ind.hi[10][t, j] + TICK)
                out.append((level, -hd, 0, "hypo_end", None))
        return out

    def _market_day(self, t: int, j: int) -> None:
        d = self.data
        o, h, lo, c = d.open[t, j], d.high[t, j], d.low[t, j], d.close[t, j]
        blocked: set = set()
        steps = 0

        def live():
            nonlocal steps
            steps += 1
            if steps > 200:
                raise RuntimeError(f"order loop at {d.days[t]} {d.symbols[j]}")
            return [x for x in self._active(t, j) if (x[3], x[1]) not in blocked]

        while True:      # orders the open has already passed fill at the open
            hit = [x for x in live() if (x[1] > 0 and x[0] <= o + EPS) or (x[1] < 0 and x[0] >= o - EPS)]
            if not hit:
                break
            x = min(hit, key=lambda x: x[2])
            if not self._fire(t, j, x, o, gap=True):
                blocked.add((x[3], x[1]))
        price = o
        for target in ([lo, h, c] if c >= o else [h, lo, c]):
            up = target > price
            while True:
                if up:
                    hit = [x for x in live() if x[1] > 0 and price - EPS <= x[0] <= target + EPS]
                    x = min(hit, key=lambda x: (x[0], x[2])) if hit else None
                else:
                    hit = [x for x in live() if x[1] < 0 and target - EPS <= x[0] <= price + EPS]
                    x = min(hit, key=lambda x: (-x[0], x[2])) if hit else None
                if x is None:
                    break
                if self._fire(t, j, x, x[0], gap=False):
                    price = x[0]
                else:
                    blocked.add((x[3], x[1]))
            price = target

    def _fire(self, t: int, j: int, x: tuple, price: float, gap: bool) -> bool:
        _, side, _, kind, arg = x
        if kind == "stop":
            self._exit_unit(t, j, arg, price, "stop (gap)" if gap else "stop")
        elif kind == "exit":
            self._exit_all(t, j, price, "exit (gap)" if gap else "exit")
        elif kind == "add":
            return self._add(t, j, price)
        elif kind == "entry":
            return self._enter(t, j, side, price, arg)
        elif kind == "hypo_start":
            n = self.n_sheet[j]
            self.hypo[j] = (side, price, price - side * self.cfg.stop_n * n)
        elif kind == "hypo_end":
            hd, entry, _ = self.hypo.pop(j)
            self.last_win[j] = (price - entry) * hd > 0
        return True

    # ----- orders -----

    def _slip(self, t: int, j: int) -> float:
        cfg = self.cfg
        return cfg.slippage_low if self.data.traded[t - 1, j] < cfg.low_price else cfg.slippage

    def _trade(self, t: int, j: int, q: float, price: float) -> float:
        """Buy (q > 0) or sell (q < 0) q shares at price plus slippage; returns the fill."""
        s = self._slip(t, j)
        fill = price * (1 + s) if q > 0 else price * (1 - s)
        self.cash -= q * fill
        self.shares[j] += q
        self.last_px[j] = price
        self.flows["slippage"] -= abs(q) * price * s
        return fill

    def _room(self, j: int, d: int) -> bool:
        cfg = self.cfg
        units = {k: len(p.units) for k, p in self.positions.items() if p.d == d}
        if units.get(j, 0) + 1 > cfg.max_units_market:
            return False
        for groups, cap in ((self.close_groups, cfg.max_units_close), (self.loose_groups, cfg.max_units_loose)):
            for g in groups:
                if j in g and sum(units.get(k, 0) for k in g) + 1 > cap:
                    return False
        return sum(units.values()) + 1 <= cfg.max_units_direction

    def _size(self, t: int, j: int, price: float) -> float:
        """Shares for one unit, shrunk to the gross-exposure cap; 0 when it does not fit."""
        cfg = self.cfg
        n = self.n_sheet[j]
        if not (n > 0):
            return 0.0
        shares = cfg.risk_per_n * self.notional / n
        if cfg.max_gross is None:
            return shares
        gross = float(np.abs(self.shares) @ self.last_px) - abs(self.shares[j]) * self.last_px[j] + \
            abs(self.shares[j]) * price
        room = cfg.max_gross * self.equity_prev - gross
        if room < cfg.min_partial * shares * price:
            return 0.0
        return min(shares, room / price)

    def _enter(self, t: int, j: int, d: int, price: float, kind: str) -> bool:
        cfg = self.cfg
        if not self._room(j, d):
            self.blocked["entry: limits"] += 1
            return False
        shares = self._size(t, j, price)
        if shares <= 0:
            self.blocked["entry: gross cap"] += 1
            return False
        n = self.n_sheet[j]
        fill = self._trade(t, j, d * shares, price)
        p = Position(j, d, [Unit(shares, fill, fill - d * cfg.stop_n * n)], fill + d * cfg.add_n * n, t, kind,
                     risk0=shares * cfg.stop_n * n)
        p.entries.append((t, shares, fill))
        p.cost += shares * price * self._slip(t, j)
        self.positions[j] = p
        return True

    def _add(self, t: int, j: int, price: float) -> bool:
        cfg = self.cfg
        p = self.positions[j]
        if not self._room(j, p.d):
            self.blocked["add: limits"] += 1
            return False
        shares = self._size(t, j, price)
        if shares <= 0:
            self.blocked["add: gross cap"] += 1
            return False
        n = self.n_sheet[j]
        fill = self._trade(t, j, p.d * shares, price)
        for u in p.units:
            u.stop += p.d * cfg.add_n * n
        p.units.append(Unit(shares, fill, fill - p.d * cfg.stop_n * n))
        p.next_add = fill + p.d * cfg.add_n * n
        p.entries.append((t, shares, fill))
        p.cost += shares * price * self._slip(t, j)
        p.peak_units = max(p.peak_units, len(p.units))
        return True

    def _exit_unit(self, t: int, j: int, i: int, price: float, reason: str) -> None:
        p = self.positions[j]
        u = p.units.pop(i)
        fill = self._trade(t, j, -p.d * u.shares, price)
        p.exits.append((t, u.shares, fill, reason))
        p.cost += u.shares * price * self._slip(t, j)
        if not p.units:
            self._finish(j)

    def _exit_all(self, t: int, j: int, price: float, reason: str) -> None:
        p = self.positions[j]
        while p.units:
            u = p.units.pop()
            fill = self._trade(t, j, -p.d * u.shares, price)
            p.exits.append((t, u.shares, fill, reason))
            p.cost += u.shares * price * self._slip(t, j)
        self._finish(j)

    def _finish(self, j: int) -> None:
        p = self.positions.pop(j)
        d = self.data
        bought = sum(s * f for _, s, f in p.entries)
        sold = sum(s * f for _, s, f, _ in p.exits)
        shares = sum(s for _, s, _ in p.entries)
        pnl = p.d * (sold - bought)
        last = p.exits[-1][0]
        self.trades.append({
            "symbol": d.symbols[j], "side": "long" if p.d > 0 else "short", "entry": p.kind,
            "entry_date": d.days[p.entry_t], "exit_date": d.days[last],
            "holding_days": (date.fromisoformat(d.days[last]) - date.fromisoformat(d.days[p.entry_t])).days,
            "sessions": last - p.entry_t, "units": p.peak_units,
            "avg_entry": bought / shares, "avg_exit": sold / shares, "position_value": bought,
            "pnl": pnl, "r": pnl / p.risk0 if p.risk0 > 0 else float("nan"), "pct": 100 * pnl / bought,
            "exit_reason": p.exits[-1][3], "unit_exits": [e[3] for e in p.exits], "cost": p.cost,
        })


def run(cfg: Config, data, ind: Indicators, t0: int, t1: int) -> dict:
    return Account(cfg, data, ind).run(t0, t1)


def combine(runs: list[dict]) -> dict:
    """Sum separate accounts (e.g. half the capital in each system) into one."""
    out = {"date": runs[0]["date"], "rate": runs[0]["rate"], "start": runs[0]["start"]}
    for k in ("equity", "gross", "net", "units_long", "units_short", "positions", "notional", "cuts"):
        out[k] = sum(r[k] for r in runs)
    out["start_equity"] = sum(r["start_equity"] for r in runs)
    out["trades"] = sorted((t for r in runs for t in r["trades"]), key=lambda t: (t["exit_date"], t["entry_date"]))
    out["flows"] = {k: sum(r["flows"][k] for r in runs) for k in runs[0]["flows"]}
    out["blocked"] = {k: sum(r["blocked"][k] for r in runs) for k in runs[0]["blocked"]}
    out["ruined"] = min((r["ruined"] for r in runs if r["ruined"]), default=None)
    return out
