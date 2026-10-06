"""Synthetic workspace cases: isolation, rule ownership, dates and account completeness."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from stratlib.config import BacktestSettings, Thresholds
from stratlib.portfolio import portfolio_plan
from stratlib.positions import account_values, assess_position, current_position, current_quantity
from stratlib.prices import Bar
from stratlib.scoring import Criterion
from stratlib.sell_rules import Position
from stratlib.store import Store
from stratlib.strategies import candidate_rows, latest_screen, project_screen, rule_snapshot

T = Thresholds(long_ma_sessions=5)
P = BacktestSettings()
DAY = "2026-09-29"


def prices(store, symbol="AAA", closes=(80, 83, 87, 90, 94)):
    start = date.fromisoformat(DAY) - timedelta(days=len(closes) - 1)
    bars = [Bar((start + timedelta(days=i)).isoformat(), c, c + 1, c - 1, c, 1e6) for i, c in enumerate(closes)]
    store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date,
                       checked_at=datetime.now(timezone.utc), status="ok")
    return bars


def tracking(strategy_id="canslim", quantity=None, t=T, p=P):
    snapshot = rule_snapshot(strategy_id, t, p)
    return {"strategy_id": strategy_id, "variant_id": snapshot["variant_id"],
            "rule_snapshot": snapshot, "quantity": quantity}


def facts():
    return {"price_date": DAY, "thresholds": rule_snapshot("trend", T, P)["thresholds"], "sample": None,
            "rows": [{"symbol": "AAA", "name": "Synthetic A", "rs": 99, "industry": "Test",
                      "industry_stats": {"rank": 1}, "prices": {"close": 94, "avg_volume": 1e6, "price_pass": True},
                      "criteria": [Criterion("c_sales", "C", "Sales", 30, True, "").to_dict()]}]}


def test_screens_are_scoped_dated_and_do_not_replace_other_strategies(store):
    a = store.save_screen(project_screen(facts(), "canslim", T, P))
    b = store.save_screen(project_screen(facts(), "trend", T, P))
    assert latest_screen(store, "canslim") == a == store.document("latest_screen")
    assert latest_screen(store, "trend") == b
    newer = store.save_screen(project_screen({**facts(), "price_date": "2026-09-30"}, "trend", T, P))
    assert store.document(b["screen_key"]) == b
    assert latest_screen(store, "trend") == newer
    assert len(store.document_keys("screen:trend:long-trend:")) == 2
    assert [r["symbol"] for r in b["candidates"]] == ["AAA"]
    assert not a["candidates"]  # Strong sales alone is not a CANSLIM breakout.


def test_projection_refuses_facts_from_different_thresholds():
    with pytest.raises(ValueError, match="Thresholds changed"):
        project_screen(facts(), "trend", replace(T, rs_min=95), P)


def test_existing_database_migrates_without_reassigning_positions(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY, symbol TEXT, entry_date TEXT, entry_price REAL, "
                     "breakout_date TEXT, breakout_price REAL, basis_date TEXT, basis_close REAL, closed_at TEXT, "
                     "created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO positions VALUES (1,'AAA','2026-09-01',100,NULL,NULL,NULL,NULL,NULL,'old','old')")
    store = Store(path)
    record = store.positions()[0]
    assert (record["strategy_id"], record["variant_id"], record["rule_snapshot"], record["quantity"]) == (
        "canslim", "breakout", None, None)
    assert record["entry_price"] == 100 and record["created_at"] == "old"
    store.close()
    store = Store(path)  # The migration is idempotent.
    assert store.positions()[0] == record
    store.close()


def test_frozen_rules_and_legacy_rules_remain_distinct(store):
    bars = prices(store)
    pos = Position("AAA", bars[0].date, 100)
    store.save_position(pos, as_of=DAY, tracking=tracking())
    store.save_position(pos, as_of=DAY)
    legacy, saved = store.positions()
    changed = replace(T, stop_loss_pct=5)
    assert assess_position(store, saved, changed, as_of=DAY).action == "Hold"
    assert assess_position(store, legacy, changed, as_of=DAY).action == "Sell"


def test_same_ticker_can_have_different_saved_exits_and_portfolios(store):
    bars = prices(store)
    pos = Position("AAA", bars[0].date, 120)
    store.save_position(pos, as_of=DAY, tracking=tracking("canslim"))
    store.save_position(pos, as_of=DAY, tracking=tracking("trend"))
    results = {r["strategy_id"]: assess_position(store, r, T, as_of=DAY).action for r in store.positions()}
    assert results == {"canslim": "Sell", "trend": "Hold"}
    report = project_screen(facts(), "trend", T, P)
    plan = portfolio_plan(store, report, "trend", T, P, {"as_of": DAY, "exposure": 100}, DAY)
    assert len(plan["records"]) == 1 and plan["records"][0]["strategy_id"] == "trend"
    assert plan["candidates"][0]["Plan"] == "Held"


def test_edit_close_reopen_preserve_saved_rules_and_quantity(store):
    bars = prices(store)
    pos = Position("AAA", bars[0].date, 100)
    ident = store.save_position(pos, as_of=DAY, tracking=tracking("trend", 12))
    store.save_position(replace(pos, entry_price=110), as_of=DAY, position_id=ident)
    store.set_position_closed(ident, True)
    record = store.positions(closed=True)[0]
    assert record["quantity"] == 12 and record["rule_snapshot"] == tracking("trend")["rule_snapshot"]
    store.set_position_closed(ident, False)
    assert store.positions()[0]["rule_snapshot"] == record["rule_snapshot"]
    assert store.positions()[0]["entry_price"] == 110


def test_split_restatement_adjusts_both_shares_and_cost(store):
    bars = prices(store)
    store.save_position(Position("AAA", bars[0].date, 100), as_of=DAY, tracking=tracking(quantity=10))
    record = store.positions()[0]
    prices(store, closes=tuple(b.close / 2 for b in bars))
    assert current_position(store, record).entry_price == 50
    assert current_quantity(store, record) == 20
    result = assess_position(store, record, T, as_of=DAY)
    assert result.gain_pct == pytest.approx(-6)
    account = account_values(store, [record], {record["id"]: result}, 60)
    assert account["holdings"] == 940 and account["total"] == 1000
    assert account["weights"][record["id"]] == 94


@pytest.mark.parametrize("quantity", [None, 10])
def test_unknown_shares_or_cash_do_not_produce_total_or_weights(store, quantity):
    bars = prices(store)
    store.save_position(Position("AAA", bars[0].date, 100), as_of=DAY, tracking=tracking(quantity=quantity))
    record = store.positions()[0]
    result = assess_position(store, record, T, as_of=DAY)
    account = account_values(store, [record], {record["id"]: result}, None if quantity else 100)
    assert account["total"] is None and account["weights"][record["id"]] is None
    if quantity is None:
        assert account["holdings"] is None


@pytest.mark.parametrize("quantity", [0, -1, float("inf"), float("nan"), True])
def test_invalid_quantity_cannot_mutate_a_position(store, quantity):
    with pytest.raises(ValueError, match="Shares"):
        store.save_position(Position("AAA", DAY, 100), as_of=DAY, tracking=tracking(quantity=quantity))
    assert store.positions() == []


def test_mismatched_saved_rules_cannot_mutate_a_position(store):
    with pytest.raises(ValueError, match="match"):
        store.save_position(Position("AAA", DAY, 100), as_of=DAY,
                            tracking={**tracking(), "rule_snapshot": tracking("trend")["rule_snapshot"]})
    assert not store.positions()


@pytest.mark.parametrize("change,market", [({"sample": 10}, {"as_of": DAY, "exposure": 100}),
    ({"price_date": "2026-01-01"}, {"as_of": DAY, "exposure": 100}),
    ({}, {"as_of": "2026-09-28", "exposure": 100}), ({}, {"as_of": DAY, "exposure": None}),
    ({"rule_snapshot": None}, {"as_of": DAY, "exposure": 100})])
def test_incomplete_or_stale_evidence_blocks_proposed_entries(store, change, market):
    report = {**project_screen(facts(), "trend", T, P), **change}
    plan = portfolio_plan(store, report, "trend", T, P, market, DAY)
    assert plan["issues"] and plan["candidates"][0]["Plan"] == "Review data"


def test_market_gate_limits_new_buys_without_forcing_trend_sales(store):
    bars = prices(store, "OLD")
    store.save_position(Position("OLD", bars[0].date, 100), as_of=DAY, tracking=tracking("trend"))
    report = project_screen(facts(), "trend", T, P)
    zero = portfolio_plan(store, report, "trend", T, P, {"as_of": DAY, "exposure": 0}, DAY)
    full = portfolio_plan(store, report, "trend", T, P, {"as_of": DAY, "exposure": 100}, DAY)
    assert not zero["issues"] and not zero["trims"] and zero["free"] == 0
    assert zero["candidates"][0]["Plan"] == "Watch" and full["candidates"][0]["Plan"] == "Proposed buy"
    assert next(iter(zero["assessments"].values())).action == "Hold"


def test_multiple_lots_hold_a_slot_until_all_have_exit_signals(store):
    bars = prices(store)
    store.save_position(Position("AAA", bars[0].date, 100), as_of=DAY,
                        tracking=tracking("trend", p=replace(P, trend_loss_cap_pct=5)))
    store.save_position(Position("AAA", bars[0].date, 90), as_of=DAY, tracking=tracking("trend"))
    report = project_screen(facts(), "trend", T, P)
    plan = portfolio_plan(store, report, "trend", T, P, {"as_of": DAY, "exposure": 100}, DAY)
    assert {a.action for a in plan["assessments"].values()} == {"Sell", "Hold"}
    assert plan["held_count"] == 1 and plan["free"] == P.max_holdings - 1


def test_trend_exit_uses_saved_window_and_no_future_closes(store):
    bars = prices(store, closes=(90, 90, 90, 90, 80))
    store.save_position(Position("AAA", bars[0].date, 90), as_of=DAY, tracking=tracking("trend"))
    record = store.positions()[0]
    result = assess_position(store, record, replace(T, long_ma_sessions=200), as_of=DAY)
    assert result.action == "Sell" and result.trend_line == 88
    before = assess_position(store, record, T, as_of=bars[-2].date)
    assert before.action == "Unavailable"  # Only four historical bars, not five plus a future close.
