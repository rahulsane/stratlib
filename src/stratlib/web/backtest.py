"""Backtest: every strategy's comparable backtest first (one set of rules for all of them, stratlib.sim),
then the older kinds. CANSLIM and Trend Leaders keep their approximate and strict replays beside the controls to run
another; the scan strategies keep their saved research runs."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from html import escape

from nicegui import run, ui

from .. import backtest_approx
from .. import app as context
from ..backtest import BacktestError
from ..backtest_data import capture_universe, coverage, import_archive, refresh_delistings, run_backtest
from ..config import ConfigError
from ..evidence import evidence_rows, verdict as research_verdict
from ..fmp import FMPError
from ..presentation import (APPROXIMATE, BACKTEST_KEYS, BACKTEST_MODES, BACKTEST_NAMES, BACKTEST_STRATEGIES,
                            INDEX_DATA_NOTE, INDEX_DATA_TITLE, OPEN_COLUMNS,
                            RESULTS, SKIPPED_COLUMNS, TRADE_COLUMNS, average_exposure, exposure_at, figure,
                            result_market_rule, result_name, result_strategy, rule_differences, without_provider)
from ..parallel import all_cores
from ..reports import load_catalog
from ..scoring import CRITERIA
from ..sim.engine import PERIOD_TITLES
from ..sim.runs import floor, json_safe, run_all, trade_rows
from ..strategies import INDEXES, STRATEGIES, current_rules
from . import parts
from .chart import backtest_chart, equity_chart
from .data import Data, Workspace
from .library import strategy_meta, strategy_select
from .reports import MARKDOWN, VariationComparison

TABS = {"trades": "Closed trades", "open": "Open at end", "skipped": "Skipped entries"}
SHOWN_ROWS = 500
# The comparable backtest's periods, in the order the switch shows them.
ENGINE_PERIODS = ("combined", "in_sample", "out_of_sample")
ENGINE_TRADES = {"ticker": "Symbol", "signal_date": "Signal", "entry_date": "Entry", "exit_date": "Exit",
                 "entry_price_as_traded": "Entry price, USD", "exit_price_as_traded": "Exit price, USD",
                 "position_value": "Position, USD", "return_pct": "Return, %", "pnl": "Profit, USD",
                 "holding_sessions": "Sessions held", "exit_reason": "Exit reason"}
def ground_rules(min_price: float, min_dollar_volume: float, strategy_id: str | None = None) -> str:
    """The shared rules, with the strategy's liquidity floor."""
    if strategy_id in INDEXES:
        return ("Every strategy runs under the same rules, so they compare: $100,000 to start; slippage of 0.10% a side, "
                "0.25% under $20; SPY with dividends as the benchmark. MSCI GARP copies its index in its own account, "
                "since the trade-by-trade engine cannot top up or trim a holding: at each review it buys and sells whole "
                "shares to bring every holding to the index's weight, it sells a stock that leaves the S&P 500, and it "
                "keeps dividends in cash until the next review. Each stock's holding period counts as one trade. The "
                "parent is the S&P 500 at each review, including companies since removed.")
    return ("All the strategies run under the same rules, so they compare: US common stocks, including "
            "those delisted since 2016; $100,000 to start; slippage of 0.10% a side, 0.25% under $20; stops filled "
            "during the day at the stop or a worse opening gap; SPY with dividends as the benchmark. Each keeps its own "
            f"entries, exits, position sizes, market rule and liquidity floor: this one buys nothing under "
            f"${min_price:g} or under ${min_dollar_volume / 1e6:g}M of average daily dollar volume on the signal day. "
            "Delisting records are thin before 2020, so the earlier years lean toward companies that survived.")


def cell(label: str, value) -> str:
    """A result value written by its column's unit."""
    if value is None or value == "":
        return "–"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if label.endswith(", USD") and isinstance(value, (int, float)):
        return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"
    if label.endswith(", %") and isinstance(value, (int, float)):
        return f"{value:+.1f}%"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def heading(label: str) -> str:
    for suffix in (", USD", ", %"):
        if label.endswith(suffix):
            return label[: -len(suffix)]
    return label


def tone(value) -> str:
    return "" if not isinstance(value, (int, float)) else "d-up" if value > 0 else "d-down" if value < 0 else ""


def rows_table(rows: list[dict], columns: dict[str, str], *, signed: frozenset = frozenset()) -> str:
    """Saved result rows as a plain table, with the signed column coloured by direction."""
    keys = [key for key in columns if key in rows[0]]
    numeric = {key for key in keys if columns[key].endswith((", USD", ", %")) or key == "shares"}
    head = "".join(f'<th class="{"d-num" if key in numeric else ""}">{escape(heading(columns[key]))}</th>' for key in keys)
    body = "".join("<tr>" + "".join(
        f'<td class="{"d-num" if key in numeric else ""} {tone(row.get(key)) if key in signed else ""}">'
        f'{escape(cell(columns[key], row.get(key)))}</td>' for key in keys) + "</tr>" for row in rows[:SHOWN_ROWS])
    more = (f'<p class="d-note">Showing the first {SHOWN_ROWS:,} of {len(rows):,}; the CSV holds them all.</p>'
            if len(rows) > SHOWN_ROWS else "")
    return f'<div class="d-table-wrap"><table class="d-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def csv_text(rows: list[dict], columns: dict[str, str]) -> str:
    keys = [key for key in columns if key in rows[0]]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([columns[key] for key in keys])
    writer.writerows([[row.get(key) for key in keys] for row in rows])
    return out.getvalue()


def snapshot_changes(saved: dict, now: dict) -> list[str]:
    """Settings that differ from a saved rule snapshot, as name: saved value (Settings value)."""
    now = json_safe(now)
    return [f"{key}: {(saved.get(group) or {}).get(key)} (Settings {value})"
            for group in ("thresholds", "params", "portfolio") for key, value in (now.get(group) or {}).items()
            if (saved.get(group) or {}).get(key) != value]


class BacktestPage:
    """One tab's Backtest page."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace = data, workspace
        spec = self.spec
        self.mode = "Approximate"
        self.strategy = spec.backtest_label if spec.backtest_label in BACKTEST_STRATEGIES else "Breakout trades"
        start, end = backtest_approx.default_period(data.store, data.settings)
        options = data.settings.backtest
        self.form = {"start": start.isoformat(), "end": end.isoformat(), "capital": float(options.initial_capital),
                     "holdings": options.max_holdings, "sample": 0, "label": "", "raise_cash": options.raise_cash}
        self.chosen: str | None = None
        self.period = "combined"
        self.tab = "trades"
        self.flash: tuple[str, str, str | None] | None = None
        self.running = False
        self.progress = {"message": "", "done": 0, "total": 1, "calls": None}
        self.archive: dict | None = None

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    @property
    def settings(self):
        return self.data.settings

    def build(self):
        self.ticker = ui.timer(0.3, self.update_progress, active=False)
        self.root = ui.element("div").classes("d-backtest")
        self.render()

    def saved(self) -> list[tuple[str, dict]]:
        store = self.data.store
        results = [(key, store.document(key)) for key in store.document_keys(RESULTS)[:12]]
        if not results and (latest := store.document("latest_backtest")):
            results = [(RESULTS + latest["created_at"], latest)]
        return [(key, report) for key, report in results if report]

    def render(self):
        spec = self.spec
        self.root.clear()
        with self.root:
            with ui.element("section").classes("d-head"):
                with ui.element("div").classes("d-head-left"):
                    ui.html("Backtest", sanitize=False, tag="h1").classes("d-title")
                    strategy_select(spec.id, self.change_strategy, disabled=self.running)
                meta = [*strategy_meta(spec, self.settings), "Replays strategy signals and compares the portfolio with SPY"]
                ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")
            self.notices = ui.element("div").classes("d-notices")
            self.show_notices()
            self.engine_doc = self.data.engine_result(spec.id)
            self.engine_view = ui.refreshable(self.engine_section)
            self.engine_view()
            if spec.scan:
                ui.html('<h2 class="d-bt-part">Research runs</h2>', sanitize=False)
                self.research(spec)
                return
            ui.html('<h2 class="d-bt-part">Approximate and strict backtests</h2><p class="d-policy">The app\'s earlier '
                    "replays of CANSLIM, Leaders and Trend Leaders, with dates, capital and holdings of your choice. They "
                    "trade without slippage and compare with SPY's price alone, so their figures differ from the "
                    "comparable backtest above.</p>", sanitize=False).classes("d-contents")
            # The public app shows saved results only: a run is long, and it writes the result.
            with ui.element("div").classes("d-bt-body" + (" is-single" if self.data.public else "")):
                with ui.element("div").classes("d-bt-results"):
                    self.results()
                if not self.data.public:
                    with ui.element("aside").classes("d-card d-bt-run").props('aria-label="Run a backtest"'):
                        self.run_panel()

    def show_notices(self):
        self.notices.clear()
        with self.notices:
            if self.running:
                self.progress_box = ui.html("", sanitize=False).classes("d-progress").props('role="status"')
                self.update_progress()
            if self.flash:
                tone_, text, detail = self.flash
                parts.notice(text, tone_, detail=detail)

    def update_progress(self):
        if not self.running:
            return
        p = self.progress
        share = min(p["done"] / max(p["total"], 1), 1.0)
        calls = f" · {p['calls']:,} API calls" if p["calls"] is not None else ""
        self.progress_box.content = (
            f'<div class="d-progress-head"><b>{escape(p["message"] or "Starting")}</b>'
            f'<span>{p["done"]:,} / {p["total"]:,}{calls}</span></div>'
            f'<div class="d-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
            f'aria-valuenow="{share * 100:.0f}"><i style="width:{share * 100:.1f}%"></i></div>')

    # The comparable backtest ---------------------------------------------------------------------------------------

    def engine_section(self):
        doc, spec = self.engine_doc, self.spec
        with ui.element("section").classes("d-card d-section d-engine").props('aria-label="Comparable backtest"'):
            with ui.element("div").classes("d-engine-head"):
                saved = (f"Data through {escape(doc['data_through'])} · saved {escape(doc['created_at'][:16].replace('T', ' '))} UTC"
                         if doc else "Not run yet")
                ui.html(f'<h2 class="d-bt-title">Comparable backtest</h2><p class="d-meta">{escape(spec.name)} · '
                        f"{escape(spec.variant_name)} · {saved}</p>", sanitize=False)
                if not self.data.public:
                    with ui.element("div").classes("d-run-actions"):
                        parts.button("Run all", on_click=lambda: self.run_engine(None),
                                     title="Backtests every strategy on one shared price panel").mark("engine-run-all").props(
                            "disabled" if self.running else "")
                        parts.button("Running…" if self.running else "Run again" if doc else "Run backtest", icon=parts.PLAY,
                                     primary=True, on_click=lambda: self.run_engine([spec.id])).mark("engine-run").props(
                            "disabled" if self.running else "")
            if spec.id in INDEXES:
                with ui.element("div").classes("d-notices"):
                    parts.notice(INDEX_DATA_TITLE, "caution", detail=INDEX_DATA_NOTE).mark("index-data-note")
            if not doc:
                ui.html("No comparable backtest of this strategy is in this snapshot." if self.data.public else
                        "No comparable backtest of this strategy yet. Run it here, or run every strategy from the command "
                        "line with <code>stratlib strategy-backtest</code>. CANSLIM and Trend Leaders also need the approximate "
                        "method's prepared data (Prepare data, below); MSCI GARP needs the S&P 500 history that "
                        "research/garp_data.py and research/mscigarp_data.py prepare.", sanitize=False, tag="p").classes("d-note")
                ui.html(escape(ground_rules(*floor(spec.id, self.settings), spec.id)), sanitize=False, tag="p").classes("d-policy")
                return
            periods = [p for p in ENGINE_PERIODS if p in doc["results"]]
            if self.period not in periods:
                self.period = periods[0]
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Test period"'):
                for period in periods:
                    ui.html(escape(PERIOD_TITLES.get(period, period)), sanitize=False, tag="button").classes(
                        "d-range" + (" is-current" if period == self.period else "")).props(
                        f'type="button" aria-pressed="{str(period == self.period).lower()}"').mark(f"engine-period-{period}").on(
                        "click", lambda period=period: self.change_period(period))
            m = doc["results"][self.period]
            spy = m.get("spy") or {}
            ratio = lambda value: "Unavailable" if value is None else f"{value:.2f}"
            figures = [("Total return", figure(m["total_return"]), f"SPY {figure(spy.get('total_return'))}", m["total_return"]),
                       ("CAGR", figure(m["cagr"]), f"SPY {figure(spy.get('cagr'))}", None),
                       ("Maximum drawdown", figure(m["max_drawdown"]), f"SPY {figure(spy.get('max_drawdown'))}", None),
                       ("Sharpe ratio", ratio(m["sharpe"]), f"SPY {ratio(spy.get('sharpe'))}", None),
                       ("Trades", f"{m['trades']:,}", f"{figure(m['win_rate'])} won", None)]
            ui.html('<div class="d-figures d-bt-figures">' + "".join(
                f'<div><b class="{tone(signed)}">{escape(value)}</b><span>{escape(label)} · {escape(note)}</span></div>'
                for label, value, note, signed in figures) + "</div>", sanitize=False).classes("d-contents").mark("engine-figures")
            equity_chart(m)
            # Results saved before the floor was a setting used the research's $5 and $20M.
            liquidity = doc.get("liquidity") or {"min_price": 5.0, "min_dollar_volume": 20e6}
            ui.html(f"{escape(m['start'])} to {escape(m['end'])}. SPY includes dividends. "
                    f"{escape(ground_rules(liquidity['min_price'], liquidity['min_dollar_volume'], doc['strategy_id']))}",
                    sanitize=False, tag="p").classes("d-note")
            if not self.data.public and (changes := snapshot_changes(doc.get("rule_snapshot") or {},
                                                                     current_rules(self.settings, spec.id))):
                parts.notice("Saved under rules that differ from current Settings; run it again to update.", "info",
                             detail="; ".join(changes[:12]) + ("; and more." if len(changes) > 12 else "."))
            self.engine_measures(m)
        self.engine_trades(doc)
        self.engine_inputs(doc, m)

    def engine_measures(self, m):
        spy = m.get("spy") or {}
        rows = [("Win rate", figure(m["win_rate"])), ("Average gain", figure(m["avg_win_pct"])),
                ("Average loss", figure(m["avg_loss_pct"])), ("Profit factor", figure(m["profit_factor"], "")),
                ("Average holding", "Unavailable" if m["avg_holding_sessions"] is None else
                 f"{m['avg_holding_sessions']:.0f} session{'' if round(m['avg_holding_sessions']) == 1 else 's'}"),
                ("Time holding a stock", figure(m["exposure_time_pct"])), ("Average invested", figure(m["exposure_avg_invested_pct"])),
                ("Best tenth of trades' profit, share of the net", figure(m["top10pct_profit_share"]))]
        measures = parts.table_html([{"measure": label, "value": value} for label, value in rows],
                                    [("measure", "Measure"), ("value", "Strategy")], numeric={"value"})
        years = []
        for year, value in (m.get("yearly") or {}).items():
            bench = (spy.get("yearly") or {}).get(year)
            gap = None if value is None or bench is None else value - bench
            years.append(f'<tr><td>{escape(year)}</td><td class="d-num {tone(value)}">{figure(value)}</td>'
                         f'<td class="d-num">{figure(bench)}</td><td class="d-num {tone(gap)}">'
                         f'{"–" if gap is None else f"{gap:+.1f} pp"}</td></tr>')
        table = ('<div class="d-table-wrap"><table class="d-table"><thead><tr><th>Year</th><th class="d-num">Strategy</th>'
                 '<th class="d-num">SPY with dividends</th><th class="d-num">Difference</th></tr></thead>'
                 f'<tbody>{"".join(years)}</tbody></table></div>')
        with ui.element("div").classes("d-grid-2 d-engine-tables"):
            ui.html(f'<h4>Trades</h4>{measures}<p class="d-note">Win and loss figures count closed trades; the last '
                    "session's holdings close at the end of the test. A share of the net above 100% means the other "
                    "trades lost money together.</p>", sanitize=False).mark("engine-measures")
            ui.html(f"<h4>Year by year</h4>{table}<p class=\"d-note\">Partial years at either end of the period.</p>",
                    sanitize=False).mark("engine-years")

    def engine_trades(self, doc):
        trades = sorted(trade_rows(doc["trades"].get(self.period) or {}), key=lambda t: (t["entry_date"], t["ticker"]))
        with ui.element("section").classes("d-card d-section d-engine-trades").props('aria-label="Comparable backtest trades"'):
            with ui.element("div").classes("d-section-head"):
                ui.html(f"<h3>Trades · {escape(PERIOD_TITLES.get(self.period, self.period))}</h3>"
                        f'<span>{len(trades):,} in entry order</span>', sanitize=False)
                if trades:
                    name = f"{doc['strategy_id']}-{self.period}-trades.csv"
                    parts.button("Trades CSV", on_click=lambda: ui.download.content(
                        csv_text(trades, ENGINE_TRADES), name, "text/csv")).classes("d-btn-quiet")
            ui.html(rows_table(trades, ENGINE_TRADES, signed=frozenset({"return_pct", "pnl"})) if trades else
                    '<p class="d-note">No trades in this period.</p>', sanitize=False).classes("d-contents").mark("engine-trades")

    def engine_inputs(self, doc, m):
        reasons = sorted((m.get("exit_reasons") or {}).items(), key=lambda kv: -kv[1])
        exits = "".join(f"<tr><td>{escape(reason)}</td><td class=\"d-num\">{count:,}</td></tr>" for reason, count in reasons)
        body = (f'<p>{escape(doc.get("universe", ""))}. Prices through {escape(doc["data_through"])}; the run took '
                f'{doc.get("elapsed_seconds") or 0:.0f} seconds.</p>'
                + (f'<h4>Exits</h4><div class="d-table-wrap"><table class="d-table"><tbody>{exits}</tbody></table></div>'
                   if exits else "")
                + "<h4>Strategy parameters</h4>" + parts.settings_html(doc.get("params") or {})
                + "<h4>Portfolio rules</h4>" + parts.settings_html(doc.get("rules") or {})
                + "<h4>Counts</h4>" + parts.settings_html(m.get("counts") or {}))
        with ui.element("details").classes("d-card d-details d-engine-inputs"):
            ui.html("Rules, exits and saved inputs", sanitize=False, tag="summary")
            with ui.element("div").classes("d-details-body"):
                if doc.get("description"):
                    ui.markdown(doc["description"], extras=MARKDOWN).classes("d-prose")
                ui.html(body, sanitize=False).classes("d-contents")
                with ui.element("div").classes("d-form-actions"):
                    parts.button("Download full result JSON", on_click=lambda: ui.download.content(
                        json.dumps(doc, indent=2), f"{doc['strategy_id']}-backtest.json", "application/json")).classes(
                        "d-btn-quiet")

    async def run_engine(self, strategy_ids):
        if self.running:
            return
        store, settings, data = self.data.store, self.settings, self.data

        def progress(message, done, total):
            self.progress = {"message": message, "done": done, "total": total, "calls": None}

        def work():
            results = run_all(store, settings, strategy_ids, workers=all_cores(), progress=progress)
            data.forget()
            return results
        await self.background(work, "Building the price panel", lambda results: (
            ("done", "Backtest saved." if len(results) == 1 else f"{len(results)} backtests saved.",
             "Every strategy's page and the Strategies comparison read the new results."), None))

    def change_period(self, period):
        self.period = period
        self.engine_view.refresh()

    # Saved results ----------------------------------------------------------------------------------------------

    def results(self):
        saved = self.saved()
        if not saved:
            ui.html("<h2>No saved backtest in this snapshot</h2>" if self.data.public else
                    "<h2>No saved backtest yet</h2><p>Choose dates, then check data coverage to see what a run needs. "
                    "The approximate method can download the data it needs.</p>", sanitize=False).classes("d-card d-empty")
            return
        keys = [key for key, _ in saved]
        if self.chosen not in keys:
            self.chosen = keys[0]
        if len(saved) > 1:
            self.comparison(saved)
        report = dict(saved)[self.chosen]
        self.report(report, latest=self.chosen == keys[0])

    def comparison(self, saved):
        rows = []
        for key, r in saved:
            m, spy = r["metrics"], r["spy_metrics"]
            selected = key == self.chosen
            rows.append(
                f'<div class="d-row{" is-selected" if selected else ""}" role="option" aria-selected="{str(selected).lower()}" '
                f'data-pick="{escape(key)}"><span class="d-sym"><b>{escape(result_name(r))}</b>'
                f'<small>{escape(result_strategy(r))} · {"Approximate" if r.get("mode") == APPROXIMATE else "Strict"} · '
                f'{escape(r["start"])} to {escape(r["end"])}</small></span>'
                f'<span class="d-num">{m["closed_trades"] + len(r["open_positions"])}</span>'
                f'<span class="d-num {tone(m["total_return_pct"])}">{figure(m["total_return_pct"])}</span>'
                f'<span class="d-num">{figure(spy["total_return_pct"])}</span>'
                f'<span class="d-num">{figure(m["max_drawdown_pct"])}</span>'
                f'<span class="d-num d-col-opt">{figure(m["win_rate_pct"])}</span>'
                f'<span class="d-num d-col-opt">{average_exposure(r):.0f}%</span>'
                f'<span class="d-dim d-col-opt">{escape(result_market_rule(r))}</span></div>')
        with ui.element("section").classes("d-card d-table-card d-bt-saved").props('aria-label="Saved results"'):
            ui.html('<div class="d-section-head"><h3>Saved results</h3><span>Pick one to show it below</span></div>',
                    sanitize=False).classes("d-bt-saved-head")
            scroll = ui.element("div").classes("d-scroll").mark("saved-results")
            scroll.on("click", lambda e: self.pick(e.args), js_handler=parts.PICK)
            with scroll:
                ui.html('<div class="d-rows d-bt-rows" role="listbox" aria-label="Saved results">'
                        '<div class="d-row d-row-head" aria-hidden="true"><span>Result</span><span class="d-num">Positions</span>'
                        '<span class="d-num">Return</span><span class="d-num">SPY</span><span class="d-num">Max drawdown</span>'
                        '<span class="d-num d-col-opt">Win rate</span><span class="d-num d-col-opt">Exposure</span>'
                        '<span class="d-col-opt">Market rule</span></div>' + "".join(rows) + "</div>",
                        sanitize=False).classes("d-contents")

    def report(self, report, *, latest: bool):
        metrics, spy = report["metrics"], report["spy_metrics"]
        approximate = report.get("mode") == APPROXIMATE
        title = report.get("label") or f"{report['start']} to {report['end']}"
        with ui.element("section").classes("d-card d-section d-bt-report").props('aria-label="Backtest result"'):
            ui.html(f'<h2 class="d-bt-title">{escape(title)}</h2>'
                    f'<p class="d-meta"><b>{"Latest result" if latest else "Saved result"}</b> · {escape(result_strategy(report))} · '
                    f'{"Approximate" if approximate else "Strict"} method · Market: {escape(result_market_rule(report))}</p>',
                    sanitize=False).classes("d-contents")
            figures = [(label, figure(metrics[key]), f"SPY {figure(spy[key])}", metrics[key] if key == "total_return_pct" else None)
                       for label, key in (("Total return", "total_return_pct"), ("CAGR", "cagr_pct"),
                                          ("Maximum drawdown", "max_drawdown_pct"))]
            figures += [("Win rate", figure(metrics["win_rate_pct"]), f"{metrics['closed_trades']} closed trades", None),
                        ("Ending value", f"${metrics['ending_equity']:,.0f}",
                         f"from ${report['portfolio']['initial_capital']:,.0f}", None)]
            ui.html('<div class="d-figures d-bt-figures">' + "".join(
                f'<div><b class="{tone(signed)}">{escape(value)}</b><span>{escape(label)} · {escape(note)}</span></div>'
                for label, value, note, signed in figures) + "</div>", sanitize=False).classes("d-contents")
            raise_cash = report["portfolio"].get("raise_cash", False)
            ui.html(f"{escape(report['start'])} to {escape(report['end'])} · saved {escape(report['created_at'][:19].replace('T', ' '))} "
                    f"UTC · {report['portfolio']['max_holdings']} maximum holdings"
                    + (" · raises cash when exposure falls" if raise_cash else "")
                    + ". The lower panel is the market's allowed exposure.", sanitize=False, tag="p").classes("d-note")
            differences = rule_differences(report, self.settings.thresholds)
            if differences:
                parts.notice("Rules that differ from the current ones." if self.data.public else
                             "Rules that differ from current Settings.", "info", detail="; ".join(differences) + ".")
            if approximate:
                counts, cover = report["signal_counts"], report["coverage"]
                sold = counts.get("delisting_exits_at_last_close", 0)
                parts.notice("Approximate result.", "info", detail=(
                    f"Statements are today's histories dated by filing, industries are today's, and "
                    f"{cover.get('delisted_members', 0):,} companies that later delisted are included."
                    + (f" {sold} {'holding was' if sold == 1 else 'holdings were'} sold at the last close on delisting." if sold else "")
                    + " Treat it as a check on how the rules behave, not a forecast."))
            if "criterion_rejections" in report and not report["trades"] and not report["open_positions"]:
                self.no_trades(report)
            if report.get("sample"):
                parts.notice(f"Development sample: at most {report['sample']} survivors evaluated per session.", "caution",
                             detail="Ranks use the full universe. These are not full-strategy results.")
            backtest_chart(report, [exposure_at(p) for p in report["equity_curve"]])
            measures = [{"measure": label, "strategy": figure(metrics[key]), "spy": figure(spy[key])}
                        for label, key in (("CAGR", "cagr_pct"), ("Maximum drawdown", "max_drawdown_pct"),
                                           ("Total return", "total_return_pct"))]
            measures += [{"measure": label, "strategy": figure(metrics[key], suffix), "spy": "Not applicable"}
                         for label, key, suffix in (("Win rate", "win_rate_pct", "%"), ("Average gain", "average_gain_pct", "%"),
                                                    ("Average loss", "average_loss_pct", "%"),
                                                    ("Gain-to-loss ratio", "gain_loss_ratio", ""))]
            ui.html(parts.table_html(measures, [("measure", "Measure"), ("strategy", result_strategy(report)), ("spy", "SPY")],
                                     numeric={"strategy", "spy"})
                    + f'<p class="d-note">{metrics["closed_trades"]} closed trades. Win and loss statistics exclude open '
                    "positions; the equity curve includes their last closing value. The SPY comparison excludes dividends.</p>",
                    sanitize=False).classes("d-contents").mark("measures")
        if "criterion_rejections" in report:
            self.funnel(report)
        elif report.get("strategy") in BACKTEST_NAMES:
            self.held(report)
        self.lists(report)
        self.inputs(report)

    def no_trades(self, report):
        counts, days = report["signal_counts"], report["market_days"]
        open_days = sum(exposure_at(p) > 0 for p in report["equity_curve"])
        reasons = [f"the market allowed new buying on {open_days} of {sum(days.values())} sessions",
                   f"{counts.get('fundamental_pass_sessions', 0):,} of {counts.get('candidate_sessions', 0):,} candidate "
                   "stock-days passed the C/A/S/L rule"]
        buyable = counts.get("buyable_sessions", counts.get("breakout_sessions", 0))
        reasons.append(f"{buyable} of those were buyable after a breakout, but not on a day the market allowed buying"
                       if buyable else "none of those was in the buy zone after a valid breakout")
        parts.notice("No trades under the current rules.", "caution", detail="; ".join(reasons).capitalize() + "."
                     + ("" if self.data.public else
                        " The rules are the thresholds in Settings; loosen them and run again to see how the result changes."))

    def funnel(self, report):
        counts, metrics, days = report["signal_counts"], report["metrics"], report["market_days"]
        limited = sum(s["reason"].startswith("Market exposure limit") for s in report["skipped_entries"])
        closed_market = sum(exposure_at(p) == 0 for p in report["equity_curve"])
        steps = [("Candidate stock-days", counts.get("candidate_sessions", 0), "passed price and RS"),
                 ("Passed C/A/S/L", counts.get("fundamental_pass_sessions", 0), "the passing rule"),
                 ("Buyable stock-days", counts.get("buyable_sessions", counts.get("breakout_sessions", 0)),
                  f"{counts['breakouts']} breakouts" if "breakouts" in counts else "valid base, volume"),
                 ("Blocked by market", counts.get("breakouts_blocked_by_market", 0) + limited,
                  f"{limited} at the exposure limit" if limited else "no buying allowed"),
                 ("Positions opened", metrics["closed_trades"] + len(report["open_positions"]), "")]
        candidates = max(counts.get("candidate_sessions", 0) - counts.get("survivor_sessions_without_statements", 0), 1)
        labels = {key: f"{letter} · {label}" for key, letter, label in CRITERIA}
        rejections = sorted(({"check": labels.get(key, key), "failed": 100 * c.get("failed", 0) / candidates,
                              "unavailable": 100 * c.get("unavailable", 0) / candidates, "only": c.get("only_blocker", 0)}
                             for key, c in report["criterion_rejections"].items()), key=lambda r: -r["failed"])
        bars = "".join(
            f'<tr><td>{escape(r["check"])}</td><td class="d-num"><span class="d-bar"><i style="width:{min(r["failed"], 100):.1f}%">'
            f'</i></span>{r["failed"]:.1f}%</td><td class="d-num">{r["unavailable"]:.1f}%</td><td class="d-num">{r["only"]:,}</td></tr>'
            for r in rejections)
        blocked = ", ".join(f"{b['symbol']} ({b['date']}, {b['market']})" for b in report["blocked_breakouts"][:20])
        with ui.element("section").classes("d-card d-section").props('aria-label="How candidates were filtered"'):
            ui.html('<div class="d-section-head"><h3>How candidates were filtered</h3><span>A stock-day is one stock on one '
                    'session</span></div>', sanitize=False).classes("d-contents")
            with ui.element("div").classes("d-steps d-bt-steps"):
                for label, count, note in steps:
                    ui.html(f"<strong>{count:,}</strong><span>{escape(label)}</span><small>{escape(note)}</small>",
                            sanitize=False).classes("d-step is-static d-count-step d-plan-step")
            ui.html(f"Market direction: confirmed uptrend on {days.get('confirmed uptrend', 0)} of {sum(days.values())} sessions, "
                    f"uptrend under pressure on {days.get('uptrend under pressure', 0)}, correction on {days.get('correction', 0)}. "
                    f"Average allowed exposure {average_exposure(report):.0f}%; no new buying on {closed_market} sessions.",
                    sanitize=False, tag="p").classes("d-note")
            parts.details("Which checks rejected candidates",
                          '<div class="d-table-wrap"><table class="d-table"><thead><tr><th>Check</th><th class="d-num">Failed</th>'
                          '<th class="d-num">Unavailable</th><th class="d-num">Only failing check</th></tr></thead>'
                          f"<tbody>{bars}</tbody></table></div><p class=\"d-note\">Shares of candidate stock-days with statements. "
                          "Unavailable means the data could not measure the check, for example cash flow against negative EPS; "
                          "it also rejects the candidate. Only failing check counts candidates that passed everything else.</p>"
                          + (f"<p>Breakouts that met every rule except market direction: {escape(blocked)}</p>" if blocked else ""))

    def held(self, report):
        curve, counts = report["equity_curve"], report["signal_counts"]
        years = max((len(curve) - 1) / 252, 1 / 252)
        trend = report.get("strategy") == "trend"
        steps = [("Trend leaders" if trend else "Stocks passing the Screen", f"{counts.get('average_eligible', 0):,}",
                  "per day, on average"),
                 ("Holdings", f"{sum(p['holdings'] for p in curve) / len(curve):.1f}", "on average"),
                 ("Invested", f"{100 * sum(1 - p['cash'] / p['equity'] for p in curve) / len(curve):.0f}%",
                  f"allowed {average_exposure(report):.0f}%"),
                 ("Sales per year", f"{len(report['trades']) / years:.0f}",
                  "trend breaks and the loss cap" if trend else "rebalances, stops and exposure cuts")]
        with ui.element("section").classes("d-card d-section").props('aria-label="How the portfolio was held"'):
            ui.html('<div class="d-section-head"><h3>How the portfolio was held</h3><span></span></div>', sanitize=False).classes("d-contents")
            with ui.element("div").classes("d-steps d-bt-steps"):
                for label, value, note in steps:
                    ui.html(f"<strong>{escape(value)}</strong><span>{escape(label)}</span><small>{escape(note)}</small>",
                            sanitize=False).classes("d-step is-static d-count-step d-plan-step")

    def lists(self, report):
        with ui.element("section").classes("d-card d-section").props('aria-label="Trades"'):
            self.trade_lists = ui.refreshable(lambda: self.trade_list(report))
            self.trade_lists()

    def trade_list(self, report):
        counts = {"trades": len(report["trades"]), "open": len(report["open_positions"]),
                  "skipped": len(report["skipped_entries"])}
        with ui.element("div").classes("d-section-head"):
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Trade lists"'):
                for key, name in TABS.items():
                    ui.html(f"{escape(name)} <span class=d-count-badge>{counts[key]:,}</span>", sanitize=False,
                            tag="button").classes("d-range" + (" is-current" if key == self.tab else "")).props(
                        f'type="button" aria-pressed="{str(key == self.tab).lower()}"').mark(f"tab-{key}").on(
                        "click", lambda key=key: self.change_tab(key))
            if self.tab == "trades" and report["trades"]:
                parts.button("Trades CSV", on_click=lambda: ui.download.content(
                    csv_text(report["trades"], TRADE_COLUMNS), "backtest-trades.csv", "text/csv")).classes("d-btn-quiet")
        if self.tab == "trades":
            body = (rows_table(report["trades"], TRADE_COLUMNS, signed=frozenset({"return_pct", "pnl"})) if report["trades"] else
                    '<p class="d-note">No closed trades in this run. See the open positions and the entry counts.</p>')
        elif self.tab == "open":
            body = (rows_table(report["open_positions"], OPEN_COLUMNS, signed=frozenset({"unrealized_pnl"})) if report["open_positions"]
                    else '<p class="d-note">No open positions at the final close.</p>')
        else:
            body = (rows_table(report["skipped_entries"], SKIPPED_COLUMNS) if report["skipped_entries"] else
                    '<p class="d-note">No scheduled entries were skipped.</p>')
        ui.html(body, sanitize=False).classes("d-contents").mark("trade-list")

    def inputs(self, report):
        notes = "".join(f"<p>{escape(without_provider(note))}</p>" for note in report["limitations"])
        parts.details("Rules, data coverage and saved inputs",
                      notes + "<p>Statement and universe observations use a conservative 13:00 Eastern information cutoff, "
                      "including regular sessions, so half-day filings cannot leak into a closing signal.</p><p>Stops use daily "
                      "lows and fill at the stop or a worse opening gap. Profit targets use closes and execute at the next open. "
                      "The fast-gain hold uses closes from the breakout pivot.</p>"
                      + parts.json_html({"coverage": report["coverage"], "counts": report["signal_counts"],
                                         "ETF volume proxy sessions": report["market_proxy_sessions"],
                                         "input_digest": report["input_digest"]})
                      + "<h4>Thresholds</h4>" + parts.settings_html(report["thresholds"]))
        with ui.element("div").classes("d-form-actions"):
            parts.button("Download full result JSON", on_click=lambda: ui.download.content(
                json.dumps(report, indent=2), "backtest-result.json", "application/json")).classes("d-btn-quiet")

    # Running ------------------------------------------------------------------------------------------------------

    def run_panel(self):
        approximate = self.mode == "Approximate"
        ui.html("Run a backtest", sanitize=False, tag="h3").classes("d-side-title")
        with ui.element("div").classes("d-ranges").props('role="group" aria-label="Method"'):
            for name in BACKTEST_MODES:
                ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if name == self.mode else "")).props(
                    f'type="button" aria-pressed="{str(name == self.mode).lower()}"').mark(f"mode-{name}").on(
                    "click", lambda name=name: self.change(mode=name))
        ui.html(escape(BACKTEST_MODES[self.mode]), sanitize=False, tag="p").classes("d-hint")
        if approximate:
            with ui.element("div").classes("d-choice-list").props('role="radiogroup" aria-label="Strategy"'):
                for name, text in BACKTEST_STRATEGIES.items():
                    ui.html(f"<b>{escape(name)}</b><span>{escape(text)}</span>", sanitize=False, tag="button").classes(
                        "d-choice" + (" is-current" if name == self.strategy else "")).props(
                        f'type="button" role="radio" aria-checked="{str(name == self.strategy).lower()}"').mark(
                        f"bt-strategy-{BACKTEST_KEYS.get(name, 'breakout')}").on("click", lambda name=name: self.change(strategy=name))
        form = self.form
        with ui.element("div").classes("d-form-grid"):
            ui.input("Start date", value=form["start"], on_change=lambda e: form.update(start=e.value or "")).mark(
                "bt-start").classes("d-form-field").props("dense outlined hide-bottom-space stack-label type=date")
            ui.input("End date", value=form["end"], on_change=lambda e: form.update(end=e.value or "")).mark(
                "bt-end").classes("d-form-field").props("dense outlined hide-bottom-space stack-label type=date")
            ui.number("Starting capital, USD", value=form["capital"], min=1, step=1000, format="%.0f",
                      on_change=lambda e: form.update(capital=e.value)).classes("d-form-field").props(
                "dense outlined hide-bottom-space stack-label")
            ui.number("Maximum holdings", value=form["holdings"], min=1, step=1, format="%d",
                      on_change=lambda e: form.update(holdings=e.value)).classes("d-form-field").props(
                "dense outlined hide-bottom-space stack-label")
            if not (approximate and self.strategy in BACKTEST_KEYS):
                ui.number("Development sample, survivors per session (0 for all)", value=form["sample"], min=0, step=10,
                          format="%d", on_change=lambda e: form.update(sample=e.value)).classes("d-form-field d-span-2").props(
                    "dense outlined hide-bottom-space stack-label")
            ui.input("Label, optional", value=form["label"], placeholder="For example: S&P 500 market rule",
                     on_change=lambda e: form.update(label=e.value or "")).mark("bt-label").classes("d-form-field d-span-2").props(
                "dense outlined hide-bottom-space stack-label")
        ui.checkbox("Raise cash when the market's allowed exposure falls below current holdings", value=form["raise_cash"],
                    on_change=lambda e: form.update(raise_cash=e.value)).classes("d-check-box").props(
            'title="Sells the weakest holdings (lowest return since entry) at the next open."')
        with ui.element("div").classes("d-run-actions"):
            parts.button("Check data coverage", on_click=self.check).mark("bt-check").props("disabled" if self.running else "")
            if approximate:
                parts.button("Prepare data", on_click=self.confirm_prepare,
                             title="Downloads delisted companies, splits and statement histories").mark(
                    "bt-prepare").props("disabled" if self.running else "")
            parts.button("Running…" if self.running else "Run backtest", icon=parts.PLAY, primary=True,
                         on_click=self.start_run).mark("bt-run").props("disabled" if self.running else "")
        if not approximate:
            self.strict_tools()

    def strict_tools(self):
        form = self.form
        with ui.element("details").classes("d-details d-strict"):
            ui.html("Prepare archived data", sanitize=False, tag="summary")
            with ui.element("div").classes("d-details-body d-strict-body"):
                ui.html("Import a dated research archive in the README format. It must contain the securities and "
                        "classifications observed on each test date, including companies that later delisted.",
                        sanitize=False, tag="p").classes("d-note")
                ui.upload(label="Dated research archive (.json)", auto_upload=True, max_files=1,
                          on_upload=self.received).props('accept=.json flat bordered dense').classes("d-upload")
                parts.button("Import archive", on_click=self.import_archive).mark("bt-import").props(
                    "" if self.archive else "disabled")
                ui.html("The delisted-company directory verifies coverage. A fresh universe capture starts an archive for future "
                        "runs; it does not fill earlier dates. Capture before 13:00 Eastern. Both make API calls.",
                        sanitize=False, tag="p").classes("d-note")
                with ui.element("div").classes("d-form-actions"):
                    parts.button("Refresh delisting directory", on_click=lambda: self.fmp_task("delistings")).classes("d-btn-quiet")
                    parts.button("Capture today's universe", on_click=lambda: self.fmp_task("capture")).classes("d-btn-quiet")
                ui.html(f'<pre class="d-code">stratlib backtest-prepare --start {escape(form["start"])} --end '
                        f'{escape(form["end"])} --sample 50</pre>', sanitize=False)

    def options(self):
        form = self.form
        if not form["start"] or not form["end"]:
            raise ValueError("Choose a start and an end date.")
        return replace(self.settings.backtest, initial_capital=float(form["capital"] or 0) or self.settings.backtest.initial_capital,
                       max_holdings=int(form["holdings"] or 1), raise_cash=bool(form["raise_cash"]))

    def coverage_status(self) -> dict:
        check = backtest_approx.check if self.mode == "Approximate" else coverage
        return check(self.data.store, self.settings, self.form["start"], self.form["end"])

    async def check(self):
        try:
            status = await run.io_bound(self.coverage_status)
        except (BacktestError, ConfigError, ValueError) as exc:
            self.flash = ("error", str(exc), None)
        else:
            if not status["ready"]:
                self.flash = ("error", "The data does not cover this run yet.", " ".join(status["errors"]))
            elif self.mode == "Approximate":
                self.flash = ("done", f"Ready: {status['sessions']} sessions.",
                              f"Data prepared {status['prepared']['prepared_at'][:10]}.")
            else:
                self.flash = ("done", f"Universe snapshots cover {status['sessions']} sessions.",
                              "The run also checks index, price, split and statement evidence before saving results.")
        self.show_notices()

    async def start_run(self):
        if self.running:
            return
        try:
            options = self.options()
            status = await run.io_bound(self.coverage_status)
        except (BacktestError, ConfigError, ValueError) as exc:
            self.flash = ("error", str(exc), None)
            self.show_notices()
            return
        if not status["ready"]:
            self.flash = ("error", "The data does not cover this run yet.", " ".join(status["errors"]))
            self.show_notices()
            return
        store, settings, form = self.data.store, self.settings, self.form
        start, end, label = form["start"], form["end"], (form["label"] or "").strip()
        sample = int(form["sample"] or 0) or None

        def progress(message, done, total):
            self.progress = {"message": message, "done": done, "total": total, "calls": 0}

        def strict_progress(day, done, total):
            self.progress = {"message": f"{day}", "done": done, "total": total, "calls": 0}

        def work():
            if self.mode == "Approximate" and self.strategy in BACKTEST_KEYS:
                return backtest_approx.run_leaders(store, settings, start, end, portfolio=options, label=label,
                                                   strategy=BACKTEST_KEYS[self.strategy], progress=progress,
                                                   workers=all_cores())
            if self.mode == "Approximate":
                return backtest_approx.run(store, settings, start, end, portfolio=options, sample=sample, label=label,
                                           progress=progress, workers=all_cores())
            return run_backtest(store, settings, start, end, portfolio=options, sample=sample, label=label,
                                progress=strict_progress)
        await self.background(work, "Loading prices", lambda report: (
            ("done", "Backtest saved.", "The result is shown first."), RESULTS + report["created_at"]))

    def confirm_prepare(self):
        with ui.dialog() as dialog, ui.element("div").classes("d-card d-dialog"):
            ui.html(f"<h3>Prepare data?</h3><p>This downloads delisted companies, splits and statement histories for "
                    f"{escape(self.form['start'])} to {escape(self.form['end'])}. A first preparation of several years takes "
                    "thousands of API calls and can run for an hour; cached pieces are reused, so repeating it is cheap.</p>",
                    sanitize=False)
            with ui.element("div").classes("d-form-actions"):
                parts.button("Cancel", on_click=dialog.close)

                async def go():
                    dialog.close()
                    await self.prepare()
                parts.button("Prepare data", primary=True, on_click=go).mark("bt-prepare-confirm")
        dialog.open()

    async def prepare(self):
        settings, form = self.settings, self.form

        def work():
            ctx = context.open_context(settings=settings)
            try:
                def progress(message, done, total):
                    self.progress = {"message": message, "done": done, "total": total, "calls": ctx.client.stats.api_calls}
                return backtest_approx.prepare(ctx.client, ctx.store, settings, form["start"], form["end"], progress=progress)
            finally:
                ctx.close()
        await self.background(work, "Preparing data", lambda result: ((
            "done", f"Prepared · {result['api_calls']:,} API calls.",
            f"{result['members']:,} stocks, including {result['delisted_members']:,} that later delisted. Statement histories "
            f"for {result['survivor_count']:,} stocks that passed the price and RS filters; "
            f"{result['statements_unavailable']:,} unavailable."), None), calls=True)

    def fmp_task(self, kind):
        settings = self.settings

        def work():
            ctx = context.open_context(settings=settings)
            try:
                if kind == "delistings":
                    result = refresh_delistings(ctx.client, ctx.store, progress=lambda message, calls: self.progress.update(
                        message=message, calls=calls))
                    return {"text": f"{len(result['rows']):,} delisting records cached.", "calls": ctx.client.stats.api_calls}
                capture_universe(ctx.client, ctx.store, settings)
                return {"text": "Today's dated universe was saved.", "calls": ctx.client.stats.api_calls}
            finally:
                ctx.close()

        async def go():
            await self.background(work, "Loading data", lambda result: (
                ("done", result["text"], f"{result['calls']:,} API calls."), None), calls=True)
        return go()

    async def received(self, event):
        try:
            self.archive = json.loads(await event.file.read())
        except (ValueError, UnicodeError) as exc:
            self.archive = None
            self.flash = ("error", "The archive is not valid JSON.", str(exc))
        else:
            self.flash = ("info", "Archive received.", "Import archive adds its dated observations.")
        self.render()

    def import_archive(self):
        try:
            result = import_archive(self.data.store, self.archive or {})
        except (BacktestError, ValueError, KeyError, TypeError) as exc:
            self.flash = ("error", "The archive could not be imported.", str(exc))
        else:
            self.archive = None
            self.flash = ("done", f"Imported {result['observations']} dated observations.", None)
        self.render()

    async def background(self, work, message, done, *, calls: bool = False):
        """Run slow work off the page's thread, with progress, then show the outcome and the new result if any."""
        self.running, self.flash = True, None
        self.progress = {"message": message, "done": 0, "total": 1, "calls": 0 if calls else None}
        self.render()
        self.ticker.activate()
        try:
            result = await run.io_bound(work)
        except (BacktestError, ConfigError, FMPError, ValueError) as exc:
            self.flash = ("error", str(exc), "Any saved result is kept.")
        else:
            self.flash, chosen = done(result)
            if chosen:
                self.chosen = chosen
        finally:
            self.ticker.deactivate()
            self.running = False
            self.render()

    # Research runs for the scan strategies ---------------------------------------------------------------------------

    def research(self, spec):
        index = spec.id in INDEXES
        ui.html(f"The research behind {escape(spec.name)}: copying MSCI's own index (its official daily levels, as the ETF "
                "or by hand) against the rebuild these rules follow. MSCI's index uses analysts' forecasts the rebuild "
                "cannot see, so it returned more." if index else
                f"The research behind {escape(spec.name)}: the variations it compared on the same engine, over a panel of "
                "stocks and ETFs. The comparable backtest above runs the rules in Settings on stocks alone, so its figures "
                "can differ from these.", sanitize=False, tag="p").classes("d-policy d-bt-intro")
        with ui.element("div").classes("d-notices"):
            if note := research_verdict(spec.id):
                parts.notice(note, "info", detail="Past results are not a forecast.")
        reports, issues = load_catalog(self.data.research_root)
        for issue in issues:
            parts.notice(issue, "caution")
        mine = [r for r in reports if r.slug in spec.reports and r.runs]
        rows = evidence_rows(spec.id, self.data.research_root) if index else []
        if rows:
            shown = [{key: (f"{value:,.2f}" if isinstance(value, float) else value) for key, value in row.items()}
                     for row in rows]
            ui.html(parts.table_html(shown, [(key, key) for key in rows[0]], numeric=set(rows[0]) - {"Run", "Period"})
                    + '<p class="d-note">From research/output/mscigarp/results.json: $100,000 from 18 December 2015, '
                    "SPY with dividends.</p>", sanitize=False).classes("d-contents").mark("index-study")
        elif not mine:
            ui.html("This strategy's research has no saved portfolio runs to compare here. Its written findings are linked "
                    "below.", sanitize=False, tag="p").classes("d-note")
        for index, report in enumerate(mine):
            with ui.element("details").classes("d-card d-details d-research").props("open" if index == 0 else ""):
                ui.html(escape(report.title), sanitize=False, tag="summary")
                with ui.element("div").classes("d-details-body"):
                    ui.html(escape(report.summary) + (f" {escape(report.note)}" if report.note else ""), sanitize=False,
                            tag="p").classes("d-note")
                    VariationComparison(report).build()
        titles = {r.slug: r.title for r in reports}
        links = [f'<a href="/reports?report={slug}">{escape(titles[slug])}</a>' for slug in spec.reports if slug in titles]
        ui.html("<p><b>Written findings.</b> " + (" · ".join(links) or '<a href="/reports">Browse research reports</a>')
                + "</p>" + ("" if self.data.public else
                            '<p class="d-note">To run a new variation, use the scripts in research/ (see research/README.md), '
                            "then publish the result in research/reports.json.</p>"),
                sanitize=False).classes("d-links d-bt-links")

    # Events -------------------------------------------------------------------------------------------------------

    def change_strategy(self, key):
        if key == self.workspace.strategy:
            return
        self.workspace.strategy = key
        spec = self.spec
        if spec.backtest_label in BACKTEST_STRATEGIES:
            self.strategy = spec.backtest_label
        if key == "trend":
            self.mode = "Approximate"
        self.flash = None
        ui.navigate.history.replace(f"/backtest?strategy={key}")
        self.render()

    def change(self, **values):
        for name, value in values.items():
            setattr(self, name, value)
        self.flash = None
        self.render()

    def change_tab(self, key):
        self.tab = key
        self.trade_lists.refresh()

    def pick(self, key):
        if key and key != self.chosen and not self.running:
            self.chosen = key
            self.render()
