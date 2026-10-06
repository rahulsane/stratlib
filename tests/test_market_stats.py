from dataclasses import replace
from datetime import date, timedelta

import pytest

from stratlib.config import Thresholds
from stratlib.market_stats import PriceMetrics, industry_ranks, price_filter_checks, price_metrics, rs_ratings
from stratlib.prices import Bar, parse_bars
from conftest import load_fixture


def synthetic_bars(gain=1.0):
    """Explicitly synthetic daily prices for controlled cross-sectional tests."""
    return [Bar((date(2025, 1, 1) + timedelta(days=i)).isoformat(), 20, 25 + gain * i,
                19, 20 + gain * i, 500_000) for i in range(253)]


def test_weighted_separate_quarters():
    bars = synthetic_bars()
    score = price_metrics(bars, [b.date for b in bars], Thresholds()).rs_score
    closes = [bars[-1-i*63].close for i in range(5)]
    returns = [closes[i]/closes[i+1]-1 for i in range(4)]
    assert score == pytest.approx(100 * (2*returns[0]+sum(returns[1:]))/5)


def test_recorded_prices_prefilter():
    bars = parse_bars(load_fixture("historical-price-eod_full_AAPL_2025-09-01.json"))
    m = price_metrics(bars, [b.date for b in bars], Thresholds())
    assert m.close == bars[-1].close
    assert m.ma50 == pytest.approx(sum(b.close for b in bars[-50:])/50)
    assert m.avg_volume == pytest.approx(sum(b.volume for b in bars[-50:])/50)


def test_prefilter_and_missing_history():
    bars = synthetic_bars()
    sessions = [b.date for b in bars]
    assert price_metrics(bars, sessions, Thresholds()).price_pass
    assert not price_metrics(bars, sessions, replace(Thresholds(), min_price=1000)).price_pass
    assert not price_metrics(bars, sessions, replace(Thresholds(), min_avg_volume=600000)).price_pass
    assert price_metrics(bars[1:], sessions, Thresholds()).rs_score is None
    assert price_metrics(bars[:-1], sessions, Thresholds()).close is None
    broken = bars[:100] + bars[101:]
    assert price_metrics(broken, sessions, Thresholds()).rs_score is None


def test_price_check_boundaries_and_unavailable_values():
    metrics = PriceMetrics(close=15, high_52w=20, below_high_pct=15, avg_volume=400000, ma50=15, ma200=14)
    checks = price_filter_checks(metrics, Thresholds())
    assert [c.passed for c in checks] == [True, True, True, False, True]
    assert all(c.passed is None for c in price_filter_checks(PriceMetrics(), Thresholds()))
    bars = synthetic_bars()
    measured = price_metrics(bars, [b.date for b in bars], Thresholds())
    assert measured.price_pass == all(c.passed is True for c in price_filter_checks(measured, Thresholds()))


def test_percentile_endpoints_ties_and_singletons():
    assert rs_ratings({"a": 10, "b": 20, "c": 30}) == {"a": 1, "b": 50, "c": 99}
    assert rs_ratings({"a": 20, "b": 20}) == {"a": 50, "b": 50}
    assert rs_ratings({"a": 20, "b": None, "c": float("nan")}) == {"a": 50}


def test_industry_groups_are_equal_weight_and_tied_ranks():
    groups = industry_ranks({"a":"Tech", "b":"Tech", "c":"Oil", "d":"Empty", "e":None},
                            {"a":10, "b":30, "c":20, "d":None, "e":1000})
    assert groups["Tech"] == {"rank":1, "return_pct":20, "members":2}
    assert groups["Oil"]["rank"] == 1
    assert "Empty" not in groups
