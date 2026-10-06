import json
from datetime import datetime

import pytest

from stratlib.backfill import BackfillError, run_backfill
from stratlib.fmp import FMPAuthError
from stratlib.prices import ET

from conftest import BASE, load_fixture, query_of

# Monday 28 Sep 2026, noon in New York: the market is open, so FMP's bar for
# the 28th is still in progress and the newest final bar is Friday the 25th.
MONDAY_NOON = datetime(2026, 9, 28, 12, 0, tzinfo=ET)
TUESDAY_EVENING = datetime(2026, 9, 29, 19, 0, tzinfo=ET)

AAPL_ROWS = load_fixture("historical-price-eod_full_AAPL_2025-09-01.json")
from test_universe import COMMON  # noqa: E402
MARKET = ["^GSPC", "^IXIC", "SPY", "QQQ"]


class FakeFMP:
    """Serves the screener fixtures and a price history for any symbol,
    filtered by the request's from/to dates like the real endpoint."""

    def __init__(self, mocked):
        self.rows = [dict(r) for r in AAPL_ROWS]
        self.price_factor: dict[str, float] = {}
        self.status: dict[str, tuple[int, str]] = {}
        self.price_requests: list[dict] = []
        mocked.add_callback("GET", f"{BASE}/company-screener", callback=self._screener)
        mocked.add_callback("GET", f"{BASE}/historical-price-eod/full", callback=self._prices)

    def _screener(self, request):
        exchange = query_of(request)["exchange"]
        return 200, {}, json.dumps(load_fixture(f"company-screener_{exchange}.json"))

    def _prices(self, request):
        q = query_of(request)
        self.price_requests.append(q)
        symbol = q["symbol"]
        if symbol in self.status:
            return self.status[symbol][0], {}, self.status[symbol][1]
        factor = self.price_factor.get(symbol, 1.0)
        rows = [
            {**r, "symbol": symbol, "close": round(r["close"] * factor, 4)}
            for r in self.rows
            if q.get("from", "0000") <= r["date"] <= q.get("to", "9999")
        ]
        return 200, {}, json.dumps(rows)

    def add_session(self, day: str, close: float) -> None:
        self.rows.insert(0, {**self.rows[0], "date": day, "close": close})


@pytest.fixture
def fmp(mocked):
    return FakeFMP(mocked)


def test_first_backfill_loads_universe_and_history(fmp, client, store, settings):
    report = run_backfill(client, store, settings, now=MONDAY_NOON)

    n = len(MARKET) + len(COMMON)
    assert report.universe.common == len(COMMON)
    assert report.universe.excluded["preferred"] == 3
    assert store.universe_symbols() == COMMON
    assert report.prices.symbols == n
    assert report.prices.fetched == n
    assert report.api_calls == 3 + n  # one screener call per exchange, one history call per symbol

    history = store.price_history("NVDA")
    assert history[0].date == "2025-09-02"
    # The in-progress bar for Monday the 28th is not stored.
    assert history[-1].date == "2026-09-25"
    assert report.prices.bars_written == n * len(history)

    runs = store.last_runs("backfill")
    assert runs[0]["summary"]["api_calls"] == 3 + n


def test_repeat_run_makes_no_api_calls(fmp, client, store, settings):
    run_backfill(client, store, settings, now=MONDAY_NOON)

    again = run_backfill(client, store, settings, now=MONDAY_NOON)

    assert again.api_calls == 0
    assert again.cache_hits == 3
    assert again.prices.already_current == len(MARKET) + len(COMMON)


def test_next_day_fetches_only_new_bars(fmp, client, store, settings):
    run_backfill(client, store, settings, now=MONDAY_NOON)
    fmp.add_session("2026-09-29", 344.0)
    fmp.price_requests.clear()

    report = run_backfill(client, store, settings, now=TUESDAY_EVENING)

    n = len(MARKET) + len(COMMON)
    assert report.api_calls == n  # universe list still cached
    assert {r["from"] for r in fmp.price_requests} == {"2026-09-25"}
    assert report.prices.bars_written == n * 2  # the 28th (now final) and the 29th
    assert [b.date for b in store.price_history("AAPL")][-3:] == ["2026-09-25", "2026-09-28", "2026-09-29"]


def test_restated_history_is_downloaded_again(fmp, client, store, settings):
    run_backfill(client, store, settings, symbols=["NVDA"], now=MONDAY_NOON)
    before = store.price_history("NVDA")
    fmp.price_factor["NVDA"] = 0.5  # FMP re-adjusted NVDA for a 2-for-1 split
    fmp.price_requests.clear()

    report = run_backfill(client, store, settings, symbols=["NVDA"], now=TUESDAY_EVENING)

    assert report.prices.restated == 1
    nvda_requests = [r for r in fmp.price_requests if r["symbol"] == "NVDA"]
    # Incremental fetch overlapping the last bar, then the full re-download
    # from the originally requested start (five years before Sunday the 27th).
    assert [r["from"] for r in nvda_requests] == ["2026-09-25", "2021-09-27"]
    after = store.price_history("NVDA")
    assert after[0].close == pytest.approx(before[0].close * 0.5, rel=1e-3)
    assert after[-1].date == "2026-09-28"


def test_symbols_option_skips_universe(fmp, client, store, settings, mocked):
    report = run_backfill(client, store, settings, symbols=["aapl", " msft "], now=MONDAY_NOON)

    assert report.universe is None
    assert report.prices.symbols == len(MARKET) + 2
    assert not any("company-screener" in c.request.url for c in mocked.calls)


def test_sample_limits_and_repeats(fmp, client, store, settings):
    first = run_backfill(client, store, settings, sample=3, now=MONDAY_NOON)
    again = run_backfill(client, store, settings, sample=3, now=MONDAY_NOON)

    assert first.prices.symbols == len(MARKET) + 3
    assert again.api_calls == 0


def test_plan_restricted_symbols_are_recorded_not_retried(fmp, client, store, settings):
    fmp.status["AEON"] = (402, load_fixture("error_402_symbol.txt"))

    first = run_backfill(client, store, settings, now=MONDAY_NOON)
    again = run_backfill(client, store, settings, now=MONDAY_NOON)

    assert first.prices.restricted == ["AEON"]
    assert store.price_states(["AEON"])["AEON"].status == "restricted"
    assert again.api_calls == 0


def test_invalid_key_aborts_and_run_is_marked_failed(fmp, client, store, settings):
    fmp.status["^GSPC"] = (401, '{"Error Message": "Invalid API KEY."}')

    with pytest.raises(FMPAuthError):
        run_backfill(client, store, settings, symbols=["AAPL"], now=MONDAY_NOON, workers=1)

    assert "FMPAuthError" in store.last_runs("backfill")[0]["summary"]["failed"]


def test_empty_screener_keeps_stored_universe(fmp, client, store, settings, mocked):
    run_backfill(client, store, settings, now=MONDAY_NOON)
    store.clear_responses("company-screener")
    mocked.replace("GET", f"{BASE}/company-screener", json=[])

    with pytest.raises(BackfillError):
        run_backfill(client, store, settings, now=TUESDAY_EVENING)
    assert store.universe_symbols() == COMMON


def test_repeat_run_keeps_open_positions_outside_the_universe_current(fmp, client, store, settings):
    from stratlib.sell_rules import Position
    run_backfill(client, store, settings, now=MONDAY_NOON)
    store.save_position(Position("BRK-A", "2026-09-01", 700000.0), as_of="2026-09-28")
    assert "BRK-A" not in store.universe_symbols()          # the secondary share class
    report = run_backfill(client, store, settings, now=MONDAY_NOON)
    assert report.prices.symbols == 4 + len(store.universe_symbols()) + 1
