from dataclasses import replace
from datetime import datetime, timezone

import pytest

from stratlib.config import Thresholds
from stratlib.positions import assess_position, current_position
from stratlib.prices import Bar
from stratlib.sell_rules import Position
from stratlib.store import Store


def seed(store, close=130):
    bars = [Bar("2026-01-05", 100, 100, 100, 100, 1000),
            Bar("2026-03-02", close, close, close, close, 1000)]
    store.write_prices("TEST", bars, replace=True, requested_from=bars[0].date,
                       checked_at=datetime.now(timezone.utc), status="ok")
    return bars


def test_lots_survive_restart_and_close_is_reversible(store):
    seed(store)
    position = Position(" test ", "2026-01-05", 100, "2026-01-05", 100)
    first = store.save_position(position, as_of="2026-03-02")
    second = store.save_position(replace(position, entry_price=102), as_of="2026-03-02")
    reopened = Store(store.db_path)
    try:
        assert {r["id"] for r in reopened.positions()} == {first, second}
        assert reopened.positions()[0]["symbol"] == "TEST"
        reopened.set_position_closed(first, True)
        assert [r["id"] for r in store.positions()] == [second]
        assert [r["id"] for r in store.positions(closed=True)] == [first]
        with pytest.raises(ValueError, match="no longer open"):
            store.save_position(position, as_of="2026-03-02", position_id=first)
        reopened.set_position_closed(first, False)
        store.save_position(replace(position, entry_price=105), as_of="2026-03-02", position_id=first)
        assert next(r for r in reopened.positions() if r["id"] == first)["entry_price"] == 105
    finally:
        reopened.close()


def test_split_restatement_scales_cost_and_breakout_without_false_alert(store):
    bars = seed(store)
    store.save_position(Position("TEST", "2026-01-05", 100, "2026-01-05", 100), as_of="2026-03-02")
    record = store.positions()[0]
    before = assess_position(store, record, Thresholds(), as_of="2026-03-02")
    store.write_prices("TEST", [replace(b, close=b.close / 10) for b in bars], replace=True,
                       requested_from=bars[0].date, checked_at=datetime.now(timezone.utc), status="ok")
    after = assess_position(store, record, Thresholds(), as_of="2026-03-02")
    assert before.action == after.action == "Take profits"
    assert before.gain_pct == pytest.approx(after.gain_pct)
    assert after.stop_price == pytest.approx(9.3)
    assert current_position(store, record).entry_price == 10
    assert current_position(store, record).breakout_price == 10
    assert store.positions()[0]["entry_price"] == 100


def test_no_cache_can_be_saved_but_requires_price_basis_confirmation(store):
    value = Position("TEST", "2026-01-05", 100)
    store.save_position(value, as_of="2026-03-02")
    record = store.positions()[0]
    assert assess_position(store, record, Thresholds(), as_of="2026-03-02").action == "Unavailable"
    seed(store)
    assert assess_position(store, record, Thresholds(), as_of="2026-03-02").action == "Unavailable"
    store.save_position(value, as_of="2026-03-02", position_id=record["id"])
    assert store.positions()[0]["basis_date"] == "2026-03-02"


def test_future_entry_and_missing_position_fail(store):
    with pytest.raises(ValueError, match="future"):
        store.save_position(Position("TEST", "2027-01-05", 100), as_of="2026-03-02")
    with pytest.raises(ValueError, match="not found"):
        store.set_position_closed(999, True)


def test_pre_entry_cache_cannot_establish_the_entry_price_basis(store):
    seed(store)
    store.save_position(Position("TEST", "2026-03-03", 13), as_of="2026-03-03")
    assert store.positions()[0]["basis_date"] is None
