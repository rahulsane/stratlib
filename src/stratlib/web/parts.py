"""Small pieces every page shares: number formats, icons, buttons, notices and the market card."""

from __future__ import annotations

import json
from html import escape

from nicegui import ui

MARKET_TONE = {"confirmed uptrend": "up", "uptrend under pressure": "caution", "correction": "down"}

# The screener's own mark: a cup with handle and its breakout.
MARK = ('<svg class="d-mark" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M2.5 6c1 8.5 3.8 12 7.6 12 3.9 0 5.6-4.6 6.2-9.2l1.9 2.6L21 4.8" fill="none" stroke="#d1d4dc" '
        'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/><circle cx="21" cy="4.8" r="2.1" fill="#26a69a"/></svg>')
PLAY = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7-4.5z" fill="currentColor" stroke="none"/></svg>'
TUNE = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h7M12.5 4.5h1M11 2.8v3.4M2.5 11.5h2M7.5 11.5h6M6 9.8v3.4"/></svg>'
ARROW = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8h9M8.5 4l4 4-4 4"/></svg>'
DOWN = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M4 9l4 4 4-4"/></svg>'
UP = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M4 7l4-4 4 4"/></svg>'
DATABASE = ('<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4" rx="5" ry="2"/>'
            '<path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"/></svg>')
BACK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.5 8h-9M7.5 4l-4 4 4 4"/></svg>'
PLUS = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>'
REDO = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13 3v4H9M12.6 7A5 5 0 1 0 13 10"/></svg>'


# A click anywhere in a list reports the row under the pointer.
PICK = '(e) => { const row = e.target.closest(".d-row[data-pick]"); if (row) emit(row.dataset.pick); }'


def screen_path(public: bool) -> str:
    """Screen is the home page, except in the public app, whose home compares the strategies."""
    return "/screen" if public else "/"


def money(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def pct(value: float | None) -> str:
    return "" if value is None else f"{value:+,.2f}%"


def fmt(value: float | None) -> str:
    return "–" if value is None else f"{value:,.2f}"


def compact(value: float | None) -> str:
    if value is None:
        return "–"
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:,.0f}"


def sparkline(series: list[float]) -> str:
    if len(series) < 2:
        return '<svg class="d-spark d-col-spark" viewBox="0 0 76 24" aria-hidden="true"></svg>'
    points = series[::3] + series[-1:]
    low, high = min(points), max(points)
    span = (high - low) or 1
    coords = " ".join(f"{i * 76 / (len(points) - 1):.1f},{22 - (p - low) / span * 20:.1f}" for i, p in enumerate(points))
    return (f'<svg class="d-spark d-col-spark is-{"up" if series[-1] >= series[0] else "down"}" viewBox="0 0 76 24" '
            f'aria-hidden="true"><polyline points="{coords}"/></svg>')


def button(label: str, *, icon: str = "", primary: bool = False, on_click=None, title: str | None = None):
    """A native button in the dashboard's style; a title becomes its tooltip."""
    element = ui.html(f"{icon}{escape(label)}", sanitize=False, tag="button").classes(
        "d-btn d-btn-primary" if primary else "d-btn").props('type="button"')
    if title:
        element.props(f'title="{escape(title)}"')
    if on_click:
        element.on("click", on_click)
    return element


def notice(text: str, tone: str = "info", *, detail: str | None = None):
    """A line of news about the page: info, caution, error or done."""
    body = f"<b>{escape(text)}</b>" + (f" <span>{escape(detail)}</span>" if detail else "")
    return ui.html(f'<i aria-hidden="true"></i><p>{body}</p>', sanitize=False).classes(f"d-notice tone-{tone}").props(
        'role="alert"' if tone in {"error", "caution"} else 'role="status"')


def toast(message: str, tone: str = "info"):
    ui.notify(message, classes=f"d-toast tone-{tone}", position="bottom-right")


def verdict_chips(verdicts: list[tuple[str, str, str]]) -> str:
    """Written verdicts as tinted chips: (label, word, tone) with tone pass, fail or none."""
    return '<div class="d-verdicts">' + "".join(
        f'<span class="d-verdict tone-{tone}"><span>{escape(label)}</span><b>{escape(word)}</b></span>'
        for label, word, tone in verdicts) + "</div>"


def market_card(market: dict, *, holdings: int, exposure: bool = True) -> str:
    """The index state tinted by condition, the allowed exposure and each index's distribution days."""
    state = market.get("state")
    tone = MARKET_TONE.get(state, "none")
    allowed = market.get("exposure") if exposure else None
    blocks = "".join(f'<span><i style="width:{max(0.0, min(1.0, ((allowed or 0) - i * 20) / 20)) * 100:.0f}%"></i></span>'
                     for i in range(5))
    indexes = " · ".join(f'{escape(entry["name"])} <b>{latest["distribution_count"]}</b>'
                         for entry in (market.get("indexes") or {}).values()
                         if (latest := entry.get("latest") or {}).get("distribution_count") is not None)
    as_of = f" · as of {escape(market['as_of'])}" if market.get("as_of") else ""
    if allowed is not None:
        first = f"<b>{allowed:g}%</b> invested · up to {int(holdings * allowed / 100 + 1e-9)} of {holdings}{as_of}"
    else:
        first = ("This strategy ignores the exposure ladder" if not exposure else "Exposure unavailable") + as_of
    meter = (f'<div class="d-meter tone-{tone}" role="img" aria-label="{allowed:g}% allowed exposure">{blocks}</div>'
             if allowed is not None else "")
    return (f'<div class="d-card d-market" role="group" aria-label="Market direction">'
            f'<div class="d-market-state tone-{tone}">{escape((state or "Market unavailable").capitalize())}</div>{meter}'
            f'<div class="d-market-foot"><span>{first}</span>'
            + (f"<span>Distribution days: {indexes}</span>" if indexes else "") + "</div></div>")


def filter_card(name: str, state: dict | None, code: str) -> str:
    """A scan strategy's own market filter instead of the exposure ladder."""
    if code == "A":
        title, tone, lines = "No market filter", "none", [f"{name} takes signals in any market."]
    elif not state:
        title, tone, lines = "Market filter unavailable", "none", [f"Run the {name} screen to measure it."]
    else:
        title = f"Market filter {'on' if state.get('on') else 'off'}"
        tone = "up" if state.get("on") else "down"
        lines = [state.get("label") or "", state.get("detail") or ""]
    return (f'<div class="d-card d-market" role="group" aria-label="Market filter">'
            f'<div class="d-market-state tone-{tone}">{escape(title)}</div>'
            f'<div class="d-market-foot">{"".join(f"<span>{escape(line)}</span>" for line in lines if line)}</div></div>')


def source_card(title: str, tone: str, lines: list[str]) -> str:
    """Where an index strategy's holdings come from, in the place of a market filter."""
    return (f'<div class="d-card d-market" role="group" aria-label="Holdings source">'
            f'<div class="d-market-state tone-{tone}">{escape(title)}</div>'
            f'<div class="d-market-foot">{"".join(f"<span>{escape(line)}</span>" for line in lines if line)}</div></div>')


def table_html(rows: list[dict], columns: list[tuple[str, str]], *, numeric=frozenset()) -> str:
    """A plain read-only table: (key, heading) columns over dict rows, numbers right-aligned."""
    head = "".join(f'<th class="{"d-num" if key in numeric else ""}">{escape(label)}</th>' for key, label in columns)
    body = "".join("<tr>" + "".join(
        f'<td class="{"d-num" if key in numeric else ""}">{escape("" if row.get(key) is None else str(row.get(key)))}</td>'
        for key, _ in columns) + "</tr>" for row in rows)
    return f'<div class="d-table-wrap"><table class="d-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def settings_html(values: dict) -> str:
    """A flat mapping of settings as a grid of names, as config.yaml spells them, and values."""
    items = "".join(f"<div><dt>{escape(key)}</dt><dd>{escape(str(value))}</dd></div>" for key, value in sorted(values.items()))
    return f'<dl class="d-kv">{items}</dl>'


def json_html(value) -> str:
    return f'<pre class="d-code">{escape(json.dumps(value, indent=2, sort_keys=True, default=str))}</pre>'


def details(summary: str, body: str, *, count: int | None = None, open_: bool = False):
    """A collapsible section under the results, closed until asked for."""
    badge = f'<span class="d-count-badge">{count:,}</span>' if count is not None else ""
    return ui.html(f'<details{" open" if open_ else ""}><summary>{escape(summary)}{badge}</summary>'
                   f'<div class="d-details-body">{body}</div></details>', sanitize=False).classes("d-card d-details")


def pill(word: str) -> str:
    """A written verdict as a small tinted pill: Pass, Fail, or N/A for an unmeasured check."""
    tone = {"Pass": "pass", "Fail": "fail"}.get(word, "none")
    return f'<span class="d-pill tone-{tone}">{"N/A" if word == "Unavailable" else escape(word)}</span>'


def checks_html(criteria: list[dict], *, explain: bool = False) -> str:
    """Checks grouped under their CANSLIM letters: name, measurement and verdict. With explain, each row also
    states its rule and opens to show the explanation saved with the measurement."""
    from ..presentation import CANSLIM, SHORT, short_value, verdict
    out = ['<div class="d-checks">']
    for letter, name in CANSLIM:
        items = [c for c in criteria if c["letter"] == letter]
        if not items:
            continue
        out.append(f'<div class="d-letter"><b>{letter}</b>{escape(name)}</div>')
        for c in items:
            label = escape(SHORT.get(c["key"], c["label"]))
            cells = f'<span class="d-num">{escape(short_value(c))}</span>{pill(verdict(c["passed"]))}'
            if explain:
                out.append(f'<details class="d-check-x"><summary class="d-check"><span>{label}'
                           f'<small>{escape(c.get("rule") or "")}</small></span>{cells}</summary>'
                           f'<p>{escape(c.get("detail") or "No explanation was saved.")}</p></details>')
            else:
                out.append(f'<div class="d-check" title="{escape(c["label"])}: {escape(c.get("rule") or "")}. '
                           f'{escape(c.get("detail") or "")}"><span>{label}</span>{cells}</div>')
    out.append("</div>")
    return "".join(out)


def stats_html(items: list[tuple[str, str]]) -> str:
    """Label and value pairs as plain rows."""
    return '<div class="d-checks">' + "".join(
        f'<div class="d-check is-plain"><span>{escape(label)}</span><span class="d-num">{escape(value)}</span></div>'
        for label, value in items) + "</div>"
