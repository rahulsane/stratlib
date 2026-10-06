"""MSCI GARP: the index rules against the research rebuild, the live screen, its alerts, its backtest account and its
research evidence.

Every company and price here is synthetic. The research functions (research/garp_index.py, mscigarp_index.py and
garp_backtest.py produced the published study) and the app's (msci_garp.py, sim/msci_garp.py) run on the same inputs
and must agree exactly.
"""

import json
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from stratlib import index_scans as IS
from stratlib import msci_garp as MG
from stratlib.evidence import evidence_rows, verdict
from stratlib.exits import assess_scan_position, review_sessions
from stratlib.prices import Bar
from stratlib.screening import run_screen
from stratlib.sell_rules import Position
from stratlib.sim import msci_garp as SG
from stratlib.sim.engine import Rules
from stratlib.strategy_params import MsciGarpParams
from stratlib.universe import parse_listings

RESEARCH = Path(__file__).resolve().parents[1] / "research"
SECTORS = ("Technology", "Healthcare", "Financial Services", "Real Estate", "Industrials")
INDUSTRIES = {"Technology": "Software - Application", "Healthcare": "Biotechnology", "Financial Services": "Banks - Regional",
              "Real Estate": "REIT - Office", "Industrials": "Aerospace & Defense"}


@pytest.fixture(scope="module")
def research():
    sys.path.insert(0, str(RESEARCH))
    try:
        import garp_backtest
        import garp_index
        import mscigarp_backtest
        import mscigarp_index
    except Exception as exc:
        pytest.skip(f"research modules unavailable: {exc}")
    yield {"GI": garp_index, "MI": mscigarp_index, "GB": garp_backtest, "MB": mscigarp_backtest}
    sys.path.remove(str(RESEARCH))


def weekdays(start: date, count: int) -> list[str]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def quarter_ends(first_year: int, last: date) -> list[date]:
    out = []
    for year in range(first_year, last.year + 1):
        for month in (3, 6, 9, 12):
            end = MG.month_end(year, month)
            if end <= last:
                out.append(end)
    return out


def company(rng, sector: str, last: date, *, losses=False, negative_equity=False, dividends=True) -> dict:
    """Statement, cash-flow and dividend bundles in the shapes FMP and the research caches use."""
    revenue0, growth, margin = rng.uniform(2e9, 40e9), rng.uniform(-0.05, 0.35), rng.uniform(0.04, 0.3)
    shares, noise = rng.uniform(2e8, 3e9), rng.normal(0, 0.06, 80)
    annual, quarters, balance, extra_balance, cash_flow = [], [], [], [], []
    for k, year in enumerate(range(2008, last.year)):
        end = date(year, 12, 31)
        if end + timedelta(days=60) > last:
            break
        revenue = revenue0 * (1 + growth) ** k
        income = revenue * (margin + noise[k]) * (-1 if losses and k % 3 == 2 else 1)
        annual.append({"date": end.isoformat(), "filingDate": (end + timedelta(days=60)).isoformat(), "revenue": revenue,
                       "netIncome": income, "weightedAverageShsOutDil": shares * 1.01, "weightedAverageShsOut": shares,
                       "interestIncome": revenue * 0.6 if sector == "Financial Services" else 0,
                       "interestExpense": revenue * 0.2 if sector == "Financial Services" else 0})
    for k, end in enumerate(quarter_ends(2012, last - timedelta(days=40))):
        revenue = revenue0 / 4 * (1 + growth) ** (k / 4)
        income = revenue * (margin + noise[k % 80]) * (-1 if losses and k % 7 == 3 else 1)
        filed = (end + timedelta(days=35)).isoformat()
        quarters.append({"date": end.isoformat(), "filingDate": filed, "revenue": revenue, "netIncome": income,
                         "weightedAverageShsOutDil": shares * 1.01, "weightedAverageShsOut": shares})
        equity = revenue * rng.uniform(2, 6) * (-1 if negative_equity else 1)
        balance.append({"date": end.isoformat(), "filingDate": filed, "totalDebt": revenue * rng.uniform(0, 5),
                        "totalStockholdersEquity": equity})
        extra_balance.append({"date": end.isoformat(), "filingDate": filed, "cashAndShortTermInvestments": revenue * 0.5})
        cash_flow.append({"date": end.isoformat(), "filingDate": filed, "operatingCashFlow": revenue * (margin + 0.05)})
    paid = {}
    if dividends:
        for end in quarter_ends(2015, last):
            paid[(end - timedelta(days=20)).isoformat()] = round(float(rng.uniform(0.05, 0.6)), 4)
    return {"statements": {"fetched_on": last.isoformat(), "income_annual": annual, "income_quarter": quarters,
                           "balance_quarter": balance},
            "extra": {"fetched_on": last.isoformat(), "balance_quarter": extra_balance, "cash_quarter": cash_flow},
            "dividends": {"fetched_on": last.isoformat(), "rows": paid}}


def market(seed=11, n=34, start=date(2018, 1, 2), sessions=900):
    """A synthetic S&P 500: tickers, names (two share classes of one company), sectors, prices, membership and bundles."""
    rng = np.random.default_rng(seed)
    days = weekdays(start, sessions)
    last = date.fromisoformat(days[-1])
    tickers = sorted([f"C{k:02d}" for k in range(n)] + ["C00B"])         # the research's tickers are sorted
    sector = {t: SECTORS[k % len(SECTORS)] for k, t in enumerate(tickers)}
    sector["C00B"] = sector["C00"]
    names = {t: f"Company {t} Inc" for t in tickers}
    names["C00B"] = names["C00"].replace("Inc", "Class B Inc")
    docs = {t: company(rng, sector[t], last, losses=k % 9 == 4, negative_equity=k % 13 == 6, dividends=k % 4 != 1)
            for k, t in enumerate(tickers)}
    docs["C00B"] = docs["C00"]
    del docs[f"C{n - 1:02d}"]                      # one company without statements
    t = np.arange(sessions)
    close = np.vstack([rng.uniform(15, 300) * np.exp(rng.normal(0.0005, 0.02, sessions).cumsum()) for _ in tickers]).T
    close[:40, tickers.index("C05")] = np.nan       # listed later
    volume = rng.uniform(1e6, 2e7, close.shape)
    return {"days": days, "tickers": tickers, "sector": sector, "names": names, "docs": docs, "close": close,
            "volume": volume, "t": t}


def membership_arrays(m, leave="C07", leave_row=600, join="C11", join_row=300):
    member = np.ones(m["close"].shape, dtype=bool)
    if leave:
        member[leave_row:, m["tickers"].index(leave)] = False
    member[:join_row, m["tickers"].index(join)] = False
    return member


def index_data(m, member) -> SG.IndexData:
    tickers, close = m["tickers"], m["close"]
    n = len(m["days"])
    valid = np.isfinite(close)
    px = np.where(valid, close, np.nan)
    for i in range(1, n):
        px[i, np.isnan(px[i])] = px[i - 1, np.isnan(px[i])]
    px = np.nan_to_num(px)
    div = np.zeros(close.shape)
    row = {d: i for i, d in enumerate(m["days"])}
    for j, t in enumerate(tickers):
        for d, v in (m["docs"].get(t, {}).get("dividends", {}).get("rows") or {}).items():
            if d in row:
                div[row[d], j] += v
    last = np.where(valid.any(axis=0), n - 1 - valid[::-1].argmax(axis=0), -1)
    funds = {t: MG.fundamentals(m["docs"][t]["statements"], m["sector"][t] == "Financial Services") if t in m["docs"]
             else MG.Fund([], [], []) for t in tickers}
    extra = {t: MG.extra_series(m["docs"].get(t, {}).get("extra")) for t in tickers}
    return SG.IndexData(np.array(m["days"]), tickers, np.where(valid, close, np.nan), px, m["volume"], np.ones(close.shape),
                        div, member, last, [m["sector"][t] for t in tickers], [INDUSTRIES[m["sector"][t]] for t in tickers],
                        [m["names"][t] for t in tickers], funds, extra)


def research_data(research, data: SG.IndexData, m):
    GI, MI = research["GI"], research["MI"]
    valid = np.isfinite(data.close)
    first = np.where(valid.any(axis=0), valid.argmax(axis=0), len(data.dates))
    funds = {t: GI.fundamentals(m["docs"][t]["statements"], m["sector"][t] == "Financial Services") if t in m["docs"]
             else GI.Fund([], [], []) for t in data.tickers}
    rdata = GI.Data(data.dates, data.tickers, data.close, data.px, data.volume, data.factor, data.div, data.member, first,
                    data.last, list(data.sector), list(data.name), funds)

    class Store:
        @staticmethod
        def document(key):
            symbol = key[len(MI.MD.EXTRA_KEY):]
            return m["docs"].get(symbol, {}).get("extra")

    class Context:
        store = Store()
    return rdata, MI.extra_series(Context(), data.tickers), list(data.industry)


# ---------------------------------------------------------------------------------------- parity with the research

def test_statements_read_the_same_as_the_research(research):
    GI, MI = research["GI"], research["MI"]
    m = market()
    for t, doc in m["docs"].items():
        financial = m["sector"][t] == "Financial Services"
        ours, theirs = MG.fundamentals(doc["statements"], financial), GI.fundamentals(doc["statements"], financial)
        assert (ours.annual, ours.quarters, ours.balance) == (theirs.annual, theirs.quarters, theirs.balance)
        ex = MG.extra_series(doc["extra"])
        for cutoff in ("2019-01-31", "2020-07-31", "2021-04-30"):
            assert MG.fundamentals_at(ours, ex, cutoff) == MI.fundamentals_at(theirs, ex, cutoff)


@pytest.mark.parametrize("variant", ["proxy", "methodology"])
def test_scores_selection_and_weights_equal_the_research(research, variant):
    MI = research["MI"]
    m = market()
    data = index_data(m, membership_arrays(m))
    rdata, ex, industries = research_data(research, data, m)
    reviews = [r for r in MI.schedule(data.dates) if r["label"] >= "2018-11"]
    assert len(reviews) >= 8
    for reb in reviews:
        theirs = MI.score(rdata, ex, industries, reb, variant)
        ours = MG.score(SG.stocks_at(data, reb), variant)
        assert [data.tickers[c] for c in theirs["cols"]] == ours["symbols"]
        for key in ("cap", "pw", "growth", "value", "quality"):
            np.testing.assert_array_equal(ours[key], theirs[key])
        current_cols = set(int(c) for c in theirs["cols"][::3])
        current = {data.tickers[c] for c in current_cols}
        chosen = MG.select(ours, current)
        np.testing.assert_array_equal(chosen, MI.select(theirs, current_cols))
        np.testing.assert_array_equal(MG.tilt_weights(ours, chosen), MI.tilt_weights(theirs, chosen))
    # The two share classes of one company count once, the more traded.
    assert sum(t in ("C00", "C00B") for t in ours["symbols"]) == 1


def test_the_index_and_the_self_managed_account_equal_the_research(research):
    MI, GB, MB = research["MI"], research["GB"], research["MB"]
    m = market()
    # The stock that leaves the S&P 500 is one the index holds, so the account has to sell it between reviews.
    first = SG.build(index_data(m, membership_arrays(m, leave=None)), MsciGarpParams(), "2018-11")
    leaver = next(t for t in first["plan"][4]["target"] if t in first["plan"][5]["target"] and t != "C11")
    leave_row = int(np.searchsorted(np.array(m["days"]), first["plan"][4]["effective"])) + 20
    data = index_data(m, membership_arrays(m, leave=leaver, leave_row=leave_row))
    rdata, ex, industries = research_data(research, data, m)
    theirs = MI.build(rdata, ex, industries, "2018-11")
    start = sorted(theirs["weights"])[2]                  # a review's effective close
    ours = SG.build(data, MsciGarpParams(), "2018-11", keep={start})
    # The research saves its targets rounded to six places.
    assert [p["target"] for p in ours["plan"]] == [{t: pytest.approx(w, abs=6e-7) for t, w in p["target"].items()}
                                                    for p in theirs["plan"]]
    np.testing.assert_allclose(ours["tr"], theirs["tr"], rtol=1e-12, equal_nan=True)
    assert ours["turnover"] == pytest.approx(theirs["turnover"])
    end = len(data.dates) - 1
    run = SG.simulate(data, ours, start, end, Rules(max_positions=500, max_position_pct=100.0))
    research_run = GB.self_managed(rdata, MB.start_at(theirs, rdata, start))
    # Day by day the same account; the app also sells everything at the last close, as the engine does.
    np.testing.assert_allclose(run["equity"][:-1], research_run["curve"][:-1], rtol=1e-12)
    assert run["equity"][-1] < research_run["curve"][-1]
    # Every dollar is accounted for: the final cash is the capital, each holding period's profit and the dividends.
    assert run["equity"][-1] == pytest.approx(100_000 + sum(t.pnl for t in run["trades"]) + run["counts"]["dividends_usd"],
                                              abs=0.05)
    assert run["counts"]["reviews"] == sum(start < r <= end for r in ours["weights"])
    reasons = {t.exit_reason for t in run["trades"]}
    assert "end of test" in reasons and "left the S&P 500" in reasons


# ---------------------------------------------------------------------------------------- the calendar

def test_reviews_take_effect_at_the_last_session_of_february_may_august_and_november():
    days = weekdays(date(2025, 1, 2), 450)
    found = review_sessions(days)
    assert found[:5] == ["2025-02-28", "2025-05-30", "2025-08-29", "2025-11-28", "2026-02-27"]
    sched = MG.schedule(np.array(days), 2025)
    assert [days[r["weights"]] for r in sched][:2] == ["2025-02-17", "2025-05-19"]   # nine sessions before
    assert sched[0]["cutoff"] == "2025-01-31" and days[sched[0]["reference"]] == "2025-01-31"
    assert MG.next_review("2026-09-30") == "2026-11-30" and MG.next_review("2026-11-30") == "2027-02-28"
    assert MG.next_review("2026-12-15") == "2027-02-28"
    # On the Friday before a weekend month end the review has taken effect: only the weekend is left.
    friday = [d for d in days if d <= "2025-11-28"]
    assert MG.schedule(np.array(friday), 2025)[-1]["label"] == "2025-11"
    assert MG.next_review("2025-11-28") == "2026-02-28" and MG.next_review("2025-11-27") == "2025-11-30"


# ---------------------------------------------------------------------------------------- the live screen

def live_market(store, *, leave="C07", join="C11", sessions=760):
    """The synthetic market in the store, ending 2026-09-30, with an S&P 500 change log: ``leave`` is removed on
    2026-09-15 (after the August review) and ``join`` added on 2026-09-10."""
    days = [d for d in weekdays(date(2014, 1, 2), 3400) if d <= "2026-09-30"][-sessions:]
    m = market(sessions=len(days), start=date.fromisoformat(days[0]))
    assert m["days"][-1] == "2026-09-30"
    store.replace_universe(parse_listings([
        {"symbol": t, "companyName": m["names"][t], "exchangeShortName": "NYSE", "sector": m["sector"][t],
         "industry": INDUSTRIES[m["sector"][t]]} for t in m["tickers"]]))
    checked = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def save(symbol, closes, volume):
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, v) for d, c, v in zip(m["days"], closes, volume) if np.isfinite(c)]
        store.write_prices(symbol, bars, replace=True, requested_from=m["days"][0], checked_at=checked, status="ok")
    for symbol in ("SPY", "QQQ"):
        save(symbol, 400 * np.exp(0.0004 * m["t"]), np.full(len(m["days"]), 5e7))
    for j, t in enumerate(m["tickers"]):
        save(t, m["close"][:, j], m["volume"][:, j])
        if t in m["docs"]:
            store.save_document(IS.STATEMENT_KEY + t, m["docs"][t]["statements"])
            store.save_document(IS.EXTRA_KEY + t, m["docs"][t]["extra"])
            store.save_document(IS.DIVIDEND_KEY + t, m["docs"][t]["dividends"])
    current = [{"symbol": t, "name": m["names"][t], "sector": m["sector"][t]} for t in m["tickers"] if t != leave]
    events = [{"date": "2026-09-15", "symbol": "", "removedTicker": leave, "removedSecurity": m["names"][leave]},
              {"date": "2026-09-10", "symbol": join, "addedSecurity": m["names"][join], "removedTicker": ""}]
    store.save_document(IS.MEMBERS_KEY, {"fetched_on": "2026-09-30", "events": events, "current": current})
    return m


def evening(day):
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, 23, tzinfo=timezone.utc)


def garp_settings(settings, **change):
    return replace(settings, strategies={**settings.strategies, "msci_garp": MsciGarpParams(warmup_reviews=2, **change)})


def test_the_screen_holds_the_latest_review_by_todays_weight(store, settings):
    m = live_market(store)
    report = run_screen(store, garp_settings(settings), strategy_id="msci_garp", now=evening(m["days"][-1]))
    assert report["strategy_id"] == "msci_garp" and "params" in report["rule_snapshot"]
    assert report["summary"]["review"] == "2026-08" and report["summary"]["effective"] == "2026-08-31"
    assert report["summary"]["weights_date"] == "2026-08-18" and report["summary"]["next_review"] == "2026-11-30"
    candidates = report["candidates"]
    assert report["members"] == [c["symbol"] for c in candidates]
    assert sum(c["weight_pct"] for c in candidates) == pytest.approx(100, abs=0.01)
    # Without the index's published holdings the screen shows the rebuild's, and says so.
    assert report["summary"]["source"] == "rebuild" and all(c["rebuild_pct"] == c["weight_pct"] for c in candidates)
    assert any("were not available" in n for n in report["warnings"])
    assert [c["weight_pct"] for c in candidates] == sorted((c["weight_pct"] for c in candidates), reverse=True)
    assert [c["rank"] for c in candidates] == list(range(1, len(candidates) + 1))
    # A member added after the review waits for the next one; one that left the S&P 500 left the index that day.
    assert "C11" not in report["members"]
    if any(s["symbol"] == "C07" for s in report["skipped"]):
        left = next(s for s in report["skipped"] if s["symbol"] == "C07")
        assert left["signal_date"] == "2026-09-14" and "left the S&P 500" in left["reason"]
    assert "C07" not in report["members"]
    # Not yet a member (C11), two share classes counted once, and no market cap without statements (C33).
    assert report["counts"]["parent"] == len(m["tickers"]) - 3
    assert report["counts"]["coverage_pct"] >= 50
    assert report["api_calls"] == 0 and any("Cache-only" in w for w in report["warnings"])
    assert any("C33" in w for w in report["warnings"])                  # a member without statements


def test_a_member_that_leaves_the_s_and_p_500_is_dropped_between_reviews(store, settings):
    m = live_market(store)
    held = run_screen(store, garp_settings(settings), strategy_id="msci_garp", now=evening(m["days"][-1]))["members"]
    # Remove the largest holding instead, after the review, and screen again.
    doc = store.document(IS.MEMBERS_KEY)
    gone = held[0]
    doc["current"] = [c for c in doc["current"] if c["symbol"] != gone]
    doc["events"].append({"date": "2026-09-21", "symbol": "", "removedTicker": gone, "removedSecurity": m["names"][gone]})
    store.save_document(IS.MEMBERS_KEY, doc)
    report = run_screen(store, garp_settings(settings), strategy_id="msci_garp", now=evening(m["days"][-1]))
    assert gone not in report["members"]
    left = next(s for s in report["skipped"] if s["symbol"] == gone)
    assert left["signal_date"] == "2026-09-18"
    assert sum(c["weight_pct"] for c in report["candidates"]) == pytest.approx(100, abs=0.01)


def test_an_online_screen_refreshes_the_list_and_only_the_missing_statements(store, settings):
    m = live_market(store)
    store.delete_document(IS.MEMBERS_KEY)
    doc = {"fetched_on": "2026-09-30",
           "events": [{"date": "2026-09-15", "symbol": "", "removedTicker": "C07", "removedSecurity": m["names"]["C07"]}],
           "current": [{"symbol": t, "name": m["names"][t], "sector": m["sector"][t]} for t in m["tickers"] if t != "C07"]}
    some = m["docs"]["C02"]

    def get(path, params=None):
        if path == "historical-sp500-constituent":
            return doc["events"]
        if path == "sp500-constituent":
            return doc["current"]
        if path == "dividends":
            return [{"date": d, "adjDividend": v} for d, v in some["dividends"]["rows"].items()]
        if path == "cash-flow-statement":
            return some["extra"]["cash_quarter"]
        if path == "balance-sheet-statement":
            return [{**a, **b} for a, b in zip(some["statements"]["balance_quarter"], some["extra"]["balance_quarter"])]
        return some["statements"]["income_annual" if params["period"] == "annual" else "income_quarter"]
    client = Mock()
    client.get.side_effect = get
    client.earnings_calendar.return_value = []
    client.stats.api_calls = 0
    report = run_screen(store, garp_settings(settings), client, strategy_id="msci_garp", now=evening(m["days"][-1]))
    assert store.document(IS.MEMBERS_KEY)["fetched_on"] == "2026-09-30"
    fetched = store.document(IS.STATEMENT_KEY + "C33")
    assert fetched is not None and fetched["income_quarter"] and store.document(IS.EXTRA_KEY + "C33")["cash_quarter"]
    assert store.document(IS.DIVIDEND_KEY + "C33")["rows"]
    calls = [c.args[0] for c in client.get.call_args_list]
    assert calls.count("sp500-constituent") == 1 and calls.count("historical-sp500-constituent") == 1
    assert len(calls) == 2 + 5                                           # the list, then one company's five sets
    assert not any("C33" in w for w in report["warnings"])


FUND_CSV = """iShares MSCI USA Quality GARP ETF
Fund Holdings as of,"Sep 30, 2026"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price
"C03","COMPANY C03 INC","Health Care","Equity","1","40.00","1","1","10.00"
"C02","COMPANY C02 INC","Financials","Equity","1","30.00","1","1","20.00"
"XOUT","OUTSIDE HOLDINGS CLASS A","Industrials","Equity","1","29.80","1","1","55.50"
"USD","USD CASH","Cash and/or Derivatives","Cash","1","0.20","1","1","1.00"
"""


def test_the_screen_lists_the_index_holdings_published_by_ishares(store, settings):
    from stratlib import estimate_snapshots as ES
    m = live_market(store)
    store.save_document(IS.FUND_KEY, {**ES.ishares_holdings(FUND_CSV), "fetched_on": "2026-09-30"})
    report = run_screen(store, garp_settings(settings), strategy_id="msci_garp", now=evening(m["days"][-1]))
    assert report["summary"]["source"] == "ishares" and report["summary"]["as_of"] == "2026-09-30"
    assert report["members"] == ["C03", "C02", "XOUT"]                       # the fund's holdings, by weight
    rows = {c["symbol"]: c for c in report["candidates"]}
    assert [round(c["weight_pct"], 2) for c in report["candidates"]] == [40.08, 30.06, 29.86]   # cash left out
    assert rows["XOUT"]["close"] == 55.5 and rows["XOUT"]["growth"] is None    # no stored prices, not in the S&P 500
    assert rows["XOUT"]["name"] == "Outside Holdings Class A" and rows["XOUT"]["rebuild_pct"] == 0
    assert rows["C03"]["close"] == pytest.approx(m["close"][-1, m["tickers"].index("C03")])
    assert rows["C03"]["growth"] is not None                                  # scored by the rebuild
    counts = report["counts"]
    assert counts["index_held"] == 3 and counts["rebuild_held"] > 3 and counts["both"] <= 2
    assert "iShares GARP ETF held it on 2026-09-30" in report["warnings"][-1] or any(
        "iShares GARP ETF held it on 2026-09-30" in n for n in report["warnings"])


def test_an_online_screen_downloads_the_holdings_and_an_old_copy_is_flagged(store, settings, monkeypatch):
    from stratlib import estimate_snapshots as ES
    m = live_market(store)
    monkeypatch.setattr(ES, "download", lambda url: FUND_CSV)
    fund, problem = IS.load_fund(store, True, "2026-09-30")
    assert problem is None and fund["as_of"] == "2026-09-30" and store.document(IS.FUND_KEY)["rows"]
    def fail(url):
        raise ConnectionError("offline")
    monkeypatch.setattr(ES, "download", fail)
    fund, problem = IS.load_fund(store, True, "2026-10-09")                   # the cache, now nine days old
    assert fund["as_of"] == "2026-09-30" and "could not be downloaded" in problem and "file of 2026-09-30" in problem
    assert problem.startswith("iShares' holdings file could not be downloaded") and "; the index's" in problem
    store.save_document(IS.FUND_KEY, {**store.document(IS.FUND_KEY), "as_of": "2026-09-20"})
    assert IS.load_fund(store, False, "2026-09-30")[1].startswith("The index's holdings are iShares' file of 2026-09-20")
    store.delete_document(IS.FUND_KEY)
    assert IS.load_fund(store, False, "2026-09-30") == (None, None)
    # A monthly estimates snapshot's copy is used when the cache is empty.
    store.save_document(ES.KEY + "2026-09", {"files": {"garp": ES.ishares_holdings(FUND_CSV)}})
    assert IS.load_fund(store, False, "2026-09-30")[0]["as_of"] == "2026-09-30"
    assert m["days"][-1] == "2026-09-30"


# ---------------------------------------------------------------------------------------- alerts

def bars_from(days, close=50.0):
    return [Bar(d, close, close, close, close, 1e6) for d in days]


def test_alerts_follow_the_index_reviews(settings):
    days = weekdays(date(2026, 1, 2), 190)
    days = [d for d in days if d <= "2026-09-30"]
    params = MsciGarpParams()
    screen = {"price_date": "2026-09-30", "members": ["KEEP"], "skipped": [{"symbol": "GONE", "signal_date": "2026-09-14"}]}

    def alert(symbol, entry, screen=screen):
        return assess_scan_position("msci_garp", params, Position(symbol, entry, 50.0), bars_from(days), as_of=days[-1],
                                    screen=screen, sessions=days)
    kept = alert("KEEP", "2026-03-02")
    assert kept.action == "Hold" and kept.hold_until == "2026-11-30"
    dropped = alert("DROP", "2026-03-02")
    assert dropped.action == "Sell" and "2026-08-31" in dropped.reason
    assert alert("GONE", "2026-09-01").action == "Sell"
    late = alert("DROP", "2026-09-08")                                  # bought after the review, outside the index
    assert late.action == "Hold" and "next review" in late.reason
    stale = alert("DROP", "2026-03-02", {**screen, "price_date": "2026-08-20"})
    assert stale.action == "Hold" and "predates" in stale.reason
    assert alert("KEEP", "2026-03-02", None).action == "Hold"
    # With the index's published holdings, a stock they leave out is sold.
    live = {"price_date": "2026-09-30", "members": ["KEEP"], "summary": {"source": "ishares", "as_of": "2026-09-30"}}
    assert alert("KEEP", "2026-09-08", live).action == "Hold"
    out = alert("DROP", "2026-09-08", live)
    assert out.action == "Sell" and "no longer holds it" in out.reason


# ---------------------------------------------------------------------------------------- research evidence

def test_the_research_study_supplies_the_evidence(tmp_path):
    folder = tmp_path / "output" / "mscigarp"
    folder.mkdir(parents=True)
    row = lambda cagr, dd: {"cagr": cagr, "maxdd": dd}  # noqa: E731
    periods = {"Main: Dec 2015 - now": {"Version 2: ETF (0.20%/yr)": row(17.4, 35.7), "Version 1: self-managed": row(17.2, 35.2),
                                        "Rebuilt index, self-managed": row(15.7, 33.6), "SPY": row(15.1, 33.7)},
               "Out-of-sample 2022-now": {"Version 2: ETF (0.20%/yr)": row(15.7, 31.3), "SPY": row(11.9, 24.5)}}
    (folder / "results.json").write_text(json.dumps({"periods": periods}), encoding="utf-8")
    rows = evidence_rows("msci_garp", tmp_path)
    assert len(rows) == 4 and rows[0]["Gap vs SPY, pp"] == pytest.approx(2.3)
    assert "Trades" not in rows[0]
    note = verdict("msci_garp", tmp_path)
    assert "17.4% a year against SPY's 15.1%" in note and "15.7%" in note and "34%" in note
    assert verdict("msci_garp", tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------------------- the comparable backtest

def test_the_comparable_backtest_saves_every_period_like_the_other_strategies(store, settings):
    from stratlib.sim.runs import latest_results, run_all, trade_rows
    days = [d for d in weekdays(date(2015, 1, 2), 3100) if d <= "2026-09-30"]
    m = live_market(store, sessions=len(days))
    series = {t: {"source": "db", "name": m["names"][t], "sector": m["sector"][t], "industry": INDUSTRIES[m["sector"][t]],
                  "spans": [[m["days"][0], "2026-09-15" if t == "C07" else ""]]} for t in m["tickers"]}
    store.save_document(SG.RESOLVED_KEY, {"made_on": "2026-10-01", "series": series, "missing": [], "renamed": []})
    store.save_document("research:spy_dividends", {"rows": {}})
    store.save_document("research:tbill3m", {"rows": {d: 2.0 for d in m["days"]}})
    doc = run_all(store, settings, ["msci_garp"])["msci_garp"]
    assert latest_results(store) == {"msci_garp": doc}
    assert set(doc["results"]) == {"in_sample", "out_of_sample", "combined"}
    combined = doc["results"]["combined"]
    assert combined["start"] == "2016-01-01" and combined["end"] == "2026-09-30"
    assert combined["trades"] > 0 and combined["spy"]["cagr"] is not None and combined["counts"]["reviews"] > 30
    assert len(combined["equity_curve"]["equity"]) == len(combined["equity_curve"]["dates"])
    assert doc["liquidity"] == {"min_price": 0.0, "min_dollar_volume": 0.0}
    assert doc["rule_snapshot"]["strategy_id"] == "msci_garp"
    trades = trade_rows(doc["trades"]["combined"])
    assert trades and all(t["exit_date"] >= t["entry_date"] for t in trades)
    assert {t["exit_reason"] for t in trades} >= {"end of test"}


def test_the_backtest_explains_what_it_needs_without_the_research_data(store, settings):
    from stratlib.sim.runs import run_all
    live_market(store)
    store.save_document("research:spy_dividends", {"rows": {}})
    store.save_document("research:tbill3m", {"rows": {}})
    with pytest.raises(ValueError, match="research/garp_data.py"):
        run_all(store, settings, ["msci_garp"])


def test_the_hypothetical_portfolio_buys_index_weights_to_the_nearest_share(settings):
    from stratlib.portfolio import hypothetical_portfolio
    candidates = [{"symbol": "BIG", "name": "Big", "close": 1000.0, "weight_pct": 65.5},
                  {"symbol": "MID", "name": "Mid", "close": 30.0, "weight_pct": 34.2},
                  {"symbol": "TINY", "name": "Tiny", "close": 500.0, "weight_pct": 0.3}]
    report = {"price_date": "2026-09-30", "candidates": candidates}
    plan = hypothetical_portfolio(report, "msci_garp", settings.thresholds, settings.backtest, {}, 10_000, params=settings)
    shares = {r["symbol"]: r["shares"] for r in plan["rows"]}
    # 0.998 x $6,550 / $1,000 = 6.5 -> 7 and 0.998 x $3,420 / $30 = 113.8 -> 114 overspend ($10,420), so the largest
    # holding gives up a share; $29.94 / $500 is under half a share.
    assert shares == {"BIG": 6, "MID": 114}
    assert plan["skipped"] == [("TINY", "its weight is under half a share")]
    assert plan["invested"] == pytest.approx(6 * 1000 + 114 * 30) and plan["cash"] == pytest.approx(580)
