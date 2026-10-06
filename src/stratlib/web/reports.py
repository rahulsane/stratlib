"""Reports: the research library. An index of saved studies, and each study as an article with its findings, a
comparison of its saved variations and the source files. Opening a report never runs a backtest."""

from __future__ import annotations

import math
from html import escape
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from nicegui import ui

from ..reports import (IMAGE_TYPES, Report, ReportError, article_sections, comparison_rows, filter_reports, link_images,
                       load_catalog, load_result, read_text, report_image)
from . import parts
from .chart import equity_chart
from .data import Data, Workspace

PERIOD_NAMES = {"out_of_sample": "Out of sample", "in_sample": "In sample", "combined": "Combined",
                "last_2_years": "Last two years"}
ALL = "All strategies"
SORTS = {"updated": "Recently updated", "title": "Title"}
# Underscores inside words stay literal, as in file and parameter names.
MARKDOWN = ["fenced-code-blocks", "tables", "cuddled-lists", "code-friendly"]
MIME = {".md": "text/markdown", ".csv": "text/csv", ".json": "application/json"}
# In-page links scroll without changing the address, which would reload the page.
JUMP = "document.getElementById('{id}')?.scrollIntoView({{behavior: 'smooth', block: 'start'}})"


def date_of(report: Report) -> str:
    return report.updated.strftime("%b %d, %Y") if report.updated else "Date unavailable"


def link(slug: str | None = None) -> str:
    return f"/reports?report={slug}" if slug else "/reports"


def image_route(data: Data):
    """The charts a report's documents embed: the only files under research/output served by address."""
    def serve(slug: str, path: str):
        image = report_image(slug, path, data.research_root)
        if image is None:
            raise HTTPException(404, "This report has no such image.")
        return FileResponse(image, media_type=IMAGE_TYPES[image.suffix.lower()])
    return serve


def download(path: Path, label: str):
    """A button that sends one saved file; missing files are skipped quietly."""
    if not path.is_file():
        return
    parts.button(label, on_click=lambda: ui.download.content(path.read_bytes(), f"{path.parent.name}_{path.name}",
                                                             MIME.get(path.suffix, "application/octet-stream"))).classes("d-btn-quiet")


class ReportsPage:
    """One tab's research library: the index, or one report."""

    def __init__(self, data: Data, workspace: Workspace, slug: str = ""):
        self.data, self.workspace, self.slug = data, workspace, slug.strip()
        self.query, self.strategy, self.sort = "", ALL, "updated"

    def build(self):
        self.reports, self.issues = load_catalog(self.data.research_root)
        self.root = ui.element("div").classes("d-reports")
        with self.root:
            if self.issues:
                with ui.element("div").classes("d-notices"):
                    for issue in self.issues:
                        parts.notice(issue, "caution")
            if self.slug:
                report = next((r for r in self.reports if r.slug == self.slug), None)
                if report is None:
                    self.missing()
                else:
                    self.post(report)
            else:
                self.index()

    # The index --------------------------------------------------------------------------------------------------

    def index(self):
        with ui.element("section").classes("d-head"):
            ui.html("Reports", sanitize=False, tag="h1").classes("d-title")
            ui.html("Research notes, results and the variations behind each experiment. Major experiments have their own "
                    "posts; minor variations are compared within them.", sanitize=False, tag="p").classes("d-meta")
        if not self.reports:
            ui.html("<h2>No reports have been published yet</h2>" + ("" if self.data.public else
                    "<p>Add an experiment to research/reports.json to list its saved results here.</p>"),
                    sanitize=False).classes("d-card d-empty")
            return
        with ui.element("div").classes("d-card d-report-list"):
            with ui.element("div").classes("d-filters"):
                search = ui.input(placeholder="Search strategy, experiment or variation", value=self.query,
                                  on_change=lambda e: self.change(query=e.value or "")).mark("report-search").classes(
                    "d-field d-search").props('dense outlined hide-bottom-space clearable debounce=200 '
                                              'aria-label="Search reports"')
                with search.add_slot("prepend"):
                    ui.html('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>',
                            sanitize=False).classes("d-field-icon")
                ui.select([ALL, *sorted({r.strategy for r in self.reports})], value=self.strategy,
                          on_change=lambda e: self.change(strategy=e.value)).mark("report-strategy").classes(
                    "d-field d-sector").props('dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                                              'aria-label="Strategy"')
                ui.select(SORTS, value=self.sort, on_change=lambda e: self.change(sort=e.value)).classes("d-field d-order").props(
                    'dense outlined hide-bottom-space options-dense prefix="Sort" popup-content-class="d-popup" '
                    'aria-label="Sort reports"')
                self.count = ui.html("", sanitize=False).classes("d-count")
            self.list = ui.refreshable(self.list_html)
            self.list()

    def list_html(self):
        matches = filter_reports(self.reports, self.query, self.strategy)
        if self.sort == "title":
            matches.sort(key=lambda r: r.title.casefold())
        self.count.content = f"<b>{len(matches)}</b> of {len(self.reports)} reports"
        if not matches:
            ui.html("No reports match these filters. Try another search or choose All strategies.", sanitize=False,
                    tag="p").classes("d-empty-line")
            return
        rows = "".join(
            f'<article class="d-report-row"><time>{escape(date_of(r))}</time><div>'
            f'<h2><a href="{link(r.slug)}">{escape(r.title)}</a></h2><p>{escape(r.summary)}</p>'
            f'<small>{escape(r.strategy)} · {f"{len(r.runs)} saved variations" if r.runs else "Study with comparative results"}'
            "</small></div></article>" for r in matches)
        ui.html(rows, sanitize=False).classes("d-contents")

    def change(self, **values):
        for name, value in values.items():
            setattr(self, name, value)
        self.list.refresh()

    def missing(self):
        with ui.element("section").classes("d-head"):
            ui.html("Report not found", sanitize=False, tag="h1").classes("d-title")
        ui.html('<h2>This link does not match a saved report</h2><p><a href="/reports">Browse all reports</a></p>',
                sanitize=False).classes("d-card d-empty d-links")

    # One report -------------------------------------------------------------------------------------------------

    def post(self, report: Report):
        ui.page_title(f"{report.title} · StratLib")
        sources = []
        problems = []
        output = self.data.research_root / "output"
        for document in report.documents:
            try:
                sources.append((document, link_images(read_text(document.path), document, report.slug, output)))
            except ReportError as exc:
                problems.append(str(exc))
        words = sum(len(text.split()) for _, text in sources)
        sections = [article_sections(text) for _, text in sources]
        facts = [report.strategy, f"Updated {date_of(report)}"]
        if words:
            facts.append(f"{max(1, math.ceil(words / 220))} min read")
        if report.runs:
            facts.append(f"{len(report.runs)} saved variations")
        with ui.element("div").classes("d-post"):
            with ui.element("article").classes("d-article"):
                ui.html(f'<a class="d-back" href="/reports">{parts.BACK}All reports</a><h1>{escape(report.title)}</h1>'
                        f'<p class="d-lede">{escape(report.summary)}</p><p class="d-meta">{" · ".join(escape(f) for f in facts)}</p>',
                        sanitize=False).classes("d-post-head")
                with ui.element("div").classes("d-notices"):
                    if report.note:
                        parts.notice(report.note, "info")
                    for problem in problems:
                        parts.notice(problem, "caution")
                for i, (document, _) in enumerate(sources):
                    ui.html(escape(document.title), sanitize=False, tag="h2").props(f'id="findings-{i}"').classes("d-doc-title")
                    for j, (title, body) in enumerate(sections[i]):
                        if title:
                            ui.html(escape(title), sanitize=False, tag="h3").props(f'id="source-{i}-{j}"')
                        if body:
                            ui.markdown(body, extras=MARKDOWN).classes("d-prose")
                if report.runs:
                    ui.html("Compare variations", sanitize=False, tag="h2").props('id="compare-variations"').classes("d-doc-title")
                    VariationComparison(report).build()
                ui.html("Source files", sanitize=False, tag="h2").props('id="source-material"').classes("d-doc-title")
                self.sources(report)
                related = [r for r in self.reports if r.strategy == report.strategy and r.slug != report.slug]
                if related:
                    ui.html("<h3>More in this strategy</h3><ul>" + "".join(
                        f'<li><a href="{link(r.slug)}">{escape(r.title)}</a></li>' for r in related) + "</ul>",
                            sanitize=False).classes("d-related d-links")
                ui.html(f'<a class="d-back" href="/reports">{parts.BACK}Back to all reports</a>', sanitize=False)
            with ui.element("aside").classes("d-card d-rail").props('aria-label="In this report"'):
                ui.html("In this report", sanitize=False, tag="h3").classes("d-side-title")
                for i, (document, _) in enumerate(sources):
                    self.jump(document.title, f"findings-{i}", top=True)
                    for j, (title, _) in enumerate(sections[i]):
                        if title:
                            self.jump(title, f"source-{i}-{j}")
                if report.runs:
                    self.jump("Compare variations", "compare-variations", top=True)
                self.jump("Source files", "source-material", top=True)

    def jump(self, label: str, target: str, *, top: bool = False):
        ui.html(escape(label), sanitize=False, tag="button").classes("d-toc-item" + ("" if top else " is-sub")).props(
            f'type="button" onclick="{JUMP.format(id=target)}"')

    def sources(self, report: Report):
        ui.html("These are the saved research files behind this post. Opening a report does not run a backtest.",
                sanitize=False, tag="p").classes("d-note")
        with ui.element("div").classes("d-source-list"):
            for document in report.documents:
                with ui.element("div").classes("d-closed-row"):
                    ui.html(f"<b>{escape(document.title)}</b><span>{escape(document.path.name)}</span>", sanitize=False).classes(
                        "d-closed-what")
                    with ui.element("div").classes("d-form-actions"):
                        download(document.path, "Markdown")
                        download(document.path.with_suffix(".json"), "Study JSON")
            for path in report.files:
                with ui.element("div").classes("d-closed-row"):
                    ui.html(f"<b>{escape(path.name)}</b><span>Supporting data</span>", sanitize=False).classes("d-closed-what")
                    download(path, "Download")


class VariationComparison:
    """A study's saved variations: choose a test period, compare every variation, inspect one in detail."""

    def __init__(self, report: Report):
        self.report = report
        self.period: str | None = None
        self.run: str | None = None

    def build(self):
        self.view = ui.refreshable(self.render)
        self.view()

    def render(self):
        report = self.report
        results, problems = {}, []
        for run in report.runs:
            try:
                results[run.name] = load_result(run)
            except ReportError as exc:
                problems.append(str(exc))
        for problem in problems:
            parts.notice(problem, "caution")
        if not results:
            ui.html("Comparable run data is unavailable. The written findings remain available above.", sanitize=False,
                    tag="p").classes("d-note")
            return
        periods = sorted({p for result in results.values() for p in result["results"]},
                         key=lambda p: (list(PERIOD_NAMES).index(p) if p in PERIOD_NAMES else 99, p))
        if not periods:
            ui.html("No saved test periods are available.", sanitize=False, tag="p").classes("d-note")
            return
        if self.period not in periods:
            self.period = periods[0]
        rows = comparison_rows(results, self.period)
        names = [row["Variation"] for row in rows]
        if self.run not in names:
            self.run = names[0] if names else None
        with ui.element("div").classes("d-ranges").props('role="group" aria-label="Test period"'):
            for period in periods:
                ui.html(escape(PERIOD_NAMES.get(period, period.replace("_", " ").capitalize())), sanitize=False,
                        tag="button").classes("d-range" + (" is-current" if period == self.period else "")).props(
                    f'type="button" aria-pressed="{str(period == self.period).lower()}"').mark(f"period-{period}").on(
                    "click", lambda period=period: self.change_period(period))
        ui.html("Saved portfolio results. Each row uses its own dates and SPY total-return benchmark. CAGR is annualized "
                "return; pp means percentage points; R is the initial trade risk. The source report explains entry, exit, "
                "cost and data assumptions.", sanitize=False, tag="p").classes("d-note")
        columns = list(rows[0]) if rows else []
        numeric = {key for key in columns if key not in {"Variation", "Start", "End"}}
        shown = [{key: (f"{value:,.2f}" if isinstance(value, float) else "–" if value is None else value)
                  for key, value in row.items()} for row in rows]
        ui.html(parts.table_html(shown, [(key, key) for key in columns], numeric=numeric), sanitize=False).classes(
            "d-contents").mark("variations-table")
        missing = len(results) - len(rows)
        if missing:
            ui.html(f"{missing} variation{'s have' if missing != 1 else ' has'} no result for this period and "
                    f"{'are' if missing != 1 else 'is'} omitted.", sanitize=False, tag="p").classes("d-note")
        if not self.run:
            return
        result = results[self.run]
        metrics = result["results"][self.period]
        with ui.element("div").classes("d-inspect"):
            ui.select(names, value=self.run, label="Inspect a variation",
                      on_change=lambda e: self.change_run(e.value)).mark("report-run").classes("d-form-field d-history").props(
                'dense outlined hide-bottom-space options-dense stack-label popup-content-class="d-popup"')
            ui.html(f"{escape(str(metrics.get('start', 'Unknown start')))} to {escape(str(metrics.get('end', 'unknown end')))}"
                    f" · data through {escape(str(result.get('data_through', 'unavailable')))}", sanitize=False,
                    tag="p").classes("d-note")
        if not equity_chart(metrics):
            ui.html("This run has no saved daily equity curve. Its summary results are in the table.", sanitize=False,
                    tag="p").classes("d-note")
        run = next(p for p in report.runs if p.name == self.run)
        with ui.element("details").classes("d-card d-details"):
            ui.html("Variation rules and downloads", sanitize=False, tag="summary")
            with ui.element("div").classes("d-details-body"):
                if result.get("description"):
                    ui.markdown(result["description"], extras=MARKDOWN).classes("d-prose")
                ui.html(parts.json_html({"parameters": result.get("params", {}), "portfolio_rules": result.get("rules", {})}),
                        sanitize=False)
                # Never use a period read from JSON as a filesystem path without an allowlist.
                files = {p.name: p for p in run.iterdir() if p.is_file() and p.resolve().parent == run.resolve()}
                with ui.element("div").classes("d-form-actions"):
                    for filename, label in [("results.json", "Results JSON"), ("report.md", "Run report"),
                                            (f"trades_{self.period}.csv", "Trades CSV"), (f"equity_{self.period}.csv", "Equity CSV")]:
                        if filename in files:
                            download(files[filename], label)

    def change_period(self, period):
        self.period = period
        self.view.refresh()

    def change_run(self, run):
        self.run = run
        self.view.refresh()
