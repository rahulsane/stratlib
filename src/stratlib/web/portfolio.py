"""Portfolio: a hypothetical portfolio built from the strategy's buy list, then the model plan beside the recorded
holdings and cash. Cached closes only; nothing here places orders."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from html import escape

from nicegui import ui

from ..portfolio import hypothetical_portfolio, portfolio_plan
from ..positions import current_quantity
from ..presentation import shown
from ..prices import eod_cutoff
from ..scanning import SCANNERS
from ..strategies import INDEXES, STRATEGIES, equal_weighted, holding_limit, latest_screen, market_policy, revision
from . import parts
from .data import Data, Workspace
from .library import strategy_library, strategy_meta, strategy_select

TONES = {"Sell": "fail", "Take profits": "pass", "Review breakout": "caution", "Hold exception": "pass",
         "Proposed buy": "pass", "Review data": "caution", "Review reduction": "caution"}


def pill(word: str) -> str:
    return f'<span class="d-pill tone-{TONES.get(word, "none")}">{escape(word)}</span>' if word else ""


def money(value: float | None) -> str:
    return "Unknown" if value is None else f"${value:,.2f}"


def cell(label: str, value) -> str:
    """A model-plan value written by its column's unit."""
    if value is None or value == "":
        return "–"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if label.endswith(", USD") and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    if label.endswith(", %") and isinstance(value, (int, float)):
        return f"{value:.1f}%"
    if isinstance(value, (int, float)) and "volume" in label.lower():
        return f"{value:,.0f}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def heading(label: str) -> str:
    """Column headings without the units their cells already show."""
    for suffix in (", USD", ", %"):
        if label.endswith(suffix):
            return label[: -len(suffix)]
    return label


class PortfolioPage:
    """One tab's Portfolio page."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.flash: tuple[str, str, str | None] | None = None
        self.capital = float(data.settings.backtest.initial_capital)

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    def build(self):
        self.root = ui.element("div").classes("d-portfolio")
        self.render()

    def render(self):
        settings, spec, store = self.settings, self.spec, self.data.store
        cutoff = self.data.snapshot_date() or eod_cutoff(datetime.now(timezone.utc), settings.prices.eod_final_hour_et)
        report = latest_screen(store, spec.id)
        market = self.data.market()
        plan = portfolio_plan(store, report, spec.id, settings.thresholds, settings.backtest, market, cutoff.isoformat(),
                              params=settings)
        public = self.data.public
        self.root.clear()
        with self.root:
            with ui.element("section").classes("d-head"):
                with ui.element("div").classes("d-head-left"):
                    ui.html("Portfolio", sanitize=False, tag="h1").classes("d-title")
                    strategy_select(spec.id, self.change_strategy)
                if not public:
                    with ui.element("div").classes("d-actions"):
                        parts.button("Record cash", on_click=lambda: self.cash_dialog(plan)).mark("record-cash")
                meta = [*strategy_meta(spec, settings),
                        f"Screen <b>{escape(report['price_date'])}</b>" if report else "Screen not run",
                        f"Market <b>{escape(market.get('as_of') or 'unavailable')}</b>", "Cached closes", "No orders are placed"]
                ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")
            with ui.element("div").classes("d-notices"):
                if self.flash:
                    tone, text, detail = self.flash
                    parts.notice(text, tone, detail=detail)
                # Without a screen the public page says so below; the issue would only advise running one.
                for issue in plan["issues"] if report or not public else []:
                    parts.notice(issue, "caution")
            with ui.element("section").classes("d-summary" + ("" if spec.uses_exposure else " is-single")).props(
                    'aria-label="Market and slots"'):
                if spec.uses_exposure:
                    ui.html(parts.market_card(market, holdings=settings.backtest.max_holdings), sanitize=False).classes("d-contents")
                self.slots(plan)
            ui.html(escape(market_policy(spec.id, settings.backtest, settings)), sanitize=False, tag="p").classes("d-policy")
            self.hypothetical(report, market, cutoff)
            if not public:  # the owner's recorded account and holdings
                self.account(plan)
                self.holdings(plan)
            self.model(plan, report)
            with ui.element("div").classes("d-extras"):
                strategy_library(settings, spec, public=public)

    def slots(self, plan):
        spec, options = self.spec, self.settings.backtest
        # The public app has no recorded holdings, so it shows the model's size instead of the held and open slots.
        public = self.data.public
        if spec.uses_exposure:
            held = (("Maximum holdings", f"{options.max_holdings}", "equal slots") if public else
                    ("Held symbols", f"{plan['held_count']}", f"{plan['free']} open slots"))
            steps = [("Initial slot", f"{100 / options.max_holdings:g}%", "at purchase"), held,
                     ("Candidates", f"{len(plan['candidates'])}", spec.variant_name)]
        elif spec.id in INDEXES:
            params, count = self.settings.strategies[spec.id], len(plan["candidates"])
            held = ([("Holdings", f"{count}", "in the index now")] if public else
                    [("Held symbols", f"{plan['held_count']}", f"of {count} in the index now")])
            steps = [*held, ("Weights", "Index", f"cap x tilt, at most {params.max_issuer_pct:g}% each"),
                     ("Reviews", "Quarterly", "end of Feb, May, Aug and Nov")]
        else:
            params = self.settings.strategies[spec.id]
            slots = holding_limit(spec.id, self.settings, options)
            sizing = (("Weight per holding", f"{100 / slots:g}%", "equal weights, as researched") if equal_weighted(spec.id)
                      else ("Risk per trade", f"{params.risk_pct:g}%", f"at most {params.max_position_pct:g}% in one position"))
            held = ([("Maximum holdings", f"{slots}", "in the model portfolio")] if public else
                    [("Held symbols", f"{plan['held_count']}", f"of {slots} slots"),
                     ("Open slots", f"{plan['free']}", "before the next entries")])
            steps = [*held, sizing, ("Candidates", f"{len(plan['candidates'])}", spec.variant_name)]
        with ui.element("div").classes("d-card d-funnel").props('aria-label="Slots"'):
            with ui.element("div").classes("d-steps"):
                for label, value, note in steps:
                    ui.html(f"<strong>{escape(value)}</strong><span>{escape(label)}</span><small>{escape(note)}</small>",
                            sanitize=False).classes("d-step is-static d-count-step d-plan-step")

    def hypothetical(self, report, market, cutoff):
        """What the strategy's rules would buy today with a chosen amount, rebuilt on every visit."""
        with ui.element("section").classes("d-card d-section d-hypo").props('aria-label="Hypothetical portfolio"'):
            with ui.element("div").classes("d-section-head d-hypo-head"):
                ui.html("<h3>Hypothetical portfolio</h3><span>What the strategy says to buy today, from its latest screen"
                        "</span>", sanitize=False).classes("d-hypo-title")
                ui.number("Starting capital, USD", value=self.capital, min=1, step=10_000, format="%.0f",
                          on_change=lambda e: self.change_capital(e.value)).mark("hypo-capital").classes(
                    "d-form-field d-hypo-capital").props("dense outlined hide-bottom-space stack-label debounce=400")
            self.hypo_view = ui.refreshable(lambda: self.hypothetical_body(report, market, cutoff))
            self.hypo_view()

    def hypothetical_body(self, report, market, cutoff):
        spec, settings = self.spec, self.settings
        if not report:
            ui.html("This strategy has no saved screen in this snapshot, so it has no buy list to build from."
                    if self.data.public else
                    "Run the screen first for this strategy; the hypothetical portfolio is built from its buy list. "
                    '<a href="/">Open Screen</a>.', sanitize=False, tag="p").classes("d-note d-links")
            return

        def price_of(symbol):
            bars = self.data.bars(symbol, cutoff.isoformat(), 1)
            return (bars[-1].date, bars[-1].close) if bars else None
        plan = hypothetical_portfolio(report, spec.id, settings.thresholds, settings.backtest, market, self.capital,
                                      params=settings, price_of=price_of)
        capital, rows = plan["capital"], plan["rows"]
        if spec.id in INDEXES:
            room = f"of {plan['candidates']} in the index"
        elif not spec.uses_exposure:
            room = f"up to {plan['slots']} holdings"
        elif market.get("exposure") is None:
            room = "market exposure is unavailable"
        else:
            room = f"the market allows {plan['capacity']} of {plan['slots']} slots at {market['exposure']:g}% exposure"
        figures = [(f"${plan['invested']:,.0f}", f"Invested · {100 * plan['invested'] / capital:.0f}%"),
                   (f"${plan['cash']:,.0f}", f"Cash · {100 * plan['cash'] / capital:.0f}%"),
                   (f"{len(rows)}", f"Positions · {room}"),
                   (f"{plan['candidates']}", "On the buy list · from the saved screen")]
        share = 100 * plan["invested"] / capital
        ui.html('<div class="d-figures d-hypo-figures">' + "".join(
            f"<div><b>{escape(value)}</b><span>{escape(label)}</span></div>" for value, label in figures) + "</div>"
            f'<div class="d-alloc" role="img" aria-label="{share:.0f}% invested, {100 - share:.0f}% cash">'
            f'<i style="width:{share:.1f}%"></i></div>', sanitize=False).classes("d-contents")
        if not rows:
            reason = ("The saved screen has no buy candidates, so the hypothetical portfolio is all cash."
                      if not plan["candidates"] else
                      "The market allows no new buying today, so the hypothetical portfolio is all cash."
                      if plan["capacity"] == 0 else "No candidate could be bought in whole shares at this amount.")
            ui.html(escape(reason), sanitize=False, tag="p").classes("d-note").mark("hypothetical")
            return
        if spec.scan:
            evidence = list(SCANNERS[spec.id].candidate_columns)

            def written(key, value):
                return shown(key, value) or "–"
        else:
            evidence = [("rs", "RS"), ("sales", "Sales growth"), ("industry_rank", "Industry rank")]

            def written(key, value):
                if value is None:
                    return "–"
                if key == "sales":
                    return f"{value:+.1f}%"
                return f"{value:.0f}" if isinstance(value, float) else str(value)
        head = ('<th class="d-num">#</th><th>Symbol</th><th class="d-num">Weight</th><th class="d-num">Shares</th>'
                '<th class="d-num">Price</th><th class="d-num">Cost</th>'
                + "".join(f'<th class="d-num">{escape(heading(label))}</th>' for _, label in evidence))
        body = "".join(
            f'<tr><td class="d-num d-dim">{r["rank"]}</td><td><a href="/stock?symbol={escape(r["symbol"])}"><b>'
            f'{escape(r["symbol"])}</b></a><small>{escape(r["name"] or "")}</small></td>'
            f'<td class="d-num">{r["weight_pct"]:.1f}%</td><td class="d-num">{r["shares"]:,}</td>'
            f'<td class="d-num">${r["price"]:,.2f}<small>{escape(r["price_date"])}</small></td>'
            f'<td class="d-num">${r["cost"]:,.2f}</td>'
            + "".join(f'<td class="d-num">{escape(written(key, r["leader"].get(key)))}</td>' for key, _ in evidence)
            + "</tr>" for r in rows)
        body += (f'<tr class="d-cash-row"><td></td><td><b>Cash</b><small>left uninvested</small></td>'
                 f'<td class="d-num">{100 * plan["cash"] / capital:.1f}%</td><td></td><td></td>'
                 f'<td class="d-num">${plan["cash"]:,.2f}</td>' + "<td></td>" * len(evidence) + "</tr>")
        sizing = ("Each position takes its weight in the index today, rounded to the nearest share" if spec.id in INDEXES else
                  "Each position takes the research's risk-based weight" if spec.scan and not equal_weighted(spec.id)
                  else f"Each position takes an equal slot of {100 / plan['slots']:g}%")
        priced = ("priced at its buy stop, which fills only if the price trades through it while the order lasts"
                  if plan["buy_stop"] else "bought at the latest cached close")
        skipped = ("; skipped " + ", ".join(f"{symbol} ({why})" for symbol, why in plan["skipped"])) if plan["skipped"] else ""
        ui.html(f'<div class="d-table-wrap"><table class="d-table d-plan-table"><thead><tr>{head}</tr></thead>'
                f'<tbody>{body}</tbody></table></div><p class="d-note">{escape(sizing)} in whole shares, {escape(priced)}, from '
                f"the top of the buy list while {escape(room)}{escape(skipped)}. It assumes nothing is held yet"
                f"{'' if self.data.public else ' and ignores your recorded holdings'}; nothing is saved or ordered.</p>",
                sanitize=False).classes("d-contents").mark("hypothetical")
        with ui.element("div").classes("d-form-actions"):
            parts.button("Download as CSV", on_click=lambda: ui.download.content(
                self.orders_csv(rows, plan["cash"]), f"hypothetical-{spec.id}.csv", "text/csv")).classes("d-btn-quiet")

    @staticmethod
    def orders_csv(rows, cash) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Rank", "Symbol", "Company", "Shares", "Price, USD", "Price date", "Cost, USD", "Weight, %"])
        writer.writerows([[r["rank"], r["symbol"], r["name"], r["shares"], f"{r['price']:.2f}", r["price_date"],
                           f"{r['cost']:.2f}", f"{r['weight_pct']:.2f}"] for r in rows])
        writer.writerow(["", "Cash", "", "", "", "", f"{cash:.2f}", ""])
        return out.getvalue()

    def change_capital(self, value):
        if value and value > 0:
            self.capital = float(value)
            self.hypo_view.refresh()

    def account(self, plan):
        account = plan["account"]
        recorded = (f"Recorded {plan['cash_record']['recorded_at'][:16].replace('T', ' ')} UTC" if plan["cash_record"]
                    else "Not recorded yet")
        with ui.element("section").classes("d-card d-section").props('aria-label="Recorded account"'):
            ui.html('<div class="d-section-head"><h3>Recorded account</h3><span>This strategy and variant only</span></div>'
                    '<div class="d-figures">'
                    f'<div><b>{money(account["holdings"])}</b><span>Holdings value</span></div>'
                    f'<div><b>{money(account["cash"])}</b><span>Recorded cash · {escape(recorded)}</span></div>'
                    f'<div><b>{money(account["total"])}</b><span>Total value</span></div></div>'
                    '<p class="d-note">Add shares in Positions and record cash to calculate the total and actual weights. '
                    "Unknown amounts are not treated as zero. Valuations use each holding's displayed close. Cash is a "
                    "manual balance: adding or closing a position does not change it.</p>", sanitize=False).classes("d-contents")

    def holdings(self, plan):
        store, account = self.data.store, plan["account"]
        records = plan["records"]
        with ui.element("section").classes("d-card d-section").props('aria-label="Holdings"'):
            ui.html(f'<div class="d-section-head"><h3>Holdings</h3><span>{len(records)} open lot'
                    f'{"" if len(records) == 1 else "s"}</span></div>', sanitize=False).classes("d-contents")
            if not records:
                ui.html('No holdings recorded for this strategy. Add them on <a href="/positions">Positions</a>.',
                        sanitize=False, tag="p").classes("d-note d-links")
                return
            body = []
            for record in records:
                result = plan["assessments"][record["id"]]
                quantity = current_quantity(store, record)
                gain = result.gain_pct
                weight = account["weights"][record["id"]]
                body.append(
                    f'<tr><td><a href="/stock?symbol={escape(record["symbol"])}"><b>{escape(record["symbol"])}</b></a>'
                    f'<small>#{record["id"]} · rules {escape(revision(record["rule_snapshot"]))}</small></td>'
                    f"<td>{pill(result.action)}</td>"
                    f'<td>{pill("Review reduction" if record["symbol"] in plan["trims"] else "")}</td>'
                    f'<td class="d-num">{"–" if quantity is None else f"{quantity:,.6g}"}</td>'
                    f'<td class="d-num">{money(account["values"][record["id"]]) if account["values"][record["id"]] is not None else "–"}</td>'
                    f'<td class="d-num">{"–" if weight is None else f"{weight:.1f}%"}</td>'
                    f'<td class="d-num">{money(result.close) if result.close is not None else "–"}</td>'
                    f'<td class="d-num {"" if gain is None else "d-up" if gain >= 0 else "d-down"}">'
                    f'{"–" if gain is None else f"{gain:+.1f}%"}</td>'
                    f'<td class="d-dim">{escape(result.price_date or "–")}</td>'
                    f'<td class="d-why">{escape(result.reason)}</td></tr>')
            ui.html('<div class="d-table-wrap"><table class="d-table d-plan-table"><thead><tr><th>Position</th>'
                    '<th>Saved-rule alert</th><th>Model change</th><th class="d-num">Shares</th><th class="d-num">Value</th>'
                    '<th class="d-num">Weight</th><th class="d-num">Close</th><th class="d-num">Gain</th><th>Price date</th>'
                    f'<th>Reason</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>'
                    '<p class="d-note">Saved-rule alerts match Positions. Model reductions use the current allocation policy and '
                    "need review; they do not replace a holding's saved exit rules. Lots of one symbol share its slot.</p>",
                    sanitize=False).classes("d-contents")

    def model(self, plan, report):
        spec = self.spec
        rows = plan["candidates"]
        with ui.element("section").classes("d-card d-section").props('aria-label="Model changes"'):
            ui.html(f'<div class="d-section-head"><h3>Model changes</h3><span>{len(rows)} candidate'
                    f'{"" if len(rows) == 1 else "s"} from the saved screen</span></div>', sanitize=False).classes("d-contents")
            if not rows:
                ui.html("No entry candidates in this saved screen. Unfilled model slots stay in cash." if report else
                        "No saved screen for this strategy, so there is no model plan." if self.data.public else
                        'Run the screen first for this strategy to create a model plan. <a href="/">Open Screen</a>.',
                        sanitize=False, tag="p").classes("d-note d-links")
                return
            labels = [key for key in rows[0] if key not in {"Rank", "Symbol", "Company", "Plan"}]
            head = ('<th class="d-num">#</th><th>Symbol</th><th>Plan</th>'
                    + "".join(f'<th class="{"d-num" if key != "Industry" else ""}">{escape(heading(key))}</th>' for key in labels))
            body = "".join(
                f'<tr><td class="d-num d-dim">{row["Rank"]}</td><td><a href="/stock?symbol={escape(row["Symbol"])}">'
                f'<b>{escape(row["Symbol"])}</b></a><small>{escape(row.get("Company") or "")}</small></td><td>{pill(row["Plan"])}</td>'
                + "".join(f'<td class="d-num">{escape(cell(key, row.get(key)))}</td>' for key in labels) + "</tr>"
                for row in rows)
            fills = ("the index's weights" if spec.id in INDEXES else
                     "the research's risk-based weights" if spec.scan and not equal_weighted(spec.id) else
                     "equal initial slots" if not spec.scan else "the research's equal weights")
            ui.html(f'<div class="d-table-wrap"><table class="d-table d-plan-table"><thead><tr>{head}</tr></thead>'
                    f'<tbody>{body}</tbody></table></div><p class="d-note">Proposed buys assume the displayed exits and '
                    f"reductions are done. They fill {fills} from the highest rank. This is a close-based plan, not an order "
                    "or a cash-funded trade list.</p>", sanitize=False).classes("d-contents")

    def cash_dialog(self, plan):
        spec = self.spec
        state = {"amount": plan["account"]["cash"]}
        with ui.dialog() as dialog, ui.element("div").classes("d-card d-dialog"):
            ui.html(f"Record cash for {escape(spec.name)}", sanitize=False, tag="h3")
            ui.html("A manual balance for this strategy and variant. Adding or closing a position does not change it; zero "
                    "is valid.", sanitize=False, tag="p")
            errors = ui.element("div").classes("d-notices")
            ui.number("Cash, USD", value=state["amount"], min=0, step=100, format="%.2f",
                      on_change=lambda e: state.update(amount=e.value)).mark("cash-amount").classes("d-form-field").props(
                'dense outlined hide-bottom-space stack-label placeholder="Enter cash for this strategy"')
            with ui.element("div").classes("d-form-actions"):
                parts.button("Cancel", on_click=dialog.close)
                parts.button("Save cash", primary=True, on_click=lambda: self.save_cash(state, dialog, errors)).mark("save-cash")
        dialog.open()

    def save_cash(self, state, dialog, errors):
        spec, amount = self.spec, state["amount"]
        if amount is None or amount < 0:
            errors.clear()
            with errors:
                parts.notice("Enter a cash balance; zero is valid.", "error")
            return
        self.data.store.save_document(f"portfolio:cash:{spec.id}:{spec.variant}",
                                      {"amount": float(amount), "recorded_at": datetime.now(timezone.utc).isoformat()})
        dialog.close()
        self.flash = ("done", f"Cash of ${float(amount):,.2f} recorded for {spec.name}.", None)
        self.render()

    def change_strategy(self, key):
        if key == self.workspace.strategy:
            return
        self.workspace.strategy = key
        self.flash = None
        ui.navigate.history.replace(f"/portfolio?strategy={key}")
        self.render()
