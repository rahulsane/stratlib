"""Portfolio simulation and metrics under the research ground rules, shared by the app's backtests and the
research scripts (research/engine.py adds the research runner, ledger and reports).

Ground rules (defaults; a strategy's prompt may override them):
- Universe: stocks and ETFs with an as-traded close of at least $5 and a
  20-day average dollar volume of at least $20M on the signal day.
- Slippage 0.10% per side, 0.25% when the as-traded price is under $20.
- Size: shares = 0.5% of equity / (entry fill - stop), capped at 20% of
  equity and at the cash available (no margin). At most 10 positions.
  Extra signals are ranked by 3-month (63-session) return, highest first.
- Periods: in-sample 2016-2021, out-of-sample 2022 onward, and combined;
  each a separate run from fresh capital.

Order of events each session:
  open   exits scheduled at the previous close; stops gapped through (exit
         at the open); then entries: next-open orders, and buy-stop orders
         that trade through their trigger (filled at the trigger, or at the
         open when it gaps above). Triggered orders compete for free slots by
         3-month return.
  day    stops (exit at the stop) take priority over targets. A position
         bought through a buy-stop can be stopped on its entry day (see
         entry_day_stop_hit for the assumed order of prices).
  close  strategy exit rules (full or partial); delisted securities exit at
         their last close; entries signalled at this close; new buy-stop
         orders; equity is marked.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np

from .panel import Panel

PERIODS = {
    "in_sample": ("2016-01-01", "2021-12-31"),
    "out_of_sample": ("2022-01-01", None),
    "combined": ("2016-01-01", None),
}
PERIOD_TITLES = {"in_sample": "In-sample (2016–2021)", "out_of_sample": "Out-of-sample (2022–present)",
                 "combined": "Combined (2016–present)", "last_2_years": "Last two years"}


@dataclass
class Rules:
    capital: float = 100_000.0
    risk_pct: float = 0.5
    max_position_pct: float = 20.0
    max_positions: int = 10
    slippage_pct: float = 0.10
    slippage_low_price_pct: float = 0.25
    low_price: float = 20.0
    # Order of prices within a buy-stop's entry day: "ohlc" assumes open, low,
    # high, close on an up day (close >= open) and open, high, low, close on a
    # down day; "worst" assumes any low below the stop came after the entry.
    entry_day_path: str = "ohlc"
    # Research only: True lifts the cash limit (positions are sized by risk and
    # the 20% cap even when that needs more than the cash available).
    allow_margin: bool = False
    # Idle cash earns the 3-month T-bill rate (calendar days between sessions / 365).
    idle_cash_tbill: bool = False
    # Overlay: all uninvested capital is held in SPY (dividends reinvested). At each close SPY is
    # sold to fund that session's entries and cash from the session's exits is swept back into SPY,
    # paying the normal slippage on SPY.
    overlay_spy: bool = False
    # With overlay_spy: keep this % of equity as a cash reserve that funds new trades (SPY is not sold to
    # fund them). Trade proceeds return to the reserve; SPY is rebalanced to (100 - reserve_pct)% of
    # equity at the first session and at each month-end close. 0 = fund trades by selling SPY.
    reserve_pct: float = 0.0


@dataclass
class Position:
    j: int
    symbol: str
    signal_day: int
    entry_day: int
    entry_price: float  # adjusted basis, before slippage
    entry_fill: float
    stop: float  # current stop, adjusted basis
    initial_stop: float
    shares: float  # shares still held
    initial_shares: float = 0.0
    target: float | None = None
    target_fraction: float = 1.0  # share of the initial shares sold at the target
    target_label: str = "target"
    cash_limited: bool = False
    entry_note: str = ""
    pending_exit: str | None = None  # reason for an exit at the next open
    proceeds: float = 0.0  # cash received from sales so far (after slippage)
    proceeds_as_traded: float = 0.0
    partials: list = field(default_factory=list)  # (session, fill, shares, reason)
    state: dict = field(default_factory=dict)  # strategy scratch space


@dataclass
class Trade:
    ticker: str
    kind: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    stop: float
    exit_price: float  # average over all shares sold, including partial sales
    entry_price_as_traded: float
    exit_price_as_traded: float
    exit_reason: str
    shares: float
    position_value: float
    return_pct: float
    r: float
    pnl: float
    holding_days: int
    holding_sessions: int
    cash_limited: bool
    entry_note: str = ""
    partial_date: str = ""
    partial_exit_price: float | None = None
    final_exit_price: float | None = None
    entry_price_raw: float | None = None  # entry price before slippage


class Strategy:
    """Base class. Subclasses fill in what they need.

    entry: "close" buys at the signal session's close; "open" at the next
    open; "stop" places a buy-stop order that stays live for order_life
    sessions after the signal; "close_confirm" watches the same orders but, on the
    first session that trades above the trigger, buys at that close if
    confirm() agrees and otherwise drops the order; "scheduled" replays given entries: scheduled(t)
    returns (column, signal session, price before slippage, note) tuples to buy
    at session t (entry-day stops follow the buy-stop rule for the note).
    setup() receives the panel and parameters and precomputes arrays.
    candidates(t) returns column indices signalled at the close of session t
    ("close" and "open" entries). orders(t) returns {column: trigger price}
    for buy-stops placed at the close of t; a new order for the same column
    replaces the old one. The engine keeps only eligible securities that are
    not already held.
    initial_stop(j, t_signal, entry_price, t_entry) returns the stop (adjusted
    basis), or None to skip the trade.
    on_close() is called each session for open positions after stops; it may
    move pos.stop / pos.target, and returns None, ("close", reason),
    ("open", reason) or ("partial", reason, fraction of the initial shares).
    """

    name = "strategy"
    entry = "close"
    order_life = 5

    def setup(self, panel: Panel, params: dict) -> None:
        self.panel, self.params = panel, params

    def candidates(self, t: int) -> np.ndarray:
        raise NotImplementedError

    def orders(self, t: int) -> dict[int, float]:
        raise NotImplementedError

    def initial_stop(self, j: int, t_signal: int, entry_price: float, t_entry: int) -> float | None:
        raise NotImplementedError

    def initial_target(self, j: int, t_signal: int, entry_price: float) -> float | None:
        return None

    def fill_order(self, j: int, t_signal: int, t: int, trigger: float) -> tuple[float, str] | None:
        """Buy-stop fill on session t: at the open if it gaps above the trigger, else at the trigger."""
        o, h = self.panel.open[t, j], self.panel.high[t, j]
        if not np.isfinite(h):
            return None
        if np.isfinite(o) and o > trigger:
            return float(o), "gap above trigger"
        if h > trigger:
            return float(trigger), "through trigger"
        return None

    def confirm(self, j: int, t_signal: int, t: int, trigger: float) -> bool:
        """entry = "close_confirm": buy at the close of the session that trades above the trigger?"""
        raise NotImplementedError

    def entry_day_stopped(self, pos: "Position", t: int, path: str) -> bool:
        p = self.panel
        return entry_day_stop_hit(pos, p.open[t, pos.j], p.low[t, pos.j], p.close[t, pos.j], path)

    def on_close(self, pos: Position, t: int):
        return None

    # Optional: size(j, t_signal, t, fill, stop, equity, available_cash, orders_left) -> shares, or None for
    # the risk-based default. `orders_left` counts this order and the ones still to be opened this session.
    # The position cap and the cash limit still apply.


# Recent set-ups for reuse: (factory, panel, parameters and constructor state as JSON, state after set-up), newest
# last. A Qullamaggie set-up holds about 1 GB of arrays, so only a couple are kept.
_SETUPS: list[tuple] = []
SETUPS_KEPT = 2


def set_up(make_strategy, panel: Panel, params: dict) -> Strategy:
    """make_strategy(), set up for params on panel.

    Signals depend on the strategy, its parameters and the panel, not on the test period, so an identical recent
    set-up is reused: the same factory and panel, with equal parameters and constructor state. Each strategy gets
    its own copy of the set-up state, taken before any run; the arrays in it are shared read-only, so a run that
    wrote to one would fail loudly rather than change another run. A strategy whose constructor state is not
    plain data (an intraday source, a wrapped strategy) is set up afresh every time.
    """
    strategy = make_strategy()
    try:
        key = json.dumps({"params": params, "state": vars(strategy)}, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        strategy.setup(panel, params)
        return strategy
    for factory, saved_panel, saved_key, state in _SETUPS:
        if factory is make_strategy and saved_panel is panel and saved_key == key:
            vars(strategy).update(_share(state, panel))
            return strategy
    strategy.setup(panel, params)
    try:
        state = _share(_freeze(vars(strategy), panel), panel)
    except Exception:  # an attribute that cannot be copied; reuse is only an optimisation
        return strategy
    _SETUPS.append((make_strategy, panel, key, state))
    del _SETUPS[:-SETUPS_KEPT]
    return strategy


def _freeze(value, panel: Panel):
    """Make the set-up's own arrays read-only; the panel's arrays are left as they are."""
    if isinstance(value, np.ndarray):
        if not any(value is a for a in vars(panel).values()):
            value.setflags(write=False)
    elif isinstance(value, dict):
        for item in value.values():
            _freeze(item, panel)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _freeze(item, panel)
    return value


def _share(value, panel: Panel):
    """A copy of set-up state that shares the panel and every array, and copies everything else."""
    if value is panel or isinstance(value, np.ndarray):
        return value
    if isinstance(value, dict):
        out = copy.copy(value)   # keeps the type, such as a defaultdict's factory
        for k, item in value.items():
            out[k] = _share(item, panel)
        return out
    if isinstance(value, list):
        out = copy.copy(value)
        out[:] = [_share(item, panel) for item in value]
        return out
    if type(value) is tuple:
        return tuple(_share(item, panel) for item in value)
    return copy.deepcopy(value)


def slippage(rules: Rules, as_traded_price: float) -> float:
    pct = rules.slippage_low_price_pct if as_traded_price < rules.low_price else rules.slippage_pct
    return pct / 100


def entry_day_stop_hit(pos: Position, o: float, lo: float, c: float, path: str) -> bool:
    """Whether a buy-stop entry reached its stop later on the entry day.

    After a gap above the trigger every low of the day follows the entry. For
    an entry through the trigger during the day, an up day is taken as open,
    low, high, close: the low came before the breakout, and only a close at or
    below the stop means the price fell back through it. A down day is taken
    as open, high, low, close: the low followed the breakout.
    """
    if not np.isfinite(lo) or lo > pos.stop:
        return False
    if path == "worst" or pos.entry_note != "through trigger" or not (np.isfinite(o) and np.isfinite(c)):
        return True
    return c <= pos.stop if c >= o else True


def market_series(panel: Panel, benchmarks: dict) -> dict:
    """Per-session T-bill rate (annual %, carried forward) and SPY total-return index."""
    rates = np.array([benchmarks["tbill3m"].get(str(d), np.nan) for d in panel.dates], dtype=float)
    for k in range(1, len(rates)):
        if not np.isfinite(rates[k]):
            rates[k] = rates[k - 1]
    j = panel.index["SPY"]
    close = panel.close_ff[:, j]
    tr = np.ones(len(close))
    for k in range(1, len(close)):
        tr[k] = tr[k - 1] * (close[k] + benchmarks["spy_dividends"].get(str(panel.dates[k]), 0.0)) / close[k - 1]
    return {"cash_rate": np.nan_to_num(rates), "spy_tr": tr * close[0]}


def simulate(panel: Panel, strategy: Strategy, start: str, end: str | None, rules: Rules | None = None,
             market: dict | None = None, accountant=None) -> dict:
    """accountant (research/tax_accounting.py, optional): dividends and taxes kept outside the strategy, which
    never sees them. It is told of every purchase (on_buy(j, t, shares, fill)) and sale (on_sell(j, t, shares,
    fill, reason)); dividends(t, {column: shares}) returns the cash due at the open for shares held at the previous
    close; at the first session of each year tax_due(t) returns the previous year's tax, paid from cash, selling
    every position pro rata at the open when the cash is short ("tax" sales); finish(t) settles the last year after
    the final sales. Not for use with overlay_spy."""
    rules = rules or Rules()
    p = panel
    t0 = p.session_index(start)
    t1 = len(p.dates) - 1 if end is None else int(np.searchsorted(p.dates, end, side="right")) - 1
    cash = rules.capital
    positions: dict[int, Position] = {}
    trades: list[Trade] = []
    equity = np.zeros(t1 - t0 + 1)
    invested = np.zeros(t1 - t0 + 1)
    held_count = np.zeros(t1 - t0 + 1, dtype=int)
    pending_entries: list[tuple[int, int]] = []  # (j, signal session) for next-open entries
    pending_orders: dict[int, tuple[float, int, int]] = {}  # j -> (trigger, signal session, last live session)
    counts = {"signals": 0, "entries": 0, "skipped_no_slot": 0, "skipped_no_cash": 0, "skipped_stop_rule": 0,
              "cash_limited_entries": 0, "gap_stop_exits": 0}
    if strategy.entry == "stop":
        counts.update({"triggered": 0, "entry_day_stop_exits": 0})
    if strategy.entry == "close_confirm":
        counts.update({"triggered": 0, "not_confirmed": 0})
    if strategy.entry == "scheduled":
        counts.update({"entry_day_stop_exits": 0})
    last_equity = rules.capital
    if (rules.idle_cash_tbill or rules.overlay_spy) and market is None:
        raise ValueError("idle_cash_tbill and overlay_spy need market=market_series(panel, benchmarks)")
    # A strategy may replace the ground-rule liquidity mask with its own (eligible_mask).
    eligible = getattr(strategy, "eligible_mask", None)
    eligible = p.eligible if eligible is None else eligible
    # Optional hooks, for strategies with their own portfolio rules (the app's CANSLIM, Trend Leaders and Nash):
    # order_key(j, t_signal) ranks competing orders instead of the 63-session return; max_positions(t_signal)
    # caps the holdings an order may join, such as a market-exposure ladder; reduce(t, positions) lists
    # (position, reason) to sell at the open before entries; on_entry(position, t) follows each purchase and
    # on_exit(position, t, reason) each final sale.
    order_key = getattr(strategy, "order_key", None)
    slot_limit = getattr(strategy, "max_positions", None)
    reduce = getattr(strategy, "reduce", None)
    on_entry = getattr(strategy, "on_entry", None)
    on_exit = getattr(strategy, "on_exit", None)
    spy_units = 0.0
    spy_j = p.index.get("SPY")
    interest = 0.0

    def spy_value(t):
        return spy_units * market["spy_tr"][t] if rules.overlay_spy else 0.0

    def spy_slip(t):
        return slippage(rules, p.close_ff[t, spy_j])

    reserve = rules.overlay_spy and rules.reserve_pct > 0
    skips: list[dict] = []

    def raise_cash(t, amount):
        """Overlay: sell SPY at the close so that cash covers `amount` (not in reserve mode)."""
        nonlocal cash, spy_units
        need = amount - cash
        if not rules.overlay_spy or reserve or need <= 0 or spy_units <= 0:
            return
        units = min(spy_units, need / (market["spy_tr"][t] * (1 - spy_slip(t))))
        spy_units -= units
        cash += units * market["spy_tr"][t] * (1 - spy_slip(t))

    def sweep(t):
        """Overlay: put all remaining cash into SPY at the close. In reserve mode, rebalance SPY to
        (100 - reserve_pct)% of equity at the first session and at month-ends instead."""
        nonlocal cash, spy_units
        if not rules.overlay_spy:
            return
        if not reserve:
            if cash > 1e-9:
                spy_units += cash * (1 - spy_slip(t)) / market["spy_tr"][t]
                cash = 0.0
            return
        month_end = t == t0 or t == len(p.dates) - 1 or str(p.dates[t])[:7] != str(p.dates[t + 1])[:7]
        if not month_end:
            return
        px = market["spy_tr"][t]
        gap = (1 - rules.reserve_pct / 100) * mark(t)[0] - spy_units * px
        if gap > 0:
            buy = min(gap, cash)
            spy_units += buy * (1 - spy_slip(t)) / px
            cash -= buy
        elif gap < 0:
            units = -gap / px
            spy_units -= units
            cash += units * px * (1 - spy_slip(t))

    def mark(t):
        value = 0.0
        for pos in positions.values():
            price = p.close_ff[t, pos.j]
            if not np.isfinite(price):
                price = pos.entry_price
            value += pos.shares * price
        return cash + value + spy_value(t), value

    def close_position(pos: Position, t: int, price: float, reason: str, fraction: float = 1.0):
        """Sell all remaining shares, or `fraction` of the initial shares."""
        nonlocal cash
        as_traded = price * p.factor[t, pos.j]
        fill = price * (1 - slippage(rules, as_traded))
        final = fraction >= 1 or pos.shares <= pos.initial_shares * fraction * (1 + 1e-9)
        sold = pos.shares if final else pos.initial_shares * fraction
        cash += sold * fill
        pos.proceeds += sold * fill
        pos.proceeds_as_traded += sold * fill * p.factor[t, pos.j]
        pos.shares -= sold
        if accountant is not None:
            accountant.on_sell(pos.j, t, sold, fill, reason)
        if not final:
            pos.partials.append((t, fill, sold, reason))
            return
        risk = pos.entry_fill - pos.initial_stop
        average = pos.proceeds / pos.initial_shares
        d_in, d_out = date.fromisoformat(p.dates[pos.entry_day]), date.fromisoformat(p.dates[t])
        first = pos.partials[0] if pos.partials else None
        trades.append(Trade(
            ticker=pos.symbol, kind=str(p.kind[pos.j]), signal_date=str(p.dates[pos.signal_day]),
            entry_date=str(p.dates[pos.entry_day]), exit_date=str(p.dates[t]),
            entry_price=pos.entry_fill, stop=pos.initial_stop, exit_price=average,
            entry_price_as_traded=pos.entry_fill * p.factor[pos.entry_day, pos.j],
            exit_price_as_traded=pos.proceeds_as_traded / pos.initial_shares,
            exit_reason=" + ".join([r for _, _, _, r in pos.partials] + [reason]), shares=pos.initial_shares,
            position_value=pos.initial_shares * pos.entry_fill, return_pct=100 * (average / pos.entry_fill - 1),
            r=(average - pos.entry_fill) / risk if risk > 0 else float("nan"),
            pnl=pos.proceeds - pos.initial_shares * pos.entry_fill,
            holding_days=(d_out - d_in).days, holding_sessions=t - pos.entry_day, cash_limited=pos.cash_limited,
            entry_note=pos.entry_note, partial_date=str(p.dates[first[0]]) if first else "",
            partial_exit_price=first[1] if first else None, final_exit_price=fill,
            entry_price_raw=pos.entry_price))
        del positions[pos.j]
        if on_exit is not None:
            on_exit(pos, t, reason)

    def hit_target(pos: Position, t: int, price: float, suffix: str):
        """Sell target_fraction of the initial shares (all of them by default); the target is then used up."""
        close_position(pos, t, price, pos.target_label + suffix, fraction=pos.target_fraction)
        pos.target = None

    def open_positions(orders: list[tuple[int, int, float, str]], t: int, equity_now: float):
        """orders: (j, signal session, price before slippage, note), ranked by 3-month return
        on the signal session. Orders that do not get a slot are cancelled."""
        nonlocal cash
        held = [o for o in orders if o[0] in positions]
        if held:
            counts["skipped_already_held"] = counts.get("skipped_already_held", 0) + len(held)

        def log(j, reason, **extra):
            skips.append({"date": str(p.dates[t]), "ticker": str(p.symbols[j]), "reason": reason,
                          "open_positions": len(positions), "invested_pct": 100 * mark(t)[1] / equity_now, **extra})

        for o in held:
            log(o[0], "stock already held")
        orders = [o for o in orders if o[0] not in positions]
        if order_key is None:
            orders.sort(key=lambda o: -np.nan_to_num(p.ret63[o[1], o[0]], nan=-np.inf))
        else:
            orders.sort(key=lambda o: order_key(o[0], o[1]))
        custom_size = getattr(strategy, "size", None)
        for k, (j, ts, price, note) in enumerate(orders):
            cap = rules.max_positions if slot_limit is None else min(rules.max_positions, slot_limit(ts))
            if len(positions) >= cap:
                counts["skipped_no_slot"] += 1
                log(j, "no free slot")
                continue
            if not np.isfinite(price) or price <= 0:
                counts["skipped_stop_rule"] += 1
                continue
            fill = price * (1 + slippage(rules, price * p.factor[t, j]))
            stop = strategy.initial_stop(j, ts, price, t)
            if stop is None or not np.isfinite(stop) or stop >= price:
                counts["skipped_stop_rule"] += 1
                continue
            available = cash + spy_value(t) * (1 - spy_slip(t)) if rules.overlay_spy and not reserve else cash
            shares = None
            if custom_size is not None:
                shares = custom_size(j, ts, t, fill, stop, equity_now, available, len(orders) - k)
            if shares is None:
                shares = (rules.risk_pct / 100) * equity_now / (fill - stop)
            shares = min(shares, (rules.max_position_pct / 100) * equity_now / fill)
            limited = False
            wanted = shares * fill
            if shares * fill > available and not rules.allow_margin:
                shares, limited = available / fill, True
            if shares * fill < 1.0:
                counts["skipped_no_cash"] += 1
                log(j, "no cash", wanted_pct=100 * wanted / equity_now, available_pct=100 * available / equity_now)
                continue
            if limited:
                log(j, "shrunk to the cash available", wanted_pct=100 * wanted / equity_now,
                    available_pct=100 * available / equity_now)
            raise_cash(t, shares * fill)
            cash -= shares * fill
            counts["entries"] += 1
            counts["cash_limited_entries"] += int(limited)
            positions[j] = Position(j=j, symbol=str(p.symbols[j]), signal_day=ts, entry_day=t, entry_price=price,
                                    entry_fill=fill, stop=stop, initial_stop=stop, shares=shares,
                                    initial_shares=shares, target=strategy.initial_target(j, ts, price),
                                    cash_limited=limited, entry_note=note)
            if accountant is not None:
                accountant.on_buy(j, t, shares, fill)
            if on_entry is not None:
                on_entry(positions[j], t)

    taxes_paid: list[tuple[str, float]] = []

    def pay_tax(t):
        """The previous year's tax, from cash; when that is short, every position is cut pro rata at the open."""
        nonlocal cash
        due = accountant.tax_due(t)
        if due <= 0:
            return
        if due > cash:
            held = [(pos, p.open[t, pos.j]) for pos in positions.values() if np.isfinite(p.open[t, pos.j])]
            value = sum(pos.shares * o * (1 - slippage(rules, o * p.factor[t, pos.j])) for pos, o in held)
            if value > 0:
                fraction = min(1.0, (due - cash) / value)
                counts["tax_sales"] = counts.get("tax_sales", 0) + len(held)
                for pos, o in held:
                    close_position(pos, t, o, "tax", fraction=fraction * pos.shares / pos.initial_shares)
        cash -= due
        taxes_paid.append((str(p.dates[t]), due))

    for t in range(t0, t1 + 1):
        if accountant is not None and t > t0:
            cash += accountant.dividends(t, {j: pos.shares for j, pos in positions.items()})
        if rules.idle_cash_tbill and t > t0 and cash > 0:
            days = (date.fromisoformat(str(p.dates[t])) - date.fromisoformat(str(p.dates[t - 1]))).days
            earned = cash * market["cash_rate"][t - 1] / 100 * days / 365
            cash += earned
            interest += earned
        # --- open
        for pos in list(positions.values()):
            o = p.open[t, pos.j]
            if not np.isfinite(o):
                continue
            if pos.pending_exit:
                close_position(pos, t, o, pos.pending_exit)
            elif o <= pos.stop:
                counts["gap_stop_exits"] += 1
                close_position(pos, t, o, "stop (gap)" if not pos.partials else "stop (gap, after partial)")
            elif pos.target is not None and o >= pos.target:
                hit_target(pos, t, o, " (gap)")
        if reduce is not None and t > t0:
            for pos, reason in reduce(t, list(positions.values())):
                if np.isfinite(p.open[t, pos.j]):
                    close_position(pos, t, p.open[t, pos.j], reason)
        if accountant is not None and t > t0 and str(p.dates[t])[:4] != str(p.dates[t - 1])[:4]:
            pay_tax(t)
        if pending_entries:
            open_positions([(j, ts, p.open[t, j], "") for j, ts in pending_entries], t, last_equity)
            pending_entries = []
        if strategy.entry == "scheduled":
            scheduled = strategy.scheduled(t)
            if scheduled:
                open_positions(scheduled, t, last_equity)
        if pending_orders and strategy.entry == "stop":
            fills = []
            for j, (trigger, ts, last) in list(pending_orders.items()):
                if t > last or j in positions:
                    del pending_orders[j]
                    continue
                filled = strategy.fill_order(j, ts, t, trigger)
                if filled is None:
                    continue
                fills.append((j, ts, filled[0], filled[1]))
                del pending_orders[j]
            counts["triggered"] += len(fills)
            if fills:
                open_positions(fills, t, last_equity)
        # --- during the session
        for pos in list(positions.values()):
            lo, hi = p.low[t, pos.j], p.high[t, pos.j]
            if not np.isfinite(p.close[t, pos.j]):
                continue
            if pos.entry_day == t and strategy.entry in ("stop", "scheduled"):
                if strategy.entry_day_stopped(pos, t, rules.entry_day_path):
                    counts["entry_day_stop_exits"] += 1
                    close_position(pos, t, pos.stop, "stop")
            elif np.isfinite(lo) and lo <= pos.stop and not (pos.entry_day == t and strategy.entry == "close"):
                close_position(pos, t, pos.stop, "stop" if not pos.partials else "stop (after partial)")
            elif pos.target is not None and np.isfinite(hi) and hi >= pos.target and pos.entry_day != t:
                hit_target(pos, t, pos.target, "")
        # --- close
        for pos in list(positions.values()):
            if not p.valid[t, pos.j]:
                continue
            if p.last_bar[pos.j] == t and t < len(p.dates) - 1:
                close_position(pos, t, p.close[t, pos.j], "delisted")
                continue
            if pos.entry_day == t and strategy.entry == "close":
                continue
            decision = strategy.on_close(pos, t)
            if decision:
                if decision[0] == "close":
                    close_position(pos, t, p.close[t, pos.j], decision[1])
                elif decision[0] == "partial":
                    close_position(pos, t, p.close[t, pos.j], decision[1], fraction=decision[2])
                else:
                    pos.pending_exit = decision[1]
        if pending_orders and strategy.entry == "close_confirm" and t < t1:
            buys = []
            for j, (trigger, ts, last) in list(pending_orders.items()):
                if t > last or j in positions:
                    del pending_orders[j]
                    continue
                h = p.high[t, j]
                if not (np.isfinite(h) and h > trigger):
                    continue
                del pending_orders[j]  # the first session through the trigger decides
                counts["triggered"] += 1
                if strategy.confirm(j, ts, t, trigger):
                    buys.append((j, ts, p.close[t, j], "confirmed at close"))
                else:
                    counts["not_confirmed"] += 1
            if buys:
                open_positions(buys, t, mark(t)[0])
        if t < t1 and strategy.entry != "scheduled":  # no new positions on the last session
            if strategy.entry in ("stop", "close_confirm"):
                for j, trigger in strategy.orders(t).items():
                    if eligible[t, j] and j not in positions:
                        counts["signals"] += int(j not in pending_orders)
                        pending_orders[j] = (float(trigger), t, t + strategy.order_life)
            else:
                candidates = [j for j in strategy.candidates(t) if eligible[t, j]]
                for j in candidates:
                    if j in positions:
                        skips.append({"date": str(p.dates[t]), "ticker": str(p.symbols[j]), "reason": "stock already held",
                                      "open_positions": len(positions)})
                signalled = [j for j in candidates if j not in positions]
                counts["signals"] += len(signalled)
                if strategy.entry == "close" and signalled:
                    open_positions([(j, t, p.close[t, j], "") for j in signalled], t, mark(t)[0])
                elif signalled:
                    pending_entries = [(j, t) for j in signalled]
        if t == t1:
            for pos in list(positions.values()):
                price = p.close_ff[t, pos.j]
                close_position(pos, t, price if np.isfinite(price) else pos.entry_price, "end of test")
        sweep(t)
        last_equity, value = mark(t)
        equity[t - t0], invested[t - t0], held_count[t - t0] = last_equity, value, len(positions)
    if rules.idle_cash_tbill:
        counts["interest_earned"] = round(interest, 2)
    out = {"start": str(p.dates[t0]), "end": str(p.dates[t1]), "dates": p.dates[t0:t1 + 1],
           "equity": equity, "invested": invested, "held": held_count, "trades": trades, "counts": counts,
           "rules": asdict(rules), "skips": skips}
    if accountant is not None:
        out["taxes_paid"] = taxes_paid
        out["tax"] = accountant.finish(t1)
    return out


# ----------------------------------------------------------------------
# Benchmarks and metrics


def spy_series(panel: Panel, dividends: dict[str, float], start: str, end: str) -> dict:
    """SPY bought at the first close: price only and with dividends reinvested at the ex-date close."""
    j = panel.index["SPY"]
    i0, i1 = panel.session_index(start), int(np.searchsorted(panel.dates, end, side="right")) - 1
    close = panel.close_ff[i0:i1 + 1, j]
    price = close / close[0]
    total = np.ones_like(price)
    for k in range(1, len(close)):
        div = dividends.get(str(panel.dates[i0 + k]), 0.0)
        total[k] = total[k - 1] * (close[k] + div) / close[k - 1]
    return {"price": price, "total": total}


def drawdown(values: np.ndarray) -> float:
    peak = np.maximum.accumulate(values)
    return float(100 * (1 - values / peak).max())


def cagr(values: np.ndarray, dates: np.ndarray) -> float:
    years = (date.fromisoformat(str(dates[-1])) - date.fromisoformat(str(dates[0]))).days / 365.25
    return float(100 * ((values[-1] / values[0]) ** (1 / years) - 1)) if years > 0 else float("nan")


def sharpe(values: np.ndarray, dates: np.ndarray, tbill: dict[str, float]) -> float:
    rets = values[1:] / values[:-1] - 1
    rf = np.array([tbill.get(str(d), np.nan) for d in dates[1:]], dtype=float)
    rf = np.nan_to_num(_ffill(rf), nan=0.0) / 100 / 252
    excess = rets - rf
    sd = excess.std(ddof=1)
    return float(excess.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")


def _ffill(a: np.ndarray) -> np.ndarray:
    out = a.copy()
    for k in range(1, len(out)):
        if not np.isfinite(out[k]):
            out[k] = out[k - 1]
    return out


def yearly(values: np.ndarray, dates: np.ndarray) -> dict[str, float]:
    result, prev = {}, values[0]
    years = np.array([str(d)[:4] for d in dates])
    for y in sorted(set(years)):
        last = values[np.flatnonzero(years == y)[-1]]
        result[y] = float(100 * (last / prev - 1))
        prev = last
    return result


def metrics(run: dict, spy: dict, tbill: dict) -> dict:
    trades, eq, dates = run["trades"], run["equity"], run["dates"]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win, gross_loss = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
    total_pnl = sum(t.pnl for t in trades)
    top_n = math.ceil(0.1 * len(trades)) if trades else 0
    top = sorted(trades, key=lambda t: -t.pnl)[:top_n]
    rs = [t.r for t in trades if np.isfinite(t.r)]
    held_any = np.array(run["held"]) > 0
    # A position opened and closed within one session still counts that session.
    days_with_position = set()
    for tr in trades:
        a, b = np.searchsorted(dates, tr.entry_date), np.searchsorted(dates, tr.exit_date)
        days_with_position.update(range(int(a), int(b) + 1))
    reasons = {}
    for tr in trades:
        reasons[tr.exit_reason] = reasons.get(tr.exit_reason, 0) + 1

    def mean(xs):
        return float(np.mean(xs)) if xs else float("nan")

    spy_total = spy["total"] * run["rules"]["capital"]
    spy_price = spy["price"] * run["rules"]["capital"]
    return {
        "start": run["start"], "end": run["end"],
        "trades": len(trades),
        "win_rate": 100 * len(wins) / len(trades) if trades else float("nan"),
        "avg_win_pct": mean([t.return_pct for t in wins]),
        "avg_loss_pct": mean([t.return_pct for t in losses]),
        "avg_win_r": mean([t.r for t in wins if np.isfinite(t.r)]),
        "avg_loss_r": mean([t.r for t in losses if np.isfinite(t.r)]),
        "expectancy_r": mean(rs),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "total_return": 100 * (eq[-1] / run["rules"]["capital"] - 1) if len(eq) else float("nan"),
        "end_equity": float(eq[-1]),
        "cagr": cagr(np.concatenate([[run["rules"]["capital"]], eq]), np.concatenate([[dates[0]], dates])),
        "max_drawdown": drawdown(np.concatenate([[run["rules"]["capital"]], eq])),
        "sharpe": sharpe(np.concatenate([[run["rules"]["capital"]], eq]), np.concatenate([[dates[0]], dates]), tbill),
        "avg_holding_days": mean([t.holding_days for t in trades]),
        "avg_holding_sessions": mean([t.holding_sessions for t in trades]),
        "exposure_time_pct": 100 * len(days_with_position) / len(dates),
        "exposure_avg_invested_pct": float(100 * np.mean(run["invested"] / eq)),
        "top10pct_trades": top_n,
        "top10pct_profit_share": 100 * sum(t.pnl for t in top) / total_pnl if total_pnl > 0 else float("nan"),
        "top10pct_pnl": sum(t.pnl for t in top), "rest_pnl": total_pnl - sum(t.pnl for t in top),
        "exit_reasons": reasons,
        "counts": run["counts"],
        "yearly": yearly(np.concatenate([[run["rules"]["capital"]], eq]), np.concatenate([[dates[0]], dates])),
        "spy": {
            "total_return": 100 * (spy["total"][-1] - 1), "price_return": 100 * (spy["price"][-1] - 1),
            "cagr": cagr(spy["total"], dates), "cagr_price": cagr(spy["price"], dates),
            "max_drawdown": drawdown(spy["total"]), "sharpe": sharpe(spy["total"], dates, tbill),
            "yearly": yearly(spy["total"], dates), "yearly_price": yearly(spy["price"], dates),
        },
        "equity_curve": {"dates": [str(d) for d in dates], "equity": eq.tolist(),
                         "spy_total": spy_total.tolist(), "spy_price": spy_price.tolist()},
    }
