from datetime import date, datetime, timezone

import pytest

from stratlib.prices import (
    ET,
    Bar,
    eod_cutoff,
    history_start,
    is_restated,
    last_eod_release,
    parse_bars,
    plan_price_job,
)
from stratlib.store import PriceState

from conftest import load_fixture


def test_parse_bars_sorts_oldest_first():
    rows = load_fixture("historical-price-eod_full_AAPL_2025-09-01.json")

    bars = parse_bars(rows)

    assert len(bars) == len(rows)
    assert bars[0].date == "2025-09-02"
    assert bars[-1].date == "2026-09-28"
    assert [b.date for b in bars] == sorted({b.date for b in bars})
    assert all(isinstance(b.close, float) and b.volume > 0 for b in bars)


def test_parse_bars_drops_rows_without_close_and_keeps_last_duplicate():
    rows = [
        {"date": "2026-01-05", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10},
        {"date": "2026-01-02", "close": None},
        {"date": "2026-01-05", "open": 1, "high": 2, "low": 1, "close": 1.6, "volume": 11},
        {"close": 3.0},
    ]
    assert parse_bars(rows) == [Bar("2026-01-05", 1.0, 2.0, 1.0, 1.6, 11.0)]


def test_fmp_history_is_split_adjusted():
    # NVDA split 10-for-1 effective 2024-06-10. Adjusted closes stay near $120
    # on both sides, and volume is adjusted too.
    bars = {b.date: b for b in parse_bars(
        load_fixture("historical-price-eod_full_NVDA_2024-06-05_2024-06-12.json")
    )}
    assert 115 < bars["2024-06-07"].close < 125
    assert 115 < bars["2024-06-10"].close < 125
    assert bars["2024-06-05"].volume > 100_000_000


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2026, 9, 28, 12, 0, tzinfo=ET), date(2026, 9, 27)),
        (datetime(2026, 9, 28, 17, 59, tzinfo=ET), date(2026, 9, 27)),
        (datetime(2026, 9, 28, 18, 0, tzinfo=ET), date(2026, 9, 28)),
        # 23:30 UTC on 28 Sep is 19:30 in New York
        (datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc), date(2026, 9, 28)),
    ],
)
def test_eod_cutoff(local, expected):
    assert eod_cutoff(local, 18) == expected


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2026, 9, 29, 19, 0, tzinfo=ET), datetime(2026, 9, 29, 18, 0, tzinfo=ET)),  # Tue evening
        (datetime(2026, 9, 29, 10, 0, tzinfo=ET), datetime(2026, 9, 28, 18, 0, tzinfo=ET)),  # Tue morning
        (datetime(2026, 9, 28, 10, 0, tzinfo=ET), datetime(2026, 9, 25, 18, 0, tzinfo=ET)),  # Mon morning
        (datetime(2026, 9, 27, 20, 0, tzinfo=ET), datetime(2026, 9, 25, 18, 0, tzinfo=ET)),  # Sunday
    ],
)
def test_last_eod_release_skips_weekends(local, expected):
    assert last_eod_release(local, 18) == expected


def test_history_start_handles_leap_day():
    assert history_start(date(2028, 2, 29), 5) == date(2023, 2, 28)
    assert history_start(date(2026, 9, 25), 5) == date(2021, 9, 25)


def test_is_restated():
    assert not is_restated(100.0, 100.2, 0.5)
    assert is_restated(100.0, 50.0, 0.5)  # 2-for-1 split
    assert is_restated(None, 50.0, 0.5)


CUTOFF = date(2026, 9, 28)
RELEASE = datetime(2026, 9, 28, 18, 0, tzinfo=ET)
START = date(2021, 9, 28)


def _state(**overrides):
    values = dict(
        symbol="AAPL",
        requested_from="2021-09-28",
        first_date="2021-09-28",
        last_date="2026-09-25",
        last_close=255.5,
        checked_at=datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc),
        status="ok",
        error=None,
    )
    values.update(overrides)
    return PriceState(**values)


def _plan(state):
    return plan_price_job("AAPL", state, desired_start=START, cutoff=CUTOFF, release=RELEASE)


def test_new_symbol_gets_full_history():
    job = _plan(None)
    assert job.full and job.start == START and job.end == CUTOFF


def test_stale_symbol_gets_incremental_fetch_overlapping_last_bar():
    job = _plan(_state())
    assert not job.full
    assert job.start == date(2026, 9, 25)
    assert job.overlap == ("2026-09-25", 255.5)
    assert job.requested_from == date(2021, 9, 28)


def test_symbol_checked_since_release_is_skipped():
    checked = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
    assert _plan(_state(checked_at=checked)) is None
    assert _plan(_state(checked_at=checked, status="restricted", last_date=None)) is None
    never_fetched = _state(
        checked_at=checked, status="restricted", requested_from=None,
        first_date=None, last_date=None, last_close=None,
    )
    assert _plan(never_fetched) is None


def test_errors_are_retried_on_the_next_run():
    checked = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
    job = _plan(_state(checked_at=checked, status="error"))
    assert job is not None and not job.full


def test_longer_history_setting_triggers_full_download():
    job = _plan(_state(requested_from="2023-01-01"))
    assert job.full and job.start == START


def test_symbol_without_bars_retries_full_range():
    job = _plan(_state(last_date=None, first_date=None, last_close=None, status="error"))
    assert job.full and job.start == date(2021, 9, 28)


def test_symbol_already_through_cutoff_is_skipped():
    assert _plan(_state(last_date="2026-09-28")) is None
