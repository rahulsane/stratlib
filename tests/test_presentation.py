"""Wording and ordering shared by the pages: criterion sorting and statement units."""

from stratlib.presentation import sort_screen_rows, statement_value


def test_numeric_criterion_sort_puts_unavailable_values_last():
    rows = [{"symbol": name, "criteria": [{"key": "a_eps", "value": value}]} for name, value in
            [("A", [100, 200, 300]), ("B", [9, 90, 90]), ("C", None), ("D", [-2, 0, 2])]]
    assert [r["symbol"] for r in sort_screen_rows(rows, "A · EPS growth", True)] == ["A", "B", "D", "C"]
    assert [r["symbol"] for r in sort_screen_rows(rows, "A · EPS growth", False)] == ["D", "B", "A", "C"]


def test_statement_totals_are_in_millions_and_per_share_figures_are_not():
    assert statement_value("revenue", 1_200_000_000) == 1200
    assert statement_value("epsDiluted", 1.25) == 1.25
    assert statement_value("reportedCurrency", "USD") == "USD"
