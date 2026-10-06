"""Positions: open holdings with their sell alerts from completed closes, each under the rules saved with it. Adding,
editing, closing and reopening write to the local store; no orders are placed and no API calls are made."""

from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape

from nicegui import ui

from ..positions import assess_position, current_position, current_quantity
from ..presentation import ALERTS, STOP_HELP, entry_defaults
from ..prices import ET, eod_cutoff
from ..sell_rules import Position
from ..strategies import STRATEGIES, current_rules, revision
from . import parts
from .data import Data, Workspace
from .library import strategy_library, strategy_meta, strategy_select

ALERT_TONE = {"Sell": "fail", "Take profits": "pass", "Review breakout": "caution", "Hold exception": "pass"}
URGENT = {"Sell", "Take profits", "Review breakout", "Unavailable"}


def alert_pill(word: str) -> str:
    return f'<span class="d-pill tone-{ALERT_TONE.get(word, "none")}">{escape(word)}</span>'


def money(value: float | None) -> str:
    return "–" if value is None else f"${value:,.2f}"


class PositionsPage:
    """One tab's Positions page."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.scope = "strategy"
        self.selected: int | None = None
        self.flash: tuple[str, str, str | None] | None = None

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    # Building -------------------------------------------------------------------------------------------------

    def build(self):
        self.root = ui.element("div").classes("d-positions")
        self.render()

    def assess(self):
        """Every open holding in scope with its alert, most urgent first."""
        store, settings, spec = self.data.store, self.settings, self.spec
        now = datetime.now(timezone.utc)
        self.today, self.cutoff = now.astimezone(ET).date(), eod_cutoff(now, settings.prices.eod_final_hour_et)
        sessions = [bar.date for bar in store.price_history("SPY", through=self.cutoff.isoformat())]
        expected = sessions[-1] if sessions else None
        records = [r for r in store.positions() if self.scope == "all"
                   or (r["strategy_id"] == spec.id and r["variant_id"] == spec.variant)]
        rows = []
        for record in records:
            result = assess_position(store, record, settings.thresholds, as_of=self.cutoff.isoformat(), sessions=sessions)
            stale = bool(result.price_date and (expected and result.price_date < expected
                                               or (self.cutoff - date.fromisoformat(result.price_date)).days > 4))
            quantity = current_quantity(store, record)
            rows.append({"record": record, "result": result, "quantity": quantity, "stale": stale,
                         "entry": current_position(store, record).entry_price,
                         "value": quantity * result.close if quantity is not None and result.close is not None else None,
                         "data": "Stale" if stale else "Cached" if result.close else "Unavailable"})
        rows.sort(key=lambda row: (ALERTS.index(row["result"].action), row["record"]["symbol"], row["record"]["id"]))
        self.rows = rows
        if self.selected not in {row["record"]["id"] for row in rows}:
            self.selected = rows[0]["record"]["id"] if rows else None

    def render(self):
        self.assess()
        spec = self.spec
        self.root.clear()
        with self.root:
            with ui.element("section").classes("d-head"):
                with ui.element("div").classes("d-head-left"):
                    ui.html("Positions", sanitize=False, tag="h1").classes("d-title")
                    strategy_select(spec.id, self.change_strategy)
                with ui.element("div").classes("d-actions"):
                    parts.button("Add position", icon=parts.PLUS, primary=True, on_click=lambda: self.open_form()).mark(
                        "add-position")
                meta = [*strategy_meta(spec, self.settings), "Sell alerts from completed daily closes", "No orders are placed"]
                ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")
            with ui.element("div").classes("d-notices"):
                if self.flash:
                    tone, text, detail = self.flash
                    parts.notice(text, tone, detail=detail)
                if any(row["stale"] for row in self.rows):
                    parts.notice("Some prices are stale.", "caution",
                                 detail="Their alerts describe the displayed price date. Run the daily backfill.")
            with ui.element("section").classes("d-summary is-even").props('aria-label="Alerts and defaults"'):
                self.counts_card()
                self.defaults_card()
            if not self.rows:
                # The scope switch stays, so holdings under other strategies are one click away.
                with ui.element("section").classes("d-card d-pos-empty").props('aria-label="Open positions"'):
                    self.list_tools()
                    ui.html(f"<h2>No open positions{'' if self.scope == 'all' else ' for ' + escape(spec.name)}</h2>"
                            "<p>Add a ticker, entry date and entry price to start tracking sell alerts. Each entry is saved "
                            "with the strategy's current rules.</p>", sanitize=False).classes("d-empty")
            else:
                with ui.element("div").classes("d-body"):
                    with ui.element("section").classes("d-card d-table-card d-pos-card").props('aria-label="Open positions"'):
                        self.list_tools()
                        scroll = ui.element("div").classes("d-scroll").mark("holdings")
                        scroll.on("click", lambda e: self.pick(e.args), js_handler=parts.PICK)
                        with scroll:
                            self.list_html()
                        ui.html("Each entry is a separate position. An alert reads the latest completed close; a historical "
                                "intraday stop touch is not an execution. A dash means the price or date is unavailable.",
                                sanitize=False).classes("d-table-foot")
                    with ui.element("aside").classes("d-card d-detail").props('aria-label="Selected position"'):
                        self.detail = ui.refreshable(self.detail_panel)
                        self.detail()
            with ui.element("div").classes("d-extras"):
                self.closed_section()
                self.definitions()
                strategy_library(self.settings, spec)

    def defaults_card(self):
        items = entry_defaults(self.spec, self.settings)
        stats = "".join(f'<div><b>{escape(value)}</b><span>{escape(label)}{f" · {escape(note)}" if note else ""}</span></div>'
                        for label, value, note in items)
        ui.html(f'<h3 class="d-side-title">New {escape(self.spec.name)} entries</h3><div class="d-defaults">{stats}</div>'
                '<p class="d-note" title="Legacy CANSLIM entries use the current Settings until assigned rules.">Each '
                "holding keeps the rules saved with it; Settings changes apply to new entries.</p>",
                sanitize=False).classes("d-card d-market")

    def counts_card(self):
        counts = {word: sum(row["result"].action == word for row in self.rows) for word in ALERTS}
        steps = [("Open", len(self.rows), "")] + [(word, count, ALERT_TONE.get(word, "none")) for word, count in counts.items()
                                                  if count or word in {"Sell", "Hold"}]
        with ui.element("div").classes("d-card d-funnel").props('aria-label="Alerts"'):
            with ui.element("div").classes("d-steps"):
                for label, count, tone in steps:
                    ui.html(f'<strong class="{f"tone-{tone}" if count and tone in {"fail", "caution"} else ""}">{count:,}</strong>'
                            f"<span>{escape(label)}</span>", sanitize=False).classes(
                        "d-step is-static d-count-step" + ("" if count else " is-zero"))

    def list_tools(self):
        with ui.element("div").classes("d-filters"):
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Holdings shown"'):
                for key, name in (("strategy", self.spec.name), ("all", "All strategies")):
                    ui.html(escape(name), sanitize=False, tag="button").classes("d-range" + (" is-current" if key == self.scope else "")).props(
                        f'type="button" aria-pressed="{str(key == self.scope).lower()}"').mark(f"scope-{key}").on(
                        "click", lambda key=key: self.change_scope(key))
            ui.html(f"<b>{len(self.rows):,}</b> open", sanitize=False).classes("d-count")

    def list_html(self):
        out = ['<div class="d-rows d-pos-rows" role="listbox" aria-label="Open positions">'
               '<div class="d-row d-row-head" aria-hidden="true"><span>Position</span><span>Alert</span>'
               '<span class="d-num">Close</span><span class="d-num">Gain</span><span class="d-num d-col-opt">Shares</span>'
               '<span class="d-num d-col-opt">Value</span><span class="d-num">Stop</span><span class="d-num d-col-opt">Target</span>'
               '<span class="d-col-opt">Entry</span></div>']
        for row in self.rows:
            record, result = row["record"], row["result"]
            selected = record["id"] == self.selected
            gain = result.gain_pct
            tone = "" if gain is None else "d-up" if gain >= 0 else "d-down"
            spec = STRATEGIES[record["strategy_id"]]
            shares = "–" if row["quantity"] is None else f"{row['quantity']:,.6g}"
            out.append(
                f'<div class="d-row{" is-selected" if selected else ""}" role="option" aria-selected="{str(selected).lower()}" '
                f'data-pick="{record["id"]}">'
                f'<span class="d-sym"><b>{escape(record["symbol"])}</b><small>{escape(spec.name)} · #{record["id"]}</small></span>'
                f'<span>{alert_pill(result.action)}</span>'
                f'<span class="d-num">{money(result.close)}'
                + ("" if row["data"] == "Cached" else f'<i class="d-flag" title="{row["data"]} price">{row["data"]}</i>')
                + "</span>"
                f'<span class="d-num {tone}">{"–" if gain is None else f"{gain:+,.2f}%"}</span>'
                f'<span class="d-num d-col-opt">{shares}</span><span class="d-num d-col-opt">{money(row["value"])}</span>'
                f'<span class="d-num">{money(result.stop_price)}</span><span class="d-num d-col-opt">{money(result.target_price)}</span>'
                f'<span class="d-dim d-col-opt">{escape(record["entry_date"])}</span></div>')
        out.append("</div>")
        ui.html("".join(out), sanitize=False).classes("d-contents")

    def detail_panel(self):
        row = next((r for r in self.rows if r["record"]["id"] == self.selected), None)
        if not row:
            ui.html("Pick a position to review it.", sanitize=False, tag="p").classes("d-empty-line")
            return
        record, result = row["record"], row["result"]
        spec = STRATEGIES[record["strategy_id"]]
        with ui.element("div").classes("d-detail-inner"):
            with ui.element("div").classes("d-sym-head"):
                ui.html(f'<div class="d-sym-id"><b>{escape(record["symbol"])}</b><span class="d-tag">#{record["id"]}</span></div>'
                        f'<p class="d-sym-name">{escape(spec.name)} · {escape(spec.variant_name)}</p>'
                        f'<p class="d-sym-where">Rules {escape(revision(record["rule_snapshot"]))}'
                        f'{" · legacy, uses current Settings" if not record["rule_snapshot"] else ""}</p>', sanitize=False)
                with ui.element("div").classes("d-sym-actions"):
                    ui.html(f"Chart{parts.ARROW}", sanitize=False, tag="a").classes("d-btn").props(
                        f'href="/stock?symbol={escape(record["symbol"])}" aria-label="Open {escape(record["symbol"])} in Stock detail"')
                    ui.html("Close", sanitize=False, tag="button").classes("d-btn d-sheet-close").props(
                        'type="button" aria-label="Close the position panel"')
            gain = result.gain_pct
            ui.html(f'<div class="d-price"><strong>{money(result.close)}</strong>'
                    f'<span class="{"d-up" if (gain or 0) >= 0 else "d-down"}">{"" if gain is None else f"{gain:+,.2f}%"}</span>'
                    f'<small>since entry</small></div><p class="d-facts">'
                    + (f"Completed close {escape(result.price_date)}" if result.price_date else "No completed close")
                    + "</p>", sanitize=False)
            ui.html(parts.verdict_chips([("Alert", result.action, ALERT_TONE.get(result.action, "none"))])
                    + f'<p class="d-alert-reason">{escape(result.reason)}</p>', sanitize=False).classes(
                "d-alert" + (" is-urgent" if result.action in URGENT else ""))
            stats = [("Entry date", record["entry_date"]), ("Entry, adjusted", money(row["entry"])),
                     ("Shares", "Unknown" if row["quantity"] is None else f"{row['quantity']:,.6g}"),
                     ("Value", money(row["value"])), ("Stop", money(result.stop_price)), ("Target", money(result.target_price))]
            if result.trend_line is not None:
                stats.append(("Trend line", money(result.trend_line)))
            if record["strategy_id"] == "canslim":
                stats += [("Hold until", result.hold_until or "–"), ("Hold exception", result.exception_status),
                          ("Fast-gain date", result.fast_gain_date or "–")]
            stats.append(("Data", row["data"]))
            ui.html(parts.stats_html(stats), sanitize=False).classes("d-contents")
            with ui.element("div").classes("d-form-actions"):
                parts.button("Edit position", on_click=lambda: self.open_form(record)).mark("edit-position")
                parts.button("Mark closed", on_click=lambda: self.close_position(record)).mark("close-position")
            if record["rule_snapshot"]:
                parts.details("Saved rules for this position", parts.json_html(record["rule_snapshot"]))

    # Below the list ---------------------------------------------------------------------------------------------

    def closed_section(self):
        spec = self.spec
        closed = [r for r in self.data.store.positions(closed=True) if self.scope == "all"
                  or (r["strategy_id"] == spec.id and r["variant_id"] == spec.variant)]
        with ui.element("details").classes("d-card d-details").mark("closed-positions"):
            ui.html(f'Closed positions<span class="d-count-badge">{len(closed):,}</span>', sanitize=False, tag="summary")
            with ui.element("div").classes("d-details-body d-closed"):
                if not closed:
                    ui.html("No closed positions.", sanitize=False, tag="p").classes("d-note")
                for record in closed:
                    with ui.element("div").classes("d-closed-row"):
                        ui.html(f'<b>{escape(record["symbol"])}</b><span>{escape(STRATEGIES[record["strategy_id"]].name)} · '
                                f'entered {escape(record["entry_date"])} · closed {escape(record["closed_at"][:10])} · '
                                f'#{record["id"]}</span>', sanitize=False).classes("d-closed-what")
                        parts.button("Reopen", on_click=lambda record=record: self.reopen(record)).classes("d-btn-quiet").mark(
                            f"reopen-{record['id']}")

    def definitions(self):
        if self.spec.id == "msci_garp":
            rules = ("Alerts compare each holding with the index's latest screen: Sell when the index dropped it at its last "
                     "review, or sold it when it left the S&P 500; otherwise Hold until the next review. There is no price "
                     "stop. A Sell that fired on an earlier day stays open until you mark the position closed.")
        elif self.spec.scan:
            rules = ("Alerts replay the strategy's research exits from the entry date over completed closes: a close at or "
                     "below the stop, a close under the trailing average, or a scheduled partial sale or time exit. Take "
                     "profits means a partial sale falls due at the latest close. A Sell that fired on an earlier day stays "
                     "open until you mark the position closed. A missing price basis requires a backfill and an edited, "
                     "re-saved position.")
        else:
            rules = ("The fast-gain rule uses completed closes from the breakout reference, including the date exactly three "
                     "calendar weeks later. The hold ends exactly eight calendar weeks from breakout, using the configured "
                     "durations. Missing breakout evidence produces Review breakout at the profit target. A missing price "
                     "basis requires a backfill and an edited, re-saved position.")
        parts.details("Price data and rule definitions",
                      "<p>Update price history after the close with the daily backfill. A ticker outside the cached universe "
                      "can be fetched explicitly with --symbols. Opening this page makes no API calls.</p>"
                      '<pre class="d-code">.venv\\Scripts\\stratlib backfill --symbols AAPL</pre>'
                      f"<p>{escape(rules)}</p>")

    # The add and edit form --------------------------------------------------------------------------------------

    def open_form(self, record: dict | None = None):
        store = self.data.store
        position = current_position(store, record) if record else None
        state = {
            "strategy": record["strategy_id"] if record else self.spec.id, "refresh": False,
            "quantity": current_quantity(store, record) if record and record.get("quantity") is not None else None,
            "ticker": position.symbol if position else "",
            "entry_date": position.entry_date if position else self.today.isoformat(),
            "entry_price": position.entry_price if position else None,
            "stop": position.stop_price if position else None,
            "breakout_date": position.breakout_date if position and position.breakout_date else "",
            "breakout_price": position.breakout_price if position else None,
        }
        with ui.dialog() as dialog, ui.element("div").classes("d-card d-dialog d-form-dialog"):
            ui.html(f"{'Edit' if record else 'Add'} position" + (f" · {escape(record['symbol'])} #{record['id']}" if record else ""),
                    sanitize=False, tag="h3")
            errors = ui.element("div").classes("d-notices")

            @ui.refreshable
            def fields():
                self.form_fields(state, record)

            options = {key: f"{s.name} · {s.variant_name}" for key, s in STRATEGIES.items()}
            ui.select(options, value=state["strategy"], label="Strategy and variant",
                      on_change=lambda e: (state.update(strategy=e.value), fields.refresh())).mark("form-strategy").classes(
                "d-form-field").props('dense outlined hide-bottom-space options-dense stack-label popup-content-class="d-popup"')
            fields()
            with ui.element("div").classes("d-form-actions"):
                parts.button("Cancel", on_click=dialog.close)
                parts.button("Save position" if record else "Add position", primary=True,
                             on_click=lambda: self.save(state, record, dialog, errors)).mark("save-position")
        dialog.open()

    def form_fields(self, state, record):
        strategy_id = state["strategy"]
        if record:
            ui.checkbox("Assign current rules to this position", value=state["refresh"],
                        on_change=lambda e: state.update(refresh=e.value)).mark("form-refresh").classes("d-check-box")
            ui.html(f"Saved rules {escape(revision(record['rule_snapshot']))}. Editing entry details keeps these rules; "
                    "changing strategy or assigning current rules saves the current settings."
                    + ("" if record["rule_snapshot"] else " Legacy CANSLIM entry: alerts use current Settings until you "
                       "assign saved rules."), sanitize=False, tag="p").classes("d-hint")
        else:
            ui.html("The chosen strategy, variant and current rule settings are saved with this entry.", sanitize=False,
                    tag="p").classes("d-hint")
        with ui.element("div").classes("d-form-grid"):
            ui.input("Ticker", value=state["ticker"], placeholder="AAPL",
                     on_change=lambda e: state.update(ticker=e.value or "")).mark("form-ticker").classes(
                "d-form-field").props("dense outlined hide-bottom-space stack-label")
            ui.number("Shares (optional)", value=state["quantity"], min=0, format="%.6g",
                      on_change=lambda e: state.update(quantity=e.value)).mark("form-quantity").classes(
                "d-form-field").props('dense outlined hide-bottom-space stack-label placeholder="Unknown"')
            ui.input("Entry date", value=state["entry_date"],
                     on_change=lambda e: state.update(entry_date=e.value or "")).mark("form-date").classes(
                "d-form-field").props(f'dense outlined hide-bottom-space stack-label type=date max={self.today.isoformat()}')
            ui.number("Entry price, split-adjusted USD", value=state["entry_price"], min=0, step=0.01, format="%.4f",
                      on_change=lambda e: state.update(entry_price=e.value)).mark("form-price").classes(
                "d-form-field").props("dense outlined hide-bottom-space stack-label")
            if strategy_id in STOP_HELP:
                ui.number("Initial stop, split-adjusted USD (optional)", value=state["stop"], min=0, step=0.01, format="%.4f",
                          on_change=lambda e: state.update(stop=e.value)).mark("form-stop").classes(
                    "d-form-field d-span-2").props("dense outlined hide-bottom-space stack-label")
                ui.html(escape(STOP_HELP[strategy_id]), sanitize=False, tag="p").classes("d-hint d-span-2")
            if strategy_id == "canslim":
                ui.html("Breakout evidence for the hold exception, optional. Record the actual breakout date and the pivot "
                        "used to measure the fast gain; leave the date empty if unknown. Entry is not assumed to be the "
                        "breakout.", sanitize=False, tag="p").classes("d-hint d-span-2")
                ui.input("Breakout date", value=state["breakout_date"],
                         on_change=lambda e: state.update(breakout_date=e.value or "")).mark("form-breakout-date").classes(
                    "d-form-field").props(f'dense outlined hide-bottom-space stack-label type=date max={self.today.isoformat()}')
                ui.number("Breakout reference, split-adjusted USD", value=state["breakout_price"], min=0, step=0.01,
                          format="%.4f", on_change=lambda e: state.update(breakout_price=e.value)).mark(
                    "form-breakout-price").classes("d-form-field").props("dense outlined hide-bottom-space stack-label")
        ui.html("Use prices and shares on the same split-adjusted basis as the cached chart. Later split restatements "
                "adjust them automatically.", sanitize=False, tag="p").classes("d-hint")

    def save(self, state, record, dialog, errors):
        strategy_id = state["strategy"]
        try:
            if not state["entry_date"]:
                raise ValueError("Choose an entry date.")
            breakout_date = state["breakout_date"] if strategy_id == "canslim" and state["breakout_date"] else None
            breakout_price = state["breakout_price"] if strategy_id == "canslim" else None
            stop = state["stop"] if strategy_id in STOP_HELP else None
            value = Position(state["ticker"], state["entry_date"], state["entry_price"] or 0.0, breakout_date,
                             breakout_price if (breakout_price or 0) > 0 or breakout_date else None,
                             stop if stop and stop > 0 else None)
            chosen = STRATEGIES[strategy_id]
            snapshot = (current_rules(self.settings, strategy_id)
                        if state["refresh"] or not record or record["strategy_id"] != strategy_id else record["rule_snapshot"])
            quantity = state["quantity"] if state["quantity"] else None
            position_id = self.data.store.save_position(
                value, as_of=self.today.isoformat(), position_id=record["id"] if record else None,
                tracking={"strategy_id": chosen.id, "variant_id": chosen.variant, "rule_snapshot": snapshot,
                          "quantity": quantity})
        except ValueError as exc:
            errors.clear()
            with errors:
                parts.notice(str(exc), "error")
            return
        dialog.close()
        self.selected = position_id
        self.flash = ("done", f"Position {'saved' if record else 'added'} for {value.symbol}.", None)
        if chosen.id != self.spec.id and self.scope != "all":
            self.flash = ("done", f"Position {'saved' if record else 'added'} for {value.symbol}.",
                          f"It is under {chosen.name}; choose that strategy or All strategies to see it.")
        self.render()

    # Events -----------------------------------------------------------------------------------------------------

    def close_position(self, record):
        self.data.store.set_position_closed(record["id"], True)
        self.flash = ("done", f"{record['symbol']} moved to closed positions.", "Reopen it from Closed positions below.")
        self.render()

    def reopen(self, record):
        self.data.store.set_position_closed(record["id"], False)
        self.selected = record["id"]
        self.flash = ("done", f"{record['symbol']} reopened.", None)
        self.render()

    def change_strategy(self, key):
        if key == self.workspace.strategy:
            return
        self.workspace.strategy = key
        self.flash = None
        ui.navigate.history.replace(f"/positions?strategy={key}")
        self.render()

    def change_scope(self, scope):
        self.scope, self.flash = scope, None
        self.render()

    def pick(self, key):
        try:
            position_id = int(key)
        except (TypeError, ValueError):
            return
        if position_id != self.selected:
            self.selected = position_id
            self.detail.refresh()
