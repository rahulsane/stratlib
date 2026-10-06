"""Pure end-of-day position rules, with explicit breakout evidence and dates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from .config import Thresholds
from .prices import Bar


def positive_price(value: float | None) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


@dataclass(frozen=True)
class Position:
    symbol: str
    entry_date: str
    entry_price: float
    breakout_date: str | None = None
    breakout_price: float | None = None
    stop_price: float | None = None     # recorded initial stop, split-adjusted; used by the scan strategies

    def __post_init__(self):
        symbol = self.symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
            raise ValueError("Enter a ticker using letters, numbers, dots or hyphens.")
        object.__setattr__(self, "symbol", symbol)
        try:
            entry = date.fromisoformat(self.entry_date)
            if entry.isoformat() != self.entry_date:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Entry date must be a valid YYYY-MM-DD date.") from None
        if not positive_price(self.entry_price):
            raise ValueError("Entry price must be a finite number greater than zero.")
        if self.stop_price is not None and not positive_price(self.stop_price):
            raise ValueError("Stop price must be a finite number greater than zero.")
        if (self.breakout_date is None) != (self.breakout_price is None):
            raise ValueError("Provide both the breakout date and its reference price, or neither.")
        if self.breakout_date is not None:
            try:
                breakout = date.fromisoformat(self.breakout_date)
                if breakout.isoformat() != self.breakout_date or breakout > entry:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("Breakout date must be valid and on or before entry.") from None
            if not positive_price(self.breakout_price):
                raise ValueError("Breakout reference price must be a finite number greater than zero.")


@dataclass(frozen=True)
class SellAssessment:
    action: str
    reason: str
    price_date: str | None = None
    close: float | None = None
    gain_pct: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    fast_gain_date: str | None = None
    hold_until: str | None = None
    exception_status: str = "Unverified"
    trend_line: float | None = None


def below_trend_line(close: float, line: float) -> bool:
    return positive_price(close) and positive_price(line) and close < line


def evaluate_sell_rules(position: Position, bars: Sequence[Bar], thresholds: Thresholds,
                        *, as_of: str, sessions: Sequence[str] | None = None) -> SellAssessment:
    """Evaluate completed closes only, never future bars or intraday extremes.

    Prices must share the position's split-adjustment basis. Calendar weeks are
    inclusive at the fast-gain boundary; the hold expires exactly eight weeks
    after the breakout. Unknown breakout evidence never asserts a normal hold
    or a profit exit while an eight-week exception could apply.
    """
    date.fromisoformat(as_of)
    ordered = sorted((b for b in bars if b.date <= as_of), key=lambda b: b.date)
    if position.entry_date > as_of:
        return SellAssessment("Unavailable", "No completed session on or after entry yet.")
    if not ordered or ordered[-1].date < position.entry_date:
        return SellAssessment("Unavailable", "No cached close on or after entry. Run the daily backfill.")
    latest = ordered[-1]
    if not positive_price(latest.close):
        return SellAssessment("Unavailable", "Latest cached close is invalid. Refresh price history.")
    gain = (latest.close / position.entry_price - 1) * 100
    stop = position.entry_price * (1 - thresholds.stop_loss_pct / 100)
    target = position.entry_price * (1 + thresholds.profit_target_pct / 100)
    details = dict(price_date=latest.date, close=latest.close, gain_pct=gain,
                   stop_price=stop, target_price=target)
    # Compare in price space to avoid percentage rounding at exact boundaries.
    if latest.close <= stop or math.isclose(latest.close, stop, rel_tol=1e-12):
        return SellAssessment("Sell", f"Close reached the {thresholds.stop_loss_pct:g}% loss limit. "
                              "The stop takes priority over any hold exception.", **details)

    status, fast_date, hold_until = "Unverified", None, None
    if date.fromisoformat(latest.date) >= date.fromisoformat(position.entry_date) + timedelta(weeks=thresholds.minimum_hold_weeks):
        # A breakout must precede entry, so even an unknown breakout's hold has expired.
        status = "Expired"
    if position.breakout_date is not None:
        start = date.fromisoformat(position.breakout_date)
        deadline = start + timedelta(weeks=thresholds.fast_gain_weeks)
        expiry = start + timedelta(weeks=thresholds.minimum_hold_weeks)
        hold_until = expiry.isoformat()
        required = position.breakout_price * (1 + thresholds.fast_gain_pct / 100)
        early = [b for b in ordered if position.breakout_date <= b.date <= deadline.isoformat()]
        fast_date = next((b.date for b in early if positive_price(b.close)
                          and (b.close >= required or math.isclose(b.close, required, rel_tol=1e-12))), None)
        if fast_date:
            status = "Active" if date.fromisoformat(latest.date) < expiry else "Expired"
        elif date.fromisoformat(latest.date) >= expiry:
            # Missing early evidence cannot change a hold that has already expired.
            status = "Expired"
        else:
            # A market session calendar distinguishes holidays from missing stock bars.
            end = min(deadline.isoformat(), latest.date)
            expected = {d for d in sessions or [] if position.breakout_date <= d <= end}
            observed = {b.date for b in early if positive_price(b.close)}
            complete = bool(expected) and position.breakout_date in observed and expected <= observed
            status = ("Watching" if end < deadline.isoformat() else "Not qualified") if complete else "Unverified"
    details.update(fast_gain_date=fast_date, hold_until=hold_until, exception_status=status)
    if status == "Active":
        return SellAssessment("Hold exception", f"Reached {thresholds.fast_gain_pct:g}% from the breakout "
                              f"within {thresholds.fast_gain_weeks} weeks. Hold until {hold_until}; "
                              "the loss limit still applies.", **details)
    if latest.close >= target or math.isclose(latest.close, target, rel_tol=1e-12):
        if status in {"Unverified", "Watching"}:
            return SellAssessment("Review breakout", "Profit target reached. Confirm the breakout date, "
                                  "reference price and early price history before applying the profit rule.", **details)
        return SellAssessment("Take profits", f"Close reached the {thresholds.profit_target_pct:g}% profit target "
                              f"and no active {thresholds.minimum_hold_weeks}-week hold applies.", **details)
    reason = "Close is between the loss limit and profit target."
    if status == "Unverified":
        reason += " Breakout evidence is incomplete; the hold exception is unverified."
    return SellAssessment("Hold", reason, **details)
