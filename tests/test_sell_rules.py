"""Recorded FMP closes plus explicitly synthetic rule-boundary sequences."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from stratlib.config import Thresholds
from stratlib.prices import Bar, parse_bars
from stratlib.sell_rules import Position, evaluate_sell_rules
from conftest import load_fixture


def synthetic_bars(values, start="2026-01-05"):
    first = date.fromisoformat(start)
    return [Bar((first + timedelta(days=offset)).isoformat(), value, value, value, value, 1000)
            for offset, value in values]


def evaluate(values, *, position=None, thresholds=None, as_of=None, sessions=True):
    bars = synthetic_bars(values)
    return evaluate_sell_rules(position or Position("TEST", "2026-01-05", 100, "2026-01-05", 100),
                               bars, thresholds or Thresholds(), as_of=as_of or bars[-1].date,
                               sessions=[b.date for b in bars] if sessions is True else sessions)


@pytest.mark.parametrize("price, action", [(92.99, "Sell"), (93, "Sell"), (93.01, "Hold"),
                                           (119.99, "Hold"), (120, "Take profits"), (130, "Take profits")])
def test_default_stop_and_target_boundaries(price, action):
    result = evaluate([(0, 100), (21, 110), (56, price)])
    assert result.action == action
    assert result.stop_price == pytest.approx(93)
    assert result.target_price == pytest.approx(120)


def test_fast_gain_holds_through_day_55_and_expires_on_day_56():
    result = evaluate([(0, 100), (21, 120), (55, 130)])
    assert result.action == "Hold exception"
    assert result.fast_gain_date == "2026-01-26"
    assert result.hold_until == "2026-03-02"
    assert evaluate([(0, 100), (21, 120), (56, 130)]).action == "Take profits"
    assert evaluate([(0, 100), (21, 120), (56, 110)]).action == "Hold"


def test_gain_after_three_weeks_does_not_qualify():
    result = evaluate([(0, 100), (21, 119.99), (22, 120)])
    assert result.action == "Take profits"
    assert result.exception_status == "Not qualified"


def test_stop_overrides_active_hold_and_hold_survives_a_retreat():
    assert evaluate([(0, 100), (14, 125), (30, 93)]).action == "Sell"
    assert evaluate([(0, 100), (14, 125), (30, 110)]).action == "Hold exception"


def test_exception_clock_and_gain_use_breakout_not_entry():
    position = Position("TEST", "2026-01-20", 119, "2026-01-05", 100)
    result = evaluate([(0, 100), (20, 120), (55, 145)], position=position)
    assert result.action == "Hold exception"
    assert result.gain_pct == pytest.approx((145 / 119 - 1) * 100)
    assert result.hold_until == "2026-03-02"


def test_future_bars_cannot_create_a_hold_or_change_the_current_close():
    result = evaluate([(0, 100), (5, 105), (14, 125), (60, 80)], as_of="2026-01-10")
    assert result.action == "Hold"
    assert result.close == 105
    assert result.fast_gain_date is None


def test_intraday_high_does_not_trigger_fast_gain_and_low_does_not_trigger_eod_stop():
    bars = [Bar("2026-01-05", 100, 125, 90, 105, 100)]
    result = evaluate_sell_rules(Position("TEST", "2026-01-05", 100, "2026-01-05", 100),
                                 bars, Thresholds(), as_of="2026-01-05", sessions=[bars[0].date])
    assert result.action == "Hold"
    assert result.fast_gain_date is None


def test_missing_breakout_or_missing_early_history_requests_review():
    position = Position("TEST", "2026-01-05", 100)
    assert evaluate([(0, 125)], position=position).action == "Review breakout"
    assert evaluate([(0, 93)], position=position).action == "Sell"
    assert evaluate([(56, 125)], position=position).action == "Take profits"
    assert evaluate([(22, 125)], sessions=["2026-01-05", "2026-01-27"]).action == "Review breakout"
    assert evaluate([(0, 100), (22, 125)], sessions=None).action == "Review breakout"
    assert evaluate([(0, 100), (22, 125)], sessions=["2026-01-05", "2026-01-06", "2026-01-27"]).action == "Review breakout"


def test_observed_fast_gain_is_sufficient_even_if_other_bars_are_missing():
    assert evaluate([(10, 125), (30, 125)], sessions=None).action == "Hold exception"


def test_custom_thresholds_change_all_rules():
    thresholds = replace(Thresholds(), stop_loss_pct=8, profit_target_pct=25,
                         fast_gain_pct=30, fast_gain_weeks=2, minimum_hold_weeks=10)
    assert evaluate([(0, 100), (70, 93)], thresholds=thresholds).action == "Hold"
    assert evaluate([(0, 100), (70, 92)], thresholds=thresholds).action == "Sell"
    assert evaluate([(0, 100), (70, 124.99)], thresholds=thresholds).action == "Hold"
    assert evaluate([(0, 100), (70, 125)], thresholds=thresholds).action == "Take profits"
    assert evaluate([(0, 100), (14, 130), (69, 140)], thresholds=thresholds).action == "Hold exception"


def test_no_data_or_invalid_latest_close_is_unavailable():
    position = Position("TEST", "2026-01-06", 100)
    for bars in ([], synthetic_bars([(0, 100)]), synthetic_bars([(1, float("nan"))])):
        assert evaluate_sell_rules(position, bars, Thresholds(), as_of="2026-01-06").action == "Unavailable"
    assert evaluate([(0, 100)], position=position).action == "Unavailable"


def test_recorded_fmp_prices_support_real_position_assessment():
    bars = parse_bars(load_fixture("cached-price-bars_APA_2026-09-25.json"))
    position = Position("APA", bars[0].date, bars[0].close, bars[0].date, bars[0].close)
    result = evaluate_sell_rules(position, bars, Thresholds(), as_of=bars[-1].date)
    assert result.price_date == "2026-09-25"
    assert result.close == bars[-1].close
    assert result.gain_pct == pytest.approx((bars[-1].close / bars[0].close - 1) * 100)
    expected = "Sell" if result.gain_pct <= -7 else "Take profits" if result.gain_pct >= 20 else "Hold"
    assert result.action == expected


@pytest.mark.parametrize("kwargs", [
    {"symbol": ""}, {"symbol": "AAPL;DROP"}, {"entry_price": 0}, {"entry_price": float("inf")},
    {"entry_price": True}, {"entry_date": "invalid"}, {"breakout_date": "2026-01-01"},
    {"breakout_date": "2026-01-06", "breakout_price": 100},
    {"breakout_date": "2026-01-01", "breakout_price": -1},
])
def test_position_validation(kwargs):
    with pytest.raises(ValueError):
        Position(**({"symbol": "TEST", "entry_date": "2026-01-05", "entry_price": 100} | kwargs))
