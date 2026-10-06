"""Rule study: the statistics on synthetic months, and the pipeline on the seeded approximate market."""

import random

import pandas as pd
import pytest

from stratlib import rule_study as R
from test_backtest_approx import seed_approximate


def months(n=36, first_year=2019):
    return [f"{first_year + k // 12}-{k % 12 + 1:02d}-01" for k in range(n)]


def synthetic(edge, n_months=36, seed=1):
    """Ten passers and ten failers a month; passers beat failers by `edge` points plus noise."""
    rng = random.Random(seed)
    rows = []
    for day in months(n_months):
        for k in range(20):
            passed = k < 10
            rows.append({"day": day, "rule": float(passed), "6 months": rng.gauss(edge if passed else 0.0, 3.0),
                         "value": k})
    return pd.DataFrame(rows)


def test_cohorts_are_each_months_first_session_with_a_session_to_enter():
    days = ["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04", "2025-03-03"]
    assert R.cohort_days(days) == ["2025-01-30", "2025-02-03"]


def test_a_consistent_edge_helps_and_noise_does_not():
    frame = synthetic(4.0)
    everyone = pd.Series(True, index=frame.index)
    result = R.effect(frame, everyone, "rule", "6 months")
    assert result["months"] == 36 and result["years"] == 3 and result["years_same"] == 3
    assert result["spread"] == pytest.approx(4.0, abs=0.5)
    assert result["pass_share_pct"] == pytest.approx(50)
    assert result["verdict"] == "Helps"
    # Overlapping six-month windows: 36 months count as six independent observations.
    spreads = [result["by_year"][y] for y in ("2019", "2020", "2021")]
    assert all(s > 0 for s in spreads)
    assert R.effect(synthetic(-4.0), everyone, "rule", "6 months")["verdict"] == "Hurts"
    assert R.effect(synthetic(0.0, seed=3), everyone, "rule", "6 months")["verdict"] in ("No clear effect", "Slight help",
                                                                                       "Slight harm")


def test_months_need_stocks_on_both_sides_of_the_rule():
    frame = synthetic(4.0)
    frame.loc[frame["day"] < "2020-01-01", "rule"] = 1.0        # nobody failed in 2019
    result = R.effect(frame, pd.Series(True, index=frame.index), "rule", "6 months")
    assert result["months"] == 24 and result["years"] == 2


def test_buckets_average_each_month_equally():
    frame = synthetic(0.0)
    frame["6 months"] = frame["value"].astype(float)            # excess equals the value
    rows = R.buckets(frame, pd.Series(True, index=frame.index), "value", [0, 10, 20], "6 months")
    assert [(r["low"], r["high"]) for r in rows] == [(0, 10), (10, 20)]
    assert rows[0]["excess"] == pytest.approx(4.5) and rows[1]["excess"] == pytest.approx(14.5)
    assert rows[0]["per_month"] == 10 and rows[0]["months"] == 36


def test_numeric_values_for_list_checks():
    assert R._numeric("a_eps", [40.0, 25.0, 30.0]) == 25.0
    assert R._numeric("s_debt", [0.5, 0.6, 0.8, 1.0]) == pytest.approx(-50.0)
    assert R._numeric("c_acceleration", 2) == 2.0
    assert R._numeric("c_eps", None) is None


def test_study_runs_on_the_seeded_market_and_saves(store, settings):
    settings, start, end = seed_approximate(store, settings)
    frame, context = R.observations(store, settings, start, end)
    assert context["cohorts"] == [start]
    aaa = frame.set_index("symbol").loc["AAA"]
    assert aaa["leader"] == 1 and aaa["passes_screen"] == 1 and aaa["in_base"] == 1
    assert frame["6 months"].isna().all()          # the seeded period is four sessions long
    study = R.run_study(store, settings, start, end)
    assert store.document(R.STUDY)["cohorts"] == 1
    assert store.document(f"{R.STUDY}:{start}:{end}") == store.document(R.STUDY)
    assert all(rule["6 months"]["verdict"] == "Too little data" for rule in study["rules"])
    assert "Rule study" in R.format_study(study)
