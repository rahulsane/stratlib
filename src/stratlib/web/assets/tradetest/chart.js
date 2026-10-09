// TradeTest chart: a canvas candle chart for the replay. It paints the bars, volume, moving averages, axes and
// crosshair, the visitor's drawings and trades, and handles making and editing them with a mouse, pen or finger.
// ChartView is the live chart on the page; renderStatic paints a finished chart whole for the report with the same
// code. No network, no storage: the page owns the state and hears about every change through the handlers.
//
// Coordinates are bar indexes (bar centres at integers, 0 .. nCtx + nReplay - 1) and chart prices. A view is
// { right, span }: the bar index at the plot's right edge and the number of bars across the plot, plus lo and hi
// once the visitor has scaled the price axis by hand (auto scaling otherwise).
//
// API reference
//   PALETTE        drawing colours by name; MA_LINES the four moving averages { key, col, color, name }
//   FUTURE_BARS    how far past the newest bar an anchor may sit; EXTENDABLE the drawing types with Extend right
//   barNumber(chart, i) -> "Bar 23" | "Bar −149"      replay bar j is index nCtx + j - 1; the last context bar is 0
//   fmtDate(iso) -> "Mar 12, 2019"
//   defaultView(chart, width) -> { right, span }      about 120 bars on a desktop, fewer on a narrow plot
//   new ChartView(host, handlers)    host is an empty element; the view adds its canvas, legend, hint and Latest
//                                    button (back to the newest bar, shown once it is panned out of view) to it
//     handlers (each optional):
//       create(drawing)                 a drawing was finished: { type, points, color, label, extend }
//       edit(drawing, before)           a drawing was dragged; it was changed in place, before is a copy
//       select(selection)               { kind: "drawing" | "trade", id } or null
//       draft(draft)                    the order being placed: { side, entry (null: market), stop, target, done }
//       level(trade, { stop, target }) -> message | null    a trade's stop or target was dragged; a message refuses
//       label(drawing)                  a drawing was double-clicked
//       tool(name)                      the view is done with a tool and hands back the cursor
//       started()                       a drawing or an order was begun on the chart (its first point or entry)
//       view(view)                      the visitor zoomed or panned
//     .update({ chart, results, settings, locked, selection, draft })   results[n] = simulate(chart.trades[n])
//     .setTool(name, preset)     preset { color, label } starts a drawing tool's drawings with them (zone presets)
//     .cancel() -> bool  .fit()  .showWhole()  .busy() -> bool  .resize()  .destroy()
//     .dropHalfMade(sentAt) -> bool    the replay stepped: a drawing or order begun before sentAt (performance.now())
//                                and not done yet is dropped, the tool kept
//     .flushView()               hands a pending view change to the view handler now (before a save)
//     .showHint(text, tone)      a message over the chart for a few seconds ("error" tone for refusals)
//     .clearFlash()              ends that message now: the tool's own hint shows again
//   renderStatic(canvas, chart, { width = 1200, height = 560, scale = 1, settings }) -> canvas
//       The whole window: every bar, the drawings and trades, the replay start and finish lines, and the real dates on
//       the time axis once chart.reveal is set.

const sim = await import(`./sim.js${new URL(import.meta.url).search}`);

const O = 0, H = 1, L = 2, C = 3, V = 4;

const COLORS = {
  bg: "#131722", panel: "#1e222d", raised: "#262a35", line: "#2a2e39", line2: "#363a45", text: "#d1d4dc",
  bright: "#f0f3fa", muted: "#9598a1", faint: "#50535e", blue: "#2962ff", green: "#26a69a", red: "#ef5350",
  greenText: "#3cc4b5", redText: "#ff7f7c", grid: "#232733",
};
export const PALETTE = {
  text: "#d1d4dc", steel: "#a3abbd", brass: "#c9a26e", teal: "#3cc4b5", coral: "#ff7f7c", violet: "#b39ddb",
  amber: "#ffb74d",
};
export const MA_LINES = [
  { key: "e9", col: 5, color: "#b39ddb", name: "EMA 9" }, { key: "e21", col: 6, color: "#e8a87c", name: "EMA 21" },
  { key: "s50", col: 7, color: "#a3abbd", name: "SMA 50" }, { key: "s200", col: 8, color: "#c9a26e", name: "SMA 200" },
];
export const FUTURE_BARS = 30;
export const EXTENDABLE = new Set(["trend", "channel", "zone"]);
const DRAW_TOOLS = new Set(["trend", "ray", "channel", "hline", "vline", "zone"]);
const MARGIN_BARS = 12;            // empty bars right of the newest in the default view
const DEFAULT_BARS = 120;
const AXIS_H = 26;
const MIN_AXIS_W = 64;
const MARKET_SHARE = 0.006;        // an entry click this close to the last price (share of plot height) is "market"
const MAGNET_PX = 12;
const FAMILY = 'Inter, -apple-system, "Segoe UI", system-ui, sans-serif';
const font = (size, weight = 500) => `${weight} ${size}px ${FAMILY}`;
const MINUS = "−";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const crisp = (v) => Math.round(v) + 0.5;
const isNum = (x) => typeof x === "number" && Number.isFinite(x);
const round4 = (p) => Math.round(p * 1e4) / 1e4;
const fmt = (p) => sim.fmtPrice(p);
const copy = (d) => JSON.parse(JSON.stringify(d));

function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export function barNumber(chart, i) {
  const n = i - (chart.nCtx - 1);
  return `Bar ${n < 0 ? MINUS + -n : n}`;
}

export function fmtDate(iso) {
  const [y, m, d] = String(iso).split("-").map(Number);
  return m ? `${MONTHS[m - 1]} ${d}, ${y}` : "";
}

// The newest bar the chart shows: the current bar while replaying, the window's last once finished.
function newestIndex(chart) {
  const loaded = chart.bars.length - 1;
  return chart.finished ? loaded : Math.min(loaded, chart.nCtx + chart.k - 1);
}

function finishIndex(chart) {
  return chart.finished ? chart.nCtx + (chart.finishedAt ?? chart.k) - 1 : null;
}

export function defaultView(chart, width = 1000) {
  const newest = newestIndex(chart), bars = clamp(Math.round((width - MIN_AXIS_W) / 7), 50, DEFAULT_BARS);
  if (chart.finished) {
    return { right: newest + MARGIN_BARS / 2, span: Math.max(bars, Math.min(chart.nReplay + 40, bars * 1.6)) };
  }
  return { right: newest + MARGIN_BARS, span: bars + MARGIN_BARS };
}

function clampView(view, chart) {
  const newest = Math.max(0, newestIndex(chart));
  const span = clamp(view.span, 16, 420);
  const right = clamp(view.right, Math.min(newest, 12), newest + Math.max(FUTURE_BARS + 4, span * 0.5));
  const out = { right, span };
  if (isNum(view.lo) && isNum(view.hi) && view.hi > view.lo) Object.assign(out, { lo: view.lo, hi: view.hi });
  return out;
}

// ---------------------------------------------------------------- geometry

// The visible price range when scaled automatically: the bars and moving averages in view, plus the levels of live
// orders and trades, padded top and bottom (more at the bottom when the volume overlay is on). topShare, a share of
// the plot's height, is the room a narrow chart's legend needs above the bars.
function autoRange(chart, view, model, extras, topShare = 0) {
  const { bars } = chart, newest = newestIndex(chart);
  const from = Math.max(0, Math.floor(view.right - view.span)), to = Math.min(newest, Math.ceil(view.right));
  const mas = MA_LINES.filter((ma) => model.settings?.ma?.[ma.key]);
  let lo = Infinity, hi = -Infinity;
  for (let i = from; i <= to; i++) {
    const bar = bars[i];
    if (!bar) continue;
    lo = Math.min(lo, bar[L]); hi = Math.max(hi, bar[H]);
    for (const ma of mas) if (isNum(bar[ma.col])) { lo = Math.min(lo, bar[ma.col]); hi = Math.max(hi, bar[ma.col]); }
  }
  if (!Number.isFinite(lo)) return null;
  // Order levels far from the bars stay out, so a mistyped price can't squash the candles into a line.
  const reach = 1.5 * Math.max(hi - lo, 1e-6 * hi);
  const near = extras.filter((p) => isNum(p) && p > lo - reach && p < hi + reach);
  for (const p of near) { lo = Math.min(lo, p); hi = Math.max(hi, p); }
  if (hi - lo < 1e-6) { hi += 0.5; lo -= 0.5; }
  const top = clamp(topShare, 0.08, 0.35), bottom = model.settings?.volume === false ? 0.08 : 0.17;
  const whole = (hi - lo) / (1 - top - bottom);
  return { lo: lo - bottom * whole, hi: hi + top * whole };
}

// Levels that auto scaling keeps in view: live trades' entries, stops and targets, and a finished draft.
function liveLevels(model) {
  const out = [];
  const { chart, results = [], draft } = model;
  (chart.trades || []).forEach((trade, n) => {
    const res = results[n];
    if (!res || !["pending", "open", "closing"].includes(res.status)) return;
    out.push(res.stopNow, res.targetNow);
    if (res.status === "pending" && trade.entry != null) out.push(trade.entry);
  });
  if (draft && draft.done) out.push(draft.entry, draft.stop, draft.target);
  return out;
}

function geometry(ctx, width, height, view, range) {
  ctx.font = font(11);
  const widest = Math.max(ctx.measureText(fmt(range.hi)).width, ctx.measureText(fmt(range.lo)).width);
  const axisW = Math.max(MIN_AXIS_W, Math.ceil(widest) + 20);
  const plotW = Math.max(20, width - axisW), plotH = Math.max(20, height - AXIS_H);
  const barW = plotW / view.span, { lo, hi } = range;
  return {
    width, height, plotW, plotH, axisW, barW, lo, hi, right: view.right, left: view.right - view.span,
    x: (i) => plotW - (view.right - i) * barW,
    i: (x) => view.right - (plotW - x) / barW,
    y: (p) => (hi - p) / (hi - lo) * plotH,
    p: (y) => hi - y / plotH * (hi - lo),
  };
}

function niceStep(range, count) {
  const raw = range / Math.max(1, count), mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
}

// ---------------------------------------------------------------- drawing shapes (screen space)

function linePrice(a, b, x) {
  return a.x === b.x ? a.p : a.p + (b.p - a.p) * (x - a.x) / (b.x - a.x);
}

// Where a line from a through b leaves the plot, far enough that the clip does the rest.
function reach(a, b, g) {
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
  if (len < 1e-9) return b;
  const far = (g.plotW + g.plotH) * 4;
  return { x: a.x + dx / len * far, y: a.y + dy / len * far };
}

// mode: "segment" between the points, "right" extended past the right-hand point, "ray" from a through b.
function segment(a, b, mode, g) {
  if (mode === "ray") return [a, reach(a, b, g)];
  if (mode === "right") {
    const [l, r] = a.x <= b.x ? [a, b] : [b, a];
    return r.x - l.x < 1e-9 ? [a, b] : [l, reach(l, r, g)];
  }
  return [a, b];
}

function shape(d, g) {
  const pt = (q) => ({ x: g.x(q.x), y: g.y(q.p) });
  const P = d.points.map(pt);
  switch (d.type) {
    case "trend": return { segs: [segment(P[0], P[1], d.extend ? "right" : "segment", g)], anchor: leftOf(P[0], P[1]) };
    case "ray": return { segs: [segment(P[0], P[1], "ray", g)], anchor: P[0] };
    case "channel": {
      // While the base line is being drawn there is no third point yet: just the base.
      if (d.points.length < 3) return { segs: [segment(P[0], P[1], "segment", g)], anchor: leftOf(P[0], P[1]) };
      const [a, b, c] = d.points, off = c.p - linePrice(a, b, c.x), mode = d.extend ? "right" : "segment";
      const s1 = segment(P[0], P[1], mode, g);
      const s2 = segment(pt({ x: a.x, p: a.p + off }), pt({ x: b.x, p: b.p + off }), mode, g);
      const mid = segment(pt({ x: a.x, p: a.p + off / 2 }), pt({ x: b.x, p: b.p + off / 2 }), mode, g);
      const upper = off > 0 ? s2 : s1;
      return { segs: [s1, s2], mid, poly: [s1[0], s1[1], s2[1], s2[0]], anchor: leftOf(upper[0], upper[1]) };
    }
    case "hline": {
      const y = g.y(d.points[0].p);
      return { segs: [[{ x: -10, y }, { x: g.plotW + 10, y }]], anchor: { x: 6, y } };
    }
    case "vline": {
      const x = g.x(d.points[0].x);
      return { segs: [[{ x, y: -10 }, { x, y: g.plotH + 10 }]], anchor: { x, y: 0 } };
    }
    case "zone": {
      const [a, b] = d.points;
      const x0 = g.x(Math.min(a.x, b.x)), y0 = g.y(Math.max(a.p, b.p)), y1 = g.y(Math.min(a.p, b.p));
      const x1 = d.extend ? g.plotW + 2 : Math.max(g.x(Math.max(a.x, b.x)), x0 + 2);
      return { rect: { x0, y0, x1, y1 }, anchor: { x: x0, y: y0 } };
    }
    default: return { segs: [] };
  }
}

const leftOf = (a, b) => (a.x <= b.x ? a : b);

// The anchors a selected drawing shows, each with how dragging it changes the drawing.
function handles(d, g) {
  const pt = (q) => ({ x: g.x(q.x), y: g.y(q.p) });
  const [a, b] = d.points;
  switch (d.type) {
    case "trend": case "ray": case "channel": return d.points.map((q, k) => ({ ...pt(q), k }));
    case "hline": return [{ x: clamp(g.x(a.x), 14, g.plotW - 14), y: g.y(a.p), k: 0 }];
    case "vline": return [{ x: g.x(a.x), y: clamp(g.y(a.p), 14, g.plotH - 14), k: 0 }];
    case "zone": return [
      { x: g.x(a.x), y: g.y(a.p), k: 0 }, { x: g.x(b.x), y: g.y(b.p), k: 1 },
      { x: g.x(a.x), y: g.y(b.p), k: 2 }, { x: g.x(b.x), y: g.y(a.p), k: 3 },
    ];
    default: return [];
  }
}

function moveHandle(d, k, q, before) {
  const pts = d.points;
  if (d.type === "hline") { pts[0] = { x: q.x, p: q.p }; return; }
  if (d.type === "vline") { pts[0] = { x: q.x, p: q.p }; return; }
  if (d.type === "zone") {
    const [a, b] = before.points;
    const set = [[0, 0], [1, 1], [0, 1], [1, 0]][k];    // which point's x, which point's price
    const next = [{ ...a }, { ...b }];
    next[set[0]].x = q.x; next[set[1]].p = q.p;
    d.points = next;
    return;
  }
  if (d.type === "channel" && k < 2) {
    // Moving a base anchor keeps the channel's width.
    const [a, b, c] = before.points, off = c.p - linePrice(a, b, c.x);
    const next = [{ ...a }, { ...b }];
    next[k] = { x: q.x, p: q.p };
    d.points = [...next, { x: c.x, p: round4(linePrice(next[0], next[1], c.x) + off) }];
    return;
  }
  pts[k] = { x: q.x, p: q.p };
}

// The whole drawing moves by one shift, held back at bar 0 and at the future limit, so its shape never changes.
function translate(d, before, dx, dp, maxX) {
  const xs = before.points.map((q) => q.x);
  const shift = clamp(dx, -Math.min(...xs), maxX - Math.max(...xs));
  d.points = before.points.map((q) => ({ x: q.x + shift, p: round4(q.p + dp) }));
}

function distToSeg(px, py, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy;
  const t = len2 ? clamp(((px - a.x) * dx + (py - a.y) * dy) / len2, 0, 1) : 0;
  return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
}

function inPoly(px, py, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i], b = poly[j];
    if ((a.y > py) !== (b.y > py) && px < (b.x - a.x) * (py - a.y) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

// Lines and edges first ("edge"), then the inside of zones and channels ("fill"), so a line drawn across a zone or
// inside a channel can still be picked.
function hitDrawing(d, g, x, y, tol, pass) {
  const s = shape(d, g);
  if (s.rect) {
    const { x0, y0, x1, y1 } = s.rect;
    const inside = x >= x0 - tol && x <= x1 + tol && y >= y0 - tol && y <= y1 + tol;
    if (pass === "fill") return inside;
    const near = (a, b) => Math.abs(a - b) <= tol;
    return inside && (near(x, x0) || near(x, x1) || near(y, y0) || near(y, y1));
  }
  if (pass === "fill") return !!(s.poly && inPoly(x, y, s.poly));
  for (const [a, b] of s.segs) if (distToSeg(x, y, a, b) <= tol) return true;
  return !!(s.mid && distToSeg(x, y, s.mid[0], s.mid[1]) <= tol);
}

// ---------------------------------------------------------------- painting primitives

function strokeLine(ctx, a, b) {
  ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
}

function hLine(ctx, y, x0, x1) {
  ctx.beginPath(); ctx.moveTo(x0, crisp(y)); ctx.lineTo(x1, crisp(y)); ctx.stroke();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}

// Text with a halo in the background colour, so it reads over candles and gridlines.
function haloText(ctx, text, x, y, color) {
  ctx.lineJoin = "round"; ctx.lineWidth = 3; ctx.strokeStyle = COLORS.bg;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color; ctx.fillText(text, x, y);
}

// The highest a label h tall, starting at left, may be centred on the live chart: below the OHLC legend over the
// plot's top-left corner, or across the whole top of a narrow chart, where the legend wraps to two or three lines.
// legend is { bottom, wide }; the report's pictures have none.
function underLegend(legend, left, h) {
  if (!legend || (left >= 560 && !legend.wide)) return -Infinity;
  return Math.max(37, legend.bottom + 3) + h / 2;
}

// A small label box on the plot.
function pill(ctx, text, x, y, opts = {}) {
  const { color = COLORS.text, bg = COLORS.raised, border = COLORS.line2, align = "left", limit = Infinity } = opts;
  const { taken, legend } = opts;
  ctx.font = font(11);
  const w = Math.ceil(ctx.measureText(text).width) + 10, h = 18;
  // limit is the right edge the box may not cross (the plot's), so a label near it slides left instead of clipping.
  const left = Math.max(2, Math.min(align === "right" ? x - w : align === "center" ? x - w / 2 : x, limit - w));
  // Never over the OHLC legend (the report's pictures keep the corner clear too).
  y = Math.max(y, legend ? underLegend(legend, left, h) : left < 560 ? 46 : -Infinity);
  // taken lists the boxes already drawn this frame: a label that would cover one steps down until it is clear.
  if (taken) {
    const hits = (top) => taken.some((r) => left < r.x + r.w && r.x < left + w && top < r.y + r.h && r.y < top + h);
    for (let tries = 0; tries < 8 && hits(y - h / 2); tries++) y += h + 2;
    taken.push({ x: left, y: y - h / 2, w, h });
  }
  roundRect(ctx, Math.round(left) + 0.5, Math.round(y - h / 2) + 0.5, w, h, 3);
  ctx.fillStyle = bg; ctx.fill();
  if (border) { ctx.strokeStyle = border; ctx.lineWidth = 1; ctx.stroke(); }
  ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, Math.round(left) + 5.5, Math.round(y) + 1);
  return { x: left, w, y, h };
}

const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

// A label moved off its line keeps a short leader back to it.
function leader(ctx, box, lineY, color) {
  const top = box.y - box.h / 2, bottom = box.y + box.h / 2;
  if (lineY >= top - 4 && lineY <= bottom + 4) return;
  const x = crisp(box.x + 8);
  ctx.strokeStyle = rgba(color, 0.55); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x, lineY); ctx.lineTo(x, lineY < top ? top : bottom); ctx.stroke();
}

// Labels after every line is down, so no level runs through one: first each closed trade's result by its exit (it
// stays put), then the orders' and positions' labels, each stepped clear of the labels before it (with a leader when
// that moves it off its line), then the drawings' labels, moved up or down off any trade label they would cover.
function paintLabels(ctx, g, ui) {
  const taken = ui.labels, hits = (r) => taken.some((t) => overlaps(r, t));
  for (const job of ui.tradeLabels) {
    if (job.type !== "halo") continue;
    const y = Math.max(job.y, underLegend(ui.legend, job.x, 18));
    ctx.font = font(11); ctx.textAlign = "left"; ctx.textBaseline = "middle";
    haloText(ctx, job.text, job.x, y, job.color);
    taken.push({ x: job.x - 2, y: y - 9, w: job.w + 4, h: 18 });
    ui.labelHits.push({ trade: job.trade, x: job.x - 2, y: y - 9, w: job.w + 4, h: 18 });
  }
  for (const job of ui.tradeLabels) {
    if (job.type !== "pill") continue;
    ctx.globalAlpha = job.alpha;
    const box = pill(ctx, job.text, job.x, job.y, { ...job.opts, taken, legend: ui.legend });
    if (Math.abs(box.y - job.y) > 2) leader(ctx, box, job.line, job.opts.color || COLORS.text);
    ctx.globalAlpha = 1;
    ui.labelHits.push({ trade: job.trade, x: box.x, y: box.y - box.h / 2, w: box.w, h: box.h });
  }
  for (const job of ui.drawingLabels) {
    const box = (dx, dy) => ({ x: job.x + dx - 2, y: job.y + dy - 11, w: job.w + 4, h: 15 });
    const fits = ([dx, dy]) => !hits(box(dx, dy)) && job.y + dy > 12 && job.y + dy < g.plotH - 4
      && job.x + dx + job.w < g.plotW - 4;
    // A row up or down, else along to the right of what it would cover, else further up or down.
    let spot = [[0, 0], [0, 16], [0, -16], [0, 32], [0, -32]].find(fits);
    for (let dx = 0, n = 0; !spot && n < 6; n++) {
      const over = taken.filter((t) => overlaps(box(dx, 0), t));
      if (!over.length) break;
      dx = Math.max(...over.map((t) => t.x + t.w)) - job.x + 6;
      if (fits([dx, 0])) spot = [dx, 0];
    }
    spot ||= [[0, 48], [0, -48], [0, 64], [0, -64]].find(fits) || [0, 0];
    ctx.font = font(11); ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
    haloText(ctx, job.text, job.x + spot[0], job.y + spot[1], job.color);
    taken.push(box(spot[0], spot[1]));
  }
}

// Price-axis tags laid out together: sorted by price, any two closer than a tag's height pushed apart (a fixed one,
// the last close, stays on its price), all kept on the axis. Each comes back with at, where it is drawn.
function layoutTags(tags, g) {
  const h = 19, lo = 9, hi = g.plotH - 9;
  const items = tags.filter((t) => t.y >= -9 && t.y <= g.plotH + 9).map((t) => ({ ...t, at: clamp(t.y, lo, hi) }));
  for (let pass = 0; pass < 24; pass++) {
    items.sort((a, b) => a.at - b.at);
    let moved = false;
    for (let n = 1; n < items.length; n++) {
      const a = items[n - 1], b = items[n], overlap = h - (b.at - a.at);
      if (overlap <= 0.01 || (a.fixed && b.fixed)) continue;
      moved = true;
      if (a.fixed) b.at += overlap;
      else if (b.fixed) a.at -= overlap;
      else { a.at -= overlap / 2; b.at += overlap / 2; }
    }
    for (const t of items) if (!t.fixed) t.at = clamp(t.at, lo, hi);
    if (!moved) break;
  }
  return items;
}

// A tag on the price axis. Filled tags carry slate text; outlined ones a tint and their colour.
function axisTag(ctx, g, y, text, { fill, color, outline }) {
  if (y < -9 || y > g.plotH + 9) return;
  const h = 18, top = Math.round(clamp(y, h / 2, g.plotH - h / 2) - h / 2);
  ctx.font = font(11, 600);
  roundRect(ctx, g.plotW + 1, top, g.axisW - 2, h, 2);
  if (outline) {
    ctx.fillStyle = COLORS.panel; ctx.fill();
    ctx.fillStyle = rgba(outline, 0.16); ctx.fill();
    ctx.strokeStyle = rgba(outline, 0.85); ctx.lineWidth = 1; ctx.stroke();
  } else { ctx.fillStyle = fill; ctx.fill(); }
  ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, g.plotW + 8, top + h / 2 + 1);
}

function timeTag(ctx, g, x, text, { fill = COLORS.line2, color = COLORS.bright, outline } = {}) {
  ctx.font = font(11, 500);
  const w = Math.ceil(ctx.measureText(text).width) + 12, left = clamp(Math.round(x - w / 2), 0, g.plotW - w);
  roundRect(ctx, left, g.plotH + 4, w, 18, 2);
  if (outline) {
    ctx.fillStyle = COLORS.panel; ctx.fill(); ctx.fillStyle = rgba(outline, 0.16); ctx.fill();
    ctx.strokeStyle = rgba(outline, 0.85); ctx.lineWidth = 1; ctx.stroke();
  } else { ctx.fillStyle = fill; ctx.fill(); }
  ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, left + w / 2, g.plotH + 14);
}

function triangle(ctx, x, y, up, color) {
  const s = 6;
  ctx.beginPath();
  if (up) { ctx.moveTo(x, y - s * 0.2); ctx.lineTo(x - s, y + s); ctx.lineTo(x + s, y + s); }
  else { ctx.moveTo(x, y + s * 0.2); ctx.lineTo(x - s, y - s); ctx.lineTo(x + s, y - s); }
  ctx.closePath();
  ctx.fillStyle = color; ctx.fill();
  ctx.strokeStyle = COLORS.bg; ctx.lineWidth = 1; ctx.stroke();
}

function cross(ctx, x, y, color) {
  const s = 5;
  ctx.lineCap = "round";
  // Twice: a halo in the background colour, then the cross itself.
  for (const [stroke, width] of [[COLORS.bg, 4], [color, 2]]) {
    ctx.strokeStyle = stroke; ctx.lineWidth = width;
    strokeLine(ctx, { x: x - s, y: y - s }, { x: x + s, y: y + s });
    strokeLine(ctx, { x: x + s, y: y - s }, { x: x - s, y: y + s });
  }
  ctx.lineCap = "butt";
}

// ---------------------------------------------------------------- the painter

// Everything the chart paints, in order: grid, shading and markers, volume, averages, candles, trades, drawings,
// the order being placed, the crosshair, then the axes and their tags. ui carries the live view's transient state
// (hover, selection, previews); renderStatic passes none.
function paint(ctx, g, model, ui = {}) {
  const { chart } = model, settings = model.settings || {};
  const { bars, nCtx } = chart, newest = newestIndex(chart), done = finishIndex(chart);
  const first = Math.max(0, Math.floor(g.left) - 1), last = Math.min(newest, Math.ceil(g.right) + 1);

  ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, g.width, g.height);
  const step = niceStep(g.hi - g.lo, g.plotH / 52), ticks = [];
  for (let p = Math.ceil(g.lo / step) * step; p <= g.hi; p += step) ticks.push(p);
  ctx.fillStyle = COLORS.grid;
  for (const p of ticks) ctx.fillRect(0, Math.round(g.y(p)), g.plotW, 1);

  ctx.save();
  ctx.beginPath(); ctx.rect(0, 0, g.plotW, g.plotH); ctx.clip();

  // The bars still to come, shaded faintly.
  if (!chart.finished && !ui.static) {
    const xf = g.x(newest + 0.5);
    if (xf < g.plotW) { ctx.fillStyle = "rgba(209, 212, 220, 0.025)"; ctx.fillRect(xf, 0, g.plotW - xf, g.plotH); }
  }
  const replayLabel = marker(ctx, g, nCtx - 0.5, "Replay starts");

  const bodyW = Math.max(1, Math.floor(g.barW * 0.72) - (Math.floor(g.barW * 0.72) % 2 === 0 ? 1 : 0));
  if (settings.volume !== false) {
    let vmax = 0;
    for (let i = first; i <= last; i++) if (bars[i] && isNum(bars[i][V])) vmax = Math.max(vmax, bars[i][V]);
    if (vmax > 0) {
      for (let i = first; i <= last; i++) {
        const bar = bars[i];
        if (!bar || !isNum(bar[V])) continue;
        const h = bar[V] / vmax * g.plotH * 0.18, xc = Math.round(g.x(i));
        ctx.fillStyle = rgba(bar[C] >= bar[O] ? COLORS.green : COLORS.red, done != null && i > done ? 0.22 : 0.4);
        ctx.fillRect(xc - (bodyW - 1) / 2, g.plotH - h, bodyW, h);
      }
    }
  }

  for (const ma of MA_LINES) {
    if (!settings.ma?.[ma.key]) continue;
    ctx.strokeStyle = ma.color; ctx.lineWidth = 1.2; ctx.lineJoin = "round";
    ctx.beginPath();
    let open = false;
    for (let i = first; i <= last; i++) {
      const v = bars[i]?.[ma.col];
      if (!isNum(v)) { open = false; continue; }
      if (open) ctx.lineTo(g.x(i), g.y(v)); else { ctx.moveTo(g.x(i), g.y(v)); open = true; }
    }
    ctx.stroke();
  }

  for (let i = first; i <= last; i++) {
    const bar = bars[i];
    if (!bar) continue;
    const up = bar[C] >= bar[O], xc = Math.round(g.x(i));
    ctx.globalAlpha = done != null && i > done ? 0.55 : 1;
    ctx.fillStyle = up ? COLORS.green : COLORS.red;
    const yh = g.y(bar[H]), yl = g.y(bar[L]);
    ctx.fillRect(xc, Math.round(yh), 1, Math.max(1, Math.round(yl) - Math.round(yh)));
    if (bodyW >= 3) {
      const top = Math.round(g.y(Math.max(bar[O], bar[C]))), bot = Math.round(g.y(Math.min(bar[O], bar[C])));
      ctx.fillRect(xc - (bodyW - 1) / 2, top, bodyW, Math.max(1, bot - top));
    }
  }
  ctx.globalAlpha = 1;
  if (done != null) marker(ctx, g, done + 0.5, "Finished", replayLabel);

  // The last price: a dashed line in the bar's direction.
  const lastBar = bars[newest];
  const lastUp = lastBar ? lastBar[C] >= lastBar[O] : true;
  if (lastBar) {
    ctx.strokeStyle = rgba(lastUp ? COLORS.green : COLORS.red, ui.marketHot ? 1 : 0.7);
    ctx.lineWidth = ui.marketHot ? 2 : 1; ctx.setLineDash([2, 3]);
    hLine(ctx, g.y(lastBar[C]), 0, g.plotW); ctx.setLineDash([]);
  }

  // Labels wait until every trade's lines and every drawing are down (see paintLabels). labelHits: where each trade's
  // label landed, so a click on it picks the trade.
  ui.labels = []; ui.tradeLabels = []; ui.drawingLabels = []; ui.labelHits = [];
  (chart.trades || []).forEach((trade, n) => {
    const res = model.results?.[n];
    if (res) paintTrade(ctx, g, chart, trade, res, ui);
  });
  for (const d of chart.drawings || []) paintDrawing(ctx, g, d, ui);
  paintLabels(ctx, g, ui);
  if (ui.pending) paintDrawing(ctx, g, ui.pending, { ...ui, preview: true, drawingLabels: null });
  const draft = ui.placing || model.draft;
  if (draft && lastBar) paintDraft(ctx, g, chart, draft, ui);
  if (ui.levelPreview) {
    const { which, price } = ui.levelPreview;
    ctx.strokeStyle = which === "stop" ? COLORS.red : COLORS.green; ctx.lineWidth = 2; ctx.setLineDash([5, 4]);
    hLine(ctx, g.y(price), 0, g.plotW); ctx.setLineDash([]);
  }
  const hair = ui.crosshair;
  if (hair) {
    ctx.strokeStyle = COLORS.faint; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(crisp(hair.x), 0); ctx.lineTo(crisp(hair.x), g.plotH); ctx.stroke();
    hLine(ctx, hair.y, 0, g.plotW);
    ctx.setLineDash([]);
  }
  ctx.restore();

  // Axes: hairlines, price labels (blanked under every tag, and well clear of the last-close tag), bar numbers or
  // dates. The price tags (price levels, a trade's levels, the order being placed, the last close) are laid out
  // together first, so none hides another.
  ctx.fillStyle = COLORS.line;
  ctx.fillRect(g.plotW, 0, 1, g.plotH + 1); ctx.fillRect(0, g.plotH, g.width, 1);
  const lastY = lastBar ? g.y(lastBar[C]) : -100;
  const tags = [];
  for (const d of chart.drawings || []) {
    if (d.type !== "hline") continue;
    const color = PALETTE[d.color] || PALETTE.steel;
    tags.push({ y: g.y(d.points[0].p), text: fmt(d.points[0].p), style: { outline: color, color } });
  }
  for (const t of ui.tags || []) tags.push({ y: g.y(t.price), text: fmt(t.price), style: t.style });
  if (lastBar) {
    tags.push({ y: lastY, text: fmt(lastBar[C]), style: { fill: lastUp ? COLORS.green : COLORS.red, color: COLORS.bg },
                fixed: true });
  }
  // A market order's entry is the last close itself: the last-close tag says it once.
  const placed = layoutTags(tags.filter((t) => t.fixed || !tags.some((f) => f.fixed && f.text === t.text
    && Math.abs(f.y - t.y) < 1)), g);
  ctx.font = font(11); ctx.textBaseline = "middle"; ctx.textAlign = "left"; ctx.fillStyle = COLORS.muted;
  for (const p of ticks) {
    const y = g.y(p);
    if (y < 8 || y > g.plotH - 6 || Math.abs(y - lastY) < 15 || placed.some((t) => Math.abs(y - t.at) < 12)
        || (hair && Math.abs(y - hair.y) < 12)) continue;
    ctx.fillText(fmt(p), g.plotW + 8, y + 1);
  }
  timeLabels(ctx, g, chart, first, last);

  for (const d of chart.drawings || []) {
    if (d.type !== "vline") continue;
    const color = PALETTE[d.color] || PALETTE.steel, x = g.x(d.points[0].x);
    if (x > 0 && x < g.plotW) timeTag(ctx, g, x, timeText(chart, d.points[0].x), { outline: color, color });
  }
  // The last close goes on top of the others.
  for (const t of placed) if (!t.fixed) axisTag(ctx, g, t.at, t.text, t.style);
  for (const t of placed) if (t.fixed) axisTag(ctx, g, t.at, t.text, t.style);
  if (hair) {
    axisTag(ctx, g, hair.y, fmt(hair.price), { fill: COLORS.line2, color: COLORS.bright });
    timeTag(ctx, g, hair.x, timeText(chart, hair.i));
  }
}

// A dashed line down the plot with its label near the bottom; returns where the label went. A label that would run
// into avoid (the other marker's) moves up a row, so "Finished" never prints over "Replay starts".
function marker(ctx, g, at, text, avoid) {
  const x = g.x(at);
  if (x < -40 || x > g.plotW + 2) return null;
  ctx.strokeStyle = rgba(COLORS.muted, 0.55); ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(crisp(x), 0); ctx.lineTo(crisp(x), g.plotH); ctx.stroke(); ctx.setLineDash([]);
  ctx.font = font(11); ctx.textBaseline = "middle"; ctx.textAlign = "left";
  // The label sits right of its line, or left of it when it would run off the plot.
  const w = ctx.measureText(text).width, x0 = x + 5 + w < g.plotW - 4 ? x + 5 : x - 5 - w;
  let y = g.plotH - 12;
  if (avoid && x0 < avoid.x1 + 6 && avoid.x0 < x0 + w + 6 && Math.abs(avoid.y - y) < 14) y -= 16;
  haloText(ctx, text, x0, y, COLORS.muted);
  return { x0, x1: x0 + w, y };
}

function timeText(chart, i) {
  const date = chart.reveal?.dates?.[i];
  return date ? fmtDate(date) : barNumber(chart, i);
}

// Bar numbers every 5, 10, 20, 50 or 100 bars, as zoom allows; month and year labels once the dates are known.
function timeLabels(ctx, g, chart, first, last) {
  ctx.font = font(11); ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = COLORS.muted;
  const y = g.plotH + 14, dates = chart.reveal?.dates;
  if (dates && dates.length) {
    let prevX = -Infinity;
    for (let i = Math.max(1, first); i <= Math.min(last, dates.length - 1); i++) {
      const [yr, mo] = dates[i].split("-"), [pyr, pmo] = dates[i - 1].split("-");
      if (mo === pmo) continue;
      const x = g.x(i), text = yr !== pyr ? yr : MONTHS[Number(mo) - 1];
      if (x < 16 || x > g.plotW - 16 || x - prevX < 44) continue;
      ctx.fillStyle = yr !== pyr ? COLORS.text : COLORS.muted;
      ctx.fillText(text, x, y);
      prevX = x;
    }
    return;
  }
  const every = [5, 10, 20, 50, 100].find((n) => n * g.barW >= 46) || 100;
  const zero = chart.nCtx - 1;
  const from = Math.ceil((Math.max(0, Math.floor(g.left)) - zero) / every) * every;
  for (let n = from; zero + n <= Math.ceil(g.right) + FUTURE_BARS; n += every) {
    const x = g.x(zero + n);
    if (x < 14 || x > g.plotW - 14) continue;
    if (zero + n > chart.nCtx + chart.nReplay - 1) break;
    ctx.fillStyle = n === 0 ? COLORS.text : COLORS.muted;
    ctx.fillText(n < 0 ? MINUS + -n : String(n), x, y);
  }
}

function paintDrawing(ctx, g, d, ui) {
  const color = PALETTE[d.color] || PALETTE.steel, s = shape(d, g);
  const selected = ui.selection?.kind === "drawing" && ui.selection.id === d.id;
  const hot = selected || (ui.hoverId != null && ui.hoverId === d.id) || ui.preview;
  ctx.lineWidth = hot ? 2 : 1.5; ctx.lineCap = "round";
  if (s.rect) {
    const { x0, y0, x1, y1 } = s.rect;
    ctx.fillStyle = rgba(color, hot ? 0.17 : 0.12); ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    ctx.strokeStyle = rgba(color, hot ? 0.95 : 0.7); ctx.lineWidth = hot ? 1.5 : 1;
    ctx.strokeRect(Math.round(x0) + 0.5, Math.round(y0) + 0.5, Math.round(x1 - x0), Math.round(y1 - y0));
  }
  if (s.poly) {
    ctx.beginPath(); s.poly.forEach((q, n) => (n ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y))); ctx.closePath();
    ctx.fillStyle = rgba(color, hot ? 0.12 : 0.08); ctx.fill();
  }
  ctx.strokeStyle = color;
  for (const [a, b] of s.segs || []) strokeLine(ctx, a, b);
  if (s.mid) {
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1; ctx.strokeStyle = rgba(color, 0.8);
    strokeLine(ctx, s.mid[0], s.mid[1]); ctx.setLineDash([]);
  }
  ctx.lineCap = "butt";
  if (d.label && s.anchor) {
    ctx.font = font(11); ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
    const text = d.label.length > 48 ? d.label.slice(0, 47) + "…" : d.label;
    // Labels stay whole inside the plot: they slide left at the right edge (a time level's flips to the other side).
    const w = ctx.measureText(text).width, inside = (x) => clamp(x, 4, Math.max(4, g.plotW - w - 4));
    let { x, y } = s.anchor;
    if (d.type === "zone") {
      const tall = s.rect.y1 - s.rect.y0 > 20;
      x = inside(Math.max(x, 0) + 6); y = tall ? s.rect.y0 + 15 : s.rect.y0 - 5;
    } else if (d.type === "vline") { x = inside(x + 6 + w > g.plotW - 4 ? x - 6 - w : x + 6); y = 16; }
    else { x = inside(x + 4); y -= 7; }
    // With the trade labels, so it can keep clear of them; a preview's label goes straight on.
    if (ui.drawingLabels) ui.drawingLabels.push({ text, x, y, w, color });
    else haloText(ctx, text, x, y, color);
  }
  if (selected && !ui.preview) {
    for (const h of handles(d, g)) {
      const hotHandle = ui.hoverHandle === h.k;
      ctx.beginPath(); ctx.arc(h.x, h.y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = hotHandle ? COLORS.blue : COLORS.bg; ctx.fill();
      ctx.strokeStyle = COLORS.blue; ctx.lineWidth = 1.5; ctx.stroke();
    }
  }
}

// One trade: a working order as dashed lines from the bar it was placed on; a position as reward and risk boxes from
// the fill to the exit (or the current bar), with the entry triangle, the exit cross and the path of the price.
function paintTrade(ctx, g, chart, trade, res, ui) {
  const { nCtx } = chart, newest = newestIndex(chart), long = trade.side !== "short";
  const selected = ui.selection?.kind === "trade" && ui.selection.id === trade.id;
  const placed = nCtx + trade.placedAt - 1, xPlaced = g.x(placed);
  const word = long ? "Long" : "Short";
  const entryPrice = trade.entry ?? sim.refClose(chart, trade.placedAt);
  if (!res.fill) {
    const end = res.missed ? res.missed.i : res.cancelled ? res.cancelled.i
      : res.status === "pending" ? null : sim.scopeEnd(chart);
    const x1 = end == null ? g.plotW : g.x(end);
    const live = res.status === "pending";
    ctx.globalAlpha = live ? 1 : 0.4;
    ctx.lineWidth = selected ? 2 : 1.25; ctx.setLineDash([5, 4]);
    ctx.strokeStyle = COLORS.text; hLine(ctx, g.y(entryPrice), xPlaced, x1);
    ctx.strokeStyle = COLORS.red; hLine(ctx, g.y(res.stopNow), xPlaced, x1);
    ctx.strokeStyle = COLORS.green; hLine(ctx, g.y(res.targetNow), xPlaced, x1);
    ctx.setLineDash([]);
    const kind = res.kind === "market" ? "market"
      : `${res.kind === "limit" ? "limit" : "stop entry"} ${fmt(trade.entry)}`;
    const status = live ? "" : ` · ${res.status}`;
    if (live || selected) {
      const yE = g.y(entryPrice);
      ui.tradeLabels.push({ type: "pill", trade, text: `#${trade.id} ${word} ${kind}${status}`,
                            x: Math.max(xPlaced, 4) + 4, y: yE - 12, line: yE, alpha: live ? 1 : 0.4,
                            opts: { color: live ? COLORS.text : COLORS.muted, limit: g.plotW - 4 } });
    }
    ctx.globalAlpha = 1;
    if (live && (selected || ui.levelTrade === trade.id)) tradeTags(ui, res, trade.entry);
    return;
  }
  const endI = res.exit ? res.exit.i : newest, closed = res.status === "closed";
  const xa = g.x(res.fill.i), xb = Math.max(g.x(endI), xa + Math.max(3, g.barW * 0.6));
  const yE = g.y(res.fill.price);
  // Boxes per stretch of bars with the same levels, so a moved stop shows where it moved.
  let runStart = res.fill.i, prev = sim.levelsAt(trade, runStart, nCtx);
  const flush = (from, to, lv) => {
    const x0 = from === res.fill.i ? xa : g.x(from - 0.5), x1 = to === endI ? xb : g.x(to + 0.5);
    const yt = g.y(lv.target), ys = g.y(lv.stop), a = closed ? 0.09 : 0.15;
    ctx.fillStyle = rgba(COLORS.green, a); ctx.fillRect(x0, Math.min(yE, yt), x1 - x0, Math.abs(yt - yE));
    ctx.fillStyle = rgba(COLORS.red, a); ctx.fillRect(x0, Math.min(yE, ys), x1 - x0, Math.abs(ys - yE));
    ctx.lineWidth = selected ? 1.5 : 1;
    ctx.strokeStyle = rgba(COLORS.green, closed ? 0.45 : 0.8); hLine(ctx, yt, x0, x1);
    ctx.strokeStyle = rgba(COLORS.red, closed ? 0.45 : 0.8); hLine(ctx, ys, x0, x1);
  };
  for (let i = res.fill.i + 1; i <= endI; i++) {
    const lv = sim.levelsAt(trade, i, nCtx);
    if (lv.stop !== prev.stop || lv.target !== prev.target) { flush(runStart, i - 1, prev); runStart = i; prev = lv; }
  }
  flush(runStart, endI, prev);
  ctx.strokeStyle = rgba(COLORS.text, 0.45); ctx.lineWidth = 1; hLine(ctx, yE, xa, xb);
  ctx.strokeStyle = rgba(COLORS.text, closed ? 0.5 : 0.8); ctx.setLineDash([3, 3]);
  strokeLine(ctx, { x: xa, y: yE }, { x: g.x(endI), y: g.y(res.mark) }); ctx.setLineDash([]);
  if (!closed) {
    // A live position's levels run on to the right edge, where they can be dragged.
    ctx.setLineDash([5, 4]); ctx.lineWidth = selected || ui.levelTrade === trade.id ? 2 : 1.25;
    ctx.strokeStyle = COLORS.red; hLine(ctx, g.y(res.stopNow), xb, g.plotW);
    ctx.strokeStyle = COLORS.green; hLine(ctx, g.y(res.targetNow), xb, g.plotW);
    ctx.setLineDash([]);
  }
  triangle(ctx, xa, yE + (long ? 4 : -4), long, COLORS.text);
  if (res.exit) {
    const color = res.exit.reason === "target" ? COLORS.green : res.exit.reason === "stop" ? COLORS.red : COLORS.text;
    cross(ctx, g.x(res.exit.i), g.y(res.exit.price), color);
  }
  const r = sim.fmtR(res.r), tone = res.r > 0 ? COLORS.greenText : res.r < 0 ? COLORS.redText : COLORS.text;
  if (closed && !selected) {
    // A closed trade keeps quiet: its result beside the exit cross (left of it near the axis). Selecting it brings
    // back the full label.
    ctx.font = font(11);
    const text = `#${trade.id} ${r}`, w = ctx.measureText(text).width, xc = g.x(res.exit.i);
    const x = xc + 9 + w <= g.plotW - 4 ? xc + 9 : Math.max(4, xc - 9 - w);
    ui.tradeLabels.push({ type: "halo", trade, text, x, y: clamp(g.y(res.exit.price), 8, g.plotH - 8), w,
                          color: tone });
    return;
  }
  const top = Math.min(yE, g.y(res.targetNow), g.y(res.stopNow));
  const text = `#${trade.id} ${word} ${r}${res.status === "closing" ? " · closing at the next open" : ""}`;
  ui.tradeLabels.push({ type: "pill", trade, text, x: Math.max(xa, 4), y: top - 12, line: top, alpha: 1,
                        opts: { color: tone, bg: COLORS.panel, limit: g.plotW - 4 } });
  if (!closed && (selected || ui.levelTrade === trade.id)) tradeTags(ui, res, null);
}

function tradeTags(ui, res, entry) {
  ui.tags = ui.tags || [];
  ui.tags.push({ price: res.stopNow, style: { outline: COLORS.red, color: COLORS.redText } });
  ui.tags.push({ price: res.targetNow, style: { outline: COLORS.green, color: COLORS.greenText } });
  if (entry != null) ui.tags.push({ price: entry, style: { outline: COLORS.text, color: COLORS.text } });
}

// The order being placed, TradingView style: a reward box over a risk box from the current bar into the empty
// space, with the prices, the moves and the reward to risk.
function paintDraft(ctx, g, chart, draft, ui) {
  const ref = sim.refClose(chart), long = draft.side !== "short", sign = long ? 1 : -1;
  const stage = ui.placing ? ui.placing.stage : 3, hover = ui.placing ? ui.hoverPrice : null;
  const market = stage === 0 ? !!ui.marketHot : draft.entry == null;
  const entry = market ? ref : stage === 0 ? hover : draft.entry;
  if (!isNum(entry)) return;
  const stop = stage === 1 ? hover : draft.stop, target = stage === 2 ? hover : draft.target;
  const { xa, xb } = draftBox(g, chart);
  const yE = g.y(entry);
  // The move to each level for the trade's side: a stop reads as a loss, a target as a gain. A level on the wrong side
  // of the entry (the price moved on, or a stray click) says so instead of showing a gain or loss it can't have.
  const side = (above) => `(must be ${above ? "above" : "below"} the entry)`;
  if (isNum(stop) && stage >= 1) {
    const ys = g.y(stop), good = (entry - stop) * sign > 0;
    ctx.fillStyle = rgba(COLORS.red, good ? 0.16 : 0.07);
    ctx.fillRect(xa, Math.min(yE, ys), xb - xa, Math.abs(ys - yE));
    ctx.strokeStyle = COLORS.red; ctx.lineWidth = 1.5; hLine(ctx, ys, xa, xb);
    const pct = sign * (stop - entry) / entry * 100;
    boxLabel(ctx, `Stop ${fmt(stop)} ${good ? `(${sim.fmtPct(pct)})` : side(!long)}`, (xa + xb) / 2, ys, ys > yE,
             COLORS.redText, ui.legend, g.plotW);
    ui.tags = [...(ui.tags || []), { price: stop, style: { outline: COLORS.red, color: COLORS.redText } }];
  }
  if (isNum(target) && stage >= 2) {
    const yt = g.y(target), good = (target - entry) * sign > 0;
    ctx.fillStyle = rgba(COLORS.green, good ? 0.16 : 0.07);
    ctx.fillRect(xa, Math.min(yE, yt), xb - xa, Math.abs(yt - yE));
    ctx.strokeStyle = COLORS.green; ctx.lineWidth = 1.5; hLine(ctx, yt, xa, xb);
    const pct = sign * (target - entry) / entry * 100;
    boxLabel(ctx, `Target ${fmt(target)} ${good ? `(${sim.fmtPct(pct)})` : side(long)}`, (xa + xb) / 2, yt, yt > yE,
             COLORS.greenText, ui.legend, g.plotW);
    ui.tags = [...(ui.tags || []), { price: target, style: { outline: COLORS.green, color: COLORS.greenText } }];
  }
  ctx.strokeStyle = COLORS.text; ctx.lineWidth = 1.5;
  if (stage === 0) ctx.setLineDash([5, 4]);
  hLine(ctx, yE, stage === 0 ? 0 : xa, xb); ctx.setLineDash([]);
  const kind = market ? "market" : sim.kindFor(draft.side, entry, ref);
  let text = `${long ? "Long" : "Short"} ${kind === "market" ? "market" : kind === "limit" ? "limit" : "stop entry"}`;
  if (kind !== "market") text += ` ${fmt(entry)}`;
  if (isNum(stop) && isNum(target) && stage >= 2 && Math.abs(entry - stop) > 0) {
    text += ` · reward to risk ${(Math.abs(target - entry) / Math.abs(entry - stop)).toFixed(2)}`;
  }
  pill(ctx, text, xa, yE + (long ? 12 : -12), { color: COLORS.bright, bg: COLORS.raised, limit: g.plotW - 4,
                                                 legend: ui.legend });
  ui.tags = [...(ui.tags || []), { price: entry, style: { outline: COLORS.text, color: COLORS.text } }];
}

// Where the order preview sits: just right of the newest bar, wide enough for its labels; near the plot's edge it
// slides left over the last bars.
function draftBox(g, chart) {
  const width = Math.max(12 * g.barW, 180);
  let xa = g.x(newestIndex(chart)) + Math.max(2, g.barW * 0.6), xb = xa + width;
  if (xb > g.plotW - 2) { xb = g.plotW - 2; xa = Math.max(2, xb - width); }
  return { xa, xb };
}

// The order preview's label for a level, centred over (or, below, under) its line, and clear of the legend; near the
// plot's right edge (limit) it slides left rather than run off it.
function boxLabel(ctx, text, x, y, below, color, legend, limit = Infinity) {
  ctx.font = font(11); ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const w = ctx.measureText(text).width;
  x = Math.max(w / 2 + 4, Math.min(x, limit - w / 2 - 4));
  haloText(ctx, text, x, Math.max(y + (below ? 11 : -10), underLegend(legend, x - w / 2, 14)), color);
}

// ---------------------------------------------------------------- the live chart

export class ChartView {
  constructor(host, handlers = {}) {
    this.host = host; this.on = handlers;
    this.canvas = document.createElement("canvas");
    this.canvas.className = "tt-canvas";
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label", "Price chart. Use the toolbar or keyboard shortcuts to draw and trade.");
    this.legend = document.createElement("div");
    this.legend.className = "tt-legend";
    this.hintEl = document.createElement("div");
    this.hintEl.className = "tt-hint";
    this.hintEl.setAttribute("aria-live", "polite");
    // Latest: shown once the newest bar is panned out of view, which steps then leave where they are (see update).
    this.latestEl = document.createElement("button");
    this.latestEl.type = "button";
    this.latestEl.className = "tt-latest";
    this.latestEl.hidden = true;
    this.latestEl.setAttribute("aria-label", "Back to the newest bar");
    this.latestEl.innerHTML = '<span>Latest</span><svg viewBox="0 0 18 18" aria-hidden="true">'
      + '<path d="M7 4.5L11.5 9 7 13.5"/></svg>';
    host.append(this.canvas, this.legend, this.hintEl, this.latestEl);
    this.labelHits = [];
    this.ctx = this.canvas.getContext("2d");
    this.model = null; this.view = null; this.tool = "cursor";
    this.pending = null; this.placing = null; this.gesture = null; this.pointers = new Map();
    this.hover = null; this.hit = null; this.levelPreview = null; this.frozen = null; this.flash = null;
    this.size = { w: 0, h: 0, dpr: 1 }; this.g = null; this.frame = 0; this.legendKey = "";
    // Mouse or finger, for the hints' wording and the hit tolerance: a phone's first hint already says "tap".
    this.lastPointer = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches ? "touch" : "mouse";
    this.listen();
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this.resize();
    if (document.fonts?.ready) document.fonts.ready.then(() => this.render());
  }

  listen() {
    const c = this.canvas, opts = { passive: false };
    this.handlers = [
      [c, "pointerdown", (e) => this.down(e)], [c, "pointermove", (e) => this.move(e)],
      [c, "pointerup", (e) => this.up(e)], [c, "pointercancel", (e) => this.up(e, true)],
      [c, "pointerleave", (e) => this.leave(e)], [c, "wheel", (e) => this.wheel(e), opts],
      [c, "dblclick", (e) => this.dblclick(e)], [c, "contextmenu", (e) => { e.preventDefault(); this.cancel(); }],
      [this.latestEl, "click", () => this.toLatest()],
      [window, "keydown", (e) => this.modifier(e)], [window, "keyup", (e) => this.modifier(e)],
      // Any press on the page (a tool picked in the toolbar too) says which pointer is in use.
      [window, "pointerdown", (e) => { if (e.pointerType) this.lastPointer = e.pointerType; }, { capture: true }],
    ];
    for (const [target, type, fn, o] of this.handlers) target.addEventListener(type, fn, o);
  }

  destroy() {
    for (const [target, type, fn, o] of this.handlers) target.removeEventListener(type, fn, o);
    this.ro.disconnect();
    cancelAnimationFrame(this.frame);
    clearTimeout(this.flashTimer); clearTimeout(this.viewTimer);
  }

  // ------------------------------------------------ data in

  update(model) {
    const previous = this.model?.chart, chart = model.chart;
    if (chart !== previous || !this.view) {
      // A chart whose bars have not arrived yet gets its view when they do.
      this.view = chart.bars.length ? clampView(chart.view || defaultView(chart, this.size.w), chart) : null;
      // A chart still being replayed opens on its newest bar, even when the saved view was left behind it.
      const now = newestIndex(chart);
      const off = this.view && (now > this.view.right - 0.5 || now < this.view.right - this.view.span + 0.5);
      if (off && !chart.finished) {
        this.view = clampView({ ...this.view, right: now + MARGIN_BARS }, chart);
      }
      if (chart !== previous) { this.cancel(); this.hover = null; this.levelPreview = null; this.lastRange = null; }
    } else if (this.model) {
      // A step forward keeps the newest bar in view, unless the visitor has panned away from it.
      const before = this.lastNewest, now = newestIndex(chart);
      if (now > before && !chart.finished && before <= this.view.right && before >= this.view.right - this.view.span) {
        this.view = clampView({ ...this.view, right: this.view.right + (now - before) }, chart);
        this.dropTouchHover();
        this.saveView();
      }
    }
    this.model = model;
    this.lastNewest = newestIndex(chart);
    if (model.locked && this.tool !== "cursor") this.setTool("cursor");
    this.render();
  }

  // preset gives a drawing tool its colour and label up front (the demand and supply zones).
  setTool(name, preset = null) {
    if (this.model?.locked && name !== "cursor") name = "cursor";
    this.tool = name;
    this.preset = DRAW_TOOLS.has(name) ? preset : null;
    this.pending = null;
    this.placing = (name === "long" || name === "short") ? { side: name, stage: 0, entry: null, stop: null } : null;
    this.frozen = null;
    // A new tool's instructions replace any tip still showing (the label tip after a drawing, say).
    this.clearFlash();
    this.showHint();
    this.render();
  }

  // Esc: drops a drawing or order half made. True when there was one.
  cancel() {
    const had = !!(this.pending || (this.placing && this.placing.stage > 0) || this.gesture);
    if (this.gesture?.before && this.gesture.drawing) Object.assign(this.gesture.drawing, copy(this.gesture.before));
    this.pending = null; this.gesture = null; this.levelPreview = null; this.frozen = null;
    if (this.placing) this.placing = { side: this.placing.side, stage: 0, entry: null, stop: null };
    this.showHint();
    this.render();
    return had;
  }

  busy() { return !!(this.pending || (this.placing && this.placing.stage > 0) || this.gesture); }

  // The replay moved on: a drawing or order begun before the step was asked for was made against the bars before it,
  // so it goes and the tool starts over. One begun while the step was on its way stays; a drag in progress carries on.
  dropHalfMade(sentAt = Infinity) {
    const half = !!(this.pending || (this.placing && this.placing.stage > 0));
    if (!half || !(this.halfSince < sentAt)) return false;
    this.pending = null; this.frozen = null;
    if (this.placing) this.placing = { side: this.placing.side, stage: 0, entry: null, stop: null };
    if (this.gesture?.type === "tool" || this.gesture?.type === "order") this.gesture = null;
    this.clearFlash();
    this.showHint();
    this.render();
    return true;
  }

  // A drawing's first point or an order's entry is down: noted, and the page told (it pauses Play).
  begun() {
    this.halfSince = performance.now();
    this.on.started?.();
  }

  fit() {
    if (!this.model) return;
    this.view = defaultView(this.model.chart, this.size.w);
    this.dropTouchHover();
    this.saveView();
    this.render();
  }

  // The newest bar back in view at the zoom the visitor chose, the prices scaled to fit again.
  toLatest() {
    if (!this.model) return;
    const now = newestIndex(this.model.chart);
    this.view = clampView({ right: now + MARGIN_BARS, span: this.view.span }, this.model.chart);
    this.dropTouchHover();
    this.saveView();
    this.render();
  }

  // Every bar of the window in view, as a finished chart shows itself.
  showWhole() {
    if (!this.model) return;
    const last = this.model.chart.bars.length - 1;
    this.view = clampView({ right: last + 3, span: last + 7 }, this.model.chart);
    this.dropTouchHover();
    this.saveView();
    this.render();
  }

  // A finger's last point means nothing once the chart moves under it (no hover follows on a touch screen): the
  // crosshair goes and the legend shows the newest bar.
  dropTouchHover() {
    if (this.hover?.touch && !this.pointers.size) { this.hover = null; this.hit = null; }
  }

  resize() {
    const r = this.host.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.max(50, Math.round(r.width)), h = Math.max(50, Math.round(r.height));
    if (w === this.size.w && h === this.size.h && dpr === this.size.dpr) return;
    this.size = { w, h, dpr };
    this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
    this.canvas.style.width = w + "px"; this.canvas.style.height = h + "px";
    this.render();
  }

  saveView() {
    clearTimeout(this.viewTimer);
    this.viewTimer = setTimeout(() => { this.viewTimer = 0; this.on.view?.({ ...this.view }); }, 250);
  }

  // Hand over a view change still waiting on the debounce, before the page saves or goes away.
  flushView() {
    if (!this.viewTimer) return;
    clearTimeout(this.viewTimer);
    this.viewTimer = 0;
    if (this.view) this.on.view?.({ ...this.view });
  }

  // ------------------------------------------------ painting

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.paintNow(); });
  }

  range() {
    if (this.frozen) return this.frozen;
    if (isNum(this.view.lo)) return { lo: this.view.lo, hi: this.view.hi };
    const room = this.legendRoom() / Math.max(1, this.size.h - AXIS_H);
    const r = autoRange(this.model.chart, this.view, this.model, liveLevels(this.model), room);
    if (r) this.lastRange = r;
    return this.lastRange || { lo: 90, hi: 110 };
  }

  // On a narrow chart the legend runs to two or three lines over the top of the plot: the bars start below it. Its
  // tallest height so far for this width and set of averages, so the range doesn't jump as the legend changes.
  legendRoom() {
    if (this.size.w > 600) return 0;
    const key = `${this.size.w}|${MA_LINES.filter((ma) => this.model.settings?.ma?.[ma.key]).length}`;
    if (this.roomKey !== key) { this.roomKey = key; this.roomMax = 0; }
    this.roomMax = Math.max(this.roomMax, this.legend.offsetHeight || 0);
    return this.roomMax ? this.roomMax + 10 : 0;
  }

  paintNow() {
    const { w, h, dpr } = this.size, ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!this.model || !this.view) {
      ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
      this.g = null; this.legend.replaceChildren(); this.legendKey = "";
      return;
    }
    const g = geometry(ctx, w, h, this.view, this.range());
    this.g = g;
    const ui = this.uiState(g);
    paint(ctx, g, this.model, ui);
    this.labelHits = ui.labelHits;
    this.paintLegend();
    // Latest sits at the plot's foot by the price axis while a chart still being replayed has its newest bar off the
    // plot.
    const chart = this.model.chart, now = newestIndex(chart);
    const away = !chart.finished && (now > this.view.right - 0.5 || now < this.view.right - this.view.span + 0.5);
    if (this.latestEl.hidden === away) this.latestEl.hidden = !away;
    if (away) Object.assign(this.latestEl.style, { right: `${w - g.plotW + 8}px`, bottom: `${h - g.plotH + 8}px` });
    // A narrow legend that just grew: the bars make room for it on the next frame.
    if (w <= 600 && !this.frozen && !isNum(this.view.lo) && this.legend.offsetHeight > (this.roomMax || 0)) {
      this.render();
    }
  }

  uiState(g) {
    const hover = this.hover, chart = this.model.chart;
    const ui = { selection: this.model.selection, pending: this.pending, levelPreview: this.levelPreview,
                 levelTrade: this.levelPreview?.trade.id ?? (this.hit?.kind === "level" ? this.hit.trade.id : null) };
    // Where the legend ends, so labels stay below it (on a narrow chart it wraps across the whole top).
    const lg = this.legend;
    ui.legend = { bottom: lg.offsetHeight ? lg.offsetTop + lg.offsetHeight : 0, wide: this.size.w <= 600 };
    if (this.hit && (this.hit.kind === "drawing" || this.hit.kind === "handle")) ui.hoverId = this.hit.drawing.id;
    if (this.hit?.kind === "handle") ui.hoverHandle = this.hit.k;
    if (this.placing) {
      ui.placing = this.placing;
      // A finger shows the next level only while it is on the glass; after a tap the preview waits for the next one,
      // instead of drawing that level where the last tap was.
      const live = !hover?.touch || (this.pointers.size > 0 && !this.gesture?.moved);
      if (hover && live) {
        const q = this.snap(hover);
        ui.hoverPrice = q.p;
        ui.marketHot = this.placing.stage === 0 && this.nearMarket(hover);
      }
    }
    if (hover && hover.x <= g.plotW && hover.y <= g.plotH && !(this.gesture?.type === "pan" && hover.touch)) {
      const q = this.snap(hover);
      const magnetic = (DRAW_TOOLS.has(this.tool) || this.gesture?.type === "handle") && this.magnet();
      ui.crosshair = { x: g.x(q.x), y: magnetic ? g.y(q.p) : hover.y, i: q.x, price: magnetic ? q.p : g.p(hover.y) };
    }
    if (chart.finished || this.model.locked) ui.marketHot = false;
    return ui;
  }

  paintLegend() {
    const chart = this.model.chart, settings = this.model.settings || {}, newest = newestIndex(chart);
    let i = this.hover && this.g ? Math.round(this.g.i(this.hover.x)) : newest;
    if (!(i >= 0 && i <= newest)) i = newest;
    const mas = MA_LINES.filter((ma) => settings.ma?.[ma.key]);
    const key = `${i}|${chart.bars.length}|${mas.map((m) => m.key).join()}|${chart.reveal ? 1 : 0}|${chart.nCtx}`;
    if (key === this.legendKey && this.legendChart === chart) return;
    this.legendKey = key; this.legendChart = chart;
    const bar = chart.bars[i], el = this.legend;
    el.replaceChildren();
    if (!bar) return;
    const prev = chart.bars[i - 1], up = bar[C] >= bar[O], tone = up ? "tt-up" : "tt-down";
    const span = (cls, text) => {
      const s = document.createElement("span");
      if (cls) s.className = cls;
      s.textContent = text;
      return s;
    };
    // Each key travels with its value, so a narrow legend wraps between pairs, never inside one.
    const pair = (cls, ...parts) => {
      const s = span(cls ? `tt-lg-item ${cls}` : "tt-lg-item", "");
      s.append(...parts);
      return s;
    };
    const line1 = document.createElement("div");
    const date = chart.reveal?.dates?.[i];
    line1.append(span("tt-lg-item tt-lg-bar", barNumber(chart, i) + (date ? ` · ${fmtDate(date)}` : "")));
    for (const [name, col] of [["O", O], ["H", H], ["L", L], ["C", C]]) {
      line1.append(pair("", span("tt-lg-key", name), span(tone, fmt(bar[col]))));
    }
    if (prev) {
      const change = (bar[C] / prev[C] - 1) * 100;
      line1.append(span(`tt-lg-item tt-lg-gap ${change >= 0 ? "tt-up" : "tt-down"}`, sim.fmtPct(change)));
    }
    if (isNum(bar[V])) {
      line1.append(pair("tt-lg-gap", span("tt-lg-key", "Vol"), span("", `${bar[V].toFixed(1)}× avg`)));
    }
    el.append(line1);
    if (mas.length) {
      const line2 = document.createElement("div");
      for (const ma of mas) {
        const s = span("tt-lg-item tt-lg-ma", `${ma.name} ${isNum(bar[ma.col]) ? fmt(bar[ma.col]) : "–"}`);
        s.style.color = ma.color;
        line2.append(s);
      }
      el.append(line2);
    }
  }

  clearFlash() {
    clearTimeout(this.flashTimer);
    this.flash = null;
  }

  // The hint over the chart: what the active tool wants next, or a refusal for a moment.
  showHint(text, tone) {
    if (text) {
      clearTimeout(this.flashTimer);
      this.flash = { text, tone };
      this.flashTimer = setTimeout(() => { this.flash = null; this.showHint(); }, 2600);
    }
    const message = this.flash || { text: this.toolHint(), tone: "" };
    this.hintEl.textContent = message.text || "";
    this.hintEl.className = "tt-hint" + (message.text ? " is-on" : "") + (message.tone ? ` tone-${message.tone}` : "");
  }

  toolHint() {
    const touch = this.lastPointer === "touch", esc = touch ? "" : " Esc cancels.";
    const click = touch ? "Tap" : "Click";
    if (this.placing) {
      const long = this.placing.side === "long";
      return [
        `${long ? "Long" : "Short"}: ${click.toLowerCase()} your entry. ${click} the last price to `
          + `${long ? "buy" : "sell"} at the next open.${esc}`,
        `${click} your stop, ${long ? "below" : "above"} the entry.${esc}`,
        `${click} your target, ${long ? "above" : "below"} the entry. Drag the price scale for more room.${esc}`,
      ][this.placing.stage] || "";
    }
    const stage = this.pending ? (this.pending.type === "channel" && this.pending.stage === 2 ? 2 : 1) : 0;
    const drag = touch ? "" : ", or press and drag";
    switch (this.tool) {
      case "trend":
        return stage ? `${click} the second point.${esc}`
          : `Trendline: ${click.toLowerCase()} two points${drag}.${esc}`;
      case "ray":
        return stage ? `${click} a point the ray passes through.${esc}`
          : `Ray: ${click.toLowerCase()} where it starts${drag}.${esc}`;
      case "channel":
        return [`Channel: ${click.toLowerCase()} two points for the base line.${esc}`,
                `${click} the second point of the base line.${esc}`,
                `${click} to set the channel’s width.${esc}`][stage];
      case "hline": return `Price level: ${click.toLowerCase()} a price.${esc}`;
      case "vline": return `Time level: ${click.toLowerCase()} a bar.${esc}`;
      case "zone":
        return stage ? `${click} the opposite corner.${esc}`
          : `${this.preset?.name || "Zone"}: ${click.toLowerCase()} two opposite corners${drag}.${esc}`;
      default: return "";
    }
  }

  // ------------------------------------------------ pointer input

  pos(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, touch: e.pointerType === "touch" };
  }

  tolerance() { return this.lastPointer === "mouse" ? 6 : 12; }

  magnet() { return !!this.model?.settings?.magnet !== !!this.ctrlHeld; }

  modifier(e) {
    const held = e.ctrlKey || e.metaKey;
    if (held !== this.ctrlHeld) { this.ctrlHeld = held; if (this.hover) this.render(); }
  }

  maxX() { return newestIndex(this.model.chart) + FUTURE_BARS; }

  // A point on the chart: the nearest bar centre and the price, pulled to the bar's open, high, low or close when the
  // magnet is on and one is within reach.
  snap(pt, magnet = this.magnet()) {
    const g = this.g, chart = this.model.chart;
    const x = clamp(Math.round(g.i(pt.x)), 0, this.maxX());
    let p = g.p(pt.y);
    const bar = chart.bars[x];
    if (magnet && bar && x <= newestIndex(chart)) {
      let best = MAGNET_PX + 1;
      for (const v of [bar[O], bar[H], bar[L], bar[C]]) {
        const d = Math.abs(g.y(v) - pt.y);
        if (d < best) { best = d; p = v; }
      }
    }
    return { x, p: round4(p) };
  }

  nearMarket(pt) {
    const g = this.g, bar = this.model.chart.bars[newestIndex(this.model.chart)];
    if (!bar) return false;
    const reach = Math.max(MARKET_SHARE * g.plotH, pt.touch ? 10 : 0);
    const dy = Math.abs(pt.y - g.y(bar[C]));
    return dy <= reach || (pt.x > g.plotW && dy <= 10);
  }

  region(pt) {
    const g = this.g;
    if (!g) return "none";
    if (pt.x > g.plotW && pt.y <= g.plotH) return "price";
    if (pt.y > g.plotH) return "time";
    return "plot";
  }

  // What is under the pointer in Cursor mode, most specific first.
  hitTest(pt) {
    const g = this.g, m = this.model, tol = this.tolerance();
    if (!g || !m) return null;
    const sel = m.selection;
    const drawings = m.chart.drawings || [];
    if (sel?.kind === "drawing" && !m.locked) {
      const d = drawings.find((x) => x.id === sel.id);
      for (const h of d ? handles(d, g) : []) {
        if (Math.hypot(h.x - pt.x, h.y - pt.y) <= tol + 2) return { kind: "handle", drawing: d, k: h.k };
      }
    }
    if (m.draft?.done && !m.locked) {
      const ref = sim.refClose(m.chart), xa = draftBox(g, m.chart).xa - 4;
      for (const which of ["stop", "target", "entry"]) {
        const price = which === "entry" ? m.draft.entry : m.draft[which];
        if (which === "entry" && price == null) continue;
        if (isNum(price) && pt.x >= xa && Math.abs(g.y(price) - pt.y) <= tol) return { kind: "draft", which, ref };
      }
    }
    // The live stop or target line nearest the pointer, within reach; on a tie the newest trade's.
    let best = null, bestDy = Infinity;
    if (!m.locked) {
      const trades = m.chart.trades || [], reach = Math.max(5, tol - 2);
      for (let n = trades.length - 1; n >= 0; n--) {
        const res = m.results?.[n], trade = trades[n];
        if (!res || (res.status !== "pending" && res.status !== "open")) continue;
        const x0 = res.fill ? g.x(res.fill.i) : g.x(m.chart.nCtx + trade.placedAt - 1);
        if (pt.x < x0 - tol) continue;
        for (const which of ["stop", "target"]) {
          const price = which === "stop" ? res.stopNow : res.targetNow, dy = Math.abs(g.y(price) - pt.y);
          if (dy <= reach && dy < bestDy) { best = { kind: "level", trade, which, price }; bestDy = dy; }
        }
      }
    }
    // A trade's label, as last painted, picks its trade: it is drawn over the lines, so it wins over a line under it,
    // unless the pointer is right on that line (which stays draggable there).
    const label = this.labelHits.find((b) => pt.x >= b.x && pt.x <= b.x + b.w && pt.y >= b.y && pt.y <= b.y + b.h);
    if (label && !(best && bestDy <= 2)) return { kind: "trade", trade: label.trade };
    if (best) return best;
    const drawingAt = (pass) => {
      for (let n = drawings.length - 1; n >= 0; n--) {
        if (hitDrawing(drawings[n], g, pt.x, pt.y, tol, pass)) return { kind: "drawing", drawing: drawings[n] };
      }
      return null;
    };
    const edge = drawingAt("edge");
    if (edge) return edge;
    const trades = m.chart.trades || [];
    for (let n = trades.length - 1; n >= 0; n--) {
      const res = m.results?.[n];
      if (!res?.fill) continue;
      const endI = res.exit ? res.exit.i : newestIndex(m.chart);
      const x0 = g.x(res.fill.i) - 3, x1 = Math.max(g.x(endI), x0 + 6) + 3;
      const ys = [res.fill.price, res.stopNow, res.targetNow].map(g.y);
      const inside = pt.x >= x0 && pt.x <= x1 && pt.y >= Math.min(...ys) && pt.y <= Math.max(...ys);
      if (inside) return { kind: "trade", trade: trades[n] };
    }
    return drawingAt("fill");
  }

  cursorFor(pt) {
    if (this.gesture?.type === "pan" && this.gesture.moved) return "grabbing";
    const region = this.region(pt);
    if (region === "price") return "ns-resize";
    if (region === "time") return "ew-resize";
    if (this.tool !== "cursor") return "crosshair";
    const hit = this.hit;
    if (!hit) return "crosshair";
    if (hit.kind === "level" || hit.kind === "draft") return "ns-resize";
    if (hit.kind === "handle") return "move";
    return "pointer";
  }

  freeze() { if (!this.frozen && this.g) this.frozen = { lo: this.g.lo, hi: this.g.hi }; }

  down(e) {
    if (!this.model || !this.g) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    this.lastPointer = e.pointerType || "mouse";
    const pt = this.pos(e);
    try { this.canvas.setPointerCapture(e.pointerId); } catch { /* the pointer may already be gone */ }
    this.pointers.set(e.pointerId, pt);
    if (this.pointers.size === 2) {
      const [a, b] = [...this.pointers.values()];
      if (this.gesture?.drawing && this.gesture.before) Object.assign(this.gesture.drawing, copy(this.gesture.before));
      // The first finger of a pinch is not a drawing's first point, nor the start of a drag; the price range stays
      // held only for a drawing or order already under way.
      if (this.pending?.fresh) { this.pending = null; this.showHint(); }
      if (!this.pending && !(this.placing && this.placing.stage > 0)) this.frozen = null;
      this.levelPreview = null;
      this.gesture = { type: "pinch", dist: Math.max(20, Math.abs(a.x - b.x)), mid: (a.x + b.x) / 2,
                       span: this.view.span, index: this.g.i((a.x + b.x) / 2) };
      return;
    }
    if (this.pointers.size > 2) return;
    this.hover = pt;
    const region = this.region(pt), g = this.g, locked = this.model.locked;
    const base = { start: pt, startY: e.clientY, moved: false, right: this.view.right, lo: g.lo, hi: g.hi,
                   span: this.view.span };
    if (region === "price") {
      if (this.placing && this.nearMarket(pt)) { this.gesture = { ...base, type: "order" }; return; }
      this.gesture = { ...base, type: "price" }; return;
    }
    if (region === "time") { this.gesture = { ...base, type: "time" }; return; }
    if (DRAW_TOOLS.has(this.tool) && !locked) {
      const q = this.snap(pt);
      if (!this.pending && this.tool !== "hline" && this.tool !== "vline") {
        this.pending = { type: this.tool, points: [q, { ...q }], stage: 1, fresh: true,
                         color: this.preset?.color || "steel", label: this.preset?.label || "",
                         extend: this.tool === "zone", id: -1 };
        this.freeze();
        this.begun();
        this.clearFlash();
        this.showHint();
      }
      this.gesture = { ...base, type: "tool" };
      this.render();
      return;
    }
    if (this.placing && !locked) { this.gesture = { ...base, type: "order" }; return; }
    let hit = this.hitTest(pt);
    // A finger has to select a drawing or trade before it can drag it, so a swipe across one still pans.
    const sel = this.model.selection;
    const chosen = (kind, id) => sel?.kind === kind && sel.id === id;
    if (pt.touch && hit?.kind === "level" && !chosen("trade", hit.trade.id)) hit = { kind: "trade", trade: hit.trade };
    if (pt.touch && hit?.kind === "drawing" && !chosen("drawing", hit.drawing.id)) {
      this.on.select?.({ kind: "drawing", id: hit.drawing.id });
      this.gesture = { ...base, type: "pan", hit };
      return;
    }
    if (hit?.kind === "handle") {
      this.freeze();
      this.gesture = { ...base, type: "handle", drawing: hit.drawing, k: hit.k, before: copy(hit.drawing) };
      return;
    }
    if (hit?.kind === "draft") { this.freeze(); this.gesture = { ...base, type: "draft", which: hit.which }; return; }
    if (hit?.kind === "level") {
      this.freeze();
      this.gesture = { ...base, type: "level", trade: hit.trade, which: hit.which };
      return;
    }
    if (hit?.kind === "drawing") {
      this.on.select?.({ kind: "drawing", id: hit.drawing.id });
      if (!locked) {
        this.freeze();
        this.gesture = { ...base, type: "body", drawing: hit.drawing, before: copy(hit.drawing),
                         from: this.snap(pt, false) };
      } else this.gesture = { ...base, type: "pan" };
      return;
    }
    if (hit?.kind === "trade") this.on.select?.({ kind: "trade", id: hit.trade.id });
    this.gesture = { ...base, type: "pan", hit };
  }

  move(e) {
    if (!this.model || !this.g) return;
    const pt = this.pos(e), g = this.g;
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, pt);
    if (e.pointerType !== "touch" || this.pointers.size) this.hover = pt;
    const gs = this.gesture;
    if (gs && gs.start && Math.hypot(pt.x - gs.start.x, pt.y - gs.start.y) > (pt.touch ? 8 : 4)) gs.moved = true;
    if (gs?.type === "pinch" && this.pointers.size >= 2) {
      const [a, b] = [...this.pointers.values()];
      const dist = Math.max(20, Math.abs(a.x - b.x)), mid = (a.x + b.x) / 2;
      const span = clamp(gs.span * gs.dist / dist, 16, 420), barW = g.plotW / span;
      this.view = clampView({ ...this.view, span, right: gs.index + (g.plotW - mid) / barW }, this.model.chart);
      this.saveView();
    } else if (gs?.type === "price") {
      const factor = Math.exp((pt.y - gs.start.y) * 0.005);
      const mid = (gs.lo + gs.hi) / 2, half = (gs.hi - gs.lo) / 2 * factor;
      // A deliberate scale replaces the range held while an order or drawing is half made, so a stop or target
      // beyond the visible prices can be reached.
      this.frozen = null;
      this.view = { ...this.view, lo: mid - half, hi: mid + half };
      this.saveView();
    } else if (gs?.type === "time") {
      const span = clamp(gs.span * Math.exp((gs.start.x - pt.x) * 0.004), 16, 420);
      this.view = clampView({ ...this.view, span }, this.model.chart);
      this.saveView();
    } else if (gs?.type === "pan" && gs.moved && pt.touch && !isNum(this.view.lo)
               && (gs.scroll ??= Math.abs(pt.y - gs.start.y) > Math.abs(pt.x - gs.start.x))) {
      // A finger that sets off up or down over prices scaled to fit (where a drag has nothing to move vertically)
      // scrolls the page, as it would anywhere else on it; one that sets off sideways pans.
      window.scrollBy(0, (gs.clientY ?? gs.startY) - e.clientY);
      gs.clientY = e.clientY;
    } else if (gs?.type === "pan" || (gs?.moved && (gs.type === "order" || (gs.type === "tool" && !this.pending)))) {
      // A drag with a one-click tool pans, as it does in Cursor mode.
      if (gs.moved) {
        const right = gs.right - (pt.x - gs.start.x) / g.barW;
        const next = { ...this.view, right };
        if (isNum(this.view.lo)) {
          const dp = (pt.y - gs.start.y) / g.plotH * (gs.hi - gs.lo);
          next.lo = gs.lo + dp; next.hi = gs.hi + dp;
        }
        this.view = clampView(next, this.model.chart);
        this.saveView();
      }
    } else if (gs?.type === "handle") {
      moveHandle(gs.drawing, gs.k, this.snap(pt), gs.before);
    } else if (gs?.type === "body") {
      const q = this.snap(pt, false), d = gs.drawing;
      if (d.type === "hline" || d.type === "vline") moveHandle(d, 0, this.snap(pt), gs.before);
      else translate(d, gs.before, q.x - gs.from.x, q.p - gs.from.p, this.maxX());
    } else if (gs?.type === "level") {
      this.levelPreview = { trade: gs.trade, which: gs.which, price: this.snap(pt).p };
    } else if (gs?.type === "draft") {
      const draft = { ...this.model.draft, [gs.which]: this.snap(pt).p };
      this.on.draft?.(draft);
    } else if ((gs?.type === "tool" || !gs) && this.pending) {
      const q = this.snap(pt), p = this.pending;
      if (p.type === "channel" && p.stage === 2) p.points[2] = q;
      else if (!gs || gs.moved || !p.fresh) p.points[1] = q;
    }
    if (!gs || gs.type === "pan" && !gs.moved) this.hit = this.tool === "cursor" ? this.hitTest(pt) : null;
    this.canvas.style.cursor = this.cursorFor(pt);
    this.render();
  }

  up(e, cancelled = false) {
    const pt = this.pos(e), gs = this.gesture;
    this.pointers.delete(e.pointerId);
    if (gs?.type === "pinch") { if (this.pointers.size === 0) this.gesture = null; return; }
    if (!gs || !this.model) return;
    this.gesture = null;
    if (cancelled) { this.cancel(); return; }
    if (gs.type === "tool") this.toolUp(pt, gs);
    else if (gs.type === "order") { if (!gs.moved) this.orderClick(pt); }
    else if (gs.type === "handle" || gs.type === "body") {
      this.frozen = null;
      if (JSON.stringify(gs.drawing.points) !== JSON.stringify(gs.before.points)) this.on.edit?.(gs.drawing, gs.before);
    } else if (gs.type === "level") {
      this.frozen = null;
      const preview = this.levelPreview;
      this.levelPreview = null;
      if (preview && gs.moved) {
        const res = this.model.results?.[(this.model.chart.trades || []).indexOf(gs.trade)];
        const levels = { stop: res?.stopNow ?? gs.trade.stop, target: res?.targetNow ?? gs.trade.target };
        levels[gs.which] = preview.price;
        const message = this.on.level?.(gs.trade, levels);
        if (message) this.showHint(message, "error");
      }
    } else if (gs.type === "draft") this.frozen = null;
    else if (gs.type === "pan" && !gs.moved && !gs.hit) this.on.select?.(null);
    // A finger leaves no crosshair behind: nothing follows it once it is off the glass, so the legend goes back to
    // the newest bar.
    if (pt.touch) { this.hover = null; this.hit = null; }
    this.render();
  }

  leave(e) {
    if (e.pointerType === "touch") return;
    if (!this.gesture) { this.hover = null; this.hit = null; this.render(); }
  }

  toolUp(pt, gs) {
    const q = this.snap(pt), tool = this.tool;
    if (tool === "hline" || tool === "vline") {
      if (gs.moved) return;
      this.finishDrawing({ type: tool, points: [q], color: "steel", label: "", extend: false });
      return;
    }
    const p = this.pending;
    if (!p) return;
    if (p.fresh) {
      p.fresh = false;
      if (!gs.moved) { this.showHint(); return; }          // a click: wait for the second point
      p.points[1] = q;                                      // press, drag, release
    } else if (p.type === "channel" && p.stage === 2) {
      p.points[2] = q;
      this.finishDrawing(p);
      return;
    } else p.points[1] = q;
    if (p.points[0].x === p.points[1].x && Math.abs(p.points[0].p - p.points[1].p) < 1e-9) return;
    if (p.type === "channel") {
      p.stage = 2;
      p.points[2] = { ...q };
      this.clearFlash();
      this.showHint();
      return;
    }
    this.finishDrawing(p);
  }

  finishDrawing(p) {
    const drawing = { type: p.type, points: p.points.map((q) => ({ x: q.x, p: round4(q.p) })),
                      color: p.color || "steel", label: p.label || "", extend: p.type === "zone" };
    if (drawing.type === "zone" && Math.abs(drawing.points[0].p - drawing.points[1].p) < 1e-9) {
      this.pending = null;
      this.showHint("A zone needs two different prices.", "error");
      this.render();
      return;
    }
    this.pending = null; this.frozen = null;
    this.on.create?.(drawing);
    this.on.tool?.("cursor");
    // With a mouse, how to name it (a phone has the label field in the panel below the chart); after the cursor is
    // back, whose instructions would otherwise replace it.
    if (this.lastPointer === "mouse") {
      this.showHint(drawing.label ? "Type to change its label, or double-click it later."
        : "Type to label it, or double-click it later.");
    }
  }

  orderClick(pt) {
    const pl = this.placing;
    if (!pl || this.model.locked) return;
    const ref = sim.refClose(this.model.chart), long = pl.side === "long", sign = long ? 1 : -1;
    const price = this.snap(pt).p;
    if (pl.stage === 0) {
      pl.entry = this.nearMarket(pt) ? null : price;
      pl.stage = 1;
      this.freeze();
      this.begun();
    } else if (pl.stage === 1) {
      const entry = pl.entry ?? ref;
      if ((entry - price) * sign <= 0) {
        this.showHint(`The stop must be ${long ? "below" : "above"} the entry for ${long ? "a long" : "a short"}.`,
                      "error");
        return;
      }
      pl.stop = price; pl.stage = 2;
      this.roomForTarget(entry, price);
    } else {
      const entry = pl.entry ?? ref;
      if ((price - entry) * sign <= 0) {
        this.showHint(`The target must be ${long ? "above" : "below"} the entry for ${long ? "a long" : "a short"}.`,
                      "error");
        return;
      }
      const draft = { side: pl.side, entry: pl.entry, stop: pl.stop, target: price, done: true };
      this.placing = null; this.frozen = null;
      this.on.draft?.(draft);
      this.on.tool?.("cursor");
      return;
    }
    // The next step's instructions, never a tip left from before.
    this.clearFlash();
    this.showHint();
    this.render();
  }

  // Once the stop is in, the price range held for the order takes in the entry plus and minus three times the stop's
  // distance, so a target of up to 3R is on the chart to click (more room: drag the price scale).
  roomForTarget(entry, stop) {
    const held = this.frozen || (this.g && { lo: this.g.lo, hi: this.g.hi });
    if (!held || !isNum(entry) || !isNum(stop)) return;
    const reach = 3 * Math.abs(entry - stop);
    let lo = Math.min(held.lo, entry - reach), hi = Math.max(held.hi, entry + reach);
    if (lo === held.lo && hi === held.hi) return;
    const pad = (hi - lo) * 0.04;
    if (lo < held.lo) lo = held.lo >= 0 ? Math.max(0, lo - pad) : lo - pad;
    if (hi > held.hi) hi += pad;
    this.frozen = { lo, hi };
  }

  wheel(e) {
    if (!this.model || !this.g) return;
    e.preventDefault();
    const g = this.g, pt = this.pos(e);
    if (this.region(pt) === "price") {
      // Over the price axis the wheel scales prices around the pointer, as dragging the axis does.
      const factor = Math.exp(e.deltaY * (e.deltaMode === 1 ? 30 : 1) * 0.0015), at = g.p(clamp(pt.y, 0, g.plotH));
      this.frozen = null;
      this.view = { ...this.view, lo: at - (at - g.lo) * factor, hi: at + (g.hi - at) * factor };
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      this.view = clampView({ ...this.view, right: this.view.right + e.deltaX / g.barW }, this.model.chart);
    } else {
      const scale = e.deltaMode === 1 ? 30 : 1;
      const span = clamp(this.view.span * Math.exp(e.deltaY * scale * 0.0015), 16, 420);
      const at = clamp(pt.x, 0, g.plotW), index = g.i(at);
      this.view = clampView({ ...this.view, span, right: index + (g.plotW - at) / (g.plotW / span) }, this.model.chart);
    }
    this.saveView();
    this.render();
  }

  dblclick(e) {
    if (!this.model || !this.g) return;
    const pt = this.pos(e), region = this.region(pt);
    if (region === "price") {
      // Back to automatic price scaling.
      this.view = { right: this.view.right, span: this.view.span };
      this.saveView();
      this.render();
      return;
    }
    if (region === "time") { this.fit(); return; }
    if (this.tool !== "cursor") return;
    const hit = this.hitTest(pt);
    if (hit?.kind === "drawing" || hit?.kind === "handle") this.on.label?.(hit.drawing);
  }
}

// ---------------------------------------------------------------- the report's images

export function renderStatic(canvas, chart, { width = 1200, height = 560, scale = 1, settings } = {}) {
  canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  const lastIndex = chart.bars.length - 1;
  const view = { right: lastIndex + 3, span: lastIndex + 7 };
  const results = (chart.trades || []).map((trade) => sim.simulate(trade, chart));
  const model = { chart, results, settings: settings || { volume: true, ma: {} } };
  const range = autoRange(chart, view, model, []) || { lo: 90, hi: 110 };
  const g = geometry(ctx, width, height, view, range);
  paint(ctx, g, model, { static: true });
  return canvas;
}
