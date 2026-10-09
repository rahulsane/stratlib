"""The web app's shell: the top bar and the routing between pages. Start it with `stratlib web`."""

from __future__ import annotations

from html import escape
from pathlib import Path

from nicegui import app, ui

from ..config import Settings
from ..strategies import STRATEGIES
from . import parts
from .data import Data, Workspace
from .backtest import BacktestPage
from .market import MarketPage
from .portfolio import PortfolioPage
from .reports import ReportsPage, image_route
from .positions import PositionsPage
from .screen import ScreenPage
from .settings import SettingsPage
from .stock import StockPage
from .strategies import StrategiesPage
from .tradetest import ASSETS as TRADETEST_ASSETS, TradeTestPage, api_router, service_for

ASSETS = Path(__file__).with_name("assets")
FONTS = Path(__file__).parents[1] / "static" / "fonts"
PORT = 8600
PAGE_CLASSES = {"Strategies": StrategiesPage, "Screen": ScreenPage, "Portfolio": PortfolioPage, "Market": MarketPage,
                "Positions": PositionsPage, "Backtest": BacktestPage, "TradeTest": TradeTestPage, "Settings": SettingsPage}
# Navigation order and each page's address; Screen is the home page.
PAGES = {"Screen": "/", "Strategies": "/strategies", "Portfolio": "/portfolio", "Market": "/market",
         "Stock detail": "/stock", "Positions": "/positions", "Backtest": "/backtest", "Reports": "/reports",
         "TradeTest": "/tradetest", "Settings": "/settings"}
# The public app opens on the strategies compared, and has no Positions or Settings.
PUBLIC_PAGES = {"Strategies": "/", "Screen": parts.screen_path(True), "Portfolio": "/portfolio", "Market": "/market",
                "Stock detail": "/stock", "Backtest": "/backtest", "Reports": "/reports", "TradeTest": "/tradetest"}

# The list highlight moves at once on a click; on a phone the stock panel opens as a sheet over the list.
# TradeTest's page is a mount point: its app loads when the element appears, and unmounts itself when it leaves.
SCRIPT = """
<script>
window.desk = {
  select(symbol) {
    document.querySelectorAll('.d-row.is-selected').forEach((row) => {
      row.classList.remove('is-selected'); row.setAttribute('aria-selected', 'false'); });
    const row = document.querySelector(`.d-row[data-pick="${CSS.escape(symbol)}"]`);
    if (row) { row.classList.add('is-selected'); row.setAttribute('aria-selected', 'true'); row.scrollIntoView({block: 'nearest'}); }
  },
};
document.addEventListener('click', (event) => {
  const row = event.target.closest('.d-row[data-pick]');
  if (row) {
    window.desk.select(row.dataset.pick);
    if (innerWidth <= 720) document.querySelector('.d-detail')?.classList.add('is-open');
    else if (innerWidth <= 1100) setTimeout(() => document.querySelector('.d-detail')?.scrollIntoView({behavior: 'smooth'}), 160);
  }
  if (event.target.closest('.d-sheet-close')) document.querySelector('.d-detail')?.classList.remove('is-open');
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') document.querySelector('.d-detail.is-open')?.classList.remove('is-open');
});
const mountTradeTest = () => document.querySelectorAll('[data-tradetest]:not([data-mounted])').forEach((el) => {
  el.dataset.mounted = '';
  import(el.dataset.assets + 'app.js?v=' + el.dataset.build).then((m) => m.mount(el)).catch((error) => {
    el.textContent = 'TradeTest could not load. Reload the page to try again.';
    console.error(error);
  });
});
new MutationObserver(mountTradeTest).observe(document.documentElement, {childList: true, subtree: true});
mountTradeTest();
</script>
"""


class Shell:
    """One tab's frame around the pages: the top bar stays while pages change beneath it."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace = data, workspace
        self.pages = PUBLIC_PAGES if data.public else PAGES
        self.links: dict[str, ui.link] = {}

    def build(self):
        ui.add_head_html(f"<style>{(ASSETS / 'theme.css').read_text(encoding='utf-8')}</style>{SCRIPT}")
        self.topbar()
        with ui.element("main").classes("d-page"):
            routes = {path: page_builder(name) for name, path in self.pages.items()}
            ui.sub_pages(routes, data={"shell": self})
        if self.data.public:
            self.footer()

    def topbar(self):
        market = self.data.market()
        tone = parts.MARKET_TONE.get(market.get("state"), "none")
        exposure = market.get("exposure")
        with ui.element("header").classes("d-topbar"):
            ui.html(f"{parts.MARK}StratLib", sanitize=False).classes("d-brand")
            with ui.element("nav").classes("d-nav").props('aria-label="Workspace"'):
                for name, path in self.pages.items():
                    self.links[name] = ui.link(name, path)
            state = (market.get("state") or "market unavailable").capitalize()
            usage = ("<span>Read-only demo</span>" if self.data.public else
                     f"<span>{self.data.calls_today():,} API calls today</span>")
            ui.html(f'<a class="d-chip tone-{tone}" href="/market" title="Market direction from the S&amp;P 500 and Nasdaq">'
                    f'<i></i>{escape(state)}{f" · <b>{exposure:g}%</b>" if exposure is not None else ""}</a>'
                    f'<span>Market data {escape(market.get("as_of") or "unavailable")}</span>{usage}',
                    sanitize=False).classes("d-top-right")

    def footer(self):
        """What the public app is, on every page."""
        through = self.data.snapshot.get("prices_through")
        ui.html("StratLib is a personal research workspace, shown read-only. Screens, portfolios and backtests are saved "
                f"results{f' from prices through {escape(through)}' if through else ''}. Past results are not a forecast, "
                "and nothing here is investment advice.", sanitize=False,
                tag="footer").classes("d-footer")

    def activate(self, name: str):
        """Mark the current page in the navigation and the window title."""
        for page, link in self.links.items():
            if page == name:
                link.props('aria-current="page"')
            else:
                link.props(remove="aria-current")
        ui.page_title(f"{name} · StratLib")


def page_builder(name: str):
    def build(shell: Shell, symbol: str = "", report: str = ""):
        # Each page reads the config as it is now, even if it was saved from another process or an editor.
        shell.data.refresh()
        shell.activate(name)
        if name == "Stock detail":
            StockPage(shell.data, shell.workspace, symbol).build()
        elif name == "Reports":
            ReportsPage(shell.data, shell.workspace, report).build()
        else:
            PAGE_CLASSES[name](shell.data, shell.workspace).build()
    return build


def root(data: Data):
    """Build one tab: the shell, then the page its address names."""
    strategy = ui.context.client.request.query_params.get("strategy", "canslim")
    Shell(data, Workspace(strategy if strategy in STRATEGIES else "canslim")).build()


def main(settings: Settings, *, port: int = PORT, host: str = "127.0.0.1", show: bool = False,
         public: bool = False) -> None:
    data = Data(settings, public=public)
    app.add_static_files("/fonts", FONTS)
    app.add_api_route("/report-files/{slug}/{path:path}", image_route(data), methods=["GET"])
    if TRADETEST_ASSETS.is_dir():
        # Browsers revalidate (a 304 when unchanged), so after a release app.js never meets a cached older chart.js.
        app.add_static_files("/tradetest-assets", TRADETEST_ASSETS, max_cache_age=0)
    app.include_router(api_router(service_for(settings)))
    app.on_shutdown(data.close)
    ui.run(lambda: root(data), host=host, port=port, title="StratLib", dark=True, reload=False, show=show,
           favicon=ASSETS / "favicon.svg", reconnect_timeout=10)
