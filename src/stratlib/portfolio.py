"""Portfolio plans and recorded account values without UI or data downloads."""

import math
from datetime import date

from .positions import account_values, assess_position
from .strategies import INDEXES, candidate_rows, equal_weighted, holding_limit, rule_snapshot, strategy

# An index copy buys each holding's weight at 99.8% of its value, leaving cash for slippage (sim/msci_garp.py).
INDEX_CASH_BUFFER = 0.002


def portfolio_plan(store, report, strategy_id, thresholds, options, market, as_of, params=None):
    """Model plan for one strategy. ``params`` is the scan strategy's Settings (or its parameters)."""
    spec = strategy(strategy_id)
    records = [r for r in store.positions() if r["strategy_id"] == spec.id and r["variant_id"] == spec.variant]
    sessions = [bar.date for bar in store.price_history("SPY", through=as_of)]
    assessments = {r["id"]: assess_position(store, r, thresholds, as_of=as_of, sessions=sessions) for r in records}
    cash_record = store.document(f"portfolio:cash:{spec.id}:{spec.variant}")
    account = account_values(store, records, assessments, cash_record["amount"] if cash_record else None)
    issues = []
    if not report:
        issues.append("Run the screen first for this strategy.")
    else:
        if report.get("rule_snapshot") != rule_snapshot(spec.id, thresholds, options, params):
            issues.append("The screen uses older or legacy rules. Run or evaluate a screen with current settings.")
        if report.get("sample"):
            issues.append("The screen is a development sample. Run the full screen for a portfolio plan.")
        if (date.fromisoformat(as_of) - date.fromisoformat(report["price_date"])).days > 4:
            issues.append("Screen prices are stale. Update prices and run the screen.")
        if report["price_date"] != market.get("as_of"):
            issues.append("Screen and market dates differ. Refresh the screen before planning new entries.")
    exposure = market.get("exposure")
    if spec.uses_exposure and (exposure is None or not market.get("as_of")
                               or (date.fromisoformat(as_of) - date.fromisoformat(market["as_of"])).days > 4):
        issues.append("Current market exposure is unavailable or stale.")
    if any(a.action == "Unavailable" or not a.price_date or (sessions and a.price_date < sessions[-1])
           or (date.fromisoformat(as_of) - date.fromisoformat(a.price_date)).days > 4 for a in assessments.values()):
        issues.append("Some holdings cannot be assessed from current prices. Resolve them before planning entries.")
    # Scan strategies carry their own market filter inside the scan; only the ladder strategies scale slots.
    slots = holding_limit(spec.id, params, options)
    capacity = math.floor(slots * (exposure or 0) / 100 + 1e-9) if spec.uses_exposure else slots
    lots = {}
    for record in records:
        lots.setdefault(record["symbol"], []).append(record)
    # A ticker occupies its slot until every lot has an exit signal.
    kept = {s for s, rows in lots.items() if any(assessments[r["id"]].action not in {"Sell", "Take profits"} for r in rows)}
    trims = set()
    if strategy_id == "canslim" and options.raise_cash and not issues:
        weakest = sorted(kept, key=lambda s: (min(assessments[r["id"]].gain_pct or 0 for r in lots[s]), s))
        trims = set(weakest[:max(0, len(kept) - capacity)])
    free = max(0, capacity - len(kept - trims))
    leaders = report.get("candidates") if report else []
    if leaders is None:
        leaders = candidate_rows(report, strategy_id, thresholds, options)
    candidates, available = [], free
    for i, leader in enumerate(leaders, 1):
        symbol = leader["symbol"]
        action = "Held" if symbol in lots else "Review data" if issues else "Proposed buy" if available else "Watch"
        if action == "Proposed buy":
            available -= 1
        row = {"Rank": i, "Symbol": symbol, "Company": leader["name"], "Plan": action}
        if spec.scan:
            from .scanning import SCANNERS
            row["Initial weight, %"] = leader.get("weight_pct") if action == "Proposed buy" else None
            row.update({label: leader.get(key) for key, label in SCANNERS[spec.id].candidate_columns})
        else:
            row.update({"Initial slot, %": 100 / slots if action == "Proposed buy" else None,
                        "RS": leader["rs"], "Sales growth, %": leader["sales"], "Industry rank": leader["industry_rank"]})
        row["Close, USD"] = leader["close"]
        candidates.append(row)
    return {"records": records, "assessments": assessments, "account": account, "cash_record": cash_record,
            "issues": issues, "capacity": capacity, "free": free, "trims": trims, "candidates": candidates,
            "held_count": len(lots)}


def hypothetical_portfolio(report, strategy_id, thresholds, options, market, capital, *, params=None, price_of=None):
    """A fresh model portfolio from the strategy's buy list: what its rules would buy today with this capital.

    CANSLIM and Trend Leaders fill only as many slots as the market's allowed exposure permits; the scan strategies
    fill their own count, their market filter having already acted inside the scan. Each position gets an equal slot,
    or the research's risk-based weight for scans that size by risk. Shares are whole, priced at the latest close that
    ``price_of(symbol)`` returns as (date, close), or the screen's close; buy-stop scans are priced at their buy stop,
    since that is where they would fill. Whatever is left stays in cash. Nothing is saved.

    An index strategy buys as its backtest does: each holding at its weight in the index rounded to the nearest whole
    share, then, if that overspends, one share less of the largest holdings until it fits.
    """
    from .scanning import SCANNERS
    spec = strategy(strategy_id)
    buy_stop = spec.scan and SCANNERS[strategy_id].entry == "stop"
    slots = holding_limit(strategy_id, params, options)
    exposure = market.get("exposure")
    capacity = math.floor(slots * (exposure or 0) / 100 + 1e-9) if spec.uses_exposure else slots
    leaders = (report.get("candidates") if report else None)
    if report and leaders is None:
        leaders = candidate_rows(report, strategy_id, thresholds, options)
    leaders = leaders or []
    rows, skipped, cash = [], [], float(capital)
    index = strategy_id in INDEXES
    for rank, leader in enumerate(leaders, 1):
        if len(rows) >= capacity:
            break
        symbol = leader["symbol"]
        weight = leader.get("weight_pct") if spec.scan and not equal_weighted(strategy_id) else None
        weight = weight or 100 / slots
        if buy_stop and leader.get("pivot"):
            price_date, price = "buy stop", leader["pivot"]
        else:
            latest = price_of(symbol) if price_of else None
            price_date, price = latest if latest else (report["price_date"], leader.get("close"))
        if not price or price <= 0:
            skipped.append((symbol, "no cached close"))
            continue
        if index:
            shares = round(capital * weight / 100 * (1 - INDEX_CASH_BUFFER) / price)
            if shares < 1:
                skipped.append((symbol, "its weight is under half a share"))
                continue
        else:
            shares = math.floor(min(capital * weight / 100, cash) / price)
        if shares < 1:
            skipped.append((symbol, "one share costs more than its slot"))
            continue
        cost = shares * price
        cash -= cost
        rows.append({"rank": rank, "symbol": symbol, "name": leader.get("name"), "target_pct": weight,
                     "weight_pct": 100 * cost / capital, "shares": shares, "price": price, "price_date": price_date,
                     "cost": cost, "leader": leader})
    while index and cash < 0 and rows:             # rounding up overspent: one share less of the largest holding
        row = max(rows, key=lambda r: r["cost"])
        row["shares"] -= 1
        row["cost"] -= row["price"]
        cash += row["price"]
        if row["shares"] == 0:
            rows.remove(row)
            skipped.append((row["symbol"], "its weight is under half a share"))
    for row in rows:
        row["weight_pct"] = 100 * row["cost"] / capital
    invested = capital - cash
    return {"rows": rows, "skipped": skipped, "capital": capital, "invested": invested, "cash": cash,
            "slots": slots, "capacity": capacity, "exposure": exposure if spec.uses_exposure else None,
            "candidates": len(leaders), "buy_stop": buy_stop}
