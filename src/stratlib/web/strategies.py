"""Strategies: the public app's home. The rule sets side by side (what each looks for, what it reads, its market
rule, its holdings, its latest screen and its comparable backtest) beside the chosen one's rules and evidence. Picking a
strategy here also picks it on every other page. Everything reads saved results."""

from __future__ import annotations

from html import escape

from nicegui import ui

from ..evidence import verdict as research_verdict
from ..presentation import INDEX_DATA_SHORT, rules_text, sizing_text, strategy_count
from ..sim.engine import PERIOD_TITLES
from ..scanning import SCANNERS
from ..strategies import INDEXES, STRATEGIES, holding_limit, market_policy
from ..strategy_params import MARKET_FILTERS
from . import parts
from .data import Data, Workspace

# What each strategy looks for, in a few words.
STYLES = {"canslim": "Growth stock breakouts", "trend": "Trend following among leaders", "qullamaggie": "Momentum breakouts",
          "minervini": "Volatility contraction breakouts", "episodic_pivot": "Earnings gaps",
          "ema_pullback": "Pullbacks in an uptrend", "tt_checklist": "Quality with momentum", "nash_quality": "Quality growth",
          "msci_garp": "Growth at a reasonable price"}
# The comparable backtest's periods, in the order the switch shows them.
COMPARED = ("combined", "out_of_sample", "in_sample")


def foot(briefs: dict) -> str:
    """The note under the list: the shared rules, with the liquidity floor when every result used the same one."""
    floors = {(b["liquidity"]["min_price"], b["liquidity"]["min_dollar_volume"]) for b in briefs.values()}
    liquidity = (f"no buy under ${min(floors)[0]:g} or ${min(floors)[1] / 1e6:g}M of daily dollar volume"
                 if len(floors) == 1 else "each strategy's own liquidity floor")
    return ("Every strategy is backtested under the same rules: US common stocks, including those delisted since 2016; "
            f"slippage; {liquidity}; stops filled during the day; SPY with dividends as the benchmark. Each keeps its own "
            "position sizes and market rule. MSCI GARP holds what its index holds, from the S&P 500 at each review. "
            "Delisting records are thin before 2020, which flatters the early years. Past results are not a forecast.")


def inputs(spec) -> str:
    """The data a strategy's signals need."""
    if spec.id == "episodic_pivot":
        return "Prices and earnings dates"
    return "Prices and statements" if not spec.scan or SCANNERS[spec.id].needs_api else "Prices only"


def holdings(key, settings, screen) -> str:
    """How many stocks the strategy holds: its limit, or for an index what its latest screen holds."""
    if key in INDEXES:
        return str(screen["count"]) if screen else "–"
    return str(holding_limit(key, settings, settings.backtest))


def market_rule(spec, settings) -> str:
    if spec.uses_exposure:
        return "Exposure ladder"
    code = settings.strategies[spec.id].market_filter
    return "None" if code == "A" else MARKET_FILTERS[code]


def percent(value) -> str:
    return "–" if value is None else f"{value:.1f}%"


class StrategiesPage:
    """One tab's Strategies page."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.period = "combined"

    def build(self):
        self.root = ui.element("div").classes("d-strategies")
        with self.root:
            through = self.data.snapshot.get("prices_through")
            with ui.element("section").classes("d-head"):
                with ui.element("div").classes("d-head-left"):
                    ui.html("Strategies", sanitize=False, tag="h1").classes("d-title")
                meta = [f"{strategy_count().capitalize()} rule sets on US stocks", "Pick one to see its rules, latest screen and backtest; "
                        "every page's strategy menu switches between them"]
                if through:
                    meta.append(f"Prices through <b>{escape(through)}</b>")
                ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")
            with ui.element("div").classes("d-body d-st-body"):
                with ui.element("section").classes("d-card d-table-card d-st-list").props('aria-label="Strategies"'):
                    self.listing = ui.refreshable(self.list_body)
                    self.listing()
                with ui.element("aside").classes("d-card d-detail").props('aria-label="Selected strategy"'):
                    self.detail = ui.refreshable(self.detail_panel)
                    self.detail()

    def result(self, spec) -> dict | None:
        """The strategy's comparable backtest in the chosen period."""
        brief = self.data.engine_summaries().get(spec.id)
        m = brief and brief["periods"].get(self.period)
        if not m:
            return None
        return {"period": f"{m['start']} to {m['end']}", "cagr": m["cagr"], "spy": m["spy_cagr"],
                "drawdown": m["max_drawdown"], "sharpe": m["sharpe"], "trades": m["trades"], "win_rate": m["win_rate"]}

    def list_body(self):
        with ui.element("div").classes("d-st-tools"):
            ui.html("Backtest period", sanitize=False, tag="span")
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Backtest period"'):
                for period in COMPARED:
                    ui.html(escape(PERIOD_TITLES[period]), sanitize=False, tag="button").classes(
                        "d-range" + (" is-current" if period == self.period else "")).props(
                        f'type="button" aria-pressed="{str(period == self.period).lower()}"').mark(f"st-period-{period}").on(
                        "click", lambda period=period: self.change_period(period))
        scroll = ui.element("div").classes("d-scroll").mark("strategies")
        scroll.on("click", lambda e: self.pick(e.args), js_handler=parts.PICK)
        with scroll:
            ui.html(self.rows_html(), sanitize=False).classes("d-contents")
        ui.html(escape(foot(self.data.engine_summaries())), sanitize=False).classes("d-table-foot")

    def rows_html(self) -> str:
        screens = self.data.screen_summaries()
        out = ['<div class="d-rows d-st-rows" role="listbox" aria-label="Strategies">'
               '<div class="d-row d-row-head" aria-hidden="true"><span>Strategy</span><span class="d-col-opt">Signals from</span>'
               '<span class="d-col-opt">Market rule</span><span class="d-num">Holdings</span>'
               '<span class="d-num">Candidates</span><span class="d-num">CAGR</span><span class="d-num">vs SPY</span>'
               '<span class="d-num">Drawdown</span></div>']
        for key, spec in STRATEGIES.items():
            result, screen = self.result(spec), screens.get(key)
            gap = (result["cagr"] - result["spy"] if result and result["cagr"] is not None and result["spy"] is not None
                   else None)
            selected = key == self.workspace.strategy
            out.append(
                f'<div class="d-row{" is-selected" if selected else ""}" role="option" aria-selected="{str(selected).lower()}" '
                f'data-pick="{key}"><span class="d-sym"><b>{escape(spec.name)}</b><small>{escape(STYLES[key])}</small></span>'
                f'<span class="d-dim d-col-opt">{escape(inputs(spec))}</span>'
                f'<span class="d-dim d-col-opt">{escape(market_rule(spec, self.settings))}</span>'
                f'<span class="d-num">{holdings(key, self.settings, screen)}</span>'
                f'<span class="d-num">{screen["count"] if screen else "–"}</span>'
                f'<span class="d-num">{percent(result["cagr"] if result else None)}</span>'
                f'<span class="d-num {"" if gap is None else "d-up" if gap >= 0 else "d-down"}">'
                f'{"–" if gap is None else f"{gap:+.1f} pp"}</span>'
                f'<span class="d-num">{percent(result["drawdown"] if result else None)}</span></div>')
        out.append("</div>")
        return "".join(out)

    def detail_panel(self):
        key = self.workspace.strategy
        spec, settings = STRATEGIES[key], self.settings
        screen = self.data.screen_summaries().get(key)
        result = self.result(spec)
        limit = holding_limit(key, settings, settings.backtest)
        with ui.element("div").classes("d-detail-inner"):
            with ui.element("div").classes("d-sym-head"):
                ui.html(f'<div class="d-sym-id"><b>{escape(spec.name)}</b><span class="d-tag">{escape(spec.variant_name)}</span>'
                        f'</div><p class="d-sym-name">{escape(spec.description)}</p>', sanitize=False)
                with ui.element("div").classes("d-sym-actions"):
                    ui.html(f"Screen{parts.ARROW}", sanitize=False, tag="a").classes("d-btn").props(
                        f'href="{parts.screen_path(self.data.public)}?strategy={key}" aria-label="Open {escape(spec.name)} in Screen"')
                    ui.html("Close", sanitize=False, tag="button").classes("d-btn d-sheet-close").props(
                        'type="button" aria-label="Close the strategy panel"')
            ui.html(parts.stats_html([
                ("Looks for", STYLES[key]), ("Signals from", inputs(spec)), ("Market rule", market_rule(spec, settings)),
                ("Holdings", f"{screen['count']} in the latest screen" if key in INDEXES and screen else
                 "Set by the index" if key in INDEXES else f"Up to {limit}"),
                ("Latest screen", screen["price_date"] if screen else "Not screened in this snapshot"),
            ]), sanitize=False).classes("d-contents")
            ui.html("Rules", sanitize=False, tag="h3").classes("d-side-title")
            ui.html(escape(rules_text(spec, settings)), sanitize=False, tag="p").classes("d-side-text")
            ui.html("Sizing and the market", sanitize=False, tag="h3").classes("d-side-title")
            ui.html(escape(f"{sizing_text(spec, settings)} {market_policy(key, settings.backtest, settings)}"),
                    sanitize=False, tag="p").classes("d-side-text")
            self.candidates(spec, screen)
            self.evidence(spec, result)
            ui.html(" · ".join(f'<a href="{path}?strategy={key}">{name}</a>' for name, path in (
                ("Screen", parts.screen_path(self.data.public)), ("Portfolio", "/portfolio"), ("Backtest", "/backtest"),
                ("Market", "/market"))), sanitize=False, tag="p").classes("d-links d-st-links")

    def candidates(self, spec, screen):
        ui.html("Latest candidates", sanitize=False, tag="h3").classes("d-side-title")
        if not screen:
            ui.html("This strategy was not screened for this snapshot.", sanitize=False, tag="p").classes("d-note")
            return
        if not screen["top"]:
            ui.html(f"No stock met the rules in the screen of {escape(screen['price_date'])}.", sanitize=False,
                    tag="p").classes("d-note")
            return
        rows = "".join(
            f'<div class="d-check is-plain"><span><a href="/stock?symbol={escape(c["symbol"])}"><b>{escape(c["symbol"])}</b></a>'
            f'<small>{escape(c.get("name") or "")}</small></span>'
            f'<span class="d-num">{"" if c.get("rs") is None else "RS %.0f" % c["rs"]}</span></div>' for c in screen["top"])
        more = screen["count"] - len(screen["top"])
        ui.html(f'<div class="d-checks d-st-picks">{rows}</div><p class="d-note">From the screen of '
                f'{escape(screen["price_date"])}, in the strategy\'s rank order'
                f'{f"; {more} more on Screen" if more > 0 else ""}.</p>', sanitize=False).classes("d-contents")

    def evidence(self, spec, result):
        ui.html("Comparable backtest", sanitize=False, tag="h3").classes("d-side-title")
        if result:
            won = "" if result["win_rate"] is None else f", {result['win_rate']:.0f}% won"
            same = ("under the same ground rules as the others, copying the index at each review" if spec.id in INDEXES else
                    f"on the same engine and rules as the other {strategy_count(1)}")
            ui.html(f'<p class="d-side-text">{escape(PERIOD_TITLES[self.period])}, {same}.</p>' + parts.stats_html([
                        ("Period", result["period"]), ("CAGR", percent(result["cagr"])),
                        ("SPY CAGR, with dividends", percent(result["spy"])),
                        ("Maximum drawdown", percent(result["drawdown"])),
                        ("Sharpe ratio", "–" if result["sharpe"] is None else f"{result['sharpe']:.2f}"),
                        ("Trades", f"{result['trades']:,}{won}"),
                    ]), sanitize=False).classes("d-contents")
        else:
            ui.html("No comparable backtest of these rules in this snapshot yet.", sanitize=False, tag="p").classes("d-note")
        if spec.id in INDEXES:
            ui.html(escape(INDEX_DATA_SHORT), sanitize=False, tag="p").classes("d-note").mark("index-data-note")
        note = research_verdict(spec.id, self.data.research_root) if spec.scan else None
        if note:
            ui.html(escape(note), sanitize=False, tag="p").classes("d-note")
        links = [f'<a href="/backtest?strategy={spec.id}">Backtest</a>']
        links += [f'<a href="/reports?report={slug}">Report</a>' for slug in spec.reports[:1]]
        ui.html(" · ".join(links), sanitize=False, tag="p").classes("d-links d-st-links")

    def change_period(self, period):
        self.period = period
        self.listing.refresh()
        self.detail.refresh()

    def pick(self, key):
        if key in STRATEGIES and key != self.workspace.strategy:
            self.workspace.strategy = key
            ui.navigate.history.replace(f"{'/' if self.data.public else '/strategies'}?strategy={key}")
            self.detail.refresh()
