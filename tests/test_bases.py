"""Recorded FMP bars and labeled synthetic shapes at each rule boundary."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from stratlib.bases import Week, cup_with_handle, detect_base, double_bottom, flat_base, weekly_bars
from stratlib.config import Thresholds
from stratlib.prices import Bar, parse_bars
from stratlib.technical import technical_criteria
from conftest import load_fixture

T=Thresholds()


def synthetic_weeks(values):
    """Each tuple is open, high, low, close, mean daily volume."""
    start=date(2025,1,6)
    return [Week((start+timedelta(weeks=i)).isoformat(),(start+timedelta(weeks=i,days=4)).isoformat(),
                 (start+timedelta(weeks=i,days=4)).isoformat(),o,h,l,c,v*5 if v is not None else None,5)
            for i,(o,h,l,c,v) in enumerate(values)]


def synthetic_cup():
    return synthetic_weeks([(99,100,95,98,1000),(97,98,90,92,1000),(92,94,84,86,1000),
                            (85,88,80,84,1000),(86,92,85,91,1000),(91,97,90,96,1000),
                            (96,99,95,98,1000),(97,98,92,94,500)])


def synthetic_double():
    return synthetic_weeks([(99,100,94,96,1000),(91,94,88,90,1000),(85,89,80,84,1000),
                            (85,96,85,95,1000),(89,92,83,85,1000),(82,86,79,82,1000),
                            (88,95,87,94,1000)])


def synthetic_flat():
    return synthetic_weeks([(99,100,97,98,1000),(95,98,91,94,1000),(92,95,85,90,1000),
                            (93,97,90,96,1000),(96,99,94,98,1000)])


def daily_from_weeks(weeks):
    return [Bar((date.fromisoformat(w.start)+timedelta(days=i)).isoformat(),w.open,w.high,w.low,w.close,
                w.volume/w.sessions if w.volume is not None else None) for w in weeks for i in range(5)]


def test_cup_handle_geometry_depth_and_ten_cent_pivot():
    weeks=synthetic_cup()
    base=cup_with_handle(weeks,T)
    assert base.pattern == 'Cup with handle'
    assert base.length_weeks == 8 and base.depth_pct == pytest.approx(20)
    assert base.pivot == pytest.approx(98.10)
    assert base.handle_volume_ratio == .5
    assert base.handle_start == weeks[-1].start
    assert cup_with_handle(weeks[1:],T) is None


@pytest.mark.parametrize('change', [dict(low=87),dict(volume=5000),dict(volume=None),dict(close=98),dict(high=101)])
def test_cup_rejects_low_handle_no_drying_volume_or_rising_handle(change):
    weeks=synthetic_cup()
    weeks[-1]=replace(weeks[-1],**change)
    assert cup_with_handle(weeks,T) is None


def test_deep_cup_requires_correction_during_its_dates():
    weeks=synthetic_cup()
    weeks[3]=replace(weeks[3],low=50)
    assert cup_with_handle(weeks,T) is None
    assert cup_with_handle(weeks,T,frozenset([weeks[3].start])).depth_pct == 50
    assert cup_with_handle(weeks,T,frozenset(['2020-01-01'])) is None
    weeks[3]=replace(weeks[3],low=49.9)
    assert cup_with_handle(weeks,T,frozenset([weeks[3].start])) is None


@pytest.mark.parametrize('low,expected',[(88,True),(88.01,False),(67,True),(66.99,False)])
def test_cup_depth_boundaries(low,expected):
    weeks=synthetic_cup()
    # Keep the same recovery/handle while moving all trough-side lows inward.
    weeks=[replace(w,low=max(w.low,low)) for w in weeks]
    weeks[3]=replace(weeks[3],low=low)
    weeks[-1]=replace(weeks[-1],low=max(weeks[-1].low,(100+low)/2))
    assert (cup_with_handle(weeks,T) is not None) is expected


def test_double_bottom_requires_undercut_middle_peak_and_recovery():
    weeks=synthetic_double()
    base=double_bottom(weeks,T)
    assert base.pattern == 'Double bottom' and base.pivot == 96 and base.length_weeks == 7
    assert double_bottom(weeks[1:],T) is None
    for changed in [replace(weeks[5],low=80),replace(weeks[5],low=81)]:
        assert double_bottom([*weeks[:5],changed,weeks[6]],T) is None
    assert double_bottom([*weeks[:-1],replace(weeks[-1],close=90)],T) is None


def test_flat_base_five_weeks_fifteen_percent_and_actual_correction():
    weeks=synthetic_flat()
    base=flat_base(weeks,T)
    assert base.pattern == 'Flat base' and base.pivot == 100
    assert base.depth_pct == pytest.approx(15)
    assert flat_base(weeks[1:],T) is None
    weeks[2]=replace(weeks[2],low=84.99)
    assert flat_base(weeks,T) is None
    rise=synthetic_weeks([(90+i,92+i,89+i,91+i,1000) for i in range(5)])
    assert flat_base(rise,T) is None


@pytest.mark.parametrize('factory,detector',[(synthetic_cup,cup_with_handle),
                                            (synthetic_double,double_bottom),(synthetic_flat,flat_base)])
def test_gaps_cannot_count_as_weeks(factory,detector):
    weeks=factory()
    weeks[2]=replace(weeks[2],week_end='2025-01-25')
    assert detector(weeks,T) is None


def test_weekly_aggregation_excludes_partial_week_and_does_not_fill_missing_sessions():
    bars=daily_from_weeks(synthetic_cup())
    assert len(weekly_bars(bars,bars[-2].date)) == 7
    complete=weekly_bars(bars,bars[-1].date)
    assert len(complete) == 8 and complete[0].volume == 5000
    missing=weekly_bars(bars[:2]+bars[3:],bars[-1].date,sessions=[b.date for b in bars])
    assert len(missing) == 7
    bars[-1]=replace(bars[-1],volume=None)
    assert weekly_bars(bars,bars[-1].date)[-1].volume is None


def test_holiday_week_uses_actual_last_session_and_waits_until_friday():
    bars=daily_from_weeks(synthetic_flat())[:-1]
    assert len(weekly_bars(bars,bars[-1].date)) == 4
    weeks=weekly_bars(bars,'2025-02-07',sessions=[b.date for b in bars])
    assert len(weeks)==5 and weeks[-1].end=='2025-02-06' and weeks[-1].volume==4000


def test_recorded_aapl_flat_base_and_negative_cup_and_double_bottom():
    bars=parse_bars(load_fixture('historical-price-eod_full_AAPL_2025-09-01.json'))
    weeks=[w for w in weekly_bars(bars,'2025-12-26') if w.start>='2025-11-24']
    base=flat_base(weeks,T)
    assert base.pivot == 288.62 and base.low == 266.95 and base.length_weeks == 5
    assert base.depth_pct == pytest.approx(7.508142193888167)
    assert cup_with_handle(weeks,T) is None and double_bottom(weeks,T) is None


@pytest.mark.parametrize('symbol,start,end,detector,pivot,length',[
    ('APA','2026-03-30','2026-09-04',cup_with_handle,45.23,23),
    ('ZIM','2026-02-17','2026-09-04',double_bottom,29.3,29),
])
def test_recorded_cache_cup_and_double_bottom(symbol,start,end,detector,pivot,length):
    bars=parse_bars(load_fixture(f'cached-price-bars_{symbol}_2026-09-25.json'))
    weeks=[w for w in weekly_bars(bars,end) if w.start>=start]
    base=detector(weeks,T)
    assert base is not None and base.pivot == pytest.approx(pivot) and base.length_weeks==length


def breakout_bars(close=102,volume=1400):
    # Flat 5-week base plus a Monday breakout. 25 sessions is an explicit test override.
    bars=daily_from_weeks(synthetic_flat())
    d=(date.fromisoformat(bars[-1].date)+timedelta(days=3)).isoformat()
    bars.append(Bar(d,100,max(103,close),99,close,volume))
    return bars,replace(T,breakout_volume_sessions=25)


@pytest.mark.parametrize('close,volume,position,volpass',[(100,1400,True,True),(105,1400,True,True),
                                                       (105.01,1400,False,True),(102,1399,True,False)])
def test_buy_zone_and_breakout_volume_use_prior_sessions(close,volume,position,volpass):
    bars,t=breakout_bars(close,volume)
    result=detect_base(bars,bars[-1].date,t)
    assert result['pattern']=='Flat base' and result['length_weeks']==5
    assert result['position_pass'] is position and result['volume_pass'] is volpass
    assert result['breakout_volume_pct']==pytest.approx((volume/1000-1)*100)


def test_below_pivot_is_not_buyable_and_later_volume_cannot_repair_failed_breakout():
    bars,t=breakout_bars(102,1000)
    bars.append(replace(bars[-1],date='2025-02-11',close=99,volume=10000))
    result=detect_base(bars,bars[-1].date,t)
    assert not result['position_pass'] and not result['volume_pass']
    assert result['breakout_volume_pct']==0 and result['breakout_date']=='2025-02-10'


def test_missing_breakout_volume_and_stale_price_remain_unavailable():
    bars,t=breakout_bars(volume=None)
    result=detect_base(bars,bars[-1].date,t)
    assert result['volume_pass'] is None
    assert detect_base(bars,'2025-02-11',t)['position_pass'] is None


def test_future_bars_do_not_change_past_and_base_low_breach_invalidates():
    bars,t=breakout_bars()
    original=detect_base(bars,bars[-1].date,t)
    bars.append(replace(bars[-1],date='2025-02-11',close=80,low=79))
    assert detect_base(bars,'2025-02-10',t)==original
    assert detect_base(bars,'2025-02-11',t)['pattern'] is None


def test_missing_session_after_formation_cannot_be_hidden_by_weekly_aggregation():
    bars,t=breakout_bars()
    bars.append(replace(bars[-1],date='2025-02-12'))
    sessions=[b.date for b in bars]+['2025-02-11']
    assert detect_base(bars,bars[-1].date,t,sessions=sorted(sessions))['pattern'] is None


def test_n_and_m_criteria_and_date_mismatch():
    bars,t=breakout_bars()
    base=detect_base(bars,bars[-1].date,t)
    market={'state':'confirmed uptrend','as_of':bars[-1].date}
    assert all(c.passed for c in technical_criteria(base,market,t,bars[-1].date))
    assert technical_criteria(base,market,t,'2025-02-11')[-1].passed is None
    market['state']='uptrend under pressure'
    assert technical_criteria(base,market,t,bars[-1].date)[-1].passed is False


def test_cup_without_handle_is_off_by_default_and_uses_the_left_rim_pivot():
    from stratlib.bases import cup_without_handle
    on = replace(T, cup_without_handle_min_weeks=7)
    cup = synthetic_cup()[:-1]                    # the same cup, right side back near the rim, no handle
    assert cup_without_handle(cup, T) is None
    base = cup_without_handle(cup, on)
    assert base.pattern == "Cup without handle" and base.handle_start is None
    assert base.length_weeks == 7 and base.depth_pct == pytest.approx(20) and base.pivot == pytest.approx(100.10)
    assert cup_without_handle(cup[1:], on) is None                                   # shorter than 7 weeks
    assert cup_without_handle(cup[:-1], on) is None                                  # right side not recovered
    deep = [replace(w, low=60) if i == 3 else w for i, w in enumerate(cup)]
    assert cup_without_handle(deep, on) is None                                      # 40% deep outside a correction


def test_detect_base_prefers_a_handle_and_finds_a_deep_cup_without_one():
    on = replace(T, cup_without_handle_min_weeks=7)
    handled = daily_from_weeks(synthetic_cup())
    as_of = handled[-1].date
    assert detect_base(handled, as_of, on)["pattern"] == "Cup with handle"
    # 20% deep: too deep for a flat base, so only the new pattern can see it.
    plain = daily_from_weeks(synthetic_cup()[:-1])
    assert detect_base(plain, plain[-1].date, T)["pattern"] is None
    assert detect_base(plain, plain[-1].date, on)["pattern"] == "Cup without handle"


def second_stage_weeks(peak=126):
    """An earlier flat base (pivot 100), a breakout and advance, then a new flat base topping at ``peak``."""
    scale = lambda value: 117 + (value - 117) * (peak - 117) / (126 - 117) if value > 117 else value
    later = [(117,125,116,124),(124,126,120,121),(121,123,115,117),(117,120,113,116),(116,121,114,120),(120,124,118,123)]
    return synthetic_weeks([(99,100,97,98,1000),(95,98,91,94,1000),(92,95,85,90,1000),(93,97,90,96,1000),
                            (96,99,94,98,1000),(99,103,98,102,1000),(102,110,101,109,1000),(109,118,108,117,1000)]
                           + [tuple(scale(v) for v in w) + (1000,) for w in later])


def test_second_stage_flat_base_needs_an_earlier_breakout_and_advance():
    on = replace(T, flat_prior_breakout_weeks=26, flat_prior_advance_pct=20)
    daily = daily_from_weeks(second_stage_weeks())
    base = detect_base(daily, daily[-1].date, on)
    assert base["pattern"] == "Flat base" and base["high"] == 126
    prior = base["prior_base"]
    assert (prior["pattern"], prior["pivot"], prior["advance_pct"]) == ("Flat base", 100, pytest.approx(26))
    assert prior["breakout_date"] < base["start"]
    assert detect_base(daily, daily[-1].date, T)["prior_base"] is None      # the specification: no check


def test_flat_base_without_a_prior_base_or_enough_advance_is_rejected():
    on = replace(T, flat_prior_breakout_weeks=26, flat_prior_advance_pct=20)
    shallow = daily_from_weeks(second_stage_weeks(peak=118))                  # only 18% above the earlier pivot
    assert detect_base(shallow, shallow[-1].date, T)["pattern"] == "Flat base"
    assert detect_base(shallow, shallow[-1].date, on)["pattern"] is None
    alone = daily_from_weeks(second_stage_weeks()[5:])                        # no earlier base at all
    assert detect_base(alone, alone[-1].date, T)["pattern"] == "Flat base"
    assert detect_base(alone, alone[-1].date, on)["pattern"] is None
