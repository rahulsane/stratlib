"""Recorded FMP observations plus explicitly synthetic transition boundaries."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from stratlib.config import Thresholds
from stratlib.market_direction import CONFIRMED, CORRECTION, PRESSURE, index_direction, market_direction
from stratlib.prices import Bar, parse_bars
from conftest import load_fixture

T = Thresholds()


def synthetic_market(n=26):
    days = []
    d = date(2025, 1, 1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(Bar(d.isoformat(), 100, 101, 99, 100, 1000 + len(days)))
        d += timedelta(days=1)
    return days


def append(bars, gain=0, volume=None, low=None):
    d = date.fromisoformat(bars[-1].date) + timedelta(days=1)
    while d.weekday() > 4:
        d += timedelta(days=1)
    close = bars[-1].close * (1 + gain / 100)
    bars.append(Bar(d.isoformat(), close, close + 1, low if low is not None else close - .01,
                    close, volume if volume is not None else bars[-1].volume + 10))


def evaluate(bars, proxy=None, t=T):
    return index_direction(bars, proxy or [], '^GSPC', 'SPY', t)


def test_recorded_index_declines_match_volume_and_return_rules():
    bars = parse_bars(load_fixture('historical-price-eod_full_GSPC_2026-08-01.json'))
    proxy = parse_bars(load_fixture('historical-price-eod_full_SPY_2026-08-01.json'))
    result = index_direction(bars, proxy, '^GSPC', 'SPY', T, as_of='2026-09-25')
    by_date = {b.date:b for b in bars}
    for previous, current in zip(result, result[1:]):
        a,b = by_date[previous.date],by_date[current.date]
        assert current.distribution == (100 * (b.close/a.close - 1) <= -.2 and b.volume > a.volume)
        assert current.volume_source == '^GSPC'
    assert result[-1].date == '2026-09-25'  # exclude the recording's intraday bar
    assert result[-1].distribution_count == sum(d.distribution is True for d in result[-25:])


@pytest.mark.parametrize('decline,volume,expected', [(-.2, 2000, True), (-.199, 2000, False),
                                                   (-.2,1025,False),(-.2,1024,False)])
def test_distribution_threshold_and_strictly_higher_volume(decline, volume, expected):
    bars=synthetic_market()
    append(bars,decline,volume)
    assert evaluate(bars)[-1].distribution is expected


def test_follow_through_day_four_from_low_then_undercut_resets():
    bars=synthetic_market()
    append(bars,-10, low=89)
    append(bars,1.25)
    append(bars,1.25)
    assert not evaluate(bars)[-1].follow_through
    append(bars,1.25)
    day=evaluate(bars)[-1]
    assert day.rally_day == 4 and day.follow_through and day.state == CONFIRMED
    append(bars,2,low=88)
    day=evaluate(bars)[-1]
    assert day.rally_day == 1 and day.state == CORRECTION and not day.follow_through
    assert day.last_follow_through == bars[-2].date


@pytest.mark.parametrize('gain,vol', [(1.249,2000),(1.25,1025),(1.25,1024)])
def test_follow_through_requires_gain_and_increased_volume(gain,vol):
    bars=synthetic_market()
    append(bars,gain,vol)
    assert evaluate(bars)[-1].state == CORRECTION


def test_four_and_five_distribution_days_and_no_automatic_correction_recovery():
    bars=synthetic_market()
    append(bars,1.25)
    for _ in range(4):
        append(bars,-.2)
    assert evaluate(bars)[-1].state == PRESSURE
    append(bars,-.2)
    assert evaluate(bars)[-1].state == CORRECTION
    for _ in range(25):
        append(bars)
    assert evaluate(bars)[-1].distribution_count == 0
    assert evaluate(bars)[-1].state == CORRECTION
    append(bars,1.25)
    assert evaluate(bars)[-1].state == CONFIRMED


def test_distribution_expiry_relaxes_pressure_at_25_sessions():
    bars=synthetic_market()
    append(bars,1.25)
    for _ in range(4):
        append(bars,-.2)
    for _ in range(21):
        append(bars)
    assert evaluate(bars)[-1].state == PRESSURE
    append(bars)
    assert evaluate(bars)[-1].distribution_count == 3
    assert evaluate(bars)[-1].state == CONFIRMED


def test_proxy_fallback_compares_both_proxy_days_and_never_uses_proxy_price():
    bars=synthetic_market()
    append(bars,-.2)
    proxy=[replace(b,volume=10,close=1) for b in bars]
    proxy[-1]=replace(proxy[-1],volume=11,close=900)
    bars[-1]=replace(bars[-1],volume=None)
    day=evaluate(bars,proxy)[-1]
    assert day.volume_source == 'SPY' and day.distribution and day.change_pct == pytest.approx(-.2)
    assert day.volume == 11


def test_constant_index_volume_falls_back_and_later_bad_data_cannot_rewrite_past():
    bars=synthetic_market()
    proxy=[replace(b,volume=50+i) for i,b in enumerate(bars)]
    bars=[replace(b,volume=5000) for b in bars]
    result=evaluate(bars,proxy)
    assert result[-1].volume_source == 'SPY'
    assert result[:10] == evaluate(bars[:10],proxy[:10])
    proxy[-1]=replace(proxy[-1],volume=None)
    assert evaluate(bars,proxy)[-1].state is None


def test_missing_session_invalidates_window_and_cannot_confirm():
    bars=synthetic_market(40)
    append(bars,1.25)
    proxy=[replace(b,volume=50+i) for i,b in enumerate(bars)]
    del bars[-3]
    result=evaluate(bars,proxy)
    assert result[-1].state is None and not result[-1].follow_through


def test_weaker_index_and_missing_index_are_explicit():
    good=synthetic_market()
    append(good,1.25)
    bad=[replace(b,close=100,low=99) for b in good]
    m=market_direction({'^GSPC':good,'^IXIC':bad,'SPY':good,'QQQ':good},T)
    assert m['state'] == CORRECTION
    assert m['indexes']['^GSPC']['latest']['state'] == CONFIRMED
    missing=market_direction({'^GSPC':good,'SPY':good,'QQQ':good},T)
    assert missing['state'] is None and missing['warnings']


def test_empty_market_has_no_invented_state():
    m=market_direction({},T)
    assert m['state'] is None and m['as_of'] is None


def test_distribution_day_expires_after_a_five_percent_rally_above_its_close():
    t = replace(T, distribution_expiry_gain_pct=5)
    bars = synthetic_market()
    append(bars, -.5, 5000)                     # distribution day, close 99.5
    for _ in range(4):
        append(bars, 1.2)                        # 99.5 * 1.012**4 = 104.37: not yet 5% above
    assert evaluate(bars, t=t)[-1].distribution_count == 1
    append(bars, .6)                             # 105.00 >= 99.5 * 1.05 = 104.475
    last = evaluate(bars, t=t)[-1]
    assert last.distribution_count == 0 and "1 more expired" in last.reason
    append(bars, -3)                             # falling back below does not revive it
    assert evaluate(bars, t=t)[-2].distribution_count == 0
    assert evaluate(bars)[-2].distribution_count == 1   # the specification's default keeps it


def test_index_rule_chooses_which_index_state_is_m():
    confirmed = synthetic_market(30)
    append(confirmed, -1, low=90)                # low for the rally count
    for gain in (0, .1, .1, 2):
        append(confirmed, gain)                  # day 5 follow-through on higher volume
    flat = synthetic_market(len(confirmed))
    histories = {"^GSPC": confirmed, "SPY": confirmed, "^IXIC": flat, "QQQ": flat}
    states = {rule: market_direction(histories, replace(T, market_index_rule=rule))["state"]
              for rule in ("both", "either", "sp500", "nasdaq")}
    assert states == {"both": CORRECTION, "either": CONFIRMED, "sp500": CONFIRMED, "nasdaq": CORRECTION}


GRADED = replace(T, correction_drawdown_pct=10, distribution_expiry_gain_pct=5, market_index_rule="average",
                 exposure_late_confirmed_pct=80, exposure_new_uptrend_pct=60, exposure_new_uptrend_sessions=10,
                 exposure_pressure_pct=40, exposure_heavy_pressure_pct=20)


def confirmed_market():
    """A follow-through on rally day 5, then quiet sessions. The low day is not a distribution day."""
    bars = synthetic_market(30)
    append(bars, -1, volume=bars[-1].volume - 5, low=90)
    for gain in (0, .1, .1, 2):
        append(bars, gain)
    return bars


def test_graded_rule_needs_price_damage_for_a_correction():
    bars = confirmed_market()
    for _ in range(12):
        append(bars, .05)                        # past the follow-through cap
    assert evaluate(bars, t=GRADED)[-1].exposure == 100
    for _ in range(6):
        append(bars, -.3, volume=bars[-1].volume + 100)   # six distribution days, only 1.8% lower
    last = evaluate(bars, t=GRADED)[-1]
    assert (last.state, last.distribution_count, last.exposure) == (PRESSURE, 6, 20)
    assert evaluate(bars)[-1].state == CORRECTION          # the specification's count ends it instead
    for _ in range(8):
        append(bars, -1.2, volume=bars[-1].volume - 1)    # no new distribution days, just falling
    days = evaluate(bars, t=GRADED)
    first = next(d for d in days if d.state == CORRECTION and d.date > days[35].date)
    assert "below the uptrend's peak" in first.reason and first.exposure == 0


def test_exposure_ladder_and_follow_through_cap():
    bars = confirmed_market()
    days = evaluate(bars, t=GRADED)
    assert days[-1].follow_through and days[-1].exposure == 60      # first sessions after a follow-through
    for _ in range(10):
        append(bars, .05)
    assert evaluate(bars, t=GRADED)[-1].exposure == 100
    for _ in range(3):
        append(bars, -.3, volume=bars[-1].volume + 100)
    assert evaluate(bars, t=GRADED)[-1].exposure == 80                # one day short of pressure
    append(bars, -.3, volume=bars[-1].volume + 100)
    assert evaluate(bars, t=GRADED)[-1].exposure == 40                # under pressure
    assert evaluate(bars)[-1].exposure == 0                           # specification: pressure buys nothing


def test_average_rule_rounds_down_to_twenty_percent_steps():
    up = confirmed_market()
    for _ in range(12):
        append(up, .05)
    flat = synthetic_market(len(up))
    market = market_direction({"^GSPC": up, "SPY": up, "^IXIC": flat, "QQQ": flat}, GRADED)
    assert market["indexes"]["^GSPC"]["latest"]["exposure"] == 100
    assert market["indexes"]["^IXIC"]["latest"]["exposure"] == 0
    assert (market["exposure"], market["state"]) == (40, PRESSURE)   # (100 + 0) / 2 = 50, rounded down


def test_ignore_rule_always_allows_full_exposure():
    flat = synthetic_market(30)
    market = market_direction({"^GSPC": flat, "SPY": flat, "^IXIC": flat, "QQQ": flat}, replace(T, market_index_rule="ignore"))
    assert market["exposure"] == 100 and market["state"] == CORRECTION   # the state is still reported
