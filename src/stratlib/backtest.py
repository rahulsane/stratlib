"""Pure daily execution and performance accounting. No data access or UI."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from statistics import mean
from typing import Callable, Sequence

from .config import BacktestSettings, Thresholds
from .market_direction import CONFIRMED
from .prices import Bar
from .sell_rules import positive_price


class BacktestError(ValueError):
    """Invalid inputs or missing evidence prevent a trustworthy result."""


@dataclass(frozen=True)
class Signal:
    symbol: str
    date: str
    pivot: float                 # same split-adjusted basis as execution bars
    rs: int
    pattern: str
    evidence: dict
    breakout_date: str | None = None    # the base's first closing breakout; defaults to the signal date


@dataclass
class Holding:
    signal: Signal
    entry_date: str
    entry_price: float
    shares: float
    fast_gain_date: str | None = None
    profit_order: bool = False


def valid_bar(bar: Bar) -> bool:
    return (all(positive_price(v) for v in (bar.open, bar.high, bar.low, bar.close))
            and bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            and isinstance(bar.volume, (float, int)) and not isinstance(bar.volume, bool)
            and math.isfinite(bar.volume) and bar.volume >= 0)


def performance(curve: Sequence[dict], trades: Sequence[dict], initial: float,
                start: str, end: str, *, field: str = "equity") -> dict:
    """Drawdown includes initial cash; trade statistics use closed trades only."""
    peak, drawdown = initial, 0.0
    for point in curve:
        value = point[field]
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    final = curve[-1][field] if curve else initial
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    years = days / 365.25
    cagr = 100 * ((final / initial) ** (1 / years) - 1) if years > 0 else None
    gains = [t["return_pct"] for t in trades if t["return_pct"] > 0]
    losses = [t["return_pct"] for t in trades if t["return_pct"] < 0]
    avg_gain, avg_loss = mean(gains) if gains else None, mean(losses) if losses else None
    return {"cagr_pct": cagr, "max_drawdown_pct": 100 * drawdown,
            "total_return_pct": 100 * (final / initial - 1), "ending_equity": final,
            "closed_trades": len(trades),
            "win_rate_pct": 100 * len(gains) / len(trades) if trades else None,
            "average_gain_pct": avg_gain, "average_loss_pct": avg_loss,
            "gain_loss_ratio": avg_gain / abs(avg_loss) if gains and losses else None}


def simulate(sessions: list[str], histories: dict[str, list[Bar]], market_states: dict[str, str | None],
             signals_on: Callable[[str], list[Signal]], thresholds: Thresholds,
             portfolio: BacktestSettings, *, settlements: dict[tuple[str, str], float] | None = None,
             delisted: dict[str, str] | None = None, stale: dict[str, set[str]] | None = None,
             exposure: dict[str, float | None] | None = None, progress=None) -> dict:
    """Signal at close -> next session open. No same-close profit executions.

    Stops fill at the lower of the open and stop when the daily low touches it.
    Close-confirmed profit orders execute at the next open. Opening proceeds
    may fund entries that morning; intraday stop proceeds cannot. A fast gain
    uses completed closes, as in the live position rules. Open positions remain
    marked to the final close, excluded from win/loss statistics. ``stale``
    names carried-forward bars for data gaps: they value a holding but can
    never fill an entry.

    ``exposure`` is the share of the portfolio the market allows invested,
    known at each close. It limits the next open to that share of the maximum
    holdings; without it, a confirmed uptrend allows 100% and anything else 0%.
    With portfolio.raise_cash, holdings above the limit are sold at the next
    open, weakest return since entry first. Each breakout is bought at most once,
    and the fast-gain and hold windows count from the breakout, not the entry.
    """
    if len(sessions) < 2 or sessions != sorted(set(sessions)):
        raise BacktestError("Choose at least two distinct trading sessions in date order.")
    bars = {symbol: {b.date: b for b in values} for symbol, values in histories.items()}
    spy = bars.get("SPY", {})
    if any(day not in spy or not valid_bar(spy[day]) for day in sessions):
        raise BacktestError("SPY needs complete daily OHLCV for the selected period.")
    settlements, delisted, stale = settlements or {}, delisted or {}, stale or {}
    if exposure is None:
        exposure = {day: 100.0 if market_states.get(day) == CONFIRMED else 0.0 for day in sessions}
    slots = lambda day: math.floor(portfolio.max_holdings * (exposure.get(day) or 0) / 100 + 1e-9)
    capacity = 0
    cash = portfolio.initial_capital
    spy_shares = cash / spy[sessions[0]].open
    holdings: dict[str, Holding] = {}
    pending: list[Signal] = []
    trades, skipped, curve = [], [], []
    bought: set[tuple[str, str]] = set()

    def close_position(symbol, day, price, reason):
        nonlocal cash
        holding = holdings.pop(symbol)
        cash += holding.shares * price
        trades.append({"symbol": symbol, "signal_date": holding.signal.date,
                       "entry_date": holding.entry_date, "entry_price": holding.entry_price,
                       "exit_date": day, "exit_price": price, "shares": holding.shares,
                       "pnl": holding.shares * (price - holding.entry_price),
                       "return_pct": 100 * (price / holding.entry_price - 1),
                       "reason": reason, "pivot": holding.signal.pivot,
                       "pattern": holding.signal.pattern, "fast_gain_date": holding.fast_gain_date,
                       "evidence": holding.signal.evidence})

    def skip(signal, day, reason):
        skipped.append({"symbol": signal.symbol, "signal_date": signal.date,
                        "entry_date": day, "reason": reason})

    for index, day in enumerate(sessions):
        # Opening exits precede sizing. Unknown marks must never disappear from equity.
        for symbol, holding in list(holdings.items()):
            if delisted.get(symbol, "9999-12-31") <= day:
                value = settlements.get((symbol, delisted[symbol]))
                if value is None or not math.isfinite(value) or value < 0:
                    raise BacktestError(f"{symbol}: need documented delisting proceeds on {delisted[symbol]}.")
                close_position(symbol, day, value, "Delisting settlement")
                continue
            bar = bars.get(symbol, {}).get(day)
            if bar is None or not valid_bar(bar):
                raise BacktestError(f"{symbol}: missing or invalid held-position bar on {day}.")
            stop = holding.entry_price * (1 - thresholds.stop_loss_pct / 100)
            if portfolio.stop_loss and bar.open <= stop:
                close_position(symbol, day, bar.open, "Gap through stop")
            elif holding.profit_order:
                close_position(symbol, day, bar.open, "Profit target, next open")

        if portfolio.raise_cash and index and len(holdings) > capacity:
            previous = sessions[index - 1]
            weakest = sorted(holdings, key=lambda s: (bars[s][previous].close / holdings[s].entry_price, s))
            for symbol in weakest[:len(holdings) - capacity]:
                close_position(symbol, day, bars[symbol][day].open,
                               f"Raise cash: market exposure {exposure.get(previous) or 0:g}%")

        opening_equity = cash + sum(h.shares * bars[symbol][day].open for symbol, h in holdings.items())
        allocation = opening_equity / portfolio.max_holdings
        # RS descending, then ticker, fixed from the signal date. Never use next-day returns.
        seen = set()
        for signal in sorted(pending, key=lambda s: (-s.rs, s.symbol)):
            if signal.symbol in seen:
                continue
            seen.add(signal.symbol)
            bar = bars.get(signal.symbol, {}).get(day)
            reason = None
            if signal.symbol in holdings:
                reason = "Already held"
            elif (signal.symbol, signal.breakout_date or signal.date) in bought:
                reason = "Already bought this breakout"
            elif delisted.get(signal.symbol, "9999-12-31") <= day:
                reason = "Delisted before entry"
            elif bar is None or not valid_bar(bar) or day in stale.get(signal.symbol, ()):
                # Missing the immediate next session is not a license to delay entry.
                reason = "Missing next-session open or valid OHLCV"
            elif bar.open > signal.pivot * (1 + thresholds.buy_zone_max_pct / 100) + 1e-9:
                reason = "Open above maximum pivot distance"
            elif len(holdings) >= capacity:
                reason = ("Maximum holdings reached" if capacity >= portfolio.max_holdings
                          else f"Market exposure limit ({capacity} of {portfolio.max_holdings} positions)")
            elif cash + 1e-8 < allocation or allocation <= 0:
                reason = "Insufficient cash for an equal-sized allocation"
            if reason:
                skip(signal, day, reason)
                continue
            holdings[signal.symbol] = Holding(signal, day, bar.open, allocation / bar.open)
            bought.add((signal.symbol, signal.breakout_date or signal.date))
            cash -= allocation

        for symbol, holding in list(holdings.items()):
            bar = bars[symbol][day]
            stop = holding.entry_price * (1 - thresholds.stop_loss_pct / 100)
            if portfolio.stop_loss and bar.low <= stop + 1e-9:
                close_position(symbol, day, min(bar.open, stop), "Stop loss")
                continue
            breakout = date.fromisoformat(holding.signal.breakout_date or holding.signal.date)
            fast_until = breakout + timedelta(weeks=thresholds.fast_gain_weeks)
            hold_until = breakout + timedelta(weeks=thresholds.minimum_hold_weeks)
            # Include the breakout close itself in the early-gain evidence.
            if holding.fast_gain_date is None:
                early = [bars[symbol][d] for d in dict.fromkeys((holding.signal.date, day)) if d in bars[symbol]
                         and breakout.isoformat() <= d <= fast_until.isoformat()]
                holding.fast_gain_date = next((b.date for b in early if b.close + 1e-9 >=
                    holding.signal.pivot * (1 + thresholds.fast_gain_pct / 100)), None)
            active_hold = holding.fast_gain_date is not None and day < hold_until.isoformat()
            holding.profit_order = (not active_hold and bar.close + 1e-9 >=
                                    holding.entry_price * (1 + thresholds.profit_target_pct / 100))

        equity = cash + sum(h.shares * bars[symbol][day].close for symbol, h in holdings.items())
        curve.append({"date": day, "equity": equity, "cash": max(0.0, cash), "holdings": len(holdings),
                      "spy_equity": spy_shares * spy[day].close, "market_state": market_states.get(day),
                      "exposure": exposure.get(day)})
        # The exposure known at this close sizes tomorrow's opening orders.
        capacity = slots(day)
        pending = signals_on(day) if capacity > 0 and index < len(sessions) - 1 else []
        if any(s.date != day or not positive_price(s.pivot) for s in pending):
            raise BacktestError("Signals must be dated at the current close with a positive pivot.")
        if progress:
            progress(day, index + 1, len(sessions))

    open_positions = [{"symbol": symbol, "entry_date": h.entry_date, "entry_price": h.entry_price,
                       "shares": h.shares, "last_close": bars[symbol][sessions[-1]].close,
                       "value": h.shares * bars[symbol][sessions[-1]].close,
                       "unrealized_pnl": h.shares * (bars[symbol][sessions[-1]].close - h.entry_price),
                       "fast_gain_date": h.fast_gain_date, "profit_order_pending": h.profit_order}
                      for symbol, h in holdings.items()]
    return {"start": sessions[0], "end": sessions[-1], "portfolio": asdict(portfolio),
            "thresholds": asdict(thresholds), "equity_curve": curve, "trades": trades,
            "open_positions": open_positions, "skipped_entries": skipped,
            "metrics": performance(curve, trades, portfolio.initial_capital, sessions[0], sessions[-1]),
            "spy_metrics": performance(curve, [], portfolio.initial_capital, sessions[0], sessions[-1], field="spy_equity")}


def _rebalances(previous: str, day: str, cadence: str) -> bool:
    if cadence == "weekly":
        return date.fromisoformat(previous).isocalendar()[:2] != date.fromisoformat(day).isocalendar()[:2]
    return previous[:7] != day[:7]


def simulate_leaders(sessions: list[str], histories: dict[str, list[Bar]], market_states: dict[str, str | None],
                     ranking_on: Callable[[str], list[str]], thresholds: Thresholds, portfolio: BacktestSettings, *,
                     exposure: dict[str, float | None], settlements: dict[tuple[str, str], float] | None = None,
                     delisted: dict[str, str] | None = None, stale: dict[str, set[str]] | None = None,
                     exit_below: dict[str, set[str]] | None = None, progress=None) -> dict:
    """Hold the top-ranked leaders at equal weight, as many as the market's exposure allows.

    ranking_on(day) lists the stocks passing the Screen at that close, best first. At
    the next open: a rebalance (monthly or weekly) sells holdings that no longer pass
    or rank below leaders_rank_buffer; raise cash sells the lowest-ranked holdings
    above the exposure limit; open slots are filled from the top of the ranking.
    Stops use daily lows as in simulate(); there is no profit target, so winners are
    held while they remain leaders. A stopped-out stock is not rebought before the
    next rebalance.

    exit_below selects the trend exit instead: for each symbol, the sessions whose
    close was below its exit line. A holding is sold at the open after such a close
    or by portfolio.trend_loss_cap_pct; rebalances and raise cash sell nothing, so
    the market's exposure only limits new buying.
    """
    if len(sessions) < 2 or sessions != sorted(set(sessions)):
        raise BacktestError("Choose at least two distinct trading sessions in date order.")
    bars = {symbol: {b.date: b for b in values} for symbol, values in histories.items()}
    spy = bars.get("SPY", {})
    if any(day not in spy or not valid_bar(spy[day]) for day in sessions):
        raise BacktestError("SPY needs complete daily OHLCV for the selected period.")
    settlements, delisted, stale = settlements or {}, delisted or {}, stale or {}
    cash = portfolio.initial_capital
    spy_shares = cash / spy[sessions[0]].open
    holdings: dict[str, Holding] = {}
    trades, skipped, curve = [], [], []
    ranking: list[str] = []
    stopped: set[str] = set()
    trend = exit_below is not None
    stop_pct = portfolio.trend_loss_cap_pct if trend else thresholds.stop_loss_pct
    use_stop = stop_pct > 0 if trend else portfolio.stop_loss
    previous = None

    def close_position(symbol, day, price, reason):
        nonlocal cash
        holding = holdings.pop(symbol)
        cash += holding.shares * price
        trades.append({"symbol": symbol, "signal_date": holding.signal.date, "entry_date": holding.entry_date,
                       "entry_price": holding.entry_price, "exit_date": day, "exit_price": price,
                       "shares": holding.shares, "pnl": holding.shares * (price - holding.entry_price),
                       "return_pct": 100 * (price / holding.entry_price - 1), "reason": reason,
                       "pivot": holding.signal.pivot, "pattern": holding.signal.pattern,
                       "fast_gain_date": None, "evidence": holding.signal.evidence})

    for index, day in enumerate(sessions):
        for symbol, holding in list(holdings.items()):
            if delisted.get(symbol, "9999-12-31") <= day:
                value = settlements.get((symbol, delisted[symbol]))
                if value is None or not math.isfinite(value) or value < 0:
                    raise BacktestError(f"{symbol}: need documented delisting proceeds on {delisted[symbol]}.")
                close_position(symbol, day, value, "Delisting settlement")
                continue
            bar = bars.get(symbol, {}).get(day)
            if bar is None or not valid_bar(bar):
                raise BacktestError(f"{symbol}: missing or invalid held-position bar on {day}.")
            if use_stop and bar.open <= holding.entry_price * (1 - stop_pct / 100):
                close_position(symbol, day, bar.open, "Gap through stop")
                stopped.add(symbol)
            elif trend and previous in exit_below.get(symbol, ()):
                close_position(symbol, day, bar.open, f"Closed below the {thresholds.long_ma_sessions}-day line")
                stopped.add(symbol)

        rank = {symbol: i for i, symbol in enumerate(ranking)}
        if previous is not None and _rebalances(previous, day, portfolio.leaders_rebalance):
            stopped.clear()
            for symbol in [] if trend else list(holdings):
                if symbol not in rank:
                    close_position(symbol, day, bars[symbol][day].open, "Rebalance: no longer passes the Screen")
                elif rank[symbol] >= portfolio.leaders_rank_buffer:
                    close_position(symbol, day, bars[symbol][day].open,
                                   f"Rebalance: ranked below {portfolio.leaders_rank_buffer}")
        capacity = (math.floor(portfolio.max_holdings * (exposure.get(previous) or 0) / 100 + 1e-9)
                    if previous is not None else 0)
        if portfolio.raise_cash and not trend and len(holdings) > capacity:
            lowest = sorted(holdings, key=lambda s: (-rank.get(s, len(ranking)), s))
            for symbol in lowest[:len(holdings) - capacity]:
                close_position(symbol, day, bars[symbol][day].open,
                               f"Raise cash: market exposure {exposure.get(previous) or 0:g}%")

        opening_equity = cash + sum(h.shares * bars[symbol][day].open for symbol, h in holdings.items())
        allocation = opening_equity / portfolio.max_holdings
        for symbol in ranking:
            if len(holdings) >= capacity or cash + 1e-8 < allocation or allocation <= 0:
                break
            if symbol in holdings or symbol in stopped or delisted.get(symbol, "9999-12-31") <= day:
                continue
            bar = bars.get(symbol, {}).get(day)
            if bar is None or not valid_bar(bar) or day in stale.get(symbol, ()):
                skipped.append({"symbol": symbol, "signal_date": previous, "entry_date": day,
                                "reason": "Missing next-session open or valid OHLCV"})
                continue
            signal = Signal(symbol, previous, bar.open, len(ranking) - rank[symbol], "Leader", {"rank": rank[symbol] + 1})
            holdings[symbol] = Holding(signal, day, bar.open, allocation / bar.open)
            cash -= allocation

        for symbol, holding in list(holdings.items()):
            bar = bars[symbol][day]
            stop = holding.entry_price * (1 - stop_pct / 100)
            if use_stop and bar.low <= stop + 1e-9:
                close_position(symbol, day, min(bar.open, stop), "Stop loss")
                stopped.add(symbol)

        equity = cash + sum(h.shares * bars[symbol][day].close for symbol, h in holdings.items())
        curve.append({"date": day, "equity": equity, "cash": max(0.0, cash), "holdings": len(holdings),
                      "spy_equity": spy_shares * spy[day].close, "market_state": market_states.get(day),
                      "exposure": exposure.get(day)})
        ranking = ranking_on(day) if index < len(sessions) - 1 else []
        previous = day
        if progress:
            progress(day, index + 1, len(sessions))

    last = sessions[-1]
    open_positions = [{"symbol": symbol, "entry_date": h.entry_date, "entry_price": h.entry_price, "shares": h.shares,
                       "last_close": bars[symbol][last].close, "value": h.shares * bars[symbol][last].close,
                       "unrealized_pnl": h.shares * (bars[symbol][last].close - h.entry_price),
                       "fast_gain_date": None, "profit_order_pending": False}
                      for symbol, h in holdings.items()]
    return {"start": sessions[0], "end": last, "portfolio": asdict(portfolio), "thresholds": asdict(thresholds),
            "equity_curve": curve, "trades": trades, "open_positions": open_positions, "skipped_entries": skipped,
            "metrics": performance(curve, trades, portfolio.initial_capital, sessions[0], last),
            "spy_metrics": performance(curve, [], portfolio.initial_capital, sessions[0], last, field="spy_equity")}
