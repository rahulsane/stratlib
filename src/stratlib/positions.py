"""Position persistence and cache wiring; the sell rules remain pure."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .config import ConfigError, Thresholds
from .exits import MEMBERSHIP, assess_scan_position, lead_in_days
from .sell_rules import Position, SellAssessment, below_trend_line, evaluate_sell_rules, positive_price
from .strategies import latest_screen, read_params, read_rules, strategy


def position_from_record(record: dict) -> Position:
    return Position(**{key: record.get(key) for key in
                       ("symbol", "entry_date", "entry_price", "breakout_date", "breakout_price", "stop_price")})


def _restate(position: Position, factor: float) -> Position:
    """Scale every recorded price by a later split's factor."""
    return replace(position, entry_price=position.entry_price * factor,
                   breakout_price=position.breakout_price * factor if position.breakout_price else None,
                   stop_price=position.stop_price * factor if position.stop_price else None)


def assess_position(store, record: dict, thresholds: Thresholds, *, as_of: str,
                    sessions: list[str] | None = None) -> SellAssessment:
    position = position_from_record(record)
    spec_id = record.get("strategy_id", "canslim")
    options = params = None
    if record.get("rule_snapshot"):
        try:
            if strategy(spec_id).scan:
                spec, params, _ = read_params(record["rule_snapshot"])
            else:
                spec, thresholds, options = read_rules(record["rule_snapshot"])
            if spec.id != spec_id or spec.variant != record.get("variant_id"):
                raise ValueError("Position metadata does not match its saved rules")
        except (KeyError, TypeError, ValueError, ConfigError) as exc:
            return SellAssessment("Unavailable", f"Saved rules cannot be read: {exc}. Edit the position to assign rules.")
    elif spec_id != "canslim":
        return SellAssessment("Unavailable", "Saved strategy rules are missing. Edit the position to assign rules.")
    if position.entry_date > as_of:
        return SellAssessment("Unavailable", "The entry is after the latest completed session.")
    anchor_date, anchor_close = record["basis_date"], record["basis_close"]
    if anchor_date is None:
        return SellAssessment("Unavailable", "Price basis was unavailable when saved. Backfill this ticker, "
                              "then edit and save the position using split-adjusted prices.")
    anchor = store.price_history(position.symbol, since=anchor_date, through=anchor_date)
    if not anchor or not positive_price(anchor[0].close) or not positive_price(anchor_close):
        return SellAssessment("Unavailable", "The saved price reference is missing. Backfill history, then "
                              "edit and save the position to confirm its split-adjusted prices.")
    # A later split restates this same cached bar and all older prices. Scale both
    # cost and breakout reference by that ratio, leaving the original inputs intact.
    factor = anchor[0].close / anchor_close
    position = _restate(position, factor)
    if params is not None:
        start = (date.fromisoformat(position.entry_date) - timedelta(days=lead_in_days(spec_id))).isoformat()
        screen = latest_screen(store, spec_id) if spec_id in MEMBERSHIP else None
        if spec_id in MEMBERSHIP and sessions is None:
            sessions = [bar.date for bar in store.price_history("SPY", through=as_of)]
        return assess_scan_position(spec_id, params, position, store.price_history(position.symbol, since=start, through=as_of),
                                    as_of=as_of, screen=screen, sessions=sessions)
    if spec_id == "trend":
        n = thresholds.long_ma_sessions
        bars = store.price_history(position.symbol, limit=n, through=as_of)
        if len(bars) < n or any(not positive_price(b.close) for b in bars) or bars[-1].date < position.entry_date:
            return SellAssessment("Unavailable", f"Need {n} cached closes through an active holding date. Run the backfill.")
        close, line = bars[-1].close, sum(b.close for b in bars) / n
        cap = options.trend_loss_cap_pct
        stop = position.entry_price * (1 - cap / 100) if cap else None
        loss = stop is not None and close <= stop
        sell = loss or below_trend_line(close, line)
        reason = (f"Closed at least {cap:g}% below entry." if loss else
                  f"Closed below the saved {n}-session moving average." if sell else
                  f"Above or at the saved {n}-session moving average.")
        return SellAssessment("Sell" if sell else "Hold", reason, price_date=bars[-1].date, close=close,
                              gain_pct=100 * (close / position.entry_price - 1), stop_price=stop,
                              exception_status="Not applicable", trend_line=line)
    bars = store.price_history(position.symbol, since=position.breakout_date or position.entry_date, through=as_of)
    return evaluate_sell_rules(position, bars, thresholds, as_of=as_of, sessions=sessions)


def current_position(store, record: dict) -> Position:
    """Return input prices on the cache's current basis for the edit form."""
    position = position_from_record(record)
    if record["basis_date"] is None:
        return position
    anchor = store.price_history(position.symbol, since=record["basis_date"], through=record["basis_date"])
    if anchor and positive_price(anchor[0].close) and positive_price(record["basis_close"]):
        return _restate(position, anchor[0].close / record["basis_close"])
    return position


def current_quantity(store, record: dict) -> float | None:
    """Shares on the same current split basis as current_position's cost."""
    quantity = record.get("quantity")
    if quantity is None:
        return None
    adjusted = current_position(store, record)
    return quantity * record["entry_price"] / adjusted.entry_price


def account_values(store, records: list[dict], assessments: dict, cash: float | None) -> dict:
    """No inferred zero quantities or complete totals when any valuation is unknown."""
    values = {}
    for record in records:
        qty, result = current_quantity(store, record), assessments[record["id"]]
        values[record["id"]] = qty * result.close if qty is not None and result.close is not None else None
    complete = all(value is not None for value in values.values())
    holdings = sum(values.values()) if complete else None
    total = holdings + cash if holdings is not None and cash is not None else None
    return {"values": values, "holdings": holdings, "cash": cash, "total": total,
            "weights": {key: 100 * value / total if total and value is not None else None for key, value in values.items()}}
