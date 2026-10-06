from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from stratlib.fmp import FMPPlanRestrictedError, FMPRequestError
from stratlib.fundamentals import STATEMENTS, load_fundamentals, refresh_due
from stratlib.screening import ScreenError, run_screen
from stratlib.universe import parse_listings
from conftest import load_fixture
from test_market_stats import synthetic_bars

NOW = datetime(2025, 9, 20, 22, tzinfo=timezone.utc)


def seed_market(store):
    store.replace_universe(parse_listings([
        {"symbol":symbol, "companyName":symbol, "exchangeShortName":"NYSE", "industry":"Tech"}
        for symbol in ["LOW", "MID", "TOP"]
    ]))
    for symbol, gain in [("LOW", .01), ("MID", .1), ("TOP", 1), ("SPY", .1)]:
        bars = synthetic_bars(gain)
        store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date,
                           checked_at=NOW, status="ok")


def statement_client():
    client = Mock()
    client.income_statement.return_value = load_fixture("income-statement_AAPL_quarter_limit12.json")
    client.balance_sheet.return_value = load_fixture("balance-sheet-statement_AAPL_annual_limit4.json")
    client.cash_flow.return_value = load_fixture("cash-flow-statement_AAPL_annual_limit4.json")
    client.splits.return_value = []
    return client


def test_survivors_only_and_full_universe_rank_with_sample(store, settings):
    seed_market(store)
    # TOP is the only RS survivor; rating must be 99, not singleton 50.
    report = run_screen(store, settings, sample=1, now=NOW)
    assert report["ranked_count"] == 3
    assert report["survivor_count"] == 1
    assert report["evaluated_count"] == 1
    assert report["api_calls"] == 0
    top = next(row for row in report["rows"] if row["symbol"] == "TOP")
    assert top["rs"] == 99
    assert top["stage"] == "Missing fundamentals"
    assert all(row["stage"] != "Missing fundamentals" for row in report["rows"] if row["symbol"] != "TOP")
    assert store.document("latest_screen")["ranked_count"] == 3
    assert report['phase']==3
    assert report['market']['state'] is None  # this fixture has no index histories
    assert top['phase3_pass'] is False
    assert [c['key'] for c in top['criteria'][-3:]]==['n_position','n_volume','m_direction']
    assert top['criteria'][-1]['passed'] is None
    assert all('base' in row for row in report['rows'])


def test_sampling_never_changes_rs_or_industry_ranks(store, settings):
    seed_market(store)
    settings = replace(settings, thresholds=replace(settings.thresholds, rs_min=1, max_below_high_pct=25))
    full = run_screen(store, settings, now=NOW)
    sample = run_screen(store, settings, sample=1, now=NOW)
    assert {r["symbol"]:(r["rs"],r["industry_stats"]) for r in full["rows"]} == {
        r["symbol"]:(r["rs"],r["industry_stats"]) for r in sample["rows"]}
    assert sample["evaluated_count"] == 1
    assert sum(r["stage"] == "Outside sample" for r in sample["rows"]) == 2


def test_extra_stock_date_cannot_displace_spy_rs_anchor(store, settings):
    seed_market(store)
    # A stock has every SPY session plus a provider-only date in the window.
    # Use alternate days so the extra date does not replace a calendar bar.
    from datetime import timedelta
    now = datetime(2026, 6, 1, 22, tzinfo=timezone.utc)
    for symbol in ["SPY", "LOW", "MID", "TOP"]:
        bars = [replace(b, date=(date(2025, 1, 1) + timedelta(days=2*i)).isoformat())
                for i, b in enumerate(store.price_history(symbol))]
        store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date,
                           checked_at=now, status="ok")
    baseline = run_screen(store, settings, now=now)
    bars = store.price_history("MID")
    extra = replace(bars[1], date="2025-01-02", close=9999, high=9999)
    store.write_prices("MID", [extra], replace=False, requested_from=bars[0].date,
                       checked_at=now, status="ok")
    result = run_screen(store, settings, now=now)
    assert result["ranked_count"] == 3
    assert {r["symbol"]: (r["rs"], r["prices"]) for r in result["rows"]} == {
        r["symbol"]: (r["rs"], r["prices"]) for r in baseline["rows"]}


def test_history_older_than_spy_window_is_stale_not_missing(store, settings):
    seed_market(store)
    old = replace(store.price_history("MID")[0], date="2024-01-01")
    store.write_prices("MID", [old], replace=True, requested_from=old.date, checked_at=NOW, status="ok")
    report = run_screen(store, settings, now=NOW)
    row = next(r for r in report["rows"] if r["symbol"] == "MID")
    assert row["rs"] is None
    assert row["stage"] == "Price filter"
    assert any("1 stocks have no bar" in warning for warning in report["warnings"])


def test_missing_full_universe_history_stops_before_fundamentals(store, settings):
    seed_market(store)
    store.write_prices("MID", [], replace=True, requested_from=None, checked_at=NOW, status="empty")
    client = Mock()
    with pytest.raises(ScreenError, match="no price history"):
        run_screen(store, settings, client, now=NOW)
    client.income_statement.assert_not_called()


def test_invalid_sample_and_empty_store(store, settings):
    with pytest.raises(ScreenError, match="positive"):
        run_screen(store, settings, sample=0)
    with pytest.raises(ScreenError, match="No stored universe"):
        run_screen(store, settings)


def test_durable_fundamentals_skip_statement_refresh_outside_earnings_week(store):
    client = statement_client()
    today = date(2026, 9, 28)
    first = load_fundamentals(client, store, "AAPL", [], today)
    assert client.income_statement.call_count == 2
    assert client.balance_sheet.call_count == 2
    assert client.cash_flow.call_count == 2
    second = load_fundamentals(client, store, "AAPL", [], date(2026, 10, 15))
    assert client.income_statement.call_count == 2
    assert second == first
    assert load_fundamentals(None, store, "AAPL", [], today) == first


def test_earnings_refresh_once_daily_after_report_date():
    cache = {"fetched_on":"2026-09-28"}
    calendar = [{"symbol":"AAPL", "date":"2026-09-29"}]
    assert not refresh_due(cache,"AAPL",calendar,date(2026,9,28))
    assert refresh_due(cache,"AAPL",calendar,date(2026,9,29))
    assert not refresh_due({"fetched_on":"2026-09-29"},"AAPL",calendar,date(2026,9,29))
    assert refresh_due(cache,"AAPL",calendar,date(2026,9,30))
    assert not refresh_due(cache,"MSFT",calendar,date(2026,9,30))
    assert not refresh_due(cache,"AAPL",calendar,date(2026,10,5))


def test_failed_bundle_does_not_overwrite_previous_statements(store):
    client = statement_client()
    old = load_fundamentals(client, store, "AAPL", [], date(2026,9,28))
    client.cash_flow.side_effect = FMPRequestError("Failure", path="cash-flow-statement")
    with pytest.raises(FMPRequestError):
        load_fundamentals(client, store, "AAPL", [{"symbol":"AAPL","date":"2026-09-29"}], date(2026,9,30))
    assert store.document("fundamentals:AAPL") == old


def test_plan_restriction_preserves_previous_screen(store, settings):
    seed_market(store)
    old = run_screen(store, settings, now=NOW)
    client = statement_client()
    client.stats.api_calls = 0
    client.on_call = None
    client.earnings_calendar.return_value = []
    client.income_statement.side_effect = FMPPlanRestrictedError("Restricted", path="income-statement", status=402)
    with pytest.raises(FMPPlanRestrictedError):
        run_screen(store, settings, client, now=NOW)
    assert store.document("latest_screen") == old
    assert client.income_statement.call_count == 1


def test_manual_sponsorship_persists_independently(store):
    assert store.sponsorship("AAPL")["verdict"] == "Unreviewed"
    store.save_sponsorship("AAPL", "Pass", "Reviewed filing on 2026-09-28")
    assert store.sponsorship("AAPL")["verdict"] == "Pass"
    assert store.sponsorship("MSFT")["verdict"] == "Unreviewed"
    with pytest.raises(ValueError):
        store.save_sponsorship("AAPL", "maybe", "")


def test_bases_split_across_worker_processes_match_one_process(store, settings, monkeypatch):
    from stratlib import technical
    seed_market(store)
    report = run_screen(store, settings, now=NOW)
    market, t = report["market"], settings.thresholds
    symbols = ["LOW", "MID", "TOP"]
    alone = technical.stock_bases(store, symbols, report["price_date"], t, market, workers=1)
    assert alone =={row["symbol"]: row["base"] for row in report["rows"]}
    # Force the worker path on this small market: one stock per batch, over two processes.
    monkeypatch.setattr(technical, "PARALLEL_MIN_STOCKS", 1)
    monkeypatch.setattr(technical, "BATCH_STOCKS", 1)
    seen = []
    split = technical.stock_bases(store, symbols, report["price_date"], t, market, workers=2,
                                  progress=lambda done, total: seen.append((done, total)))
    assert split == alone
    assert seen[-1] == (3, 3)


def test_real_client_caches_all_seven_survivor_requests(mocked, client, store):
    from conftest import BASE, query_of
    fixtures = {
        "income-statement": "income-statement_AAPL_quarter_limit12.json",
        "balance-sheet-statement": "balance-sheet-statement_AAPL_annual_limit4.json",
        "cash-flow-statement": "cash-flow-statement_AAPL_annual_limit4.json",
        "splits": "splits_NVDA.json",
    }
    for endpoint, filename in fixtures.items():
        mocked.get(f"{BASE}/{endpoint}", json=load_fixture(filename))
    today = date(2026, 9, 28)
    load_fundamentals(client, store, "AAPL", [], today)
    load_fundamentals(client, store, "AAPL", [], today)
    assert client.stats.api_calls == 7
    assert len(mocked.calls) == 7
    queries = [query_of(call.request) for call in mocked.calls]
    assert queries[0] == {"symbol":"AAPL", "period":"quarter", "limit":"12"}
    assert queries[1] == {"symbol":"AAPL", "period":"annual", "limit":"4"}
