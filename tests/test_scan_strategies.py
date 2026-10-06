"""The scan strategies: parameters, live orders, exit alerts, saved screens and plans.

Every price here is explicitly synthetic. The rule arithmetic itself is checked against the research
code in test_setups_parity.py; these tests check how a scan turns it into candidates and alerts.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from stratlib import exits, scanning, setups
from stratlib.config import BacktestSettings, ConfigError, Thresholds, load_settings
from stratlib.exits import assess_scan_position, walk
from stratlib.live_panel import LivePanel
from stratlib.portfolio import portfolio_plan
from stratlib.positions import assess_position
from stratlib.prices import Bar
from stratlib.sell_rules import Position
from stratlib.strategies import (STRATEGIES, current_rules, latest_screen, market_policy, read_params, read_rules,
                                revision, rule_snapshot)
from stratlib.strategy_params import EmaPullbackParams, QullamaggieParams, build_params
from stratlib.universe import parse_listings
from test_setups_parity import make_panel

SCAN_IDS = ("qullamaggie", "minervini", "episodic_pivot", "ema_pullback", "tt_checklist", "nash_quality", "msci_garp")
P, T = BacktestSettings(), Thresholds()


# ---------------------------------------------------------------------------------------- parameters

def config_with(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return load_settings(path)


def test_defaults_come_from_the_research_configurations(tmp_path):
    settings = config_with(tmp_path, "fmp: {}\n")
    assert set(settings.strategies) == set(SCAN_IDS)
    q = settings.strategies["qullamaggie"]
    assert (q.prior_move_pct, q.consolidation_min, q.consolidation_max, q.trail_sma) == (30.0, 10, 40, 10)
    assert settings.strategies["episodic_pivot"].market_filter == "B"      # the corrected report's runs
    assert settings.strategies["episodic_pivot"].gap_pct == 20.0
    assert settings.strategies["ema_pullback"].stop_pct == 3.0


def test_config_overrides_and_rejects_bad_values(tmp_path):
    settings = config_with(tmp_path, "strategies:\n  qullamaggie:\n    min_adr_pct: 6\n    partial_fraction: 0.5\n")
    assert settings.strategies["qullamaggie"].min_adr_pct == 6 and settings.strategies["minervini"].swing_pct == 3.0
    for body, message in [("strategies:\n  nosuch: {}\n", "Unknown strategy"),
                          ("strategies:\n  qullamaggie:\n    nope: 1\n", "Unknown setting"),
                          ("strategies:\n  qullamaggie:\n    lookback: 2.5\n", "integer"),
                          ("strategies:\n  qullamaggie:\n    min_adr_pct: -1\n", "negative"),
                          ("strategies:\n  minervini:\n    exit: weekly\n", "one of"),
                          ("strategies:\n  ema_pullback:\n    ema_fast: 30\n", "EMA spans"),
                          ("strategies:\n  episodic_pivot:\n    market_filter: Z\n", "one of"),
                          ("strategies: [1]\n", "mapping")]:
        with pytest.raises(ConfigError, match=message):
            config_with(tmp_path, body)


# ---------------------------------------------------------------------------------------- snapshots

def test_scan_snapshots_ignore_unrelated_settings_and_freeze_parameters():
    base = rule_snapshot("qullamaggie", T, P)
    assert rule_snapshot("qullamaggie", replace(T, stop_loss_pct=9), replace(P, trend_min_rs=95)) == base
    assert rule_snapshot("qullamaggie", T, replace(P, max_holdings=5)) != base
    assert rule_snapshot("qullamaggie", T, P, QullamaggieParams(min_adr_pct=6)) != base
    spec, params, holdings = read_params(base)
    assert (spec.id, params, holdings) == ("qullamaggie", QullamaggieParams(), 10)
    assert revision(base) != revision(rule_snapshot("minervini", T, P))
    with pytest.raises(ValueError, match="parameters, not"):
        read_rules(base)
    # The two original strategies keep their snapshots (and so their revision hashes) byte for byte.
    assert set(rule_snapshot("canslim", T, P)) == {"version", "strategy_id", "variant_id", "thresholds", "portfolio"}


def test_market_policy_names_each_strategys_filter():
    assert "without a market rule" in market_policy("ema_pullback", P)
    assert "six market filters" in market_policy("qullamaggie", P)
    assert "SPY above its 50-day average" in market_policy("episodic_pivot", P)
    assert "exposure" in market_policy("trend", P)


# ---------------------------------------------------------------------------------------- live orders

def panel_with(rows, sessions=12):
    """Flat prices for one stock; rows maps session -> (open, high, low, close)."""
    n = sessions
    arrays = {k: np.full((n, 1), 10.0) for k in ("open", "high", "low", "close")}
    for i, (o, h, lo, c) in rows.items():
        arrays["open"][i, 0], arrays["high"][i, 0], arrays["low"][i, 0], arrays["close"][i, 0] = o, h, lo, c
    return LivePanel(np.array([f"d{i:02d}" for i in range(n)]), np.array(["AAA"]), volume=np.full((n, 1), 1e6), **arrays)


def test_orders_live_for_order_life_sessions_and_a_newer_setup_replaces_the_older():
    panel = panel_with({})
    setup, pivot = np.zeros((12, 1), bool), np.full((12, 1), np.nan)
    setup[[5, 9], 0], pivot[5, 0], pivot[9, 0] = True, 11.0, 12.0
    on = np.ones(12, bool)
    orders, blocked = scanning.live_orders(panel, setup, pivot, panel.eligible | True, on, order_life=5)
    assert [(o["row"], o["pivot"]) for o in orders] == [(9, 12.0)] and not blocked
    # Session 5's order was live for sessions 6-10 only; by the close of 11 (last) it has expired.
    only_old = setup.copy()
    only_old[9, 0] = False
    assert scanning.live_orders(panel, only_old, pivot, panel.eligible | True, on, 5)[0] == []


def test_a_pivot_that_traded_through_is_triggered_not_a_candidate():
    flat = {i: (10, 10.5, 9.5, 10) for i in range(12)}
    flat[10] = (10.2, 11.4, 10.0, 11.0)                   # high above the 11.0 pivot on session 10
    panel = panel_with(flat)
    setup, pivot = np.zeros((12, 1), bool), np.full((12, 1), np.nan)
    setup[8, 0], pivot[8, 0] = True, 11.0
    (order,), _ = scanning.live_orders(panel, setup, pivot, np.ones((12, 1), bool), np.ones(12, bool), 5)
    assert order["triggered_row"] == 10
    gap = {**flat, 10: (11.6, 11.9, 11.5, 11.8)}          # opens above: triggered by the gap
    (order,), _ = scanning.live_orders(panel_with(gap), setup, pivot, np.ones((12, 1), bool), np.ones(12, bool), 5)
    assert order["triggered_row"] == 10


def test_a_market_filter_that_is_off_holds_setups_back_instead_of_replacing_orders():
    panel = panel_with({})
    setup, pivot = np.zeros((12, 1), bool), np.full((12, 1), np.nan)
    setup[[8, 10], 0], pivot[8, 0], pivot[10, 0] = True, 11.0, 12.0
    mask = np.ones(12, bool)
    mask[10] = False
    orders, blocked = scanning.live_orders(panel, setup, pivot, np.ones((12, 1), bool), mask, 5)
    assert [(o["row"], o["pivot"]) for o in orders] == [(8, 11.0)]      # the filter-off setup made no order
    assert blocked == []                                                 # ...and session 8's order still stands
    mask[8] = False
    orders, blocked = scanning.live_orders(panel, setup, pivot, np.ones((12, 1), bool), mask, 5)
    assert orders == [] and sorted(b["row"] for b in blocked) == [10]


def test_risk_weight_uses_the_research_sizing_rule():
    p = QullamaggieParams()
    risk, weight = scanning.risk_weight(p, 100.0, 96.0)
    assert (risk, weight) == (pytest.approx(4.0), pytest.approx(12.5))      # 0.5% risk / 4% stop
    assert scanning.risk_weight(p, 100.0, 99.9)[1] == 20.0                    # capped at 20% of equity
    assert scanning.risk_weight(p, 100.0, 100.0) == (None, None)


# ---------------------------------------------------------------------------------------- exits

def bars_from(values, *, start="2026-01-05", volume=1_000_000, low_gap=0.5):
    """Bars for consecutive weekdays; each value is a close, the low sits low_gap under it."""
    out, day = [], date.fromisoformat(start)
    for value in values:
        while day.weekday() > 4:
            day += timedelta(days=1)
        out.append(Bar(day.isoformat(), value, value + low_gap, value - low_gap, value, volume))
        day += timedelta(days=1)
    return out


def alert(strategy_id, closes, *, entry_index=12, entry_price=None, params=None, stop=None, **kw):
    bars = bars_from(closes, **kw)
    position = Position("AAA", bars[entry_index].date, entry_price or closes[entry_index], stop_price=stop)
    params = params or build_params(strategy_id, None)
    return assess_scan_position(strategy_id, params, position, bars, as_of=bars[-1].date)


RISING = [50 + i * 0.2 for i in range(13)]    # quiet history, entry on the last of these (index 12)


def test_qullamaggie_sells_a_third_on_day_three_then_trails_the_ten_day_average():
    a = alert("qullamaggie", RISING + [53, 54])                        # age 2: nothing due yet
    assert a.action == "Hold" and a.stop_price == pytest.approx(RISING[11] - 0.5)   # the prior session's low
    a = alert("qullamaggie", RISING + [53, 54, 55])                     # age 3: the partial sale falls due
    assert a.action == "Take profits" and "sell 33%" in a.reason
    a = alert("qullamaggie", RISING + [53, 54, 55, 56])                 # later: no fresh alert, the sale is history
    assert a.action == "Hold" and "partial sale fell due" in a.reason
    a = alert("qullamaggie", RISING + [53, 54, 55, 56, 57, 53.5])       # under the 10-day average, above the raised stop
    assert a.action == "Sell" and "10-day average" in a.reason


def test_a_close_at_the_stop_sells_and_an_earlier_signal_stays_open():
    a = alert("qullamaggie", RISING + [51, 53, 54])                     # the stop is the prior low, 51.7: day 1 closes at 51
    assert a.action == "Sell" and "at or below the stop" in a.reason and "still open" in a.reason


def test_a_recorded_stop_overrides_the_derived_one_and_must_sit_below_entry():
    assert alert("qullamaggie", RISING + [53, 54], stop=49.0).stop_price == 49.0
    assert alert("qullamaggie", RISING + [53], stop=60.0).action == "Unavailable"
    assert alert("qullamaggie", RISING + [53], entry_index=0).action == "Unavailable"   # no session before entry


def test_episodic_pivot_skips_the_day_three_sale_below_entry_and_honours_time_exits():
    closes = RISING + [50.0, 50.0, 50.0]                                # entry 52.4; day-3 close under it
    a = alert("episodic_pivot", closes, entry_price=52.4, stop=40.0)
    assert a.action == "Hold" and "rules skip it" in a.reason
    params = build_params("episodic_pivot", {"exit": "time20"})
    a = alert("episodic_pivot", RISING + [53] * 20, params=params, stop=40.0)
    assert a.action == "Sell" and "session 20" in a.reason
    params = build_params("episodic_pivot", {"exit": "trail10"})
    assert alert("episodic_pivot", RISING + [53] * 21, params=params, stop=40.0).action == "Hold"
    assert alert("episodic_pivot", RISING + [53] * 20 + [50], params=params, stop=40.0).action == "Sell"


def test_minervini_sells_a_breakout_that_lacked_volume_but_keeps_one_that_had_it():
    def with_entry_volume(entry_volume):
        bars = bars_from([50.0] * 60 + [50.5, 51.0])                      # 60 quiet sessions, entry on the 61st
        volumes = [1e6] * 60 + [entry_volume, 1e6]
        bars = [Bar(b.date, b.open, b.high, b.low, b.close, v) for b, v in zip(bars, volumes)]
        position = Position("AAA", bars[60].date, 50.5)
        return assess_scan_position("minervini", build_params("minervini", None), position, bars, as_of=bars[-1].date)
    assert with_entry_volume(1.2e6).action == "Sell"                       # 1.2x the 50-day average, under 1.4x
    assert with_entry_volume(1.6e6).action == "Hold"


def test_minervini_half_exit_takes_half_at_twenty_percent_then_trails_the_fifty_day_average():
    params = build_params("minervini", {"exit": "half20"})
    bars = bars_from([40.0] * 60 + [50.0] * 3)
    bars[60] = Bar(bars[60].date, 40, 50.5, 39.5, 50.0, 2e6)              # a confirmed breakout day
    bars[-1] = Bar(bars[-1].date, 50, 61.0, 49.5, 50.0, 1e6)              # the high reaches +20% (60) on the last session
    position = Position("AAA", bars[60].date, 50.0, stop_price=40.0)
    a = assess_scan_position("minervini", params, position, bars, as_of=bars[-1].date)
    assert a.action == "Take profits" and a.target_price == pytest.approx(60.0)
    # 40.5 is under the 50-day average (40.6: forty-six closes at 40, four higher) and over the 40 stop.
    bars += bars_from([40.5], start=(date.fromisoformat(bars[-1].date) + timedelta(days=1)).isoformat())
    a = assess_scan_position("minervini", params, position, bars, as_of=bars[-1].date)
    assert a.action == "Sell" and "50-day average" in a.reason


def test_ema_pullback_exits_on_the_first_or_second_close_below_the_slow_ema():
    up = [50 + i * 0.5 for i in range(60)]
    closes = up + [up[-1] - 6]                                            # drops well under the 21-day EMA
    first = alert("ema_pullback", closes, entry_index=50, entry_price=up[50])
    assert first.action == "Sell" and "First close below the 21-day EMA" in first.reason
    second = build_params("ema_pullback", {"exit_rule": "second"})
    assert alert("ema_pullback", closes, entry_index=50, entry_price=up[50], params=second).action == "Hold"
    assert alert("ema_pullback", closes + [closes[-1] - 1], entry_index=50, entry_price=up[50],
                 params=second).action == "Sell"


def test_walk_reports_unavailable_rather_than_guessing_when_the_trailing_average_lacks_closes():
    bars = bars_from([50.0] * 6)
    arrays = {"close": np.array([b.close for b in bars]), "high": np.array([b.high for b in bars]),
              "low": np.array([b.low for b in bars]), "volume": np.array([b.volume for b in bars])}
    rule = {"kind": "partial_day", "day": 1, "fraction": 0.5, "n": 10}
    result = walk([b.date for b in bars], arrays, 0, 45.0, 40.0, rule)
    assert result.action == "Unavailable" and "10 closes" in result.reason


# ---------------------------------------------------------------------------------------- store

NOW = datetime(2026, 9, 29, 23, tzinfo=timezone.utc)


def seed_scan_market(store, *, sessions=320, spy_drift=0.0004):
    """SPY, QQQ and three stocks. UP1 ends on a fresh 9/21 EMA pullback; FLAT has no setup."""
    days, day = [], date(2025, 6, 2)
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    store.replace_universe(parse_listings([
        {"symbol": s, "companyName": f"{s} Corp", "exchangeShortName": "NYSE", "sector": "Tech", "industry": "Software"}
        for s in ("UP1", "FLAT", "SMALL")]))

    def save(symbol, closes, volume=1_000_000, low_factor=0.997, last_low=None):
        bars = [Bar(d, c * 0.999, c * 1.003, c * low_factor, c, volume) for d, c in zip(days, closes)]
        if last_low:
            b = bars[-1]
            bars[-1] = Bar(b.date, b.open, b.high, last_low, b.close, b.volume)
        store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date, checked_at=NOW, status="ok")
    t = np.arange(sessions)
    for symbol in ("SPY", "QQQ"):
        save(symbol, 400 * np.exp(spy_drift * t))
    up = 20 * 1.004 ** t
    last = up[-1]
    up[-1] = last * 0.985                                                # a one-day dip, still above the 21-day EMA
    fast = float(setups.ema(up[:, None], 9)[-1, 0])
    save("UP1", up, last_low=fast * 0.995)
    save("FLAT", np.full(sessions, 30.0) + np.sin(t / 5))
    save("SMALL", up, volume=100)                                        # the same chart, but illiquid
    return days


def test_ema_scan_saves_a_dated_screen_scoped_to_its_strategy(store, settings):
    days = seed_scan_market(store)
    settings = replace(settings, strategies={**settings.strategies, "ema_pullback": EmaPullbackParams(min_dollar_volume_m=5.0)})
    from stratlib.screening import run_screen
    report = run_screen(store, settings, strategy_id="ema_pullback", now=NOW)
    assert report["price_date"] == days[-1] and report["strategy_id"] == "ema_pullback"
    assert [c["symbol"] for c in report["candidates"]] == ["UP1"]         # FLAT has no setup, SMALL is illiquid
    candidate = report["candidates"][0]
    assert candidate["stop"] == pytest.approx(candidate["ema_slow"] * 0.97, rel=1e-3)
    assert candidate["weight_pct"] == pytest.approx(min(20, 0.5 / candidate["risk_pct"] * 100), rel=1e-3)
    assert report["rule_snapshot"] == current_rules(settings, "ema_pullback")
    assert report["market_filter"]["code"] == "A" and report["api_calls"] == 0
    assert latest_screen(store, "ema_pullback") == report
    assert latest_screen(store, "canslim") is None and latest_screen(store, "qullamaggie") is None
    assert len(store.document_keys("screen:ema_pullback:ema-pullback:")) == 1


def test_a_scan_needs_enough_spy_history_and_prices(store, settings):
    from stratlib.screening import ScreenError, run_screen
    seed_scan_market(store, sessions=120)
    with pytest.raises(ScreenError, match="needs .* completed sessions"):
        run_screen(store, settings, strategy_id="ema_pullback", now=NOW)


def test_the_market_filter_holds_candidates_back_when_spy_is_below_its_average(store, settings):
    from stratlib.screening import run_screen
    seed_scan_market(store, spy_drift=-0.0006)                              # SPY falling: under its 50-day average
    params = EmaPullbackParams(min_dollar_volume_m=5.0, market_filter="B")
    settings = replace(settings, strategies={**settings.strategies, "ema_pullback": params})
    report = run_screen(store, settings, strategy_id="ema_pullback", now=NOW)
    assert report["market_filter"]["on"] is False
    assert report["candidates"] == [] and [b["symbol"] for b in report["blocked"]] == ["UP1"]
    assert scanning.quick_filter(store, "B", report["price_date"])["on"] is False
    assert scanning.quick_filter(store, "D", report["price_date"]) is None   # breadth needs a scan


def test_episodic_pivot_without_earnings_dates_lists_gaps_as_unverified(store, settings):
    from stratlib.screening import run_screen
    days = seed_scan_market(store)
    gap_bars = store.price_history("UP1", through=days[-2])
    prev = gap_bars[-1].close
    store.write_prices("UP1", [*gap_bars, Bar(days[-1], prev * 1.3, prev * 1.36, prev * 1.29, prev * 1.35, 6_000_000)],
                       replace=True, requested_from=gap_bars[0].date, checked_at=NOW, status="ok")
    settings = replace(settings, strategies={**settings.strategies, "episodic_pivot": build_params(
        "episodic_pivot", {"market_filter": "A", "min_dollar_volume_m": 5.0})})
    offline = run_screen(store, settings, strategy_id="episodic_pivot", now=NOW)
    assert offline["candidates"] == [] and offline["skipped"][0]["symbol"] == "UP1"
    assert "Unverified" in offline["skipped"][0]["reason"] and any("offline" in w for w in offline["warnings"])


def test_episodic_pivot_with_an_earnings_calendar_makes_the_gap_a_candidate(store, settings):
    from unittest.mock import Mock
    from stratlib.screening import run_screen
    days = seed_scan_market(store)
    gap_bars = store.price_history("UP1", through=days[-2])
    prev = gap_bars[-1].close
    store.write_prices("UP1", [*gap_bars, Bar(days[-1], prev * 1.3, prev * 1.36, prev * 1.29, prev * 1.35, 6_000_000)],
                       replace=True, requested_from=gap_bars[0].date, checked_at=NOW, status="ok")
    settings = replace(settings, strategies={**settings.strategies, "episodic_pivot": build_params(
        "episodic_pivot", {"market_filter": "A", "min_dollar_volume_m": 5.0})})
    client = Mock()
    client.stats.api_calls = 0
    client.earnings_calendar.return_value = [{"symbol": "UP1", "date": days[-2]}, {"symbol": "FLAT", "date": days[-10]}]
    report = run_screen(store, settings, client, strategy_id="episodic_pivot", now=NOW)
    assert [c["symbol"] for c in report["candidates"]] == ["UP1"]
    assert report["candidates"][0]["earnings_date"] == days[-2] and report["candidates"][0]["gap_pct"] == pytest.approx(30, abs=1)
    client.earnings_calendar.assert_called_once()
    # A gap with no release (news, not earnings) is not an episodic pivot.
    client.earnings_calendar.return_value = []
    report = run_screen(store, settings, client, strategy_id="episodic_pivot", now=NOW)
    assert report["candidates"] == [] and "No earnings release" in report["skipped"][0]["reason"]


def tracking(settings, strategy_id, quantity=None):
    snapshot = current_rules(settings, strategy_id)
    return {"strategy_id": strategy_id, "variant_id": snapshot["variant_id"], "rule_snapshot": snapshot, "quantity": quantity}


def test_positions_keep_their_recorded_stop_and_restate_it_with_splits(store, settings):
    days = seed_scan_market(store)
    bars = store.price_history("UP1", since=days[-20])
    entry = bars[5]
    store.save_position(Position("UP1", entry.date, entry.close, stop_price=entry.close * 0.9), as_of=days[-1],
                        tracking=tracking(settings, "ema_pullback"))
    record = store.positions()[0]
    assert record["stop_price"] == pytest.approx(entry.close * 0.9) and record["strategy_id"] == "ema_pullback"
    before = assess_position(store, record, settings.thresholds, as_of=days[-1])
    assert before.stop_price == pytest.approx(entry.close * 0.9)
    # A 2-for-1 split restates every cached price; the recorded stop follows them.
    halved = [Bar(b.date, b.open / 2, b.high / 2, b.low / 2, b.close / 2, b.volume * 2)
              for b in store.price_history("UP1")]
    store.write_prices("UP1", halved, replace=True, requested_from=halved[0].date, checked_at=NOW, status="ok")
    after = assess_position(store, record, settings.thresholds, as_of=days[-1])
    assert after.stop_price == pytest.approx(before.stop_price / 2) and after.gain_pct == pytest.approx(before.gain_pct)


def test_each_strategy_with_a_position_reads_only_its_own_saved_rules(store, settings):
    days = seed_scan_market(store)
    entry = store.price_history("UP1", since=days[-30])[0]
    for strategy_id in SCAN_IDS:
        store.save_position(Position("UP1", entry.date, entry.close, stop_price=entry.close * 0.5), as_of=days[-1],
                            tracking=tracking(settings, strategy_id))
    by_strategy = {r["strategy_id"]: assess_position(store, r, settings.thresholds, as_of=days[-1]) for r in store.positions()}
    assert set(by_strategy) == set(SCAN_IDS)
    assert all(a.action in {"Hold", "Sell", "Take profits"} for a in by_strategy.values())
    # A position saved without rules cannot be assessed under a scan strategy.
    with pytest.raises(ValueError, match="requires saved rules"):
        store.save_position(Position("UP1", entry.date, entry.close), as_of=days[-1],
                            tracking={"strategy_id": "qullamaggie", "variant_id": STRATEGIES["qullamaggie"].variant,
                                      "rule_snapshot": None})


# ---------------------------------------------------------------------------------------- plan

def scan_report(settings, strategy_id="qullamaggie", **change):
    candidates = [{"symbol": s, "name": s, "close": 10.0, "weight_pct": w, "stop": 9.0, "pivot": 10.5, "risk_pct": 5.0,
                   "rank": i, "order": [i, s], "ret63_pct": 30.0 - i}
                  for i, (s, w) in enumerate([("AAA", 10.0), ("BBB", 8.0), ("CCC", 6.0)], 1)]
    return {"price_date": "2026-09-29", "sample": None, "candidates": candidates, "strategy_id": strategy_id,
            "rule_snapshot": current_rules(settings, strategy_id), **change}


def test_plans_ignore_the_exposure_ladder_and_size_by_risk(store, settings):
    report = scan_report(settings)
    market = {"as_of": "2026-09-29", "exposure": 0}                         # the ladder says no buying
    plan = portfolio_plan(store, report, "qullamaggie", settings.thresholds, settings.backtest, market, "2026-09-29",
                          params=settings)
    assert not plan["issues"] and plan["capacity"] == settings.backtest.max_holdings
    rows = {r["Symbol"]: r for r in plan["candidates"]}
    assert [r["Plan"] for r in plan["candidates"]] == ["Proposed buy"] * 3
    assert rows["AAA"]["Initial weight, %"] == 10.0 and "Buy stop, USD" in rows["AAA"] and "Initial slot, %" not in rows["AAA"]
    # The same exposure closes the ladder strategies' plan to new buys.
    trend = {"price_date": "2026-09-29", "sample": None, "candidates": [],
             "rule_snapshot": rule_snapshot("trend", settings.thresholds, settings.backtest)}
    assert portfolio_plan(store, trend, "trend", settings.thresholds, settings.backtest, market, "2026-09-29")["capacity"] == 0


def test_plans_flag_screens_made_under_other_parameters(store, settings):
    report = scan_report(settings)
    changed = replace(settings, strategies={**settings.strategies, "qullamaggie": QullamaggieParams(min_adr_pct=7.0)})
    plan = portfolio_plan(store, report, "qullamaggie", settings.thresholds, settings.backtest,
                          {"as_of": "2026-09-29", "exposure": 100}, "2026-09-29", params=changed)
    assert any("older or legacy rules" in issue for issue in plan["issues"])
    assert plan["candidates"][0]["Plan"] == "Review data"


def test_held_symbols_fill_slots_under_a_scan_strategy(store, settings):
    days = seed_scan_market(store)
    entry = store.price_history("UP1", since=days[-30])[0]
    store.save_position(Position("UP1", entry.date, entry.close, stop_price=entry.close * 0.5), as_of=days[-1],
                        tracking=tracking(settings, "qullamaggie"))
    report = scan_report(settings)
    report["candidates"][0]["symbol"] = "UP1"
    plan = portfolio_plan(store, report, "qullamaggie", settings.thresholds, settings.backtest,
                          {"as_of": days[-1], "exposure": 100}, days[-1], params=settings)
    assert plan["held_count"] == 1 and plan["free"] == settings.backtest.max_holdings - 1
    assert plan["candidates"][0]["Plan"] == "Held"


# ---------------------------------------------------------------------------------------- evidence

def test_evidence_reads_saved_runs_and_reports_the_verdict(tmp_path):
    import json
    from stratlib import evidence
    run = tmp_path / "output" / "qullamaggie_sma10"
    run.mkdir(parents=True)
    metrics = lambda cagr: {"trades": 100, "cagr": cagr, "spy": {"cagr": 15.0}, "max_drawdown": 30.0, "expectancy_r": -0.1}  # noqa: E731
    (run / "results.json").write_text(json.dumps({"results": {"combined": metrics(-3.0), "out_of_sample": metrics(-5.0)}}))
    rows = evidence.evidence_rows("qullamaggie", tmp_path)
    assert [(r["Period"], r["Gap vs SPY, pp"]) for r in rows] == [("2016 to present", -18.0), ("2022 to present", -20.0)]
    assert "0 of 1 saved runs beat SPY" in evidence.verdict("qullamaggie", tmp_path)
    assert "18.0 points a year behind" in evidence.verdict("qullamaggie", tmp_path)
    assert evidence.evidence_rows("minervini", tmp_path) == [] and evidence.verdict("minervini", tmp_path) is None


# ---------------------------------------------------------------------------------------- command line

def test_the_screen_command_runs_a_scan_without_an_api_key(settings, monkeypatch, capsys):
    import json
    from stratlib import cli
    from stratlib.store import Store
    app_store = Store(settings.data.db_path)          # the command opens the configured database
    seed_scan_market(app_store)
    app_store.close()
    monkeypatch.delenv("FMP_API_KEY")
    monkeypatch.setattr("stratlib.config.DEFAULT_ENV_PATH", settings.path.parent / "no.env")   # never the real .env
    monkeypatch.setattr("stratlib.cli.configure_logging", lambda *a, **k: None)
    assert cli.main(["--config", str(settings.path), "screen", "--strategy", "ema_pullback"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["strategy_id"] == "ema_pullback" and summary["candidate_count"] == 1 and summary["api_calls"] == 0
    assert "candidates" not in summary
    # A strategy that calls FMP still requires the key.
    assert cli.main(["--config", str(settings.path), "screen", "--strategy", "nash_quality"]) == 2
    assert "API key is not set" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--config", str(settings.path), "screen", "--strategy", "nosuch"])
