"""Market: the shared index observations behind M, with the selected strategy's own market policy. Distribution
and follow-through days are marked on each index's chart. Everything reads cached prices."""

from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape

from nicegui import ui

from ..market_direction import INDEX_RULES
from ..prices import Bar, eod_cutoff
from ..scanning import quick_filter
from ..strategies import STRATEGIES, latest_screen, market_policy
from . import parts
from .chart import price_chart
from .data import Data, Workspace
from .library import strategy_library, strategy_meta, strategy_select

RANGES = {"3M": 63, "6M": 126, "1Y": 252}


def pct(value: float | None, digits: int = 1) -> str:
    return "Unavailable" if value is None else f"{value:.{digits}f}%"


class MarketPage:
    """One tab's Market page."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.index: str | None = None
        self.range = "6M"

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    def build(self):
        self.root = ui.element("div").classes("d-market-page")
        self.render()

    def render(self):
        market, spec = self.data.market(), self.spec
        if self.index not in (market.get("indexes") or {}):
            self.index = next(iter(market.get("indexes") or {}), None)
        self.root.clear()
        with self.root:
            self.header(market)
            public = self.data.public
            if not market.get("as_of"):
                ui.html("<h2>No market history in this snapshot</h2>" if public else
                        "<h2>No market history yet</h2><p>Backfill the index and ETF histories to calculate market "
                        'direction:</p><pre class="d-code">.venv\\Scripts\\stratlib backfill --symbols "^GSPC,^IXIC,SPY,QQQ"'
                        "</pre>", sanitize=False).classes("d-card d-empty")
                with ui.element("div").classes("d-extras"):
                    strategy_library(self.settings, spec, public=public)
                return
            with ui.element("div").classes("d-notices"):
                cutoff = (self.data.snapshot_date()
                          or eod_cutoff(datetime.now(timezone.utc), self.settings.prices.eod_final_hour_et))
                if (cutoff - date.fromisoformat(market["as_of"])).days > 4:
                    parts.notice("Market prices are stale.", "caution",
                                 detail="Run the daily backfill before using this assessment.")
                for warning in market["warnings"]:
                    parts.notice(warning, "caution")
            with ui.element("section").classes("d-summary").props('aria-label="Market direction and indexes"'):
                self.policy_card(market)
                with ui.element("div").classes("d-card d-funnel").props('aria-label="Indexes"'):
                    with ui.element("div").classes("d-steps").props('role="tablist"'):
                        for symbol, entry in market["indexes"].items():
                            self.index_tab(symbol, entry)
            with ui.element("div").classes("d-stock-top"):
                with ui.element("section").classes("d-card d-chart-card").props('aria-label="Index chart"'):
                    self.chart = ui.refreshable(self.chart_card)
                    self.chart()
                with ui.element("aside").classes("d-card d-side").props('aria-label="Index details"'):
                    self.details = ui.refreshable(self.index_details)
                    self.details()
            with ui.element("div").classes("d-extras"):
                self.events = ui.refreshable(self.event_list)
                self.events()
                self.rules()
                strategy_library(self.settings, spec, public=public)

    def header(self, market):
        spec = self.spec
        with ui.element("section").classes("d-head"):
            with ui.element("div").classes("d-head-left"):
                ui.html("Market", sanitize=False, tag="h1").classes("d-title")
                strategy_select(spec.id, self.change_strategy)
            meta = strategy_meta(spec, self.settings)
            if market.get("as_of"):
                meta.append(f"Completed prices through <b>{escape(market['as_of'])}</b>")
            meta += [escape(INDEX_RULES[self.settings.thresholds.market_index_rule]), "Cached data, no API calls"]
            ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")

    def policy_card(self, market):
        """The exposure ladder for strategies that use it; a scan strategy's own market filter otherwise."""
        spec = self.spec
        if spec.uses_exposure:
            ui.html(parts.market_card(market, holdings=self.settings.backtest.max_holdings), sanitize=False).classes("d-contents")
            return
        code = self.settings.strategies[spec.id].market_filter
        saved = latest_screen(self.data.store, spec.id)
        # Filters A to C come straight from cached SPY and QQQ bars; D to F need the last scan's breadth.
        state = quick_filter(self.data.store, code, market["as_of"]) or (saved or {}).get("market_filter")
        if state and state.get("code") != code:
            state = None
        if state and code not in "ABC" and saved:
            state = {**state, "detail": f"{state.get('detail') or ''} · from the screen of {saved['price_date']}"}
        ui.html(parts.filter_card(spec.name, state, code), sanitize=False).classes("d-contents")

    def index_tab(self, symbol, entry):
        latest = entry.get("latest") or {}
        state = latest.get("state")
        tone = parts.MARKET_TONE.get(state, "none")
        current = symbol == self.index
        count = latest.get("distribution_count")
        caption = (f"{count} distribution days" if count is not None and state else "Unavailable")
        if latest.get("exposure") is not None:
            caption += f" · {latest['exposure']:g}%"
        ui.html(f'<span class="d-index-name">{escape(entry["name"])}</span>'
                f'<strong class="d-market-state tone-{tone}">{escape((state or "Unavailable").capitalize())}</strong>'
                f"<span>{escape(caption)}</span>", sanitize=False, tag="button").classes(
            "d-step d-index-step" + (" is-current" if current else "")).props(
            f'type="button" role="tab" aria-selected="{str(current).lower()}"').mark(f"index-{symbol}").on(
            "click", lambda symbol=symbol: self.change_index(symbol))

    def chart_card(self):
        market = self.data.market()
        entry = market["indexes"][self.index]
        sessions = RANGES[self.range]
        days = {d["date"]: d for d in entry["history"]}
        bars = self.data.bars(self.index, market["as_of"], sessions + 199)
        # Volume is the series each comparison used: the index's own, or its ETF's where the index has none.
        bars = [Bar(b.date, b.open, b.high, b.low, b.close, days[b.date]["volume"] if b.date in days else None) for b in bars]
        events = {d["date"]: "distribution" if d["distribution"] else "follow_through"
                  for d in entry["history"][-sessions:] if d["distribution"] or d["follow_through"]}
        last = bars[-1] if bars else None
        change = 100 * (bars[-1].close / bars[-sessions:][0].close - 1) if len(bars) > 1 else None
        with ui.element("div").classes("d-chart-head"):
            ui.html(f'<div class="d-sym-id"><b>{escape(entry["name"])}</b><span class="d-tag">{escape(self.index)}</span></div>'
                    f'<p class="d-sym-where">Volume from {escape((entry.get("latest") or {}).get("volume_source") or "unavailable")}'
                    "</p>", sanitize=False)
            if last:
                ui.html(f'<div class="d-price"><strong>{last.close:,.2f}</strong>'
                        f'<span class="{"d-up" if (change or 0) >= 0 else "d-down"}">{parts.pct(change)}</span>'
                        f'<small>{self.range}</small></div><p class="d-facts">Close {escape(last.date)}</p>',
                        sanitize=False).classes("d-chart-price")
        with ui.element("div").classes("d-chart-tools"):
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Chart range"'):
                for name in RANGES:
                    ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if name == self.range else "")).props(
                        f'type="button" aria-pressed="{str(name == self.range).lower()}"').on(
                        "click", lambda name=name: self.change_range(name))
            ui.html('<span class="d-key is-down"></span>Distribution day <span class="d-key is-up"></span>Follow-through day',
                    sanitize=False).classes("d-legend")
        if not bars:
            ui.html("This index has no cached price history. Run the market-symbol backfill.", sanitize=False,
                    tag="p").classes("d-empty-line")
            return
        price_chart(bars, sessions, large=True, events=events)
        ui.html("Volume bars show the source each comparison used. Averages are 50 and 200 sessions.", sanitize=False,
                tag="p").classes("d-chart-foot")

    def index_details(self):
        market, spec = self.data.market(), self.spec
        entry = market["indexes"][self.index]
        latest = entry.get("latest") or {}
        ui.html(escape(entry["name"]), sanitize=False, tag="h3").classes("d-side-title")
        state = latest.get("state")
        ui.html(parts.verdict_chips([("State", (state or "Unavailable").capitalize(),
                                      {"confirmed uptrend": "pass", "correction": "fail"}.get(state, "none"))]),
                sanitize=False).classes("d-contents")
        ui.html(parts.stats_html([
            ("Allowed exposure", pct(latest.get("exposure"), 0)),
            ("Below uptrend peak", pct(latest.get("drawdown_pct"))),
            ("Distribution days", str(latest["distribution_count"]) if state and latest.get("distribution_count") is not None
             else "Unavailable"),
            ("Last follow-through", latest.get("last_follow_through") or "None observed"),
            ("Rally day", str(latest["rally_day"]) if latest.get("rally_day") is not None else "None"),
            ("Volume source", latest.get("volume_source") or "Unavailable"),
        ]), sanitize=False).classes("d-contents")
        if latest.get("reason"):
            ui.html(escape(latest["reason"]), sanitize=False, tag="p").classes("d-note")
        ui.html(f"{escape(spec.name)} policy", sanitize=False, tag="h3").classes("d-side-title")
        ui.html(escape(market_policy(spec.id, self.settings.backtest, self.settings)), sanitize=False,
                tag="p").classes("d-side-text")

    def event_list(self):
        entry = self.data.market()["indexes"][self.index]
        events = [{"date": d["date"], "event": "Distribution" if d["distribution"] else "Follow-through",
                   "change": f"{d['change_pct']:+.2f}%" if d.get("change_pct") is not None else "",
                   "source": d["volume_source"] or "", "state": (d["state"] or "Unavailable").capitalize()}
                  for d in reversed(entry["history"][-126:]) if d["distribution"] or d["follow_through"]]
        body = (parts.table_html(events, [("date", "Date"), ("event", "Event"), ("change", "Change"),
                                          ("source", "Volume source"), ("state", "State after")], numeric={"change"})
                if events else '<p class="d-note">No distribution or follow-through days in the last 126 sessions.</p>')
        parts.details(f"{entry['name']}: distribution and follow-through days, last 126 sessions", body, count=len(events),
                      open_=True)

    def rules(self):
        t = self.settings.thresholds
        ending = (f"{t.distribution_heavy_count} or more is heavy pressure. Distribution days never end an uptrend by "
                  f"themselves: a close {t.correction_drawdown_pct:g}% below the uptrend's peak close, or an undercut "
                  "of the rally low, starts a correction." if t.correction_drawdown_pct > 0
                  else f"{t.distribution_correction_count} means correction.")
        paragraphs = [
            f"Distribution: a decline of at least {t.distribution_decline_pct:g}% on higher volume. Count the last "
            f"{t.distribution_window_sessions} sessions; {t.distribution_pressure_count} means under pressure and {ending} "
            f"A gain of at least {t.follow_through_gain_pct:g}% on higher volume, on rally day {t.follow_through_min_day} or "
            "later, confirms an uptrend. Rally days count from the intraday low; an undercut restarts the count. Correction "
            "persists until a new follow-through.",
            f"Allowed exposure: confirmed uptrend {t.exposure_confirmed_pct:g}% ({t.exposure_late_confirmed_pct:g}% with "
            f"{t.distribution_pressure_count - 1} distribution days, and at most {t.exposure_new_uptrend_pct:g}% for the first "
            f"{t.exposure_new_uptrend_sessions} sessions after a follow-through); under pressure {t.exposure_pressure_pct:g}% "
            f"({t.exposure_heavy_pressure_pct:g}% with {t.distribution_heavy_count} or more); correction 0%. M passes, and "
            "new buying is allowed, only above 0%.",
            f"{INDEX_RULES[t.market_index_rule]} " + (
                f"A distribution day stops counting once a later close is {t.distribution_expiry_gain_pct:g}% above its close."
                if t.distribution_expiry_gain_pct > 0 else "Distribution days count for the full window."),
            "The history starts unconfirmed. Missing prices or volume make M unavailable until a complete comparison window "
            "exists. Expiring distribution days can relieve pressure but cannot end a correction.",
        ]
        parts.details("Market rules", "".join(f"<p>{escape(p)}</p>" for p in paragraphs))

    # Events -----------------------------------------------------------------------------------------------------

    def change_strategy(self, key):
        if key == self.workspace.strategy:
            return
        self.workspace.strategy = key
        ui.navigate.history.replace(f"/market?strategy={key}")
        self.render()

    def change_index(self, symbol):
        if symbol == self.index:
            return
        self.index = symbol
        self.render()

    def change_range(self, name):
        self.range = name
        self.chart.refresh()
