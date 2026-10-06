"""The statement-based scans must equal the research code on the same statements.

research/nash_screen.py, nash_screen_clean.py and tt_quality.py produced the published results. These tests
feed seeded synthetic statement bundles (quarterly income, balance-sheet, cash-flow and key-metric rows in the
shape FMP returns them) to both the research functions and fundamental_scans.py and require identical output.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from stratlib import fundamental_scans as fs
from stratlib.live_panel import LivePanel
from stratlib.strategy_params import ChecklistParams

RESEARCH = Path(__file__).resolve().parents[1] / "research"


@pytest.fixture(scope="module")
def research():
    sys.path.insert(0, str(RESEARCH))
    try:
        import nash_screen
        import nash_screen_clean
        import panel as research_panel
        import tt_quality
        from nash_rules import PriceTools
    except Exception as exc:  # the checklist module reads a scratch folder outside the repository
        pytest.skip(f"research modules unavailable: {exc}")
    yield {"nash": nash_screen, "clean": nash_screen_clean, "tt": tt_quality, "panel": research_panel, "tools": PriceTools}
    sys.path.remove(str(RESEARCH))


def quarter_ends(n, first=date(2019, 3, 31)):
    out, year, month = [], first.year, first.month
    for _ in range(n):
        last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)) if month < 12 else date(year, 12, 31)
        out.append(last)
        month += 3
        if month > 12:
            month -= 12
            year += 1
    return out


def make_doc(seed, n=26, *, currency="USD", gap_at=None, growth=0.03, profitable=True, first=date(2019, 3, 31),
             fcf_level=0.04):
    rng = np.random.default_rng(seed)
    days = quarter_ends(n, first)
    if gap_at is not None:
        days = [d for i, d in enumerate(days) if i != gap_at]        # a missing quarter breaks the run
    income, balance, cash, metrics = [], [], [], []
    base = rng.uniform(150e6, 900e6)
    for i, d in enumerate(days):
        revenue = base * (1 + growth + rng.normal(0, 0.01)) ** i
        opex = revenue * rng.uniform(0.25, 0.45)
        operating = revenue * (rng.uniform(0.12, 0.3) if profitable else rng.uniform(-0.2, 0.05))
        pretax = operating * rng.uniform(0.9, 1.05)
        filed = (d + timedelta(days=int(rng.integers(5, 50)))).isoformat()
        income.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency, "revenue": revenue,
                       "operatingExpenses": opex, "operatingIncome": operating, "incomeBeforeTax": pretax,
                       "incomeTaxExpense": pretax * rng.uniform(0.1, 0.3), "netIncome": pretax * 0.8,
                       "weightedAverageShsOutDil": 100e6 * (1 + rng.normal(0, 0.005))})
        balance.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency,
                        "cashAndShortTermInvestments": revenue * rng.uniform(0.3, 1.4),
                        "totalDebt": revenue * rng.uniform(0.1, 1.0),
                        "capitalLeaseObligations": revenue * rng.uniform(0, 0.1) if i % 3 else None,
                        "totalStockholdersEquity": revenue * rng.uniform(0.5, 2.5)})
        cash.append({"date": d.isoformat(), "filingDate": filed, "reportedCurrency": currency,
                     "freeCashFlow": revenue * (fcf_level + 0.012 * i / n + rng.normal(0, 0.03))})
        metrics.append({"date": d.isoformat(), "reportedCurrency": currency if rng.random() > 0.05 else "CAD",
                        "marketCap": revenue * 4 * (1 + 0.04 * i) * rng.uniform(0.8, 1.2)})
    if n > 8:
        income[-2]["netIncome"] = None                                # an incomplete row
    return {"fetched_on": "2026-09-30", "income": income, "balance": balance, "cash": cash, "metrics": metrics}


DOCS = [make_doc(1, fcf_level=0.2), make_doc(2, growth=0.06, fcf_level=0.2), make_doc(3, gap_at=10), make_doc(4, currency="EUR"),
        make_doc(5, profitable=False), make_doc(6, n=9), make_doc(7, n=6), make_doc(8, growth=0.0)]


@pytest.mark.parametrize("doc", DOCS, ids=range(len(DOCS)))
def test_nash_snapshots_and_the_data_guards_equal_research(research, doc):
    ours, theirs = fs.nash_snapshots(doc), research["nash"].snapshots(doc)
    assert ours == theirs
    assert fs.nash_clean_snapshots(doc) == research["clean"].clean_snapshots(doc)


def test_the_nash_fixtures_exercise_both_outcomes(research):
    from stratlib.strategy_params import NashParams
    outcomes = {fs.nash_passes(m, NashParams(margin=margin)) for margin in ("FCF", "OM")
                for doc in DOCS for _, _, m in fs.nash_snapshots(doc)}
    assert outcomes == {True, False}


@pytest.mark.parametrize("margin", ["FCF", "OM"])
def test_the_nash_pass_rule_equals_research(research, margin):
    from stratlib.strategy_params import NashParams
    params = NashParams(margin=margin)
    checked = 0
    for doc in DOCS:
        for _, _, m in fs.nash_snapshots(doc):
            assert fs.nash_passes(m, params) == research["nash"].passes(m, margin)
            checked += 1
    assert checked > 50


def test_the_nash_sector_exclusions_equal_research(research):
    for sector, industry in [("Industrials", "Aerospace & Defense"), ("Industrials", "Machinery"), ("Energy", "Oil"),
                             ("Technology", "Semiconductors"), ("Technology", "Software"), ("Healthcare", "Biotech")]:
        assert fs.cyclical(sector, industry) == research["nash"].cyclical(sector, industry)


@pytest.mark.parametrize("doc", DOCS, ids=range(len(DOCS)))
def test_quality_series_equals_research(research, doc):
    ours, theirs = fs.quality_series(doc), research["tt"].quality_series(doc)
    assert (ours is None) == (theirs is None)
    if ours is None:
        return
    assert ours.dates == theirs.dates and ours.avail == theirs.avail and ours.ccy == theirs.ccy
    np.testing.assert_array_equal(ours.run, theirs.run)
    assert set(ours.m) <= set(theirs.m)
    for name in ours.m:
        np.testing.assert_array_equal(np.nan_to_num(ours.m[name], nan=-7e9), np.nan_to_num(theirs.m[name], nan=-7e9), name)


def stock_panel(n_sessions=900, symbols=8, seed=11):
    rng = np.random.default_rng(seed)
    days, day = [], date(2023, 1, 2)
    while len(days) < n_sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    close = 40 * np.exp(np.cumsum(rng.normal(0.0004, 0.014, (n_sessions, symbols)), axis=0))
    return LivePanel(np.array(days), np.array([f"S{j}" for j in range(symbols)]), close, close * 1.01, close * 0.99, close,
                     np.full_like(close, 2e6))


def test_quality_flags_equal_research_rows(research):
    live = stock_panel()
    kind = np.array(["stock"] * len(live.symbols))
    theirs = research["panel"].Panel(live.dates, live.symbols, kind, np.array([""] * len(live.symbols)), live.open, live.high,
                                     live.low, live.close, live.volume, np.ones_like(live.close))
    tools = research["tools"](theirs)
    # Statements end (31 March 2026) shortly before the price history does, as a live screen sees them.
    docs = {s: make_doc(20 + j, n=22, first=date(2020, 9, 30), growth=[0.02, 0.04, 0.0, 0.05, 0.03, 0.06, 0.01, 0.045][j])
            for j, s in enumerate(live.symbols)}
    their_series = {s: research["tt"].quality_series(d) for s, d in docs.items()}
    row = len(live.dates) - 1
    expected = research["tt"].quality_rows(theirs, tools, their_series, {}, row)
    day = str(live.dates[row])
    params = ChecklistParams()
    compared = flagged = 0
    for j, symbol in enumerate(live.symbols):
        series = fs.quality_series(docs[symbol])
        k = fs.snapshot_index(series, day, params.stale_days)
        if symbol not in expected:
            assert k is None
            continue
        flags = fs.quality_flags(series, k, float(live.close[row, j]), fs.close_on_or_before(live, j, series.dates[k]), params)
        for name in ("PEHIST", "PEGT", "ROIC15", "DE1", "FCFUP", "QUAL"):
            assert np.isclose(flags[name], expected[symbol][name], equal_nan=True), (symbol, name)
            compared += 1
        flagged += int(flags["QUAL"] in (0.0, 1.0))
    assert compared >= 24 and flagged >= 3
