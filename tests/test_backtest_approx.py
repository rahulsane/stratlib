"""Approximate filing-date backtest. Synthetic prices and statements; all I/O stays offline."""

import random
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from stratlib import backtest_approx as A
from stratlib.backtest import Signal, simulate
from stratlib.backtest_data import public_statements, restore_price_basis, run_backtest
from stratlib.config import BacktestSettings
from stratlib.market_stats import industry_ranks, price_metrics, rs_ratings
from stratlib.prices import Bar
from stratlib.universe import parse_listings
from test_backtest_data import seed_archive, synthetic_bundle

NOW = datetime(2025, 2, 14, 22, tzinfo=timezone.utc)


def listing(symbol, industry="Technology", exchange="NYSE"):
    return {"symbol": symbol, "companyName": f"{symbol} Corp", "exchange": exchange,
            "industry": industry, "sector": "Tech", "isEtf": False, "isFund": False}


def prepared(store, settings, start, end):
    store.save_document(A.PREPARED, {"start": start, "end": end, "prepared_at": NOW.isoformat(),
                                     "survivor_count": 2, "statements_unavailable": 0,
                                     "price_thresholds": A._price_thresholds(settings.thresholds)})


def seed_approximate(store, settings, *, aaa_delisted=None):
    """seed_archive's market, supplied through today's universe and statement histories instead."""
    settings, start, end, _ = seed_archive(store, settings)
    current = ["BBB"] if aaa_delisted else ["AAA", "BBB"]
    store.replace_universe(parse_listings([listing(s) for s in current]))
    for symbol in ("AAA", "BBB"):
        store.save_document(A.STATEMENTS + symbol, {**synthetic_bundle(), "fetched_on": "2025-02-14"})
    store.save_document(A.SPLITS, {"fetched_on": "2025-02-14", "from": start, "to": "2025-02-14", "rows": []})
    if aaa_delisted:
        store.save_document("backtest:delisted", {"complete": True, "fetched_at": NOW.isoformat(), "rows": [
            {"symbol": "AAA", "companyName": "AAA Corp", "exchange": "NYSE", "ipoDate": "2010-01-01",
             "delistedDate": aaa_delisted}]})
        store.save_document(A.PROFILE + "AAA", {"fetched_on": "2025-02-14",
                                                "profile": {"industry": "Technology", "isEtf": False, "isFund": False}})
    prepared(store, settings, start, end)
    return settings, start, end


def test_worker_processes_give_the_single_process_result(store, settings):
    settings, start, end = seed_approximate(store, settings)

    def strip(report):
        return {k: v for k, v in report.items() if k != "created_at"}
    alone = A.run(store, settings, start, end)
    assert alone["trades"] and alone["signal_counts"]["signals"]
    # Over two processes the sessions run in several batches, merged back in session order.
    assert strip(A.run(store, settings, start, end, workers=2)) == strip(alone)
    for strategy in ("leaders", "trend"):
        assert (A.leader_orders(store, settings, start, end, strategy=strategy, workers=2)
                == A.leader_orders(store, settings, start, end, strategy=strategy))


def test_a_saved_leader_ranking_serves_portfolio_and_market_variants_only(store, settings, monkeypatch):
    settings, start, end = seed_approximate(store, settings)

    def strip(report):
        return {k: v for k, v in report.items() if k != "created_at"}
    A.run_leaders(store, settings, start, end, strategy="leaders")
    assert len(store.document_keys(A.ORDERS_CACHE)) == 1
    # Fewer holdings and a different exposure ladder rank the same stocks, so the saved ranking is reused,
    # with the result a fresh ranking gives.
    variant = replace(settings, thresholds=replace(settings.thresholds, exposure_pressure_pct=100),
                      backtest=replace(settings.backtest, max_holdings=1))
    fresh = A.run_leaders(store, variant, start, end, strategy="leaders", orders=A.leader_orders(store, variant, start, end))
    real, calls = A.leader_orders, []
    monkeypatch.setattr(A, "leader_orders", lambda *a, **k: calls.append(k) or real(*a, **k))
    assert strip(A.run_leaders(store, variant, start, end, strategy="leaders")) == strip(fresh)
    assert calls == []
    # A threshold the ranking reads, or new prices, make a new ranking.
    stricter = replace(settings, thresholds=replace(settings.thresholds, rs_min=settings.thresholds.rs_min + 1))
    A.run_leaders(store, stricter, start, end, strategy="leaders")
    store.write_prices("AAA", [], replace=False, requested_from=None, checked_at=NOW + timedelta(days=1), status="ok")
    A.run_leaders(store, settings, start, end, strategy="leaders")
    assert len(calls) == 2
    assert len(store.document_keys(A.ORDERS_CACHE)) == 3


def test_matches_strict_engine_on_the_same_market(store, settings):
    settings, start, end = seed_approximate(store, settings)
    strict = run_backtest(store, settings, start, end)
    report = A.run(store, settings, start, end)
    assert report["mode"] == "approximate"
    assert [(t["symbol"], t["entry_date"], t["exit_date"], t["entry_price"], t["reason"]) for t in report["trades"]] == \
           [(t["symbol"], t["entry_date"], t["exit_date"], t["entry_price"], t["reason"]) for t in strict["trades"]]
    assert report["metrics"]["ending_equity"] == pytest.approx(strict["metrics"]["ending_equity"])
    assert report["daily_screen"][0]["members"] == 2
    assert store.document("latest_backtest")["mode"] == "approximate"


def test_delisted_holding_is_sold_at_last_close_and_labelled(store, settings):
    settings, start, end = seed_approximate(store, settings, aaa_delisted="2025-02-12")
    report = A.run(store, settings, start, end)
    trade = report["trades"][0]
    assert (trade["symbol"], trade["entry_date"], trade["exit_date"]) == ("AAA", "2025-02-11", "2025-02-12")
    assert trade["exit_price"] == 104          # 2025-02-11 close, the last before delisting
    assert trade["reason"] == "Delisted; sold at last close (approximation)"
    assert trade["evidence"]["listing"] == "delisted"
    assert report["signal_counts"]["delisting_exits_at_last_close"] == 1
    assert report["coverage"]["delisted_members"] == 1


def test_members_combine_current_and_delisted_common_stocks(store, settings):
    store.replace_universe(parse_listings([listing("KEEP"), listing("REUSE")]))
    rows = [("GONE", "Gone Corp", "2026-03-01"), ("TRKR", "Tracker Holdings", "2026-03-01"), ("REUSE", "Old Co", "2026-03-01"),
            ("EARLY", "Early Corp", "2025-01-01"), ("NOPR", "Unknown Corp", "2026-03-01"),
            ("WARRW", "Warrant Corp Warrants", "2026-03-01")]
    store.save_document("backtest:delisted", {"complete": True, "fetched_at": NOW.isoformat(), "rows": [
        {"symbol": s, "companyName": n, "exchange": "NASDAQ", "ipoDate": "2015-01-01", "delistedDate": d} for s, n, d in rows]})
    store.save_document(A.PROFILE + "GONE", {"profile": {"industry": "Retail", "isEtf": False, "isFund": False}})
    store.save_document(A.PROFILE + "TRKR", {"profile": {"industry": "Asset Management", "isEtf": True, "isFund": False}})
    found, counts = A.members(store, settings, "2025-09-26", "2026-09-25")
    assert set(found) == {"KEEP", "REUSE", "GONE", "NOPR"}
    assert found["GONE"] == {"name": "Gone Corp", "exchange": "NASDAQ", "industry": "Retail",
                             "until": "2026-03-01", "source": "delisted"}
    assert found["REUSE"]["source"] == "current" and found["NOPR"]["industry"] is None
    assert counts["delisted_tickers_reused"] == 1 and counts["delisted_non_common"] == 1
    assert counts["delisted_members_without_profile"] == 1


def test_statements_become_visible_after_the_filing_cutoff():
    bundle = synthetic_bundle()
    latest = max(bundle["income_quarter"], key=lambda r: r["date"])
    latest["acceptedDate"] = "2025-01-15 16:05:00"      # after the 13:00 Eastern information cutoff
    visible = lambda day: {r["date"] for r in public_statements(bundle, day)["income_quarter"]}
    assert latest["date"] not in visible("2025-01-15")
    assert latest["date"] in visible("2025-01-16")


def test_an_indexed_bundle_shows_the_same_rows_in_the_same_order_every_day():
    from stratlib.backtest_data import STATEMENT_KEYS, statement_available_at, statement_index

    def unindexed(bundle, day):
        """The original reading: parse every row's timestamps on every call."""
        from stratlib.backtest_data import session_close
        cutoff, result = session_close(day), {**bundle}
        for key in STATEMENT_KEYS:
            result[key] = []
            for row in bundle.get(key, []):
                try:
                    if statement_available_at(row) <= cutoff and str(row["date"]) <= day:
                        result[key].append(row)
                except (ValueError, KeyError, TypeError):
                    continue
            result[key].sort(key=statement_available_at, reverse=True)
        return result

    bundle = synthetic_bundle()
    quarters = bundle["income_quarter"]
    quarters[0]["acceptedDate"] = "2025-01-15 16:05:00"              # after the 13:00 cutoff
    quarters[1]["acceptedDate"], quarters[2]["acceptedDate"] = "2024-08-01", "2024-08-01"   # a tie, date-only
    quarters[3].pop("acceptedDate", None)
    quarters[3]["filingDate"] = "not a date"                         # falls back to the 45-day rule
    quarters[4] = {k: v for k, v in quarters[4].items() if k != "date"}   # unreadable period: never shown
    index = statement_index(bundle)
    for day in ("2023-06-30", "2024-08-01", "2024-08-02", "2025-01-15", "2025-01-16", "2026-09-25"):
        assert public_statements(bundle, day, index) == unindexed(bundle, day)


def test_bulk_ranking_matches_the_live_price_metrics(store, settings):
    """Random walks with gaps, a 10-for-1 split and a delisting, checked against price_metrics."""
    t = replace(settings.thresholds, high_sessions=30, short_ma_sessions=5, long_ma_sessions=20, volume_sessions=10,
                rs_quarter_sessions=8, industry_sessions=15, rs_min=40, min_price=12)
    settings = replace(settings, thresholds=t)
    rng = random.Random(4)
    sessions, day = [], date(2025, 1, 2)
    while len(sessions) < 90:
        if day.weekday() < 5:
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    checked = datetime(2025, 6, 30, 22, tzinfo=timezone.utc)
    names = [f"S{i:02d}" for i in range(24)]
    store.replace_universe(parse_listings([listing(s, industry=f"Group {i % 4}") for i, s in enumerate(names[:-1])]))
    for symbol in ["SPY", *names]:
        price, bars = rng.uniform(1, 40), []
        for i, d in enumerate(sessions):
            price *= 1 + rng.gauss(0.004, 0.03)
            if symbol != "SPY" and rng.random() < 0.02:
                continue                                       # data gap
            bars.append(Bar(d, price, price * 1.02, price * 0.98, price, rng.choice([2e5, 6e5, 1e6])))
        store.write_prices(symbol, bars, replace=True, requested_from=sessions[0], checked_at=checked, status="ok")
    split = {"symbol": "S03", "date": sessions[-3], "numerator": 10, "denominator": 1}
    store.save_document(A.SPLITS, {"fetched_on": "2025-06-30", "from": sessions[0], "to": "2025-06-30", "rows": [split]})
    store.save_document("backtest:delisted", {"complete": True, "fetched_at": checked.isoformat(), "rows": [
        {"symbol": names[-1], "companyName": "Last Corp", "exchange": "NYSE", "ipoDate": "2010-01-01",
         "delistedDate": sessions[-5]}]})
    start, end = sessions[60], sessions[-1]
    found, _ = A.members(store, settings, start, end)
    ranked = A.rankings(store, settings, start, end, found)
    assert list(ranked["days"]) == sessions[60:]
    survivors_seen = 0
    for d in sessions[60:]:
        upto = sessions[:sessions.index(d) + 1]
        metrics = {}
        for symbol, info in found.items():
            if info["until"] is not None and d >= info["until"]:
                continue
            bars = store.price_history(symbol, through=d)
            if not bars or bars[-1].date != d:
                continue
            adjusted, _ = restore_price_basis(bars, [split] if symbol == "S03" else [], "2025-06-30", d)
            metrics[symbol] = price_metrics(adjusted, upto, t)
        ratings = rs_ratings({s: m.rs_score for s, m in metrics.items()})
        expected = {s: ratings[s] for s, m in metrics.items() if m.price_pass and ratings.get(s, 0) >= t.rs_min}
        got = ranked["days"][d]
        assert {s: v["rs"] for s, v in got["survivors"].items()} == expected, d
        groups = industry_ranks({s: found[s]["industry"] for s in metrics},
                                {s: m.industry_return for s, m in metrics.items()})
        # Split restoration rescales prices before dividing, so the last digit can differ.
        assert got["groups"] == {name: {**g, "return_pct": pytest.approx(g["return_pct"], rel=1e-12)}
                                 for name, g in groups.items()}
        assert got["members"] == len(metrics) and got["ranked"] == len(ratings)
        survivors_seen += len(expected)
    assert survivors_seen > 20
    chunked = A.rankings(store, settings, start, end, found, chunk_days=7)     # many chunks, same answers
    assert chunked["days"].keys() == ranked["days"].keys()
    for d, got in ranked["days"].items():
        other = chunked["days"][d]
        assert other["survivors"] == got["survivors"] and other["ranked"] == got["ranked"], d
        assert other["groups"].keys() == got["groups"].keys()
    # Research mode ranks only the chosen sessions, with every rated member's filter inputs.
    chosen = {sessions[61], sessions[75], sessions[-1]}
    detail = A.rankings(store, settings, start, end, found, detail_days=chosen)
    assert set(detail["days"]) == chosen
    for d in chosen:
        got, full = detail["days"][d], ranked["days"][d]
        assert got["survivors"] == full["survivors"] and got["ranked"] == full["ranked"], d
        assert len(got["features"]) == got["ranked"]
        assert {s for s, f in got["features"].items() if f["price_pass"] and f["rs"] >= t.rs_min} == set(full["survivors"])
        assert all(f["rs"] == full["survivors"][s]["rs"] for s, f in got["features"].items() if s in full["survivors"])


def test_carried_forward_marks_never_fill_an_entry():
    days = ["2025-03-03", "2025-03-04", "2025-03-05"]
    spy = [Bar(d, 100, 101, 99, 100, 1e6) for d in days]
    stock = [Bar(days[0], 10, 11, 9, 10, 1e6), Bar(days[1], 10, 10, 10, 10, 0.0), Bar(days[2], 10, 11, 9, 10, 1e6)]
    signal = Signal("XYZ", days[0], 10, 99, "Flat base", {})
    t = replace(BacktestSettings(), max_holdings=1)
    from stratlib.config import Thresholds
    report = simulate(days, {"SPY": spy, "XYZ": stock}, {d: "confirmed uptrend" for d in days},
                      lambda d: [signal] if d == days[0] else [], Thresholds(), t, stale={"XYZ": {days[1]}})
    assert report["trades"] == [] and report["open_positions"] == []
    assert report["skipped_entries"][0]["reason"] == "Missing next-session open or valid OHLCV"


def test_check_asks_for_preparation_and_reports_period(store, settings):
    settings, start, end = seed_approximate(store, settings)
    assert A.check(store, settings, start, end)["ready"]
    store.save_document(A.PREPARED, {**store.document(A.PREPARED), "end": "2025-02-12"})
    status = A.check(store, settings, start, end)
    assert not status["ready"] and status["errors"][0].startswith("Prepare data")


def test_cli_runs_the_approximate_method_without_a_key(settings, monkeypatch, capsys):
    import json
    import yaml
    from stratlib.cli import main
    from stratlib.store import Store
    monkeypatch.delenv("FMP_API_KEY")
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    raw = yaml.safe_load(settings.path.read_text())
    raw["thresholds"], raw["backtest"] = asdict(updated.thresholds), asdict(updated.backtest)
    settings.path.write_text(yaml.safe_dump(raw))
    store.close()
    args = ["--config", str(settings.path), "backtest", "--approximate", "--start", start, "--end", end]
    assert main([*args, "--check"]) == 0
    assert '"ready": true' in capsys.readouterr().out
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["metrics"]["closed_trades"] == 1
    assert main([*args, "--strategy", "trend", "--label", "CLI trend"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["strategy"], result["label"]) == ("trend", "CLI trend")
    strict = ["--config", str(settings.path), "backtest", "--start", start, "--end", end, "--strategy", "leaders"]
    assert main(strict) == 1


def test_members_skip_a_delisted_listing_of_a_company_already_present(store, settings):
    store.replace_universe(parse_listings([listing("NEWT")]))
    store.save_document("backtest:delisted", {"complete": True, "fetched_at": NOW.isoformat(), "rows": [
        {"symbol": "OLDT", "companyName": "NEWT Corp", "exchange": "NYSE", "ipoDate": "2015-01-01", "delistedDate": "2026-03-01"},
        {"symbol": "GONEA", "companyName": "Gone Holdings Class A", "exchange": "NYSE", "ipoDate": "2015-01-01", "delistedDate": "2026-03-01"},
        {"symbol": "GONEB", "companyName": "Gone Holdings Class B", "exchange": "NYSE", "ipoDate": "2015-01-01", "delistedDate": "2026-03-01"}]})
    found, counts = A.members(store, settings, "2025-09-26", "2026-09-25")
    assert set(found) == {"NEWT", "GONEA"}     # a renamed ticker and a second class are the same company
    assert counts["delisted_same_company"] == 2


def test_leaders_portfolio_runs_on_the_approximate_data(store, settings):
    settings, start, end = seed_approximate(store, settings)
    report = A.run_leaders(store, settings, start, end, label="Leaders")
    assert report["strategy"] == "leaders" and report["label"] == "Leaders"
    assert report["signal_counts"]["eligible_sessions"] >= 1
    bought = {t["symbol"] for t in report["trades"]} | {p["symbol"] for p in report["open_positions"]}
    assert bought <= {"AAA", "BBB"} and bought
    assert store.document("latest_backtest")["strategy"] == "leaders"


def test_statement_cache_keeps_scorer_fields_and_evicts(store):
    store.save_document(A.STATEMENTS + "AAA", {"fetched_on": "2026-01-01", "income_quarter": [
        {"date": "2025-12-31", "epsDiluted": 1.0, "costOfRevenue": 5, "link": "x"}], "splits": []})
    store.save_document(A.STATEMENTS + "BBB", {"fetched_on": "2026-01-01", "error": "HTTP 404"})
    cache = A.StatementCache(store, size=1)
    assert cache.get("AAA")["income_quarter"] == [{"date": "2025-12-31", "epsDiluted": 1.0}]
    assert "error" in cache.get("BBB") and list(cache.docs) == ["BBB"]      # AAA evicted
    assert cache.fetched == {"AAA": "2026-01-01", "BBB": "2026-01-01"}


def test_splits_before_the_calendar_window_come_from_each_symbols_history(store, monkeypatch):
    from stratlib.fmp import FMPError, FMPPlanRestrictedError
    monkeypatch.setattr(A, "_today", lambda: "2025-06-30")

    class Client:
        def __init__(self):
            self.calendar, self.histories = [], []

        def splits_calendar(self, begin, end, cache_ttl=None):
            self.calendar.append((begin, end))
            if begin < date(2024, 12, 1):  # the plan's window
                raise FMPPlanRestrictedError("from not available")
            row = {"symbol": "NEW", "date": "2025-03-03", "numerator": 3, "denominator": 1}
            return [row] if begin.isoformat() <= row["date"] <= end.isoformat() else []

        def splits(self, symbol, cache_ttl=None):
            self.histories.append(symbol)
            if symbol == "BAD":
                raise FMPError("down")
            return [{"date": "2025-03-03", "numerator": 3, "denominator": 1},
                    {"date": "2024-05-01", "numerator": 2, "denominator": 1},
                    {"date": "2019-01-02", "numerator": 5, "denominator": 1}]

    bar = lambda day: Bar(day, 10, 11, 9, 10, 1000)
    for symbol, first in (("OLD", "2023-01-03"), ("BAD", "2023-01-03"), ("NEW", "2025-01-02")):
        store.write_prices(symbol, [bar(first)], replace=True, requested_from=first, checked_at=NOW, status="ok")
    client = Client()
    doc = A.refresh_split_calendar(client, store, "2024-01-02", ["OLD", "BAD", "NEW", "GONE"])
    # One refused calendar call; only symbols priced before the window need their own history.
    assert sum(begin < date(2024, 12, 1) for begin, _ in client.calendar) == 1
    assert sorted(client.histories) == ["BAD", "OLD"]
    assert doc["histories_unavailable"] == ["BAD"]
    covered = doc["calendar_from"]
    assert "2024-12-01" <= covered <= "2025-01-30"
    rows = sorted((r["symbol"], r["date"]) for r in doc["rows"])
    # OLD's 2019 split is before the period and its 2025 split is the calendar's to report.
    assert rows == [("NEW", "2025-03-03"), ("OLD", "2024-05-01")]
    assert A.split_rows(store)["OLD"][0]["numerator"] == 2

    # Past splits are kept: a later refresh refetches only the failed history.
    store.delete_document(A.SPLITS)
    client.histories.clear()
    A.refresh_split_calendar(client, store, "2024-01-02", ["OLD", "BAD", "NEW"])
    assert client.histories == ["BAD"]


def test_trend_order_requires_rs_group_sales_and_trading_value():
    from stratlib.config import Thresholds
    from stratlib.scoring import Criterion
    t, p = Thresholds(industry_top=40), BacktestSettings(trend_min_rs=90, trend_min_sales_growth_pct=20,
                                                         trend_min_dollar_volume_m=50)
    sales = lambda value: [Criterion("c_sales", "C", "Quarterly sales growth", value, None, "")]
    group = {"rank": 12}
    assert A.trend_order("AAA", 95, group, sales(30), 60e6, t, p) == ((-95, -30, "AAA"), None)
    assert A.trend_order("AAA", 89, group, sales(30), 60e6, t, p) == (None, "trend_rs")
    assert A.trend_order("AAA", 95, {"rank": 41}, sales(30), 60e6, t, p) == (None, "trend_industry")
    assert A.trend_order("AAA", 95, None, sales(30), 60e6, t, p) == (None, "trend_industry")
    assert A.trend_order("AAA", 95, group, sales(19.9), 60e6, t, p) == (None, "trend_sales")
    assert A.trend_order("AAA", 95, group, sales(None), 60e6, t, p) == (None, "trend_sales")
    assert A.trend_order("AAA", 95, group, sales(30), 40e6, t, p) == (None, "trend_dollar_volume")
    # Higher RS first, then faster sales growth.
    assert sorted([A.trend_order("B", 95, group, sales(25), 60e6, t, p)[0],
                   A.trend_order("A", 95, group, sales(80), 60e6, t, p)[0],
                   A.trend_order("C", 97, group, sales(21), 60e6, t, p)[0]]) == [(-97, -21, "C"), (-95, -80, "A"),
                                                                                  (-95, -25, "B")]


def test_below_line_marks_closes_under_the_moving_average(store):
    closes = [10, 11, 12, 13, 12, 9, 14]
    day = date(2025, 3, 3)
    bars = []
    for c in closes:
        bars.append(Bar(day.isoformat(), c, c + 1, c - 1, c, 1000))
        day += timedelta(days=1)
    store.write_prices("AAA", bars, replace=True, requested_from=bars[0].date, checked_at=NOW, status="ok")
    # Three-session averages: 11, 12, 12.33, 11.33, 11.67 from the third bar on.
    assert A.below_line(store, "AAA", 3, bars[0].date, bars[-1].date) == {bars[4].date, bars[5].date}
    assert A.below_line(store, "AAA", 3, bars[5].date, bars[-1].date) == {bars[5].date}


def test_trend_leaders_run_on_the_approximate_data_and_reject_other_rankings(store, settings):
    settings, start, end = seed_approximate(store, settings)
    open_rules = replace(settings.backtest, trend_min_rs=settings.thresholds.rs_min, trend_min_sales_growth_pct=-1e9)
    report = A.run_leaders(store, settings, start, end, portfolio=open_rules, strategy="trend", label="Trend")
    assert report["strategy"] == "trend" and report["limitations"][0].startswith("Trend leaders")
    assert report["signal_counts"]["eligible_sessions"] >= 1
    screen_orders = A.leader_orders(store, settings, start, end)
    with pytest.raises(A.BacktestError, match="different strategy"):
        A.run_leaders(store, settings, start, end, portfolio=open_rules, strategy="trend", orders=screen_orders)
    trend_orders = A.leader_orders(store, settings, start, end, strategy="trend", portfolio=open_rules)
    with pytest.raises(A.BacktestError, match="different strategy"):
        A.run_leaders(store, settings, start, end, strategy="trend", orders=trend_orders)   # default thresholds differ
