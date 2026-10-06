import json
from pathlib import Path

import pytest

from stratlib.reports import (RESEARCH_ROOT, ReportError, article_body, article_sections, comparison_rows,
                            document_images, filter_reports, link_images, load_catalog, load_result, read_text,
                            report_image, report_url, safe_path)


@pytest.fixture
def research(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "study.md").write_text("# Original title\n\nThe original finding.\n\n## Method\n\nFixed rules.", encoding="utf-8")
    for name, periods in [("baseline", ["in_sample", "out_of_sample"]), ("variation", ["in_sample"])]:
        run = output / name
        run.mkdir()
        result = {"name": name, "data_through": "2026-09-29", "params": {}, "rules": {}, "results": {}}
        for period in periods:
            result["results"][period] = {
                "start": "2022-01-03", "end": "2026-09-29", "trades": 0,
                "cagr": 0, "max_drawdown": 0, "sharpe": None, "spy": {"cagr": 10},
                "equity_curve": {"dates": ["2022-01-03", "2026-09-29"], "equity": [100, 100],
                                 "spy_total": [100, 110]}}
        (run / "results.json").write_text(json.dumps(result))
        (run / "report.md").write_text(f"# {name}\nSaved run.")
        (run / "trades_out_of_sample.csv").write_text("symbol,return\nAAA,0\n")
    manifest = [{"slug": "entry-study", "title": "Entry study", "strategy": "Breakout", "summary": "Two entry rules.",
                 "documents": [{"path": "study.md", "title": "Findings"}], "runs": ["baseline", "variation"]},
                {"slug": "exit-study", "title": "Exit study", "strategy": "Breakout", "summary": "An exit experiment.",
                 "documents": [{"path": "study.md", "title": "Findings"}]}]
    (tmp_path / "reports.json").write_text(json.dumps(manifest))
    return tmp_path


def test_catalog_groups_variations_and_searches_without_loading_results(research):
    (research / "output/baseline/results.json").write_text("unfinished JSON")
    reports, issues = load_catalog(research)
    assert not issues
    assert len(reports) == 2
    entry = next(r for r in reports if r.slug == "entry-study")
    assert [r.name for r in entry.runs] == ["baseline", "variation"]
    assert filter_reports(reports, "BASELINE")[0].slug == "entry-study"
    assert filter_reports(reports, "exit", "Breakout")[0].slug == "exit-study"
    assert filter_reports(reports, strategy="Other") == []


def test_catalog_covers_all_existing_reports_and_keeps_major_experiments_separate():
    reports, issues = load_catalog()
    assert not issues
    slugs = [r.slug for r in reports]
    assert len(slugs) == len(set(slugs))
    covered = {d.path for r in reports for d in r.documents}
    covered.update(p / "report.md" for r in reports for p in r.runs)
    assert set((RESEARCH_ROOT / "output").rglob("*.md")) <= covered
    entries = next(r for r in reports if r.slug == "breakout-entry-rules")
    exits = next(r for r in reports if r.slug == "breakout-exit-rules")
    assert entries.runs and exits.runs and not set(entries.runs) & set(exits.runs)
    assert all(d.path.is_file() for r in reports for d in r.documents)
    assert all(p.is_file() for r in reports for p in r.files)


def test_invalid_manifest_path_and_duplicate_slug_are_isolated(research):
    manifest = research / "reports.json"
    entries = json.loads(manifest.read_text())
    entries += [entries[0], {**entries[0], "slug": "escape", "documents": [{"path": "../../secret", "title": "No"}]}]
    manifest.write_text(json.dumps(entries))
    reports, issues = load_catalog(research)
    assert len(reports) == 2 and len(issues) == 2
    with pytest.raises(ReportError):
        safe_path(research / "output", "../../secret")
    manifest.write_text("unfinished")
    assert load_catalog(research)[0] == []
    assert load_catalog(research)[1]


def test_result_cache_refreshes_and_comparison_never_substitutes_a_period(research):
    run = research / "output/baseline"
    first = load_result(run)
    results = {"baseline": first, "variation": load_result(research / "output/variation")}
    rows = comparison_rows(results, "out_of_sample")
    assert len(rows) == 1
    assert rows[0]["Trades"] == 0 and rows[0]["CAGR, %"] == 0
    assert rows[0]["Gap vs SPY, pp"] == -10
    assert rows[0]["Sharpe"] is None
    changed = json.loads((run / "results.json").read_text())
    changed["results"]["out_of_sample"]["cagr"] = 12.25
    (run / "results.json").write_text(json.dumps(changed))
    assert load_result(run)["results"]["out_of_sample"]["cagr"] == 12.25
    assert first["results"]["out_of_sample"]["cagr"] == 0


def test_saved_curves_keep_their_values_and_the_body_keeps_its_findings(research):
    curve = load_result(research / "output/baseline")["results"]["in_sample"]["equity_curve"]
    assert curve["equity"] == [100, 100] and curve["spy_total"] == [100, 110]
    assert article_body("# Title\n\nFinding.\n\n## Method\nText") == "Finding.\n\n### Method\nText"
    assert report_url("entry-study") == "?page=Reports&report=entry-study"
    assert article_sections("# Title\nIntro\n## Results\n0%\n## Method\n```\n## Code\n```") == [
        (None, "Intro"), ("Results", "0%"), ("Method", "```\n## Code\n```")]


def test_a_report_serves_only_the_images_its_documents_embed(research):
    output = research / "output"
    for path in (output / "chart one.png", output / "baseline/unlisted.png", research / "outside.png"):
        path.write_bytes(b"\x89PNG\r\n")
    (output / "study.md").write_text(
        "# Title\n\n![Growth](chart%20one.png)\n\n![Web](https://example.com/x.png) ![Gone](missing.png) "
        "![Up](../outside.png) ![Data](baseline/results.json)\n\n## Method\n\nFixed rules.", encoding="utf-8")
    report = next(r for r in load_catalog(research)[0] if r.slug == "entry-study")
    document = report.documents[0]
    text = read_text(document.path)
    assert list(document_images(document, text, output)) == ["chart%20one.png"]
    linked = link_images(text, document, report.slug, output)
    assert "![Growth](/report-files/entry-study/chart%20one.png)" in linked
    assert all(f"({link})" in linked for link in
               ("https://example.com/x.png", "missing.png", "../outside.png", "baseline/results.json"))
    assert report_image("entry-study", "chart one.png", research) == (output / "chart one.png").resolve()
    for slug, path in [("entry-study", "baseline/unlisted.png"), ("entry-study", "../outside.png"),
                       ("entry-study", "baseline/results.json"), ("no-such-report", "chart one.png")]:
        assert report_image(slug, path, research) is None
