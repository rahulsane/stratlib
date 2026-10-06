"""Portfolio page rules: trend leaders from the saved screen, the trend exit and open slots."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from stratlib.config import BacktestSettings, Thresholds
from stratlib.strategies import candidate_rows, rule_snapshot
from stratlib.positions import assess_position


def trend_leaders(report, t, options):
    return candidate_rows(report, "trend", t, options)


def holding_status(store, records, t, options, as_of):
    # Explicitly assign the tested trend rules; these synthetic lots have no user settings.
    record = {**records[0], "strategy_id": "trend", "variant_id": "long-trend",
              "rule_snapshot": rule_snapshot("trend", t, options)}
    result = assess_position(store, record, t, as_of=as_of)
    return {"action": result.action, "line": result.trend_line, "gain_pct": result.gain_pct, "why": result.reason}
from stratlib.prices import Bar
from stratlib.scoring import Criterion

T = Thresholds(industry_top=40, long_ma_sessions=5)
P = BacktestSettings(trend_min_rs=90, trend_min_sales_growth_pct=20)
NOW = datetime(2026, 9, 28, 22, tzinfo=timezone.utc)


def row(symbol, rs, sales, *, rank=5, price_pass=True, scored=True, close=50.0, volume=1e6):
    criteria = [Criterion("c_sales", "C", "Quarterly sales growth", sales, None, "").to_dict()] if scored else []
    return {"symbol": symbol, "name": f"{symbol} Inc", "industry": "Tech", "rs": rs, "criteria": criteria,
            "industry_stats": {"rank": rank}, "prices": {"close": close, "avg_volume": volume, "price_pass": price_pass}}


def test_trend_leaders_apply_the_backtest_rule_to_the_saved_screen():
    report = {"rows": [row("AAA", 95, 30), row("BBB", 99, 25), row("CCC", 99, 60), row("LOW", 89, 90),
                       row("SLOW", 99, 10), row("WEAK", 99, 50, rank=41), row("FAIL", 99, 50, price_pass=False),
                       row("NEW", 99, 50, scored=False), row("THIN", 99, 50, volume=1e5)]}
    leaders = trend_leaders(report, T, P)
    # RS first, then sales growth; unscored, failing and weak-group rows are left out.
    assert [l["symbol"] for l in leaders] == ["CCC", "THIN", "BBB", "AAA"]
    assert leaders[0]["sales"] == 60 and leaders[0]["industry_rank"] == 5
    floor = trend_leaders(report, T, replace(P, trend_min_dollar_volume_m=10))
    assert "THIN" not in [l["symbol"] for l in floor]


def lot(store, symbol, closes, cost):
    from stratlib.sell_rules import Position
    day, bars = date(2026, 9, 1), []
    for close in closes:
        bars.append(Bar(day.isoformat(), close, close + 1, close - 1, close, 1_000_000))
        day += timedelta(days=1)
    store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date, checked_at=NOW, status="ok")
    store.save_position(Position(symbol, bars[0].date, cost), as_of=bars[-1].date)
    return bars[-1].date


def test_holdings_sell_only_below_the_line_or_at_the_loss_cap(store):
    as_of = lot(store, "UP", [10, 11, 12, 13, 14], 10)
    status = holding_status(store, store.positions(), T, P, as_of)
    assert (status["action"], status["line"], round(status["gain_pct"])) == ("Hold", 12, 40)
    lot(store, "DOWN", [14, 13, 12, 11, 10], 14)
    down = [r for r in store.positions() if r["symbol"] == "DOWN"]
    assert holding_status(store, down, T, P, as_of)["action"] == "Sell"          # 10 < line of 12
    lot(store, "DIP", [20, 20, 20, 20, 20.5], 25)
    dip = [r for r in store.positions() if r["symbol"] == "DIP"]
    assert holding_status(store, dip, T, P, as_of)["action"] == "Hold"            # no cap by default
    capped = holding_status(store, dip, T, replace(P, trend_loss_cap_pct=15), as_of)
    assert (capped["action"], capped["why"]) == ("Sell", "Closed at least 15% below entry.")
    lot(store, "NEW", [10, 11], 10)
    new = [r for r in store.positions() if r["symbol"] == "NEW"]
    assert holding_status(store, new, T, P, as_of)["action"] == "Unavailable"



def test_a_hypothetical_portfolio_fills_the_slots_the_market_allows_in_whole_shares():
    from stratlib.portfolio import hypothetical_portfolio
    report = {"price_date": "2026-09-25", "candidates": [
        {"symbol": "AAA", "name": "A", "close": 100.0}, {"symbol": "BBB", "name": "B", "close": 33.0},
        {"symbol": "CCC", "name": "C", "close": 7.0}]}
    options = BacktestSettings(max_holdings=10)
    # 20% exposure allows 2 of 10 equal slots; the rest stays in cash.
    plan = hypothetical_portfolio(report, "trend", T, options, {"exposure": 20}, 100_000)
    assert [(r["symbol"], r["shares"]) for r in plan["rows"]] == [("AAA", 100), ("BBB", 303)]
    assert plan["capacity"] == 2 and plan["cash"] == 100_000 - 100 * 100.0 - 303 * 33.0
    assert plan["invested"] + plan["cash"] == 100_000
    # The latest cached close replaces the screen's close when there is one.
    plan = hypothetical_portfolio(report, "trend", T, options, {"exposure": 100}, 100_000,
                                  price_of=lambda symbol: ("2026-09-29", 50.0) if symbol == "AAA" else None)
    assert [(r["symbol"], r["shares"], r["price_date"]) for r in plan["rows"]][0] == ("AAA", 200, "2026-09-29")
    assert len(plan["rows"]) == 3
    # No allowed exposure, no positions.
    assert hypothetical_portfolio(report, "trend", T, options, {"exposure": 0}, 100_000)["rows"] == []
    # A $50 slot is too small for one $100 share, which is skipped and named; the others fit.
    plan = hypothetical_portfolio(report, "trend", T, options, {"exposure": 100}, 500)
    assert [r["symbol"] for r in plan["rows"]] == ["BBB", "CCC"] and plan["skipped"] == [("AAA", "one share costs more than its slot")]


def test_a_scan_strategy_uses_its_research_weights_and_ignores_the_exposure_ladder():
    from stratlib.portfolio import hypothetical_portfolio
    report = {"price_date": "2026-09-25", "candidates": [
        {"symbol": "AAA", "name": "A", "close": 10.0, "weight_pct": 6.0},
        {"symbol": "BBB", "name": "B", "close": 20.0, "weight_pct": 12.5}]}
    plan = hypothetical_portfolio(report, "ema_pullback", T, BacktestSettings(max_holdings=10), {"exposure": 0}, 10_000)
    assert [(r["symbol"], r["shares"]) for r in plan["rows"]] == [("AAA", 60), ("BBB", 62)]
    assert plan["exposure"] is None
    # A buy-stop strategy is sized at its buy stop, where it would fill, not at the last close.
    report["candidates"][0]["pivot"] = 12.0
    plan = hypothetical_portfolio(report, "minervini", T, BacktestSettings(max_holdings=10), {"exposure": 0}, 10_000)
    assert (plan["rows"][0]["price"], plan["rows"][0]["price_date"], plan["rows"][0]["shares"]) == (12.0, "buy stop", 50)
