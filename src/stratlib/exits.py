"""Sell alerts for positions under the scan strategies, from completed daily closes.

The exit rules are the research strategies' (research/strategies/*.py, exit_replay.py), replayed
session by session from the entry date. Alerts use completed closes only: a stop is reached when a
close is at or below it, a trailing average is broken by a close under it. The research engine
also fills stops at intraday lows; here an intraday touch is not an execution, as in the CANSLIM
alerts. Partial sales are reported as "Take profits" on the session they fall due.

The initial stop is the recorded stop if the holding has one, otherwise the strategy's own rule:
  Qullamaggie      the low of the session before entry
  Minervini        max_stop_pct below entry (the pattern's low is not stored; record the stop for exactness)
  Episodic Pivot   the low of the entry session
  9/21 EMA         stop_pct below the 21-day EMA at the entry session
  Checklist        stop_pct (20%) below entry, not trailed
  Nash             no stop
  MSCI GARP        no stop

The two statement-based strategies also rebalance quarterly. The research keeps a holding while it is in the
selected set at each rebalance (the first session of January, April, July and October) and sells it otherwise,
so their alerts compare the holding with the latest saved screen's ``members``. MSCI GARP copies an index whose reviews
take effect at the last session of February, May, August and November; its screen's ``members`` are the index's
holdings: the iShares GARP ETF's published holdings, so a holding missing from them is sold, or, when those could
not be loaded, the rebuild's, with stocks that left the S&P 500 since the review listed among its ``skipped``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .sell_rules import Position, SellAssessment, positive_price

NOT_APPLICABLE = "Not applicable"
MEMBERSHIP = ("tt_checklist", "nash_quality", "msci_garp")


@dataclass
class Replay:
    action: str            # Sell | Take profits | Hold
    reason: str
    date: str | None = None
    partial_date: str | None = None
    target: float | None = None


def sma(values: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(values).rolling(n, min_periods=n).mean().to_numpy()


def lead_in_days(strategy_id: str) -> int:
    """Calendar days of history before entry that the exit averages need."""
    return {"ema_pullback": 320, "minervini": 160, "tt_checklist": 10, "nash_quality": 10, "msci_garp": 10}.get(strategy_id, 120)


def rebalance_sessions(sessions: list[str]) -> list[str]:
    """The first session of each calendar quarter, when the statement-based strategies rebalance."""
    return [d for i, d in enumerate(sessions) if i > 0 and d[5:7] in ("01", "04", "07", "10") and sessions[i - 1][:7] != d[:7]]


def next_rebalance(as_of: str) -> str:
    """First day of the next quarter month after ``as_of``: the session on or after it is the rebalance."""
    year, month = int(as_of[:4]), int(as_of[5:7])
    month = next(m for m in (1, 4, 7, 10, 13) if m > month)
    return f"{year + month // 13}-{(month - 1) % 12 + 1:02d}-01"


def _below(close, line, i) -> bool | None:
    value = line[i]
    return None if not math.isfinite(value) else bool(close[i] < value)


def walk(dates, bars: dict, entry_idx: int, entry: float, stop: float, rule: dict, *,
         breakout_volume: float | None = None) -> Replay:
    """Replay one rule from the entry session to the newest close. bars holds high/low/close/volume arrays."""
    close, high, volume = bars["close"], bars["high"], bars["volume"]
    last = len(close) - 1
    kind, state = rule["kind"], {}
    line = ema_line(bars, rule["n"]) if kind == "ema" else sma(close, rule["n"]) if "n" in rule else None
    target = entry * (1 + rule["pct"] / 100) if kind == "partial_pct" else None
    avg_before = sma(volume, 50) if breakout_volume else None
    for i in range(entry_idx, last + 1):
        age, day = i - entry_idx, dates[i]
        if close[i] <= stop:
            return Replay("Sell", f"Closed at {close[i]:,.2f}, at or below the stop of {stop:,.2f}, on {day}.", day)
        if breakout_volume and age == 0:
            average = avg_before[i - 1] if i > 0 else np.nan
            if not (math.isfinite(average) and math.isfinite(volume[i]) and volume[i] >= breakout_volume * average):
                return Replay("Sell", f"Breakout-day volume was under {breakout_volume:g} x the 50-day average on {day}; "
                              "the research sells such breakouts at that close.", day)
        if kind == "partial_day":
            if "partial" not in state:
                if age >= rule["day"]:
                    state["partial"] = i
                    if rule.get("require_profit") and not close[i] > entry:
                        state["skipped"] = True      # below entry: no sale, original stop stays, trail from the next session
                    else:
                        stop = max(stop, entry)
                        if i == last:
                            return Replay("Take profits", f"Day {rule['day']} close: sell {rule['fraction']:.0%} and raise the "
                                          f"stop on the rest to the entry price ({entry:,.2f}).", day, day)
            elif i > state["partial"]:
                broke = _below(close, line, i)
                if broke is None:
                    return Replay("Unavailable", f"Need {rule['n']} closes for the trailing average.")
                if broke:
                    return Replay("Sell", f"Closed below the {rule['n']}-day average on {day}; sell the rest.", day,
                                  dates[state["partial"]])
        elif kind == "partial_pct":
            if "partial" not in state and age >= 1 and high[i] >= target:
                state["partial"] = i
                if i == last:
                    return Replay("Take profits", f"The high reached the {rule['pct']:g}% target ({target:,.2f}): sell "
                                  f"{rule['fraction']:.0%}.", day, day, target)
            if "partial" in state:
                broke = _below(close, line, i)
                if broke is None:
                    return Replay("Unavailable", f"Need {rule['n']} closes for the trailing average.")
                if broke:
                    return Replay("Sell", f"Closed below the {rule['n']}-day average on {day}; sell the rest.", day,
                                  dates[state["partial"]], target)
        elif kind == "time":
            if age >= rule["days"]:
                return Replay("Sell", f"Time exit: session {rule['days']} after entry ({day}).", day)
        elif kind == "time_then_sma":
            if age >= rule["days"]:
                broke = _below(close, line, i)
                if broke is None:
                    return Replay("Unavailable", f"Need {rule['n']} closes for the trailing average.")
                if broke:
                    return Replay("Sell", f"Closed below the {rule['n']}-day average after session {rule['days']} ({day}).", day)
        elif kind == "ema":
            if i > entry_idx and math.isfinite(line[i]):
                if close[i] < line[i]:
                    state["below"] = state.get("below", 0) + 1
                    state["below_at"] = i
                    if rule["exit_rule"] == "first" or state["below"] >= 2:
                        which = "first" if rule["exit_rule"] == "first" else "second"
                        return Replay("Sell", f"{which.capitalize()} close below the {rule['n']}-day EMA on {day}.", day)
                elif "below_at" in state and i - state["below_at"] > rule["reset_sessions"]:
                    state["below"] = 0
    note = {"partial_day": "", "partial_pct": "", "time": f"Time exit at session {rule.get('days')}.",
            "time_then_sma": f"Trail starts at session {rule.get('days')}.", "ema": ""}[kind]
    state_text = ""
    if "partial" in state:
        state_text = (f" The {rule['fraction']:.0%} partial sale fell due on {dates[state['partial']]}"
                      + (" but the close was not above entry, so the rules skip it." if state.get("skipped") else "."))
    return Replay("Hold", ("No exit condition at the latest close." + state_text + (" " + note if note else "")).strip(),
                  None, dates[state["partial"]] if "partial" in state else None, target)


def exit_rule(strategy_id: str, p) -> dict:
    """The research exit rule a saved parameter set selects."""
    if strategy_id == "qullamaggie":
        return {"kind": "partial_day", "day": p.partial_session, "fraction": p.partial_fraction, "n": p.trail_sma}
    if strategy_id == "minervini":
        if p.exit == "c10":
            return {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10}
        return {"kind": "partial_pct", "pct": 20.0, "fraction": 0.5, "n": 50}
    if strategy_id == "episodic_pivot":
        return {"c10": {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10, "require_profit": True},
                "time20": {"kind": "time", "days": 20},
                "trail10": {"kind": "time_then_sma", "days": 20, "n": 10}}[p.exit]
    if strategy_id == "ema_pullback":
        return {"kind": "ema", "n": p.ema_slow, "exit_rule": p.exit_rule, "reset_sessions": p.reset_sessions}
    raise ValueError(f"{strategy_id} has no exit rule")


def initial_stop(strategy_id: str, p, position: Position, bars: dict, entry_idx: int) -> tuple[float | None, str]:
    """(stop, where it came from) for a holding, on the cache's current split basis."""
    if position.stop_price is not None:
        return position.stop_price, "recorded stop"
    if strategy_id == "qullamaggie":
        if entry_idx < 1 or not math.isfinite(bars["low"][entry_idx - 1]):
            return None, "the low of the session before entry is not cached"
        return float(bars["low"][entry_idx - 1]), "low of the session before entry"
    if strategy_id == "minervini":
        return position.entry_price * (1 - p.max_stop_pct / 100), f"{p.max_stop_pct:g}% below entry"
    if strategy_id == "episodic_pivot":
        return float(bars["low"][entry_idx]), "low of the entry session"
    if strategy_id == "ema_pullback":
        line = pd.Series(bars["close"]).ewm(span=p.ema_slow, adjust=False, min_periods=p.ema_slow).mean().to_numpy()
        if not math.isfinite(line[entry_idx]):
            return None, f"the {p.ema_slow}-day EMA needs more cached closes"
        return float(line[entry_idx] * (1 - p.stop_pct / 100)), f"{p.stop_pct:g}% below the {p.ema_slow}-day EMA at entry"
    raise ValueError(f"{strategy_id} has no stop rule")


def ema_line(bars: dict, span: int) -> np.ndarray:
    return pd.Series(bars["close"]).ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


def review_sessions(sessions: list[str]) -> list[str]:
    """The sessions on which MSCI GARP's reviews took effect: the last on or before each review month's end."""
    from .msci_garp import schedule
    if not sessions:
        return []
    dates = np.array(sessions)
    return [sessions[r["effective"]] for r in schedule(dates, int(sessions[0][:4]))
            if r["effective"] >= 0 and sessions[r["effective"]][:7] == r["label"]]


def assess_index_position(position: Position, details: dict, screen: dict | None, sessions: list[str],
                          last_date: str) -> SellAssessment:
    """Alert for a holding copied from the MSCI GARP index: whether the index still holds it."""
    from .msci_garp import next_review
    upcoming = next_review(last_date)
    if screen is None or "members" not in screen:
        return SellAssessment("Hold", "Run the screen to check whether the index still holds this stock.", **details)
    summary = screen.get("summary") or {}
    if summary.get("source") == "ishares":
        if position.symbol in screen["members"]:
            return SellAssessment("Hold", f"Held by the index in iShares' holdings of {summary.get('as_of')} (screen of "
                                  f"{screen['price_date']}).", hold_until=upcoming, **details)
        return SellAssessment("Sell", f"The index no longer holds it: iShares' holdings of {summary.get('as_of')} leave it "
                              "out. A copy of the index sells it.", **details)
    left = next((s for s in screen.get("skipped") or [] if s.get("symbol") == position.symbol), None)
    if left and position.entry_date <= left["signal_date"]:
        return SellAssessment("Sell", f"It left the S&P 500, and the index sold it at the close of {left['signal_date']}.",
                              **details)
    if position.symbol in screen["members"]:
        return SellAssessment("Hold", f"Still held by the index in the screen of {screen['price_date']}.",
                              hold_until=upcoming, **details)
    reviews = [d for d in review_sessions(sessions) if d <= last_date]
    due = reviews[-1] if reviews else None
    if due and position.entry_date < due and screen["price_date"] >= due:
        return SellAssessment("Sell", f"Not held by the index after its review of {due} (screen of "
                              f"{screen['price_date']}). A copy of the index sells it at that review.", **details)
    if due and screen["price_date"] < due:
        return SellAssessment("Hold", f"The latest screen ({screen['price_date']}) predates the review of {due}. Run the "
                              "screen to check this holding.", hold_until=upcoming, **details)
    return SellAssessment("Hold", f"Not held by the index in the screen of {screen['price_date']}. A copy of the index "
                          "sells it at the next review (the last session on or before the date shown).",
                          hold_until=upcoming, **details)


def assess_member_position(strategy_id: str, params, position: Position, ordered: list, screen: dict | None,
                           sessions: list[str]) -> SellAssessment:
    """Alert for a rebalanced holding: its stop, then whether the latest screen still selects it."""
    last = ordered[-1]
    details = {"price_date": last.date, "close": last.close, "gain_pct": 100 * (last.close / position.entry_price - 1),
               "exception_status": NOT_APPLICABLE}
    if strategy_id == "msci_garp":
        return assess_index_position(position, details, screen, sessions, last.date)
    stop = None
    if strategy_id == "tt_checklist":
        stop = position.stop_price if position.stop_price is not None else position.entry_price * (1 - params.stop_pct / 100)
        details["stop_price"] = stop
        for bar in ordered:
            if bar.date >= position.entry_date and bar.close <= stop:
                return SellAssessment("Sell", f"Closed at {bar.close:,.2f}, at or below the stop of {stop:,.2f}, on {bar.date}."
                                      + (" The signal is still open: the position has not been marked closed."
                                         if bar.date < last.date else ""), **details)
    if screen is None or "members" not in screen:
        return SellAssessment("Hold", "Run the screen to check whether the strategy still selects this stock.", **details)
    upcoming = next_rebalance(last.date)
    if position.symbol in screen["members"]:
        return SellAssessment("Hold", f"Still selected in the screen of {screen['price_date']}.", hold_until=upcoming, **details)
    rebalances = [d for d in rebalance_sessions(sessions) if d <= last.date]
    due = rebalances[-1] if rebalances else None
    if due and position.entry_date < due and screen["price_date"] >= due:
        return SellAssessment("Sell", f"Not selected in the screen of {screen['price_date']}. The research sells holdings "
                              f"outside the selection at each quarterly rebalance, and the last was {due}.", **details)
    if due and screen["price_date"] < due:
        return SellAssessment("Hold", f"The latest screen ({screen['price_date']}) predates the {due} rebalance. Run the "
                              "screen to check this holding.", hold_until=upcoming, **details)
    return SellAssessment("Hold", f"Not selected in the screen of {screen['price_date']}. The research sells it at the next "
                          "quarterly rebalance (the first session on or after the date shown).", hold_until=upcoming, **details)


def assess_scan_position(strategy_id: str, params, position: Position, history: list, *, as_of: str,
                         screen: dict | None = None, sessions: list[str] | None = None) -> SellAssessment:
    """Alert for a holding. ``history`` holds cached bars through ``as_of`` on the position's price basis."""
    ordered = sorted((b for b in history if b.date <= as_of and positive_price(b.close)), key=lambda b: b.date)
    if position.entry_date > as_of or not ordered or ordered[-1].date < position.entry_date:
        return SellAssessment("Unavailable", "No cached close on or after entry. Run the daily backfill.")
    if strategy_id in MEMBERSHIP:
        return assess_member_position(strategy_id, params, position, ordered, screen, sessions or [])
    dates = [b.date for b in ordered]
    nan = float("nan")
    bars = {"close": np.array([b.close for b in ordered]),
            "high": np.array([b.high if b.high is not None else nan for b in ordered]),
            "low": np.array([b.low if b.low is not None else nan for b in ordered]),
            "volume": np.array([b.volume if b.volume is not None else nan for b in ordered])}
    entry_idx = next(i for i, d in enumerate(dates) if d >= position.entry_date)
    stop, source = initial_stop(strategy_id, params, position, bars, entry_idx)
    if stop is None or not math.isfinite(stop):
        return SellAssessment("Unavailable", "The initial stop cannot be derived: "
                              f"{source if stop is None else 'the session low is missing'}. Record the stop on the position.")
    if stop >= position.entry_price:
        return SellAssessment("Unavailable", f"The stop ({stop:,.2f}, {source}) is not below the entry price. "
                              "Check the entry price or record the stop.")
    rule = exit_rule(strategy_id, params)
    volume = params.breakout_volume if strategy_id == "minervini" else None
    replay = walk(dates, bars, entry_idx, position.entry_price, stop, rule, breakout_volume=volume)
    last = len(dates) - 1
    line = None
    if rule["kind"] == "ema":
        line = ema_line(bars, rule["n"])[last]
    elif "n" in rule:
        line = sma(bars["close"], rule["n"])[last]
    line = float(line) if line is not None and math.isfinite(line) else None
    earlier = replay.date is not None and replay.date < dates[last] and replay.action == "Sell"
    reason = replay.reason + (" The signal is still open: the position has not been marked closed." if earlier else "")
    return SellAssessment(replay.action, f"{reason} Stop: {source}." if replay.action in {"Hold", "Take profits"} else reason,
                          price_date=dates[last], close=float(bars["close"][last]),
                          gain_pct=100 * (float(bars["close"][last]) / position.entry_price - 1),
                          stop_price=stop, target_price=replay.target, exception_status=NOT_APPLICABLE, trend_line=line)
