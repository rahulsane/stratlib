// TradeTest report: the visitor's scorecard as one standalone HTML page, and their trades as CSV. Every figure comes
// from sim.js, the code the page scores with. Pure: no DOM, no network, no Math.random; the page hands in the chart
// pictures, so the same file runs in the browser and under `node --test`.
//
// Only finished charts are scored or revealed. An unfinished chart shows its progress and nothing else: no symbol,
// name, dates, trades or drawings. Everything taken from the state is escaped, visitor text (labels, notes) above
// all, and the charts' bars are never embedded: a finished chart appears as the picture renderImage draws.
//
// API reference
//
//   buildReport(state, { renderImage, generatedAt }) -> string        a complete HTML document
//       state is the page's saved state ({ set, charts: [ChartState] }). renderImage(chart) -> an image data URL of
//       a finished chart, called with the chart as saved; anything else, or a throw, leaves the picture out.
//       generatedAt: Date | ISO string | ms, default now, shown in the visitor's local time.
//   buildTradesCsv(state) -> string      RFC 4180 with CRLF line ends: one row per trade on a finished chart, in real
//                                        prices and ISO dates, numbers with a plain minus and no grouping
//   resultLine(state) -> string          one line to share, from the same figures as the report: "TradeTest: +3.20R
//                                        on 4 charts, 60% won, ahead of 71% of random-entry runs. <site link>", plus
//                                        "(also 3 trades closed on unfinished charts: -2.10R)" when there were any, and
//                                        "trades on 2 (other) unfinished charts couldn't be loaded and aren't counted"
//                                        when charts with trades came without their rows
//   guessLine(chart) -> string           a finished chart's guess and how it went: "Your guess: 2010s, stock. Era
//                                        right, type right." or "No guess"; "" for a chart not finished
//   reportFileName(state) -> "tradetest-report-<set>.html"
//   tradesCsvFileName(state) -> "tradetest-trades-<set>.csv"
//   escapeHtml(value) -> string          & < > " ' as entities; null and undefined as ""
//   csvField(value) -> string            one CSV field, quoted when it holds a comma, quote, CR or LF
//   DISCLAIMER                           the line the page and the report carry
//
// Real prices are chart prices divided by reveal.scale: prices as traded on the replay's last day.
//
// An unfinished chart's closed trades are counted beside the score from its rows up to its current bar (the page keeps
// them); a chart with trades whose rows didn't come is named as not counted, never left out without a word.

// sim.js under this file's own version query, so the page and the report share one copy of the scoring code.
const {
  COST, DOLLARS_PER_R, MIN_RISK, buyAndHold, chartSummary, fmtPct, fmtPrice, fmtR, randomBaseline, scoredResults,
  summarize, unfinishedClosed,
} = await import(`./sim.js${new URL(import.meta.url).search}`);

export const DISCLAIMER =
  "Simulated trades on historical prices. Past results are not a forecast, and nothing here is investment advice.";

const MINUS = String.fromCharCode(0x2212);   // true minus sign
const NONE = String.fromCharCode(0x2013);    // en dash for a missing value
const DRAWS = 2000;                          // random-entry draws, named in the text
const SITE = "https://stratlib.blxnksy.dev/tradetest";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const C = 3;                                 // a bar row's close: [o, h, l, c, v, ...]

const SIDES = { long: "Long", short: "Short" };
const KINDS = { market: "Market", limit: "Limit", stop: "Stop entry" };
const DRAWINGS = { trend: "Trendline", ray: "Ray", channel: "Channel", hline: "Price level", vline: "Time level",
                   zone: "Zone" };
// Drawing colours by name; the state's colour is only ever a key into this table, never written into the page.
const COLOURS = { text: "#d1d4dc", steel: "#a3abbd", brass: "#c9a26e", teal: "#3cc4b5", coral: "#ff7f7c",
                  violet: "#b39ddb", amber: "#ffb74d" };
// How a closed trade ended, then the detail under it. Stops and targets say "gap open" when the bar opened through
// the level. An order that never filled was cancelled, missed (a bar opened past its level first) or expired.
const EXITS = { target: ["Target", ""], stop: ["Stop", ""], close: ["Closed by you", "at the next open"],
                finish: ["Closed at finish", "at the next open"], end: ["End of replay", "at the last close"] };
const MISSES = { gap_stop: "opened past the stop", gap_target: "opened past the target" };
// The guess offered before the reveal: the decade the replay starts in, and stock or ETF.
const ERAS = { "2000s": 2000, "2010s": 2010, "2020s": 2020 };
const GUESS_KINDS = { stock: "stock", etf: "ETF" };

// A table's entry for a value from the state: only the table's own keys count, never "constructor" and the like.
const pick = (table, key, fallback) => (typeof key === "string" && Object.hasOwn(table, key) ? table[key] : fallback);

// ---------------------------------------------------------------- text

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return value == null ? "" : String(value).replace(/[&<>"']/g, (ch) => ENTITIES[ch]);
}

const esc = escapeHtml;
const num = (x) => typeof x === "number" && Number.isFinite(x);
const plural = (n, one, many = one + "s") => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
const capital = (text) => (text ? text[0].toUpperCase() + text.slice(1) : text);

// The text-colour class for a figure's direction; a value that rounds to zero stays neutral.
function tone(x, unit = 0.005) {
  if (!num(x) || Math.abs(x) < unit) return "";
  return x > 0 ? "tt-up" : "tt-down";
}

// "+$5,200", "−$310", "$0".
function signedDollars(x) {
  if (!num(x)) return NONE;
  const text = "$" + fmtPrice(Math.abs(x), 0);
  return Math.round(x) === 0 ? text : (x < 0 ? MINUS : "+") + text;
}

const dollars = (x) => (num(x) ? "$" + fmtPrice(x) : NONE);
// An R distance with no sign, for spreads: "3.20R".
const plainR = (x) => (num(x) ? fmtPrice(Math.abs(x)) + "R" : NONE);
const share = (x) => (num(x) ? fmtPct(100 * x, 0, false) : NONE);
// The percentile, worded as the page words it: "ahead of 71% of random-entry runs".
const ahead = (base) => `ahead of ${share(base.percentile)} of random-entry runs`;

// "Mar 12, 2019" from an ISO date, read as written (no time zone).
function fmtDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(typeof iso === "string" ? iso : "");
  if (!m || +m[2] < 1 || +m[2] > 12) return null;
  return `${MONTHS[+m[2] - 1]} ${+m[3]}, ${m[1]}`;
}

function stamp(at) {
  const d = at instanceof Date ? at : new Date(at ?? Date.now());
  if (Number.isNaN(d.getTime())) return "";
  const pad = (x) => String(x).padStart(2, "0");
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const setId = (state) => String(state?.set ?? "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 16) || "set";

export function reportFileName(state) {
  return `tradetest-report-${setId(state)}.html`;
}

export function tradesCsvFileName(state) {
  return `tradetest-trades-${setId(state)}.csv`;
}

// ---------------------------------------------------------------- charts

// The chart as sim.js reads it when the report may score and reveal it (finished, with its bars in), with missing
// lists made empty and the finish point as sim.js takes it; null otherwise. Every sim.js set statistic skips null,
// so the report and the page score the same trades.
function scoredChart(chart) {
  if (!chart || chart.finished !== true || !Array.isArray(chart.bars) || chart.bars.length < chart.nCtx) return null;
  return { ...chart, trades: chart.trades || [], drawings: chart.drawings || [],
           finishedAt: chart.finishedAt ?? chart.k };
}

const reveal = (chart) => (chart.reveal && typeof chart.reveal === "object" ? chart.reveal : {});

// The bar's number as the page shows it: replay bars from 1, the last context bar 0, history negative.
function barLabel(chart, i) {
  if (!num(i)) return NONE;
  const j = Math.round(i) - chart.nCtx + 1;
  return "Bar " + (j < 0 ? MINUS + -j : j);
}

function isoAt(chart, i) {
  const dates = reveal(chart).dates;
  return Array.isArray(dates) && num(i) && typeof dates[i] === "string" ? dates[i] : null;
}

const dateAt = (chart, i) => fmtDate(isoAt(chart, i));

// "Bar 13 · Mar 29, 2019"; on paper the date goes under the bar, to keep the trades table narrow.
function where(chart, i) {
  const date = dateAt(chart, i);
  return barLabel(chart, i) + (date ? `<span class="tt-sep"> · </span><span class="tt-date">${date}</span>` : "");
}

// The chart shows the real price times the scale.
function realPrice(chart, p) {
  const scale = reveal(chart).scale;
  return num(p) && num(scale) && scale > 0 ? p / scale : null;
}

function progress(chart) {
  const k = num(chart?.k) ? chart.k : 0, n = num(chart?.nReplay) ? chart.nReplay : 100;
  return k > 0 ? `Bar ${k} of ${n}` : "Not started";
}

function identity(chart) {
  const r = reveal(chart), symbol = r.symbol ? String(r.symbol) : "", name = r.name ? String(r.name) : "";
  return { symbol, name: name || symbol || "Instrument not loaded" };
}

// The visitor's guess against the reveal. era and kind are the guessed values (null when not answered, or not one
// of the choices); eraRight and kindRight are true or false, or null when there is nothing to judge.
function guessOf(chart) {
  const g = chart.guess && typeof chart.guess === "object" ? chart.guess : {}, r = reveal(chart);
  const era = typeof g.era === "string" && Object.hasOwn(ERAS, g.era) ? g.era : null;
  const kind = typeof g.kind === "string" && Object.hasOwn(GUESS_KINDS, g.kind) ? g.kind : null;
  const year = /^(\d{4})-\d{2}-\d{2}/.exec(typeof r.start === "string" ? r.start : "");
  const actualEra = year ? `${Math.floor(+year[1] / 10) * 10}s` : null;
  const actualKind = r.kind === "stock" || r.kind === "etf" ? r.kind : null;
  return { era, kind, eraRight: era && actualEra ? era === actualEra : null,
           kindRight: kind && actualKind ? kind === actualKind : null };
}

// [what was guessed, how it went]: ["2010s, stock", "Era right, type right"]; ["", ""] for no guess.
function guessParts(chart) {
  const { era, kind, eraRight, kindRight } = guessOf(chart);
  const said = [era, kind && GUESS_KINDS[kind]].filter(Boolean).join(", ");
  const marks = [eraRight == null ? "" : `era ${eraRight ? "right" : "wrong"}`,
                 kindRight == null ? "" : `type ${kindRight ? "right" : "wrong"}`].filter(Boolean).join(", ");
  return [said, capital(marks)];
}

export function guessLine(chart) {
  const scored = scoredChart(chart);
  if (!scored) return "";
  const [said, marks] = guessParts(scored);
  return said ? `Your guess: ${said}.${marks ? ` ${marks}.` : ""}` : "No guess";
}

// ---------------------------------------------------------------- tables

function table(head, rows, cls = "") {
  const th = head.map(([text, numeric]) => `<th${numeric ? ' class="tt-num"' : ""}>${text}</th>`).join("");
  return `<div class="tt-wrap"><table class="tt-table ${cls}"><thead><tr>${th}</tr></thead>` +
    `<tbody>${rows.join("")}</tbody></table></div>`;
}

// A cell holding a value with an optional muted detail under it.
const cell = (value, detail = "", cls = "") =>
  `<td${cls ? ` class="${cls}"` : ""}>${value}${detail ? `<small>${detail}</small>` : ""}</td>`;

const numCell = (value, cls = "") => `<td class="tt-num${cls ? " " + cls : ""}">${value}</td>`;

// ---------------------------------------------------------------- the score

function figure(key, label, value, detail, cls = "") {
  return `<div class="tt-figure" data-figure="${key}"><span>${label}</span><b class="${cls}">${value}</b>` +
    `<span>${detail}</span></div>`;
}

function figures(sum) {
  const n = sum.trades;
  const wins = n ? `${sum.wins} won, ${sum.losses} lost${sum.flat ? `, ${sum.flat} flat` : ""}` : "No closed trades";
  const se = num(sum.seR) ? ` <small>± ${plainR(sum.seR)}</small>` : "";
  const pfDetail = !n ? "No closed trades" : !sum.losses ? "No losing trades"
    : `${fmtPrice(sum.grossWinR)}R won over ${fmtPrice(sum.grossLossR)}R lost`;
  return '<div class="tt-figures">' +
    figure("total-r", "Total R", fmtR(sum.totalR), `Net of costs ${fmtR(sum.totalRNet)}`, tone(sum.totalR)) +
    figure("win-rate", "Win rate", share(sum.winRate), wins) +
    figure("mean-r", "Average R", fmtR(sum.meanR) + se,
           num(sum.seR) ? "Per trade, ± one standard error" : "Per trade", tone(sum.meanR)) +
    figure("profit-factor", "Profit factor", num(sum.profitFactor) ? fmtPrice(sum.profitFactor) : NONE, pfDetail) +
    figure("trades", "Closed trades", String(n),
           n ? `${sum.longs.trades} long, ${sum.shorts.trades} short` : "On finished charts") +
    "</div>" +
    '<p class="tt-dollars" data-figure="dollars">At 1% risk per trade on a $100,000 account: ' +
    `<b class="${tone(sum.dollars, 0.5)}">${signedDollars(sum.dollars)}</b>, or ` +
    `<b class="${tone(sum.dollarsNet, 0.5)}">${signedDollars(sum.dollarsNet)}</b> after costs.</p>`;
}

const colour = (x, text = fmtR(x)) => `<span class="${tone(x)}">${text}</span>`;

// Trades closed on charts the visitor didn't finish: not in the score, whose charts must be finished to compare with
// random entries, but beside it, so a set can't be judged on its good charts only. Charts whose rows didn't come are
// named as not counted.
function alsoClosed(also, missing) {
  const lost = missing.count ? `<p class="tt-also" data-figure="not-loaded">${capital(notLoaded(missing))}.</p>` : "";
  if (!also.trades) return lost;
  return '<p class="tt-also" data-figure="unfinished">Also closed on charts you didn\'t finish: ' +
    `${plural(also.trades, "trade")}, ${colour(also.totalR)} (net of costs ${colour(also.totalRNet)}). Not in the ` +
    "figures above; finish those charts to score them.</p>" + lost;
}

// "trades on 2 unfinished charts couldn't be loaded and aren't counted" ("other" beside an Also-closed count).
function notLoaded(missing, other = false) {
  const what = `${other ? "other " : ""}${missing.unfinished ? "unfinished chart" : "chart"}`;
  return `trades on ${plural(missing.count, what)} couldn't be loaded and aren't counted`;
}

function comparisons(sum, base, holds) {
  const trades = base.trades === 1 ? "your one closed trade was"
    : `every one of your ${plural(base.trades, "closed trade")} was`;
  const random = base.trades
    ? `<b data-figure="beat">${capital(ahead(base))}</b>` +
      `<p>In each of ${base.draws.toLocaleString("en-US")} runs, ${trades} re-entered as a market order on a random ` +
      "bar of its own replay, with the same side and the same stop and target distances from its planned entry. " +
      `The runs made ${fmtR(base.totalMean)} on average (${colour(base.meanR)} a trade), give or take ` +
      `${plainR(base.sdTotal)}; yours made ${colour(base.total)} (${colour(sum.meanR)} a trade).</p>`
    : "<b>Nothing to compare yet</b><p>Finish a chart with a closed trade to see how random entries with the same " +
      "stops and targets would have done.</p>";
  const moves = holds.filter(num), avg = mean(moves);
  const hold = moves.length
    ? `<b data-figure="buy-hold">${colour(avg, fmtPct(avg, 1))} a chart on average</b>` +
      "<p>Buying at the last close before each replay and selling at its last close, over " +
      `${plural(moves.length, "finished chart")}; ${moves.filter((x) => x > 0).length} rose and ` +
      `${moves.filter((x) => x < 0).length} fell. A yardstick for how the charts moved, not a like-for-like score: ` +
      "your trades risked 1R each.</p>"
    : "<b>Nothing to compare yet</b><p>Finish a chart to see what simply holding it through the replay would have " +
      "made.</p>";
  return '<div class="tt-compare">' +
    `<div class="tt-cmp"><span>Random entries with your stops and targets</span>${random}</div>` +
    `<div class="tt-cmp"><span>Buy and hold over the same replays</span>${hold}</div></div>`;
}

// Round steps of 1, 2 or 5 times a power of ten, the ones inside lo..hi.
function niceTicks(lo, hi, count = 6) {
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].find((m) => m * mag >= raw * (1 - 1e-9)) * mag;
  const ticks = [];
  for (let j = Math.ceil(lo / step - 1e-9); j * step <= hi + step * 1e-9; j++) ticks.push(j * step);
  return { ticks, digits: step >= 1 ? 0 : step >= 0.1 ? 1 : 2 };
}

const pc = (x) => `${Math.round(x * 1000) / 10}%`;
const r1 = (v) => Math.round(v * 10) / 10;

// The curve's chart labels, laid out for the narrowest plot each screen size gives (the plot stretches with the page):
// wide screens, then tablets and paper, then phones, as the CSS media queries below split them. Widths are estimates
// of 11px text, kept on the generous side.
const AXIS_LAYOUTS = [["w", 820], ["m", 420], ["n", 200]];   // [key, plot width in px]
const AXIS_DIGIT = 7, AXIS_WORD = 34, AXIS_GAP = 6;            // px: a digit, "Chart ", the space kept between labels

// Every band gets a label under its middle: "Chart 5" when every band holds its label, otherwise the chart number
// alone after a first "Chart 1" (when that fits), and a label that would run into the one before drops to a row
// below, with a short leader up to the axis, so none is ever left out.
// Bands run from divider to divider, in trade steps (trade j sits at j + 1). Returns the label spans, each carrying
// its row and form per layout as custom properties, and the plot's style: its most rows per layout ("" for one row).
function axisLabels(bands, n) {
  const spans = bands.map((band) => {
    const left = band.from ? band.from + 0.5 : 0, right = band.to < n ? band.to + 0.5 : n;
    const digits = String(band.chart + 1).length * AXIS_DIGIT;
    return { middle: (left + right) / 2 / n, width: (right - left) / n, digits, full: AXIS_WORD + digits };
  });
  const layouts = AXIS_LAYOUTS.map(([key, px]) => {
    const ends = [], fits = spans.map(({ width, full }) => width * px >= full + AXIS_GAP), all = fits.every(Boolean);
    const marks = spans.map(({ middle, digits, full }, j) => {
      const long = all || (j === 0 && fits[0]), half = (long ? full : digits) / 2;
      let row = ends.findIndex((end) => middle * px - half >= end + AXIS_GAP);
      if (row < 0) row = ends.push(0) - 1;
      ends[row] = middle * px + half;
      return [row ? `--r${key}:${row}` : "", long ? "" : `--w${key}:none`];
    });
    return { marks, style: ends.length > 1 ? `--x${key}:${ends.length - 1}` : "" };
  });
  const labels = bands.map((band, j) => {
    const vars = layouts.flatMap((layout) => layout.marks[j]).filter(Boolean).map((v) => ";" + v).join("");
    return `<span class="tt-x" style="left:${pc(spans[j].middle)}${vars}"><span class="tt-x-word">Chart </span>` +
      `${band.chart + 1}</span>`;
  });
  return { labels, style: layouts.map((layout) => layout.style).filter(Boolean).join(";") };
}

// Cumulative R over the closed trades, chart by chart in the order they closed, with the random-entry average as a
// dashed line. The lines are SVG stretched to the box; the dots and labels are HTML laid over it, so they keep their
// size at any width, and each dot's tooltip names its trade.
function curve(results, base) {
  const done = results.filter((res) => res.exit && num(res.r))
    .sort((a, b) => a.chart - b.chart || a.exit.i - b.exit.i || a.id - b.id);
  const n = done.length;
  if (!n) return '<p class="tt-empty">No closed trades on finished charts yet, so there is no curve to draw.</p>';
  const points = [[0, 0]];
  let total = 0;
  for (const res of done) points.push([points.length, (total += res.r)]);
  const randomEnd = num(base.meanR) ? base.meanR * n : null;
  const ys = points.map((p) => p[1]).concat(randomEnd ?? 0);
  let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  if (hi - lo < 1) {
    const mid = (hi + lo) / 2;
    lo = Math.min(lo, mid - 0.5);
    hi = Math.max(hi, mid + 0.5);
  }
  const room = (hi - lo) * 0.08;
  lo -= room;
  hi += room;
  const W = 1000, H = 300, x = (v) => (v / n) * W, y = (v) => ((hi - v) / (hi - lo)) * H;

  const { ticks, digits } = niceTicks(lo, hi);
  const grid = ticks.map((t) => `<line class="${Math.abs(t) < 1e-9 ? "tt-zero" : "tt-grid"}" x1="0" x2="${W}" ` +
    `y1="${r1(y(t))}" y2="${r1(y(t))}"/>`);
  const yLabels = ticks.map((t) => `<span class="tt-y" style="top:${pc(y(t) / H)}">${fmtR(t, digits)}</span>`);
  // One band per chart: a divider before it, and its label under the axis.
  const bands = [];
  done.forEach((res, j) => {
    const last = bands[bands.length - 1];
    if (last && last.chart === res.chart) last.to = j + 1;
    else bands.push({ chart: res.chart, from: j, to: j + 1 });
  });
  const dividers = bands.filter((band) => band.from).map((band) =>
    `<line class="tt-divider" x1="${r1(x(band.from + 0.5))}" x2="${r1(x(band.from + 0.5))}" y1="0" y2="${H}"/>`);
  const { labels: xLabels, style } = axisLabels(bands, n);
  const line = points.map(([px, py], j) => `${j ? "L" : "M"}${r1(x(px))} ${r1(y(py))}`).join(" ");
  const random = randomEnd == null ? ""
    : `<line class="tt-random" x1="0" y1="${r1(y(0))}" x2="${W}" y2="${r1(y(randomEnd))}"/>`;
  const dots = n > 160 ? [] : done.map((res, j) => {
    const [px, py] = points[j + 1];
    const what = `Chart ${res.chart + 1}, trade #${res.id} · ${pick(SIDES, res.side, "Trade")} · ` +
      `${pick(EXITS, res.exit.reason, ["Closed"])[0]} · ${fmtR(res.r)} · running total ${fmtR(py)}`;
    const cls = res.r > 0 ? "tt-pt-up" : res.r < 0 ? "tt-pt-down" : "tt-pt-flat";
    return `<i class="tt-pt ${cls}" style="left:${pc(x(px) / W)};top:${pc(y(py) / H)}" title="${esc(what)}"></i>`;
  });
  const label = `Cumulative R over ${plural(n, "closed trade")}, ending at ${fmtR(total)}` +
    (randomEnd == null ? "" : `; random entries averaged ${fmtR(randomEnd)} over as many trades`);
  return '<div class="tt-legend">' +
    `<span><i class="tt-key-line"></i>Your trades <b class="${tone(total)}">${fmtR(total)}</b></span>` +
    (randomEnd == null ? ""
      : `<span><i class="tt-key-dash"></i>Random entries, average <b>${fmtR(randomEnd)}</b></span>`) +
    '<span><i class="tt-key-dot tt-pt-up"></i>Won</span><span><i class="tt-key-dot tt-pt-down"></i>Lost</span></div>' +
    `<div class="tt-plot" role="img" aria-label="${esc(label)}" data-total="${fmtR(total)}" data-points="${n}"` +
    `${style ? ` style="${style}"` : ""}>` +
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${grid.join("")}${dividers.join("")}` +
    `${random}<path class="tt-line" d="${line}"/></svg>${dots.join("")}${yLabels.join("")}${xLabels.join("")}</div>`;
}

function measures(sum, results, charts) {
  const sides = [["Long", sum.longs], ["Short", sum.shorts], ["All", sum]].map(([label, s]) =>
    `<tr${label === "All" ? ' class="tt-total"' : ""}><td>${label}</td>${numCell(s.trades)}${numCell(s.wins)}` +
    numCell(share(s.trades ? s.wins / s.trades : null)) + numCell(s.trades ? fmtR(s.totalR) : NONE, tone(s.totalR)) +
    numCell(fmtR(s.meanR), tone(s.meanR)) + "</tr>");
  const row = (label, value) => `<tr><td>${label}</td>${numCell(fmtR(value), tone(value))}</tr>`;
  const trade = [row("Best trade", sum.bestR), row("Worst trade", sum.worstR), row("Median trade", sum.medianR),
    row("Average win", sum.avgWinR), row("Average loss", sum.avgLossR), row("Average net of costs", sum.meanRNet),
    `<tr><td>Bars held, average</td>${numCell(num(sum.avgBarsHeld) ? sum.avgBarsHeld.toFixed(1) : NONE)}</tr>`];
  const ended = Object.entries(EXITS).map(([reason, [label]]) => {
    const hits = results.filter((res) => res.exit?.reason === reason), r = hits.reduce((a, res) => a + res.r, 0);
    return `<tr><td>${label}</td>${numCell(hits.length)}${numCell(hits.length ? fmtR(r) : NONE, tone(r))}</tr>`;
  });
  const never = { cancelled: 0, missed: 0, expired: 0 };
  for (const chart of charts.filter(Boolean)) {
    const s = chartSummary(chart);
    for (const key of Object.keys(never)) never[key] += s[key];
  }
  const unfilled = never.cancelled + never.missed + never.expired
    ? `<p class="tt-foot">Orders that never filled: ${never.cancelled} cancelled, ${never.missed} missed on a gap, ` +
      `${never.expired} expired at the finish. They are listed under their charts and not scored.</p>` : "";
  return '<div class="tt-trio">' +
    "<div><h3>Longs and shorts</h3>" +
    `${table([["Side"], ["Trades", 1], ["Won", 1], ["Win rate", 1], ["Total R", 1], ["Average R", 1]], sides)}</div>` +
    `<div><h3>Trade measures</h3>${table([["Measure"], ["R", 1]], trade)}</div>` +
    `<div><h3>How trades ended</h3>${table([["Exit"], ["Trades", 1], ["Total R", 1]], ended)}${unfilled}</div></div>`;
}

// "3 of 4 eras, 4 of 4 types" right, over the finished charts where they were guessed.
function guessTotals(charts) {
  const count = { era: [0, 0], kind: [0, 0] };
  for (const chart of charts.filter(Boolean)) {
    const g = guessOf(chart);
    for (const [key, right] of [["era", g.eraRight], ["kind", g.kindRight]]) {
      if (right == null) continue;
      count[key][0] += right ? 1 : 0;
      count[key][1] += 1;
    }
  }
  const [eraRight, eras] = count.era, [kindRight, kinds] = count.kind;
  return [eras ? `${eraRight} of ${plural(eras, "era")}` : "", kinds ? `${kindRight} of ${plural(kinds, "type")}` : ""]
    .filter(Boolean).join(", ");
}

// The scorecard: one row per chart, the unfinished ones with their progress only.
function overview(raw, charts) {
  const rows = [], holds = [];
  let closed = 0, won = 0, totalR = 0, totalRNet = 0;
  raw.forEach((rawChart, index) => {
    const chart = charts[index], n = index + 1;
    if (!chart) {
      rows.push(`<tr class="tt-open"><td>Chart ${n}</td><td colspan="8">` +
        `<span class="tt-pending">Not finished, not scored</span><small>${progress(rawChart)}</small></td></tr>`);
      return;
    }
    const s = chartSummary(chart), hold = buyAndHold(chart), { symbol, name } = identity(chart), r = reveal(chart);
    const start = fmtDate(r.start), end = fmtDate(r.end), [said, marks] = guessParts(chart);
    closed += s.closed;
    won += s.wins;
    totalR += s.totalR;
    totalRNet += s.totalRNet;
    if (num(hold)) holds.push(hold);
    rows.push(`<tr><td><a href="#chart-${n}">Chart ${n}</a></td>` + cell(`<b>${esc(symbol || NONE)}</b>`, esc(name)) +
      numCell(fmtR(s.totalR), tone(s.totalR)) + numCell(fmtR(s.totalRNet), tone(s.totalRNet)) +
      numCell(s.closed) + numCell(s.wins) + numCell(fmtPct(hold, 1), tone(hold)) +
      cell(start && end ? `${start} to ${end}` : NONE,
           chart.finishedAt < chart.nReplay ? `Finished at bar ${chart.finishedAt}` : "Played to the end") +
      (said ? cell(said, marks) : '<td><span class="tt-muted">No guess</span></td>') + "</tr>");
  });
  const done = charts.filter(Boolean).length, avg = mean(holds), guessed = guessTotals(charts);
  if (done) {
    rows.push(`<tr class="tt-total"><td>All</td><td>${plural(done, "finished chart")}</td>` +
      numCell(fmtR(totalR), tone(totalR)) + numCell(fmtR(totalRNet), tone(totalRNet)) + numCell(closed) +
      numCell(won) + cell(fmtPct(avg, 1), "average", `tt-num ${tone(avg)}`) + "<td></td>" +
      (guessed ? cell(guessed, "guessed right") : '<td><span class="tt-muted">No guesses</span></td>') + "</tr>");
  }
  const head = [["Chart"], ["Instrument"], ["Total R", 1], ["Net R", 1], ["Closed", 1], ["Won", 1], ["Buy and hold", 1],
                ["Replay"], ["Your guess"]];
  return { holds, html: table(head, rows, "tt-overview") };
}

// ---------------------------------------------------------------- one finished chart

// An order level in chart prices, the real price under it, and where it was moved to if it was.
function priceCell(chart, p, moved) {
  const real = realPrice(chart, p);
  const detail = [real == null ? "" : dollars(real), num(moved) && moved !== p ? `moved to ${fmtPrice(moved)}` : ""];
  return cell(fmtPrice(p), detail.filter(Boolean).join("<br>"), "tt-num");
}

// [what happened, the detail under it]
function outcome(chart, res) {
  if (res.status === "closed") {
    const [label, detail] = pick(EXITS, res.exit.reason, ["Closed", ""]);
    const level = res.exit.reason === "stop" ? res.stopNow : res.exit.reason === "target" ? res.targetNow : null;
    return [label, num(level) && res.exit.price !== level ? "gap open" : detail];
  }
  if (res.status === "missed") {
    return ["Missed", `${barLabel(chart, res.missed.i)} ${pick(MISSES, res.missed.reason, "gapped")}`];
  }
  // The bar the visitor was on when they cancelled, as the page says it.
  if (res.status === "cancelled") return ["Cancelled", `at bar ${res.cancelled.i - chart.nCtx}`];
  if (res.status === "expired") return ["Expired", "unfilled at the finish"];
  if (res.status === "closing") return ["Closing", "at the next open"];
  return [res.status === "open" ? "Open" : "Pending", ""];
}

function tradesTable(chart, results) {
  if (!chart.trades.length) return '<p class="tt-empty">No trades on this chart.</p>';
  const none = results.some((res) => res.status === "closed") ? ""
    : '<p class="tt-empty tt-above">No closed trades on this chart.</p>';
  const withNotes = chart.trades.some((trade) => typeof trade.note === "string" && trade.note.trim());
  const rows = chart.trades.map((trade, n) => {
    const res = results[n], [what, why] = outcome(chart, res), placed = chart.nCtx + trade.placedAt - 1;
    const held = num(res.barsHeld) ? (res.barsHeld ? plural(res.barsHeld, "bar") : "same bar") : "";
    const point = (at) => (at ? cell(fmtPrice(at.price), where(chart, at.i), "tt-num") : numCell(NONE));
    // A market order is planned at the close it was placed at, which is where its risk is measured from.
    const market = cell("Market", num(res.planned) ? `planned ${fmtPrice(res.planned)}` : "next open", "tt-num");
    return `<tr>${numCell(esc(trade.id))}` +
      cell(pick(SIDES, trade.side, NONE), pick(KINDS, res.kind, "")) +
      cell(barLabel(chart, placed), dateAt(chart, placed) || "") +
      (trade.entry == null ? market : priceCell(chart, trade.entry)) +
      priceCell(chart, trade.stop, res.stopNow) + priceCell(chart, trade.target, res.targetNow) +
      point(res.fill) + point(res.exit) + cell(what, why, "tt-result") +
      cell(fmtR(res.r), res.fill ? `net ${fmtR(res.rNet)}` : "", `tt-num ${tone(res.r)}`) +
      cell(fmtPct(res.pct), held, `tt-num ${tone(res.pct)}`) +
      (withNotes ? `<td class="tt-note">${esc(trade.note)}</td>` : "") + "</tr>";
  });
  const head = [["#", 1], ["Trade"], ["Placed"], ["Entry", 1], ["Stop", 1], ["Target", 1], ["Fill", 1], ["Exit", 1],
                ["Result"], ["R", 1], ["Move", 1]];
  return none + table(withNotes ? head.concat([["Note"]]) : head, rows, "tt-trades") +
    '<p class="tt-foot">Prices as on the chart; under each order level, the real price as traded on the replay\'s ' +
    "last day. R is measured from the planned entry: the close a market order was placed at, or the entry price.</p>";
}

// Where a drawing sits, in words: "Bar −12 at 98.40 $40.56 to Bar 8 at 104.10 $42.91, extended right".
function placement(chart, d) {
  const price = (p) => {
    const real = realPrice(chart, p);
    return num(p) ? `${fmtPrice(p)}${real == null ? "" : ` <span class="tt-real">${dollars(real)}</span>`}` : NONE;
  };
  const ok = (pt) => !!pt && num(pt.x) && num(pt.p);
  const at = (pt) => (ok(pt) ? `${barLabel(chart, pt.x)} at ${price(pt.p)}` : NONE);
  const [a, b, c] = Array.isArray(d.points) ? d.points : [];
  const extend = d.extend && ["trend", "channel", "zone"].includes(d.type) ? ", extended right" : "";
  if (d.type === "hline") return price(a?.p);
  if (d.type === "vline") return num(a?.x) ? where(chart, a.x) : NONE;
  if (d.type === "ray") return `From ${at(a)} through ${at(b)}`;
  if (d.type === "zone" && ok(a) && ok(b)) {
    return `${price(Math.min(a.p, b.p))} to ${price(Math.max(a.p, b.p))}, ${barLabel(chart, Math.min(a.x, b.x))} ` +
      `to ${barLabel(chart, Math.max(a.x, b.x))}${extend}`;
  }
  if (d.type === "channel" && ok(a) && ok(b) && ok(c) && a.x !== b.x) {
    // The parallel line's distance from the base line, measured at the third point's bar.
    const width = c.p - (a.p + ((b.p - a.p) * (c.x - a.x)) / (b.x - a.x));
    const side = width >= 0 ? "above" : "below";
    return `${at(a)} to ${at(b)}, parallel line ${fmtPrice(Math.abs(width))} ${side}${extend}`;
  }
  return `${at(a)} to ${at(b)}${extend}`;
}

function drawingsTable(chart) {
  if (!chart.drawings.length) return '<p class="tt-empty">No drawings on this chart.</p>';
  const rows = chart.drawings.map((d) => {
    const label = typeof d.label === "string" && d.label.trim() ? esc(d.label)
      : '<span class="tt-muted">No label</span>';
    // When it was drawn, and when its geometry last changed if that was later: a line moved with hindsight says so.
    const created = num(d.createdAt) && d.createdAt > 0 ? Math.round(d.createdAt) : 0;
    const drawn = (created ? `At bar ${created}` : "Before the replay") +
      (num(d.movedAt) && d.movedAt > created ? `, moved at bar ${Math.round(d.movedAt)}` : "");
    return `<tr><td><i class="tt-swatch" style="background:${pick(COLOURS, d.color, COLOURS.steel)}"></i>` +
      `${pick(DRAWINGS, d.type, "Drawing")}</td><td class="tt-note">${label}</td><td>${placement(chart, d)}</td>` +
      `<td>${drawn}</td></tr>`;
  });
  return table([["Type"], ["Label"], ["Where"], ["Drawn"]], rows, "tt-drawings");
}

function picture(chart, index, renderImage) {
  if (typeof renderImage !== "function") return "";
  let url;
  try {
    url = renderImage(chart);
  } catch {
    return '<p class="tt-empty">The picture of this chart could not be drawn.</p>';
  }
  if (typeof url !== "string" || !/^data:image\/(png|jpeg|webp|gif|svg\+xml)[;,]/i.test(url)) return "";
  const { symbol } = identity(chart);
  const alt = `Chart ${index + 1}${symbol ? `, ${symbol},` : ""} on daily bars with your drawings and trades`;
  return `<figure class="tt-shot"><img src="${esc(url)}" alt="${esc(alt)}"></figure>`;
}

function chartSection(raw, chart, index, renderImage) {
  const n = index + 1, s = chartSummary(chart), r = reveal(chart), { symbol, name } = identity(chart);
  const kind = r.kind === "etf" ? "an ETF" : r.kind === "stock" ? "a stock" : "an instrument";
  const sector = [r.sector, r.industry].filter((x) => typeof x === "string" && x.trim()).join(", ");
  const start = fmtDate(r.start), end = fmtDate(r.end), from = dateAt(chart, 0);
  const real100 = realPrice(chart, 100), hold = buyAndHold(chart);
  const early = chart.finishedAt < chart.nReplay, stopped = dateAt(chart, chart.nCtx + chart.finishedAt - 1);
  const named = symbol && symbol !== name;
  const tags = [r.kind === "etf" ? "ETF" : r.kind === "stock" ? "Stock" : "", r.exchange]
    .filter((x) => typeof x === "string" && x).map((x) => `<span class="tt-tag">${esc(x)}</span>`).join("");
  // The real prices: the close before the replay (100.00 on the chart) and the replay's last close, as traded then.
  const realEnd = realPrice(chart, chart.bars[chart.nCtx + chart.nReplay - 1]?.[C]);
  const sentence = `This was ${esc(name)}${named ? ` (${esc(symbol)})` : ""}, ${kind}` +
    `${sector ? ` in ${esc(sector)}` : ""}${start && end ? `, from ${start} to ${end} on daily bars` : ""}.` +
    (real100 == null ? "" : ` 100.00 on the chart, the close before the replay, was ${dollars(real100)}.` +
      (realEnd == null ? "" : ` The last close was ${dollars(realEnd)}.`));
  // From the close the visitor finished on to the last close: what they didn't get to trade.
  const finishBar = chart.bars[chart.nCtx + chart.finishedAt - 1], lastBar = chart.bars[chart.nCtx + chart.nReplay - 1];
  const after = early && finishBar && lastBar ? 100 * (lastBar[C] / finishBar[C] - 1) : null;
  const context = [
    from ? `The ${chart.nCtx} bars of history before the replay start on ${from}.` : "",
    early ? `You finished at bar ${chart.finishedAt}${stopped ? `, ${stopped}` : ""}; ` +
      (chart.nReplay - chart.finishedAt === 1 ? "the bar after it is shown dimmed and was not traded."
        : `the ${plural(chart.nReplay - chart.finishedAt, "bar")} after it are shown dimmed and were not traded.`) : "",
    num(after) ? `From your finish to the last bar: ${colour(after, fmtPct(after, 1))}.` : "",
    r.note ? `${esc(r.note)}.` : "",
  ].filter(Boolean).join(" ");
  const [said, marks] = guessParts(chart);
  const guess = said ? `<span>Your guess:</span> ${said}.${marks ? ` ${marks}.` : ""}` : "<span>No guess</span>";
  const stat = (label, value, cls = "") => `<div><span>${label}</span><b class="${cls}">${value}</b></div>`;
  // One heading line, no label above it: "Chart 1 · Comerica Incorporated (CMA)", then the tags.
  return `<section class="tt-card tt-chart" id="chart-${n}" data-chart="${n}">` +
    `<h2><span>Chart ${n} · ${esc(name)}${named ? ` <span class="tt-sym">(${esc(symbol)})</span>` : ""}</span>` +
    `${tags}</h2>` +
    `<p class="tt-reveal">${sentence}</p><p class="tt-guess" data-guess="${n}">${guess}</p>` +
    `${context ? `<p class="tt-context">${context}</p>` : ""}` +
    '<div class="tt-stats">' + stat("Closed trades", s.closed) + stat("Won", s.wins) +
    stat("Total R", fmtR(s.totalR), tone(s.totalR)) + stat("Net of costs", fmtR(s.totalRNet), tone(s.totalRNet)) +
    stat("Buy and hold", fmtPct(hold, 1), tone(hold)) +
    stat("Played", early ? `${chart.finishedAt} of ${chart.nReplay} bars` : `All ${chart.nReplay} bars`) + "</div>" +
    picture(raw, index, renderImage) +
    `<h3>Trades</h3>${tradesTable(chart, s.results)}<h3>Drawings</h3>${drawingsTable(chart)}</section>`;
}

// ---------------------------------------------------------------- the rules, in plain words

const RULES = [
  ["Hidden identity", "Each chart is a real US-listed stock or ETF over a real stretch of daily bars, picked at " +
    "random. Prices are rescaled so the last bar before the replay closes at 100, and volume is shown relative to " +
    "its average, so neither gives the instrument away. Your browser gets the name, the dates and the bars you haven't " +
    "reached only when you finish the chart, and there is no going back a bar."],
  ["Real prices", "A real price is the chart price divided by the chart's scale. Each chart above gives the real " +
    "close before the replay, which is 100.00 on the chart, and the last close. Chart prices carry a small random " +
    "offset, at most about 0.3% of the price, so the numbers can't give the real price away. Real prices are as " +
    "traded on the replay's last day, adjusted only for splits during the replay."],
  ["Orders", "Every trade has an entry, a stop and a target, set when it is placed. An order at the current price is " +
    "a market order and fills at the next bar's open. A limit entry (below the price for a long, above it for a " +
    "short) fills when price reaches it, at the entry or a better open. A stop entry (beyond the price) fills when " +
    "price reaches it, at the entry or a worse open."],
  ["Gaps", "A market order is missed if the next bar opens at or past its stop or target, a limit entry if a bar " +
    "opens at or past its stop, and a stop entry if a bar opens at or past its target. An open trade that gaps " +
    "through its stop or target exits at the open, so a gap can cost more than 1R."],
  ["Stops and targets", "On the bar an order fills only the stop can be hit, and when one bar could have reached " +
    "both the stop and the target the worse is assumed. Moving a stop or target takes effect from the next bar."],
  ["Closing", "A close request exits at the next bar's open and is listed as closed by you, even when you finish the " +
    "chart on that bar. Finishing a chart closes the other open trades at the next bar's open, or at the last close " +
    "when the replay has run out, and orders still waiting expire unfilled."],
  ["R", "Each trade is sized when it is placed. Risk is the distance from the planned entry to the stop in effect " +
    "when the order fills: the planned entry is the close a market order was placed at, or the entry price of a " +
    `limit or stop entry, and the risk is at least ${fmtPct(MIN_RISK * 100, 1, false)} of it. Moving the stop after ` +
    "the fill doesn't change it. R is the profit or loss from the fill divided by that risk, so a stop-out filled " +
    `at the planned price is ${MINUS}1R, and a fill better or worse than planned, on a gap, shows up as more or less ` +
    "R, the way slippage does. A win is a trade with R above zero. Net R takes off costs of " +
    `${fmtPct(COST * 100, 2, false)} of the price on each fill, in and out. Move is the price change from fill to ` +
    "exit in the trade's direction."],
  ["Dollars", `Each trade risks 1% of a $100,000 account, so 1R is $${fmtPrice(DOLLARS_PER_R, 0)}. Results add up ` +
    "trade by trade, with no compounding."],
  ["Random entries", "Each closed trade is re-entered as a market order on a random bar of the same chart's replay: " +
    "planned at the previous bar's close with the same side and the same stop and target distances as a share of " +
    "the planned entry, filled at the bar's open, and walked forward by the same rules to the end of the replay " +
    "(closing at the last close if neither level is hit). Only bars where that order fills are drawn. A " +
    `random-entry run re-enters every closed trade once; "ahead of 60% of random-entry runs" means 60% of ` +
    `${DRAWS.toLocaleString("en-US")} runs ended with a lower total R than yours.`],
  ["Scoring", "Only finished charts are scored. Trades closed on charts you didn't finish are shown beside the " +
    "score, not in it; those charts stay hidden and are listed with their progress only."],
  ["Guesses", "Before a chart is revealed you can guess its era, the decade its replay starts in, and whether it is " +
    "a stock or an ETF. Guesses are counted in the chart-by-chart table and are not part of the score."],
];

// ---------------------------------------------------------------- the document

// The site's mark: a cup with handle and its breakout.
const MARK = '<svg class="tt-mark" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 6c1 8.5 3.8 12 7.6 12 ' +
  '3.9 0 5.6-4.6 6.2-9.2l1.9 2.6L21 4.8" fill="none" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round" stroke-linejoin="round"/><circle cx="21" cy="4.8" r="2.1" fill="#26a69a"/></svg>';

// The site's dark slate on screen; plain black on white on paper, one chart to a page.
const CSS = `
:root { --bg: #131722; --panel: #1e222d; --raised: #262a35; --line: #2a2e39; --line-2: #363a45;
  --row: rgba(42, 46, 57, 0.6); --text: #d1d4dc; --bright: #f0f3fa; --muted: #9598a1; --link: #5b8cff;
  --grid: #232733; --green: #26a69a; --green-text: #3cc4b5; --red: #ef5350; --red-text: #ff7f7c;
  --font: Inter, -apple-system, "Segoe UI", system-ui, sans-serif; color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font: 400 14px/1.45 var(--font); font-feature-settings: "tnum" 1, "cv11" 1; font-variant-numeric: tabular-nums;
  -webkit-font-smoothing: antialiased; }
::selection { background: rgba(41, 98, 255, 0.16); color: var(--bright); }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.tt-page { max-width: 1160px; margin: 0 auto; padding: 20px 20px 36px; }
.tt-brand { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--bright); }
.tt-brand span { font-weight: 400; color: var(--muted); }
.tt-mark { width: 20px; height: 20px; color: var(--text); }
h1 { margin: 18px 0 4px; font-size: 24px; font-weight: 600; line-height: 1.2; letter-spacing: -0.01em;
  color: var(--bright); }
h2 { margin: 0; font-size: 15px; font-weight: 600; line-height: 1.35; color: var(--bright); }
h3 { margin: 18px 0 8px; font-size: 13px; font-weight: 600; color: var(--bright); }
.tt-meta { margin: 0; font-size: 12px; color: var(--muted); }
.tt-meta b { font-weight: 500; color: var(--text); }
.tt-notice { display: flex; align-items: baseline; gap: 10px; margin: 12px 0 0; padding: 9px 12px; border-radius: 6px;
  background: var(--raised); font-size: 13px; }
.tt-notice i { flex: none; width: 7px; height: 7px; border-radius: 50%; background: #5b8cff;
  transform: translateY(-1px); }
.tt-notice b { font-weight: 500; color: var(--bright); }
.tt-notice span { color: var(--muted); }
.tt-card { margin-top: 12px; padding: 14px 16px 16px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 6px; }
.tt-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 2px 12px;
  margin-bottom: 12px; }
.tt-head span { font-size: 12px; color: var(--muted); }
.tt-figures { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px 16px; }
.tt-figure { display: grid; align-content: start; gap: 2px; min-width: 0; }
.tt-figure span { font-size: 12px; color: var(--muted); }
.tt-figure b { font-size: 20px; font-weight: 600; line-height: 1.3; letter-spacing: -0.01em; color: var(--bright);
  white-space: nowrap; }
.tt-figure b small { font-size: 13px; font-weight: 500; letter-spacing: 0; color: var(--muted); }
.tt-dollars { margin: 14px 0 0; padding-top: 12px; border-top: 1px solid var(--line); font-size: 13px; }
.tt-dollars b { font-weight: 600; color: var(--bright); }
.tt-also { margin: 8px 0 0; font-size: 12px; color: var(--muted); }
.tt-also span { font-weight: 600; }
.tt-compare { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
.tt-cmp { display: grid; align-content: start; gap: 4px; padding: 14px 16px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; }
.tt-cmp > span { font-size: 12px; color: var(--muted); }
.tt-cmp > b { font-size: 17px; font-weight: 600; line-height: 1.35; color: var(--bright); }
.tt-cmp p { max-width: 72ch; margin: 2px 0 0; font-size: 12px; line-height: 1.55; color: var(--muted); }
.tt-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 12px; font-size: 12px; color: var(--muted); }
.tt-legend span { display: inline-flex; align-items: center; gap: 7px; }
.tt-legend b { font-weight: 600; color: var(--bright); }
.tt-key-line { width: 18px; height: 2px; border-radius: 1px; background: var(--text); }
.tt-key-dash { width: 18px; height: 0; border-top: 1.5px dashed var(--muted); }
.tt-key-dot { width: 8px; height: 8px; border-radius: 50%; }
.tt-plot { position: relative; height: 240px; margin: 4px 52px calc(28px + var(--xw, 0) * 15px) 0; }
.tt-plot svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.tt-plot line, .tt-plot path { vector-effect: non-scaling-stroke; fill: none; }
.tt-grid { stroke: var(--grid); stroke-width: 1; }
.tt-zero { stroke: var(--line-2); stroke-width: 1; }
.tt-divider { stroke: var(--line-2); stroke-width: 1; stroke-dasharray: 3 3; }
.tt-random { stroke: var(--muted); stroke-width: 1.5; stroke-dasharray: 5 4; }
.tt-line { stroke: var(--text); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.tt-pt { position: absolute; width: 10px; height: 10px; margin: -5px 0 0 -5px; border: 2px solid var(--panel);
  border-radius: 50%; transition: transform 120ms ease-out; }
.tt-pt:hover { transform: scale(1.5); }
.tt-pt-up { background: var(--green); }
.tt-pt-down { background: var(--red); }
.tt-pt-flat { background: var(--muted); }
.tt-y { position: absolute; left: calc(100% + 8px); transform: translateY(-50%); font-size: 11px; color: var(--muted);
  white-space: nowrap; }
.tt-x { --x-row: var(--rw, 0); --x-word: var(--ww, inline); position: absolute;
  top: calc(100% + 8px + var(--x-row) * 15px); transform: translateX(-50%); font-size: 11px; line-height: 15px;
  color: var(--muted); white-space: nowrap; }
.tt-x::before { content: ""; position: absolute; left: 50%; bottom: 100%;
  height: calc(var(--x-row) * 15px + min(var(--x-row), 1) * 6px); border-left: 1px solid var(--line-2); }
.tt-x-word { display: var(--x-word); }
.tt-trio { display: grid; grid-template-columns: 1.25fr 1fr 1fr; gap: 12px 16px; align-items: start; margin-top: 4px; }
.tt-trio h3 { margin-top: 12px; }
.tt-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }
.tt-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.tt-table th { padding: 7px 10px; border-bottom: 1px solid var(--line); background: var(--panel); text-align: left;
  font-weight: 500; color: var(--muted); white-space: nowrap; }
.tt-table td { padding: 6px 10px; border-bottom: 1px solid var(--row); color: var(--text); white-space: nowrap;
  vertical-align: top; }
.tt-table tr:last-child td { border-bottom: 0; }
.tt-table .tt-num { text-align: right; }
.tt-table td small { display: block; margin-top: 1px; font-size: 11px; line-height: 1.35; color: var(--muted); }
.tt-table td b { font-weight: 600; color: var(--bright); }
.tt-table tr.tt-total td { border-top: 1px solid var(--line-2); color: var(--bright); font-weight: 500; }
.tt-table .tt-note { min-width: 150px; max-width: 300px; white-space: pre-line; overflow-wrap: anywhere; }
.tt-overview td:first-child a { font-weight: 500; }
.tt-open td { color: var(--muted); }
.tt-table .tt-open td small { display: inline; margin-left: 8px; }
.tt-pending { color: var(--text); }
.tt-real, .tt-muted { color: var(--muted); }
.tt-swatch { display: inline-block; width: 8px; height: 8px; margin-right: 8px; border-radius: 50%;
  vertical-align: 1px; }
.tt-foot { margin: 8px 0 0; font-size: 12px; line-height: 1.5; color: var(--muted); }
.tt-empty { margin: 0; font-size: 13px; color: var(--muted); }
.tt-empty.tt-above { margin-bottom: 8px; }
.tt-chart { margin-top: 16px; }
.tt-chart h2 { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 17px; }
.tt-sym { font-weight: 600; color: var(--muted); }
.tt-tag { padding: 1px 6px; border: 1px solid var(--line-2); border-radius: 4px; font-size: 11px; font-weight: 500;
  color: var(--muted); }
.tt-reveal { max-width: 90ch; margin: 8px 0 0; font-size: 13px; line-height: 1.55; }
.tt-guess { margin: 4px 0 0; font-size: 13px; line-height: 1.55; }
.tt-guess span { color: var(--muted); }
.tt-context { max-width: 90ch; margin: 2px 0 0; font-size: 12px; line-height: 1.55; color: var(--muted); }
.tt-stats { display: flex; flex-wrap: wrap; gap: 10px 28px; margin-top: 14px; }
.tt-stats div { display: grid; gap: 1px; }
.tt-stats span { font-size: 12px; color: var(--muted); }
.tt-stats b { font-size: 15px; font-weight: 600; color: var(--bright); }
.tt-shot { margin: 14px 0 0; }
.tt-shot img { display: block; width: 100%; height: auto; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); }
.tt-rules { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 8px 16px; margin: 0; font-size: 13px;
  line-height: 1.55; }
.tt-rules dt { font-weight: 500; color: var(--bright); }
.tt-rules dd { max-width: 100ch; margin: 0; }
.tt-disclaimer { margin: 20px 0 0; padding-top: 12px; border-top: 1px solid var(--line); font-size: 12px;
  color: var(--muted); }
.tt-page .tt-up { color: var(--green-text); }
.tt-page .tt-down { color: var(--red-text); }
@media (max-width: 960px) {
  .tt-figures { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .tt-trio { grid-template-columns: minmax(0, 1fr); }
  .tt-plot { margin-bottom: calc(28px + var(--xm, 0) * 15px); }
  .tt-x { --x-row: var(--rm, 0); --x-word: var(--wm, inline); }
}
@media (max-width: 720px) {
  .tt-compare { grid-template-columns: minmax(0, 1fr); }
  .tt-rules { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .tt-rules dd { margin-bottom: 8px; }
}
@media (max-width: 560px) {
  .tt-page { padding: 16px 16px 28px; }
  .tt-figures { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .tt-plot { height: 200px; margin-right: 44px; margin-bottom: calc(28px + var(--xn, 0) * 15px); }
  .tt-x { --x-row: var(--rn, 0); --x-word: var(--wn, inline); }
  .tt-pt { width: 8px; height: 8px; margin: -4px 0 0 -4px; border-width: 1.5px; }
  .tt-stats { gap: 10px 20px; }
  .tt-table th, .tt-table td { padding-inline: 8px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
@media print {
  :root { --bg: #fff; --panel: #fff; --raised: #f1f2f4; --line: #d6d9df; --line-2: #c2c6ce; --row: #e6e8ec;
    --text: #1f232b; --bright: #000; --muted: #5c626e; --grid: #eceef2; --link: #1f232b; --green-text: #00796b;
    --red-text: #c62828; color-scheme: light; }
  @page { margin: 14mm 12mm; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { font-size: 11px; }
  .tt-page { max-width: none; padding: 0; }
  h1 { margin-top: 10px; }
  .tt-card, .tt-cmp { padding: 0; border: 0; background: none; }
  .tt-card { margin-top: 20px; }
  .tt-notice { border: 1px solid var(--line); }
  .tt-figures { grid-template-columns: repeat(5, minmax(0, 1fr)); }
  .tt-compare { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; margin-top: 18px; }
  .tt-trio { grid-template-columns: 1.25fr 1fr 1fr; }
  .tt-rules { grid-template-columns: 130px minmax(0, 1fr); gap: 6px 14px; font-size: 11px; }
  .tt-wrap { overflow: visible; }
  .tt-table { font-size: 9.5px; }
  .tt-table th, .tt-table td { padding: 4px 5px; }
  .tt-table td small { font-size: 9px; }
  .tt-table .tt-note { min-width: 72px; max-width: 160px; }
  .tt-trades .tt-result small { white-space: normal; }
  .tt-sep { display: none; }
  .tt-date { display: block; }
  .tt-pt { border-color: #fff; }
  .tt-swatch { box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.35); }
  .tt-chart { break-before: page; margin-top: 0; }
  .tt-figures, .tt-compare, .tt-plot, .tt-legend, .tt-shot, .tt-stats, tr { break-inside: avoid; }
  h2, h3 { break-after: avoid; }
  thead { display: table-header-group; }
}
`;

function notice(done, total) {
  if (!done) {
    return '<p class="tt-notice"><i></i><span><b>No charts finished yet.</b> Finish a chart on the TradeTest page to ' +
      "score it and reveal what it was, then download the report again.</span></p>";
  }
  if (done === total) return "";
  return `<p class="tt-notice"><i></i><span><b>${plural(total - done, "chart")} not finished.</b> They are not ` +
    "scored and stay hidden; finish them on the TradeTest page and download the report again to include them." +
    "</span></p>";
}

const card = (title, aside, body) =>
  `<section class="tt-card"><div class="tt-head"><h2>${title}</h2>${aside ? `<span>${aside}</span>` : ""}</div>` +
  `${body}</section>`;

// Whether a chart not scored has its rows up to its current bar, so its closed trades can be counted.
const rowsIn = (chart) => Array.isArray(chart.bars) && num(chart.nCtx) && num(chart.k)
  && chart.bars.length >= chart.nCtx + chart.k;

// What the report scores: the finished charts as sim.js reads them (null for the rest), their summary and random
// baseline, the trades closed on the charts it doesn't score, and how many of those had trades but no rows (missing:
// { count, unfinished: true when none of them is flagged finished }).
function scoring(state) {
  const raw = Array.isArray(state?.charts) ? state.charts : [];
  const charts = raw.map(scoredChart), results = scoredResults(charts);
  // A chart the report doesn't score counts as unfinished here, whatever its own flag says.
  const unscored = raw.map((chart, index) => (charts[index] || !chart ? null : { ...chart, finished: false }));
  const also = unfinishedClosed(unscored.map((chart) => (chart && rowsIn(chart) ? chart : null)));
  const lost = raw.filter((chart, index) => unscored[index] && !rowsIn(unscored[index])
    && Array.isArray(chart.trades) && chart.trades.length);
  const missing = { count: lost.length, unfinished: lost.every((chart) => chart.finished !== true) };
  return { raw, charts, results, sum: summarize(results), base: randomBaseline(charts, { draws: DRAWS }), also,
           missing, done: charts.filter(Boolean).length };
}

export function resultLine(state) {
  const { sum, base, also, missing, done } = scoring(state);
  const score = !done ? "no charts finished yet"
    : !sum.trades ? `no closed trades on ${plural(done, "chart")}`
    : `${fmtR(sum.totalR)} on ${plural(done, "chart")}, ${share(sum.winRate)} won` +
      (base.trades ? `, ${ahead(base)}` : "");
  const notes = [also.trades && `${done ? "also " : ""}${plural(also.trades, "trade")} closed on unfinished charts: ` +
    fmtR(also.totalR), missing.count && notLoaded(missing, !!also.trades)].filter(Boolean);
  return `TradeTest: ${score}${notes.length ? ` (${notes.join("; ")})` : ""}. ${SITE}`;
}

export function buildReport(state, { renderImage, generatedAt } = {}) {
  state = state && typeof state === "object" ? state : {};
  const { raw, charts, results, sum, base, also, missing, done } = scoring(state), when = stamp(generatedAt);
  const set = state.set == null ? "" : String(state.set);
  const { holds, html: scorecard } = overview(raw, charts);
  const meta = [when && `Generated <b>${when}</b>`, set && `Set <b>${esc(set)}</b>`,
                `<b>${done}</b> of ${plural(raw.length, "chart")} finished`,
                `<b>${sum.trades}</b> closed ${sum.trades === 1 ? "trade" : "trades"}`].filter(Boolean).join(" · ");
  const sections = raw.map((chart, index) => (charts[index] ? chartSection(chart, charts[index], index, renderImage)
    : ""));
  const rules = RULES.map(([term, text]) => `<dt>${term}</dt><dd>${text}</dd>`).join("");

  return "<!doctype html>\n" +
    '<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<meta name="color-scheme" content="dark light">' +
    `<title>TradeTest report${set ? ` · set ${esc(set)}` : ""}</title>` +
    `<style>${CSS}</style></head><body><main class="tt-page">` +
    `<header><div class="tt-brand">${MARK}StratLib <span>TradeTest</span></div><h1>TradeTest report</h1>` +
    `<p class="tt-meta">${meta}</p>${notice(done, raw.length)}</header>` +
    card("Your score", "Closed trades on finished charts", figures(sum) + alsoClosed(also, missing)) +
    comparisons(sum, base, holds) +
    card("Cumulative R, trade by trade", "Closed trades in the order they closed, chart by chart",
         curve(results, base) + (sum.trades ? measures(sum, results, charts) : "")) +
    card("Chart by chart", "Buy and hold runs from the last close before the replay to its last close", scorecard) +
    sections.join("") +
    card("How trades were scored", "", `<dl class="tt-rules">${rules}</dl>`) +
    `<p class="tt-disclaimer">${esc(DISCLAIMER)} Made on <a href="${SITE}">StratLib TradeTest</a>.</p>` +
    "</main></body></html>\n";
}

// ---------------------------------------------------------------- CSV

export function csvField(value) {
  const text = value == null ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

// A spreadsheet runs a cell starting with = + - @ as a formula, so text cells that do get a leading apostrophe.
function textCell(value) {
  const text = value == null ? "" : String(value);
  return /^[=+\-@\t\r]/.test(text) ? "'" + text : text;
}

// Plain machine numbers: an ASCII minus, no grouping, never "-0.000".
function csvNumber(x, digits) {
  if (!num(x)) return "";
  const text = x.toFixed(digits);
  return Number(text) === 0 ? (0).toFixed(digits) : text;
}

// Real prices keep up to four decimals, at least two: 39.40, 40.6125.
const csvPrice = (x) => (num(x) ? x.toFixed(4).replace(/(\.\d\d\d*?)0+$/, "$1") : "");

// Planned entry is where risk is measured from (the close a market order was placed at); R = (exit - fill) / risk,
// signed by side.
const CSV_COLUMNS = ["Chart", "Symbol", "Trade", "Side", "Order", "Status", "Placed", "Filled", "Exited", "Entry, USD",
  "Planned entry, USD", "Stop, USD", "Target, USD", "Final stop, USD", "Final target, USD", "Fill, USD", "Exit, USD",
  "Exit reason", "Risk, USD", "R", "Net R", "Move, %", "Bars held", "Note"];

export function buildTradesCsv(state) {
  const raw = Array.isArray(state?.charts) ? state.charts : [];
  const lines = [CSV_COLUMNS.map(csvField).join(",")];
  raw.forEach((rawChart, index) => {
    const chart = scoredChart(rawChart);
    if (!chart) return;
    const { symbol } = identity(chart), price = (p) => csvPrice(realPrice(chart, p));
    chartSummary(chart).results.forEach((res, n) => {
      const trade = chart.trades[n];
      const reason = res.status === "closed" ? pick(EXITS, res.exit.reason, [""])[0]
        : res.status === "missed" ? `Missed, ${pick(MISSES, res.missed.reason, "gapped")}` : "";
      const row = [index + 1, textCell(symbol), trade.id, pick(SIDES, trade.side, ""), pick(KINDS, res.kind, ""),
        res.status.charAt(0).toUpperCase() + res.status.slice(1), isoAt(chart, chart.nCtx + trade.placedAt - 1) || "",
        (res.fill && isoAt(chart, res.fill.i)) || "", (res.exit && isoAt(chart, res.exit.i)) || "",
        trade.entry == null ? "" : price(trade.entry), price(res.planned), price(trade.stop), price(trade.target),
        price(res.stopNow), price(res.targetNow), res.fill ? price(res.fill.price) : "",
        res.exit ? price(res.exit.price) : "", reason, price(res.risk), csvNumber(res.r, 3), csvNumber(res.rNet, 3),
        csvNumber(res.pct, 2), res.barsHeld ?? "", textCell(trade.note)];
      lines.push(row.map(csvField).join(","));
    });
  });
  return lines.join("\r\n") + "\r\n";
}
