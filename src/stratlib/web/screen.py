"""Screen page: the workspace strategy's saved screen. A market card and the screen's funnel, then a dense results
list beside the selected stock's chart and evidence. Running a screen and reopening a saved run live in the header."""

from __future__ import annotations

from dataclasses import asdict
from html import escape

from nicegui import run, ui

# Modules, not names: the data access is looked up at call time, so it can be replaced in tests.
from .. import app as context, config, logging_setup
from ..config import ConfigError, Thresholds
from ..fmp import FMPError
from ..presentation import (CANSLIM, COUNT_LABELS, FILTERED, LABELS, TABLE_NOTE, TABLE_NOTES, change_pct,
                            criterion_number, funnel_steps, index_source, index_table_note, letter_states, scan_note,
                            scope_rows, shown, tone, verdict)
from ..scanning import SCANNERS
from ..scoring import CRITERIA
from ..screening import ScreenError, run_screen
from ..strategies import INDEXES, STRATEGIES, current_rules, project_screen, rule_snapshot
from ..parallel import all_cores
from ..technical import TECHNICAL_CRITERIA
from . import parts
from .chart import RANGES, price_chart
from .data import Data, Workspace
from .library import strategy_library, strategy_meta, strategy_select

ALL_CRITERIA = [*CRITERIA, *TECHNICAL_CRITERIA]
SCOPE_NOUN = {"all": "stocks", "survivors": "survivors", "casl": "pass C/A/S/L", "all_rules": "pass every rule",
              "candidates": "entry candidates"}
ANY_CHECK = "Any check"
RESULTS = {"Fail": "Fails", "Pass": "Passes", "Unavailable": "Not measured"}
MIN_RS = ["Any", "70", "80", "90", "95"]
SECTORS = "All sectors"


def kind_of(spec) -> str:
    return "scan" if spec.scan else "trend" if spec.id == "trend" else "canslim"


class ScreenPage:
    """One tab's Screen page and the choices on it."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.saved = "Latest"
        self.refresh, self.sample = True, 0
        self.flash: tuple[str, str, str | None] | None = None
        self.running = False
        self.progress = {"message": "", "done": 0, "total": 1, "calls": 0}
        self.range = "6M"
        self.selected: str | None = None
        self.rows: list[dict] = []
        self.report: dict | None = None
        self.reset_filters()

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    @property
    def kind(self) -> str:
        return kind_of(self.spec)

    def reset_filters(self):
        self.scope, self.search, self.sector, self.min_rs = "survivors", "", SECTORS, "Any"
        self.check, self.result = ANY_CHECK, "Fail"
        self.order, self.descending = ("RS" if self.kind == "canslim" else "rank"), True

    # Building -------------------------------------------------------------------------------------------------

    def build(self):
        # Outside the part that redraws, so a run's progress keeps updating while the page changes.
        self.ticker = ui.timer(0.3, self.update_progress, active=False)
        ui.keyboard(on_key=self.keys)
        self.root = ui.element("div").classes("d-screen")
        self.render()

    def render(self):
        """Everything from the header down; filters keep their values."""
        self.report = self.data.screen(self.spec.id, None if self.saved == "Latest" else self.saved)
        self.load_rows()
        self.root.clear()
        with self.root:
            self.header()
            self.notices = ui.element("div").classes("d-notices")
            self.show_notices()
            if not self.report:
                self.empty()
            else:
                with ui.element("section").classes("d-summary").props('aria-label="Market and screen funnel"'):
                    self.market()
                    self.funnel()
                with ui.element("div").classes("d-body"):
                    with ui.element("section").classes("d-card d-table-card").props('aria-label="Screener results"'):
                        self.filters()
                        scroll = ui.element("div").classes("d-scroll").mark("results")
                        scroll.on("click", lambda e: self.pick(e.args), js_handler=parts.PICK)
                        with scroll:
                            self.table = ui.refreshable(self.rows_html)
                            self.table()
                        foot = self.table_note()
                        if foot:
                            ui.html(escape(foot), sanitize=False).classes("d-table-foot")
                    with ui.element("aside").classes("d-card d-detail").props('aria-label="Selected stock"'):
                        self.detail = ui.refreshable(self.detail_panel)
                        self.detail()
            with ui.element("div").classes("d-extras"):
                self.extras()

    def header(self):
        spec, report = self.spec, self.report
        with ui.element("section").classes("d-head"):
            with ui.element("div").classes("d-head-left"):
                ui.html("Screen", sanitize=False, tag="h1").classes("d-title")
                strategy_select(spec.id, self.change_strategy, disabled=self.running)
                history = self.data.screen_history(spec.id)
                if history:
                    runs = {"Latest": "Latest run", **{key: f"{key.split(':')[-2]} · Run {key.split(':')[-1][:8]}"
                                                       for key in history}}
                    ui.select(runs, value=self.saved if self.saved in runs else "Latest",
                              on_change=lambda e: self.change_saved(e.value)).mark("saved-run").classes("d-field d-saved").props(
                        'dense outlined hide-bottom-space options-dense popup-content-class="d-popup" aria-label="Saved run"')
            if not self.data.public:  # the public app reads published screens as saved
                self.actions()
            meta = strategy_meta(spec, self.settings)
            if report:
                meta.append(f"Prices through <b>{escape(report['price_date'])}</b>")
                if report.get("fundamental_as_of"):
                    meta.append(f"Statements {escape(report['fundamental_as_of'])}")
                if report.get("created_at"):
                    meta.append(f"Saved {escape(report['created_at'][:16].replace('T', ' '))}")
                if report.get("api_calls") is not None:
                    meta.append(f"{report['api_calls']:,} API calls · {report.get('elapsed_seconds', 0):.1f}s")
                if report.get("base_count") is not None:
                    meta.append(f"{report['base_count']:,} bases detected")
            ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")

    def actions(self):
        with ui.element("div").classes("d-actions"):
            if self.spec.id == "trend" and self.data.store.document("latest_screen"):
                parts.button("Evaluate saved data", icon=parts.REDO, on_click=self.evaluate,
                             title="Apply the Trend Leaders rules to the saved CANSLIM screen facts. No API calls.").mark("evaluate")
            with ui.element("div").classes("d-anchor"):
                parts.button("Run options", icon=parts.TUNE)
                with ui.menu().classes("d-popup d-menu"):
                    self.run_options()
            self.run_button = parts.button("Running…" if self.running else "Run screen", icon=parts.PLAY, primary=True,
                                           on_click=self.start_run).mark("run-screen")
            if self.running:
                self.run_button.props("disabled")

    def run_options(self):
        spec = self.spec
        with ui.element("div").classes("d-menu-body"):
            if spec.scan:
                if spec.id == "episodic_pivot":
                    self.option_box("Fetch earnings dates (one API call)",
                                    "The earnings gap rule needs release dates. Without them gap signals are listed as unverified.")
                elif spec.id == "msci_garp":
                    self.option_box("Refresh the S&P 500 list and statements",
                                    "Refreshes the S&P 500 list and change log (two calls, at most weekly) and fetches five "
                                    "statement sets for each member missing from the cache or that has reported since its "
                                    "last fetch: up to about 2,500 calls after a full earnings season. Unchecked, the "
                                    "screen reads the cached list and statements only.")
                elif SCANNERS[spec.id].needs_api:
                    self.option_box("Refresh company statements",
                                    "Fetches four statement sets for each company missing from the cache or that has reported "
                                    "since its last fetch. A first run on an empty cache takes thousands of calls; unchecked, "
                                    "the screen reads the cached statements only.")
                text = ("The index reads cached completed prices for the S&P 500's members. Run the daily backfill to "
                        "update them." if spec.id == "msci_garp" else
                        "Scans read cached completed prices for the whole universe. Run the daily backfill to update them."
                        + ("" if SCANNERS[spec.id].needs_api else " This scan makes no API calls."))
            else:
                self.option_box("Refresh survivor fundamentals",
                                "Unchecked, the screen reads cached statements only and makes no API calls.")
                ui.html("Development sample of survivors, 0 for all", sanitize=False, tag="span").classes("d-menu-label")
                ui.number(value=self.sample, min=0, step=10, format="%d",
                          on_change=lambda e: setattr(self, "sample", int(e.value or 0))).classes("d-field d-number").props(
                    'dense outlined hide-bottom-space aria-label="Development sample of survivors, 0 for all"')
                text = "RS and industry ranks always use the full cached universe. Run the daily backfill to update prices."
            ui.html(escape(text), sanitize=False, tag="p").classes("d-hint")

    def option_box(self, label: str, help_text: str):
        ui.checkbox(label, value=self.refresh, on_change=lambda e: setattr(self, "refresh", e.value)).mark("refresh").classes(
            "d-check-box")
        ui.html(escape(help_text), sanitize=False, tag="p").classes("d-hint")

    def show_notices(self):
        self.notices.clear()
        with self.notices:
            if self.running:
                self.progress_box = ui.html("", sanitize=False).classes("d-progress").props('role="status"')
                self.update_progress()
            if self.flash:
                tone_, text, detail = self.flash
                parts.notice(text, tone_, detail=detail)
            for tone_, text in self.warnings():
                parts.notice(text, tone_)

    def warnings(self) -> list[tuple[str, str]]:
        report, spec, settings = self.report, self.spec, self.settings
        if not report:
            return []
        out = []
        # Only the owner can act on advice to run the screen again; the public app shows the screen as saved.
        owner = not self.data.public
        if self.kind == "canslim":
            if owner and (asdict(Thresholds(**report["thresholds"])) != asdict(settings.thresholds)
                          or report.get("rule_snapshot") and report["rule_snapshot"] !=
                          rule_snapshot(spec.id, settings.thresholds, settings.backtest)):
                out.append(("caution", "Settings have changed since this screen. Run it again to apply the current config."))
            if owner and report.get("phase") != 3:
                out.append(("info", "This saved screen predates Phase 3. Run the screen to add bases and the N and M checks; "
                                    "Run options can leave out the API calls."))
        elif self.kind == "trend":
            if owner and report.get("rule_snapshot") != rule_snapshot("trend", settings.thresholds, settings.backtest):
                out.append(("caution", "Settings have changed. Run or evaluate a screen to apply the current rules."))
        else:
            if owner and report.get("rule_snapshot") != current_rules(settings, spec.id):
                out.append(("caution", "Settings have changed since this screen. Run it again to apply the current rules."))
            state = report.get("market_filter") or {}
            if state and state.get("code") != "A" and not state.get("on"):
                out.append(("caution", f"The market filter is off ({(state.get('label') or '').lower()}). The research takes "
                                       "no new signals while it is off, so there are no candidates. Setups it held back "
                                       "are listed below."))
        return out

    def update_progress(self):
        if not self.running:
            return
        p = self.progress
        share = min(p["done"] / max(p["total"], 1), 1.0)
        self.progress_box.content = (
            f'<div class="d-progress-head"><b>{escape(p["message"] or "Starting")}</b>'
            f'<span>{p["done"]:,} / {p["total"]:,} · {p["calls"]:,} API calls this run</span></div>'
            f'<div class="d-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
            f'aria-valuenow="{share * 100:.0f}"><i style="width:{share * 100:.1f}%"></i></div>')

    def market(self):
        spec, report = self.spec, self.report
        if spec.id in INDEXES:
            ui.html(parts.source_card(*index_source(report)), sanitize=False).classes("d-contents").mark("index-source")
        elif spec.scan:
            code = self.settings.strategies[spec.id].market_filter
            ui.html(parts.filter_card(spec.name, report.get("market_filter"), code), sanitize=False).classes("d-contents")
        else:
            market = report.get("market") if self.kind == "canslim" and report.get("market") else self.data.market()
            ui.html(parts.market_card(market, holdings=self.settings.backtest.max_holdings), sanitize=False).classes("d-contents")

    def funnel(self):
        report = self.report
        with ui.element("div").classes("d-card d-funnel").props('aria-label="Screen funnel"'):
            with ui.element("div").classes("d-steps").props('role="tablist"' if self.kind == "canslim" else ""):
                if self.kind == "canslim":
                    for key, label, count in funnel_steps(report):
                        current = key == self.scope
                        ui.html(f"<strong>{count:,}</strong><span>{escape(label)}</span>", sanitize=False, tag="button").classes(
                            "d-step" + (" is-current" if current else "") + ("" if count else " is-zero")).props(
                            f'type="button" role="tab" aria-selected="{str(current).lower()}"').on(
                            "click", lambda key=key: self.change_scope(key))
                else:
                    index = self.spec.id in INDEXES
                    if self.kind == "trend":
                        steps = [("Universe", report["universe_count"]), ("Evaluated", report["evaluated_count"])]
                    elif index:            # an index has no liquidity screen; its holdings are the last step
                        steps = [(COUNT_LABELS.get(key, (key, None))[0], value)
                                 for key, value in (report.get("counts") or {}).items() if key != "held"]
                    else:
                        steps = [("Liquid stocks", report.get("eligible_count", report.get("evaluated_count", 0))),
                                 *[(COUNT_LABELS.get(key, (key, None))[0], value)
                                   for key, value in (report.get("counts") or {}).items()]]
                    if "index_held" not in (report.get("counts") or {}):
                        steps.append(("Holdings" if index else "Candidates", len(report.get("candidates") or [])))
                    for label, count in steps:
                        ui.html(f"<strong>{count:,}</strong><span>{escape(label)}</span>", sanitize=False).classes(
                            "d-step is-static" + (" is-current" if label == "Candidates" else "") + ("" if count else " is-zero"))
            self.coverage()

    def coverage(self):
        """What the screen measured, and its data warnings, behind one quiet button."""
        report = self.report
        warnings = report.get("warnings") or []
        if self.kind == "canslim":
            label = f"{report['ranked_count']:,} / {report['universe_count']:,} ranked"
            if report.get("sample"):
                label += f" · sample of {report['evaluated_count']}"
            if report.get("cache_only"):
                label += " · cache only"
        elif warnings:
            label = f"{len(warnings)} data note{'s' if len(warnings) != 1 else ''}"
        else:
            return
        with ui.element("div").classes("d-anchor d-coverage"):
            parts.button(label, icon=parts.DATABASE).classes("d-btn-quiet")
            with ui.menu().classes("d-popup d-menu"):
                with ui.element("div").classes("d-menu-body"):
                    ui.html("Data coverage", sanitize=False, tag="h3").classes("d-menu-title")
                    for warning in warnings or ["No coverage warnings."]:
                        ui.html(escape(warning), sanitize=False, tag="p").classes("d-menu-line")

    def filters(self):
        kind = self.kind
        with ui.element("div").classes("d-filters"):
            search = ui.input(placeholder="Search symbol or company", value=self.search,
                              on_change=lambda e: self.change(search=e.value or "")).mark("search").classes(
                "d-field d-search").props('dense outlined hide-bottom-space clearable debounce=250 '
                                          'aria-label="Search symbol or company"')
            with search.add_slot("prepend"):
                ui.html('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>',
                        sanitize=False).classes("d-field-icon")
            ui.select(self.sectors(), value=self.sector, on_change=lambda e: self.change(sector=e.value)).classes(
                "d-field d-sector").props('dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                                          'aria-label="Sector"')
            orders = self.orders()
            with ui.element("div").classes("d-group"):
                ui.select(orders, value=self.order if self.order in orders else next(iter(orders)),
                          on_change=lambda e: self.change(order=e.value)).classes("d-field d-order").props(
                    'dense outlined hide-bottom-space options-dense prefix="Rank by" popup-content-class="d-popup" '
                    'aria-label="Rank by"')
                parts.button("", icon=parts.DOWN if self.descending else parts.UP,
                             title="Highest first" if self.descending else "Lowest first",
                             on_click=lambda: self.change(descending=not self.descending)).classes("d-btn-icon").props(
                    f'aria-label="{"Highest first" if self.descending else "Lowest first"}; select to reverse"')
            ui.select(MIN_RS, value=self.min_rs, on_change=lambda e: self.change(min_rs=e.value)).classes(
                "d-field d-minrs").props('dense outlined hide-bottom-space options-dense prefix="Min RS" '
                                         'popup-content-class="d-popup" aria-label="Minimum RS"')
            if kind == "canslim":
                with ui.element("div").classes("d-group"):
                    checks = [ANY_CHECK, *[LABELS[key] for key, _, _ in ALL_CRITERIA]]
                    ui.select(checks, value=self.check, on_change=lambda e: self.change(check=e.value)).mark("check").classes(
                        "d-field d-check-filter").props('dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                                                        'aria-label="Filter by check"')
                    if self.check != ANY_CHECK:
                        ui.select(RESULTS, value=self.result, on_change=lambda e: self.change(result=e.value)).mark("result").classes(
                            "d-field d-result").props('dense outlined hide-bottom-space options-dense '
                                                      'popup-content-class="d-popup" aria-label="Check result"')
            self.count = ui.html("", sanitize=False).classes("d-count")
            self.update_count()

    def update_count(self):
        noun = SCOPE_NOUN.get(self.scope, "stocks") if self.kind == "canslim" else "candidates"
        self.count.content = f"<b>{len(self.rows):,}</b> {escape(noun)}"

    def table_note(self) -> str | None:
        if self.kind == "scan":
            return index_table_note(self.report) if self.spec.id in INDEXES else scan_note(self.spec)
        if self.kind == "trend":
            return "Every candidate passes the saved Trend Leaders rules. Market exposure is applied in Portfolio."
        return None

    # Rows -----------------------------------------------------------------------------------------------------

    def orders(self) -> dict[str, str]:
        if self.kind == "canslim":
            return {"RS": "RS", "change": "6M change", "passed": "Checks passed", "Price": "Price",
                    **{LABELS[key]: LABELS[key] for key, _, _ in ALL_CRITERIA}}
        return {"rank": "Strategy rank", "RS": "RS", "change": "6M change"}

    def sectors(self) -> list[str]:
        report = self.report or {}
        listings = self.data.listings()
        names = {r.get("sector") or listings.get(r["symbol"], {}).get("sector") or "Unclassified"
                 for r in report.get("rows") or report.get("candidates") or []}
        return [SECTORS, *sorted(names)]

    def load_rows(self):
        report = self.report
        if not report:
            self.rows, self.selected = [], None
            return
        listings = self.data.listings()
        if self.kind == "canslim":
            rows = [{"symbol": r["symbol"], "name": r["name"], "rs": r["rs"], "close": r["prices"]["close"],
                     "sector": r["sector"], "industry": r["industry"], "row": r} for r in scope_rows(report, self.scope)]
        else:
            rows = [{"symbol": c["symbol"], "name": c.get("name"), "rs": c.get("rs"), "close": c.get("close"),
                     "sector": c.get("sector"), "industry": c.get("industry"), "candidate": c,
                     "rank": c.get("rank") or i} for i, c in enumerate(report.get("candidates") or [], 1)]
        for r in rows:
            info = listings.get(r["symbol"], {})
            r["sector"] = r["sector"] or info.get("sector") or "Unclassified"
            r["industry"] = r["industry"] or info.get("industry")
            r["name"] = r["name"] or info.get("name")
        if self.sector != SECTORS:
            rows = [r for r in rows if r["sector"] == self.sector]
        if self.search:
            needle = self.search.lower()
            rows = [r for r in rows if needle in f"{r['symbol']} {r['name'] or ''}".lower()]
        if self.min_rs != "Any":
            rows = [r for r in rows if (r["rs"] or 0) >= int(self.min_rs)]
        if self.kind == "canslim" and self.check != ANY_CHECK:
            rows = [r for r in rows if verdict(next((c for c in r["row"]["criteria"] if LABELS[c["key"]] == self.check),
                                                    {"passed": None})["passed"]) == self.result]
        closes = self.data.closes(tuple(r["symbol"] for r in rows), report["price_date"]) if rows else {}
        for r in rows:
            r["series"] = closes.get(r["symbol"], [])
            r["change"] = change_pct(r["series"])
            if "row" in r:
                r["letters"] = letter_states(r["row"])
                r["passed"] = sum(c["passed"] is True for c in r["row"]["criteria"])
        self.rows = sort_rows(rows, self.order if self.order in self.orders() else next(iter(self.orders())),
                              self.descending)
        if self.selected not in {r["symbol"] for r in self.rows}:
            self.selected = self.rows[0]["symbol"] if self.rows else None

    def last_column(self) -> tuple[str, str]:
        """The trailing column: the base for CANSLIM, the signal date for scans that have one, else the industry."""
        if self.kind == "canslim":
            return "base", "Base"
        if self.kind == "scan" and any(r["candidate"].get("signal_date") for r in self.rows):
            return "signal", "Signal"
        return "industry", "Industry"

    def rows_html(self):
        if not self.rows:
            report = self.report or {}
            if self.kind == "canslim" and report.get("rows") or self.kind != "canslim" and report.get("candidates"):
                text = "No stocks match these filters. Clear the search or the check filter, or pick another funnel step."
            elif self.kind == "trend":
                text = "No stocks meet the Trend Leaders rules in this saved screen."
            else:
                text = "No stocks meet the rules on the last completed close."
            ui.html(text, sanitize=False, tag="p").classes("d-empty-line")
            return
        letters = self.kind == "canslim"
        ranked = not letters
        key, heading = self.last_column()
        classes = "d-rows" + ("" if letters else " no-letters") + (" has-rank" if ranked else "")
        head_letters = ('<span class="d-letters">' + "".join(f"<i>{letter}</i>" for letter, _ in CANSLIM) + "</span>"
                        if letters else "")
        out = [f'<div class="{classes}" role="listbox" aria-label="Results">'
               f'<div class="d-row d-row-head" aria-hidden="true">{"<span class=d-num>#</span>" if ranked else ""}'
               f'<span>Symbol</span><span class="d-col-spark">6M</span>'
               f'<span class="d-num d-col-last">Last</span><span class="d-num">Chg 6M</span><span class="d-num d-rs">RS</span>'
               f'{head_letters}<span class="d-col-base">{heading}</span></div>']
        for r in self.rows:
            filtered = "row" in r and r["row"]["stage"] in FILTERED
            change = r["change"]
            change_tone = "" if change is None else "d-up" if change >= 0 else "d-down"
            cells = ""
            if letters:
                cells = '<span class="d-letters">' + "".join(
                    f'<i class="is-{mark}" title="{letter}: {"not measured" if mark == "none" else mark}">{letter}</i>'
                    for letter, mark in r["letters"]) + "</span>"
            if key == "base":
                last = (r["row"].get("base") or {}).get("pattern") or ""
            elif key == "signal":
                last = r["candidate"].get("signal_date") or ""
            else:
                last = r["industry"] or ""
            selected = r["symbol"] == self.selected
            rs = "" if r["rs"] is None else f"{r['rs']:.0f}"
            out.append(
                f'<div class="d-row{" is-selected" if selected else ""}{" is-filtered" if filtered else ""}" role="option" '
                f'aria-selected="{str(selected).lower()}" data-pick="{escape(r["symbol"])}" data-symbol="{escape(r["symbol"])}">'
                + (f'<span class="d-num d-dim">{r["rank"]}</span>' if ranked else "")
                + f'<span class="d-sym"><b>{escape(r["symbol"])}</b><small>{escape(r["name"] or "")}</small></span>'
                f'{parts.sparkline(r["series"])}<span class="d-num d-col-last">{parts.money(r["close"])}</span>'
                f'<span class="d-num {change_tone}">{parts.pct(change)}</span>'
                f'<span class="d-num d-rs">{rs}</span>'
                f'{cells}<span class="d-dim d-col-base">{escape(str(last))}</span></div>')
        out.append("</div>")
        ui.html("".join(out), sanitize=False).classes("d-contents")

    # The selected stock ---------------------------------------------------------------------------------------

    def detail_panel(self):
        report = self.report
        r = next((x for x in self.rows if x["symbol"] == self.selected), None)
        if not report or not r:
            ui.html("Pick a row to see its chart and checks.", sanitize=False, tag="p").classes("d-empty-line")
            return
        # Stock detail opens on the stock last looked at here.
        self.workspace.symbol = r["symbol"]
        info = self.data.listings().get(r["symbol"], {})
        row = r.get("row") or {}
        candidate = r.get("candidate") or {}
        prices = row.get("prices") or {}
        exchange = row.get("exchange") or info.get("exchange") or ""
        where = " · ".join(v for v in (r["sector"], r["industry"]) if v)
        change = r["change"]
        facts = [f"Close {report['price_date']}"]
        if prices.get("below_high_pct") is not None:
            facts.append(f"{prices['below_high_pct']:.1f}% below 52-week high")
        if r["rs"] is not None:
            facts.append(f"RS {r['rs']:.0f}")
        industry = (row.get("industry_stats") or {}).get("rank")
        if industry is not None:
            facts.append(f"Industry #{industry} of {report.get('industry_count', '?')}")
        if candidate.get("ret63_pct") is not None:
            facts.append(f"63-day return {candidate['ret63_pct']:+.1f}%")
        if candidate.get("weight_pct") is not None:
            facts.append(f"Weight {candidate['weight_pct']:.1f}%")
        with ui.element("div").classes("d-detail-inner"):
            with ui.element("div").classes("d-sym-head"):
                ui.html(f'<div class="d-sym-id"><b>{escape(r["symbol"])}</b>'
                        f'{f"<span class=d-tag>{escape(exchange)}</span>" if exchange else ""}</div>'
                        f'<p class="d-sym-name">{escape(r["name"] or r["symbol"])}</p>'
                        f'<p class="d-sym-where">{escape(where)}</p>', sanitize=False)
                with ui.element("div").classes("d-sym-actions"):
                    ui.html(f"Open{parts.ARROW}", sanitize=False, tag="a").classes("d-btn").props(
                        f'href="/stock?symbol={escape(r["symbol"])}" aria-label="Open {escape(r["symbol"])} in Stock detail"')
                    ui.html("Close", sanitize=False, tag="button").classes("d-btn d-sheet-close").props(
                        'type="button" aria-label="Close the stock panel"')
            ui.html(f'<div class="d-price"><strong>{parts.money(r["close"])}</strong>'
                    f'<span class="{"d-up" if (change or 0) >= 0 else "d-down"}">{parts.pct(change)}</span><small>6M</small></div>'
                    f'<p class="d-facts">{" · ".join(escape(f) for f in facts)}</p>', sanitize=False)
            chips = self.verdicts(r)
            if chips:
                ui.html(parts.verdict_chips(chips), sanitize=False).classes("d-contents")
            with ui.element("div").classes("d-ranges").props('role="tablist" aria-label="Chart range"'):
                for name in RANGES:
                    ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if name == self.range else "")).props(
                        f'type="button" role="tab" aria-selected="{str(name == self.range).lower()}"').on(
                        "click", lambda name=name: self.change_range(name))
            sessions = RANGES[self.range]
            base = row.get("base") or {}
            price_chart(self.data.bars(r["symbol"], report["price_date"], sessions + 199), sessions, base=base)
            if base.get("pattern"):
                ui.html(f'<b>{escape(base["pattern"])}</b> · {base["length_weeks"]} weeks · {base["depth_pct"]:.1f}% deep · '
                        f'pivot {parts.money(base["pivot"])} · {base["distance_pct"]:+.1f}% from pivot',
                        sanitize=False, tag="p").classes("d-note")
            elif base.get("reason"):
                ui.html(escape(base["reason"]), sanitize=False, tag="p").classes("d-note")
            if row and row.get("stage") in FILTERED:
                ui.html(f"Stopped at the {escape(row['stage'].lower())}: {escape(prices.get('reason') or 'no reason saved')}. "
                        "Criteria are scored only for survivors.", sanitize=False, tag="p").classes("d-note")
            elif row:
                self.checks(row)
            elif self.kind == "trend":
                self.trend_fields(candidate)
            else:
                self.signal_fields(candidate)
            ui.html("<kbd>J</kbd> <kbd>K</kbd> or the arrow keys step through the rows", sanitize=False, tag="p").classes("d-hint")

    def verdicts(self, r) -> list[tuple[str, str, str]]:
        row, report = r.get("row"), self.report
        if row and row.get("stage") not in FILTERED:
            chips = [("C/A/S/L", verdict(row["phase2_pass"]), tone(verdict(row["phase2_pass"])))]
            if report.get("phase") == 3:
                chips.append(("C/A/N/S/L/M", verdict(row.get("phase3_pass")), tone(verdict(row.get("phase3_pass")))))
            return chips
        if row:
            return [("Stopped at", row["stage"], "none")]
        total = len(report.get("candidates") or [])
        if self.kind == "trend":
            return [("Trend leader", f"#{r['rank']} of {total}", "pass")]
        chips = [("Candidate", f"#{r['rank']} of {total}", "pass")]
        if r["candidate"].get("signal_date"):
            chips.append(("Signal", r["candidate"]["signal_date"], "none"))
        return chips

    def checks(self, row):
        passed = sum(c["passed"] is True for c in row["criteria"])
        out = [f'<div class="d-section-head"><h3>CANSLIM checks</h3><span>{passed} of {len(row["criteria"])} pass · '
               f'I is assessed by hand</span></div>', parts.checks_html(row["criteria"])]
        for warning in row.get("warnings") or []:
            out.append(f'<p class="d-note">{escape(warning)}</p>')
        ui.html("".join(out), sanitize=False).classes("d-contents")

    def trend_fields(self, candidate):
        fields = [("RS", f"{candidate['rs']:.0f}" if candidate.get("rs") is not None else "No data"),
                  ("Sales growth", f"{candidate['sales']:+.1f}%" if candidate.get("sales") is not None else "No data"),
                  ("Industry rank", f"{candidate['industry_rank']}" if candidate.get("industry_rank") is not None else "No data")]
        rows = "".join(f'<div class="d-check"><span>{escape(label)}</span><span class="d-num">{escape(value)}</span>'
                       f'<span class="d-pill tone-pass">Pass</span></div>' for label, value in fields)
        ui.html(f'<div class="d-section-head"><h3>Trend Leaders rules</h3><span>From the saved screen</span></div>'
                f'<div class="d-checks">{rows}</div><p class="d-note">The rules also require the price filters and enough daily '
                f'trading value. Exits are a close below the long moving average.</p>', sanitize=False).classes("d-contents")

    def signal_fields(self, candidate):
        spec = self.spec
        rows = "".join(f'<div class="d-check is-plain"><span>{escape(label)}</span>'
                       f'<span class="d-num">{escape(shown(key, candidate.get(key)))}</span></div>'
                       for key, label in SCANNERS[spec.id].candidate_columns if candidate.get(key) is not None)
        ui.html(f'<div class="d-section-head"><h3>Signal</h3><span>From the saved screen</span></div>'
                f'<div class="d-checks">{rows}</div><p class="d-note">'
                f'{escape(index_table_note(self.report) if spec.id in INDEXES else TABLE_NOTES.get(spec.id, TABLE_NOTE))}</p>',
                sanitize=False).classes("d-contents")

    # Below the results ----------------------------------------------------------------------------------------

    def extras(self):
        spec, report = self.spec, self.report
        if report and self.kind == "scan":
            base = [("symbol", "Symbol"), ("name", "Company"), ("close_text", "Close")]
            groups = [("triggered", "Buy stops already triggered",
                       [("signal_date", "Signal"), ("entry_date", "Triggered"), ("entry_text", "Entry"),
                        ("volume_text", "Volume confirmed")],
                       "The pivot traded through after the setup, so the research already holds these. A later purchase "
                       "chases the breakout and is not one of its trades."),
                      ("blocked", "Held back by the market filter", [("signal_date", "Signal"), ("reason", "Reason")], None),
                      ("skipped", "Skipped by the strategy's rules",
                       [("stop_text", "Stop"), ("signal_date", "Signal"), ("reason", "Reason")], None)]
            for key, title, columns, footnote in groups:
                items = report.get(key) or []
                if not items:
                    continue
                rows = [{**item, "close_text": shown("close", item.get("close")), "entry_text": shown("entry_price", item.get("entry_price")),
                         "stop_text": shown("stop", item.get("stop")), "volume_text": shown("volume_confirmed", item.get("volume_confirmed"))}
                        for item in items]
                wanted = [(k, label) for k, label in [*base, *columns] if any(row.get(k) is not None for row in rows)]
                parts.details(title, parts.table_html(rows, wanted, numeric={"close_text", "entry_text", "stop_text"})
                              + (f'<p class="d-note">{escape(footnote)}</p>' if footnote else ""), count=len(items))
            parts.details("Saved strategy rules", parts.json_html(report.get("rule_snapshot")))
        if report and self.kind == "canslim":
            parts.details("Screen definitions and thresholds",
                          '<p>RS weights four separate 63-session returns 2:1:1:1. Tied scores share an average percentile rank. '
                          "Industry ranks use the equal-weight 126-session returns of all eligible group members.</p>"
                          + parts.settings_html(report["thresholds"]))
        strategy_library(self.settings, spec, public=self.data.public)

    def empty(self):
        spec = self.spec
        if self.data.public:
            ui.html(f"<h2>No saved {escape(spec.name)} screen</h2><p>This strategy was not screened for this snapshot. Its "
                    "rules and research are below, and its saved results are on Backtest and Reports.</p>",
                    sanitize=False).classes("d-card d-empty")
            return
        if spec.scan:
            text = f"Run the screen to apply the {spec.name} rules to the cached completed prices."
            code = f".venv\\Scripts\\stratlib backfill\n.venv\\Scripts\\stratlib screen --strategy {spec.id}"
        elif spec.id == "trend":
            text = ("Evaluate the saved CANSLIM screen with the Trend Leaders rules, or run the screen to rank cached prices "
                    "and fetch statements for the survivors.")
            code = ".venv\\Scripts\\stratlib backfill\n.venv\\Scripts\\stratlib screen --strategy trend"
        else:
            text = "Run the screen to rank cached prices and fetch statements for stocks that pass the price and RS filters."
            code = ".venv\\Scripts\\stratlib backfill\n.venv\\Scripts\\stratlib screen --sample 50"
        ui.html(f'<h2>No saved {escape(spec.name)} screen yet</h2><p>{escape(text)} Run screen above does the same from here; '
                f'from a terminal:</p><pre class="d-code">{escape(code)}</pre>', sanitize=False).classes("d-card d-empty")

    # Events ---------------------------------------------------------------------------------------------------

    def change_strategy(self, key):
        if key == self.workspace.strategy:
            return
        self.workspace.strategy = key
        self.saved, self.flash, self.selected = "Latest", None, None
        self.reset_filters()
        ui.navigate.history.replace(f"{parts.screen_path(self.data.public)}?strategy={key}")
        self.render()

    def change_saved(self, key):
        self.saved, self.flash = key, None
        self.render()

    def change_scope(self, scope):
        self.scope = scope
        self.render()

    def change(self, **values):
        """A filter or ordering change: the list, the panel and the count follow."""
        rebuild = "check" in values or "descending" in values
        for name, value in values.items():
            setattr(self, name, value)
        self.load_rows()
        if rebuild:
            self.render()
            return
        self.table.refresh()
        self.detail.refresh()
        self.update_count()

    def change_range(self, name):
        self.range = name
        self.detail.refresh()

    def pick(self, symbol):
        if symbol and symbol != self.selected:
            self.selected = symbol
            self.detail.refresh()

    def keys(self, event):
        if not event.action.keydown or event.key.name not in {"j", "k", "ArrowDown", "ArrowUp"} or not self.rows:
            return
        symbols = [r["symbol"] for r in self.rows]
        index = symbols.index(self.selected) if self.selected in symbols else 0
        step = 1 if event.key.name in {"j", "ArrowDown"} else -1
        self.selected = symbols[max(0, min(len(symbols) - 1, index + step))]
        ui.run_javascript(f"window.desk && desk.select({self.selected!r})")
        self.detail.refresh()

    def evaluate(self):
        source = self.data.store.document("latest_screen")
        try:
            self.data.store.save_screen(project_screen(source, "trend", self.settings.thresholds, self.settings.backtest))
        except ValueError as exc:
            self.flash = ("error", str(exc), None)
        else:
            self.flash = ("done", "Strategy screen saved.", "Evaluated from the saved screen facts; no API calls.")
        self.saved = "Latest"
        self.data.forget()
        self.render()

    async def start_run(self):
        if self.running:
            return
        spec, settings = self.spec, self.settings
        uses_api = not spec.scan or spec.id == "episodic_pivot" or SCANNERS[spec.id].needs_api
        refresh, sample = self.refresh and uses_api, int(self.sample or 0) if not spec.scan else 0
        self.running, self.flash = True, None
        self.progress = {"message": "Loading the market", "done": 0, "total": 1, "calls": 0}
        self.render()
        self.ticker.activate()

        def progress(message, done, total, calls):
            self.progress = {"message": message, "done": done, "total": total, "calls": calls}

        def work():
            ctx = None
            try:
                if refresh:
                    key = config.load_fmp_api_key()
                    logging_setup.configure_logging(settings.data.log_dir, secrets=[key])
                    ctx = context.open_context(settings=settings, api_key=key)
                run_screen(self.data.store, settings, ctx.client if ctx else None, sample=sample or None,
                           progress=progress, strategy_id=spec.id, workers=all_cores())
            finally:
                if ctx:
                    ctx.close()
        try:
            await run.io_bound(work)
        except (ScreenError, ConfigError, FMPError) as exc:
            self.flash = ("error", str(exc), "The previous completed screen is preserved. Check data access or finish the "
                                             "backfill, then retry.")
        else:
            self.flash = ("done", "Screen saved.", None)
            self.saved = "Latest"
        finally:
            self.ticker.deactivate()
            self.running = False
            self.data.forget()
            self.render()


def sort_rows(rows, order, descending):
    """Rows by the chosen measure, with unavailable values always last."""
    def value(r):
        if order == "rank":
            return -r["rank"]  # highest first puts rank 1 on top
        if order == "RS":
            return r["rs"]
        if order == "change":
            return r["change"]
        if order == "passed":
            return r.get("passed")
        if order == "Price":
            return r["close"]
        item = next((c for c in r["row"]["criteria"] if LABELS[c["key"]] == order), None)
        return criterion_number(item) if item else None
    return sorted(rows, key=lambda r: (value(r) is None, -(value(r) or 0) if descending else (value(r) or 0), r["symbol"]))
