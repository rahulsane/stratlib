"""The live scan rules must equal the research strategies' rules on the same arrays.

The published backtests ran research/strategies/*.py. setups.py ports their signal code so the
Screen can apply it to the cached market; these tests run both on identical synthetic panels (seeded
random walks with injected trends, pullbacks, gaps and volume spikes, checked to produce signals)
and require exactly equal masks and pivots.
"""

import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from stratlib import setups
from stratlib.live_panel import LivePanel
from stratlib.strategy_params import EmaPullbackParams, EpisodicPivotParams, MinerviniParams, QullamaggieParams

RESEARCH = Path(__file__).resolve().parents[1] / "research"


@pytest.fixture(scope="module")
def research():
    sys.path.insert(0, str(RESEARCH))
    try:
        import engine  # noqa: F401
        import market_filters
        from strategies import ema_pullback, episodic_pivot, minervini, qullamaggie
    except Exception as exc:  # pragma: no cover - the research folder is part of this repository
        pytest.skip(f"research modules unavailable: {exc}")
    yield {"ema": ema_pullback, "ep": episodic_pivot, "min": minervini, "q": qullamaggie, "mf": market_filters}
    sys.path.remove(str(RESEARCH))


def make_panel(seed=3, sessions=420, stocks=70, *, gaps=False):
    """Trending stocks with pullbacks and bursts of volatility; some gap up on heavy volume."""
    rng = np.random.default_rng(seed)
    t = np.arange(sessions)[:, None]
    drift = rng.uniform(-0.0004, 0.0035, stocks)[None, :]
    period = rng.uniform(25, 70, stocks)[None, :]
    wave = rng.uniform(0.02, 0.18, stocks)[None, :] * np.sin(2 * np.pi * t / period + rng.uniform(0, 6, stocks)[None, :])
    noise = np.cumsum(rng.normal(0, 0.012, (sessions, stocks)), axis=0)
    close = 20 * np.exp(drift * t + wave + noise * rng.uniform(0.3, 1.2, stocks)[None, :])
    spread = close * rng.uniform(0.008, 0.045, (sessions, stocks))
    open_ = close * (1 + rng.normal(0, 0.006, (sessions, stocks)))
    high = np.maximum(open_, close) + spread * rng.uniform(0.1, 1, (sessions, stocks))
    low = np.minimum(open_, close) - spread * rng.uniform(0.1, 1, (sessions, stocks))
    volume = rng.uniform(0.8e6, 3e6, (sessions, stocks)) * (1 + 2 * (rng.random((sessions, stocks)) < 0.04))
    if gaps:
        for _ in range(60):
            i, j = rng.integers(80, sessions), rng.integers(stocks)
            lift = rng.uniform(1.15, 1.45)
            open_[i, j] = close[i - 1, j] * lift
            close[i:, j] *= lift
            high[i, j], low[i, j] = max(high[i, j], close[i, j] * 1.01), min(low[i, j], open_[i, j] * 0.99)
            volume[i, j] *= 6
    dates = np.array([f"d{i:04d}" for i in range(sessions)])
    symbols = np.array([f"S{j:03d}" for j in range(stocks)])
    for arr in (close, high, low, open_):
        arr[rng.random(arr.shape) < 0.002] = np.nan      # a few missing bars
    return LivePanel(dates, symbols, open_, high, low, close, volume)


def same(a, b):
    np.testing.assert_array_equal(np.nan_to_num(a, nan=-1.0), np.nan_to_num(b, nan=-1.0))


def test_panel_arrays_match_the_research_panel(research):
    import panel as research_panel
    p = make_panel(sessions=300, stocks=30)
    kind = np.array(["stock"] * len(p.symbols))
    theirs = research_panel.Panel(p.dates, p.symbols, kind, np.array([""] * len(p.symbols)), p.open, p.high, p.low,
                                  p.close, p.volume, np.ones_like(p.close))
    for name in ("valid", "close_ff", "adv20", "ret63", "eligible"):
        same(getattr(p, name), getattr(theirs, name))


@pytest.mark.parametrize("overrides", [
    {"prior_move_pct": 8.0, "tight_range_pct": 25.0, "min_adr_pct": 1.0, "max_depth_pct": 40.0},
    {"prior_move_pct": 12.0, "tight_range_pct": 18.0, "min_adr_pct": 2.0, "consolidation_min": 6, "consolidation_max": 30,
     "sma_fast": 5, "trail_sma": 20},
])
def test_qullamaggie_setups_equal_research(research, overrides):
    panel = make_panel()
    params = QullamaggieParams(**overrides)
    ours = setups.qullamaggie_setups(panel, params)
    strategy = research["q"].Qullamaggie()
    strategy.setup(panel, asdict(params))
    assert ours["setups"].sum() > 20
    same(ours["setups"], strategy.setups)
    same(ours["pivot"], strategy.pivot)
    same(ours["adr"], strategy.adr)


@pytest.mark.parametrize("overrides", [
    {"rs_min_pct": 0.0, "swing_pct": 3.0},
    {"rs_min_pct": 30.0, "swing_pct": 2.0, "min_pullbacks": 2, "final_max_pct": 14.0, "volume_dry": 1.4},
])
def test_minervini_template_and_vcp_equal_research(research, overrides):
    panel = make_panel(stocks=60)
    params = MinerviniParams(**overrides)
    template, rs = setups.minervini_template(panel, params)
    theirs_template, theirs_rs = research["min"].trend_template(panel, asdict(params))
    same(template, theirs_template)
    same(rs, theirs_rs)
    scan = setups.minervini_vcp(panel, template, params)
    reference = research["min"].vcp_scan(panel, template, asdict(params))
    assert template.sum() > 200 and scan["setup"].sum() > 20
    same(scan["setup"], reference["setup"])
    same(scan["pivot"], reference["pivot"])
    same(scan["peak_at"], reference["peak_at"])


@pytest.mark.parametrize("overrides", [
    {"gap_pct": 12.0, "volume_mult": 2.5},
    {"gap_pct": 12.0, "volume_mult": 2.0, "neglected": True, "neglect_max_pct": 60.0, "earnings": False},
])
def test_episodic_pivot_signals_equal_research(research, overrides):
    panel = make_panel(gaps=True)
    params = EpisodicPivotParams(**overrides)
    rng = np.random.default_rng(1)
    dates = {str(s): [str(d) for d in rng.choice(panel.dates, 25)] for s in panel.symbols}
    mask = setups.earnings_mask(panel, dates)
    same(mask, research["ep"].earnings_mask(panel, dates))
    signals = setups.episodic_signals(panel, params, mask)
    same(signals, research["ep"].signal_mask(panel, asdict(params), mask))
    same(setups.strong_close(panel, params.close_in_range), research["ep"].strong_close(panel, params.close_in_range))
    same(setups.prior_liquidity(panel), research["ep"].prior_liquidity(panel, {"split_window_start": panel.dates[0]}))
    assert signals.sum() > 3


@pytest.mark.parametrize("overrides", [{}, {"fresh_sessions": 2, "high_recent": 10, "high_lookback": 60}])
def test_ema_pullback_setups_equal_research(research, overrides):
    panel = make_panel()
    params = EmaPullbackParams(**overrides)
    ours = setups.ema_pullback_setups(panel, params)
    strategy = research["ema"].EmaPullback()
    strategy.setup(panel, asdict(params))
    assert ours["setups"].sum() > 20
    same(ours["setups"], strategy.setups)
    same(ours["ema_slow"], strategy.ema_slow)


def test_market_filters_equal_research(research):
    import panel as research_panel
    live = make_panel(sessions=360, stocks=50)
    rng = np.random.default_rng(9)
    index = np.cumsum(rng.normal(0.0004, 0.01, (len(live.dates), 2)), axis=0)
    etfs = 100 * np.exp(index)
    symbols = np.array([*live.symbols, "SPY", "QQQ"])
    wide = lambda stock, etf: np.hstack([stock, etf])  # noqa: E731
    fake = lambda a: np.full((len(live.dates), 2), 1.0) * a  # noqa: E731
    theirs = research_panel.Panel(
        live.dates, symbols, np.array(["stock"] * len(live.symbols) + ["etf", "etf"]),
        np.array([""] * len(symbols)), wide(live.open, etfs), wide(live.high, etfs * 1.01),
        wide(live.low, etfs * 0.99), wide(live.close, etfs), wide(live.volume, fake(5e7)),
        np.ones((len(live.dates), len(symbols))))
    reference = research["mf"].compute(theirs)["masks"]
    ours = setups.market_filter_masks(live, theirs.close_ff[:, -2], theirs.close_ff[:, -1])["masks"]
    assert set(ours) == set(reference)
    for code in reference:
        same(ours[code], reference[code])
    assert ours["B"].any() and not ours["B"].all()
