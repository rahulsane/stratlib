"""TradeTest's server side: the bank builder, sealed cursors, set selection, the rows sent to the browser, the API and
the page. Banks here are tiny: one built from a synthetic database, others written directly."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

import stratlib.tradetest as tradetest
import stratlib.tradetest_bank as tradetest_bank
from stratlib.prices import Bar
from stratlib.store import Store
from stratlib.tradetest import (MAX_AVOID, Sealer, TradeTestError, TradeTestService, TradeTestUnavailable,
                                dither_steps, dither_volume, lattice_strength, load_bank, price_grid, residual_grid,
                                window_rows)
from stratlib.tradetest_bank import (CHECKS, CRASHES, FRESH, LOOKBACK, N_CONTEXT, SHOCKS, WINDOW, BankBuilder,
                                     bad_bars, bad_prints, bank_version, build_bank, calendar_ok, day_numbers,
                                     dividend_adjustments, eligible_starts, event_scope, repair, splice_points,
                                     split_factors, unrecorded_splits, window_checks)
from stratlib.universe import ListedStock
from stratlib.web.app import PAGES, PUBLIC_PAGES, root
from stratlib.web.data import Data
from stratlib.web.tradetest import RateLimiter, api_router, client_address

NOW = datetime(2026, 10, 8, 22, 0)
KEY = b"test key"


def sessions(first: str, last: str) -> list[str]:
    days = np.arange(np.datetime64(first), np.datetime64(last) + np.timedelta64(1, "D"))
    return [str(d) for d in days[np.is_busday(days)]]


def walk(dates, start=50.0, *, seed=0, volume=1e6) -> list[Bar]:
    """A random walk of daily bars, each opening at the last close."""
    closes = start * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.01, len(dates))))
    bars, previous = [], start
    for day, close in zip(dates, closes.tolist()):
        bars.append(Bar(day, previous, max(previous, close) * 1.01, min(previous, close) * 0.99, close, volume))
        previous = close
    return bars


def scaled(bars, factor, volume=None):
    return [replace(b, open=b.open * factor, high=b.high * factor, low=b.low * factor, close=b.close * factor,
                    volume=b.volume if volume is None else volume) for b in bars]


def in_cents(bars):
    return [replace(b, open=round(b.open, 2), high=round(b.high, 2), low=round(b.low, 2), close=round(b.close, 2))
            for b in bars]


def wave(dates, level, *, cents=True, volume=2e7) -> list[Bar]:
    """Bars that wander within about 12% of ``level`` and keep coming back (so no test depends on where a walk
    drifts), in whole cents or not."""
    rng, wander = np.random.default_rng(int(level * 100)), np.zeros(len(dates))
    for t in range(1, len(dates)):
        wander[t] = 0.97 * wander[t - 1] + 0.01 * rng.standard_normal()
    closes = level * np.exp(wander)
    closes = np.round(closes, 2) if cents else closes
    opens = np.concatenate([[closes[0]], closes[:-1]])
    pad, rounded = (0.02, lambda x: round(x, 2)) if cents else (level * 0.004, float)
    return [Bar(day, o, rounded(max(o, c) + pad), rounded(min(o, c) - pad), c, volume)
            for day, o, c in zip(dates, opens.tolist(), closes.tolist())]


def listed(symbol, name, *, industry="Software", is_etf=False):
    return ListedStock(symbol, name, "NASDAQ", "NASDAQ Global Select", "Technology", industry, "US", 1e9, 50.0, 1e6,
                       is_etf, False, "etf" if is_etf else None)


def split(symbol, day, numerator, denominator):
    return {"symbol": symbol, "date": day, "numerator": numerator, "denominator": denominator}


@pytest.fixture
def market(tmp_path):
    """A synthetic database: SPY's calendar, four clean stocks, one delisted stock and one of each flaw, the ETFs
    the database holds, split records (one the stored prices contradict), special dividends, reused tickers and series
    stored under two tickers (one of them adjusted differently)."""
    calendar = sessions("2009-06-01", "2014-02-28")
    store = Store(tmp_path / "market.db")
    write = lambda symbol, bars: store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date,
                                                    checked_at=NOW, status="ok")
    for i, symbol in enumerate(("SPY", "QQQ", "^GSPC", "DIA", "TQQQ")):
        write(symbol, walk(calendar, 130.0, seed=100 + i, volume=1e8))
    for i, symbol in enumerate(("AAA", "BBB", "CCC", "DDD")):
        write(symbol, walk(calendar, seed=i))
    write("PNY", walk(calendar, 3.0, seed=10))                     # a penny stock
    write("THIN", walk(calendar, seed=11, volume=1e4))             # $0.5M a day
    write("GAP", [b for i, b in enumerate(walk(calendar, seed=12)) if i not in (300, 600, 900)])
    jumps = [1.8 ** ((i >= 300) + (i >= 600) + (i >= 900)) for i in range(len(calendar))]   # +80% three times
    write("GLT", [scaled([b], f)[0] for b, f in zip(walk(calendar, seed=13), jumps)])
    write("FLAT", [replace(b, open=b.close, high=b.close, low=b.close) if i % 20 == 0 else b
                   for i, b in enumerate(walk(calendar, seed=14))])
    write("ZERO", [replace(b, volume=0) if i % 50 == 0 else b for i, b in enumerate(walk(calendar, seed=15))])
    write("SHEL", walk(calendar, seed=16))
    write("ETFX", walk(calendar, seed=17))
    old = [d for d in calendar if d <= "2013-06-28"]
    write("OLD", walk(old, seed=18))
    write("FOR", walk(calendar, seed=19))
    write("GLD", walk(calendar, 110.0, seed=21, volume=5e6))       # the database's GLD wins over the cache's
    # Split records: stored prices are adjusted, so these trade at 4x, 1/10 or 1000x the stored price before them.
    write("SPLT", wave(calendar, 3.3, cents=False, volume=5e6))
    write("RVS", wave(calendar, 36.25))
    write("HUGE", walk(calendar, seed=24))
    write("BADS", walk(calendar, seed=25))
    write("STEP", wave(calendar, 3.0))                              # cents on a $3 stored price: a staircase
    # REUS: an old company until the splice at 620, today's company after it (5x the price, 30x the volume).
    first = walk(calendar[:620], 30.0, seed=27)
    write("REUS", first + walk(calendar[620:], first[-1].close * 5, seed=28, volume=3e7))
    # KMGX, a directory stock: its own listing, another company's prices from 460 to 1000, then its own again.
    a = walk(calendar[:460], seed=29)
    b = walk(calendar[460:1000], a[-1].close * 5, seed=30, volume=3e7)
    write("KMGX", a + b + walk(calendar[1000:], b[-1].close / 5, seed=31))
    write("NEWC", walk(calendar, seed=32))                         # its saved profile says it listed at 500
    # PRD's series is CAR's too until 700, where CAR's ticker passes to a new company at less than half the price
    # (no splice: the volume hardly changes).
    prd = walk(calendar, seed=33)
    write("PRD", prd)
    write("CAR", prd[:700] + walk(calendar[700:], prd[699].close * 0.45, seed=34))
    # RETD is RENM's old ticker: the same series until RETD stops at 903 and RENM carries on. (903 is a few sessions
    # into a probe of the series, so both tickers have prices after the last probe they share.)
    renm = walk(calendar, seed=35)
    write("RENM", renm)
    write("RETD", renm[:903])
    # NXT's stored prices are adjusted for the 5-for-2 split its record has at 700, so no close of theirs is the same
    # as CRN's, which holds the same history as traded, in whole cents, until 700. There CRN keeps a separation its
    # stored prices never adjusted for (a third off) and goes on as another company, while NXT carries the series on.
    orig = in_cents(walk(calendar, seed=37))
    write("NXT", scaled(orig, 1 / 2.5, volume=2.5e6))
    write("CRN", orig[:700] + in_cents(walk(calendar[700:], orig[699].close * 0.67, seed=38)))
    # VMK carries on EQX's series (its retired ticker, stopped at 1100), but VMK's split document has a 2-for-1 split
    # at 1150 that the prices never show: the two tickers' prices as traded disagree.
    eqx = walk(calendar, seed=42)
    write("VMK", eqx)
    write("EQX", eqx[:1100])
    # UNAD's stored prices halve at 800, on the date of a 2-for-1 split record: they were never adjusted for it.
    write("UNAD", [scaled([b], 0.5 if i >= 800 else 1.0)[0] for i, b in enumerate(walk(calendar, 60.0, seed=39))])
    # Special dividends at 500 in the dividend documents: DIVA's stored prices run straight through its 40% (they were
    # adjusted for it), DIVB's fall by its 30% (they are as traded around it).
    diva, divb = walk(calendar, seed=43), walk(calendar, seed=44)
    write("DIVA", diva)
    write("DIVB", divb[:500] + scaled(divb[500:], 0.7))
    store.replace_universe([listed(s, f"{s} Corp") for s in ("AAA", "BBB", "CCC", "DDD", "PNY", "THIN", "GAP", "GLT",
                                                              "FLAT", "ZERO", "SPLT", "RVS", "HUGE", "BADS", "STEP",
                                                              "REUS", "NEWC", "PRD", "CAR", "RENM", "NXT", "CRN",
                                                              "UNAD", "VMK", "DIVA", "DIVB")]
                           + [listed("SHEL", "Blank Check Acquisition Corp", industry="Shell Companies"),
                              listed("ETFX", "Some ETF", is_etf=True)])
    store.save_document("backtest:delisted", {"rows": [
        {"symbol": "OLD", "companyName": "Old Industries Inc.", "exchange": "NYSE", "ipoDate": "1999-01-04",
         "delistedDate": "2013-06-28"},
        {"symbol": "FOR", "companyName": "Foreign Holdings plc", "exchange": "LSE", "ipoDate": "2001-01-02",
         "delistedDate": "2020-01-02"},
        {"symbol": "REUS", "companyName": "Old Reus Corp", "exchange": "NYSE", "ipoDate": "1999-01-04",
         "delistedDate": calendar[619]},
        {"symbol": "KMGX", "companyName": "KMGX Chemicals", "exchange": "NYSE", "ipoDate": "1997-01-28",
         "delistedDate": "2014-02-28"},
        {"symbol": "RETD", "companyName": "Retired Ticker Inc.", "exchange": "NYSE", "ipoDate": "1999-01-04",
         "delistedDate": calendar[902]},
        {"symbol": "EQX", "companyName": "Equity Example Trust", "exchange": "NYSE", "ipoDate": "1999-01-04",
         "delistedDate": calendar[1099]}]})
    for symbol, rows in {"SPLT": [split("SPLT", "2012-01-03", 4, 1)], "RVS": [split("RVS", "2013-06-03", 1, 10)],
                         "HUGE": [split("HUGE", calendar[-1], 1000, 1)], "BADS": [split("BADS", "2012-01-03", 0, 1)],
                         "STEP": [split("STEP", calendar[-1], 4, 1)], "UNAD": [split("UNAD", calendar[800], 2, 1)],
                         "NXT": [split("NXT", calendar[700], 5, 2)], "VMK": [split("VMK", calendar[1150], 2, 1)],
                         # A split after the last stored day is not in the stored prices yet.
                         "AAA": [split("AAA", "2014-06-02", 50, 1)]}.items():
        store.save_document(f"backtest:splits:{symbol}", {"fetched_on": "2014-03-01", "rows": rows})
    store.save_document("backtest:approx:profile:NEWC", {"profile": {"symbol": "NEWC", "ipoDate": calendar[500]}})
    for symbol, bars, share in (("DIVA", diva, 0.4), ("DIVB", divb, 0.3)):
        store.save_document(f"research:nash:div:{symbol}", {"rows": {calendar[100]: 0.2,
                                                                      calendar[500]: share * bars[499].close}})
    store.close()
    gold = {b.date: [b.open, b.high, b.low, b.close, 5e6] for b in walk(calendar, 120.0, seed=20)}
    silver = {b.date: [b.open, b.high, b.low, b.close, 5e6] for b in walk(calendar, 20.0, seed=36)}
    cache = tmp_path / "etfs.json"
    cache.write_text(json.dumps({"bars": {"GLD": gold, "SLV": silver, "XYZE": gold, "SPY": {}}}), encoding="utf-8")
    return tmp_path / "market.db", cache, calendar


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bank(path):
    with np.load(path, allow_pickle=False) as data:
        return {name: data[name] for name in data.files}


def windows_of(bank, symbol, calendar):
    """(first lookback position, first bar, last bar, listing index) of each of the symbol's windows."""
    days = day_numbers(calendar)
    out = []
    for i in np.flatnonzero(bank["symbols"][bank["instrument"]] == symbol):
        start = int(np.searchsorted(days, bank["day"][i][0]))
        out.append((start - LOOKBACK, start, start + WINDOW - 1, int(bank["instrument"][i]), int(i)))
    return out


# The builder ------------------------------------------------------------------------------------------------------

@pytest.fixture
def built(market, tmp_path):
    db, cache, calendar = market
    before = digest(db)
    summary = build_bank(db, tmp_path / "bank.npz", windows=200, seed=7, etf_cache=cache)
    assert digest(db) == before
    return summary, read_bank(tmp_path / "bank.npz"), calendar


def test_the_builder_takes_every_clean_window_from_a_read_only_database(built, market, tmp_path):
    summary, bank, calendar = built
    meta = json.loads(str(bank["meta"]))
    n = summary["windows"]
    # The synthetic pool runs dry long before 200: windows on one instrument never share a session.
    assert 20 <= n < 100 and meta["windows"] == n and meta["version"] == summary["version"]
    assert (meta["n_context"], meta["n_replay"], meta["lookback"]) == (150, 100, 199)
    assert meta["data_through"] == calendar[-1] and len(meta["version"]) == 12
    assert bank["bars"].shape == (n, WINDOW, 5) and bank["bars"].dtype == np.float32
    assert bank["lookback"].shape == (n, LOOKBACK) and bank["day"].shape == (n, WINDOW)
    assert bank["basis"].shape == (n,) and bank["grid"].shape == (n, LOOKBACK + WINDOW)
    symbols = [str(s) for s in bank["symbols"][bank["instrument"]]]
    assert set(symbols) == {"AAA", "BBB", "CCC", "DDD", "OLD", "SPY", "QQQ", "DIA", "GLD", "SLV", "SPLT", "RVS",
                            "REUS", "KMGX", "NEWC", "PRD", "CAR", "RENM", "RETD", "NXT", "CRN", "UNAD", "VMK", "DIVA",
                            "DIVB"}
    listing = {str(s): i for i, s in enumerate(bank["symbols"])}
    # The directory's delisting dates run late, so the note gives none.
    assert [str(bank[f][listing["OLD"]]) for f in ("names", "kinds", "exchanges", "sectors", "notes")] == [
        "Old Industries Inc.", "stock", "NYSE", "", "Since delisted"]
    assert [str(bank[f][listing["DIA"]]) for f in ("names", "kinds", "sectors", "exchanges")] == [
        "SPDR Dow Jones Industrial Average ETF Trust", "etf", "Index", "NYSE Arca"]
    assert [str(bank[f][listing["SPY"]]) for f in ("names", "sectors")] == ["SPDR S&P 500 ETF Trust", "Index"]
    assert [str(bank[f][listing["SLV"]]) for f in ("names", "sectors")] == ["iShares Silver Trust", "Commodities"]
    assert str(bank["names"][listing["AAA"]]) == "AAA Corp" and str(bank["sectors"][listing["AAA"]]) == "Technology"
    calendar_days = day_numbers(calendar)
    spans = {}
    for i, symbol in enumerate(symbols):
        start = int(np.searchsorted(calendar_days, bank["day"][i][0]))
        # 250 consecutive SPY sessions, the year of the first replay bar and all 199 closes before the window.
        assert (bank["day"][i] == calendar_days[start:start + WINDOW]).all() and start >= LOOKBACK
        assert bank["year"][i] == int(calendar[start + N_CONTEXT][:4])
        assert np.isfinite(bank["lookback"][i]).all()
        spans.setdefault(symbol, []).append((start, start + WINDOW - 1))
        if symbol == "OLD":
            assert str(np.datetime64(int(bank["day"][i][-1]), "D")) <= "2013-06-28"
    for windows in spans.values():
        windows.sort()
        assert all(a[1] < b[0] for a, b in zip(windows, windows[1:]))
    assert summary["etf_windows"] + summary["stock_windows"] == n and summary["etf_target"] == 30
    assert summary["etf_share"] == round(summary["etf_windows"] / n, 3) and summary["etf_windows"] >= 5
    assert sum(v["stock"] + v["etf"] for v in summary["by_year"].values()) == n
    assert set(summary["rejected_starts"]) <= set(CHECKS) | {"naming", "dither width", "residual grid"}
    assert summary["rejected_starts"]["naming"] > 0
    # The same seed builds the same bank.
    db, cache, _ = market
    assert build_bank(db, tmp_path / "again.npz", windows=200, seed=7, etf_cache=cache)["version"] == summary["version"]


def test_etfs_come_from_the_database_and_the_cache_only_fills_in(built):
    summary, bank, calendar = built
    gold = walk(calendar, 110.0, seed=21, volume=5e6)
    found = windows_of(bank, "GLD", calendar)
    assert found
    for _, start, _, _, i in found:
        # The database's GLD (from 110) wins over the cache's (from 120).
        assert bank["bars"][i][0][3] == pytest.approx(gold[start].close, rel=1e-6)
    assert "XYZE" not in set(bank["symbols"].tolist()) and "TQQQ" not in set(bank["symbols"].tolist())
    assert summary["etfs"]["DIA"] >= 1 and set(summary["etfs"]) <= {"SPY", "QQQ", "DIA", "GLD", "SLV"}


def test_split_records_turn_stored_prices_into_prices_as_traded(built):
    _, bank, calendar = built
    split_at = calendar.index("2012-01-03")
    found = windows_of(bank, "SPLT", calendar)
    assert found
    for first, start, last, _, i in found:
        # Stored near $3.30, SPLT traded near $13 before its 4-for-1 split: only then is it no penny stock.
        assert start + N_CONTEXT - 1 < split_at
        assert bank["basis"][i] == (4.0 if last < split_at else 1.0)
        # One cent as traded, in stored prices, day by day: a quarter of a cent before the split.
        days = np.arange(first, last + 1)
        assert bank["grid"][i] == pytest.approx(np.where(days < split_at, 0.0025, 0.01))
    # RVS, stored near $36, traded near $3.60 before a 1-for-10 reverse split: only windows after it are in, and a cent
    # as traded before the split is ten stored cents.
    reverse_at = calendar.index("2013-06-03")
    found = windows_of(bank, "RVS", calendar)
    assert found and all(start + N_CONTEXT - 1 >= reverse_at for _, start, _, _, _ in found)
    for first, _, last, _, i in found:
        assert bank["basis"][i] == 1.0
        assert bank["grid"][i] == pytest.approx(np.where(np.arange(first, last + 1) < reverse_at, 0.1, 0.01))
    # A split after the last stored day is not in the stored prices: AAA has no split before then.
    assert all(bank["basis"][i] == 1.0 for *_, i in windows_of(bank, "AAA", calendar))
    # UNAD's stored prices halve on the date of its 2-for-1 record, so they were never adjusted for it: the windows
    # before quote the stored prices as traded, and none crosses the jump.
    unad = windows_of(bank, "UNAD", calendar)
    assert unad and all(last < 800 and bank["basis"][i] == 1.0 for _, _, last, _, i in unad)
    symbols = set(bank["symbols"].tolist())
    # $50,000 as traded means bad split records; a split record that cannot be read leaves the prices unknown; cents
    # on a $3 stored price draw a staircase.
    assert not {"HUGE", "BADS", "STEP", "PNY"} & symbols


def test_a_reveal_quotes_the_price_as_traded_at_the_end_of_the_window(built, tmp_path):
    _, bank, calendar = built
    service = TradeTestService(tmp_path / "bank.npz", key=KEY)
    loaded = service.bank()
    for *_, i in windows_of(bank, "SPLT", calendar) + windows_of(bank, "AAA", calendar):
        rows, scale = window_rows(loaded, i, KEY)
        stored = float(bank["bars"][i][N_CONTEXT - 1][3])
        assert rows[N_CONTEXT - 1][3] == 100.0 and 100 / scale == pytest.approx(stored * bank["basis"][i])


def test_the_dither_moves_no_price_of_a_bank_window_by_more_than_the_report_says(built, tmp_path):
    # The report tells visitors the chart prices carry an offset of at most about 0.3% of the price.
    loaded = load_bank(tmp_path / "bank.npz")
    worst = 0.0
    for w in range(loaded.windows):
        bars, before = loaded.bars[w].astype(np.float64), loaded.closes_before[w].astype(np.float64)
        step, second = dither_steps(bars, before, loaded.grid[w])
        moved, shifted = tradetest.dither(bars, before, step, second, b"seed %d" % w)
        worst = max(worst, np.abs(moved[:, :4] / bars[:, :4] - 1).max(), np.abs(shifted / before - 1).max())
    assert 0 < worst <= tradetest_bank.MAX_DITHER == 0.003


def test_reused_tickers_and_spliced_histories_never_lend_a_name_to_another_companys_prices(built):
    _, bank, calendar = built
    names = bank["names"]
    reus = windows_of(bank, "REUS", calendar)
    assert {str(names[li]) for *_, li, _ in reus} == {"Old Reus Corp", "REUS Corp"}
    for first, _, last, li, _ in reus:
        # Before the splice at 620 the ticker was the older company's; no window crosses it.
        assert (str(names[li]), str(bank["notes"][li])) == (("Old Reus Corp", "Since delisted") if last < 620 else
                                                           ("REUS Corp", ""))
        assert last < 620 or first >= 620
    # KMGX's own listing began in the stretch before the first splice; the stretch after it is another company's.
    kmgx = windows_of(bank, "KMGX", calendar)
    assert kmgx and all(last < 460 for _, _, last, _, _ in kmgx)
    # NEWC's saved profile says it listed at 500: the ticker's earlier prices are not its own.
    newc = windows_of(bank, "NEWC", calendar)
    assert newc and all(first >= 500 for first, *_ in newc)
    # CAR shares PRD's series until 700 and then jumps: PRD carries the series on, so only PRD has windows there.
    car = windows_of(bank, "CAR", calendar)
    assert car and all(first >= 700 for first, *_ in car)
    assert any(first < 700 for first, *_ in windows_of(bank, "PRD", calendar))
    # RETD is RENM's retired ticker: a window of the stretch they share is named from RETD's directory record (the
    # company the chart was then), a later one RENM's, and each stretch of the series is in the bank once.
    retd, renm = windows_of(bank, "RETD", calendar), windows_of(bank, "RENM", calendar)
    assert retd and renm and all(last < 903 for *_, last, _, _ in retd) and all(last >= 903 for *_, last, _, _ in renm)
    assert {(str(names[li]), str(bank["notes"][li])) for *_, li, _ in retd} == {
        ("Retired Ticker Inc.", "Since delisted")}
    shown = sorted((start, last) for _, start, last, _, _ in retd + renm)
    assert all(a[1] < b[0] for a, b in zip(shown, shown[1:]))
    # CRN holds NXT's history adjusted another way until 700, where it keeps an unadjusted separation and goes on as
    # another company: the return probe finds the copy, so only NXT has windows of it.
    crn, nxt = windows_of(bank, "CRN", calendar), windows_of(bank, "NXT", calendar)
    assert crn and all(first >= 700 for first, *_ in crn)
    assert any(last < 700 and bank["basis"][i] == 2.5 for _, _, last, _, i in nxt)
    # VMK carries on EQX's series, but before its split at 1150 VMK's document doubles the prices as traded that EQX's
    # leaves alone: one of them is wrong, so no window that ends before it is in the bank, under either ticker.
    assert windows_of(bank, "EQX", calendar) == []
    assert all(last >= 1150 for *_, last, _, _ in windows_of(bank, "VMK", calendar))
    assert splice_points(repair(np.array([[b.open, b.high, b.low, b.close, b.volume] for b in
                                          walk(calendar[:620], 30.0, seed=27) + walk(calendar[620:], 90.0, seed=28,
                                                                                      volume=3e7)]))).tolist() == [620]


def test_the_return_probe_finds_a_copy_adjusted_another_way(market):
    db, cache, _ = market
    builder = BankBuilder(db, etf_cache=cache)
    try:
        pool = {c.symbol: c for c in builder.pool}
        crn, nxt = builder.history(pool["CRN"]).bars[:700, 3], builder.history(pool["NXT"]).bars[:700, 3]
        # The same series at another level: no close of NXT's is within half of CRN's, so only the returns match.
        assert np.abs(nxt * 2.5 / crn - 1).max() < 0.01 and (np.abs(crn / nxt - 1) > 0.5).all()
        foreign, inherited = builder.shares(pool["CRN"])
        assert inherited == [] and [(a, 690 <= b <= 730) for a, b in foreign] == [(0, True)]
        assert [(a, 690 <= b <= 730, partner) for a, b, partner in builder.shares(pool["NXT"])[1]] == [(0, True, "CRN")]
        # A copy in the same prices is found as well: RENM carries on RETD's series, PRD carries on CAR's.
        assert builder.shares(pool["RETD"])[1] == [] and [p for *_, p in builder.shares(pool["RENM"])[1]] == ["RETD"]
        assert [p for *_, p in builder.shares(pool["PRD"])[1]] == ["CAR"]
    finally:
        builder.close()


def test_stored_prices_adjusted_for_a_dividend_no_record_carries_are_not_quoted_before_it(built):
    _, bank, calendar = built
    # DIVA's stored prices run straight through its 40% special dividend at 500: adjusted for it, so the prices as
    # traded before it are unknown and no window reaches before it. DIVB's fall by its 30%: as traded already.
    diva, divb = windows_of(bank, "DIVA", calendar), windows_of(bank, "DIVB", calendar)
    assert diva and all(first >= 500 for first, *_ in diva)
    assert any(first < 500 for first, *_ in divb) and all(bank["basis"][i] == 1.0 for *_, i in diva + divb)


def test_a_dividend_the_stored_prices_do_not_fall_by_leaves_the_prices_before_it_unknown():
    days = day_numbers(sessions("2010-01-04", "2012-12-31"))
    closes, at = clean(len(days))[:, 3], np.arange(len(days))
    when, last, before = str(np.datetime64(int(days[300]), "D")), int(days[-1]), float(closes[299])
    # A special dividend of 40% the stored closes run straight through, and one worth five times the stored close
    # (Keurig Dr Pepper's $103.75 on a stored $19.79): adjusted for, so the prices before the date are unknown.
    assert (dividend_adjustments(days, {when: 0.4 * before}, last, closes) == (at < 300)).all()
    assert (dividend_adjustments(days, {when: 5.2 * before}, last, closes) == (at < 300)).all()
    # Stored closes that fall by it are as traded; a 5% dividend is too small to judge; one after the last stored day
    # is not in the prices yet.
    fell = np.where(at >= 300, closes * 0.6, closes)
    assert not dividend_adjustments(days, {when: 0.4 * before}, last, fell).any()
    assert not dividend_adjustments(days, {when: 0.05 * before}, last, closes).any()
    assert not dividend_adjustments(days, {when: 0.4 * before}, int(days[299]), closes).any()
    # A separation recorded as a split of about its size on its date is in the factor already (Danaher's Fortive,
    # 1319:1000); a reverse split on the same day is not it (Expedia's 1-for-2 with TripAdvisor).
    record = lambda numerator, denominator: [split("X", when, numerator, denominator)]
    assert not dividend_adjustments(days, {when: 0.3 * before}, last, closes, record(1319, 1000)).any()
    assert (dividend_adjustments(days, {when: 0.3 * before}, last, closes, record(1, 2)) == (at < 300)).all()
    # Junk in the documents is skipped.
    assert not dividend_adjustments(days, {"not a date": 3.0, when: None}, last, closes, [{}]).any()
    assert not dividend_adjustments(days, ["junk"], last, closes, "junk").any()
    # No span reaches before such a date: the closes before the window feed the averages.
    bars = repair(clean(len(days)))
    checks = window_checks(bars, "stock", ok_for(bars), None, None, adjusted=at < 300)
    assert checks["unrecorded adjustment"][LOOKBACK:].tolist() == [s - LOOKBACK >= 300
                                                                   for s in range(LOOKBACK, len(checks["bars"]))]


def test_the_hand_checked_lists_hold_the_known_bad_records_adjustments_and_names():
    bad, formed, predecessors = tradetest_bank.BAD_SPLITS, tradetest_bank.FORMED, tradetest_bank.PREDECESSORS
    # Split documents wrong throughout, or before a date (SSP's 2015 and 2025 records are both wrong).
    assert all(bad[symbol] is None for symbol in ("ENS", "MDP", "CPA", "WT", "WETF", "GRUB", "KRC", "SKM", "COL", "SIR",
                                                  "GOL", "BKD", "AET", "FMD"))
    cutoffs = ("J", "ES", "GSK", "WCN", "SSP", "DBRG", "MTCH", "AIV", "AXIA", "MT")
    assert {symbol: bad[symbol] for symbol in cutoffs} == {
        "J": "2017-02-15", "ES": "2013-04-30", "GSK": "2022-07-22", "WCN": "2016-06-01", "SSP": "2025-12-08",
        "DBRG": "2017-01-11", "MTCH": "2020-07-01", "AIV": "2011-06-09", "AXIA": "2025-12-30", "MT": "2014-10-02"}
    # Separations, special dividends and splits no record carries.
    assert tradetest_bank.UNRECORDED == {"KDP": "2018-07-09", "OVV": "2009-12-01", "AROC": "2015-11-04",
                                         "TWX": "2009-12-10", "MDLZ": "2012-10-02", "MMM": "2024-04-01",
                                         "BKR": "2017-07-05", "NVS": "2023-10-04", "OA": "2015-02-10",
                                         "SM": "2020-03-09"}
    # Companies formed after the start of their tickers' histories, and the companies the prices were before.
    assert {symbol: formed[symbol] for symbol in ("BKR", "DBRG", "AROC", "CNH", "IAC", "PPLI")} == {
        "BKR": "2017-07-03", "DBRG": "2017-01-10", "AROC": "2007-08-20", "CNH": "2013-09-29", "IAC": "2020-07-01",
        "PPLI": "2020-07-01"}
    # Old IAC's whole history, at the right prices, is PPLI's copy; MTCH's joins Match Group's own prices to part of it.
    assert predecessors["PPLI"] == ("2020-07-01", "IAC", "IAC/InterActiveCorp", "NASDAQ") and "MTCH" not in predecessors
    assert predecessors["CNH"] == (formed["CNH"], "CNH", "CNH Global N.V.", "NYSE")
    assert tradetest_bank.RENAMED == {"GRUB": "Grubhub Inc."}


def test_names_follow_the_company_whose_prices_a_window_shows(market, tmp_path, monkeypatch):
    db, cache, calendar = market
    # BBB's prices before 600 were a known older company's; OLD's directory record has the wrong name, and its company
    # (a directory stock) was formed at 300; RENM and its retired ticker RETD hold the history of a company formed at
    # 700, so neither name may go on the stretch before.
    monkeypatch.setattr(tradetest_bank, "PREDECESSORS", {"BBB": (calendar[600], "BBBQ", "Bee Predecessor Co.", "NYSE")})
    monkeypatch.setattr(tradetest_bank, "RENAMED", {"OLD": "Older Industries Inc."})
    monkeypatch.setattr(tradetest_bank, "FORMED", {"OLD": calendar[300], "RENM": calendar[700], "RETD": calendar[700]})
    build_bank(db, tmp_path / "bank.npz", windows=200, seed=7, etf_cache=cache)
    bank = read_bank(tmp_path / "bank.npz")
    named = lambda found: {tuple(str(bank[field][li]) for field in ("names", "exchanges", "notes"))
                           for *_, li, _ in found}
    # A window wholly before 600 is the older company's; one that spans 600 would show two companies.
    older, bbb = windows_of(bank, "BBBQ", calendar), windows_of(bank, "BBB", calendar)
    assert older and all(last < 600 for *_, last, _, _ in older)
    assert named(older) == {("Bee Predecessor Co.", "NYSE", "")}
    assert bbb and all(first >= 600 for first, *_ in bbb) and named(bbb) == {("BBB Corp", "NASDAQ", "")}
    old = windows_of(bank, "OLD", calendar)
    assert old and all(first >= 300 for first, *_ in old)
    assert named(old) == {("Older Industries Inc.", "NYSE", "Since delisted")}
    assert windows_of(bank, "RETD", calendar) == []
    renm = windows_of(bank, "RENM", calendar)
    assert renm and all(first >= 700 for first, *_ in renm) and named(renm) == {("RENM Corp", "NASDAQ", "")}


def test_known_bad_split_documents_and_formation_dates_keep_windows_out(market, tmp_path, monkeypatch):
    db, cache, calendar = market
    # CCC's split document is wrong throughout and DDD's before 700; AAA's stored prices before 650 carry an adjustment
    # no record explains. BBB's company was formed at 600, and RENM's at 900, after the stretch of history it shares
    # with its retired ticker RETD.
    monkeypatch.setattr(tradetest_bank, "BAD_SPLITS", {"CCC": None, "DDD": calendar[700]})
    monkeypatch.setattr(tradetest_bank, "UNRECORDED", {"AAA": calendar[650]})
    monkeypatch.setattr(tradetest_bank, "FORMED", {"BBB": calendar[600], "RENM": calendar[900]})
    summary = build_bank(db, tmp_path / "bank.npz", windows=200, seed=7, etf_cache=cache)
    bank = read_bank(tmp_path / "bank.npz")
    assert windows_of(bank, "CCC", calendar) == [] and summary["rejected_starts"]["split documents"] > 0
    ddd, bbb = windows_of(bank, "DDD", calendar), windows_of(bank, "BBB", calendar)
    assert ddd and all(first >= 700 for first, *_ in ddd) and bbb and all(first >= 600 for first, *_ in bbb)
    aaa = windows_of(bank, "AAA", calendar)
    assert aaa and all(first >= 650 for first, *_ in aaa) and summary["rejected_starts"]["unrecorded adjustment"] > 0
    # A company formed after the stretch cannot carry it on: the retired ticker keeps it, under its own record.
    retd = windows_of(bank, "RETD", calendar)
    assert retd and all(last < 903 for *_, last, _, _ in retd)
    assert {str(bank["names"][li]) for *_, li, _ in retd} == {"Retired Ticker Inc."}
    assert all(first >= 900 for first, *_ in windows_of(bank, "RENM", calendar))


def test_only_broad_index_etfs_keep_the_short_shocks_out_of_their_whole_window(market):
    db, cache, calendar = market
    assert [event_scope(s) for s in ("SPY", "QQQ", "DIA", "IWM", "MDY", "IJR", "VTI", "GLD", "XLE", "AAPL")] == \
        ["window"] * 7 + ["replay"] * 3
    builder = BankBuilder(db, etf_cache=cache)
    try:
        pool = {c.symbol: c for c in builder.pool}
        flash = int(np.searchsorted(day_numbers(calendar), day_numbers(["2010-05-06"])[0]))
        gold, index = builder.eligible(pool["GLD"]).tolist(), builder.eligible(pool["DIA"]).tolist()
        # The flash crash may sit in a gold ETF's context, as in a stock's, but never in its replay; an index ETF's
        # window never touches it.
        assert any(s <= flash < s + N_CONTEXT for s in gold)
        assert not any(s + N_CONTEXT <= flash < s + WINDOW for s in gold)
        assert index and not any(s <= flash < s + WINDOW for s in index)
    finally:
        builder.close()


def test_the_builder_needs_spy_and_works_without_the_etf_cache(market, tmp_path):
    db, _, _ = market
    summary = build_bank(db, tmp_path / "bank.npz", windows=12, seed=1, etf_cache=tmp_path / "missing.json")
    with np.load(tmp_path / "bank.npz", allow_pickle=False) as data:
        assert "SLV" not in set(data["symbols"].tolist()) and summary["windows"] == 12
    empty = Store(tmp_path / "empty.db")
    empty.close()
    with pytest.raises(ValueError, match="SPY has 0 sessions"):
        build_bank(tmp_path / "empty.db", tmp_path / "none.npz", windows=12)


def clean(sessions_=500, price=50.0, volume=1e6):
    """Steady bars that wobble by about 0.4% a day, off any price grid (three repeated prices would fit every fine
    grid, which real prices never do)."""
    bars = np.zeros((sessions_, 5))
    bars[:, :4] = (price * (1 + 0.004 * np.random.default_rng(0).standard_normal(sessions_)))[:, None]
    bars[:, 1] *= 1.01
    bars[:, 2] *= 0.99
    bars[:, 4] = volume
    return bars


def ok_for(bars):
    return np.ones(len(bars) - WINDOW + 1, dtype=bool)


def starts(bars, kind="stock", factor=None, **options):
    bars = repair(bars)
    return eligible_starts(bars, kind, ok_for(bars), factor, splice_points(bars), **options).tolist()


def test_eligibility_rejects_gaps_penny_stocks_thin_trading_flat_bars_and_glitches():
    # A window needs the 199 closes before it, so the first start is 199.
    every = list(range(LOOKBACK, 251))
    assert starts(clean()) == every
    missing = clean()
    missing[460] = np.nan
    assert starts(missing) == list(range(LOOKBACK, 211))
    early = clean()
    early[100] = np.nan                                            # a gap in the closes before every window
    assert starts(early) == []
    assert starts(clean(price=4.0)) == []                        # last context close under $5
    dip = clean(price=10.37, volume=2e6)                           # prices that linger at a round $10 pass for a grid
    dip[470:500, :4] = np.concatenate([np.linspace(10, 1.9, 21), np.linspace(2.2, 5, 9)])[:, None]
    dip[:, 1], dip[:, 2] = dip[:, 3] * 1.01, dip[:, 3] * 0.99
    assert starts(dip) == list(range(LOOKBACK, 241))               # a close under $2 at bar 490
    bad = clean()
    bad[300, 0] = 0.0
    assert starts(bad) == []
    assert starts(clean(volume=1e5)) == [] and starts(clean(volume=1e5), "etf") == every   # $5M a day
    jump = clean()
    jump[20:, :4] *= 1.7
    # The span's first close may gap from the day before; a move inside the window or the closes before it fails.
    assert starts(jump) == list(range(219, 251))
    quiet = clean()
    quiet[300:303, 4] = 0
    assert starts(quiet) == every
    quiet[303, 4] = np.nan                                         # a missing volume counts as none
    assert starts(quiet) == []
    # So does a placeholder: 100 shares on a stock that trades a million a day (BlackBerry's 2004 series).
    placeholder = clean()
    placeholder[300:303, 4] = 100
    assert starts(placeholder) == every
    placeholder[303, 4] = 100
    assert starts(placeholder) == []
    flat = clean()
    flat[300:306, :4] = 50.0
    assert starts(flat) == []
    # Repair widens a high or low that misses the open or close, rather than rejecting the bar. (A high of 40 on a $50
    # bar would widen the low to 40: a bad bar, below.)
    odd = clean()
    odd[350, 1] = odd[350, 3] * 0.95
    assert starts(odd) == every and repair(odd)[350, 1] == max(odd[350, 0], odd[350, 3])
    assert repair(odd)[350, 2] == odd[350, 3] * 0.95


def test_eligibility_judges_prices_as_traded_and_refuses_staircases_splices_and_junk_prints():
    # Stored at $4 (split-adjusted), traded at $8: not a penny stock. Records that cannot be read refuse everything.
    # (Dollar volume is the same either way: the stored volume is split-adjusted too.)
    assert starts(clean(price=4.0, volume=5e6)) == []
    assert starts(clean(price=4.0, volume=5e6), factor=np.full(500, 2.0)) == list(range(LOOKBACK, 251))
    assert starts(clean(), factor=np.full(500, np.nan)) == []
    # Above $20,000 as traded the split records are wrong, except where a stock really trades there.
    assert starts(clean(), factor=np.full(500, 1000.0)) == []
    assert starts(clean(), factor=np.full(500, 1000.0), max_last=np.inf) == list(range(LOOKBACK, 251))
    # Whole cents on a stock stored near $3 draw a staircase; near $5 they are fine.
    staircase = window_checks(repair(np.round(clean(price=3.0, volume=5e6), 2)), "stock", ok_for(clean()),
                              np.full(500, 4.0))
    assert not staircase["quantisation"][LOOKBACK:].any() and staircase["price"][LOOKBACK:].all()
    assert starts(np.round(clean(price=5.0, volume=5e6), 2), factor=np.full(500, 4.0)) == list(range(LOOKBACK, 251))
    # A one-day jump of 3x with 30x the volume is another security: no span crosses it (and the jump alone, a glitch).
    spliced = clean()
    spliced[400:, :4] *= 3
    spliced[400:, 4] *= 30
    bars = repair(spliced)
    assert splice_points(bars).tolist() == [400]
    checks = window_checks(bars, "stock", ok_for(bars), None, splice_points(bars))
    assert not checks["splice"][LOOKBACK:].any() and starts(spliced) == []
    assert splice_points(repair(np.where(np.arange(500)[:, None] >= 400, clean() * [3, 3, 3, 3, 1],
                                         clean()))).tolist() == []    # the same jump on steady volume is no splice
    # Closes under 5 cents as traded, even in the closes before the window, are a buyout's junk prints.
    junk = clean(price=1.0, volume=2e6)
    junk[:, :4] *= (0.03 * 1.03 ** np.arange(500)).clip(max=10.37)[:, None]
    cut = int(np.flatnonzero(junk[:, 3] < 0.05).max())
    assert starts(junk) == list(range(max(LOOKBACK, cut + LOOKBACK + 1), 251))


def test_split_records_the_stored_prices_contradict_or_leave_unexplained_are_not_trusted():
    days = day_numbers(sessions("2010-01-04", "2012-12-31"))
    closes, at = clean(len(days))[:, 3], np.arange(len(days))
    record = lambda numerator, denominator: [split("X", str(np.datetime64(int(days[300]), "D")), numerator, denominator)]
    # Stored prices that run smoothly through a 2-for-1 record are adjusted for it: twice the stored price before.
    factor, jumps = split_factors(days, record(2, 1), int(days[-1]), closes)
    assert (factor == np.where(at < 300, 2.0, 1.0)).all() and jumps.tolist() == []
    # Stored prices that halve on its date were never adjusted for it: they are as traded already.
    halved = np.where(at >= 300, closes / 2, closes)
    factor, jumps = split_factors(days, record(2, 1), int(days[-1]), halved)
    assert (factor == 1).all() and jumps.tolist() == [300]
    # So were stored prices that halve a session or three off its date (South Jersey Industries' on 2015-05-08, a
    # session before its 2-for-1 record); four sessions off, or a 10% stock dividend's 10% move two sessions off, is
    # too far from it or too small to tell from a move, so the record holds.
    for offset in (-1, 3):
        early = np.where(at >= 300 + offset, closes / 2, closes)
        factor, jumps = split_factors(days, record(2, 1), int(days[-1]), early)
        assert (factor == 1).all() and jumps.tolist() == [300 + offset]
    far = np.where(at >= 304, closes / 2, closes)
    assert (split_factors(days, record(2, 1), int(days[-1]), far)[0] == np.where(at < 300, 2.0, 1.0)).all()
    dividend = np.where(at >= 302, closes / 1.1, closes)
    assert (split_factors(days, record(11, 10), int(days[-1]), dividend)[0] == np.where(at < 300, 1.1, 1.0)).all()
    # A 1-for-3 record where the stored prices rise half as much again (a reverse split with spin-offs, adjusted for
    # some of it): the prices as traded before it are unknown.
    risen = np.where(at >= 300, closes * 1.5, closes)
    factor, jumps = split_factors(days, record(1, 3), int(days[-1]), risen)
    assert np.isnan(factor[:300]).all() and (factor[300:] == 1).all() and jumps.tolist() == [300]
    # A 21-for-20 stock dividend is too small to judge from a move of its size; without the stored prices nothing is
    # judged; a record after the last stored day is not in the prices at all.
    dipped = np.where(at >= 300, closes / 1.05, closes)
    assert (split_factors(days, record(21, 20), int(days[-1]), dipped)[0][:300] == 1.05).all()
    assert (split_factors(days, record(2, 1), int(days[-1]))[0] == np.where(at < 300, 2.0, 1.0)).all()
    assert (split_factors(days, record(2, 1), int(days[299]), halved)[0] == 1).all()
    # No span crosses a jump: the closes before the window included, but not the move into its first close.
    bars = repair(clean(len(days)))
    checks = window_checks(bars, "stock", ok_for(bars), None, None, breaks=np.array([300]))
    assert checks["unadjusted split"][LOOKBACK:].tolist() == [s >= 300 + LOOKBACK
                                                              for s in range(LOOKBACK, len(checks["bars"]))]


def test_unrecorded_splits_and_bad_prints_are_found_in_the_prices():
    every = list(range(LOOKBACK, 251))
    # A 2-for-1 split the stored prices never adjusted for: the bar opens at half the close before, trades a calm range
    # and holds the new level. No span may cross it. (Off whole dollars: two levels near $50 and $25 would pass for a
    # dollar grid.)
    halved = clean(price=50.37)
    halved[460:, :4] /= 2
    assert unrecorded_splits(repair(halved)).nonzero()[0].tolist() == [460]
    assert starts(halved) == list(range(LOOKBACK, 211))
    # The same gap on a bar that ranges 12%, or a fall of 40% (no split ratio), is a move: it stays.
    wide, fall = halved.copy(), clean(price=50.37)
    wide[460, 1] *= 1.1
    fall[460:, :4] *= 0.6
    assert not unrecorded_splits(repair(wide)).any() and not unrecorded_splits(repair(fall)).any()
    assert starts(wide) == every and starts(fall) == every
    # A close 30% off that the next bar opens back from and undoes, on ordinary volume: a bad print.
    bad = clean()
    bad[460, 3] *= 0.7
    assert bad_prints(repair(bad)).nonzero()[0].tolist() == [460] and starts(bad) == list(range(LOOKBACK, 211))
    # The same spike on eight times the volume, or one the next bar opens at and takes the session to undo, is news.
    news, slow = bad.copy(), bad.copy()
    news[460, 4] *= 8
    slow[461, 0] = slow[460, 3]
    assert not bad_prints(repair(news)).any() and not bad_prints(repair(slow)).any()
    assert starts(news) == every and starts(slow) == every
    # A close 18% off that the next bar opens back from, on a bar that opened flat and closed at its low (Xilinx's 22.29
    # between 27.06 and 27.35), is a bad print too. One 12% off is a move, and so is an 18% swing on a bar that gapped
    # at the open (a crash day's: JPMorgan opened 9% up on 2020-03-13 and closed 18% up).
    dip, drop, swing = clean(), clean(), clean()
    dip[460, 3] *= 0.82
    drop[460, 3] *= 0.88
    swing[460, 3] *= 0.82
    swing[460, 0] *= 0.91
    assert bad_prints(repair(dip)).nonzero()[0].tolist() == [460]
    assert not bad_prints(repair(drop)).any() and not bad_prints(repair(swing)).any()
    # A calm opening gap of 40% (the fall above) on a sixth of the usual volume is a share conversion the stored
    # prices never adjusted for (Precision Drilling's in 2005): no span may cross it. News moves that far on volume.
    converted = fall.copy()
    converted[460, 4] /= 6
    assert unrecorded_splits(repair(converted)).nonzero()[0].tolist() == [460]
    assert starts(converted) == list(range(LOOKBACK, 211))


def test_bad_opens_highs_and_lows_on_calm_days_keep_a_window_out():
    every = list(range(LOOKBACK, 251))
    # A low 25% under the bar while the close and the next open stay put, on ordinary volume (Whirlpool's 72.10 under a
    # 95 bar): a stop there would fill at a price that never traded, so no window that shows it is kept.
    wick = clean()
    wick[460, 2] = wick[460, 3] * 0.75
    assert bad_bars(repair(wick)).nonzero()[0].tolist() == [460] and starts(wick) == list(range(LOOKBACK, 211))
    # The chart shows only the closes of the bars before the window.
    early = clean()
    early[100, 2] = early[100, 3] * 0.75
    assert bad_bars(repair(early)).nonzero()[0].tolist() == [100] and starts(early) == every
    # An open 15% under the close before that is the bar's low, the close back where it was (Penn's 13.34 after
    # 14.97), and one 79% over it that is the high (Penske's 23.58 after 13.17).
    low_open, high_open = clean(), clean()
    low_open[460, 0] = low_open[460, 2] = low_open[459, 3] * 0.85
    high_open[460, 0] = high_open[460, 1] = high_open[459, 3] * 1.79
    assert [bad_bars(repair(bars)).nonzero()[0].tolist() for bars in (low_open, high_open)] == [[460], [460]]
    assert starts(low_open) == starts(high_open) == list(range(LOOKBACK, 211))
    # Real moves stay: the wick on 2.5 times the volume (news), with the next bar opening 6% away, or on a day of real
    # wild prints such as the flash crash; a 15% gap the close holds; a 9% wick.
    news, onward, flash = wick.copy(), wick.copy(), wick.copy()
    news[460, 4] *= 2.5
    onward[461, 0] = onward[460, 3] * 1.06
    held, small = clean(), clean()
    held[460:, :4] *= 0.85
    small[460, 2] = small[460, 3] * 0.91
    wild = np.zeros(len(flash), dtype=bool)
    wild[460] = True
    assert not any(bad_bars(repair(bars)).any() for bars in (news, onward, held, small))
    assert not bad_bars(repair(flash), wild).any() and starts(flash, wild=wild) == every
    assert starts(news) == starts(onward) == starts(held) == starts(small) == every


def test_prices_as_traded_off_whole_cents_mean_a_split_the_records_miss():
    every = list(range(LOOKBACK, 251))
    traded = np.round(clean(), 2)                                  # prices as traded: whole cents
    times = lambda bars, k: np.concatenate([bars[:, :4] * k, bars[:, 4:]], axis=1)   # the prices, not the volume
    assert starts(traded) == every
    # Stored at a half, a third or a quarter of the prices as traded, at full precision, with no record of the split
    # (Celgene's 2-for-1 of 2014): the prices quoted would be that much too low. With the record they are right.
    for ratio in (2, 3, 4):
        stored = times(traded, 1 / ratio)
        assert not window_checks(repair(stored), "stock", ok_for(stored), None)["cent grid"][LOOKBACK:].any()
        assert starts(stored) == [] and starts(stored, factor=np.full(500, float(ratio))) == every
    # Stored at twice the prices as traded (on even cents: a split that never happened, or a reverse split the records
    # miss), or at five times them under records that say ten (DBV Technologies' depositary shares in 2016, quoted at
    # half their price): refused. With the record of the reverse split, right.
    assert starts(times(traded, 2)) == [] and starts(times(traded, 5), factor=np.full(500, 0.1)) == []
    assert starts(times(traded, 2), factor=np.full(500, 0.5)) == every
    # The database rounds the prices it adjusts to cents, so stored cents times a factor of 1.5, 1.75, 2.25 or 2.5 sit
    # on fractions of a cent as traded (Brown-Forman's $96.90 in 2015, stored at $38.76 under 2.5): no missed split.
    for ratio in (1.5, 1.75, 2.25, 2.5):
        assert starts(np.round(times(traded, 1 / ratio), 2), factor=np.full(500, ratio)) == every
    # Nor is a factor a stock dividend leaves a little off a whole number (Aimco's 7.5011, four times which is 30.0044),
    # whose stored cents land near cents by chance over a narrow range of prices.
    assert starts(np.round(clean(price=6.8, volume=2e6), 2), factor=np.full(500, 7.5011)) == every
    # The same goes day by day, across a 2-for-1 split inside the window (twice stored cents before it, even cents);
    # and shares quoted in whole dollars at $1,000 or more sit on nickels too, so their even cents are no split that
    # never happened (Berkshire's B shares near $3,000 before 2010, stored at a fiftieth of that). Dimes at $500 are a
    # 1-for-10 reverse split the records miss.
    assert starts(traded, factor=np.where(np.arange(500) < 440, 2.0, 1.0)) == every
    assert starts(times(np.round(clean(price=3000.0)), 1 / 50), factor=np.full(500, 50.0)) == every
    assert starts(times(traded, 10)) == []
    # First Marblehead's stored prices are ten times its prices as traded (a later 1-for-10 that no record carries)
    # and two thirds of that before a 3-for-2 of 2006 that none carries either: across that split the whole window
    # sits on cents often enough, but its replay sits on dimes.
    marblehead = np.where(np.arange(500)[:, None] < 360, times(traded, 20 / 3), times(traded, 10))
    assert not window_checks(repair(marblehead), "stock", ok_for(traded), None)["cent grid"][LOOKBACK:].any()
    # Prices on no grid at all (adjusted for dividends) say nothing either way; a split the records carry inside the
    # window leaves each day's prices as traded on whole cents.
    assert starts(clean()) == every and starts(times(clean(), 0.5)) == every
    inside = traded.copy()
    inside[:300, :4] = np.round(clean()[:300, :4] * 2, 2) / 2      # traded at twice this before a 2-for-1 at 300
    assert starts(inside, factor=np.where(np.arange(500) < 300, 2.0, 1.0)) == every


def test_the_builder_judges_the_cent_grid_on_the_stored_prices_at_full_precision(tmp_path):
    calendar = sessions("2009-06-01", "2012-12-31")
    store = Store(tmp_path / "market.db")
    write = lambda symbol, bars: store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date,
                                                    checked_at=NOW, status="ok")
    write("SPY", walk(calendar, 130.0, seed=100, volume=1e8))
    # Whole cents as traded; half of them, as a 2-for-1 split the database adjusted for and no record carries leaves
    # them; and cents the database rounded after adjusting them for the 3-for-2 split on ROUND's record.
    for symbol, seed in (("CENT", 50), ("HALF", 51), ("ROUND", 52)):
        bars = in_cents(walk(calendar, seed=seed))
        write(symbol, scaled(bars, 0.5) if symbol == "HALF" else bars)
    store.replace_universe([listed(s, f"{s} Corp") for s in ("CENT", "HALF", "ROUND")])
    store.save_document("backtest:splits:ROUND", {"fetched_on": "2013-01-02",
                                                   "rows": [split("ROUND", calendar[900], 3, 2)]})
    store.close()
    builder = BankBuilder(tmp_path / "market.db")
    try:
        pool = {c.symbol: c for c in builder.pool}
        cent, half, rounded = (builder.eligible(pool[s]) for s in ("CENT", "HALF", "ROUND"))
        assert cent.size and not half.size and builder.rejected["cent grid"] > 0
        assert builder.history(pool["ROUND"]).factor[0] == 1.5 and any(s + WINDOW <= 900 for s in rounded)
    finally:
        builder.close()


def test_a_price_grid_is_found_the_way_the_attack_would_find_it():
    rng = np.random.default_rng(5)
    cents = rng.integers(300, 900, 2000) / 100
    assert lattice_strength(cents, 0.01) == pytest.approx(1.0) and lattice_strength(cents, 0.05) < 0.1
    # Cents, nickels (a grid of the grids below it), cents with a few odd prints, and prices on no grid at all.
    assert price_grid(cents) == 0.01 and price_grid(cents * 5) == 0.05
    assert price_grid(np.concatenate([cents[:1880], cents[1880:] + 0.0037])) == 0.01
    assert price_grid(rng.uniform(3, 9, 2000)) == 0.0
    # A grid coarser than a cent needs prices that span 20 of its steps, or lingering at one price would pass for a
    # grid; a cent grid always counts.
    assert price_grid(rng.integers(100, 118, 2000) * 0.05) == 0.01
    assert price_grid(rng.integers(100, 140, 2000) * 0.05) == 0.05
    assert price_grid(rng.integers(500, 520, 2000) / 100) == 0.01
    # Highs and lows that gather at whole dollars on a $700 stock make a grid the attack can use, so it counts.
    dollars = rng.integers(60000, 90000, 2000) / 100
    dollars[::3] = np.round(dollars[::3])
    assert price_grid(dollars) == 1.0 and price_grid(rng.integers(60000, 90000, 2000) / 100) == 0.01


def test_windows_never_touch_the_recognisable_market_events():
    calendar = sessions("2007-06-01", "2026-09-30")
    days = day_numbers(calendar)
    stock, etf = calendar_ok(days, "replay"), calendar_ok(days, "window")
    assert len(stock) == len(calendar) - WINDOW + 1 and 0 < etf.sum() < stock.sum() < len(stock)
    overlaps = lambda a, b, periods: any(a <= last and b >= first for first, last in periods)
    for s in range(len(stock)):
        first, replay, last = calendar[s], calendar[s + N_CONTEXT], calendar[s + WINDOW - 1]
        whole = last < FRESH and not overlaps(first, last, CRASHES)
        # The short shocks keep a stock's (or most ETFs') window out only from its replay; an index ETF's, from all.
        assert stock[s] == (whole and not overlaps(replay, last, SHOCKS))
        assert etf[s] == (whole and not overlaps(first, last, SHOCKS))


# A bank written directly ------------------------------------------------------------------------------------------

def fake_bank(path, *, windows=80, instruments=40, years=20, seed=1, step=None, traded=None, lots=False):
    """Random-walk windows on business days, two per instrument, replays starting in twenty different years. With a
    ``step`` every stored price is a whole number of it (cents, as most stored prices are, or nickels). With
    ``traded``, a split factor, the prices as traded are whole cents while the stored ones (traded / factor) keep every
    decimal, as a split-adjusted series can. With ``lots``, volumes are thin and in round lots of 100 shares."""
    rng = np.random.default_rng(seed)
    closes = 40 * np.exp(np.cumsum(rng.normal(0, 0.015, (windows, WINDOW)), axis=1))
    bars = np.zeros((windows, WINDOW, 5))
    bars[:, :, 3] = closes
    bars[:, 1:, 0] = closes[:, :-1]
    bars[:, 0, 0] = closes[:, 0]
    bars[:, :, 1] = bars[:, :, [0, 3]].max(axis=2) * 1.005
    bars[:, :, 2] = bars[:, :, [0, 3]].min(axis=2) * 0.995
    bars[:, :, 4] = (rng.integers(50, 251, (windows, WINDOW)) * 100.0 if lots else
                     rng.uniform(5e5, 2e6, (windows, WINDOW)))
    bars[0, :5, 4] = 0                         # zero volumes are left out of the typical volume
    lookback = 40 * np.exp(np.cumsum(rng.normal(0, 0.015, (windows, LOOKBACK)), axis=1))
    if step:
        bars[:, :, :4], lookback = np.round(bars[:, :, :4] / step) * step, np.round(lookback / step) * step
    if traded:
        bars[:, :, :4] = np.round(bars[:, :, :4] * traded, 2) / traded
        lookback = np.round(lookback * traded, 2) / traded
    lookback[0] = np.nan                       # no history before the first window (an older bank)
    lookback[1, :120] = np.nan                 # 79 closes: enough for SMA 50, not for SMA 200
    day = np.zeros((windows, WINDOW), dtype=np.int32)
    for w in range(windows):
        first = np.busday_offset(np.datetime64(f"{2003 + w % years}-06-01"), 0, roll="forward") - np.timedelta64(200, "D")
        day[w] = np.busday_offset(first, np.arange(WINDOW), roll="forward").astype(np.int64)
    arrays = {"bars": bars.astype(np.float32), "lookback": lookback.astype(np.float32), "day": day,
              "instrument": (np.arange(windows) % instruments).astype(np.int32),
              "year": (day[:, N_CONTEXT].astype("datetime64[D]").astype("datetime64[Y]").astype(int) + 1970).astype(np.int16),
              "basis": np.where(np.arange(windows) == 2, 4.0, 1.0),   # window 2 had a 4-for-1 split after it
              "grid": np.where(np.arange(windows)[:, None] == 2, 0.0025, np.full((windows, LOOKBACK + WINDOW),
                                                                                  0.01)).astype(np.float32)}
    if traded:
        arrays["basis"] = np.full(windows, float(traded))
        arrays["grid"] = np.full((windows, LOOKBACK + WINDOW), 0.01 / traded, dtype=np.float32)
    arrays["symbols"] = np.array([f"S{i:02d}" for i in range(instruments)])
    arrays["names"] = np.array([f"Company {i}" for i in range(instruments)])
    arrays["kinds"] = np.array(["etf" if i < 4 else "stock" for i in range(instruments)])
    arrays["sectors"] = np.array(["Technology"] * instruments)
    arrays["industries"] = np.array(["Software"] * instruments)
    arrays["exchanges"] = np.array(["NYSE"] * instruments)
    arrays["notes"] = np.array(["Delisted 2015-03-02" if i == 5 else "" for i in range(instruments)])
    meta = {"version": bank_version(arrays), "n_context": 150, "n_replay": 100, "lookback": 199,
            "built": "2026-10-08", "windows": windows, "data_through": "2026-10-05"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        np.savez_compressed(handle, meta=np.array(json.dumps(meta)), **arrays)
    return arrays, meta


@pytest.fixture
def bank(tmp_path):
    arrays, meta = fake_bank(tmp_path / "bank.npz")
    return TradeTestService(tmp_path / "bank.npz", key=KEY), arrays, meta


def cursor_of(service, chart=0):
    return service.new_set()["charts"][chart]["cursor"]


def test_sealed_tokens_round_trip_and_refuse_tampering_other_keys_and_junk():
    sealer = Sealer(b"one")
    token = sealer.seal({"v": "abc", "k": 3})
    assert sealer.open(token) == {"v": "abc", "k": 3}
    assert sealer.seal({"k": 3}) != sealer.seal({"k": 3})                     # a fresh nonce each time
    assert "abc" not in token
    middle = len(token) // 2
    forged = token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1:]
    for junk in (forged, "", "not a token!", "AAAA", "A" * 601, None, 12, token + "=="):
        with pytest.raises(TradeTestError) as caught:
            sealer.open(junk)
        assert (caught.value.code, caught.value.status) == ("bad_token", 400)
    with pytest.raises(TradeTestError) as caught:
        Sealer(b"two").open(token)
    assert (caught.value.code, caught.value.status) == ("expired", 410)


def test_cursors_from_another_bank_version_have_expired(bank, tmp_path):
    service, _, _ = bank
    fake_bank(tmp_path / "other" / "bank.npz", seed=2)
    other = TradeTestService(tmp_path / "other" / "bank.npz", key=KEY)
    with pytest.raises(TradeTestError) as caught:
        other.bars(cursor_of(service))
    assert (caught.value.code, caught.value.status) == ("expired", 410)
    # A well-sealed payload must still name a real chart.
    service.bank()
    version = service.bank().version
    for payload in ({"v": version, "s": "0a1b2c3d", "i": 0, "w": 80, "k": 0},
                    {"v": version, "s": "0a1b2c3d", "i": 10, "w": 1, "k": 0},
                    {"v": version, "s": "0a1b2c3d", "i": 0, "w": 1, "k": 101},
                    {"v": version, "s": "XYZ", "i": 0, "w": 1, "k": 0},
                    {"v": version, "s": "0a1b2c3d", "i": 0, "w": True, "k": 0}):
        with pytest.raises(TradeTestError, match="not valid"):
            service.bars(service._sealer.seal(payload))
    with pytest.raises(TradeTestError) as caught:
        service.bars(service._sealer.seal({"v": "000000000000", "s": "0a1b2c3d", "i": 0, "w": 1, "k": 0}))
    assert caught.value.code == "expired"


def test_a_set_is_ten_distinct_instruments_with_at_most_two_per_year_and_skips_recent_charts(bank):
    service, arrays, _ = bank
    avoid, seen = None, []
    for _ in range(4):
        made = service.new_set(avoid)
        assert len(made["charts"]) == 10 and len(made["set"]) == 8 and made["n_context"] == 150
        windows = [service._cursor(c["cursor"])["w"] for c in made["charts"]]
        assert [service._cursor(c["cursor"])["i"] for c in made["charts"]] == list(range(10))
        assert all(service._cursor(c["cursor"])["k"] == 0 for c in made["charts"])
        assert len({int(arrays["instrument"][w]) for w in windows}) == 10
        years = [int(arrays["year"][w]) for w in windows]
        assert max(years.count(y) for y in years) <= 2
        # 80 windows: four sets fit before fewer than 50 are unseen.
        assert not set(windows) & set(seen)
        seen += windows
        avoid = made["avoid"]
        assert service._sealer.open(avoid, limit=MAX_AVOID)["ids"] == seen
    assert len(service.new_set(avoid)["charts"]) == 10                         # then the whole bank again
    # A stale or forged list is ignored; a list that is not text is refused.
    assert len(service.new_set("junk")["charts"]) == 10
    with pytest.raises(TradeTestError) as caught:
        service.new_set(["x"])
    assert caught.value.code == "bad_request"


def test_a_small_bank_relaxes_the_year_cap_before_repeating_an_instrument(tmp_path):
    arrays, _ = fake_bank(tmp_path / "bank.npz", windows=12, instruments=12, years=3)
    service = TradeTestService(tmp_path / "bank.npz", key=KEY)
    windows = [service._cursor(c["cursor"])["w"] for c in service.new_set()["charts"]]
    years = [int(arrays["year"][w]) for w in windows]
    assert len(set(windows)) == 10 and max(years.count(y) for y in years) > 2


def test_rows_are_rescaled_to_100_with_relative_volume_and_averages_over_the_lookback(bank):
    service, arrays, _ = bank
    bank_ = service.bank()
    for w in (0, 1, 2):
        rows, scale = window_rows(bank_, w)
        real = arrays["bars"][w].astype(np.float64)
        chart = 100 / real[N_CONTEXT - 1, 3]
        assert len(rows) == WINDOW and all(len(row) == 9 for row in rows)
        # The reveal's scale turns a chart price into the price as traded at the end of the window (window 2 split).
        assert rows[N_CONTEXT - 1][3] == 100.0 and scale == pytest.approx(chart / arrays["basis"][w])
        assert rows[10][:4] == [round(v * chart, 4) for v in real[10, :4].tolist()]
        context = real[:N_CONTEXT, 4]
        assert rows[20][4] == round(real[20, 4] / np.median(context[context > 0]), 3)
        closes = np.concatenate([arrays["lookback"][w].astype(np.float64), real[:, 3]])
        assert rows[100][7] == pytest.approx(closes[LOOKBACK + 51:LOOKBACK + 101].mean() * chart, abs=1e-4)
        assert not any(isinstance(v, float) and np.isnan(v) for row in rows for v in row)
    rows, _ = window_rows(bank_, 0)
    # No history before the first window: SMA 50 starts at its 50th bar, SMA 200 at its 200th; the EMAs at once.
    assert rows[48][7] is None and rows[49][7] is not None and rows[198][8] is None and rows[199][8] is not None
    assert rows[0][5] == rows[0][3] and rows[0][6] == rows[0][3]
    alpha, ema = 2 / 10, None
    for close in arrays["bars"][0][:, 3].astype(np.float64).tolist()[:30]:
        ema = close if ema is None else ema + alpha * (close - ema)
    assert rows[29][5] == pytest.approx(ema * 100 / float(arrays["bars"][0][N_CONTEXT - 1, 3]), abs=1e-4)
    rows, _ = window_rows(bank_, 1)
    assert rows[0][7] is not None and rows[0][8] is None and rows[120][8] is not None
    assert window_rows(bank_, 2)[0][0][8] is not None


def recovered_cents(rows, true_cents):
    """The attack on a rescaled cent grid: the real last close P (in cents) makes every chart price x times P/100 a
    whole number, so cos(2 pi x m / 100) averages near 1 at m = P and its multiples. The smallest such m from P/2 up."""
    x = np.unique(np.array([row[:4] for row in rows[:N_CONTEXT]], dtype=np.float64).ravel())
    candidates = np.arange(true_cents // 2, true_cents * 2 + 1, dtype=np.float64)
    score = np.concatenate([np.cos(2 * np.pi * np.outer(chunk, x) / 100).mean(axis=1)
                            for chunk in np.array_split(candidates, 20)])
    return int(candidates[np.flatnonzero(score > score.max() - 0.02)[0]])


def test_dithered_rows_are_stable_keep_the_100_close_and_hide_the_price_grid(tmp_path):
    arrays, _ = fake_bank(tmp_path / "bank.npz", step=0.01)
    bank_ = load_bank(tmp_path / "bank.npz")
    assert price_grid(arrays["bars"][3][:, :4]) == 0.01
    for w in (2, 3, 4):
        plain, scale = window_rows(bank_, w)
        rows, same_scale = window_rows(bank_, w, KEY)
        # The same on every request, unpredictable without the key, and the reveal's scale is the undithered one.
        assert rows == window_rows(bank_, w, KEY)[0] and rows != window_rows(bank_, w, b"other key")[0]
        assert rows != plain and same_scale == scale and rows[N_CONTEXT - 1][3] == 100.0
        real = arrays["bars"][w].astype(np.float64)
        last = real[N_CONTEXT - 1, 3]
        moved = np.array([row[:4] for row in rows]) * last / 100 - real[:, :4]
        # Each price within half a grid step of the stored one (half a cent, plus half the as-traded cent where that
        # is another grid, as in window 2), and so is the last close the chart is scaled by; highs and lows still
        # hold the open and close.
        half = 0.005 + (0.00125 if w == 2 else 0.0)
        assert (np.abs(moved) <= half * (1 + real[:, :4] / last) + 1e-4).all() and (np.abs(moved) > 1e-4).mean() > 0.9
        assert all(row[1] >= max(row[0], row[3]) and row[2] <= min(row[0], row[3]) for row in rows)
        # The averages follow the dithered closes.
        assert rows[100][7] == pytest.approx(np.mean([row[3] for row in rows[51:101]]), abs=1e-3)
    hits = {"plain": 0, "dithered": 0}
    for w in range(10):
        true = round(float(arrays["bars"][w][N_CONTEXT - 1, 3]) * 100)
        hits["plain"] += recovered_cents(window_rows(bank_, w)[0], true) == true
        hits["dithered"] += recovered_cents(window_rows(bank_, w, KEY)[0], true) == true
    assert hits["plain"] >= 8 and hits["dithered"] == 0


def residual(rows, period, last):
    """How firmly the chart prices still sit on a grid of ``period`` in stored prices (last: the stored last close)."""
    return lattice_strength(np.array([row[:4] for row in rows]).ravel(), period * 100 / last)


def test_the_dither_clears_the_grid_of_the_prices_as_traded_and_coarser_grids(tmp_path):
    # Stored prices that keep every decimal while the prices as traded (x 1.1647, a split and a spin-off) are whole
    # cents: the stored prices show no grid, yet the attack finds the traded one unless it is cleared as well.
    arrays, _ = fake_bank(tmp_path / "bank.npz", traded=1.1647)
    bank_ = load_bank(tmp_path / "bank.npz")
    assert price_grid(arrays["bars"][3][:, :4]) == 0.0 and price_grid(arrays["bars"][3][:, :4] * 1.1647) == 0.01
    hits = {"plain": 0, "dithered": 0}
    for w in range(10):
        true = round(float(arrays["bars"][w][N_CONTEXT - 1, 3]) * 1.1647 * 100)
        hits["plain"] += recovered_cents(window_rows(bank_, w)[0], true) == true
        hits["dithered"] += recovered_cents(window_rows(bank_, w, KEY)[0], true) == true
    assert hits["plain"] >= 8 and hits["dithered"] == 0
    # Stored prices in nickels: a cent of dither would leave the nickel grid standing.
    arrays, _ = fake_bank(tmp_path / "nickels.npz", step=0.05)
    bank_ = load_bank(tmp_path / "nickels.npz")
    for w in range(2, 6):
        last = float(arrays["bars"][w][N_CONTEXT - 1, 3])
        assert residual(window_rows(bank_, w)[0], 0.05, last) > 0.9
        assert residual(window_rows(bank_, w, KEY)[0], 0.05, last) < 0.1


def test_a_grid_that_drifts_with_dividend_adjustments_is_cleared_too():
    # Prices as traded in nickels, stored 0.6% lower after each quarterly dividend: no single grid lines up across the
    # window, but each stretch keeps one, so the dither spans the nickel and not just the cent.
    rng = np.random.default_rng(7)
    closes = np.round(80 * np.exp(np.cumsum(rng.normal(0, 0.012, WINDOW + LOOKBACK))) / 0.05) * 0.05
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.05 * rng.integers(0, 8, len(closes))
    lows = np.minimum(opens, closes) - 0.05 * rng.integers(0, 8, len(closes))
    stored = np.stack([opens, highs, lows, closes], axis=1) / (1 + 0.006 * (np.arange(len(closes)) // 63))[:, None]
    bars = np.zeros((WINDOW, 5))
    bars[:, :4] = stored[LOOKBACK:]
    before = stored[:LOOKBACK, 3]
    assert lattice_strength(np.concatenate([before, bars[:, :4].ravel()]), 0.05) < 0.1
    step, second = dither_steps(bars, before, np.full(WINDOW + LOOKBACK, 0.01))
    assert second == pytest.approx(np.full(WINDOW + LOOKBACK, 0.05))


def test_the_grid_attack_on_every_period_finds_a_grid_nobody_listed():
    # Prices as traded in cents, stored scaled by 3 (a 1-for-3 reverse split the split records miss) or by 93.13 (a
    # share conversion): a cent of dither leaves their grid of 3 or 93.13 cents, and the check finds it.
    rng = np.random.default_rng(9)
    traded = rng.integers(1500, 2500, 1200) / 100
    for scale, period in ((3, 0.03), (93.13, 0.9313)):
        stored = np.round(traded * scale, 2)
        strength, found = residual_grid(stored + rng.uniform(-0.005, 0.005, stored.size))
        assert strength > 0.5 and any(found == pytest.approx(period / k, rel=0.02) for k in (1, 2))
    # Prices on no grid, and whole cents under a cent of dither, show nothing.
    assert residual_grid(rng.uniform(15, 25, 1200))[0] < 0.2
    assert residual_grid(traded + rng.uniform(-0.005, 0.005, traded.size))[0] < 0.2


def test_the_service_dithers_with_its_own_key(tmp_path):
    fake_bank(tmp_path / "bank.npz", step=0.01)
    one, two = (TradeTestService(tmp_path / "bank.npz", key=key) for key in (KEY, KEY))
    other = TradeTestService(tmp_path / "bank.npz", key=b"another server")
    cursor = cursor_of(one)
    w = one._cursor(cursor)["w"]
    assert one.bars(cursor)["bars"] == two._rows(w)[0][:150] != other._rows(w)[0][:150]
    assert one.reveal(cursor)["rest"] == one._rows(w)[0][150:]


def recovered_median(rows, true_median):
    """The attack on round lots: relative volumes times the real context median M are whole hundreds of shares, so
    cos(2 pi v M / 100) averages near 1 at M. The best candidate from M/2 to 2M, leaving out multiples of 1,000 (three
    decimals of relative volume make those trivial)."""
    v = np.array([row[4] for row in rows[:N_CONTEXT]], dtype=np.float64)
    candidates = np.arange(true_median // 2, true_median * 2, dtype=np.float64)
    candidates = candidates[candidates % 1000 != 0]
    score = np.cos(2 * np.pi * np.outer(candidates, v[v > 0]) / 100).mean(axis=1)
    return float(candidates[int(np.argmax(score))])


def test_the_volume_dither_is_stable_and_hides_the_round_lots(tmp_path):
    arrays, _ = fake_bank(tmp_path / "bank.npz", lots=True)
    bank_ = load_bank(tmp_path / "bank.npz")
    hits = {"plain": 0, "dithered": 0}
    for w in range(10):
        context = arrays["bars"][w][:N_CONTEXT, 4].astype(np.float64)
        median = float(np.median(context[context > 0]))
        plain, rows = window_rows(bank_, w)[0], window_rows(bank_, w, KEY)[0]
        # The same on every request and unpredictable without the key, but hardly different to read: on these thin
        # volumes (5,000 shares and up) within half a lot on the bar and on the median, and the rounding; a bar with
        # no volume still shows none.
        assert rows == window_rows(bank_, w, KEY)[0]
        assert [r[4] for r in rows] != [r[4] for r in window_rows(bank_, w, b"other key")[0]]
        before, after = np.array([r[4] for r in plain]), np.array([r[4] for r in rows])
        traded = before > 0
        assert ((after == 0) == ~traded).all() and (np.abs(after[traded] / before[traded] - 1) <= 0.03).all()
        # Nothing scales the median: the context's relative volumes have a median of one, so there is no hidden scale
        # to read back from them.
        context = after[:N_CONTEXT]
        assert np.median(context[context > 0]) == pytest.approx(1.0, abs=1e-3)
        hits["plain"] += abs(recovered_median(plain, median) - median) <= max(1.0, 0.002 * median)
        hits["dithered"] += abs(recovered_median(rows, median) - median) <= max(1.0, 0.002 * median)
    assert hits["plain"] >= 8 and hits["dithered"] == 0
    assert not hasattr(tradetest, "MEDIAN_DITHER")
    volume = np.array([0.0, 100.0, 5000.0, 1e6])
    moved = dither_volume(volume, b"seed")
    assert (moved == dither_volume(volume, b"seed")).all() and (moved[1:] != dither_volume(volume, b"other")[1:]).all()
    assert moved[0] == 0 and (np.abs(moved[1:] - volume[1:]) <= 50).all() and len(set(moved[1:] - volume[1:])) == 3


def test_bars_and_steps_never_reach_past_the_cursor(bank):
    service, _, _ = bank
    cursor = cursor_of(service)
    first = service.bars(cursor)
    assert (first["k"], first["n_context"], first["n_replay"], len(first["bars"])) == (0, 150, 100, 150)
    step = service.step(cursor, 5)
    assert step["k"] == 5 and len(step["bars"]) == 5 and not step["done"] and step["cursor"] != cursor
    rows, _ = service._rows(service._cursor(cursor)["w"])
    assert step["bars"] == rows[150:155]
    assert len(service.bars(step["cursor"])["bars"]) == 155 and len(service.bars(cursor)["bars"]) == 150
    for n in (0, 21, "3", True, 2.5, None):
        with pytest.raises(TradeTestError) as caught:
            service.step(cursor, n)
        assert caught.value.code == "bad_request"
    position = step["cursor"]
    for _ in range(5):
        step = service.step(position, 20)
        position = step["cursor"]
    assert step["k"] == 100 and step["done"] and len(step["bars"]) == 15
    end = service.step(position, 3)
    assert end == {"k": 100, "bars": [], "cursor": position, "done": True}


def test_reveal_names_the_instrument_and_dates_and_sends_the_rest_without_moving_the_cursor(bank):
    service, arrays, _ = bank
    made = service.new_set()
    cursor = service.step(made["charts"][3]["cursor"], 7)["cursor"]
    w = service._cursor(cursor)["w"]
    shown = service.reveal(cursor)
    i = int(arrays["instrument"][w])
    assert (shown["k"], shown["symbol"], shown["name"], shown["kind"]) == (7, f"S{i:02d}", f"Company {i}",
                                                                          "etf" if i < 4 else "stock")
    assert (shown["sector"], shown["industry"], shown["exchange"]) == ("Technology", "Software", "NYSE")
    assert shown["note"] == ("Delisted 2015-03-02" if i == 5 else "")
    assert len(shown["dates"]) == 250 and shown["dates"] == sorted(shown["dates"])
    assert shown["dates"][0] == str(np.datetime64(int(arrays["day"][w][0]), "D"))
    assert (shown["start"], shown["end"]) == (shown["dates"][150], shown["dates"][249])
    date.fromisoformat(shown["start"])
    rows, scale = service._rows(w)
    assert shown["rest"] == rows[157:] and shown["scale"] == scale
    assert service.bars(cursor)["k"] == 7
    json.dumps(shown, allow_nan=False)


def test_a_revealed_chart_is_finished_and_steps_no_further(bank, monkeypatch):
    service, _, _ = bank
    made = service.new_set()
    first = made["charts"][0]["cursor"]
    stepped = service.step(first, 5)["cursor"]
    assert service.reveal(stepped)["k"] == 5
    for cursor in (stepped, first):            # every cursor of the chart, an older one included
        with pytest.raises(TradeTestError) as caught:
            service.step(cursor, 1)
        assert (caught.value.code, caught.value.status, caught.value.message) == (
            "finished", 409, "This chart has already been finished.")
    # Its bars and its reveal still answer, and the set's other charts step on.
    assert len(service.bars(stepped)["bars"]) == 155 and service.reveal(first)["k"] == 0
    assert service.revealed_at(service._cursor(first)) == 0
    assert service.step(made["charts"][1]["cursor"], 1)["k"] == 1
    # The same chart of another set is that set's own.
    assert service.step(service.new_set()["charts"][0]["cursor"], 1)["k"] == 1
    # The record is bounded: past the limit the oldest are forgotten (best effort, like a restart).
    monkeypatch.setattr(tradetest, "REVEALED_KEPT", 2)
    for chart in made["charts"][2:5]:
        service.reveal(chart["cursor"])
    assert len(service._revealed) == 2 and service.step(first, 1)["k"] == 1


def test_a_missing_or_broken_bank_is_unavailable_until_a_good_one_appears(tmp_path):
    service = TradeTestService(tmp_path / "bank.npz", key=KEY)
    assert not service.available
    with pytest.raises(TradeTestUnavailable) as caught:
        service.new_set()
    assert (caught.value.code, caught.value.status) == ("unavailable", 503)
    (tmp_path / "bank.npz").write_bytes(b"not a bank")
    assert not service.available
    fake_bank(tmp_path / "bank.npz")
    assert service.available and len(service.new_set()["charts"]) == 10


# The API ----------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def api(service, limiter=None, client=("testclient", 50000)):
    app = FastAPI()
    app.include_router(api_router(service, limiter))
    return TestClient(app, client=client)


def test_the_api_answers_json_without_caching_and_refuses_bad_requests_with_400(bank):
    service, _, _ = bank
    client = api(service)
    made = client.post("/api/tradetest/new", json={"avoid": None})
    assert made.status_code == 200 and made.headers["cache-control"] == "no-store"
    cursor = made.json()["charts"][0]["cursor"]
    assert len(client.post("/api/tradetest/bars", json={"cursor": cursor}).json()["bars"]) == 150
    stepped = client.post("/api/tradetest/step", json={"cursor": cursor, "n": 3}).json()
    assert stepped["k"] == 3 and len(stepped["bars"]) == 3
    shown = client.post("/api/tradetest/reveal", json={"cursor": stepped["cursor"]})
    assert shown.status_code == 200 and len(shown.json()["rest"]) == 97
    finished = client.post("/api/tradetest/step", json={"cursor": stepped["cursor"], "n": 1})
    assert finished.status_code == 409 and finished.headers["cache-control"] == "no-store"
    assert finished.json() == {"error": "finished", "message": "This chart has already been finished."}
    empty = client.post("/api/tradetest/new", content=b"", headers={"content-type": "application/json; charset=utf-8"})
    assert empty.status_code == 200                                                  # an empty body is no avoid list
    assert client.post("/api/tradetest/new", json={"avoid": made.json()["avoid"]}).status_code == 200
    refused = [client.post("/api/tradetest/bars", content=b"{not json", headers={"content-type": "application/json"}),
               client.post("/api/tradetest/bars", json=[cursor]),
               client.post("/api/tradetest/bars", json={}),
               client.post("/api/tradetest/bars", json={"cursor": 5}),
               client.post("/api/tradetest/step", json={"cursor": cursor, "n": "3"}),
               client.post("/api/tradetest/step", json={"cursor": cursor, "n": 50}),
               client.post("/api/tradetest/step", json={"cursor": cursor}),
               client.post("/api/tradetest/new", json={"avoid": 3}),
               client.post("/api/tradetest/reveal", content=b"\xff\xfe", headers={"content-type": "application/json"}),
               client.post("/api/tradetest/bars", json={"cursor": "x" * 9000})]
    for response in refused:
        assert response.status_code == 400 and response.json()["error"] == "bad_request"
        assert response.json()["message"] and response.headers["cache-control"] == "no-store"
    forged = client.post("/api/tradetest/bars", json={"cursor": cursor[:-3] + "AAA"})
    assert forged.status_code == 400 and forged.json()["error"] == "bad_token"
    stale = client.post("/api/tradetest/bars", json={"cursor": Sealer(b"old").seal({"v": "x"})})
    assert stale.status_code == 410 and stale.json()["error"] == "expired"
    assert client.get("/api/tradetest/new").status_code == 405


def test_the_api_takes_only_json_and_never_fails_on_deep_nesting(bank):
    service, _, _ = bank
    limiter = RateLimiter({"new": (2, 3600), "reveal": (10, 3600), "replay": (10, 1)}, clock=Clock())
    client = api(service, limiter)
    # What another site's form or a no-cors fetch can send without a preflight is refused before it spends a budget.
    for headers, content in (({"content-type": "text/plain"}, b'{"avoid": null}'), ({}, b""),
                             ({"content-type": "application/x-www-form-urlencoded"}, b"avoid=")):
        refused = client.post("/api/tradetest/new", content=content, headers=headers)
        assert refused.status_code == 415 and refused.json()["error"] == "bad_request"
        assert refused.json()["message"] and refused.headers["cache-control"] == "no-store"
    made = [client.post("/api/tradetest/new", json={}) for _ in range(2)]
    assert [response.status_code for response in made] == [200, 200]
    cursor = made[0].json()["charts"][0]["cursor"]
    for content in (b"[" * 5000, b'{"cursor":' + b"[" * 5000, b'{"a":' * 1600):
        deep = client.post("/api/tradetest/bars", content=content, headers={"content-type": "application/json"})
        assert deep.status_code == 400 and deep.json()["error"] == "bad_request"
    assert client.post("/api/tradetest/bars", json={"cursor": cursor}).status_code == 200


def test_refusals_for_too_many_requests_say_when_to_come_back(bank):
    service, _, _ = bank
    clock = Clock()
    client = api(service, RateLimiter({"new": (2, 3600), "reveal": (2, 3600), "replay": (2, 1)}, clock=clock))
    cursor = client.post("/api/tradetest/new", json={}).json()["charts"][0]["cursor"]
    clock.now += 600
    assert client.post("/api/tradetest/new", json={}).status_code == 200
    clock.now += 60
    refused = client.post("/api/tradetest/new", json={})
    # The first set leaves the hour's count in 2,940 seconds: 49 minutes.
    assert refused.status_code == 429 and refused.headers["retry-after"] == "2940"
    assert refused.json() == {"error": "rate_limited", "message": "You’ve started many sets from this connection in "
                                                                  "the last hour. Try again in 49 minutes."}
    clock.now += 2939.5
    refused = client.post("/api/tradetest/new", json={})
    assert refused.headers["retry-after"] == "1" and refused.json()["message"].endswith("Try again in 1 minute.")
    replay = [client.post("/api/tradetest/bars", json={"cursor": cursor}) for _ in range(3)]
    assert [response.status_code for response in replay] == [200, 200, 429]
    assert replay[2].headers["retry-after"] == "1"
    assert replay[2].json()["message"] == "Too many requests at once. Wait a moment, then try again."
    assert client.post("/api/tradetest/reveal", json={"cursor": cursor}).status_code == 200


def test_the_messages_a_visitor_reads_use_typographic_apostrophes(bank):
    from stratlib.web import tradetest as web
    messages = [*tradetest.MESSAGES.values(), *web.LIMIT_MESSAGES.values(), *web.FIELD_MESSAGES.values()]
    assert all("'" not in message for message in messages) and sum("’" in message for message in messages) >= 4
    service, _, _ = bank
    refused = api(service).post("/api/tradetest/bars", json={"cursor": 5}).json()["message"]
    assert refused == "Send the chart’s cursor as text."


def test_the_api_reports_an_unavailable_bank_with_503(tmp_path):
    response = api(TradeTestService(tmp_path / "missing.npz")).post("/api/tradetest/new", json={})
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable", "message": "TradeTest is not available right now. Please try "
                                                                  "again later."}


def test_the_api_limits_each_address_and_trusts_only_the_proxys_forwarded_address(bank):
    service, _, _ = bank
    clock = Clock()
    limiter = RateLimiter({"new": (2, 3600), "reveal": (2, 3600), "replay": (3, 1)}, clock=clock)
    proxied = api(service, limiter, client=("127.0.0.1", 40000))
    made = proxied.post("/api/tradetest/new", json={}, headers={"x-forwarded-for": "9.9.9.9, 1.2.3.4"}).json()
    cursor = made["charts"][0]["cursor"]
    assert proxied.post("/api/tradetest/new", json={}, headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200
    limited = proxied.post("/api/tradetest/new", json={}, headers={"x-forwarded-for": "5.5.5.5, 1.2.3.4"})
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
    assert proxied.post("/api/tradetest/new", json={},
                        headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"}).status_code == 200
    # Bars and steps share a per-second budget.
    replay = [proxied.post(f"/api/tradetest/{path}", json={"cursor": cursor, "n": 1},
                           headers={"x-forwarded-for": "1.2.3.4"}).status_code for path in ("bars", "step", "bars", "step")]
    assert replay == [200, 200, 200, 429]
    clock.now += 1.5
    assert proxied.post("/api/tradetest/bars", json={"cursor": cursor},
                        headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200
    clock.now += 3600
    assert proxied.post("/api/tradetest/new", json={}, headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200
    # A visitor's own header is ignored when the request does not come through the proxy.
    direct = api(service, limiter, client=("203.0.113.7", 40000))
    statuses = [direct.post("/api/tradetest/new", json={}, headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code
                for i in range(3)]
    assert statuses == [200, 200, 429]


def test_the_rate_limiter_forgets_idle_addresses_and_tracks_a_bounded_number():
    clock = Clock()
    limiter = RateLimiter({"new": (1, 10), "replay": (5, 1)}, clock=clock, max_clients=3)
    for i in range(5):
        assert limiter.allow(f"10.0.0.{i}", "new")
    assert len(limiter) == 3
    assert not limiter.allow("10.0.0.4", "new")
    clock.now += 11
    assert limiter.allow("10.0.0.9", "replay") and len(limiter) == 1


def test_the_client_address_is_the_right_most_forwarded_entry_only_from_loopback():
    class Request:
        def __init__(self, host, forwarded=None):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
    assert client_address(Request("127.0.0.1", "1.1.1.1, 2.2.2.2")) == "2.2.2.2"
    assert client_address(Request("::1", "2001:db8::1")) == "2001:db8::1"
    assert client_address(Request("127.0.0.1")) == "127.0.0.1"
    assert client_address(Request("198.51.100.4", "1.1.1.1")) == "198.51.100.4"


# The page ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def app_for(settings, *, public=False):
    if public:
        from stratlib.publish import publish
        store = Store(settings.data.db_path)
        bars = walk(sessions("2026-01-02", "2026-10-02"), 600.0)
        store.write_prices("SPY", bars, replace=True, requested_from=bars[0].date, checked_at=NOW, status="ok")
        store.close()
        snapshot = settings.path.parent / "public" / "stratlib.db"
        publish(settings, snapshot)
        settings = replace(settings, data=replace(settings.data, db_path=snapshot))
    data = Data(settings, public=public)
    try:
        async with user_simulation(lambda: root(data)) as user:
            yield user
    finally:
        data.close()


def nav_links(user):
    return {link.text: link.props.get("href") for link in user.find(ui.link).elements}


@pytest.mark.anyio
@pytest.mark.parametrize("public", [False, True])
async def test_the_page_says_when_there_is_no_bank_and_both_apps_list_it(settings, monkeypatch, public):
    monkeypatch.delenv("STRATLIB_TRADETEST_BANK", raising=False)
    async with app_for(settings, public=public) as user:
        await user.open("/tradetest")
        await user.should_see("TradeTest is not available right now")
        if public:
            await user.should_see("Please check back later")
            await user.should_not_see("tradetest-bank")
        else:
            await user.should_see("stratlib tradetest-bank")
        await user.should_not_see("data-tradetest")
        assert nav_links(user)["TradeTest"] == "/tradetest"


@pytest.mark.anyio
@pytest.mark.parametrize("public", [False, True])
async def test_the_page_is_a_mount_point_for_the_browser_app(settings, monkeypatch, public):
    monkeypatch.delenv("STRATLIB_TRADETEST_BANK", raising=False)
    fake_bank(settings.path.parent / "public" / "tradetest_bank.npz")
    async with app_for(settings, public=public) as user:
        await user.open("/tradetest")
        await user.should_see('<div class="tt-root" data-tradetest data-assets="/tradetest-assets/" data-build="')
        await user.should_see("<noscript>")
        await user.should_not_see("not available")
        assert nav_links(user)["TradeTest"] == "/tradetest"
    # After Reports in both apps' navigation.
    assert list(PAGES)[-2:] == ["TradeTest", "Settings"] and list(PUBLIC_PAGES)[-2:] == ["Reports", "TradeTest"]


@pytest.mark.anyio
async def test_the_bank_can_live_anywhere_the_environment_says(settings, monkeypatch, tmp_path):
    fake_bank(tmp_path / "elsewhere" / "bank.npz")
    monkeypatch.setenv("STRATLIB_TRADETEST_BANK", str(tmp_path / "elsewhere" / "bank.npz"))
    async with app_for(settings) as user:
        await user.open("/tradetest")
        await user.should_see("data-tradetest")
