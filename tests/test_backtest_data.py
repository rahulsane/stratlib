"""Recorded FMP schemas and synthetic dated archives. All I/O stays offline."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from stratlib.backtest import BacktestError
from stratlib.backtest_data import (available_bundle, capture_universe, coverage, import_archive,
                                  refresh_delistings, restore_price_basis, run_backtest, statement_available_at)
from stratlib.cli import main
from stratlib.prices import Bar
from stratlib.scoring import score_fundamentals
from conftest import BASE, load_fixture
from test_bases import daily_from_weeks, synthetic_flat


def synthetic_bundle():
    """Fabricated growth histories with every C/A/S check passing."""
    quarters, annuals, balances, cash = [], [], [], []
    for i in range(12):
        year, quarter = 2022 + i // 4, 1 + i % 4
        month = quarter * 3
        end = date(year, month, 31 if month in (3, 12) else 30)
        income = 100 * 1.15 ** (i + i * i / 10)
        quarters.append({"date": end.isoformat(), "fiscalYear": year, "period": f"Q{quarter}",
                         "acceptedDate": (end + timedelta(days=15)).isoformat() + " 10:00:00",
                         "reportedCurrency": "USD", "weightedAverageShsOutDil": 100,
                         "epsDiluted": income / 100, "netIncome": income,
                         "netIncomeFromContinuingOperations": income, "revenue": income * 10})
    for i in range(4):
        year, income = 2021 + i, 100 * 2 ** i
        meta = {"date": f"{year}-12-31", "fiscalYear": year, "period": "FY", "reportedCurrency": "USD",
                "acceptedDate": f"{year + 1}-01-20 10:00:00"}
        annuals.append({**meta, "epsDiluted": income / 100, "netIncome": income,
                        "netIncomeFromContinuingOperations": income, "weightedAverageShsOutDil": 100})
        balances.append({**meta, "totalDebt": 100, "totalStockholdersEquity": income * 2})
        cash.append({**meta, "operatingCashFlow": income * 1.5})
    return {"income_quarter": quarters, "income_annual": annuals, "balance_quarter": [],
            "cash_quarter": [], "balance_annual": balances, "cash_annual": cash,
            "splits": [], "share_basis_date": "2025-02-01", "fetched_on": "2025-02-01"}


def seed_archive(store, settings):
    """Two historical companies, one later delisted, and a real end-to-end rule path."""
    t = replace(settings.thresholds, high_sessions=25, short_ma_sessions=5, long_ma_sessions=20,
                volume_sessions=25, rs_quarter_sessions=6, industry_sessions=20,
                breakout_volume_sessions=25, distribution_window_sessions=5)
    settings = replace(settings, thresholds=t, backtest=replace(settings.backtest, max_holdings=1))
    start, end = "2025-02-10", "2025-02-13"
    prior = []
    day = date(2024, 11, 1)
    while day < date(2025, 1, 6):
        if day.weekday() < 5:
            price = 60 + len(prior) * .6
            prior.append(Bar(day.isoformat(), price, price + .5, price - .5, price, 1_000_000))
        day += timedelta(days=1)
    prices = prior + [replace(b, volume=b.volume * 1000) for b in daily_from_weeks(synthetic_flat())]
    prices += [Bar(start, 100, 103, 99, 102, 1_400_000), Bar("2025-02-11", 103, 105, 101, 104, 1_000_000),
               Bar("2025-02-12", 104, 105, 90, 98, 1_000_000), Bar(end, 98, 99, 96, 97, 1_000_000)]
    benchmark = [Bar(b.date, 100 + i * 3, 101 + i * 3, 99 + i * 3, 100 + i * 3, 1_000_000 + i)
                 for i, b in enumerate(prices)]
    other = [Bar(b.date, 50, 51, 49, 50, 1_000_000) for b in prices]
    now = datetime(2025, 2, 14, 22, tzinfo=timezone.utc)
    for symbol, bars in {"AAA": prices, "BBB": other, "SPY": benchmark, "QQQ": benchmark,
                         "^GSPC": benchmark, "^IXIC": benchmark}.items():
        store.write_prices(symbol, bars, replace=True, requested_from=prices[0].date, checked_at=now, status="ok")
        store.save_document(f"backtest:splits:{symbol}", {"fetched_on": "2025-02-14", "rows": []})
    stocks = [{"symbol": s, "companyName": s, "exchange": "NYSE", "industry": "Technology",
               "isEtf": False, "isFund": False} for s in ("AAA", "BBB")]
    payload = {"schema_version": 1,
               "universes": [{"date": b.date, "observed_at": f"{b.date}T12:00:00-05:00",
                              "source": "SYNTHETIC TEST UNIVERSE", "complete": True, "stocks": deepcopy(stocks)}
                             for b in prices if start <= b.date <= end],
               "fundamentals": [{"symbol": s, "observed_at": "2025-02-01T12:00:00-05:00",
                                 "source": "SYNTHETIC TEST STATEMENTS", "bundle": synthetic_bundle()} for s in ("AAA", "BBB")]}
    import_archive(store, payload, now=now)
    store.save_document("backtest:delisted", {"complete": True, "fetched_at": now.isoformat(), "rows": [
        {"symbol": "AAA", "companyName": "AAA", "exchange": "NYSE", "ipoDate": "2010-01-01", "delistedDate": "2025-03-01"}]})
    return settings, start, end, payload


def test_end_to_end_historical_screen_signal_stop_and_persistence(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    assert all(c.passed for c in score_fundamentals(synthetic_bundle(), settings.thresholds, date.fromisoformat(start))[0])
    report = run_backtest(store, settings, start, end)
    assert report["signal_counts"]["signals"] == 1
    trade = report["trades"][0]
    assert (trade["symbol"], trade["entry_date"], trade["exit_date"]) == ("AAA", "2025-02-11", "2025-02-12")
    assert trade["entry_price"] == 103
    assert trade["return_pct"] == pytest.approx(-7)
    assert report["metrics"]["ending_equity"] == pytest.approx(93000)
    assert report["api_calls"] == 0
    assert store.document("latest_backtest") == report
    assert store.document(f"backtest:result:{report['created_at']}") == report
    assert report["daily_screen"][0]["members"] == 2
    assert any(c["key"] == "l_rs" and c["value"] == 99 for c in trade["evidence"]["criteria"])


def test_today_universe_and_future_statement_revisions_cannot_change_past(store, settings):
    from stratlib.universe import parse_listings
    settings, start, end, payload = seed_archive(store, settings)
    original = run_backtest(store, settings, start, end)
    # Survivorship-biased current metadata is deliberately contradictory.
    store.replace_universe(parse_listings([{"symbol": "BBB", "industry": "New industry", "price": 9999}]))
    revision = deepcopy(payload["fundamentals"][0])
    revision["observed_at"] = "2025-03-01T12:00:00-05:00"
    revision["bundle"]["income_quarter"][-1]["epsDiluted"] = -999
    import_archive(store, {"schema_version": 1, "fundamentals": [revision]})
    revised = run_backtest(store, settings, start, end)
    assert revised["trades"] == original["trades"]
    assert revised["equity_curve"] == original["equity_curve"]


def test_no_universe_forward_fill_or_current_fundamentals_fallback(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    store._conn.execute("DELETE FROM research_vintages WHERE kind='universe' AND item=?", (start,))
    with pytest.raises(BacktestError, match="Missing dated universe"):
        run_backtest(store, settings, start, end)
    assert store.document("latest_backtest") is None


def test_missing_vintage_preserves_previous_completed_result(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    original = run_backtest(store, settings, start, end)
    store._conn.execute("DELETE FROM research_vintages WHERE kind='fundamentals'")
    store.save_document("fundamentals:AAA", synthetic_bundle())
    with pytest.raises(BacktestError, match="no archived statement vintage"):
        run_backtest(store, settings, start, end)
    assert store.document("latest_backtest") == original


def test_omitted_delisted_member_blocks_survivorship_biased_archive(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    directory = store.document("backtest:delisted")
    directory["rows"].append({"symbol": "DEAD", "companyName": "Dead Company", "exchange": "NYSE",
                              "ipoDate": "2010-01-01", "delistedDate": "2025-02-12"})
    store.save_document("backtest:delisted", directory)
    with pytest.raises(BacktestError, match="omits formerly listed companies: DEAD"):
        run_backtest(store, settings, start, end)


def test_ticker_reuse_outside_the_period_does_not_change_historical_membership(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    original = run_backtest(store, settings, start, end)
    directory = store.document("backtest:delisted")
    directory["rows"] += [
        {"symbol": "AAA", "exchange": "NYSE", "ipoDate": "2000-01-01", "delistedDate": "2009-01-01"},
        {"symbol": "AAA", "exchange": "NYSE", "ipoDate": "2026-01-01", "delistedDate": "2027-01-01"},
    ]
    store.save_document("backtest:delisted", directory)
    result = run_backtest(store, settings, start, end)
    assert result["trades"] == original["trades"]
    assert result["equity_curve"] == original["equity_curve"]


@pytest.mark.parametrize("damage,match", [("splits", "split history"), ("price", "missing historical member"),
                                         ("gap", "price gaps"), ("industry", "industry classification")])
def test_historical_coverage_errors_are_not_silent_exclusions(store, settings, damage, match):
    settings, start, end, _ = seed_archive(store, settings)
    if damage == "splits":
        store._conn.execute("DELETE FROM screening_data WHERE key='backtest:splits:BBB'")
    elif damage == "price":
        store._conn.execute("DELETE FROM prices WHERE symbol='BBB' AND date=?", (start,))
    elif damage == "gap":
        store._conn.execute("DELETE FROM prices WHERE symbol='BBB' AND date='2025-02-05'")
    else:
        for vintage in store.vintages("universe"):
            vintage["body"]["stocks"][1]["industry"] = None
            import json
            store._conn.execute("UPDATE research_vintages SET body=? WHERE kind='universe' AND item=?",
                                (json.dumps(vintage["body"]), vintage["item"]))
    with pytest.raises(BacktestError, match=match):
        run_backtest(store, settings, start, end)


def test_development_sample_keeps_full_cross_section_for_ranks(store, settings):
    settings, start, end, _ = seed_archive(store, settings)
    result = run_backtest(store, settings, start, end, sample=1)
    assert result["sample"] == 1
    assert result["daily_screen"][0]["ranked"] == 2
    assert result["trades"][0]["evidence"]["criteria"][-5]["key"] == "l_rs"


def test_public_timing_recorded_acceptance_date_and_fallbacks():
    row = load_fixture("income-statement_AAPL_quarter_limit12.json")[0]
    actual = statement_available_at(row)
    assert actual.date().isoformat() >= row["acceptedDate"][:10]
    assert statement_available_at({"date": "2024-12-31", "period": "Q4"}).astimezone(timezone.utc).date() == date(2025, 2, 14)
    assert statement_available_at({"date": "2024-12-31", "period": "FY"}).date() == date(2025, 3, 31)
    assert statement_available_at({"date": "2024-12-31", "filingDate": "2025-02-10"}).date() == date(2025, 2, 11)


def test_after_hours_and_unobserved_restatements_are_unavailable():
    bundle = synthetic_bundle()
    bundle["income_quarter"] = [{"date": "2024-12-31", "period": "Q4", "acceptedDate": "2025-02-10 16:05:00"}]
    vintage = {"observed_at": "2025-02-10T12:00:00-05:00", "body": {"bundle": bundle}}
    assert available_bundle([vintage], "2025-02-10")["income_quarter"] == []
    assert len(available_bundle([vintage], "2025-02-11")["income_quarter"]) == 1
    vintage["observed_at"] = "2025-02-11T12:00:00-05:00"
    assert available_bundle([vintage], "2025-02-10") is None


def test_split_normalization_restores_historical_absolute_price_and_volume():
    bars = [Bar("2024-06-07", 10, 11, 9, 10, 1_000_000)]
    splits = load_fixture("splits_NVDA.json")
    restored, factor = restore_price_basis(bars, splits, "2024-06-12", "2024-06-07")
    assert factor == 10
    assert restored[0].close == 100 and restored[0].volume == 100000
    assert restore_price_basis(bars, splits, "2024-06-12", "2024-06-12")[1] == 1


def test_archive_import_is_idempotent_immutable_and_validated_before_write(store, settings):
    settings, start, end, payload = seed_archive(store, settings)
    original_count = len(store.vintages("universe"))
    import_archive(store, payload)
    assert len(store.vintages("universe")) == original_count
    changed = deepcopy(payload)
    changed["universes"][0]["stocks"][0]["industry"] = "Future industry"
    with pytest.raises(ValueError, match="already exists"):
        import_archive(store, changed)
    assert len(store.vintages("universe")) == original_count
    invalid = deepcopy(payload)
    invalid["universes"][0]["observed_at"] = "2025-02-10T11:00:00-05:00"
    invalid["settlements"] = [{"symbol": "AAA", "cash_per_share": -1}]
    with pytest.raises(BacktestError, match="Settlements"):
        import_archive(store, invalid)
    assert len(store.vintages("universe")) == original_count


@pytest.mark.parametrize("changes", [{"observed_at": "2035-02-10T12:00:00-05:00"},
                                     {"observed_at": "2025-02-10T18:00:00-05:00"},
                                     {"observed_at": "2025-02-10T12:00:00"}, {"complete": False}, {"source": ""}])
def test_archive_rejects_false_chronology_and_missing_provenance(store, settings, changes):
    _, _, _, payload = seed_archive(store, settings)
    item = deepcopy(payload["universes"][0])
    item.update(changes)
    with pytest.raises(BacktestError):
        import_archive(store, {"schema_version": 1, "universes": [item]})


def test_delisted_pagination_uses_recorded_schema_and_shared_cache(mocked, client, store):
    rows = load_fixture("delisted-companies_page0.json")
    mocked.get(f"{BASE}/delisted-companies?page=0&limit=100", json=rows, match=[
        __import__("responses").matchers.query_param_matcher({"page": "0", "limit": "100"})])
    mocked.get(f"{BASE}/delisted-companies?page=1&limit=100", json=[], match=[
        __import__("responses").matchers.query_param_matcher({"page": "1", "limit": "100"})])
    first = refresh_delistings(client, store)
    assert first["complete"] and first["rows"] == rows and first["api_calls"] == 2
    second = refresh_delistings(client, store)
    assert second["api_calls"] == 0
    assert len(mocked.calls) == 2


def test_repeated_delisted_page_never_marks_directory_complete(client, store, monkeypatch):
    monkeypatch.setattr(client, "delisted_companies", lambda **_: load_fixture("delisted-companies_page0.json"))
    with pytest.raises(BacktestError, match="repeated"):
        refresh_delistings(client, store)
    assert store.document("backtest:delisted") is None


def test_capture_refuses_backdating_an_after_close_current_universe(client, store, settings):
    with pytest.raises(BacktestError, match="before 13:00"):
        capture_universe(client, store, settings, now=datetime(2025, 2, 10, 22, tzinfo=timezone.utc))
    assert client.stats.api_calls == 0


def test_cli_check_and_run_need_no_key(settings, monkeypatch, capsys):
    monkeypatch.delenv("FMP_API_KEY")
    assert main(["--config", str(settings.path), "backtest", "--start", "2025-01-01", "--end", "2025-02-01", "--check"]) == 1
    assert '"ready": false' in capsys.readouterr().out
    assert main(["--config", str(settings.path), "backtest", "--start", "wrong", "--end", "2025-02-01"]) == 1
