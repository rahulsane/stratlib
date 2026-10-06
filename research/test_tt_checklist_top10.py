"""Portfolio-level checks for quarterly ranking, sizing, retention, and stops."""

import numpy as np
import pytest

from test_engine import make_panel
from engine import Rules, simulate
from panel import Panel
from strategies.tt_checklist_top10 import RankedChecklist, ranked_passers


def make_strategy(passing, top_n=10):
    panel = Panel(**make_panel(n_symbols=15))
    panel.ret63[:] = np.arange(15) / 100
    strategy = RankedChecklist()
    strategy.passes = passing
    strategy.setup(panel, {"stop_pct": 20.0, "equal_weight": True, "top_n": top_n})
    return panel, strategy


def run(panel, strategy):
    return simulate(panel, strategy, str(panel.dates[30]), None,
                    Rules(risk_pct=1.0, max_positions=10, max_position_pct=100 / 3))


def test_buys_only_top_ten_and_allocates_all_cash_to_them():
    panel, strategy = make_strategy({30: frozenset(range(15))})
    result = run(panel, strategy)
    assert {t.ticker for t in result["trades"]} == {f"S{j}" for j in range(5, 15)}
    assert sum(t.position_value for t in result["trades"]) == pytest.approx(100_000)
    assert all(t.position_value == pytest.approx(10_000) for t in result["trades"])
    assert result["held"].max() == 10
    assert result["counts"]["skipped_no_slot"] == 0


def test_reranking_replaces_loser_but_retains_other_positions_and_stops():
    panel, strategy = make_strategy({30: frozenset(range(15)), 50: frozenset(range(15))})
    panel.ret63[50, 0] = 1.0
    strategy.passes = strategy.screen_passes
    strategy.setup(panel, strategy.params)
    result = run(panel, strategy)
    by = {t.ticker: t for t in result["trades"]}
    assert by["S5"].exit_date == str(panel.dates[50])
    assert by["S5"].exit_reason == "fell outside top 10 at a rebalance"
    assert by["S0"].entry_date == str(panel.dates[50])
    assert all(by[f"S{j}"].entry_date == str(panel.dates[30]) for j in range(6, 15))
    assert all(t.stop == 80.0 for t in result["trades"])
    assert result["held"].max() == 10


def test_stopped_position_stays_in_cash_until_next_rebalance():
    panel, strategy = make_strategy({30: frozenset(range(15)), 50: frozenset(range(15))})
    panel.low[40, 14] = 75
    result = run(panel, strategy)
    trades = [t for t in result["trades"] if t.ticker == "S14"]
    assert len(trades) == 2
    assert trades[0].exit_date == str(panel.dates[40])
    assert trades[0].exit_reason == "stop"
    assert trades[1].entry_date == str(panel.dates[50])
    assert np.all(result["held"][10:20] == 9)


def test_missing_momentum_ineligible_and_future_returns_cannot_enter_ranking():
    panel, _ = make_strategy({30: frozenset(range(15))})
    panel.ret63[30, 14] = np.nan
    panel.eligible[30, 13] = False
    panel.ret63[31:, 0] = 99
    selected, audit = ranked_passers(panel, {30: frozenset(range(15))}, 10)
    assert selected[30] == frozenset(range(3, 13))
    assert len(audit) == 13


def test_fewer_than_ten_preserves_the_original_one_third_position_cap():
    panel, strategy = make_strategy({30: frozenset([0, 1])})
    result = run(panel, strategy)
    assert len(result["trades"]) == 2
    assert sum(t.position_value for t in result["trades"]) == pytest.approx(200_000 / 3)


def test_ties_are_deterministic_by_ticker():
    panel, _ = make_strategy({30: frozenset(range(15))})
    panel.ret63[30] = 0.1
    selected, _ = ranked_passers(panel, {30: frozenset(range(15))}, 10)
    expected = sorted(range(15), key=lambda j: str(panel.symbols[j]))[:10]
    assert selected[30] == frozenset(expected)
