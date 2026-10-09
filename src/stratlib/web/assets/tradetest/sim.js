// TradeTest trade simulation: order kinds, fills, exits, R, chart and set statistics, and the random-entry
// baseline. The page and the report both score trades here and nowhere else. Pure and deterministic: no DOM, no
// imports, no Math.random, so the same file runs in the browser and under `node --test`.
//
// API reference (prices are chart prices; i is a bar index 0..nCtx+nReplay-1; k counts revealed replay bars, and
// after k of them the current bar is index nCtx + k - 1)
//
//   A chart is the page's saved chart state: { bars: [[o, h, l, c, v, ...]], nCtx, nReplay, k, finished, finishedAt,
//   trades }. A trade is { id, side: "long"|"short", kind: "market"|"limit"|"stop", entry (null for market), stop,
//   target, placedAt: k, changes: [{ at: k, stop, target }], cancelAt: k|null, closeAt: k|null }.
//
//   COST = 0.001            cost per fill, as a share of price (0.10%)
//   MIN_RISK = 0.001        the stop must sit at least 0.1% of the entry away; also the floor on risk
//   DOLLARS_PER_R = 1000    1% risk per trade on a $100,000 account
//   sign(side) -> 1 | -1
//   kindFor(side, entry, ref) -> "market" | "limit" | "stop"
//       ref is the current close; an entry of null, or exactly ref, is "market".
//   refClose(chart, k = chart.k) -> number | null      close of index nCtx + k - 1: an order placed at k uses it
//   scopeEnd(chart) -> i     last bar walked: the current bar, or on a finished chart the bar before the finish point
//   levelsAt(trade, i, nCtx) -> { stop, target }       the stop and target in effect at bar i
//   validateOrder({ side, entry, stop, target }, ref) -> null | message
//   validateChange(trade, sim, { stop, target }, currentClose) -> null | message    sim = simulate(trade, chart)
//       The caller records an accepted change as { at: chart.k, stop, target }; it acts from the next bar.
//   simulate(trade, chart) -> {
//       id, side, kind,
//       status: "pending" | "open" | "closing" | "closed" | "cancelled" | "missed" | "expired",
//       planned,                             the planned entry: the close a market order was placed at (refClose at
//                                            placedAt), the entry of a limit or stop entry; null if not available
//       fill: { i, price } | null,
//       exit: { i, price, reason: "stop" | "target" | "close" | "finish" | "end" } | null,
//                                            "close" is a close the visitor asked for, kept even when they finished
//                                            the chart on the bar they asked; "finish" a position the finish closed
//       missed: { i, reason: "gap_stop" | "gap_target" } | null,
//       cancelled: { i } | null,             i = nCtx + cancelAt, the first bar the order no longer worked
//       stopNow, targetNow,                  the levels in effect now (a change made at the current k included), or
//                                            on the bar the trade ended
//       risk, r, rNet, pct, barsHeld, mark } all null until filled. risk = the planned entry to the stop in effect on
//                                            the fill bar, at least 0.1% of the planned entry; r = (mark - fill) *
//                                            side / risk, so a fill better or worse than planned (a gap) is more or
//                                            less R, as slippage is on a position sized before the fill; mark = the
//                                            exit price, or the current close while open; pct is a percent (2.4 means
//                                            +2.4%) from the fill
//   chartSummary(chart) -> { trades, closed, wins, losses, totalR, totalRNet, open, closing, openR, pending,
//       cancelled, missed, expired, results }
//       A win is a closed trade with R > 0, a loss R < 0. open counts open and closing trades, openR their R.
//       results[n] = simulate(chart.trades[n]).
//   scoredResults(charts) -> [simulate() output + { chart: index }]   closed trades on finished charts, in order
//   unfinishedClosed(charts) -> { trades, totalR, totalRNet }
//       Closed trades on charts not finished yet: kept out of the score (the random baseline needs the whole
//       replay), and shown beside it so a set can't be scored on its good charts only.
//   summarize(results) -> { trades, wins, losses, flat, winRate, totalR, meanR, seR, medianR, bestR, worstR,
//       profitFactor, grossWinR, grossLossR, avgWinR, avgLossR, avgBarsHeld, longs, shorts, totalRNet, meanRNet,
//       dollars, dollarsNet }
//       Only closed results count. winRate is a share (0.55 means 55%). seR is the standard error of the mean R
//       (null below two trades). longs/shorts = { trades, wins, totalR, meanR }. profitFactor = grossWinR /
//       grossLossR, null when no trade lost (grossLossR is then 0). avgLossR is negative. dollars = totalR * 1000.
//       Empty input gives 0 for counts and totals and null for the rest; never NaN.
//   buyAndHold(chart) -> percent | null     last context close to last replay close; null until all bars are in
//   randomBaseline(charts, { draws = 2000, seed = 1 }) -> { meanR, sdTotal, totalMean, percentile, draws, trades,
//       total }
//       Each closed trade on a finished chart is re-entered as a market order on a random replay bar j, planned at
//       the close of bar j - 1 with the same side and the same stop and target distances as shares of the planned
//       entry, filled at bar j's open and scored by the rules above. Only bars where that order fills are drawn (a
//       trade with none adds 0R). total = the visitor's total R over those trades; totalMean and sdTotal describe
//       the draws' totals; meanR is per trade; percentile is the share of draws whose total is below the visitor's
//       (0.93 means beat 93%). No closed trades on finished charts: nulls, draws 0, trades 0, total 0.
//   mulberry32(seed) -> () => number in [0, 1)
//   fmtR(x, digits = 2) -> "+1.25R" | "\u22120.50R" | "0.00R"
//   fmtPct(x, digits = 2, signed = true) -> "+2.40%" | "\u22121.10%" | "55%" (signed false)
//   fmtPrice(x, digits = 2) -> "1,234.50"
//       Negatives use the true minus sign (U+2212); a value that rounds to zero has no sign; anything that is not a
//       finite number formats as an en dash.

const O = 0, H = 1, L = 2, C = 3;

export const COST = 0.001;
export const MIN_RISK = 0.001;
export const DOLLARS_PER_R = 1000;
const MINUS = String.fromCharCode(0x2212);   // true minus sign
const NONE = String.fromCharCode(0x2013);    // en dash for a missing value
// Totals of R are sums of quotients, so two equal outcomes can differ in the last bit; a draw only counts as beaten
// when it is below the visitor's total by more than this.
const R_TOLERANCE = 1e-9;

export function sign(side) {
  return side === "short" ? -1 : 1;
}

// Side-relative comparisons, so each rule is written once: for a long "under" is <=, for a short it is >=.
const under = (a, b, s) => (s > 0 ? a <= b : a >= b);
const over = (a, b, s) => (s > 0 ? a >= b : a <= b);
const lower = (a, b, s) => (s > 0 ? Math.min(a, b) : Math.max(a, b));
const higher = (a, b, s) => (s > 0 ? Math.max(a, b) : Math.min(a, b));
// The bar's worst and best prices for a holder of the side.
const worst = (bar, s) => (s > 0 ? bar[L] : bar[H]);
const best = (bar, s) => (s > 0 ? bar[H] : bar[L]);

const isPrice = (x) => typeof x === "number" && Number.isFinite(x) && x > 0;

// ---------------------------------------------------------------- placement

export function kindFor(side, entry, ref) {
  if (entry == null || entry === ref) return "market";
  return (entry - ref) * sign(side) < 0 ? "limit" : "stop";
}

export function refClose(chart, k = chart.k) {
  const bar = chart.bars[chart.nCtx + k - 1];
  return bar ? bar[C] : null;
}

// The stop sits far enough from the entry. The threshold is relative, with room for the last bit of rounding, so a
// stop typed exactly 0.1% away passes.
function farEnough(entry, stop) {
  return Math.abs(entry - stop) >= MIN_RISK * entry * (1 - 1e-9);
}

// The stop strictly on the losing side of the price p and the target strictly on the winning side.
function bracketMessage(s, p, stop, target, against) {
  const long = s > 0, side = long ? "a long" : "a short";
  if (under(stop, p, s) && stop !== p) {
    if (over(target, p, s) && target !== p) return null;
    return `The target must be ${long ? "above" : "below"} ${against} for ${side}.`;
  }
  return `The stop must be ${long ? "below" : "above"} ${against} for ${side}.`;
}

function priceMessage(stop, target) {
  if (!isPrice(stop)) return "Enter a stop price.";
  if (!isPrice(target)) return "Enter a target price.";
  return null;
}

export function validateOrder({ side, entry, stop, target }, ref) {
  if (side !== "long" && side !== "short") return "Choose long or short.";
  if (entry != null && !isPrice(entry)) return "Enter an entry price, or choose the market.";
  const e = entry == null ? ref : entry;
  if (!isPrice(e)) return "The current price is not available.";
  const s = sign(side);
  return priceMessage(stop, target) || bracketMessage(s, e, stop, target, "the entry")
    || (farEnough(e, stop) ? null : "The stop must be at least 0.1% away from the entry.");
}

// A change made now, at the current k. An open trade keeps its stop and target either side of the current close, a
// pending limit or stop order either side of its entry, and a pending market order either side of the close it was
// placed at. Risk is fixed once a trade fills, so only a pending order's stop must stay 0.1% away.
export function validateChange(trade, sim, { stop, target }, currentClose) {
  if (sim.status === "closing") return "This trade closes at the next open and can't be changed.";
  if (sim.status !== "pending" && sim.status !== "open") return "Only pending orders and open trades can be changed.";
  const bad = priceMessage(stop, target);
  if (bad) return bad;
  const s = sign(trade.side), e = sim.status === "open" || trade.kind === "market" ? currentClose : trade.entry;
  if (!isPrice(e)) return "The current price is not available.";
  if (sim.status === "open") return bracketMessage(s, e, stop, target, "the current price");
  return bracketMessage(s, e, stop, target, "the entry")
    || (farEnough(e, stop) ? null : "The stop must be at least 0.1% away from the entry.");
}

// ---------------------------------------------------------------- walking the bars

export function scopeEnd(chart) {
  return Math.min(chart.nCtx + finishPoint(chart) - 1, chart.bars.length - 1);
}

// k for an unfinished chart; the k it was finished at otherwise.
function finishPoint(chart) {
  return chart.finished ? chart.finishedAt ?? chart.k : chart.k;
}

export function levelsAt(trade, i, nCtx) {
  let stop = trade.stop, target = trade.target;
  for (const change of trade.changes || []) {
    if (nCtx + change.at > i) continue;
    if (change.stop != null) stop = change.stop;
    if (change.target != null) target = change.target;
  }
  return { stop, target };
}

// A pending order on one bar: { price } when it fills, { missed: reason } when a gap kills it, {} while it waits.
// A market order always resolves on its first bar.
function entryOn(kind, entry, bar, { stop, target }, s) {
  const open = bar[O];
  if (kind === "market") {
    if (under(open, stop, s)) return { missed: "gap_stop" };
    if (over(open, target, s)) return { missed: "gap_target" };
    return { price: open };
  }
  if (kind === "limit") {
    if (under(open, stop, s)) return { missed: "gap_stop" };
    return under(worst(bar, s), entry, s) ? { price: lower(open, entry, s) } : {};
  }
  if (over(open, target, s)) return { missed: "gap_target" };
  return over(best(bar, s), entry, s) ? { price: higher(open, entry, s) } : {};
}

// On the fill bar only the stop can exit: when the bar could have hit both, the worse is assumed.
function fillBarExit(bar, i, { stop }, s) {
  return under(worst(bar, s), stop, s) ? { i, price: stop, reason: "stop" } : null;
}

// An open position on a later bar. A gap through a level exits at the open, which can be worse than the stop
// (more than 1R lost) or better than the target; within the bar the stop is checked before the target.
function exitOn(bar, i, { stop, target }, s, closing) {
  const open = bar[O];
  if (closing) return { i, price: open, reason: "close" };
  if (under(open, stop, s)) return { i, price: open, reason: "stop" };
  if (over(open, target, s)) return { i, price: open, reason: "target" };
  if (under(worst(bar, s), stop, s)) return { i, price: stop, reason: "stop" };
  if (over(best(bar, s), target, s)) return { i, price: target, reason: "target" };
  return null;
}

// A position still open when the chart was finished at k = at leaves at the next bar's open, or at the last close
// when the replay had run out. null when that bar is not loaded yet. A close the visitor asked for on the finish bar
// leaves at the same open and keeps its reason, "close".
function finishExit(chart, at, closeFrom) {
  const { bars, nCtx, nReplay } = chart;
  if (at >= nReplay) {
    const last = nCtx + nReplay - 1;
    return bars[last] ? { i: last, price: bars[last][C], reason: "end" } : null;
  }
  const i = nCtx + at;
  return bars[i] ? { i, price: bars[i][O], reason: i >= closeFrom ? "close" : "finish" } : null;
}

export function simulate(trade, chart) {
  const { bars, nCtx } = chart, s = sign(trade.side);
  const kind = trade.kind || kindFor(trade.side, trade.entry, refClose(chart, trade.placedAt));
  const at = finishPoint(chart), end = scopeEnd(chart);
  const cancelFrom = trade.cancelAt == null ? Infinity : nCtx + trade.cancelAt;
  // A close request at k exits at the open of bar nCtx + k; ">=" also covers a request made before the fill.
  const closeFrom = trade.closeAt == null ? Infinity : nCtx + trade.closeAt;
  let fill = null, exit = null, missed = null;
  for (let i = nCtx + trade.placedAt; i <= end && !exit && !missed; i++) {
    const bar = bars[i], levels = levelsAt(trade, i, nCtx);
    if (fill) { exit = exitOn(bar, i, levels, s, i >= closeFrom); continue; }
    if (i >= cancelFrom) break;
    const step = entryOn(kind, trade.entry, bar, levels, s);
    if (step.missed) missed = { i, reason: step.missed };
    else if (step.price != null) { fill = { i, price: step.price }; exit = fillBarExit(bar, i, levels, s); }
  }

  let status, cancelled = null;
  if (missed) status = "missed";
  else if (!fill && trade.cancelAt != null) { status = "cancelled"; cancelled = { i: cancelFrom }; }
  else if (!fill) status = chart.finished ? "expired" : "pending";
  else if (exit) status = "closed";
  else if (chart.finished) { exit = finishExit(chart, at, closeFrom); status = exit ? "closed" : "closing"; }
  else status = trade.closeAt != null ? "closing" : "open";

  const now = exit ? exit.i : missed ? missed.i : cancelled ? cancelled.i : nCtx + at;
  const { stop: stopNow, target: targetNow } = levelsAt(trade, now, nCtx);
  const planned = kind === "market" ? refClose(chart, trade.placedAt) : trade.entry;
  const out = { id: trade.id, side: trade.side, kind, status, planned: isPrice(planned) ? planned : null, fill, exit,
                missed, cancelled, stopNow, targetNow, risk: null, r: null, rNet: null, pct: null, barsHeld: null,
                mark: null };
  if (!fill) return out;
  // The position is sized when the order is placed, as a real one is: risk runs from the planned entry to the stop in
  // effect on the fill bar (the stop as placed, unless the order was edited while it waited), and a fill better or
  // worse than planned shows up as more or less R. Later stop moves don't change it.
  const markIndex = exit ? exit.i : end, mark = exit ? exit.price : bars[end][C], basis = out.planned ?? fill.price;
  out.risk = Math.max(Math.abs(basis - levelsAt(trade, fill.i, nCtx).stop), MIN_RISK * basis);
  out.r = (mark - fill.price) * s / out.risk;
  out.rNet = out.r - COST * (fill.price + mark) / out.risk;
  out.pct = 100 * (mark / fill.price - 1) * s;
  out.barsHeld = markIndex - fill.i;
  out.mark = mark;
  return out;
}

// ---------------------------------------------------------------- statistics

export function chartSummary(chart) {
  const results = (chart.trades || []).map((trade) => simulate(trade, chart));
  const out = { trades: results.length, closed: 0, wins: 0, losses: 0, totalR: 0, totalRNet: 0, open: 0, closing: 0,
                openR: 0, pending: 0, cancelled: 0, missed: 0, expired: 0, results };
  for (const res of results) {
    if (res.status === "closed") {
      out.closed += 1;
      out.wins += res.r > 0 ? 1 : 0;
      out.losses += res.r < 0 ? 1 : 0;
      out.totalR += res.r;
      out.totalRNet += res.rNet;
    } else if (res.status === "open" || res.status === "closing") {
      out.open += 1;
      out.closing += res.status === "closing" ? 1 : 0;
      out.openR += res.r;
    } else out[res.status] += 1;
  }
  return out;
}

export function scoredResults(charts) {
  const out = [];
  charts.forEach((chart, index) => {
    if (!chart || !chart.finished) return;
    for (const res of chartSummary(chart).results) if (res.status === "closed") out.push({ ...res, chart: index });
  });
  return out;
}

export function unfinishedClosed(charts) {
  const out = { trades: 0, totalR: 0, totalRNet: 0 };
  for (const chart of charts || []) {
    if (!chart || chart.finished || !Array.isArray(chart.bars)) continue;
    const s = chartSummary(chart);
    out.trades += s.closed;
    out.totalR += s.totalR;
    out.totalRNet += s.totalRNet;
  }
  return out;
}

const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const meanOf = (xs) => (xs.length ? sum(xs) / xs.length : null);

// Sample standard deviation; null below two values.
function sdOf(xs) {
  if (xs.length < 2) return null;
  const m = sum(xs) / xs.length;
  return Math.sqrt(sum(xs.map((x) => (x - m) ** 2)) / (xs.length - 1));
}

function medianOf(xs) {
  if (!xs.length) return null;
  const sorted = [...xs].sort((a, b) => a - b), mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function sideStats(results, side) {
  const rs = results.filter((res) => res.side === side).map((res) => res.r);
  return { trades: rs.length, wins: rs.filter((r) => r > 0).length, totalR: sum(rs), meanR: meanOf(rs) };
}

export function summarize(results) {
  const closed = (results || []).filter((res) => res && res.status === "closed" && Number.isFinite(res.r));
  const rs = closed.map((res) => res.r), n = rs.length;
  const wins = rs.filter((r) => r > 0), losses = rs.filter((r) => r < 0);
  const grossWinR = sum(wins), grossLossR = sum(losses.map((r) => -r));
  const totalR = sum(rs), totalRNet = sum(closed.map((res) => res.rNet));
  const sd = sdOf(rs);
  return {
    trades: n, wins: wins.length, losses: losses.length, flat: n - wins.length - losses.length,
    winRate: n ? wins.length / n : null,
    totalR, meanR: meanOf(rs), seR: sd == null ? null : sd / Math.sqrt(n), medianR: medianOf(rs),
    bestR: n ? Math.max(...rs) : null, worstR: n ? Math.min(...rs) : null,
    profitFactor: grossLossR > 0 ? grossWinR / grossLossR : null, grossWinR, grossLossR,
    avgWinR: meanOf(wins), avgLossR: meanOf(losses), avgBarsHeld: meanOf(closed.map((res) => res.barsHeld)),
    longs: sideStats(closed, "long"), shorts: sideStats(closed, "short"),
    totalRNet, meanRNet: n ? totalRNet / n : null,
    dollars: totalR * DOLLARS_PER_R, dollarsNet: totalRNet * DOLLARS_PER_R,
  };
}

const complete = (chart) => chart.bars.length >= chart.nCtx + chart.nReplay;

export function buyAndHold(chart) {
  if (!complete(chart)) return null;
  const { bars, nCtx, nReplay } = chart;
  return 100 * (bars[nCtx + nReplay - 1][C] / bars[nCtx - 1][C] - 1);
}

// ---------------------------------------------------------------- random-entry baseline

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// R of one random market order on bar j: planned at the close of bar j - 1 with the stop and target at the trade's
// distances from that close, filled at bar j's open, walked by the same rules to the last bar and closed at its close
// if neither level is hit. null when the open gaps past the stop or target, which misses the order.
function randomEntryR(bars, j, last, s, stopDist, targetDist) {
  const planned = bars[j - 1][C];
  const levels = { stop: planned * (1 - s * stopDist), target: planned * (1 + s * targetDist) };
  const entry = entryOn("market", null, bars[j], levels, s);
  if (entry.missed) return null;
  let exit = fillBarExit(bars[j], j, levels, s);
  for (let i = j + 1; !exit && i <= last; i++) exit = exitOn(bars[i], i, levels, s, false);
  const risk = Math.max(planned * stopDist, MIN_RISK * planned);
  return ((exit ? exit.price : bars[last][C]) - entry.price) * s / risk;
}

// The entry bar is a draw's only randomness, so each trade's R is worked out once for every replay bar it could
// enter on (nCtx .. last - 1) and the draws pick from the bars where the order fills, as the visitor's scored trades
// all did.
export function randomBaseline(charts, { draws = 2000, seed = 1 } = {}) {
  const outcomes = [];
  let total = 0;
  for (const chart of charts) {
    if (!chart || !chart.finished || !complete(chart)) continue;
    const { bars, nCtx, nReplay } = chart, last = nCtx + nReplay - 1;
    for (const trade of chart.trades || []) {
      const res = simulate(trade, chart);
      if (res.status !== "closed" || last <= nCtx) continue;
      // The distances are shares of the planned entry, as the trade's own risk is.
      const { stop, target } = levelsAt(trade, res.fill.i, nCtx), s = sign(trade.side);
      const p = res.planned ?? res.fill.price, stopDist = Math.abs(p - stop) / p, targetDist = Math.abs(target - p) / p;
      const rs = [];
      for (let j = nCtx; j < last; j++) {
        const r = randomEntryR(bars, j, last, s, stopDist, targetDist);
        if (r != null) rs.push(r);
      }
      outcomes.push(rs.length ? rs : [0]);
      total += res.r;
    }
  }
  if (!outcomes.length) {
    return { meanR: null, sdTotal: null, totalMean: null, percentile: null, draws: 0, trades: 0, total: 0 };
  }
  const n = Math.max(1, Math.floor(draws) || 0), rand = mulberry32(seed), totals = [];
  let below = 0;
  for (let d = 0; d < n; d++) {
    let t = 0;
    for (const rs of outcomes) t += rs[Math.floor(rand() * rs.length)];
    totals.push(t);
    if (t < total - R_TOLERANCE) below += 1;
  }
  const totalMean = sum(totals) / n;
  return { meanR: totalMean / outcomes.length, sdTotal: sdOf(totals) ?? 0, totalMean, percentile: below / n, draws: n,
           trades: outcomes.length, total };
}

// ---------------------------------------------------------------- formatting

// Fixed decimals with thousands separators and a true minus sign; no sign when the value rounds to zero.
function number(x, digits, signed) {
  const text = Math.abs(x).toFixed(digits);
  const [whole, frac] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (frac ? "." + frac : "");
  if (Number(text) === 0) return grouped;
  return (x < 0 ? MINUS : signed ? "+" : "") + grouped;
}

const missing = (x) => typeof x !== "number" || !Number.isFinite(x);

export function fmtR(x, digits = 2) {
  return missing(x) ? NONE : number(x, digits, true) + "R";
}

export function fmtPct(x, digits = 2, signed = true) {
  return missing(x) ? NONE : number(x, digits, signed) + "%";
}

export function fmtPrice(x, digits = 2) {
  return missing(x) ? NONE : number(x, digits, false);
}
