"""stratlib publish: a read-only snapshot holding what the public pages read, and nothing private."""

import sqlite3
from datetime import timedelta

import pytest

from stratlib.publish import SNAPSHOT, PublishError, publish
from stratlib.screening import run_screen
from stratlib.sell_rules import Position
from stratlib.store import Store
from test_screening import NOW, seed_market


def first_bar(store, symbol):
    return store.price_history(symbol, limit=None)[0].date


def test_a_snapshot_keeps_the_public_data_and_leaves_private_records_behind(settings, tmp_path):
    store = Store(settings.data.db_path)
    try:
        seed_market(store)
        report = run_screen(store, settings, now=NOW)
        store.save_position(Position("TOP", "2025-06-02", 20.0), as_of=report["price_date"])
        store.save_sponsorship("TOP", "Pass", "Private notes")
        store.save_document("portfolio:cash:canslim:breakout", {"amount": 500.0, "recorded_at": "2025-09-20"})
        store.save_document("jev:review", {"answer": "private"})
        store.save_document("research:nash:AAA", {"rows": []})
        # Comparable backtests: only each strategy's newest run is published.
        for key in ("canslim:2026-09-01T00:00:00+00:00", "canslim:2026-10-01T00:00:00+00:00", "trend:2026-09-15T00:00:00+00:00"):
            store.save_document(f"backtest:engine:{key}", {"key": key})
        store.put_response("https://example.test/endpoint", {"cached": True})
        store.finish_run(store.start_run("backfill", {}), {"api_calls": 1})
        spy_start, top_start = first_bar(store, "SPY"), first_bar(store, "TOP")
    finally:
        store.close()

    output = tmp_path / "public" / "stratlib.db"
    summary = publish(settings, output, years=0.5, now=NOW)
    assert summary["screens"] == {"canslim": report["price_date"]}
    assert summary["prices_since"] > top_start

    snapshot = Store(output, read_only=True)
    try:
        assert snapshot.document("latest_screen")["price_date"] == report["price_date"]
        # Parsed once and shared by every visitor, since nothing can change it.
        assert snapshot.document("latest_screen") is snapshot.document("latest_screen")
        assert snapshot.document(SNAPSHOT)["prices_through"] == summary["prices_through"]
        assert len(snapshot.universe()) == 3
        # Stocks keep recent prices; the market symbols keep their whole history for market direction.
        assert first_bar(snapshot, "TOP") >= summary["prices_since"]
        assert first_bar(snapshot, "SPY") == spy_start
        assert snapshot.positions() == []
        assert snapshot.sponsorship("TOP") == {"verdict": "Unreviewed", "notes": ""}
        assert snapshot.document_keys("backtest:engine:") == ["backtest:engine:trend:2026-09-15T00:00:00+00:00",
                                                              "backtest:engine:canslim:2026-10-01T00:00:00+00:00"]
        assert summary["comparable_backtests"] == 2
        for key in ("portfolio:cash:canslim:breakout", "jev:review", "research:nash:AAA",
                    "backtest:engine:canslim:2026-09-01T00:00:00+00:00"):
            assert snapshot.document(key) is None
        assert snapshot.last_runs("backfill") == []
        assert snapshot.get_response("https://example.test/endpoint", max_age=timedelta(days=9999)) is None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            snapshot.save_document("anything", {"written": True})
    finally:
        snapshot.close()
    # A single file: no WAL beside it once closed, so it can be copied to a server as it is.
    with sqlite3.connect(output) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not output.with_name(output.name + ".partial").exists()


def test_publishing_refuses_to_replace_the_working_database(settings):
    Store(settings.data.db_path).close()
    with pytest.raises(PublishError, match="working database"):
        publish(settings, settings.data.db_path)


def test_a_read_only_store_needs_an_existing_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError, match="stratlib publish"):
        Store(tmp_path / "missing.db", read_only=True)
    assert not (tmp_path / "missing.db").exists()
