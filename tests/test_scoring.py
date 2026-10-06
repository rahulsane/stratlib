"""Recorded statements for real arithmetic; explicit mutations for boundaries."""

from copy import deepcopy
from datetime import date

import pytest

from stratlib.config import Thresholds
from stratlib.scoring import (continuing_eps, growth, leadership_criteria, ordered_statements,
                             score_fundamentals, split_factor)
from conftest import load_fixture

TODAY = date(2026, 9, 28)
T = Thresholds()


@pytest.fixture
def bundle():
    return {
        "income_quarter": load_fixture("income-statement_AAPL_quarter_limit12.json"),
        "income_annual": load_fixture("income-statement_AAPL_annual_limit5.json")[:4],
        "balance_annual": load_fixture("balance-sheet-statement_AAPL_annual_limit4.json"),
        "cash_annual": load_fixture("cash-flow-statement_AAPL_annual_limit4.json"),
        "splits": [], "share_basis_date": "2026-09-28", "fetched_on": "2026-09-28",
    }


def score(bundle):
    return {c.key: c for c in score_fundamentals(bundle, T, TODAY)[0]}


def test_recorded_aapl_measurements(bundle):
    result = score(bundle)
    q, a, b, cf = (bundle[key] for key in ["income_quarter", "income_annual", "balance_annual", "cash_annual"])
    assert result["c_eps"].value == pytest.approx((continuing_eps(q[0]) / continuing_eps(q[4]) - 1) * 100)
    assert result["c_sales"].value == pytest.approx((q[0]["revenue"] / q[4]["revenue"] - 1) * 100)
    assert result["c_margin"].value == pytest.approx(100 * q[0]["netIncomeFromContinuingOperations"] / q[0]["revenue"])
    assert result["a_roe"].value == pytest.approx(100 * a[0]["netIncomeFromContinuingOperations"] /
                                                ((b[0]["totalStockholdersEquity"] + b[1]["totalStockholdersEquity"]) / 2))
    cfps = cf[0]["operatingCashFlow"] / a[0]["weightedAverageShsOutDil"]
    assert result["a_cash"].value == pytest.approx(100 * (cfps / continuing_eps(a[0]) - 1))
    assert result["s_shares"].passed is True
    assert len(result["s_debt"].value) == 4


def set_eps(row, eps):
    row.update(epsDiluted=eps, netIncome=eps * 100, netIncomeFromContinuingOperations=eps * 100,
               weightedAverageShsOutDil=100)


@pytest.mark.parametrize("value,passed", [(1.25, True), (1.249, False), (-1, False)])
def test_quarter_eps_threshold(bundle, value, passed):
    set_eps(bundle["income_quarter"][4], 1)
    set_eps(bundle["income_quarter"][0], value)
    assert score(bundle)["c_eps"].passed is passed


@pytest.mark.parametrize("prior", [0, None, float("nan"), float("inf")])
def test_zero_or_missing_comparison_is_unavailable(prior):
    assert growth(2, prior) is None


@pytest.mark.parametrize("current,prior,expected", [
    (-1.96, -2.13, 100 * 0.17 / 2.13),
    (-1.5, -2, 25), (-2.5, -2, -25), (-2, -2, 0),
    (1, -2, 150), (-1, 2, -150), (0, -2, 100), (0, 2, -100),
    (1.25, 1, 25), (0.75, 1, -25),
])
def test_growth_uses_absolute_prior_value(current, prior, expected):
    assert growth(current, prior) == pytest.approx(expected)


@pytest.mark.parametrize("current", [None, float("nan"), float("inf"), float("-inf")])
def test_missing_or_nonfinite_current_growth_is_unavailable(current):
    assert growth(current, -2) is None


@pytest.mark.parametrize("current,passed", [(-1.5, True), (-1.501, False), (-2.5, False), (1, True)])
def test_quarter_eps_applies_threshold_to_loss_reduction(bundle, current, passed):
    set_eps(bundle["income_quarter"][4], -2)
    set_eps(bundle["income_quarter"][0], current)
    item = score(bundle)["c_eps"]
    assert item.passed is passed
    assert item.value == pytest.approx(100 * (current + 2) / 2)


def test_annual_eps_applies_threshold_to_three_loss_reductions(bundle):
    for row, value in zip(bundle["income_annual"], [-27, -36, -48, -64]):
        set_eps(row, value)
    item = score(bundle)["a_eps"]
    assert item.value == pytest.approx([25, 25, 25])
    assert item.passed is True


def test_zero_prior_eps_stays_explicitly_undefined(bundle):
    set_eps(bundle["income_quarter"][4], 0)
    item = score(bundle)["c_eps"]
    assert item.value is None
    assert item.passed is None
    assert "prior EPS is zero" in item.detail


@pytest.mark.parametrize("rates,passed", [([40, 30, 20, 10], True), ([40, 30, 10, 20], True),
                                         ([40, 10, 20, 30], False), ([20, 20, 20, 20], False)])
def test_eps_acceleration(bundle, rates, passed):
    for i, rate in enumerate(rates):
        set_eps(bundle["income_quarter"][i + 4], 1)
        set_eps(bundle["income_quarter"][i], 1 + rate / 100)
    assert score(bundle)["c_acceleration"].passed is passed


@pytest.mark.parametrize("rates,passed", [([25, 10, 15], True), ([20, 15, 10], True),
                                         ([20, 20, 10], False), ([20, 10, 15], False)])
def test_sales_threshold_or_three_rising_rates(bundle, rates, passed):
    for i, rate in enumerate(rates):
        bundle["income_quarter"][i + 4]["revenue"] = 100
        bundle["income_quarter"][i]["revenue"] = 100 + rate
    assert score(bundle)["c_sales"].passed is passed


@pytest.mark.parametrize("margin,passed", [(19, True), (18.99, False), (-1, False)])
def test_margin_near_three_year_high(bundle, margin, passed):
    for row in bundle["income_quarter"]:
        row.update(revenue=100, netIncomeFromContinuingOperations=20)
    bundle["income_quarter"][0]["netIncomeFromContinuingOperations"] = margin
    assert score(bundle)["c_margin"].passed is passed


@pytest.mark.parametrize("eps,passed", [([1.953125, 1.5625, 1.25, 1], True),
                                       ([2, 1.25, 1.2, 1], False)])
def test_annual_eps_needs_three_separate_growth_years(bundle, eps, passed):
    for row, value in zip(bundle["income_annual"], eps):
        set_eps(row, value)
    assert score(bundle)["a_eps"].passed is passed


@pytest.mark.parametrize("income,passed", [(17, True), (16.99, False)])
def test_roe_uses_average_equity(bundle, income, passed):
    bundle["income_annual"][0]["netIncomeFromContinuingOperations"] = income
    for row, equity in zip(bundle["balance_annual"], [120, 80]):
        row["totalStockholdersEquity"] = equity
    assert score(bundle)["a_roe"].passed is passed


@pytest.mark.parametrize("cash,passed", [(120.01, True), (119.99, False)])
def test_cash_flow_per_diluted_share(bundle, cash, passed):
    set_eps(bundle["income_annual"][0], 1)
    bundle["cash_annual"][0]["operatingCashFlow"] = cash
    assert score(bundle)["a_cash"].passed is passed


def test_cash_flow_exact_threshold(bundle):
    set_eps(bundle["income_annual"][0], 1)
    bundle["cash_annual"][0]["operatingCashFlow"] = 120
    assert score(bundle)["a_cash"].passed is True


@pytest.mark.parametrize("values,passed", [([100, 100, 100, 100], True), ([90, 95, 98, 100], True),
                                          ([90, 110, 98, 100], True), ([91, 90, 98, 100], False),
                                          ([101, 110, 98, 100], False),
                                          ([898002000, 904059000, 903284000, 950182000], True)])
def test_share_trend_over_three_years_and_latest_year(bundle, values, passed):
    for row, value in zip(bundle["income_annual"], values):
        row["weightedAverageShsOutDil"] = value
    assert score(bundle)["s_shares"].passed is passed
    assert score(bundle)["s_shares"].value == pytest.approx((values[0] / values[-1] - 1) * 100)


@pytest.mark.parametrize("values,passed", [([0, 0, 0, 0], True), ([1, 2, 3, 4], True), ([2, 1, 3, 4], True),
                                          ([5, 6, 3, 4], False), ([1, 0, 0, 0], False)])
def test_debt_equity_over_three_years(bundle, values, passed):
    for row, value in zip(bundle["balance_annual"], values):
        row.update(totalDebt=value, totalStockholdersEquity=1)
    assert score(bundle)["s_debt"].passed is passed


def test_share_and_debt_growth_thresholds_apply_to_period_totals(bundle):
    from dataclasses import replace
    for row, value in zip(bundle["income_annual"], [102, 100, 120, 100]):
        row["weightedAverageShsOutDil"] = value
    for row, value in zip(bundle["balance_annual"], [1.1, 2, 0.5, 1]):
        row.update(totalDebt=value, totalStockholdersEquity=1)
    thresholds = replace(T, shares_max_growth_pct=2, shares_latest_max_growth_pct=2, debt_equity_max_growth_pct=10)
    result = {c.key: c for c in score_fundamentals(bundle, thresholds, TODAY)[0]}
    assert result["s_shares"].passed is True
    assert result["s_debt"].passed is True
    result = {c.key: c for c in score_fundamentals(bundle, replace(thresholds, shares_latest_max_growth_pct=0), TODAY)[0]}
    assert result["s_shares"].passed is False


def test_negative_equity_cannot_pass(bundle):
    bundle["balance_annual"][0]["totalStockholdersEquity"] = -1
    assert score(bundle)["a_roe"].passed is None
    assert score(bundle)["s_debt"].passed is None


def test_recorded_nvda_is_not_split_adjusted_twice(bundle):
    bundle["income_annual"] = load_fixture("income-statement_NVDA_annual_limit4.json")
    bundle["splits"] = load_fixture("splits_NVDA.json")
    result = score(bundle)
    assert result["s_shares"].passed is True
    assert abs(result["s_shares"].value) < 5
    assert split_factor(bundle["splits"], date(2024, 6, 5), date(2024, 6, 12)) == 10
    assert split_factor(bundle["splits"], TODAY, TODAY) == 1


def test_continuing_eps_excludes_discontinued_income():
    assert continuing_eps({"epsDiluted": 3, "weightedAverageShsOutDil": 100, "netIncome": 300,
                           "netIncomeFromContinuingOperations": 200}) == 2


def test_identifiable_material_items_are_flagged(bundle):
    bundle["income_quarter"][0]["otherAdjustmentsToNetIncome"] = 100_000_000_000
    _, warnings = score_fundamentals(bundle, T, TODAY)
    assert any("otherAdjustmentsToNetIncome" in warning for warning in warnings)


def test_gap_does_not_compare_wrong_quarter(bundle):
    del bundle["income_quarter"][2]
    assert score(bundle)["c_eps"].passed is None
    assert score(bundle)["c_margin"].passed is None


def test_duplicate_periods_do_not_fill_history(bundle):
    bundle["income_quarter"] = [bundle["income_quarter"][0]] * 12
    assert score(bundle)["c_margin"].passed is None


def test_future_filings_and_currency_changes_are_excluded(bundle):
    bundle["income_quarter"][0]["acceptedDate"] = "2026-10-01 10:00:00"
    assert len(ordered_statements(bundle["income_quarter"], TODAY, False)) == 11
    bundle["income_annual"][2]["reportedCurrency"] = "EUR"
    assert score(bundle)["a_eps"].passed is None
    bundle["cash_annual"][0]["date"] = "2025-09-26"
    assert score(bundle)["a_cash"].passed is None


def test_missing_and_stale_data_never_passes(bundle):
    assert all(c.passed is None for c in score_fundamentals({}, T, TODAY)[0])
    result, warnings = score_fundamentals(bundle, T, date(2030, 1, 1))
    assert all(c.passed is None for c in result)
    assert warnings


def test_pure_scoring_does_not_mutate_inputs(bundle):
    before = deepcopy(bundle)
    score(bundle)
    assert bundle == before


def test_recorded_mrna_loss_reductions_have_values_and_keep_cash_measurement():
    bundle = load_fixture("cached-fundamentals_MRNA_2026-09-28.json")
    result = score(bundle)
    assert result["c_eps"].value == pytest.approx(7.981220657276995)
    assert result["c_eps"].passed is False
    assert result["c_acceleration"].value == 2
    assert result["c_acceleration"].passed is True
    assert result["a_eps"].value == pytest.approx([100 * 2.01 / 9.27, 100 * 3.07 / 12.34, 100 * -32.44 / 20.10])
    assert result["a_eps"].passed is False
    for key in ("c_eps", "c_acceleration", "a_eps"):
        assert "abs(prior)" in result[key].detail
        assert "missing, stale" not in result[key].detail
        assert "None" not in result[key].detail
        assert "Not meaningful:" not in result[key].detail
    assert "loss narrowed" in result["c_eps"].detail
    assert "loss widened" in result["c_acceleration"].detail
    # Cash flow above EPS is a separate same-period comparison, not YoY growth.
    assert result["a_cash"].value is None
    assert result["a_cash"].passed is None
    assert result["a_cash"].detail.startswith("Not meaningful:")
    assert "-1.96" in result["c_eps"].detail
    assert "-2.13" in result["c_eps"].detail
    assert "2026 Q2" in result["c_eps"].detail
    assert "2025 Q2" in result["c_eps"].detail
    assert "-7.26" in result["a_cash"].detail
    assert "-4.81" in result["a_cash"].detail
    assert "2025" in result["a_cash"].detail
    assert "2023" in result["a_eps"].detail
    assert "20.10" in result["a_eps"].detail


def test_missing_eps_is_distinguished_from_nonpositive_eps(bundle):
    bundle["income_quarter"][0]["netIncomeFromContinuingOperations"] = None
    result = score(bundle)["c_eps"]
    assert result.passed is None
    assert result.detail.startswith("Unavailable:")
    assert "continuing EPS is missing" in result.detail
    assert "Not meaningful:" not in result.detail


def test_cash_per_share_is_available_without_an_eps_comparison(bundle):
    set_eps(bundle["income_annual"][0], 0)
    bundle["cash_annual"][0]["operatingCashFlow"] = 125
    result = score(bundle)["a_cash"]
    assert result.passed is None
    assert "cash flow per diluted share: 1.25" in result.detail
    assert "EPS: 0.00" in result.detail
    assert result.detail.startswith("Not meaningful:")


def test_missing_cash_statement_is_not_described_as_nonpositive_eps(bundle):
    set_eps(bundle["income_annual"][0], -1)
    bundle["cash_annual"] = []
    result = score(bundle)["a_cash"]
    assert result.passed is None
    assert result.detail.startswith("Unavailable:")
    assert "matching cash flow statement" in result.detail


def test_cash_comparison_requires_a_known_currency(bundle):
    bundle["income_annual"][0].pop("reportedCurrency")
    bundle["cash_annual"][0].pop("reportedCurrency")
    result = score(bundle)["a_cash"]
    assert result.passed is None
    assert "reported currency is missing" in result.detail


@pytest.mark.parametrize("rs,rank,expected", [(80, 40, [True, True]), (79, 41, [False, False]),
                                           (None, None, [None, None])])
def test_leadership_thresholds(rs, rank, expected):
    items = leadership_criteria(rs, {"rank": rank} if rank is not None else None, T)
    assert [c.passed for c in items] == expected


def test_passing_rule_must_haves_scored_checks_and_unmeasurable():
    from dataclasses import replace
    from stratlib.config import Thresholds
    from stratlib.scoring import CRITERIA, Criterion, meets_rule
    def checks(**results):
        return [Criterion(key, letter, label, None, results.get(key, True), "") for key, letter, label in CRITERIA]
    spec, graded = Thresholds(), replace(Thresholds(), scored_checks_required=5, min_measurable_scored_checks=6)
    assert meets_rule(checks(), spec) and not meets_rule(checks(a_eps=False), spec)
    assert meets_rule(checks(a_eps=False, a_roe=False, s_shares=False), graded)          # 5 of 8
    assert not meets_rule(checks(a_eps=False, a_roe=False, s_shares=False, s_debt=False), graded)
    assert not meets_rule(checks(c_sales=False), graded) and not meets_rule(checks(c_eps=None), graded)
    assert meets_rule(checks(a_cash=None, c_margin=None, a_eps=False, a_roe=False), graded)   # 4 of 6 measurable
    assert not meets_rule(checks(a_cash=None, c_margin=None, a_roe=None), graded)            # too few measurable
