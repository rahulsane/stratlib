"""Stock detail: one stock's chart and base beside the rules it passes or fails, its saved screen measurements and
statements, the manual I assessment and the optional Jev base review. Everything reads cached data; only a Jev
review, on request, calls out."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape

from nicegui import run, ui

from ..config import ConfigError, Thresholds
from ..jev import JevError, request_key, review_base, review_request
from ..market_stats import PriceMetrics, price_filter_checks
from ..presentation import (FILTERED, STATEMENT_LABELS, STATEMENTS, comparison_rows, shown, statement_value,
                            tone, verdict)
from ..prices import eod_cutoff
from ..scanning import SCANNERS
from ..scoring import CRITERIA
from ..strategies import STRATEGIES, latest_screen
from ..technical import stock_setup, technical_criteria
from . import parts
from .chart import price_chart, weekly
from .data import Data, Workspace

# Each range as daily sessions and weekly bars.
RANGES = {"6M": (126, 26), "1Y": (252, 52), "3Y": (756, 156)}
CASL = {key for key, _, _ in CRITERIA}
SIGNAL_KINDS = {"candidates": "Candidate", "triggered": "Buy stop already triggered",
                "blocked": "Held back by the market filter", "skipped": "Skipped by the strategy's rules"}


def price_rows(prices: dict, thresholds: Thresholds) -> list[tuple[str, str, str, str]]:
    """The five price prefilter checks as (check, measured, requirement, verdict), under the screen's thresholds."""
    metrics = PriceMetrics(**prices)
    rows = []
    for check in price_filter_checks(metrics, thresholds):
        def written(value):
            if value is None:
                return "Unavailable"
            if check.key == "volume":
                return f"{value:,.0f} shares/day"
            if check.key == "high":
                return f"{value:.2f}%"
            return f"${value:,.2f}"
        measured = written(check.value)
        if check.key == "high" and metrics.high_52w is not None:
            measured += f" (high ${metrics.high_52w:,.2f})"
        rows.append((check.label, measured, f"{check.comparison} {written(check.threshold)}", verdict(check.passed)))
    return rows


def statement_cell(field, value) -> str:
    value = statement_value(field, value)
    if value is None:
        return ""
    if isinstance(value, float) and field == "epsDiluted":
        return f"{value:,.2f}"
    if isinstance(value, (int, float)) and STATEMENT_LABELS[field].endswith(", millions"):
        return f"{value:,.1f}"
    return str(value)


class StockPage:
    """One tab's Stock detail page."""

    def __init__(self, data: Data, workspace: Workspace, symbol: str | None = None):
        self.data, self.workspace, self.settings = data, workspace, data.settings
        self.requested = (symbol or "").strip().upper() or None
        self.cadence, self.range, self.period = "Daily", "1Y", "quarter"
        self.flash: tuple[str, str, str | None] | None = None
        self.jev_flash: tuple[str, str] | None = None
        self.reviewing = False
        self.history_run: int | None = None

    @property
    def spec(self):
        return STRATEGIES[self.workspace.strategy]

    # Building -------------------------------------------------------------------------------------------------

    def build(self):
        listings = self.data.listings()
        if not listings:
            self.header(None)
            ui.html("<h2>No stocks in this snapshot</h2>" if self.data.public else
                    "<h2>No stocks loaded yet</h2><p>Load the universe and price history first:</p>"
                    '<pre class="d-code">.venv\\Scripts\\stratlib backfill</pre>', sanitize=False).classes("d-card d-empty")
            return
        symbols = sorted(listings)
        missing = self.requested and self.requested not in listings
        self.symbol = (self.requested if self.requested in listings else
                       self.workspace.symbol if self.workspace.symbol in listings else symbols[0])
        if missing:
            self.flash = ("caution", f"{self.requested} is not in the stored universe.", f"Showing {self.symbol} instead.")
        self.root = ui.element("div").classes("d-stock")
        self.render()

    def render(self):
        """Recalculate the stock's setup from the cache and draw the page."""
        self.workspace.symbol = self.symbol
        store, settings = self.data.store, self.settings
        self.info = self.data.listings().get(self.symbol, {})
        spec = self.spec
        # C/A/S/L facts come from a screen that saved them; a scan strategy's screen holds only its own candidates.
        self.report = (None if spec.scan else latest_screen(store, spec.id)) or store.document("latest_screen")
        self.row = next((r for r in self.report["rows"] if r["symbol"] == self.symbol), None) if self.report else None
        self.scan_report = latest_screen(store, spec.id) if spec.scan else None
        self.market = self.data.market()
        cutoff = self.data.snapshot_date() or eod_cutoff(datetime.now(timezone.utc), settings.prices.eod_final_hour_et)
        self.as_of = self.market["as_of"] or cutoff.isoformat()
        self.bars, self.base = stock_setup(store, self.symbol, self.as_of, settings.thresholds, self.market)
        self.technical = [c.to_dict() for c in technical_criteria(self.base, self.market, settings.thresholds, self.as_of)]
        self.root.clear()
        with self.root:
            self.header(self.symbol)
            with ui.element("div").classes("d-notices"):
                if self.flash:
                    tone_, text, detail = self.flash
                    parts.notice(text, tone_, detail=detail)
            with ui.element("div").classes("d-stock-top"):
                with ui.element("section").classes("d-card d-chart-card").props('aria-label="Price chart"'):
                    self.chart = ui.refreshable(self.chart_card)
                    self.chart()
                with ui.element("aside").classes("d-card d-side").props('aria-label="Screen standing"'):
                    self.standing()
            # Two independent columns, so a long card does not leave a gap beside a short one.
            with ui.element("div").classes("d-grid-2"):
                with ui.element("div").classes("d-col"):
                    with ui.element("section").classes("d-card d-section").props('aria-label="Base and market checks"'):
                        self.base_checks()
                    with ui.element("section").classes("d-card d-section").props('aria-label="Price prefilter"'):
                        self.prefilter()
                with ui.element("div").classes("d-col"):
                    with ui.element("section").classes("d-card d-section").props('aria-label="Saved C/A/S/L checks"'):
                        self.saved_checks()
                    # The owner's notes and paid reviews stay out of the public app.
                    if not self.data.public:
                        with ui.element("section").classes("d-card d-section").props('aria-label="Institutional sponsorship"'):
                            self.sponsorship()
            with ui.element("section").classes("d-card d-section").props('aria-label="Fundamentals"'):
                self.statements = ui.refreshable(self.fundamentals)
                self.statements()
            if not self.data.public:
                with ui.element("section").classes("d-card d-section").props('aria-label="Jev base review"'):
                    self.jev = ui.refreshable(self.jev_section)
                    self.jev()

    def header(self, symbol):
        listings = self.data.listings()
        with ui.element("section").classes("d-head"):
            with ui.element("div").classes("d-head-left"):
                ui.html("Stock detail", sanitize=False, tag="h1").classes("d-title")
                if symbol:
                    options = {s: f"{s} · {row.get('name') or s}" for s, row in sorted(listings.items())}
                    ui.select(options, value=symbol, with_input=True, on_change=lambda e: self.change_symbol(e.value)).mark(
                        "symbol").classes("d-field d-strategy d-symbol").props(
                        'dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                        'input-debounce=150 aria-label="Stock"')
            if symbol:
                spec, report = self.spec, self.report
                meta = [escape(spec.name)]
                if report:
                    meta.append(f"C/A/S/L facts from the screen of <b>{escape(report['price_date'])}</b>")
                meta += [f"Market data through <b>{escape(self.as_of)}</b>", "Cached data, no API calls"]
                ui.html(" · ".join(meta), sanitize=False, tag="p").classes("d-meta")

    # The chart and the stock's standing -----------------------------------------------------------------------

    def chart_card(self):
        info, row = self.info, self.row or {}
        exchange = row.get("exchange") or info.get("exchange") or ""
        where = " · ".join(str(info.get(k) or "Unclassified") for k in ("sector", "industry"))
        daily, weeks = RANGES[self.range]
        shown_bars = self.bars[-daily:]
        change = (100 * (shown_bars[-1].close / shown_bars[0].close - 1)
                  if len(shown_bars) > 1 and shown_bars[0].close else None)
        last = self.bars[-1] if self.bars else None
        with ui.element("div").classes("d-chart-head"):
            ui.html(f'<div class="d-sym-id"><b>{escape(self.symbol)}</b>'
                    f'{f"<span class=d-tag>{escape(exchange)}</span>" if exchange else ""}</div>'
                    f'<p class="d-sym-name">{escape(info.get("name") or self.symbol)}</p>'
                    f'<p class="d-sym-where">{escape(where)}</p>', sanitize=False)
            if last:
                ui.html(f'<div class="d-price"><strong>{parts.money(last.close)}</strong>'
                        f'<span class="{"d-up" if (change or 0) >= 0 else "d-down"}">{parts.pct(change)}</span>'
                        f'<small>{self.range}</small></div><p class="d-facts">Close {escape(last.date)}</p>',
                        sanitize=False).classes("d-chart-price")
        with ui.element("div").classes("d-chart-tools"):
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Bar length"'):
                for name in ("Daily", "Weekly"):
                    ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if name == self.cadence else "")).props(
                        f'type="button" aria-pressed="{str(name == self.cadence).lower()}"').on(
                        "click", lambda name=name: self.change_chart(cadence=name))
            with ui.element("div").classes("d-ranges").props('role="group" aria-label="Chart range"'):
                for name in RANGES:
                    ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if name == self.range else "")).props(
                        f'type="button" aria-pressed="{str(name == self.range).lower()}"').on(
                        "click", lambda name=name: self.change_chart(range=name))
        if not self.bars:
            ui.html(self.no_prices(), sanitize=False, tag="p").classes("d-empty-line")
            return
        if self.cadence == "Weekly":
            price_chart(weekly(self.bars), weeks, base=self.base, averages=(10, 40), large=True, shade=True)
        else:
            price_chart(self.bars, daily, base=self.base, large=True, shade=True)
        ui.html(f"Split-adjusted prices and volume through {escape(self.bars[-1].date)}. "
                + ("The last week may be partial; base detection uses completed weeks only. Averages are 10 and 40 weeks. "
                   if self.cadence == "Weekly" else "Averages are 50 and 200 sessions. ")
                + ("The outlined range is the base, its handle shaded; the triangle marks the first closing breakout."
                   if self.base.get("pattern") else "No completed base is drawn."),
                sanitize=False, tag="p").classes("d-chart-foot")

    def standing(self):
        row, report = self.row, self.report
        if self.spec.scan:
            self.signal()
        ui.html("Saved screen", sanitize=False, tag="h3").classes("d-side-title")
        if not row:
            ui.html(self.unscreened("Not in the saved screen. Run the screen to see this stock's C/A/S/L measurements."
                                    if report else "Run the screen to see C/A/S/L measurements and their results."),
                    sanitize=False, tag="p").classes("d-note")
            return
        chips = []
        if row["stage"] not in FILTERED:
            chips.append(("C/A/S/L", verdict(row["phase2_pass"]), tone(verdict(row["phase2_pass"]))))
            if report.get("phase") == 3:
                chips.append(("C/A/N/S/L/M", verdict(row.get("phase3_pass")), tone(verdict(row.get("phase3_pass")))))
        else:
            chips.append(("Stopped at", row["stage"], "none"))
        ui.html(parts.verdict_chips(chips), sanitize=False).classes("d-contents")
        prices = row["prices"]
        stats = []
        if prices.get("close") is not None:
            stats.append((f"Close, {report['price_date']}", parts.money(prices["close"])))
        if prices.get("below_high_pct") is not None:
            stats.append(("Below 52-week high", f"{prices['below_high_pct']:.1f}%"))
        stats.append(("RS", f"{row['rs']:.0f}" if row.get("rs") is not None else "Unavailable"))
        industry = (row.get("industry_stats") or {}).get("rank")
        if industry is not None:
            stats.append(("Industry rank", f"#{industry} of {report.get('industry_count', '?')}"))
        stats.append(("Stage", row["stage"]))
        ui.html(parts.stats_html(stats), sanitize=False).classes("d-contents")

    def signal(self):
        spec, report = self.spec, self.scan_report
        ui.html(f"{escape(spec.name)} signal", sanitize=False, tag="h3").classes("d-side-title")
        if not report:
            ui.html(f"No saved {escape(spec.name)} screen in this snapshot." if self.data.public else
                    f"Run the {escape(spec.name)} screen to see whether {escape(self.symbol)} is a candidate.",
                    sanitize=False, tag="p").classes("d-note")
            return
        for kind, title in SIGNAL_KINDS.items():
            match = next((r for r in report.get(kind) or [] if r["symbol"] == self.symbol), None)
            if not match:
                continue
            columns = [("signal_date", "Signal"), *SCANNERS[spec.id].candidate_columns, ("entry_date", "Triggered"),
                       ("entry_price", "Entry, USD"), ("weight_pct", "Weight, %")]
            ui.html(parts.verdict_chips([(title, report["price_date"], "pass" if kind == "candidates" else "none")]),
                    sanitize=False).classes("d-contents")
            ui.html(parts.stats_html([(label, shown(key, match.get(key))) for key, label in columns if match.get(key) is not None]),
                    sanitize=False).classes("d-contents")
            if match.get("reason"):
                ui.html(escape(match["reason"]) + ".", sanitize=False, tag="p").classes("d-note")
            return
        ui.html(f"Not in the {escape(spec.name)} screen of {escape(report['price_date'])}: it did not set up on the last "
                "completed close.", sanitize=False, tag="p").classes("d-note")

    def unscreened(self, advice: str) -> str:
        """Why the saved screen has no measurement: the owner gets advice; the public app can only read the screen."""
        if not self.data.public:
            return advice
        return "Not in the saved screen." if self.report else "No saved screen with C/A/S/L measurements in this snapshot."

    def no_prices(self) -> str:
        return ("No price history for this symbol in this snapshot." if self.data.public else
                "No cached price history for this symbol. Run the backfill.")

    # Checks -----------------------------------------------------------------------------------------------------

    def base_checks(self):
        base = self.base
        ui.html(f'<div class="d-section-head"><h3>Base and market checks</h3><span>Recalculated for {escape(self.as_of)} '
                "with current thresholds</span></div>", sanitize=False).classes("d-contents")
        if not self.bars:
            ui.html(self.no_prices(), sanitize=False, tag="p").classes("d-note")
            return
        if base.get("pattern"):
            ui.html(parts.stats_html([
                ("Base", base["pattern"]), ("Length", f"{base['length_weeks']} weeks"), ("Depth", f"{base['depth_pct']:.1f}%"),
                ("Pivot", parts.money(base["pivot"])), ("From pivot", f"{base['distance_pct']:+.1f}%"),
                ("Dates", f"{base['start']} to {base['end']}"),
                ("Closing breakout", base["breakout_date"] or "None")]), sanitize=False).classes("d-contents")
        else:
            ui.html(escape(base["reason"]), sanitize=False, tag="p").classes("d-note")
        ui.html(parts.checks_html(self.technical, explain=True), sanitize=False).classes("d-contents")
        for warning in self.market["warnings"]:
            parts.notice(warning, "caution")

    def saved_checks(self):
        row, report = self.row, self.report
        aside = (f"Screen of {escape(report['price_date'])}, saved {escape(report['created_at'][:10])}, with its thresholds"
                 if row else "")
        ui.html(f'<div class="d-section-head"><h3>Saved C/A/S/L checks</h3><span>{aside}</span></div>',
                sanitize=False).classes("d-contents")
        if not row:
            ui.html(self.unscreened("Run the screen to see C/A/S/L measurements and their pass or fail results."),
                    sanitize=False, tag="p").classes("d-note")
            return
        if row["stage"] in FILTERED:
            ui.html(f"Stopped at the {escape(row['stage'].lower())}: criteria are scored only for survivors.",
                    sanitize=False, tag="p").classes("d-note")
        ui.html(parts.checks_html([c for c in row["criteria"] if c["key"] in CASL], explain=True),
                sanitize=False).classes("d-contents")
        for warning in row.get("warnings") or []:
            parts.notice(warning, "caution")
        if row.get("error"):
            parts.notice(row["error"], "error")

    def prefilter(self):
        row, report = self.row, self.report
        ui.html('<div class="d-section-head"><h3>Price prefilter</h3><span>The screen\'s first gate</span></div>',
                sanitize=False).classes("d-contents")
        if not row:
            ui.html(self.unscreened("Run the screen to see the five price checks for this stock."), sanitize=False,
                    tag="p").classes("d-note")
            return
        rows = price_rows(row["prices"], Thresholds(**report["thresholds"]))
        body = "".join(f'<div class="d-check is-rule"><span>{escape(check)}<small>{escape(rule)}</small></span>'
                       f'<span class="d-num">{escape(measured)}</span>{parts.pill(word)}</div>'
                       for check, measured, rule, word in rows)
        reason = row["prices"].get("reason")
        note = f'<p class="d-note">{escape(reason)}.</p>' if reason and reason != "Passed" else ""
        ui.html(f'<div class="d-checks">{body}</div>{note}', sanitize=False).classes("d-contents")

    def sponsorship(self):
        ui.html('<div class="d-section-head"><h3>Institutional sponsorship</h3><span>The I in CANSLIM, assessed by hand'
                "</span></div>", sanitize=False).classes("d-contents")
        ui.html("Saved per stock. It does not change the C/A/S/L result.", sanitize=False, tag="p").classes("d-note")
        saved = self.data.store.sponsorship(self.symbol)
        state = {"verdict": saved["verdict"], "notes": saved["notes"]}
        ui.select(["Unreviewed", "Pass", "Fail"], value=saved["verdict"],
                  on_change=lambda e: state.update(verdict=e.value)).mark("sponsorship").classes("d-field d-sponsor").props(
            'dense outlined hide-bottom-space options-dense popup-content-class="d-popup" aria-label="Sponsorship assessment"')
        ui.textarea(placeholder="Record institutions, sources, and the date of your review.", value=saved["notes"],
                    on_change=lambda e: state.update(notes=e.value or "")).mark("sponsorship-notes").classes(
            "d-field d-notes").props('outlined hide-bottom-space autogrow aria-label="Research notes"')
        with ui.element("div").classes("d-form-actions"):
            parts.button("Save sponsorship", on_click=lambda: self.save_sponsorship(state)).mark("save-sponsorship")
            if saved.get("updated_at"):
                ui.html(f"Last saved {escape(saved['updated_at'][:16].replace('T', ' '))} UTC", sanitize=False,
                        tag="span").classes("d-hint")

    def save_sponsorship(self, state):
        self.data.store.save_sponsorship(self.symbol, state["verdict"], state["notes"])
        parts.toast(f"Sponsorship saved for {self.symbol}.", "done")

    # Statements ---------------------------------------------------------------------------------------------------

    def fundamentals(self):
        bundle = self.data.store.document(f"fundamentals:{self.symbol}")
        with ui.element("div").classes("d-section-head"):
            ui.html("Fundamentals", sanitize=False, tag="h3")
            if bundle:
                with ui.element("div").classes("d-ranges").props('role="group" aria-label="Statement period"'):
                    for key, name in (("quarter", "Quarterly"), ("annual", "Annual")):
                        ui.html(name, sanitize=False, tag="button").classes("d-range" + (" is-current" if key == self.period else "")).props(
                            f'type="button" aria-pressed="{str(key == self.period).lower()}"').on(
                            "click", lambda key=key: self.change_period(key))
        if not bundle:
            ui.html("Statements are fetched only for price and RS survivors. This stock has no cached fundamentals.",
                    sanitize=False, tag="p").classes("d-note")
            return
        ui.html(f"Fetched {escape(bundle['fetched_on'])} · standardized statements · EPS excludes identifiable "
                "non-continuing income. Totals are in millions of the reported currency; diluted shares in millions of "
                "shares. Reported EPS is per share, before the continuing-operations adjustment the scorer uses.",
                sanitize=False, tag="p").classes("d-note")
        for name, label, fields in STATEMENTS:
            records = bundle.get(f"{name}_{self.period}", [])
            rows = [{field: statement_cell(field, record.get(field)) for field in fields} for record in records]
            numeric = {f for f in fields if STATEMENT_LABELS[f].endswith(", millions") or f == "epsDiluted"}
            ui.html(f"<h4>{escape(label)}</h4>" + (parts.table_html(rows, [(f, STATEMENT_LABELS[f]) for f in fields],
                                                                         numeric=numeric)
                                                    if rows else '<p class="d-note">No statements cached.</p>'),
                    sanitize=False).classes("d-statement")

    # Jev ------------------------------------------------------------------------------------------------------------

    def jev_section(self):
        settings, store = self.settings, self.data.store
        ui.html('<div class="d-section-head"><h3>Jev base review</h3><span>Optional chart judgments beside the rules'
                "</span></div>", sanitize=False).classes("d-contents")
        ui.html("Advisory answers do not change screening, sell alerts or backtests.", sanitize=False, tag="p").classes("d-note")
        request = current = None
        if not settings.jev.enabled:
            parts.notice("Jev is disabled.", "info", detail="Set jev.enabled to true in config.yaml and add "
                                                            "AI_GATEWAY_API_KEY to .env to review a base.")
        elif not self.base.get("pattern") or not self.base.get("available"):
            parts.notice("A detected, completed base is required for a Jev review.", "info")
        else:
            try:
                request = review_request(self.symbol, self.bars, self.base, self.as_of, settings.thresholds,
                                         settings.jev.model, sessions=[d["date"] for d in self.market["history"]] or None)
                current = store.document(request_key(request))
            except JevError as exc:
                parts.notice(str(exc), "info")
        if request:
            if self.jev_flash:
                tone_, text = self.jev_flash
                parts.notice(text, tone_)
            with ui.element("div").classes("d-form-actions"):
                parts.button("Reviewing…" if self.reviewing else "Review again with Jev" if current else "Review with Jev",
                             on_click=lambda: self.confirm_review(request, current)).mark("jev-review").props(
                    "disabled" if self.reviewing else "")
                ui.html(f"Model {escape(settings.jev.model)} · sends {len(request['state']['weekly_bars'])} completed weekly "
                        "bars and the base measurements to Vercel AI Gateway. Each review is a paid request.",
                        sanitize=False, tag="span").classes("d-hint")
            if current:
                ui.html("This saved review matches the current evidence and questions; viewing it uses the cache.",
                        sanitize=False, tag="p").classes("d-note")
                self.review_html(current)
            else:
                ui.html("No saved review matches these bars, base measurements and thresholds.", sanitize=False,
                        tag="p").classes("d-note")
        runs = store.last_runs(f"jev:{self.symbol}", limit=20)
        if runs:
            ids = {r["id"]: r for r in runs}
            chosen = ids.get(self.history_run) or runs[0]
            with ui.element("div").classes("d-jev-history"):
                ui.html("Review history", sanitize=False, tag="h4")
                ui.html("The most recent 20 attempts for this stock. Older evidence is historical, not a review of the "
                        "current setup.", sanitize=False, tag="p").classes("d-note")
                ui.select({r["id"]: f"#{r['id']} · {r['started_at']} · {(r['summary'] or {}).get('status', 'incomplete')}"
                           for r in runs}, value=chosen["id"], on_change=lambda e: self.change_history(e.value)).classes(
                    "d-field d-history").props('dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                                               'aria-label="Saved attempt"')
                summary = chosen["summary"] or {}
                if summary.get("result"):
                    self.review_html(summary["result"], history=True)
                else:
                    parts.notice(summary.get("error", "This attempt did not finish. You can retry the current setup."), "info")

    def review_html(self, result, *, history=False):
        state = result["request"]["state"]
        base = state["base_measurements"]
        rows = "".join(
            f'<div class="d-judgment"><h5>{escape(row["Judgment"])}</h5>'
            f'<p><span>Jev</span>{escape(row["Jev answer"])}</p><p><span>Rule</span>{escape(row["Rule evidence"])}</p>'
            f'<small>Probability assigned to this answer {row["Answer probability"]:.0%} · model confidence '
            f'{row["Confidence"]:.2f} out of 1</small></div>' for row in comparison_rows(result))
        ui.html(f'<p class="d-note">{"Historical review" if history else "Saved review"} '
                f'{escape(result["created_at"][:19].replace("T", " "))} UTC · evidence through {escape(state["as_of"])} · '
                f'{escape(base["pattern"])} · base {escape(base["start"])} to {escape(base["end"])}. Rule checks use the '
                "measurements and thresholds saved with the review; Jev adds a qualitative assessment and does not decide "
                f'whether a numeric rule passes.</p><div class="d-judgments">{rows}</div>'
                '<p class="d-note">Answer probability compares the available answers. Confidence is a separate certainty '
                "score. Neither is the probability that the stock will rise. An inconclusive assessment is not a rule "
                f"failure. Returned model {escape(result['returned_model'])} · underlying version "
                f"{escape(result['returned_version'] or 'not reported by Gateway')}.</p>", sanitize=False).classes("d-contents")
        body = json.dumps(result, indent=2, allow_nan=False)
        parts.details("Review evidence and probability distributions", parts.json_html(result))
        parts.button("Download review JSON", on_click=lambda: ui.download.content(
            body, f"jev-{state['symbol']}-{result['run_id']}.json", "application/json")).classes("d-btn-quiet")

    def confirm_review(self, request, current):
        if self.reviewing:
            return
        async def confirmed():
            dialog.close()
            await self.review(request, current)

        with ui.dialog() as dialog, ui.element("div").classes("d-card d-dialog"):
            ui.html(f"<h3>{'Review again' if current else 'Review'} {escape(self.symbol)} with Jev?</h3>"
                    f"<p>This sends {len(request['state']['weekly_bars'])} completed weekly bars and the base measurements "
                    "to Vercel AI Gateway and makes a paid request.</p>", sanitize=False)
            with ui.element("div").classes("d-form-actions"):
                parts.button("Cancel", on_click=dialog.close)
                parts.button("Make paid request", primary=True, on_click=confirmed).mark("jev-confirm")
        dialog.open()

    async def review(self, request, current):
        self.reviewing, self.jev_flash = True, None
        self.jev.refresh()
        try:
            result = await run.io_bound(review_base, self.data.store, self.settings.jev, request, refresh=current is not None)
        except (JevError, ConfigError) as exc:
            self.jev_flash = ("error", str(exc))
        else:
            count = result["api_calls"]
            self.jev_flash = ("done", f"Review saved. {count} Gateway HTTP {'call' if count == 1 else 'calls'}, including retries.")
        finally:
            self.reviewing = False
            self.history_run = None
            self.jev.refresh()

    # Events -----------------------------------------------------------------------------------------------------

    def change_symbol(self, symbol):
        if not symbol or symbol == self.symbol:
            return
        self.symbol, self.flash, self.jev_flash, self.history_run = symbol, None, None, None
        ui.navigate.history.replace(f"/stock?symbol={symbol}")
        self.render()

    def change_chart(self, **values):
        for name, value in values.items():
            setattr(self, name, value)
        self.chart.refresh()

    def change_period(self, period):
        self.period = period
        self.statements.refresh()

    def change_history(self, run_id):
        self.history_run = run_id
        self.jev.refresh()
