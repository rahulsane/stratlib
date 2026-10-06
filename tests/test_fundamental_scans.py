"""Nash and the Traveling Trader checklist: scans, statement refresh and quarterly-rebalance alerts.

All companies and prices are explicitly synthetic. The statement rules are checked against the research code
in test_fundamental_parity.py.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import numpy as np
import pytest

from stratlib import fundamental_scans as fs
from stratlib.exits import assess_scan_position, next_rebalance, rebalance_sessions
from stratlib.fmp import FMPAuthError, FMPRequestError
from stratlib.prices import Bar
from stratlib.screening import run_screen
from stratlib.sell_rules import Position
from stratlib.strategy_params import ChecklistParams, NashParams, build_params
from stratlib.universe import parse_listings
from test_fundamental_parity import quarter_ends

SESSIONS = 850


def company_doc(*, n=22, first=date(2020, 9, 30), growth=0.06, fcf_margin=0.25, net_margin=0.16, cash=200e6, debt=100e6,
                revenue0=100e6, opex_growth=0.03, currency="USD", filing_delay=30):
    """A profitable, growing company whose market value stays flat, so its P/E falls as earnings rise."""
    income, balance, cash_flow, metrics = [], [], [], []
    for i, d in enumerate(quarter_ends(n, first)):
        revenue = revenue0 * (1 + growth) ** i
        filed = (d + timedelta(days=filing_delay)).isoformat()
        income.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency, "revenue": revenue,
                       "operatingExpenses": 30e6 * (1 + opex_growth) ** i, "operatingIncome": revenue * 0.25,
                       "incomeBeforeTax": revenue * 0.2, "incomeTaxExpense": revenue * 0.04,
                       "netIncome": revenue * net_margin, "weightedAverageShsOutDil": 100e6})
        balance.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency,
                        "cashAndShortTermInvestments": cash, "totalDebt": debt, "capitalLeaseObligations": None,
                        "totalStockholdersEquity": 500e6})
        cash_flow.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency,
                          "freeCashFlow": revenue * fcf_margin})
        metrics.append({"date": d.isoformat(), "reportedCurrency": currency, "marketCap": 1e9})
    return {"fetched_on": "2026-09-30", "income": income, "balance": balance, "cash": cash_flow, "metrics": metrics}


def seed(store, companies, *, sessions=SESSIONS, spy_drift=0.0004):
    """companies: symbol -> dict(sector, doc, drift, volume). Returns the session dates."""
    days, day = [], date(2023, 1, 2)
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    store.replace_universe(parse_listings([
        {"symbol": s, "companyName": f"{s} Inc", "exchangeShortName": "NYSE", "sector": c.get("sector", "Technology"),
         "industry": "Software"} for s, c in companies.items()]))
    t = np.arange(sessions)

    def save(symbol, closes, volume):
        bars = [Bar(d, c, c * 1.004, c * 0.996, c, volume) for d, c in zip(days, closes)]
        store.write_prices(symbol, bars, replace=True, requested_from=days[0],
                           checked_at=datetime(2026, 4, 30, tzinfo=timezone.utc), status="ok")
    for symbol in ("SPY", "QQQ"):
        save(symbol, 400 * np.exp(spy_drift * t), 5_000_000)
    for symbol, c in companies.items():
        save(symbol, 40 * np.exp(c.get("drift", 0.0004) * t), c.get("volume", 2_000_000))
        if c.get("doc") is not None:
            store.save_document(fs.STATEMENT_KEY + symbol, c["doc"])
    return days


def evening_after(day):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, 23, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------- Nash

def nash_market():
    return {
        "GOOD1": {"doc": company_doc(growth=0.06, fcf_margin=0.25)},
        "GOOD2": {"doc": company_doc(growth=0.04, fcf_margin=0.35)},
        "BANK": {"doc": company_doc(), "sector": "Financial Services"},
        "SLOW": {"doc": company_doc(growth=0.0, opex_growth=0.0)},
        "THIN": {"doc": company_doc(fcf_margin=0.05)},
        "DEBT": {"doc": company_doc(cash=50e6, debt=300e6)},
        "TINY": {"doc": company_doc(revenue0=5e6)},
        "OLD": {"doc": company_doc(first=date(2019, 3, 31))},            # last statement 2024-06: stale
        "ILLIQUID": {"doc": company_doc(), "volume": 100},
        "NODOC": {},
    }


def test_nash_scan_ranks_passers_by_rule_of_40_and_applies_every_exclusion(store, settings):
    days = seed(store, nash_market())
    report = run_screen(store, settings, strategy_id="nash_quality", now=evening_after(days[-1]))
    symbols = [c["symbol"] for c in report["candidates"]]
    assert set(symbols) == {"GOOD1", "GOOD2"}                      # the rest each fail one named rule
    assert report["members"] == symbols and all(c["weight_pct"] == 10.0 for c in report["candidates"])
    scores = [c["rule_of_40"] for c in report["candidates"]]
    assert scores == sorted(scores, reverse=True) and report["candidates"][0]["rank"] == 1
    assert report["counts"]["pass_rules"] == 2 and report["counts"]["liquid"] == 8   # the bank and the illiquid stock are out
    assert any("1 of" in w and "no cached statements" in w for w in report["warnings"])
    assert report["strategy_id"] == "nash_quality" and "thresholds" not in report["rule_snapshot"]


def test_nash_parameters_change_the_screen(store, settings):
    days = seed(store, nash_market())
    now = evening_after(days[-1])

    def run(**params):
        changed = replace(settings, strategies={**settings.strategies, "nash_quality": build_params("nash_quality", params)})
        return {c["symbol"] for c in run_screen(store, changed, strategy_id="nash_quality", now=now)["candidates"]}
    assert run(data_guards=False) == {"GOOD1", "GOOD2", "TINY"}                # the size floor is a guard
    assert run(min_margin_pct=30) == {"GOOD2"}
    assert run(min_revenue_growth_pct=20) == {"GOOD1"}
    assert run(margin="OM") == {"GOOD1", "GOOD2", "THIN"}                      # operating margin is 25% for everyone
    assert run(stale_days=2000) >= {"GOOD1", "GOOD2", "OLD"}


def test_nash_rule_seven_excludes_cyclical_industries(store, settings):
    market = {"GOOD1": {"doc": company_doc(), "sector": "Energy"}, "GOOD2": {"doc": company_doc(growth=0.05)}}
    days = seed(store, market)
    changed = replace(settings, strategies={**settings.strategies, "nash_quality": NashParams(exclude_cyclicals=True)})
    report = run_screen(store, changed, strategy_id="nash_quality", now=evening_after(days[-1]))
    assert [c["symbol"] for c in report["candidates"]] == ["GOOD2"]


# ---------------------------------------------------------------------------------------- checklist

def checklist_market(drifts):
    market = {f"P{i}": {"doc": company_doc(growth=0.06 + 0.002 * i), "drift": d} for i, d in enumerate(drifts, 1)}
    market["WEAK"] = {"doc": company_doc(net_margin=0.02)}                   # earns too little: ROIC and PEG fail
    market["LEVERED"] = {"doc": company_doc(debt=900e6, cash=10e6)}
    return market


def test_checklist_selects_the_best_momentum_passers_and_lists_the_rest(store, settings):
    days = seed(store, checklist_market([0.0002, 0.0003, 0.0004, 0.0005, 0.0006]))
    changed = replace(settings, strategies={**settings.strategies, "tt_checklist": ChecklistParams(top_n=3)})
    report = run_screen(store, changed, strategy_id="tt_checklist", now=evening_after(days[-1]))
    assert [c["symbol"] for c in report["candidates"]] == ["P5", "P4", "P3"]      # by 63-session return
    assert report["members"] == ["P5", "P4", "P3"]
    assert [s["symbol"] for s in report["skipped"]] == ["P2", "P1"] and "outside the top 3" in report["skipped"][0]["reason"]
    first = report["candidates"][0]
    assert first["stop"] == pytest.approx(first["close"] * 0.8, rel=1e-3) and first["pe"] < first["pe_median"]
    assert first["roic_pct"] > 15 and first["debt_equity"] < 1 and first["weight_pct"] == pytest.approx(100 / 3, abs=0.01)
    assert report["counts"]["pass_checklist"] == 5 and report["counts"]["liquid"] == 7


def test_checklist_trend_gates_block_entries_without_backfilling(store, settings):
    market = checklist_market([0.0003, 0.0004, 0.0005, 0.0006, 0.0007])
    market["FALLING"] = {"doc": company_doc(growth=0.05), "drift": -0.0006}
    days = seed(store, market)
    now = evening_after(days[-1])

    def run(gate, **extra):
        params = ChecklistParams(top_n=6, trend_gate=gate)
        changed = replace(settings, strategies={**settings.strategies, "tt_checklist": params})
        return run_screen(store, changed, strategy_id="tt_checklist", now=now)
    plain, stock = run("none"), run("stock")
    assert "FALLING" in [c["symbol"] for c in plain["candidates"]]
    assert "FALLING" not in [c["symbol"] for c in stock["candidates"]] and "FALLING" in stock["members"]   # still selected
    assert [b["symbol"] for b in stock["blocked"]] == ["FALLING"] and "200-day" in stock["blocked"][0]["reason"]
    assert len(stock["candidates"]) == len(plain["candidates"]) - 1                                   # not replaced


def test_checklist_market_gate_blocks_every_entry_when_spy_trends_down(store, settings):
    days = seed(store, checklist_market([0.0004, 0.0005]), spy_drift=-0.0005)
    changed = replace(settings, strategies={**settings.strategies, "tt_checklist": ChecklistParams(trend_gate="market")})
    report = run_screen(store, changed, strategy_id="tt_checklist", now=evening_after(days[-1]))
    assert report["candidates"] == [] and len(report["blocked"]) == 2 and report["members"]


def test_checklist_needs_enough_price_history(store, settings):
    from stratlib.screening import ScreenError
    days = seed(store, checklist_market([0.0004]), sessions=300)
    with pytest.raises(ScreenError, match="needs 800 completed sessions"):
        run_screen(store, settings, strategy_id="tt_checklist", now=evening_after(days[-1]))


# ---------------------------------------------------------------------------------------- refresh

def fmp_rows(doc):
    by_path = {fs.ENDPOINTS[kind]: rows for kind, rows in doc.items() if kind in fs.ENDPOINTS}
    return lambda path, params=None, **kw: by_path[path]


def client_for(doc, calendar=()):
    client = Mock()
    client.get.side_effect = fmp_rows(doc)
    client.earnings_calendar.return_value = list(calendar)
    client.stats.api_calls = 0
    return client


def test_refresh_fetches_missing_and_released_bundles_and_leaves_the_rest(store):
    today = date(2026, 9, 30)
    store.save_document(fs.STATEMENT_KEY + "FRESH", {**company_doc(), "fetched_on": "2026-09-20"})
    store.save_document(fs.STATEMENT_KEY + "REPORTED", {**company_doc(), "fetched_on": "2026-09-01"})
    store.save_document(fs.STATEMENT_KEY + "ANCIENT", {**company_doc(), "fetched_on": "2026-03-01"})
    client = client_for(company_doc(), calendar=[{"symbol": "REPORTED", "date": "2026-09-15"},
                                                 {"symbol": "FRESH", "date": "2026-09-01"},      # before its fetch
                                                 {"symbol": "OTHER", "date": "2026-09-15"}])
    due = fs.statements_due(store, client, ["FRESH", "REPORTED", "ANCIENT", "MISSING"], today)
    assert due == ["ANCIENT", "MISSING", "REPORTED"]
    result = fs.refresh_statements(store, client, ["FRESH", "REPORTED", "ANCIENT", "MISSING"], today, workers=2)
    assert result == {"due": 3, "fetched": 3, "errors": 0}
    assert store.document(fs.STATEMENT_KEY + "MISSING")["fetched_on"] == "2026-09-30"
    assert store.document(fs.STATEMENT_KEY + "FRESH")["fetched_on"] == "2026-09-20"
    assert client.get.call_count == 3 * 4                                             # four statements each


def test_a_failed_fetch_keeps_the_good_bundle_and_an_auth_failure_stops_the_run(store):
    today = date(2026, 9, 30)
    store.save_document(fs.STATEMENT_KEY + "OLDDOC", {**company_doc(), "fetched_on": "2026-03-01"})
    client = Mock()
    client.get.side_effect = FMPRequestError("server said no", path="income-statement")
    client.earnings_calendar.return_value = []
    result = fs.refresh_statements(store, client, ["OLDDOC", "NEWDOC"], today, workers=1)
    assert result["errors"] == 2 and result["fetched"] == 1                   # only NEWDOC gets an (error) entry
    assert store.document(fs.STATEMENT_KEY + "OLDDOC")["fetched_on"] == "2026-03-01"
    assert fs.load_statements(store, "OLDDOC") is not None and fs.load_statements(store, "NEWDOC") is None
    client.get.side_effect = FMPAuthError("bad key")
    with pytest.raises(FMPAuthError):
        fs.refresh_statements(store, client, ["ANOTHER"], today, workers=1)


def test_an_online_scan_fetches_missing_statements_then_screens_them(store, settings):
    market = nash_market()
    market["NODOC"] = {}
    days = seed(store, market)
    client = client_for(company_doc(growth=0.05, fcf_margin=0.3))
    report = run_screen(store, settings, client, strategy_id="nash_quality", now=evening_after(days[-1]))
    assert store.document(fs.STATEMENT_KEY + "NODOC") is not None             # fetched and cached for the next run
    assert "NODOC" in [c["symbol"] for c in report["candidates"]]
    assert not any("no cached statements" in w for w in report["warnings"])
    # An offline run reads the same cache and makes no calls.
    offline = run_screen(store, settings, strategy_id="nash_quality", now=evening_after(days[-1]))
    assert "NODOC" in [c["symbol"] for c in offline["candidates"]] and offline["api_calls"] == 0


# ---------------------------------------------------------------------------------------- alerts

def sessions_for(year=2026):
    days, day = [], date(year - 1, 12, 1)
    while day <= date(year, 12, 31):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def flat_bars(days, price=50.0):
    return [Bar(d, price, price, price, price, 1e6) for d in days]


def test_rebalance_dates_are_the_first_session_of_each_calendar_quarter():
    days = sessions_for()
    assert rebalance_sessions(days) == ["2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]
    assert [next_rebalance(d) for d in ("2026-09-29", "2026-10-01", "2026-12-31", "2026-02-03")] == [
        "2026-10-01", "2027-01-01", "2027-01-01", "2026-04-01"]


def member_alert(strategy_id, *, entry, as_of, members, screen_date=None, price=50.0, params=None, stop=None):
    days = sessions_for()
    history = flat_bars([d for d in days if d <= as_of], price)
    screen = {"price_date": screen_date or as_of, "members": members}
    position = Position("AAA", entry, 50.0, stop_price=stop)
    return assess_scan_position(strategy_id, params or build_params(strategy_id, None), position, history, as_of=as_of,
                                screen=screen, sessions=days)


def test_a_holding_in_the_selection_is_held_and_one_outside_is_sold_at_the_rebalance():
    held = member_alert("tt_checklist", entry="2026-05-04", as_of="2026-09-29", members=["AAA", "BBB"])
    assert held.action == "Hold" and "Still selected" in held.reason and held.hold_until == "2026-10-01"
    outside = member_alert("tt_checklist", entry="2026-05-04", as_of="2026-09-29", members=["BBB"])
    assert outside.action == "Sell" and "quarterly rebalance" in outside.reason and "2026-07-01" in outside.reason
    assert member_alert("nash_quality", entry="2026-05-04", as_of="2026-09-29", members=["BBB"]).action == "Sell"


def test_a_holding_bought_after_the_latest_rebalance_waits_for_the_next_one():
    early = member_alert("nash_quality", entry="2026-07-06", as_of="2026-09-29", members=["BBB"])   # after the July 1 rebalance
    assert early.action == "Hold"
    assert early.hold_until == "2026-10-01" and "next quarterly rebalance" in early.reason
    # A screen that predates the latest rebalance cannot decide.
    stale = member_alert("tt_checklist", entry="2026-01-06", as_of="2026-09-29", members=["BBB"], screen_date="2026-06-30")
    assert stale.action == "Hold" and "predates the 2026-07-01 rebalance" in stale.reason


def test_the_checklist_stop_is_twenty_percent_below_entry_and_does_not_trail():
    days = [d for d in sessions_for() if d <= "2026-05-29"]
    bars = flat_bars(days, 50.0)
    bars[-1] = Bar(bars[-1].date, 39.0, 39.0, 39.0, 39.0, 1e6)                        # 22% under the 50.0 entry
    position = Position("AAA", days[-40], 50.0)
    params = build_params("tt_checklist", None)
    hit = assess_scan_position("tt_checklist", params, position, bars, as_of=days[-1],
                               screen={"price_date": days[-1], "members": ["AAA"]}, sessions=sessions_for())
    assert hit.action == "Sell" and hit.stop_price == pytest.approx(40.0) and "stop" in hit.reason
    above = assess_scan_position("tt_checklist", params, position, flat_bars(days, 41.0), as_of=days[-1],
                                 screen={"price_date": days[-1], "members": ["AAA"]}, sessions=sessions_for())
    assert above.action == "Hold"
    recorded = replace(position, stop_price=30.0)
    assert assess_scan_position("tt_checklist", params, recorded, bars, as_of=days[-1],
                                screen={"price_date": days[-1], "members": ["AAA"]}, sessions=sessions_for()).action == "Hold"
    # Nash has no price stop at all.
    nash = assess_scan_position("nash_quality", build_params("nash_quality", None), position, bars, as_of=days[-1],
                                screen={"price_date": days[-1], "members": ["AAA"]}, sessions=sessions_for())
    assert nash.action == "Hold" and nash.stop_price is None


def test_without_a_saved_screen_the_alert_asks_for_one():
    a = assess_scan_position("nash_quality", build_params("nash_quality", None), Position("AAA", "2026-05-04", 50.0),
                             flat_bars([d for d in sessions_for() if d <= "2026-09-29"]), as_of="2026-09-29")
    assert a.action == "Hold" and "Run the screen" in a.reason


# ---------------------------------------------------------------------------------------- plan

def test_plans_use_each_strategys_own_holding_count_and_equal_weights(store, settings):
    from stratlib.portfolio import portfolio_plan
    from stratlib.strategies import current_rules, equal_weighted, holding_limit
    changed = replace(settings, strategies={**settings.strategies, "nash_quality": NashParams(slots=4),
                                            "tt_checklist": ChecklistParams(top_n=6)})
    assert [holding_limit(s, changed, settings.backtest) for s in ("nash_quality", "tt_checklist", "qullamaggie")] == [
        4, 6, settings.backtest.max_holdings]
    assert equal_weighted("nash_quality") and equal_weighted("tt_checklist") and not equal_weighted("minervini")
    snapshot = current_rules(changed, "nash_quality")
    assert snapshot["portfolio"]["max_holdings"] == 4
    # Changing the global holdings limit must not look like a rule change for a strategy that ignores it.
    assert current_rules(replace(changed, backtest=replace(changed.backtest, max_holdings=3)), "nash_quality") == snapshot
    candidates = [{"symbol": f"S{i}", "name": f"S{i}", "close": 10.0, "weight_pct": 25.0, "rank": i, "order": [i, f"S{i}"],
                   "rule_of_40": 50.0 - i, "revenue_growth_pct": 20.0, "fcf_margin_pct": 20.0, "operating_margin_pct": 20.0,
                   "quarter_end": "2026-06-30"} for i in range(1, 7)]
    report = {"price_date": "2026-09-29", "sample": None, "candidates": candidates, "members": [c["symbol"] for c in candidates],
              "rule_snapshot": snapshot}
    plan = portfolio_plan(store, report, "nash_quality", settings.thresholds, settings.backtest,
                          {"as_of": "2026-09-29", "exposure": 0}, "2026-09-29", params=changed)
    assert plan["capacity"] == 4 and not plan["issues"]
    assert [r["Plan"] for r in plan["candidates"]] == ["Proposed buy"] * 4 + ["Watch"] * 2
    assert plan["candidates"][0]["Initial weight, %"] == 25.0 and plan["candidates"][0]["Rule of 40, pts"] == 49.0
