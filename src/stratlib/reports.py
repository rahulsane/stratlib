"""Read-only catalog of research posts and their saved backtest variations.

The manifest makes experiment boundaries explicit; filenames alone cannot tell
whether a new run is a variation or a different research question.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse

from .config import PROJECT_ROOT

RESEARCH_ROOT = PROJECT_ROOT / "research"
# A chart a document embeds as ![alt](file.png), relative to the document; an optional "title" may follow.
IMAGE_LINK = re.compile(r'(!\[[^\]]*\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
# Raster only: an SVG opened from this origin could run script.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}


class ReportError(ValueError):
    """A catalog entry or saved source cannot be read."""


@dataclass(frozen=True)
class Document:
    path: Path
    title: str


@dataclass(frozen=True)
class Report:
    slug: str
    title: str
    strategy: str
    summary: str
    documents: tuple[Document, ...]
    runs: tuple[Path, ...]
    updated: datetime | None
    note: str = ""
    files: tuple[Path, ...] = ()


def report_url(slug: str | None = None) -> str:
    return "?" + urlencode({"page": "Reports", **({"report": slug} if slug else {})})


def safe_path(root: Path, relative: str) -> Path:
    """Only catalog-owned files under the output directory may be served."""
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReportError("Source path must stay inside research/output.")
    return path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReportError(f"Cannot read {path.name}. Restore the saved source file and reload.") from exc


def load_catalog(root: Path = RESEARCH_ROOT) -> tuple[list[Report], list[str]]:
    """Load metadata only. Large result and trade files wait until a post opens."""
    manifest = root / "reports.json"
    if not manifest.exists():
        return [], []
    try:
        entries = json.loads(read_text(manifest))
        if not isinstance(entries, list):
            raise ReportError("reports.json must contain a list of posts.")
    except (ReportError, json.JSONDecodeError) as exc:
        return [], [f"The report catalog could not be loaded: {exc}"]
    reports, issues, seen = [], [], set()
    output = root / "output"
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ReportError("Each catalog entry must be an object.")
            for key in ("slug", "title", "strategy", "summary"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    raise ReportError(f"Missing {key} in catalog entry.")
            slug = entry["slug"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in seen:
                raise ReportError(f"Invalid or duplicate report slug: {slug}.")
            documents = tuple(Document(safe_path(output, d["path"]), d["title"])
                              for d in entry["documents"])
            if not documents:
                raise ReportError(f"{slug} has no report documents.")
            runs = set()
            for pattern in entry.get("runs", []):
                safe_path(output, pattern)
                for path in output.glob(pattern):
                    path = safe_path(output, str(path.relative_to(output)))
                    if path.is_dir() and (path / "results.json").is_file():
                        runs.add(path)
            attachments = tuple(safe_path(output, p) for p in entry.get("files", []))
            files = [d.path for d in documents] + [p / "results.json" for p in runs] + list(attachments)
            stamps = [p.stat().st_mtime for p in files if p.is_file()]
            updated = datetime.fromtimestamp(max(stamps), timezone.utc) if stamps else None
            reports.append(Report(slug, entry["title"], entry["strategy"], entry["summary"],
                                  documents, tuple(sorted(runs)), updated, entry.get("note", ""), attachments))
            seen.add(slug)
        except (ReportError, KeyError, TypeError, OSError, ValueError) as exc:
            issues.append(f"A report was skipped: {exc}")
    reports.sort(key=lambda report: (-(report.updated.timestamp() if report.updated else 0), report.title))
    return reports, issues


def filter_reports(reports: list[Report], query: str = "", strategy: str = "All strategies") -> list[Report]:
    words = query.casefold().split()
    return [r for r in reports if (strategy == "All strategies" or r.strategy == strategy)
            and all(word in " ".join([r.title, r.strategy, r.summary, *(p.name for p in r.runs)]).casefold()
                    for word in words)]


@lru_cache(maxsize=64)
def _read_result(path: Path, modified_ns: int, size: int) -> dict:
    # The fingerprint invalidates cached content when a backtest is regenerated.
    try:
        result = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path.parent.name}/results.json is incomplete or invalid. Regenerate that result.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("results"), dict):
        raise ReportError(f"{path.parent.name} has no comparable engine results. Read its written report below.")
    for period, metrics in result["results"].items():
        if not isinstance(period, str) or not isinstance(metrics, dict):
            raise ReportError(f"{path.parent.name} has an invalid results table.")
    return result


def load_result(run: Path) -> dict:
    path = run / "results.json"
    try:
        stat = path.stat()
        return _read_result(path, stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise ReportError(f"Cannot read {run.name}/results.json. Restore the saved result and reload.") from exc


def comparison_rows(results: dict[str, dict], period: str) -> list[dict]:
    """Use each run's own benchmark and dates; never substitute another window."""
    def number(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

    rows = []
    for name, result in results.items():
        metrics = result["results"].get(period)
        if metrics is None:
            continue
        cagr = number(metrics.get("cagr"))
        spy = number((metrics.get("spy") or {}).get("cagr"))
        rows.append({"Variation": name, "Start": metrics.get("start"), "End": metrics.get("end"),
                     "Trades": number(metrics.get("trades")), "CAGR, %": cagr, "SPY CAGR, %": spy,
                     "Gap vs SPY, pp": cagr - spy if cagr is not None and spy is not None else None,
                     "Max drawdown, %": number(metrics.get("max_drawdown")),
                     "Sharpe": number(metrics.get("sharpe")), "Win rate, %": number(metrics.get("win_rate")),
                     "Expectancy, R": number(metrics.get("expectancy_r"))})
    return rows


def image_url(slug: str, relative: str) -> str:
    return f"/report-files/{slug}/{quote(relative)}"


def document_images(document: Document, text: str, output: Path) -> dict[str, Path]:
    """The images a document embeds by relative link and that exist under output, by link as written."""
    output, found = output.resolve(), {}
    for match in IMAGE_LINK.finditer(text):
        link = match.group(2)
        if urlparse(link).scheme or link.startswith(("/", "#")):
            continue
        path = (document.path.parent / unquote(link)).resolve()
        if path.suffix.lower() in IMAGE_TYPES and path.is_relative_to(output) and path.is_file():
            found[link] = path
    return found


def link_images(text: str, document: Document, slug: str, output: Path) -> str:
    """Point a document's own images at its report's file address; other links stay as written."""
    images, output = document_images(document, text, output), output.resolve()

    def swap(match: re.Match) -> str:
        path = images.get(match.group(2))
        if path is None:
            return match.group(0)
        return match.group(1) + image_url(slug, path.relative_to(output).as_posix()) + match.group(3)
    return IMAGE_LINK.sub(swap, text)


def report_image(slug: str, relative: str, root: Path = RESEARCH_ROOT) -> Path | None:
    """An image the named report's documents embed. Every other file under research/output is refused."""
    report = next((r for r in load_catalog(root)[0] if r.slug == slug), None)
    if report is None:
        return None
    output = (root / "output").resolve()
    for document in report.documents:
        try:
            text = read_text(document.path)
        except ReportError:
            continue
        for path in document_images(document, text, output).values():
            if path.relative_to(output).as_posix() == relative:
                return path
    return None


def article_body(text: str) -> str:
    """The page owns the title; source headings become article subsections."""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    rendered, fence = [], None
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            marker = line.lstrip()[:3]
            fence = None if fence == marker else marker if fence is None else fence
        rendered.append(re.sub(r"^(#{1,5}) ", r"#\1 ", line) if fence is None else line)
    return "\n".join(rendered).strip()


def article_sections(text: str) -> list[tuple[str | None, str]]:
    """Split source sections for explicit, collision-free in-page links."""
    sections, lines, title, fence = [], [], None, None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
        if fence is None and line.startswith("## "):
            sections.append((title, article_body("\n".join(lines))))
            title, lines = line[3:].strip(), []
        else:
            lines.append(line)
    sections.append((title, article_body("\n".join(lines))))
    return sections
