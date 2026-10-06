"""The web app, driven through NiceGUI's simulated user: no browser, no network."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from nicegui.testing.user_simulation import user_simulation

from stratlib.presentation import LABELS
from stratlib.screening import run_screen
from stratlib.store import Store
from stratlib.strategies import latest_screen
from stratlib.web.app import root
from stratlib.web.data import Data
from stratlib.web.screen import sort_rows
from test_jev import evidence, reply  # noqa: F401  shared synthetic fixtures
from test_reports import research  # noqa: F401  a small saved-research folder
from test_screening import NOW, seed_market


def scan_market(settings):
    """The scan strategies' synthetic market, with index history for the Market page."""
    from test_market_direction import append, synthetic_market
    from test_scan_strategies import NOW as SCAN_NOW, seed_scan_market
    store = Store(settings.data.db_path)
    days = seed_scan_market(store)
    bars = synthetic_market()
    append(bars, 1.25)
    for symbol in ("^GSPC", "^IXIC"):
        store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date, checked_at=SCAN_NOW, status="ok")
    return store, days

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def web(settings):
    data = Data(settings)
    try:
        async with user_simulation(lambda: root(data)) as user:
            yield user
    finally:
        data.close()


def screened(settings, strategy_id="canslim"):
    store = Store(settings.data.db_path)
    try:
        seed_market(store)
        return run_screen(store, settings, now=NOW, strategy_id=strategy_id)
    finally:
        store.close()


def value_of(user, marker):
    return next(iter(user.find(marker=marker).elements))


async def test_an_empty_store_teaches_the_first_screen_and_every_page_opens(settings):
    async with web(settings) as user:
        await user.open("/")
        await user.should_see("No saved CANSLIM screen yet")
        await user.should_see("stratlib screen --sample 50")
        # Every page in the navigation opens in this app.
        for path in ("/portfolio", "/market", "/stock", "/positions", "/backtest", "/reports", "/settings"):
            await user.open(f"{path}?strategy=trend")
            await user.should_not_see("has not moved")


async def test_the_list_scopes_filters_and_follows_the_picked_row(settings):
    report = screened(settings)
    async with web(settings) as user:
        await user.open("/")
        # TOP is the only survivor; the funnel's first step shows every stock.
        await user.should_see('data-symbol="TOP"')
        await user.should_not_see('data-symbol="LOW"')
        user.find("All stocks").click()
        for row in report["rows"]:
            await user.should_see(f'data-symbol="{row["symbol"]}"')
        user.find(marker="results").trigger("click", "MID")
        await user.should_see('class="d-sym-id"><b>MID</b>')
        user.find(marker="search").type("NO-SUCH-SYMBOL")
        await user.should_see("No stocks match these filters")


async def test_the_check_filter_keeps_rows_with_that_result(settings):
    report = screened(settings)
    top = next(r for r in report["rows"] if r["symbol"] == "TOP")
    check = next(c for c in top["criteria"] if c["passed"] is not None)
    async with web(settings) as user:
        await user.open("/")
        value_of(user, "check").set_value(LABELS[check["key"]])
        value_of(user, "result").set_value("Pass" if check["passed"] else "Fail")
        await user.should_see('data-symbol="TOP"')
        value_of(user, "result").set_value("Fail" if check["passed"] else "Pass")
        await user.should_see("No stocks match these filters")


async def test_run_screen_saves_a_cache_only_screen(settings):
    store = Store(settings.data.db_path)
    seed_market(store)
    try:
        async with web(settings) as user:
            await user.open("/")
            await user.should_see("No saved CANSLIM screen yet")
            value_of(user, "refresh").set_value(False)
            user.find(marker="run-screen").click()
            await user.should_see("Screen saved.", retries=200)
            await user.should_see('data-symbol="TOP"')
        saved = latest_screen(store, "canslim")
        assert saved["cache_only"] and saved["api_calls"] == 0
    finally:
        store.close()


async def test_trend_evaluation_saves_its_own_screen_and_reopens_history(settings):
    original = screened(settings)
    store = Store(settings.data.db_path)
    try:
        async with web(settings) as user:
            await user.open("/")
            value_of(user, "strategy").set_value("trend")
            await user.should_see("No saved Trend Leaders screen yet")
            user.find(marker="evaluate").click()
            await user.should_see("Strategy screen saved.")
            saved = latest_screen(store, "trend")
            assert saved["strategy_id"] == "trend" and saved["price_date"] == original["price_date"]
            assert latest_screen(store, "canslim") == original
            value_of(user, "saved-run").set_value(saved["screen_key"])
            await user.should_see("Trend Leaders rules" if saved["candidates"] else "No stocks meet the Trend Leaders rules")
    finally:
        store.close()


async def test_running_a_scan_shows_its_candidates_signal_and_rules(settings):
    store, _ = scan_market(settings)
    try:
        async with web(settings) as user:
            await user.open("/?strategy=ema_pullback")
            await user.should_see("No saved Traveling Trader 9/21 EMA screen yet")
            user.find(marker="run-screen").click()
            await user.should_see("Screen saved.", retries=200)
            await user.should_see('data-symbol="UP1"')
            await user.should_see("Liquid stocks")
            await user.should_see("<h3>Signal</h3>")
            await user.should_see("Saved strategy rules")
        assert [c["symbol"] for c in latest_screen(store, "ema_pullback")["candidates"]] == ["UP1"]
        assert latest_screen(store, "canslim") is None
    finally:
        store.close()


def test_rows_sort_by_rank_or_measure_with_unavailable_values_last():
    rows = [{"symbol": "A", "rank": 2, "rs": 90, "change": None},
            {"symbol": "B", "rank": 1, "rs": None, "change": 5.0},
            {"symbol": "C", "rank": 3, "rs": 95, "change": -1.0}]
    assert [r["symbol"] for r in sort_rows(rows, "rank", True)] == ["B", "A", "C"]
    assert [r["symbol"] for r in sort_rows(rows, "RS", True)] == ["C", "A", "B"]
    assert [r["symbol"] for r in sort_rows(rows, "RS", False)] == ["A", "C", "B"]
    assert [r["symbol"] for r in sort_rows(rows, "change", True)] == ["B", "C", "A"]


async def test_stock_detail_without_a_universe_teaches_the_backfill(settings):
    async with web(settings) as user:
        await user.open("/stock")
        await user.should_see("No stocks loaded yet")


async def test_stock_detail_shows_saved_checks_under_the_screens_thresholds_and_saves_sponsorship(settings):
    from dataclasses import replace
    store = Store(settings.data.db_path)
    seed_market(store)
    # Saved thresholds, not the current config, explain this screen's verdict.
    run_screen(store, replace(settings, thresholds=replace(settings.thresholds, min_price=1000)), now=NOW)
    try:
        async with web(settings) as user:
            await user.open("/stock?symbol=TOP")
            await user.should_see('class="d-sym-id"><b>TOP</b>')
            await user.should_see("Saved C/A/S/L checks")
            await user.should_see("Close above 200-session average")
            await user.should_see("&gt;= $1,000.00")
            await user.should_see("Base and market checks")
            value_of(user, "sponsorship").set_value("Pass")
            value_of(user, "sponsorship-notes").set_value("Reviewed annual filing")
            user.find(marker="save-sponsorship").click()
            await user.should_see("Sponsorship saved for TOP.")
            assert store.sponsorship("TOP")["verdict"] == "Pass"
            assert store.sponsorship("TOP")["notes"] == "Reviewed annual filing"
            value_of(user, "symbol").set_value("MID")
            await user.should_see('class="d-sym-id"><b>MID</b>')
            await user.open("/stock?symbol=ZZZZ")
            await user.should_see("ZZZZ is not in the stored universe.")
    finally:
        store.close()


async def test_the_screen_panel_opens_the_picked_stock_in_stock_detail(settings):
    from nicegui import ui
    screened(settings)
    async with web(settings) as user:
        await user.open("/")
        user.find("All stocks").click()
        user.find(marker="results").trigger("click", "LOW")
        await user.should_see('class="d-sym-id"><b>LOW</b>')
        links = [e.props["href"] for e in user.find(kind=ui.html).elements if e.props.get("href", "").startswith("/stock")]
        assert links == ["/stock?symbol=LOW"]


async def test_a_jev_review_asks_before_its_paid_request_and_shows_the_saved_answers(settings, evidence, reply, mocked,
                                                                                     monkeypatch):
    from dataclasses import replace
    import responses
    from stratlib.config import JevSettings
    from stratlib.jev import ENDPOINT
    from stratlib.universe import parse_listings
    from test_jev import KEY
    bars, base, as_of = evidence
    monkeypatch.setenv("AI_GATEWAY_API_KEY", KEY)
    monkeypatch.setattr("stratlib.web.stock.stock_setup", lambda *a, **k: (bars, base))
    monkeypatch.setattr(Data, "market", lambda self: {"as_of": as_of, "history": [], "warnings": [], "state": None,
                                                      "exposure": None, "indexes": {}})
    mocked.add(responses.POST, ENDPOINT, json=reply)
    store = Store(settings.data.db_path)
    store.replace_universe(parse_listings([{"symbol": "TEST", "companyName": "Test Corp", "exchangeShortName": "NYSE"}]))
    try:
        async with web(replace(settings, jev=JevSettings(enabled=True, max_retries=0))) as user:
            await user.open("/stock?symbol=TEST")
            await user.should_see("Each review is a paid request")
            user.find(marker="jev-review").click()
            await user.should_see("makes a paid request.")
            assert not mocked.calls
            user.find(marker="jev-confirm").click()
            await user.should_see("Review saved. 1 Gateway HTTP call, including retries.", retries=100)
            assert len(mocked.calls) == 1
            await user.should_see("Review again with Jev")
            await user.should_see("potential warning sign")
            await user.should_see("handle shows erratic selling")
            await user.should_see("Review history")
    finally:
        store.close()


def market_history(settings):
    from test_market_direction import append, synthetic_market
    store = Store(settings.data.db_path)
    bars = synthetic_market()
    append(bars, 1.25)
    for symbol in ("^GSPC", "^IXIC", "SPY", "QQQ"):
        store.write_prices(symbol, bars, replace=True, requested_from=bars[0].date, checked_at=NOW, status="ok")
    store.close()


async def test_market_without_history_teaches_the_backfill(settings):
    async with web(settings) as user:
        await user.open("/market")
        await user.should_see("No market history yet")
        await user.should_see("stratlib backfill --symbols")


async def test_market_switches_indexes_and_follows_the_strategys_policy(settings):
    market_history(settings)
    async with web(settings) as user:
        await user.open("/market")
        await user.should_see("Confirmed uptrend")
        await user.should_see('class="d-sym-id"><b>S&amp;P 500</b>')
        await user.should_see("Market rules")
        user.find(marker="index-^IXIC").click()
        await user.should_see('class="d-sym-id"><b>Nasdaq Composite</b>')
        await user.should_see("Nasdaq Composite: distribution and follow-through days")
        value_of(user, "strategy").set_value("qullamaggie")
        await user.should_see("Qullamaggie Breakout policy")
        await user.should_not_see("invested · up to")


async def test_positions_add_validate_edit_close_and_reopen(settings):
    from datetime import date
    seed = Store(settings.data.db_path)
    seed_market(seed)
    seed.close()
    store = Store(settings.data.db_path)
    try:
        async with web(settings) as user:
            await user.open("/positions")
            await user.should_see("No open positions for CANSLIM")
            user.find(marker="add-position").click()
            user.find(marker="save-position").click()
            await user.should_see("Enter a ticker using letters, numbers, dots or hyphens.")
            assert not store.positions()
            value_of(user, "form-ticker").set_value(" top ")
            value_of(user, "form-price").set_value(100.0)
            value_of(user, "form-date").set_value("")
            user.find(marker="save-position").click()
            await user.should_see("Choose an entry date.")
            value_of(user, "form-date").set_value(date.today().isoformat())
            value_of(user, "form-quantity").set_value(12)
            user.find(marker="save-position").click()
            await user.should_see("Position added for TOP.")
            record = store.positions()[0]
            assert (record["symbol"], record["entry_price"], record["quantity"], record["strategy_id"]) == ("TOP", 100, 12, "canslim")
            assert record["rule_snapshot"]["thresholds"]["stop_loss_pct"] == settings.thresholds.stop_loss_pct
            # Editing the entry keeps the saved rules.
            user.find(marker="edit-position").click()
            value_of(user, "form-price").set_value(110.0)
            user.find(marker="save-position").click()
            await user.should_see("Position saved for TOP.")
            after = store.positions()[0]
            assert after["entry_price"] == 110 and after["rule_snapshot"] == record["rule_snapshot"]
            user.find(marker="close-position").click()
            await user.should_see("TOP moved to closed positions.")
            assert not store.positions()
            user.find(marker=f"reopen-{record['id']}").click()
            await user.should_see("TOP reopened.")
            assert store.positions()[0]["id"] == record["id"]
    finally:
        store.close()


async def test_positions_under_a_scan_strategy_save_the_stop_and_its_own_rules(settings):
    store, days = scan_market(settings)
    try:
        async with web(settings) as user:
            await user.open("/positions?strategy=qullamaggie")
            user.find(marker="add-position").click()
            assert value_of(user, "form-strategy").value == "qullamaggie"
            value_of(user, "form-ticker").set_value("UP1")
            value_of(user, "form-date").set_value(days[-1])
            value_of(user, "form-price").set_value(60.0)
            value_of(user, "form-stop").set_value(55.0)
            value_of(user, "form-quantity").set_value(10)
            user.find(marker="save-position").click()
            await user.should_see("Position added for UP1.")
            record = store.positions()[0]
            assert (record["strategy_id"], record["stop_price"], record["quantity"]) == ("qullamaggie", 55.0, 10)
            assert record["rule_snapshot"]["params"]["trail_sma"] == 10 and "thresholds" not in record["rule_snapshot"]
            await user.should_see('data-pick="')
            # The CANSLIM workspace does not list it, all strategies do.
            value_of(user, "strategy").set_value("canslim")
            await user.should_see("No open positions for CANSLIM")
            user.find(marker="scope-all").click()
            await user.should_see(f'data-pick="{record["id"]}"')
    finally:
        store.close()


async def test_portfolio_without_a_screen_asks_for_one_and_scopes_cash_to_the_strategy(settings):
    store = Store(settings.data.db_path)
    try:
        async with web(settings) as user:
            await user.open("/portfolio")
            await user.should_see("Run the screen first for this strategy.")
            user.find(marker="record-cash").click()
            user.find(marker="save-cash").click()
            await user.should_see("Enter a cash balance; zero is valid.")
            value_of(user, "cash-amount").set_value(500)
            user.find(marker="save-cash").click()
            await user.should_see("Cash of $500.00 recorded for CANSLIM.")
            value_of(user, "strategy").set_value("trend")
            await user.should_see("Not recorded yet")
        assert store.document("portfolio:cash:canslim:breakout")["amount"] == 500
        assert store.document("portfolio:cash:trend:long-trend") is None
    finally:
        store.close()


async def test_portfolio_plans_trend_leaders_beside_the_holdings(settings, monkeypatch):
    from datetime import date
    from stratlib.scoring import Criterion
    from stratlib.sell_rules import Position
    from stratlib.strategies import project_screen, rule_snapshot
    store = Store(settings.data.db_path)
    seed_market(store)
    report = run_screen(store, settings, now=NOW)
    # Make the three synthetic stocks leaders: TOP and MID qualify; LOW's sales growth is too slow.
    for row in report["rows"]:
        rs, sales = {"TOP": (99, 50.0), "MID": (95, 30.0), "LOW": (92, 10.0)}[row["symbol"]]
        row["rs"], row["industry_stats"] = rs, {"rank": 1, "return_pct": 10.0, "members": 3}
        row["prices"] = {**row["prices"], "price_pass": True, "avg_volume": 1e6}
        row["criteria"] = [Criterion("c_sales", "C", "Quarterly sales growth", sales, True, "").to_dict()]
    store.save_screen(project_screen(report, "trend", settings.thresholds, settings.backtest))
    tracking = {"strategy_id": "trend", "variant_id": "long-trend",
                "rule_snapshot": rule_snapshot("trend", settings.thresholds, settings.backtest)}
    store.save_position(Position("LOW", "2025-06-02", 20.0), as_of=report["price_date"], tracking=tracking)
    store.save_position(Position("TOP", "2025-06-02", 20.0), as_of=report["price_date"], tracking=tracking)
    # Let the market allow full exposure on the screen's date, so open slots are bought.
    real = Data.market
    monkeypatch.setattr(Data, "market", lambda self: {**real(self), "exposure": 100.0})
    monkeypatch.setattr("stratlib.web.portfolio.eod_cutoff", lambda *a: date.fromisoformat(report["price_date"]))
    try:
        async with web(settings) as user:
            await user.open("/portfolio?strategy=trend")
            await user.should_see("Model changes")
            await user.should_see('href="/stock?symbol=MID"><b>MID</b></a><small>MID</small></td><td><span class="d-pill tone-pass">Proposed buy')
            await user.should_see('href="/stock?symbol=TOP"><b>TOP</b></a><small>TOP</small></td><td><span class="d-pill tone-none">Held')
            await user.should_see("<b>LOW</b>")
            # The hypothetical portfolio buys the strategy's list from scratch, ignoring the recorded lots.
            await user.should_see("Hypothetical portfolio")
            hypothetical = next(iter(user.find(marker="hypothetical").elements)).content
            assert 'symbol=TOP"><b>TOP</b>' in hypothetical and 'symbol=MID"><b>MID</b>' in hypothetical
            assert "Cash" in hypothetical
            value_of(user, "hypo-capital").set_value(1)
            await user.should_see("No candidate could be bought in whole shares at this amount.")
    finally:
        store.close()


async def test_settings_cover_every_threshold_and_validate_before_saving(settings):
    from dataclasses import fields, replace
    from stratlib.config import Thresholds, load_settings, save_thresholds
    from stratlib.presentation import THRESHOLD_GROUPS
    names = [name for group in THRESHOLD_GROUPS.values() for name, _ in group]
    assert len(names) == len(set(names)) == len(fields(Thresholds))
    async with web(settings) as user:
        await user.open("/settings")
        for field in fields(Thresholds):
            assert user.find(marker=f"threshold-{field.name}").elements
        original = settings.path.read_bytes()
        value_of(user, "threshold-stop_loss_pct").set_value(100.0)
        await user.should_see("1 unsaved change.")
        user.find(marker="save-thresholds").click()
        await user.should_see("Settings were not saved.")
        assert settings.path.read_bytes() == original
        value_of(user, "threshold-stop_loss_pct").set_value(8.0)
        value_of(user, "threshold-profit_target_pct").set_value(25.0)
        user.find(marker="save-thresholds").click()
        await user.should_see("Thresholds saved.")
        assert load_settings(settings.path).thresholds.stop_loss_pct == 8
        user.find(marker="load-defaults").click()
        await user.should_see("Defaults loaded into the form.")
        assert value_of(user, "threshold-stop_loss_pct").value == 7
        assert load_settings(settings.path).thresholds.stop_loss_pct == 8
        user.find(marker="reload-settings").click()
        await user.should_see("Saved settings reloaded.")
        assert value_of(user, "threshold-stop_loss_pct").value == 8
        # An edit saved elsewhere since the form loaded is never overwritten.
        current = load_settings(settings.path).thresholds
        save_thresholds(settings.path, replace(current, stop_loss_pct=9), expected=current)
        value_of(user, "threshold-stop_loss_pct").set_value(6.0)
        user.find(marker="save-thresholds").click()
        await user.should_see("Thresholds changed on disk")
        assert load_settings(settings.path).thresholds.stop_loss_pct == 9


async def test_reports_index_searches_and_opens_a_post_with_its_variations(settings, research, tmp_path):
    data = Data(settings)
    data.research_root = research
    try:
        async with user_simulation(lambda: root(data)) as user:
            await user.open("/reports")
            await user.should_see("of 2 reports")
            value_of(user, "report-search").set_value("nothing matches")
            await user.should_see("No reports match these filters.")
            value_of(user, "report-search").set_value("entry")
            await user.should_see("<b>1</b> of 2 reports")
            await user.open("/reports?report=entry-study")
            await user.should_see("Entry study")
            await user.should_see("The original finding.")
            await user.should_see("Compare variations")
            await user.should_see(">baseline<")
            await user.should_not_see(">variation<")
            user.find(marker="period-in_sample").click()
            await user.should_see(">variation<")
            value_of(user, "report-run").set_value("variation")
            await asyncio.sleep(0.1)  # a refresh runs on the next turn of the event loop
            user.find(marker="period-out_of_sample").click()
            await user.should_not_see(">variation<")
            # The out-of-sample window has no result for that variation, so the inspector falls back.
            assert value_of(user, "report-run").value == "baseline"
            await user.open("/reports?report=../../outside")
            await user.should_see("This link does not match a saved report")
        empty = tmp_path / "empty"
        empty.mkdir()
        data.research_root = empty
        async with user_simulation(lambda: root(data)) as user:
            await user.open("/reports")
            await user.should_see("No reports have been published yet")
    finally:
        data.close()


@asynccontextmanager
async def public_web(settings):
    """The public app over a published snapshot of the store."""
    from dataclasses import replace
    from stratlib.publish import publish
    snapshot = settings.path.parent / "public" / "stratlib.db"
    publish(settings, snapshot)
    data = Data(replace(settings, data=replace(settings.data, db_path=snapshot)), public=True)
    try:
        async with user_simulation(lambda: root(data)) as user:
            yield user
    finally:
        data.close()


async def test_the_public_app_compares_the_strategies_and_leaves_out_every_write(settings):
    from stratlib.sell_rules import Position
    report = screened(settings)
    store = Store(settings.data.db_path)
    store.save_position(Position("TOP", "2025-06-02", 20.0), as_of=report["price_date"])
    store.save_sponsorship("TOP", "Pass", "Private notes")
    store.close()
    async with public_web(settings) as user:
        await user.open("/")
        await user.should_see("Nine rule sets on US stocks")
        await user.should_see("Read-only demo")
        await user.should_see("nothing here is investment advice")
        await user.should_not_see("API calls today")
        # Picking a strategy shows its rules, and every other page then opens on it.
        await user.should_see("Buy qualified breakouts")
        user.find(marker="strategies").trigger("click", "minervini")
        await user.should_see("Buy a stock in a Stage 2 uptrend")
        await user.should_see("This strategy was not screened for this snapshot.")
        await user.open("/screen?strategy=canslim")
        await user.should_see('data-symbol="TOP"')
        await user.should_not_see("Run screen")
        await user.should_not_see("Run options")
        await user.open("/screen?strategy=nash_quality")
        await user.should_see("No saved Nash Quality Screen screen")
        await user.open("/portfolio")
        await user.should_see("Hypothetical portfolio")
        await user.should_not_see("Record cash")
        await user.should_not_see("Recorded account")
        await user.open("/stock?symbol=TOP")
        await user.should_see("Saved C/A/S/L checks")
        await user.should_not_see("Institutional sponsorship")
        await user.should_not_see("Private notes")
        await user.should_not_see("Jev base review")
        await user.open("/backtest")
        await user.should_see("No saved backtest in this snapshot")
        await user.should_not_see("Run a backtest")
        for path in ("/positions", "/settings"):
            assert (await user.http_client.get(path)).status_code == 404
        for path in ("/", "/screen", "/portfolio", "/market", "/stock", "/backtest", "/reports"):
            await user.open(f"{path}?strategy=trend")
            await user.should_not_see("produced an error")
            await user.should_not_see("FMP")                 # the data provider is never named


async def test_the_public_strategies_page_and_backtest_show_saved_results_only(settings, monkeypatch):
    from stratlib import backtest_approx
    from test_backtest_approx import seed_approximate
    from test_sim import saved_result
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    current = configure(settings, updated)
    backtest_approx.run(store, updated, start, end, label="Breakout check")
    results = saved_result(store, current, monkeypatch, "canslim")["results"]
    store.close()
    async with public_web(current) as user:
        await user.open("/")
        # The comparable backtest stands for each strategy, in the period chosen above the list.
        await user.should_see("SPY CAGR, with dividends")
        await user.should_see(f"{results['combined']['cagr']:.1f}%")
        user.find(marker="st-period-in_sample").click()
        await user.should_see(f"{results['in_sample']['cagr']:.1f}%")
        await user.should_see("2021-07-01 to 2021-12-20")
        await user.open("/backtest")
        await user.should_see("Comparable backtest")
        await user.should_see("end of test")
        await user.should_see("Year by year")
        # The older kinds stay below it.
        await user.should_see('<h2 class="d-bt-title">Breakout check</h2>')
        await user.should_not_see("Run a backtest")
        await user.should_not_see("Run again")
        await user.should_not_see("Prepare data")


def configure(settings, updated, **backtest):
    """Write a seeded scenario's rules into the config file, then read it back as the app will."""
    import yaml
    from dataclasses import asdict
    from stratlib.config import load_settings
    raw = yaml.safe_load(settings.path.read_text())
    raw["thresholds"], raw["backtest"] = asdict(updated.thresholds), {**asdict(updated.backtest), **backtest}
    settings.path.write_text(yaml.safe_dump(raw))
    return load_settings(settings.path)


def run_form(user, start, end):
    value_of(user, "bt-start").set_value(start)
    value_of(user, "bt-end").set_value(end)
    user.find(marker="bt-run").click()


async def test_backtest_without_results_explains_what_is_missing(settings):
    async with web(settings) as user:
        await user.open("/backtest")
        await user.should_see("No saved backtest yet")
        user.find(marker="bt-check").click()
        await user.should_see("The data does not cover this run yet.", retries=50)
        await user.should_see("SPY")
        await user.should_see("Prepare data")
        user.find(marker="mode-Strict").click()
        await user.should_see("Prepare archived data")
        user.find(marker="bt-check").click()
        await user.should_see("The data does not cover this run yet.", retries=50)
        assert "disabled" in next(iter(user.find(marker="bt-import").elements)).props


async def test_a_strict_backtest_runs_shows_its_result_and_keeps_it_when_a_later_run_fails(settings):
    from test_backtest_data import seed_archive
    store = Store(settings.data.db_path)
    updated, start, end, _ = seed_archive(store, settings)
    current = configure(settings, updated)
    try:
        async with web(current) as user:
            await user.open("/backtest")
            user.find(marker="mode-Strict").click()
            await user.should_see("Prepare archived data")
            run_form(user, start, end)
            await user.should_see("Backtest saved.", retries=200)
            await user.should_see("<td class=\"\">CAGR</td>")
            await user.should_see(">AAA<")
            old = store.document("latest_backtest")
            run_form(user, "2025-01-01", end)
            await user.should_see("Missing dated universe", retries=200)
            assert store.document("latest_backtest") == old
            await user.should_see(">AAA<")
    finally:
        store.close()


async def test_an_approximate_backtest_is_labelled_and_lists_the_rejecting_checks(settings):
    from test_backtest_approx import seed_approximate
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    current = configure(settings, updated)
    store.close()
    async with web(current) as user:
        await user.open("/backtest")
        run_form(user, start, end)
        await user.should_see("Backtest saved.", retries=200)
        await user.should_see("Approximate result.")
        await user.should_see("Approximate method")
        await user.should_see(">AAA<")
        await user.should_see("Which checks rejected candidates")


async def test_a_no_trade_result_explains_the_funnel(settings):
    from dataclasses import replace
    from stratlib import backtest_approx
    from test_backtest_approx import seed_approximate
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    backtest_approx.run(store, replace(updated, thresholds=replace(updated.thresholds, eps_growth_pct=10_000)), start, end)
    store.close()
    async with web(settings) as user:
        await user.open("/backtest")
        await user.should_see("No trades under the current rules.")
        await user.should_see("How candidates were filtered")


async def test_saved_backtests_are_compared_and_one_can_be_picked(settings):
    from dataclasses import replace
    from stratlib import backtest_approx
    from test_backtest_approx import seed_approximate
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    current = configure(settings, updated)
    backtest_approx.run(store, replace(updated, thresholds=replace(updated.thresholds, market_index_rule="sp500")),
                        start, end, label="Older run")
    backtest_approx.run(store, updated, start, end, label="Newer run")
    older = store.document_keys("backtest:result:")[1]
    store.close()
    async with web(current) as user:
        await user.open("/backtest")
        await user.should_see("Saved results")
        await user.should_see('<h2 class="d-bt-title">Newer run</h2>')
        await user.should_see("Latest result")
        user.find(marker="saved-results").trigger("click", older)
        await user.should_see('<h2 class="d-bt-title">Older run</h2>')
        await user.should_see("Saved result")
        await user.should_see("Index that determines M: S&amp;P 500 alone (Settings Both indexes")


@pytest.mark.parametrize("choice, extra", [("leaders", {}), ("trend", {"trend_min_sales_growth_pct": -1e9})])
async def test_leaders_and_trend_leaders_run_from_the_backtest_page(settings, choice, extra):
    from test_backtest_approx import seed_approximate
    store = Store(settings.data.db_path)
    updated, start, end = seed_approximate(store, settings)
    if choice == "trend":
        extra = {**extra, "trend_min_rs": updated.thresholds.rs_min}
    current = configure(settings, updated, **extra)
    store.close()
    async with web(current) as user:
        await user.open("/backtest")
        user.find(marker=f"bt-strategy-{choice}").click()
        await asyncio.sleep(0.1)  # the panel redraws with the chosen strategy
        run_form(user, start, end)
        await user.should_see("Backtest saved.", retries=200)
        await user.should_see("How the portfolio was held")
        await user.should_see("Leaders" if choice == "leaders" else "Trend leaders")


async def test_a_scan_strategy_shows_its_research_runs_instead_of_run_controls(settings):
    async with web(settings) as user:
        await user.open("/backtest?strategy=minervini")
        await user.should_see("Written findings.")
        await user.should_not_see("Run a backtest")


async def test_every_strategy_runs_its_comparable_backtest_and_joins_the_comparison(settings, monkeypatch):
    from stratlib.web import backtest as page
    from test_sim import saved_result
    store, asked = Store(settings.data.db_path), []

    def run_all(_store, run_settings, strategy_ids, **options):
        asked.append(strategy_ids)
        return {sid: saved_result(store, run_settings, monkeypatch, sid) for sid in strategy_ids}
    monkeypatch.setattr(page, "run_all", run_all)
    try:
        async with web(settings) as user:
            await user.open("/backtest?strategy=minervini")
            await user.should_see("No comparable backtest of this strategy yet.")
            user.find(marker="engine-run").click()
            await user.should_see("Backtest saved.", retries=200)
            await user.should_see("end of test")
            await user.should_see("Written findings.")                     # the research runs stay below
            user.find(marker="engine-period-out_of_sample").click()
            await user.should_see("2022-01-01 to 2022-06-20")
            cagr = store.document(store.document_keys("backtest:engine:minervini:")[0])["results"]["combined"]["cagr"]
            await user.open("/strategies?strategy=minervini")
            await user.should_see(f"{cagr:.1f}%")
            await user.should_see("SPY CAGR, with dividends")
    finally:
        store.close()
    assert asked == [["minervini"]]


PAGE_PATHS = ("/", "/portfolio", "/market", "/positions", "/backtest", "/stock")


async def test_every_strategy_opens_on_every_page(settings, caplog):
    from stratlib.strategies import STRATEGIES
    store, _ = scan_market(settings)
    store.close()
    async with web(settings) as user:
        for strategy_id in STRATEGIES:
            for path in PAGE_PATHS:
                await user.open(f"{path}?strategy={strategy_id}")
                await user.should_see(marker="symbol" if path == "/stock" else "strategy")
                if path != "/stock":
                    assert value_of(user, "strategy").value == strategy_id
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert not errors, errors


async def test_portfolio_and_stock_detail_follow_a_saved_scan(settings, monkeypatch):
    from datetime import date
    from stratlib.scanning import run_scan
    from test_scan_strategies import NOW as SCAN_NOW
    store, days = scan_market(settings)
    report = run_scan(store, settings, strategy_id="ema_pullback", now=SCAN_NOW)
    assert [c["symbol"] for c in report["candidates"]] == ["UP1"]
    store.close()
    # Today is the scan's last session, for the plan and for the market it is checked against.
    for module in ("stratlib.web.portfolio", "stratlib.web.data"):
        monkeypatch.setattr(f"{module}.eod_cutoff", lambda *a: date.fromisoformat(days[-1]))
    async with web(settings) as user:
        await user.open("/portfolio?strategy=ema_pullback")
        await user.should_see('symbol=UP1"><b>UP1</b></a>')
        await user.should_see('<span class="d-pill tone-pass">Proposed buy</span>')
        await user.should_not_see("Review data")
        await user.should_see("Initial weight")
        await user.should_not_see("Initial slot")
        hypothetical = next(iter(user.find(marker="hypothetical").elements)).content
        assert 'symbol=UP1"><b>UP1</b>' in hypothetical
        await user.open("/stock?symbol=UP1&strategy=ema_pullback")
        await user.should_see("Traveling Trader 9/21 EMA signal")
        await user.should_see("Candidate")
        await user.open("/stock?symbol=FLAT&strategy=ema_pullback")
        await user.should_see("Not in the Traveling Trader 9/21 EMA screen of")


async def test_market_shows_a_scan_strategys_own_filter(settings):
    store, _ = scan_market(settings)
    store.close()
    async with web(settings) as user:
        await user.open("/market?strategy=episodic_pivot")
        await user.should_see("Market filter")
        await user.should_see("SPY above its 50-day average")


@pytest.mark.parametrize("strategy_id", ["qullamaggie", "minervini", "episodic_pivot", "ema_pullback", "tt_checklist",
                                         "nash_quality", "msci_garp"])
async def test_a_scan_positions_alert_appears_in_the_holdings_list(settings, strategy_id):
    from stratlib.sell_rules import Position
    from stratlib.strategies import STRATEGIES, current_rules
    store, days = scan_market(settings)
    entry = store.price_history("UP1", since=days[-30])[0]
    snapshot = current_rules(settings, strategy_id)
    record = store.save_position(Position("UP1", entry.date, entry.close, stop_price=entry.close * 0.5), as_of=days[-1],
                                 tracking={"strategy_id": strategy_id, "variant_id": snapshot["variant_id"],
                                           "rule_snapshot": snapshot})
    store.close()
    async with web(settings) as user:
        await user.open(f"/positions?strategy={strategy_id}")
        await user.should_see(f'data-pick="{record}"')
        await user.should_see(STRATEGIES[strategy_id].name + " · #")
        from nicegui import ui
        content = " ".join(e.content for e in user.find(kind=ui.html).elements)
        assert any(f'">{word}</span>' in content for word in ("Hold", "Sell", "Take profits"))


async def test_the_screen_refreshes_statements_for_a_statement_strategy(settings, monkeypatch):
    from datetime import date
    from stratlib import fundamental_scans as fs
    from stratlib.strategies import latest_screen
    from test_fundamental_scans import client_for, company_doc, seed as seed_companies
    store = Store(settings.data.db_path)
    doc = company_doc(first=date(2017, 9, 30))                               # statements that end in March 2023
    seed_companies(store, {"HAVE": {"doc": doc}, "NEEDS": {}}, sessions=200)
    store.close()
    client = client_for(doc)

    class Context:
        def __init__(self):
            self.client = client

        def close(self):
            pass
    monkeypatch.setattr("stratlib.app.open_context", lambda **kwargs: Context())
    monkeypatch.setattr("stratlib.config.load_fmp_api_key", lambda *a, **k: "test-key")
    monkeypatch.setattr("stratlib.logging_setup.configure_logging", lambda *a, **k: None)
    async with web(settings) as user:
        await user.open("/?strategy=nash_quality")
        box = value_of(user, "refresh")
        assert box.value and "statements" in box.text
        user.find(marker="run-screen").click()
        await user.should_see("Screen saved.", retries=200)
        # Price-only strategies offer no refresh at all.
        await user.open("/?strategy=qullamaggie")
        await user.should_not_see(marker="refresh")
    check = Store(settings.data.db_path)
    try:
        assert check.document(fs.STATEMENT_KEY + "NEEDS") is not None            # fetched, because it had no bundle
        assert client.get.call_count == 4                                        # one company, four statement sets
        assert latest_screen(check, "nash_quality")["counts"]["with_statements"] == 2
    finally:
        check.close()


async def test_a_jev_review_without_a_gateway_key_explains_what_to_add(settings, evidence, monkeypatch):
    from dataclasses import replace
    from stratlib.config import JevSettings
    from stratlib.universe import parse_listings
    bars, base, as_of = evidence
    # An empty environment wins over any real .env on the development machine.
    monkeypatch.setenv("AI_GATEWAY_API_KEY", " ")
    monkeypatch.setattr("stratlib.web.stock.stock_setup", lambda *a, **k: (bars, base))
    monkeypatch.setattr(Data, "market", lambda self: {"as_of": as_of, "history": [], "warnings": [], "state": None,
                                                      "exposure": None, "indexes": {}})
    store = Store(settings.data.db_path)
    store.replace_universe(parse_listings([{"symbol": "TEST", "companyName": "Test Corp", "exchangeShortName": "NYSE"}]))
    store.close()
    async with web(replace(settings, jev=JevSettings(enabled=True))) as user:
        await user.open("/stock?symbol=TEST")
        user.find(marker="jev-review").click()
        user.find(marker="jev-confirm").click()
        await user.should_see("AI_GATEWAY_API_KEY", retries=100)


async def test_msci_garp_opens_on_every_page(settings):
    from nicegui import ui
    from test_msci_garp import evening, garp_settings, live_market
    chosen = garp_settings(settings)
    store = Store(settings.data.db_path)
    m = live_market(store)
    rebuilt = run_screen(store, chosen, strategy_id="msci_garp", now=evening(m["days"][-1]))
    from stratlib import estimate_snapshots as ES, index_scans as IS
    from test_msci_garp import FUND_CSV
    store.save_document(IS.FUND_KEY, ES.ishares_holdings(FUND_CSV))      # the index's published holdings
    report = run_screen(store, chosen, strategy_id="msci_garp", now=evening(m["days"][-1]))
    store.close()
    top = report["members"][0]
    assert rebuilt["summary"]["source"] == "rebuild" and report["summary"]["source"] == "ishares"
    async with web(chosen) as user:
        await user.open("/strategies?strategy=msci_garp")
        await user.should_see("Growth at a reasonable price")
        await user.should_see("Hold the MSCI USA Quality GARP Select Index")
        await user.should_see(f"{len(report['members'])} in the latest screen")
        await user.should_see(marker="index-data-note")
        await user.should_see("by about 1.6 points a year from December 2015")
        await user.open("/?strategy=msci_garp")
        await user.should_see(f'data-symbol="{top}"')
        await user.should_see("The index changes only at its quarterly reviews")
        await user.should_see("Weight in the index, %")
        await user.should_see(marker="index-source")
        await user.should_see("Live index holdings")
        await user.should_see("as the iShares GARP ETF held it on 2026-09-30")
        content = " ".join(e.content for e in user.find(kind=ui.html).elements)
        assert "Index holdings" in content and "Rebuild match, %" in content and "Held by both" in content
        await user.open("/portfolio?strategy=msci_garp")
        await user.should_see("Each position takes its weight in the index today")
        await user.open("/backtest?strategy=msci_garp")
        await user.should_see("research/mscigarp_data.py prepare")
        await user.should_see(marker="index-data-note")
        await user.should_see("returned 16.0% a year against 17.6%")
        await user.open("/positions?strategy=msci_garp")
        await user.should_see("Alerts compare each holding with the index")
        await user.open("/market?strategy=msci_garp")
        await user.should_see("MSCI GARP is tested and run without a market rule")


async def test_a_report_shows_its_own_charts_and_its_address_serves_nothing_else(settings, research):
    from fastapi import HTTPException
    from nicegui import ui

    from stratlib.web.reports import image_route
    output = research / "output"
    (output / "chart.png").write_bytes(b"\x89PNG\r\n")
    (output / "study.md").write_text("# Title\n\nThe original finding.\n\n![Growth](chart.png)\n", encoding="utf-8")
    data = Data(settings)
    data.research_root = research
    try:
        serve = image_route(data)
        assert serve("entry-study", "chart.png").path == (output / "chart.png").resolve()
        for slug, path in [("entry-study", "baseline/results.json"), ("no-such-report", "chart.png")]:
            with pytest.raises(HTTPException):
                serve(slug, path)
        async with user_simulation(lambda: root(data)) as user:
            await user.open("/reports?report=entry-study")
            await user.should_see("The original finding.")
            assert any("![Growth](/report-files/entry-study/chart.png)" in element.content
                       for element in user.find(ui.markdown).elements)
    finally:
        data.close()
