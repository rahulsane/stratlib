"""Candlestick charts in TradingView's manner, drawn with ECharts: the Screen panel's small chart and the Stock
detail chart with its base, handle and breakout."""

from __future__ import annotations

from datetime import date

from nicegui import ui

from ..prices import Bar
from .parts import compact, fmt

GREEN, RED, MUTED, LINE = "#26a69a", "#ef5350", "#9598a1", "#2a2e39"
AVERAGE_COLOURS = ("#a3abbd", "#c9a26e")
RANGES = {"3M": 63, "6M": 126, "1Y": 252}

# The OHLC legend in the chart's top-left corner, following the crosshair like TradingView's.
LEGEND = """params => {
  const num = (x) => x == null ? '–' : Number(x).toLocaleString('en', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const k = params.find((p) => p.seriesType === 'candlestick');
  if (!k) return '';
  const d = k.data.length === 5 ? k.data.slice(1) : k.data;
  const tone = d[1] >= d[0] ? '#3cc4b5' : '#ff7f7c';
  const vol = params.find((p) => p.seriesName === 'Volume');
  const v = vol ? Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1}).format(vol.data.value ?? vol.data) : '–';
  const lines = params.filter((p) => p.seriesType === 'line')
    .map((p) => `<span style="color:${p.color}">${p.seriesName} ${num(p.data)}</span>`).join(' &nbsp;');
  return `<span style="color:#9598a1">${k.axisValue}</span>&nbsp; O <b style="color:${tone}">${num(d[0])}</b> H <b style="color:${tone}">${num(d[3])}</b>`
    + ` L <b style="color:${tone}">${num(d[2])}</b> C <b style="color:${tone}">${num(d[1])}</b>`
    + `<br>Vol <b>${v}</b> &nbsp;${lines}`;
}"""


def sma(values: list[float], n: int) -> list[float | None]:
    out, total = [], 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        out.append(round(total / n, 2) if i >= n - 1 else None)
    return out


def weekly(bars: list[Bar]) -> list[Bar]:
    """Daily bars as weekly candles, each dated by its week's last session."""
    weeks: list[list[Bar]] = []
    for bar in bars:
        if weeks and date.fromisoformat(bar.date).isocalendar()[:2] == date.fromisoformat(weeks[-1][0].date).isocalendar()[:2]:
            weeks[-1].append(bar)
        else:
            weeks.append([bar])
    out = []
    for days in weeks:
        highs = [b.high for b in days if b.high is not None]
        lows = [b.low for b in days if b.low is not None]
        volumes = [b.volume for b in days if b.volume is not None]
        out.append(Bar(days[-1].date, days[0].open, max(highs) if highs else None, min(lows) if lows else None,
                       days[-1].close, sum(volumes) if volumes else None))
    return out


def _ohlc(b: Bar) -> tuple[float, float, float, float]:
    """Open, high, low and close, with missing prices taken from the close."""
    o = b.open if b.open is not None else b.close
    return o, max(x for x in (b.high, o, b.close) if x is not None), min(x for x in (b.low, o, b.close) if x is not None), b.close


def _locate(dates: list[str], day: str | None, *, first: bool) -> str | None:
    """The shown date for a day: the first on or after it, or the last on or before it."""
    if not day:
        return None
    if first:
        return next((d for d in dates if d >= day), None)
    return next((d for d in reversed(dates) if d <= day), None)


def price_chart(history: list[Bar], sessions: int, *, base: dict | None = None, averages: tuple[int, int] = (50, 200),
                large: bool = False, shade: bool = False, events: dict[str, str] | None = None):
    """Candles with two moving averages, volume, the last close and the base's pivot. History should hold enough
    bars before the shown ones to seed the longer average. With shade, the base and its handle are shaded and the
    first closing breakout is marked. Events maps a date to "distribution" (a red mark above the bar) or
    "follow_through" (a green mark below it)."""
    if not history:
        ui.html("No cached prices for this symbol. Run the backfill.", sanitize=False, tag="p").classes("d-note")
        return
    closes = [b.close for b in history]
    names = [f"MA {n}" for n in averages]
    lines_ = [sma(closes, n)[-sessions:] for n in averages]
    bars = history[-sessions:]
    candles = [_ohlc(b) for b in bars]
    last, last_open = bars[-1], candles[-1][0]
    up = len(bars) < 2 or last.close >= bars[-2].close
    marks = [{"yAxis": last.close, "lineStyle": {"color": GREEN if up else RED, "type": "dashed", "width": 1},
              "label": {"position": "end", "formatter": f"{last.close:,.2f}", "color": "#131722", "fontSize": 11,
                        "backgroundColor": GREEN if up else RED, "padding": [2, 5], "borderRadius": 2}}]
    if base and base.get("pivot"):
        marks.append({"yAxis": base["pivot"], "lineStyle": {"color": MUTED, "type": "dotted", "width": 1},
                      "label": {"position": "insideStartTop", "distance": 4, "formatter": f"Pivot {base['pivot']:,.2f}",
                                "color": "#d1d4dc", "fontSize": 11, "backgroundColor": "#363a45", "padding": [2, 5],
                                "borderRadius": 2}})
    dates = [b.date for b in bars]
    areas, points = [], []
    if shade and base and base.get("pattern") and base.get("low") is not None and base.get("high") is not None:
        start, end = _locate(dates, base["start"], first=True), _locate(dates, base["end"], first=False)
        if start and end and start <= end:
            areas.append([{"xAxis": start, "yAxis": base["low"]}, {"xAxis": end, "yAxis": base["high"]}])
            handle = _locate(dates, base.get("handle_start"), first=True)
            if handle and handle <= end:
                areas.append([{"xAxis": handle, "yAxis": base["low"], "itemStyle": {"color": "rgba(209, 212, 220, 0.08)"}},
                              {"xAxis": end, "yAxis": base["high"]}])
        breakout = _locate(dates, base.get("breakout_date"), first=True)
        if breakout and base.get("pivot"):
            points.append({"coord": [breakout, base["pivot"]], "symbol": "triangle", "symbolSize": 10,
                           "itemStyle": {"color": "#d1d4dc", "borderColor": "#131722", "borderWidth": 1.5},
                           "label": {"show": False}})
    for day, (_, high, low, _) in zip(dates, candles):
        kind = (events or {}).get(day)
        if kind == "distribution":
            points.append({"coord": [day, high], "symbol": "triangle", "symbolRotate": 180, "symbolSize": 8,
                           "symbolOffset": [0, -9], "itemStyle": {"color": RED}, "label": {"show": False}})
        elif kind == "follow_through":
            points.append({"coord": [day, low], "symbol": "diamond", "symbolSize": 9, "symbolOffset": [0, 9],
                           "itemStyle": {"color": GREEN}, "label": {"show": False}})
    lows, highs = [c[2] for c in candles], [c[1] for c in candles]
    span = max(highs) - min(lows)
    # Hide any tick label the last-close tag would cover, as TradingView does.
    hide_near = f"v => Math.abs(v - {last.close:.4f}) < {span * 0.15:.4f} ? '' : v.toLocaleString('en')"
    # The price axis is as wide as the last-close tag, so four-digit prices are not clipped.
    axis = max(56, 14 + 7 * len(f"{last.close:,.2f}"))
    tone = "d-up" if last.close >= last_open else "d-down"
    # One axis label per month on its first bar; one per year once the chart spans several years.
    years = date.fromisoformat(dates[-1]).year - date.fromisoformat(dates[0]).year
    key = 4 if years >= 2 else 7
    firsts = [i for i, day in enumerate(dates) if i == 0 or day[:key] != dates[i - 1][:key]][1:]
    label = "{year: 'numeric'}" if key == 4 else "{month: 'short'}"
    font = {"fontFamily": "Inter, Segoe UI, sans-serif", "color": MUTED, "fontSize": 11}
    legend = (f'<span class="d-ohlc-date">{last.date}</span> O <b class="{tone}">{candles[-1][0]:,.2f}</b> '
              f'H <b class="{tone}">{candles[-1][1]:,.2f}</b> L <b class="{tone}">{candles[-1][2]:,.2f}</b> '
              f'C <b class="{tone}">{last.close:,.2f}</b><br>Vol <b>{compact(last.volume)}</b>'
              + "".join(f' <span class="d-ma" style="color:{colour}">{name} {fmt(values[-1])}</span>'
                        for name, values, colour in zip(names, lines_, AVERAGE_COLOURS)))
    with ui.element("div").classes("d-chart-wrap"):
        ui.html(legend, sanitize=False).classes("d-ohlc").props('aria-label="Latest bar"')
        ui.echart({
            "backgroundColor": "transparent", "animation": False, "textStyle": font,
            "grid": [{"left": 4, "right": axis, "top": 42, "height": "64%" if large else "60%"},
                     {"left": 4, "right": axis, "top": "84%" if large else "82%", "bottom": 20}],
            "tooltip": {"trigger": "axis", "position": [0, 0], "backgroundColor": "#1e222d",
                        "borderWidth": 0, "padding": [0, 8, 2, 6], "extraCssText": "box-shadow:none;pointer-events:none",
                        "textStyle": {**font, "color": "#d1d4dc", "fontSize": 11, "lineHeight": 16}, ":formatter": LEGEND,
                        "axisPointer": {"type": "cross", "lineStyle": {"color": "#50535e", "type": "dashed"},
                                        "crossStyle": {"color": "#50535e"}, "label": {"backgroundColor": "#363a45"}}},
            "axisPointer": {"link": [{"xAxisIndex": "all"}]},
            "xAxis": [{"type": "category", "data": dates, "axisLine": {"lineStyle": {"color": LINE}}, "axisTick": {"show": False},
                       "axisLabel": {"show": False}},
                      {"type": "category", "gridIndex": 1, "data": dates, "axisLine": {"lineStyle": {"color": LINE}},
                       "axisTick": {"show": False},
                       "axisLabel": {**font, "hideOverlap": True, ":interval": f"(i) => {firsts}.includes(i)",
                                     ":formatter": f"v => new Date(v + 'T12:00').toLocaleString('en', {label})"}}],
            # Headroom above the highest bar keeps the pivot chip clear of the legend.
            "yAxis": [{"scale": True, "position": "right", "splitNumber": 6 if large else 4, "boundaryGap": ["2%", "12%"],
                       "axisLabel": {**font, ":formatter": hide_near}, "splitLine": {"lineStyle": {"color": "#232733"}}},
                      {"gridIndex": 1, "position": "right", "splitNumber": 1, "axisLabel": {"show": False}, "splitLine": {"show": False}}],
            "series": [
                {"type": "candlestick", "name": "Price", "data": [[o, c, lo, hi] for o, hi, lo, c in candles],
                 "itemStyle": {"color": GREEN, "color0": RED, "borderColor": GREEN, "borderColor0": RED},
                 "markLine": {"symbol": ["none", "none"], "silent": True, "animation": False, "data": marks},
                 "markArea": {"silent": True, "itemStyle": {"color": "rgba(209, 212, 220, 0.05)", "borderColor": "#363a45",
                                                             "borderWidth": 1}, "data": areas},
                 "markPoint": {"silent": True, "animation": False, "data": points}},
                *[{"type": "line", "name": name, "data": values, "showSymbol": False, "smooth": False,
                   "lineStyle": {"width": 1.2, "color": colour}, "itemStyle": {"color": colour}, "z": 3}
                  for name, values, colour in zip(names, lines_, AVERAGE_COLOURS)],
                {"type": "bar", "name": "Volume", "xAxisIndex": 1, "yAxisIndex": 1,
                 "data": [{"value": b.volume, "itemStyle": {"color": GREEN if c[3] >= c[0] else RED, "opacity": 0.4}}
                          for b, c in zip(bars, candles)]},
            ],
        }).classes("d-chart d-chart-lg" if large else "d-chart")


def equity_chart(metrics: dict) -> bool:
    """A saved run's daily portfolio value beside SPY with dividends. False when the run saved no curve."""
    curve = metrics.get("equity_curve") or {}
    dates = curve.get("dates") or []
    series = [(key, label, colour, dashed) for key, label, colour, dashed in
              (("equity", "Strategy", "#d1d4dc", False), ("spy_total", "SPY with dividends", MUTED, True))
              if len(curve.get(key) or []) == len(dates)]
    if not dates or not series or series[0][0] != "equity":
        return False
    font = {"fontFamily": "Inter, Segoe UI, sans-serif", "color": MUTED, "fontSize": 11}
    years = [i for i, day in enumerate(dates) if i == 0 or day[:4] != dates[i - 1][:4]][1:]
    span_years = int(dates[-1][:4]) - int(dates[0][:4])
    label = "{year: 'numeric'}" if span_years >= 2 else "{month: 'short', year: '2-digit'}"
    ui.echart({
        "backgroundColor": "transparent", "animation": False, "textStyle": font,
        "grid": {"left": 4, "right": 64, "top": 34, "bottom": 22},
        "legend": {"top": 0, "left": 0, "itemWidth": 18, "itemHeight": 2, "textStyle": {**font, "color": "#d1d4dc"},
                   "icon": "rect"},
        "tooltip": {"trigger": "axis", "backgroundColor": "#1e222d", "borderColor": "#363a45",
                    "textStyle": {**font, "color": "#d1d4dc"},
                    ":valueFormatter": "v => v == null ? '–' : '$' + Math.round(v).toLocaleString('en')",
                    "axisPointer": {"type": "line", "lineStyle": {"color": "#50535e", "type": "dashed"}}},
        "xAxis": {"type": "category", "data": dates, "boundaryGap": False, "axisLine": {"lineStyle": {"color": LINE}},
                  "axisTick": {"show": False},
                  "axisLabel": {**font, "hideOverlap": True,
                                ":interval": f"(i) => {years}.includes(i)" if span_years >= 2 else "auto",
                                ":formatter": f"v => new Date(v + 'T12:00').toLocaleString('en', {label})"}},
        "yAxis": {"type": "value", "scale": True, "position": "right", "splitNumber": 4,
                  "axisLabel": {**font, ":formatter": "v => '$' + Intl.NumberFormat('en', {notation: 'compact'}).format(v)"},
                  "splitLine": {"lineStyle": {"color": "#232733"}}},
        "series": [{"type": "line", "name": label_, "data": curve[key], "showSymbol": False, "smooth": False,
                    "lineStyle": {"width": 1.6, "color": colour, "type": "dashed" if dashed else "solid"},
                    "itemStyle": {"color": colour}}
                   for key, label_, colour, dashed in series],
    }).classes("d-chart d-equity")
    return True


def backtest_chart(report: dict, exposure: list[float]):
    """A backtest's portfolio value beside SPY buy and hold, over the market's allowed exposure."""
    curve = report["equity_curve"]
    dates = [p["date"] for p in curve]
    font = {"fontFamily": "Inter, Segoe UI, sans-serif", "color": MUTED, "fontSize": 11}
    span_years = int(dates[-1][:4]) - int(dates[0][:4]) if dates else 0
    firsts = [i for i, day in enumerate(dates) if i == 0 or day[:4 if span_years >= 2 else 7] != dates[i - 1][:4 if span_years >= 2 else 7]][1:]
    label = "{year: 'numeric'}" if span_years >= 2 else "{month: 'short'}"
    name = {"leaders": "Leaders", "trend": "Trend leaders"}.get(report.get("strategy"), "Strategy")
    axis = {"type": "category", "data": dates, "boundaryGap": False, "axisLine": {"lineStyle": {"color": LINE}},
            "axisTick": {"show": False}}
    ui.echart({
        "backgroundColor": "transparent", "animation": False, "textStyle": font,
        "legend": {"top": 0, "left": 0, "itemWidth": 18, "itemHeight": 2, "icon": "rect",
                   "textStyle": {**font, "color": "#d1d4dc"}},
        "grid": [{"left": 4, "right": 64, "top": 34, "height": "64%"}, {"left": 4, "right": 64, "top": "80%", "bottom": 22}],
        "tooltip": {"trigger": "axis", "backgroundColor": "#1e222d", "borderColor": "#363a45",
                    "textStyle": {**font, "color": "#d1d4dc"},
                    "axisPointer": {"type": "line", "lineStyle": {"color": "#50535e", "type": "dashed"}}},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "xAxis": [{**axis, "axisLabel": {"show": False}},
                  {**axis, "gridIndex": 1, "axisLabel": {**font, "hideOverlap": True, ":interval": f"(i) => {firsts}.includes(i)",
                                                          ":formatter": f"v => new Date(v + 'T12:00').toLocaleString('en', {label})"}}],
        "yAxis": [{"type": "value", "scale": True, "position": "right", "splitNumber": 4,
                   "axisLabel": {**font, ":formatter": "v => '$' + Intl.NumberFormat('en', {notation: 'compact'}).format(v)"},
                   "splitLine": {"lineStyle": {"color": "#232733"}}},
                  {"type": "value", "gridIndex": 1, "position": "right", "min": 0, "max": 100, "interval": 50,
                   "axisLabel": {**font, "formatter": "{value}%"}, "splitLine": {"show": False}}],
        "series": [
            {"type": "line", "name": name, "data": [p["equity"] for p in curve], "showSymbol": False,
             "lineStyle": {"width": 1.8, "color": "#d1d4dc"}, "itemStyle": {"color": "#d1d4dc"},
             "tooltip": {":valueFormatter": "v => '$' + Math.round(v).toLocaleString('en')"}},
            {"type": "line", "name": "SPY buy and hold", "data": [p["spy_equity"] for p in curve], "showSymbol": False,
             "lineStyle": {"width": 1.4, "color": MUTED, "type": "dashed"}, "itemStyle": {"color": MUTED},
             "tooltip": {":valueFormatter": "v => '$' + Math.round(v).toLocaleString('en')"}},
            {"type": "line", "name": "Allowed exposure", "xAxisIndex": 1, "yAxisIndex": 1, "data": exposure, "step": "end",
             "showSymbol": False, "lineStyle": {"width": 1, "color": GREEN}, "itemStyle": {"color": GREEN},
             "areaStyle": {"color": "rgba(38, 166, 154, 0.15)"}, "tooltip": {":valueFormatter": "v => v + '%'"}},
        ],
    }).classes("d-chart d-backtest-chart")
