// TradeTest's report and trades CSV (src/stratlib/web/assets/tradetest/report.js): structure, figures that match
// sim.js, nothing revealed for unfinished charts, escaping, CSV quoting and real prices. Run with
// `node --test "tests/js/*.test.mjs"`.

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import * as report from "../../src/stratlib/web/assets/tradetest/report.js";
import * as sim from "../../src/stratlib/web/assets/tradetest/sim.js";

const {
  DISCLAIMER, buildReport, buildTradesCsv, csvField, escapeHtml, guessLine, reportFileName, resultLine, tradesCsvFileName,
} = report;
const SITE = "https://stratlib.blxnksy.dev/tradetest";

const MINUS = String.fromCharCode(0x2212);

// ---------------------------------------------------------------- a synthetic set

// A bar row as the server sends it: o, h, l, c, relative volume, then the moving averages.
const b = (o, h, l, c) => [o, h, l, c, 1, null, null, null, null];
// Three context bars; the last closes at 100. The first holds an odd price that must never reach the report.
const CTX = [b(99, 100.5, 98.7654, 99.5), b(99.5, 100.5, 99, 100), b(100, 100.6, 99.4, 100)];
const N_CTX = CTX.length, N_REPLAY = 8;
const REPLAY = [
  b(100, 101, 99, 100.5),       // bar 1
  b(100.5, 104, 100, 103.5),    // bar 2
  b(104, 104.5, 102.5, 103),    // bar 3: opens above bar 2's close
  b(103, 103.5, 100, 100.5),    // bar 4
  b(100.5, 101, 97, 97.5),      // bar 5
  b(95, 96, 94, 95.5),          // bar 6: gaps down
  b(95.5, 98, 95, 97.8),        // bar 7
  b(97.8, 99, 97.5, 98.6),      // bar 8
];
const BARS = [...CTX, ...REPLAY];

function dates(first) {
  const out = [], d = new Date(first + "T12:00:00Z");
  while (out.length < N_CTX + N_REPLAY) {
    if (d.getUTCDay() % 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

function trade(fields) {
  return { id: 1, side: "long", kind: "market", entry: null, stop: 95, target: 110, placedAt: 0, note: "", changes: [],
           cancelAt: null, closeAt: null, ...fields };
}

const HOSTILE_NOTE = 'Breakout, "clean" retest\nsecond line';
const SCRIPT_NOTE = "<script>alert(1)</script>";
const IMG_LABEL = "<img src=x onerror=alert(1)>";

// Chart 1, played to the end: every exit reason except "finish", every way an order can fail to fill, a moved stop.
const ACME_DATES = dates("2019-03-04");
const chartA = {
  cursor: "c1", k: N_REPLAY, nCtx: N_CTX, nReplay: N_REPLAY, bars: BARS, finished: true, finishedAt: N_REPLAY, nextId: 12,
  guess: { era: "2010s", kind: "stock" },
  reveal: { symbol: "ACME", name: 'Acme "Rockets" & Co <b>', kind: "stock", sector: "Industrials",
            industry: "Aerospace & Defense", exchange: "NYSE", note: "Delisted 2020-01-02", dates: ACME_DATES, scale: 0.5,
            start: ACME_DATES[N_CTX], end: ACME_DATES[N_CTX + N_REPLAY - 1] },
  trades: [
    trade({ id: 1, stop: 98, target: 103.5, note: HOSTILE_NOTE }),                                   // target, bar 2
    trade({ id: 2, side: "short", stop: 102, target: 95, note: SCRIPT_NOTE }),                        // stop, bar 2
    trade({ id: 3, kind: "limit", entry: 101, stop: 98, target: 106, placedAt: 3,
            changes: [{ at: 4, stop: 99, target: 106 }] }),                                           // moved stop hit, bar 5
    trade({ id: 4, stop: 96.5, target: 104, placedAt: 4 }),                                           // gaps through the stop, bar 6
    trade({ id: 5, side: "short", kind: "stop", entry: 99, stop: 101.5, target: 95.5, placedAt: 4,
            note: '=HYPERLINK("http://x")' }),                                                        // gaps through the target
    trade({ id: 6, kind: "limit", entry: 90, stop: 88, target: 110 }),                                // never fills: expired
    trade({ id: 7, kind: "limit", entry: 96.5, stop: 95.5, target: 100, placedAt: 5 }),               // missed, gap_stop
    trade({ id: 8, kind: "stop", entry: 103.7, stop: 102.5, target: 103.9, placedAt: 2 }),            // missed, gap_target
    trade({ id: 9, stop: 94, target: 105, placedAt: 6, closeAt: 7, note: "+2R plan, -ish" }),         // closed by the visitor
    trade({ id: 10, stop: 96, target: 110, placedAt: 7 }),                                            // end of the replay
    trade({ id: 11, side: "short", kind: "limit", entry: 106, stop: 108, target: 96, placedAt: 1, cancelAt: 2 }),  // cancelled
  ],
  drawings: [
    { id: 1, type: "trend", points: [{ x: 0, p: 99 }, { x: 5, p: 103 }], color: "steel", label: IMG_LABEL, extend: true, createdAt: 0 },
    { id: 2, type: "hline", points: [{ x: 0, p: 104 }], color: "brass", label: 'Support, "major"', extend: false, createdAt: 0,
      movedAt: 5 },
    { id: 3, type: "zone", points: [{ x: 6, p: 97 }, { x: 2, p: 98.5 }], color: "teal", label: "Demand & supply", extend: true, createdAt: 2,
      movedAt: 6 },
    { id: 4, type: "channel", points: [{ x: 1, p: 99 }, { x: 5, p: 101 }, { x: 3, p: 102 }], color: "violet", label: "", extend: false, createdAt: 0 },
    { id: 5, type: "ray", points: [{ x: 3, p: 100 }, { x: 6, p: 101 }], color: "coral", label: "Ray one", extend: false, createdAt: 1,
      movedAt: 1 },
    { id: 6, type: "vline", points: [{ x: 7, p: 0 }], color: "red;background:url(https://evil.example/x)", label: "Earnings",
      extend: false, createdAt: 3 },
  ],
  view: null,
};

// Chart 2, finished at bar 4 of 8: the open long leaves at the next open ("finish"), the waiting order expires.
const FUND_DATES = dates("2016-08-01");
const chartB = {
  cursor: "c2", k: 4, nCtx: N_CTX, nReplay: N_REPLAY, bars: BARS, finished: true, finishedAt: 4, nextId: 4,
  guess: { era: "2020s", kind: "stock" },
  reveal: { symbol: "GLDX", name: "Gold Things Fund", kind: "etf", sector: "Commodities", industry: "", exchange: "",
            note: "", dates: FUND_DATES, scale: 2, start: FUND_DATES[N_CTX], end: FUND_DATES[N_CTX + N_REPLAY - 1] },
  trades: [
    trade({ id: 1, stop: 97, target: 120, placedAt: 1 }),                                             // open at the finish
    trade({ id: 2, kind: "limit", entry: 92, stop: 90, target: 99 }),                                 // expires
    trade({ id: 3, side: "short", stop: 104.5, target: 99, placedAt: 2 }),                            // short, exits at target
  ],
  drawings: [],
  view: null,
};

// Chart 3, unfinished at bar 3, with two closed trades, labelled drawings, and a stray reveal and guess that must never be
// shown.
const chartC = {
  cursor: "c3", k: 3, nCtx: N_CTX, nReplay: N_REPLAY, bars: BARS.slice(0, N_CTX + 3), finished: false, finishedAt: null, nextId: 3,
  guess: { era: "2000s", kind: "etf" },
  reveal: { symbol: "LEAKY", name: "Leaky Corp", kind: "stock", sector: "Secrets", industry: "", exchange: "NASDAQ", note: "",
            dates: dates("1999-01-04"), scale: 3, start: "1999-06-01", end: "1999-10-01" },
  trades: [trade({ id: 1, stop: 98, target: 103.5, note: "SECRET-NOTE" }), trade({ id: 2, side: "short", stop: 104, target: 96, placedAt: 1 })],
  drawings: [{ id: 1, type: "hline", points: [{ x: 0, p: 101.2345 }], color: "teal", label: "SECRET-LABEL", extend: false, createdAt: 0 }],
  view: null,
};

// Chart 4, not started: context only. Chart 5, finished with no trades or drawings at all.
const chartD = { cursor: "c4", k: 0, nCtx: N_CTX, nReplay: N_REPLAY, bars: CTX, finished: false, finishedAt: null, reveal: null,
                 trades: [], drawings: [], nextId: 1, view: null };
const QUIET_DATES = dates("2021-02-01");
const chartE = {
  cursor: "c5", k: N_REPLAY, nCtx: N_CTX, nReplay: N_REPLAY, bars: BARS, finished: true, finishedAt: N_REPLAY, nextId: 1,
  reveal: { symbol: "QUIET", name: "Quiet Ltd", kind: "stock", sector: "", industry: "", exchange: "NASDAQ", note: "",
            dates: QUIET_DATES, scale: 1, start: QUIET_DATES[N_CTX], end: QUIET_DATES[N_CTX + N_REPLAY - 1] },
  trades: [], drawings: [], view: null,
};
// Chart 6 was never loaded: no bars yet.
const chartF = { cursor: "c6", k: 0, nCtx: N_CTX, nReplay: N_REPLAY, bars: [], finished: false, finishedAt: null, reveal: null,
                 trades: [], drawings: [], nextId: 1, view: null };

const STATE = { schema: 1, version: "abcdef012345", set: "3fa2c1d0", avoid: "x", created: "2026-10-08T12:00:00Z", active: 0,
                settings: {}, charts: [chartA, chartB, chartC, chartD, chartE, chartF] };
const GENERATED = new Date(2026, 9, 8, 14, 5);
const IMAGE = "data:image/png;base64,iVBORw0KGgo=";

const rendered = [];
const HTML = buildReport(STATE, { renderImage: (chart) => { rendered.push(chart); return IMAGE; }, generatedAt: GENERATED });
const CSV = buildTradesCsv(STATE);

const SUM = sim.summarize(sim.scoredResults(STATE.charts));
const BASE = sim.randomBaseline(STATE.charts);
const ALSO = sim.unfinishedClosed(STATE.charts);

// The text of the element marked data-figure="key" (tags stripped, entities kept). Figures hold no nested element
// of their own kind.
function figure(html, key) {
  const m = new RegExp(`<(\\w+)[^>]*data-figure="${key}"[^>]*>([\\s\\S]*?)</\\1>`).exec(html);
  assert.ok(m, `no figure ${key}`);
  return m[2].replace(/<[^>]+>/g, "");
}

// The HTML of chart n's section, and the rows of one of its tables.
function section(html, n) {
  const m = new RegExp(`<section[^>]*data-chart="${n}"[^>]*>([\\s\\S]*?)</section>`).exec(html);
  return m ? m[1] : null;
}

function tableRows(html, cls) {
  const start = html.indexOf(`tt-table ${cls}`);
  assert.ok(start >= 0, `no ${cls} table`);
  const body = html.slice(html.indexOf("<tbody>", start) + 7, html.indexOf("</tbody>", start));
  return body.split("<tr>").slice(1);
}

// An independent date format to check the report's against: "Mar 7, 2019".
const day = (iso) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric",
                                                                                  timeZone: "UTC" });
const has = (text, part, what = part) => assert.ok(text.includes(part), `missing: ${what}`);

// RFC 4180: fields split on commas outside quotes, records on CRLF outside quotes, "" inside quotes is a quote.
function parseCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"' && field === "") quoted = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\r" && text[i + 1] === "\n") { row.push(field); rows.push(row); row = []; field = ""; i++; }
    else {
      assert.notEqual(ch, "\n", "bare LF outside quotes");
      assert.notEqual(ch, '"', "quote inside an unquoted field");
      field += ch;
    }
  }
  assert.equal(field, "", "the last record ends with CRLF");
  assert.equal(row.length, 0);
  return rows;
}

// ---------------------------------------------------------------- the module itself

describe("module", () => {
  const source = readFileSync(new URL("../../src/stratlib/web/assets/tradetest/report.js", import.meta.url), "utf8");
  const code = source.replace(/\/\/.*$/gm, "");

  it("imports only sim.js, under its own version query, and touches no DOM, network or Math.random", () => {
    assert.doesNotMatch(code, /^\s*import\b/m);
    assert.equal(code.match(/\bimport\(/g).length, 1);
    assert.ok(code.includes("await import(`./sim.js${new URL(import.meta.url).search}`)"));
    assert.doesNotMatch(code, /\brequire\(/);
    assert.doesNotMatch(code, /\bMath\.random\b/);
    // Uses, not words: a comment or a sentence may say "window" or "document".
    assert.doesNotMatch(code, /\b(document|window|globalThis|self|localStorage|sessionStorage|navigator)(\.[A-Za-z_$]|\[)/);
    assert.doesNotMatch(code, /\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\s*\(/);
  });

  it("documents every export in the API reference at the top", () => {
    const header = source.slice(0, source.indexOf("\nconst "));
    assert.ok(header.length > 500);
    for (const name of Object.keys(report)) assert.match(header, new RegExp(`\\b${name}\\b`), name);
  });

  it("never names the data provider", () => {
    assert.doesNotMatch(source, /\bFMP\b|financial\s*modeling/i);
    assert.doesNotMatch(HTML, /\bFMP\b|financial\s*modeling/i);
  });
});

// ---------------------------------------------------------------- the document

describe("buildReport: structure", () => {
  it("is one complete standalone document", () => {
    assert.match(HTML, /^<!doctype html>\n<html lang="en"><head><meta charset="utf-8">/);
    assert.ok(HTML.trimEnd().endsWith("</html>"));
    assert.equal(HTML.match(/<html/g).length, 1);
    assert.equal(HTML.match(/<h1>/g).length, 1);
    assert.match(HTML, /<h1>TradeTest report<\/h1>/);
    assert.match(HTML, /<title>TradeTest report · set 3fa2c1d0<\/title>/);
    assert.match(HTML, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
  });

  it("states when it was made, the set, and how much of it was finished", () => {
    assert.match(HTML, /Generated <b>Oct 8, 2026, 14:05<\/b>/);
    assert.match(HTML, /Set <b>3fa2c1d0<\/b>/);
    assert.match(HTML, /<b>3<\/b> of 6 charts finished/);
    assert.match(HTML, new RegExp(`<b>${SUM.trades}</b> closed trades`));
    assert.match(HTML, /3 charts not finished\./);
  });

  it("has no script, no external resources and no outside requests", () => {
    assert.doesNotMatch(HTML, /<script/i);
    assert.doesNotMatch(HTML, /<link\b|@import|<iframe|<object|<embed/i);
    assert.doesNotMatch(HTML, /\b(src|href)="(?!data:image\/|#chart-\d+"|https:\/\/stratlib\.blxnksy\.dev\/tradetest")/);
  });

  it("carries the disclaimer and the rules, with print styles and the site's tokens", () => {
    assert.ok(HTML.includes(escapeHtml(DISCLAIMER)));
    assert.equal(DISCLAIMER, "Simulated trades on historical prices. Past results are not a forecast, and nothing here is investment advice.");
    assert.match(HTML, /<h2>How trades were scored<\/h2>/);
    for (const term of ["Hidden identity", "Real prices", "Orders", "Gaps", "Stops and targets", "Closing", "R", "Dollars",
                        "Random entries", "Scoring", "Guesses"]) {
      assert.match(HTML, new RegExp(`<dt>${term}</dt>`), term);
    }
    assert.match(HTML, /costs of 0\.10% of the price on each fill/);
    assert.match(HTML, /@media print \{[\s\S]*--bg: #fff/);
    for (const token of ["#131722", "#1e222d", "#2a2e39", "#d1d4dc", "#f0f3fa", "#9598a1", "#3cc4b5", "#ff7f7c"]) {
      assert.ok(HTML.includes(token), token);
    }
  });

  it("never prints NaN, undefined, null or an object", () => {
    for (const bad of ["NaN", "undefined", "null", "[object", "Infinity"]) assert.ok(!HTML.includes(bad), bad);
    for (const bad of ["NaN", "undefined", "null", "[object", "Infinity"]) assert.ok(!CSV.includes(bad), bad);
  });

  it("embeds no bar data, only the chart pictures", () => {
    assert.ok(!HTML.includes("98.7654"));
    assert.ok(!HTML.includes("98.77"));
    assert.doesNotMatch(HTML, /\[\s*\d+(\.\d+)?\s*,\s*\d+(\.\d+)?\s*,\s*\d+(\.\d+)?\s*,/);
    assert.ok(!HTML.includes('"bars"'));
  });
});

describe("buildReport: figures match sim.js", () => {
  it("five figures: total R, win rate, average R with its standard error, profit factor, closed trades", () => {
    assert.ok(SUM.trades >= 8 && SUM.wins > 0 && SUM.losses > 0, "the fixture has winners and losers");
    assert.equal(figure(HTML, "total-r"), `Total R${sim.fmtR(SUM.totalR)}Net of costs ${sim.fmtR(SUM.totalRNet)}`);
    assert.equal(figure(HTML, "win-rate"), `Win rate${Math.round(SUM.winRate * 100)}%${SUM.wins} won, ${SUM.losses} lost` +
      (SUM.flat ? `, ${SUM.flat} flat` : ""));
    assert.equal(figure(HTML, "mean-r"),
      `Average R${sim.fmtR(SUM.meanR)} ± ${sim.fmtPrice(SUM.seR)}RPer trade, ± one standard error`);
    assert.equal(figure(HTML, "profit-factor"),
      `Profit factor${sim.fmtPrice(SUM.profitFactor)}${sim.fmtPrice(SUM.grossWinR)}R won over ${sim.fmtPrice(SUM.grossLossR)}R lost`);
    assert.equal(figure(HTML, "trades"), `Closed trades${SUM.trades}${SUM.longs.trades} long, ${SUM.shorts.trades} short`);
  });

  it("the dollar line: 1% risk per trade on $100,000, before and after costs", () => {
    const money = (x) => (Math.round(x) === 0 ? "" : x < 0 ? MINUS : "+") + "$" + sim.fmtPrice(Math.abs(x), 0);
    assert.equal(figure(HTML, "dollars"),
      `At 1% risk per trade on a $100,000 account: ${money(SUM.dollars)}, or ${money(SUM.dollarsNet)} after costs.`);
    assert.equal(SUM.dollars, SUM.totalR * 1000);
  });

  it("random entries with the same stops and targets, from randomBaseline", () => {
    assert.equal(BASE.trades, SUM.trades);
    assert.equal(figure(HTML, "beat"), `Ahead of ${Math.round(BASE.percentile * 100)}% of random-entry runs`);
    assert.ok(HTML.includes(`${sim.fmtR(BASE.meanR)}</span> a trade`));
    assert.ok(HTML.includes(`The runs made ${sim.fmtR(BASE.totalMean)} on average`));
    has(HTML, `In each of 2,000 runs, every one of your ${SUM.trades} closed trades was re-entered as a market order on a ` +
      "random bar of its own replay, with the same side and the same stop and target distances from its planned entry.");
  });

  it("buy and hold: each finished chart over its replay, and their average", () => {
    const holds = [chartA, chartB, chartE].map(sim.buyAndHold);
    const mean = holds.reduce((a, x) => a + x, 0) / holds.length;
    assert.equal(figure(HTML, "buy-hold"), `${sim.fmtPct(mean, 1)} a chart on average`);
    for (const n of [1, 2, 5]) assert.ok(section(HTML, n).includes(sim.fmtPct(holds[[1, 2, 5].indexOf(n)], 1)));
  });

  it("the cumulative R curve has one point per closed trade and ends at the total", () => {
    has(HTML, `data-total="${sim.fmtR(SUM.totalR)}" data-points="${SUM.trades}"`);
    assert.equal(HTML.match(/class="tt-pt /g).length, SUM.trades);
    assert.match(HTML, /<svg viewBox="0 0 1000 300" preserveAspectRatio="none"/);
    assert.ok(HTML.includes(`Your trades <b class="${SUM.totalR > 0 ? "tt-up" : "tt-down"}">${sim.fmtR(SUM.totalR)}</b>`));
  });

  it("longs and shorts, and how trades ended", () => {
    assert.match(HTML, new RegExp(`<td>Long</td><td class="tt-num">${SUM.longs.trades}</td><td class="tt-num">${SUM.longs.wins}</td>`));
    assert.match(HTML, new RegExp(`<td>Short</td><td class="tt-num">${SUM.shorts.trades}</td><td class="tt-num">${SUM.shorts.wins}</td>`));
    const reasons = sim.scoredResults(STATE.charts).map((res) => res.exit.reason);
    for (const [reason, label] of [["target", "Target"], ["stop", "Stop"], ["close", "Closed by you"], ["finish", "Closed at finish"],
                                   ["end", "End of replay"]]) {
      const n = reasons.filter((x) => x === reason).length;
      assert.ok(n > 0, `the fixture has a ${reason} exit`);
      assert.match(HTML, new RegExp(`<tr><td>${label}</td><td class="tt-num">${n}</td>`), label);
    }
    assert.match(HTML, /Orders that never filled: 1 cancelled, 2 missed on a gap, 2 expired at the finish\./);
  });

  it("the chart-by-chart table totals the finished charts", () => {
    const a = sim.chartSummary(chartA), bb = sim.chartSummary(chartB);
    assert.ok(HTML.includes(`<td class="tt-num ${a.totalR > 0 ? "tt-up" : "tt-down"}">${sim.fmtR(a.totalR)}</td>`));
    assert.ok(HTML.includes(sim.fmtR(bb.totalR)));
    assert.match(HTML, /<tr class="tt-total"><td>All<\/td><td>3 finished charts<\/td>/);
  });
});

describe("buildReport: finished charts", () => {
  it("reveal the instrument, the dates and the scale", () => {
    const a = section(HTML, 1), acmeEnd = day(ACME_DATES[N_CTX + N_REPLAY - 1]);
    // 100.00 is the close before the replay; the last replay bar closes at 98.60, so at scale 0.5 it was $197.20.
    has(a, "This was Acme &quot;Rockets&quot; &amp; Co &lt;b&gt; (ACME), a stock in Industrials, Aerospace &amp; Defense, " +
      `from ${day(ACME_DATES[N_CTX])} to ${acmeEnd} on daily bars. 100.00 on the chart, the close before the replay, was ` +
      "$200.00. The last close was $197.20.");
    has(a, `The 3 bars of history before the replay start on ${day(ACME_DATES[0])}. Delisted 2020-01-02.`);
    has(a, '<span class="tt-tag">Stock</span><span class="tt-tag">NYSE</span>');
    const fund = section(HTML, 2), fundEnd = day(FUND_DATES[N_CTX + N_REPLAY - 1]);
    has(fund, `This was Gold Things Fund (GLDX), an ETF in Commodities, from ${day(FUND_DATES[N_CTX])} to ` +
      `${fundEnd} on daily bars.`);
    // Finished at bar 4, but the replay still ends at its last bar: 98.60 at scale 2.
    has(fund, "100.00 on the chart, the close before the replay, was $50.00. The last close was $49.30.");
    assert.ok(!HTML.includes("(Prices as traded"));
    // The old sentence read as if $X were the last day's price.
    assert.ok(!HTML.includes("100.00 on the chart was"));
    has(fund, `You finished at bar 4, ${day(FUND_DATES[N_CTX + 3])}; the 4 bars after it are shown dimmed and were not traded.`);
    has(fund, '<span class="tt-tag">ETF</span></h2>');
  });

  it("show the picture renderImage drew for each finished chart, and only those", () => {
    assert.deepEqual(rendered, [chartA, chartB, chartE]);
    assert.equal(HTML.match(/<img /g).length, 3);
    assert.ok(section(HTML, 1).includes(`<img src="${IMAGE}" alt="Chart 1, ACME, on daily bars with your drawings and trades">`));
  });

  it("list every trade with chart and real prices, fills, exits, results and R", () => {
    const results = sim.chartSummary(chartA).results, rows = tableRows(section(HTML, 1), "tt-trades");
    assert.equal(rows.length, chartA.trades.length);
    // #1: a market long at the next open, stop 98 ($196.00), out at the target 103.50 on bar 2.
    assert.ok(rows[0].includes("Market<small>planned 100.00</small>"));
    assert.ok(rows[0].includes("98.00<small>$196.00</small>"));
    assert.ok(rows[0].includes("103.50<small>$207.00</small>"));
    has(rows[0], `Bar 1<span class="tt-sep"> · </span><span class="tt-date">${day(ACME_DATES[N_CTX])}</span>`);
    has(rows[0], `Bar 2<span class="tt-sep"> · </span><span class="tt-date">${day(ACME_DATES[N_CTX + 1])}</span>`);
    has(rows[0], `Bar 0<small>${day(ACME_DATES[N_CTX - 1])}</small>`);
    assert.ok(rows[0].includes(`>Target<`));
    assert.ok(rows[0].includes(sim.fmtR(results[0].r)) && rows[0].includes(`net ${sim.fmtR(results[0].rNet)}`));
    // #3: a limit entry, with the moved stop shown under the original.
    assert.ok(rows[2].includes("101.00<small>$202.00</small>"));
    assert.ok(rows[2].includes("98.00<small>$196.00<br>moved to 99.00</small>"));
    assert.ok(rows[2].includes("Limit"));
    // #4: a gap through the stop; #5 a short stop entry gapping through its target.
    assert.ok(rows[3].includes("Stop<small>gap open</small>"));
    assert.ok(rows[4].includes("Stop entry") && rows[4].includes("Target<small>gap open</small>"));
    // The orders that never filled.
    assert.ok(rows[5].includes("Expired<small>unfilled at the finish</small>"));
    assert.ok(rows[6].includes("Missed<small>Bar 6 opened past the stop</small>"));
    assert.ok(rows[7].includes("Missed<small>Bar 3 opened past the target</small>"));
    assert.ok(rows[8].includes("Closed by you<small>at the next open</small>"));
    assert.ok(rows[9].includes("End of replay<small>at the last close</small>"));
    assert.ok(rows[10].includes("Cancelled<small>at bar 2</small>"));
    assert.ok(section(HTML, 2).includes("Closed at finish<small>at the next open</small>"));
    // R, % move and bars held for each filled trade, straight from simulate().
    results.forEach((res, n) => {
      if (!res.fill) return;
      assert.ok(rows[n].includes(sim.fmtR(res.r)), `#${n + 1} R`);
      assert.ok(rows[n].includes(sim.fmtPct(res.pct)), `#${n + 1} pct`);
    });
    assert.ok(rows[4].includes("2 bars") || rows[4].includes("1 bar"));
  });

  it("list the drawings with their type, label and prices", () => {
    const rows = tableRows(section(HTML, 1), "tt-drawings");
    assert.equal(rows.length, chartA.drawings.length);
    assert.ok(rows[0].includes("Trendline") && rows[0].includes(`Bar ${MINUS}2 at 99.00`) && rows[0].includes("extended right"));
    assert.ok(rows[1].includes("Price level") && rows[1].includes('104.00 <span class="tt-real">$208.00</span>'));
    assert.ok(rows[2].includes("Zone") && rows[2].includes("97.00") && rows[2].includes("98.50") && rows[2].includes(`Bar 0 to Bar 4`));
    assert.ok(rows[3].includes("Channel") && rows[3].includes("parallel line 2.00 above") && rows[3].includes("No label"));
    assert.ok(rows[4].includes("Ray") && rows[4].includes("From Bar 1 at 100.00"));
    assert.ok(rows[5].includes("Time level") && rows[5].includes("Bar 5"));
    assert.ok(rows[0].includes("<td>Before the replay</td>") && rows[3].includes("<td>Before the replay</td>"));
    // A drawing moved after it was drawn says when, so a line fitted with hindsight can't pass for one drawn blind.
    assert.ok(rows[1].includes("<td>Before the replay, moved at bar 5</td>"));
    assert.ok(rows[2].includes("<td>At bar 2, moved at bar 6</td>"));
    assert.ok(rows[4].includes("<td>At bar 1</td>"));   // moved on the bar it was drawn: nothing to say
    assert.ok(rows[5].includes("<td>At bar 3</td>"));
  });

  it("a finished chart without trades or drawings says so", () => {
    const quiet = section(HTML, 5);
    assert.ok(quiet.includes("No trades on this chart."));
    assert.ok(quiet.includes("No drawings on this chart."));
  });
});

describe("buildReport: unfinished charts", () => {
  it("are listed as not finished, with progress only", () => {
    assert.match(HTML, /<td>Chart 3<\/td><td colspan="8"><span class="tt-pending">Not finished, not scored<\/span><small>Bar 3 of 8<\/small>/);
    assert.match(HTML, /<td>Chart 4<\/td><td colspan="8"><span class="tt-pending">Not finished, not scored<\/span><small>Not started<\/small>/);
    assert.match(HTML, /<td>Chart 6<\/td><td colspan="8"><span class="tt-pending">Not finished, not scored<\/span><small>Not started<\/small>/);
    for (const n of [3, 4, 6]) assert.equal(section(HTML, n), null, `chart ${n} has no section`);
  });

  it("reveal nothing: no symbol, name, sector, dates, trades or drawings", () => {
    for (const secret of ["LEAKY", "Leaky", "Secrets", "1999", "SECRET-NOTE", "SECRET-LABEL", "101.23", "$303", "2000s"]) {
      assert.ok(!HTML.includes(secret), secret);
      assert.ok(!CSV.includes(secret), secret);
    }
  });

  it("are not scored, and an all-unfinished set shows no score at all", () => {
    const html = buildReport({ set: "abc", charts: [chartC, chartD, chartF] }, { generatedAt: GENERATED });
    assert.match(html, /No charts finished yet\./);
    assert.match(html, /No closed trades on finished charts yet/);
    assert.equal(figure(html, "total-r"), "Total R0.00RNet of costs 0.00R");
    assert.equal(figure(html, "win-rate"), `Win rate${String.fromCharCode(0x2013)}No closed trades`);
    assert.equal(figure(html, "trades"), "Closed trades0On finished charts");
    for (const bad of ["NaN", "undefined", "null", "LEAKY", "SECRET", "<img "]) assert.ok(!html.includes(bad), bad);
  });
});

describe("buildReport: escaping", () => {
  it("escapes visitor notes and labels", () => {
    assert.ok(!HTML.includes(SCRIPT_NOTE));
    assert.ok(HTML.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
    assert.ok(!HTML.includes(IMG_LABEL));
    assert.ok(HTML.includes("&lt;img src=x onerror=alert(1)&gt;"));
    assert.doesNotMatch(HTML, /<[^>]*\son\w+\s*=/i);
    assert.ok(HTML.includes("Breakout, &quot;clean&quot; retest\nsecond line"));
    assert.ok(HTML.includes("Support, &quot;major&quot;"));
    assert.ok(HTML.includes("Demand &amp; supply"));
  });

  it("escapes the instrument's details too, and only ever writes known colours", () => {
    assert.ok(!HTML.includes("Co <b>"));
    assert.ok(!HTML.includes("evil.example"));
    assert.match(section(HTML, 1), /background:#a3abbd"><\/i>Time level/);
  });

  it("reads names from its own tables only, so odd keys print as unknown, never as code", () => {
    const odd = { ...chartE, trades: [trade({ id: 1, side: "constructor", kind: "toString", stop: 98, target: 103.5 })],
                  drawings: [{ id: 1, type: "constructor", points: [], color: "__proto__", label: "x", extend: false, createdAt: 0 }] };
    const html = buildReport({ set: "odd", charts: [odd] }, { generatedAt: GENERATED }), csv = buildTradesCsv({ charts: [odd] });
    for (const text of [html, csv]) assert.doesNotMatch(text, /function|native code|\[object/);
    assert.match(html, /background:#a3abbd"><\/i>Drawing<\/td>/);
  });

  it("escapeHtml covers the five characters and empty values", () => {
    assert.equal(escapeHtml(`<a href="x" title='y'>&</a>`), "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;");
    assert.equal(escapeHtml(null), "");
    assert.equal(escapeHtml(undefined), "");
    assert.equal(escapeHtml(0), "0");
  });

  it("refuses pictures that are not image data URLs, and survives renderImage throwing", () => {
    const one = { set: "x1", charts: [chartE] };
    for (const url of ['javascript:alert(1)', 'https://evil.example/a.png', 'data:text/html,<script>alert(1)</script>', 42, null]) {
      const html = buildReport(one, { renderImage: () => url, generatedAt: GENERATED });
      assert.ok(!html.includes("<img"), String(url));
      assert.ok(!html.includes("evil.example") && !html.includes("javascript:"), String(url));
    }
    const html = buildReport(one, { renderImage: () => { throw new Error("no canvas"); }, generatedAt: GENERATED });
    assert.match(html, /The picture of this chart could not be drawn\./);
    const quoted = buildReport(one, { renderImage: () => 'data:image/svg+xml;utf8,<svg x="1"/>', generatedAt: GENERATED });
    assert.ok(quoted.includes('<img src="data:image/svg+xml;utf8,&lt;svg x=&quot;1&quot;/&gt;"'));
  });

  it("handles a missing or odd state without throwing", () => {
    for (const state of [undefined, null, {}, { charts: null }, { set: "<b>x</b>", charts: [null] }]) {
      const html = buildReport(state, { generatedAt: GENERATED });
      assert.ok(html.startsWith("<!doctype html>"));
      for (const bad of ["NaN", "undefined", "<b>x</b>"]) assert.ok(!html.includes(bad), `${JSON.stringify(state)}: ${bad}`);
    }
  });
});

// ---------------------------------------------------------------- CSV

describe("buildTradesCsv", () => {
  const rows = parseCsv(CSV);
  const [head, ...body] = rows;
  const col = (name) => head.indexOf(name);

  it("has a header and one row per trade on finished charts, with CRLF line ends", () => {
    assert.deepEqual(head, ["Chart", "Symbol", "Trade", "Side", "Order", "Status", "Placed", "Filled", "Exited", "Entry, USD",
      "Planned entry, USD", "Stop, USD", "Target, USD", "Final stop, USD", "Final target, USD", "Fill, USD", "Exit, USD",
      "Exit reason", "Risk, USD", "R", "Net R", "Move, %", "Bars held", "Note"]);
    assert.equal(body.length, chartA.trades.length + chartB.trades.length);
    assert.ok(body.every((row) => row.length === head.length));
    assert.deepEqual([...new Set(body.map((row) => row[col("Symbol")]))], ["ACME", "GLDX"]);
    assert.ok(CSV.endsWith("\r\n"));
    assert.ok(!/[^\r]\n/.test(CSV.replace(/"[^"]*"/g, "")), "every line break outside quotes is CRLF");
  });

  it("quotes commas, quotes and line breaks per RFC 4180", () => {
    assert.ok(CSV.includes('"Breakout, ""clean"" retest\nsecond line"'));
    const note = body[0][col("Note")];
    assert.equal(note, HOSTILE_NOTE);
    assert.equal(body[1][col("Note")], SCRIPT_NOTE);
    assert.equal(csvField('a,b'), '"a,b"');
    assert.equal(csvField('say "hi"'), '"say ""hi"""');
    assert.equal(csvField("two\r\nlines"), '"two\r\nlines"');
    assert.equal(csvField("plain"), "plain");
    assert.equal(csvField(null), "");
  });

  it("guards text that a spreadsheet would run as a formula", () => {
    assert.equal(body[4][col("Note")], `'=HYPERLINK("http://x")`);
    assert.equal(body[8][col("Note")], "'+2R plan, -ish");
    // Numbers keep their plain minus.
    assert.equal(body[1][col("R")], "-1.000");
  });

  it("converts chart prices to real prices: chart price / scale", () => {
    const one = body[0];   // ACME, scale 0.5: a market long, stop 98, target 103.5, filled at 100
    assert.equal(one[col("Entry, USD")], "");
    assert.equal(one[col("Order")], "Market");
    assert.equal(one[col("Stop, USD")], "196.00");
    assert.equal(one[col("Target, USD")], "207.00");
    assert.equal(one[col("Fill, USD")], "200.00");
    assert.equal(one[col("Exit, USD")], "207.00");
    assert.equal(one[col("Planned entry, USD")], "200.00");   // the close it was placed at, 100.00
    assert.equal(one[col("Risk, USD")], "4.00");
    const three = body[2];
    assert.equal(three[col("Entry, USD")], "202.00");
    assert.equal(three[col("Planned entry, USD")], "202.00");
    assert.equal(three[col("Risk, USD")], "6.00");   // 101 to the stop of 98 in effect on its fill bar
    assert.equal(three[col("Stop, USD")], "196.00");
    assert.equal(three[col("Final stop, USD")], "198.00");
    const fund = body[chartA.trades.length];   // GLDX, scale 2: stop 97 is $48.50
    assert.equal(fund[col("Stop, USD")], "48.50");
    assert.equal(fund[col("Target, USD")], "60.00");
  });

  it("dates come from the revealed dates: placed, filled, exited", () => {
    const one = body[0];
    assert.equal(one[col("Placed")], ACME_DATES[N_CTX - 1]);
    assert.equal(one[col("Filled")], ACME_DATES[N_CTX]);
    assert.equal(one[col("Exited")], ACME_DATES[N_CTX + 1]);
    assert.equal(body[5][col("Filled")], "");
  });

  it("R, net R, % move, bars held, status and reason match simulate()", () => {
    const results = [...sim.chartSummary(chartA).results, ...sim.chartSummary(chartB).results];
    results.forEach((res, n) => {
      const row = body[n];
      assert.equal(row[col("Status")].toLowerCase(), res.status, `row ${n}`);
      if (!res.fill) {
        assert.deepEqual([row[col("R")], row[col("Net R")], row[col("Move, %")], row[col("Bars held")]], ["", "", "", ""]);
        return;
      }
      assert.equal(Number(row[col("R")]), Number(res.r.toFixed(3)));
      assert.equal(Number(row[col("Net R")]), Number(res.rNet.toFixed(3)));
      assert.equal(Number(row[col("Move, %")]), Number(res.pct.toFixed(2)));
      assert.equal(Number(row[col("Bars held")]), res.barsHeld);
    });
    const reasons = body.map((row) => row[col("Exit reason")]);
    for (const reason of ["Target", "Stop", "Closed by you", "Closed at finish", "End of replay", "Missed, opened past the stop",
                          "Missed, opened past the target"]) {
      assert.ok(reasons.includes(reason), reason);
    }
    assert.deepEqual(body.map((row) => row[col("Status")]).filter((s) => s !== "Closed").sort(),
      ["Cancelled", "Expired", "Expired", "Missed", "Missed"]);
  });

  it("is a header only when nothing is finished", () => {
    assert.equal(buildTradesCsv({ charts: [chartC, chartD] }).split("\r\n").length, 2);
    assert.equal(buildTradesCsv(undefined).split("\r\n").length, 2);
  });
});

describe("buildReport: the new rules and wording", () => {
  it("measures risk from the planned entry, in the rules and under the trades", () => {
    has(HTML, "Risk is the distance from the planned entry to the stop in effect when the order fills: the planned entry " +
      "is the close a market order was placed at, or the entry price of a limit or stop entry");
    has(HTML, "a fill better or worse than planned, on a gap, shows up as more or less R, the way slippage does");
    assert.doesNotMatch(HTML, /distance from the fill to the stop/);
    has(section(HTML, 1), "R is measured from the planned entry: the close a market order was placed at, or the entry price.");
    // #9 is a market long placed at bar 6, which closed at 95.50.
    assert.ok(tableRows(section(HTML, 1), "tt-trades")[8].includes("Market<small>planned 95.50</small>"));
  });

  it("says a close asked for on the finish bar stays a close by you, and how real prices are measured", () => {
    has(HTML, "A close request exits at the next bar's open and is listed as closed by you, even when you finish the chart " +
      "on that bar.");
    has(HTML, "Chart prices carry a small random offset, at most about 0.3% of the price, so the numbers can't give the real " +
      "price away. Real prices are as traded on the replay's last day, adjusted only for splits during the replay.");
    has(HTML, "Each chart above gives the real close before the replay, which is 100.00 on the chart, and the last close.");
    has(HTML, "Each chart is a real US-listed stock or ETF over a real stretch of daily bars, picked at random.");
    assert.ok(!HTML.includes("real instrument"));
    for (const old of ["end of the window", "half a tick", "random draws", "You beat"]) assert.ok(!HTML.includes(old), old);
    has(HTML, "Your browser gets the name, the dates and the bars you haven't reached only when you finish the chart");
    assert.doesNotMatch(HTML, /impossible|even with devtools/i);
  });

  it("tells a visitor who finished early how the price went after", () => {
    // Chart 2 was finished at bar 4 (close 100.50); the last bar closes at 98.60.
    const move = 100 * (98.6 / 100.5 - 1);
    has(section(HTML, 2), `From your finish to the last bar: <span class="tt-down">${sim.fmtPct(move, 1)}</span>.`);
    assert.ok(!section(HTML, 1).includes("From your finish"));   // played to the end
  });

  it("a finished chart whose orders never filled says no closed trades, and still lists them", () => {
    const waiting = { ...chartE, trades: [trade({ kind: "limit", entry: 90, stop: 88, target: 110 }), trade({ id: 2, stop: 101 })] };
    const one = section(buildReport({ set: "w", charts: [waiting] }, { generatedAt: GENERATED }), 1);
    has(one, "No closed trades on this chart.");
    assert.ok(!one.includes("No trades on this chart."));
    assert.equal(tableRows(one, "tt-trades").length, 2);
    assert.ok(section(HTML, 5).includes("No trades on this chart.") && !section(HTML, 5).includes("No closed trades"));
    assert.ok(!section(HTML, 1).includes("No closed trades"));
  });
});

describe("buildReport: trades closed on unfinished charts", () => {
  it("are shown beside the headline, not in it", () => {
    assert.equal(ALSO.trades, 2);
    assert.equal(figure(HTML, "unfinished"), `Also closed on charts you didn't finish: 2 trades, ${sim.fmtR(ALSO.totalR)} ` +
      `(net of costs ${sim.fmtR(ALSO.totalRNet)}). Not in the figures above; finish those charts to score them.`);
    // The headline is the finished charts' alone.
    assert.equal(SUM.trades, sim.scoredResults([chartA, chartB, chartE]).length);
    assert.ok(figure(HTML, "trades").startsWith(`Closed trades${SUM.trades}`));
  });

  it("keep the verification round's example honest: +2R finished, -1R left unfinished", () => {
    const ctx = [b(100, 100.5, 99.5, 100), b(100, 100.5, 99.5, 100)];
    const t = trade({ stop: 98, target: 104 }), five = Array.from({ length: 5 }, (_, i) => `2015-01-0${i + 1}`);
    const won = { cursor: "a", k: 3, nCtx: 2, nReplay: 3, finished: true, finishedAt: 3, trades: [t], drawings: [],
                  bars: [...ctx, b(100, 101, 99.8, 101), b(101, 104, 100.5, 103.5), b(103.5, 104, 103, 103.8)],
                  reveal: { symbol: "WIN", name: "Win", kind: "stock", dates: five, scale: 1, start: five[2], end: five[4] } };
    const lost = { cursor: "b", k: 3, nCtx: 2, nReplay: 3, finished: false, finishedAt: null, trades: [t], drawings: [],
                   bars: [...ctx, b(100, 100.2, 97, 97.5), b(97.5, 98, 96, 96.5), b(96.5, 97, 96, 96.8)], reveal: null };
    const html = buildReport({ set: "x", charts: [won, lost] }, { generatedAt: GENERATED });
    assert.equal(figure(html, "total-r"), `Total R+2.00RNet of costs ${sim.fmtR(sim.simulate(t, won).rNet)}`);
    assert.match(figure(html, "unfinished"), new RegExp(`^Also closed on charts you didn't finish: 1 trade, ${MINUS}1\\.00R `));
  });

  it("are left out when there are none", () => {
    const html = buildReport({ set: "y", charts: [chartA, chartB, chartD] }, { generatedAt: GENERATED });
    assert.ok(!html.includes('data-figure="unfinished"'));
    assert.ok(!html.includes('data-figure="not-loaded"'));
  });
});

// The verification round's set: an unfinished chart with closed trades whose rows the page couldn't get (offline, or
// the charts were updated). Its trades must never vanish without a word, nor be counted from rows that stop short.
describe("buildReport: unfinished charts whose rows didn't load", () => {
  const noRows = { ...chartC, bars: [] };
  const shortRows = { ...chartC, bars: BARS.slice(0, N_CTX + 1) };     // saved at bar 1, the chart is at bar 3

  it("are named as not counted, in place of a missing Also-closed line", () => {
    for (const unloaded of [noRows, shortRows]) {
      const html = buildReport({ set: "m", charts: [chartA, unloaded] }, { generatedAt: GENERATED });
      assert.equal(figure(html, "not-loaded"), "Trades on 1 unfinished chart couldn't be loaded and aren't counted.");
      assert.ok(!html.includes('data-figure="unfinished"'));
      // Nothing of the chart leaks either way.
      assert.ok(!html.includes("SECRET-NOTE") && !html.includes("LEAKY"));
    }
  });

  it("sit beside the Also-closed line when other unfinished charts did load", () => {
    const html = buildReport({ set: "m", charts: [chartA, chartC, noRows, { ...noRows, cursor: "c7" }] },
                             { generatedAt: GENERATED });
    assert.match(figure(html, "unfinished"), /^Also closed on charts you didn't finish: 2 trades, /);
    assert.equal(figure(html, "not-loaded"), "Trades on 2 unfinished charts couldn't be loaded and aren't counted.");
    assert.ok(html.indexOf('data-figure="unfinished"') < html.indexOf('data-figure="not-loaded"'));
  });

  it("count only charts with trades, and say 'charts' when one was flagged finished", () => {
    assert.ok(!buildReport({ set: "m", charts: [chartA, chartF] }, { generatedAt: GENERATED }).includes("not-loaded"));
    const finishedNoRows = { ...chartA, bars: [] };
    const html = buildReport({ set: "m", charts: [chartB, finishedNoRows] }, { generatedAt: GENERATED });
    assert.equal(figure(html, "not-loaded"), "Trades on 1 chart couldn't be loaded and aren't counted.");
  });

  it("change nothing in the CSV, which lists finished charts only", () => {
    assert.equal(buildTradesCsv({ charts: [chartA, noRows] }), buildTradesCsv({ charts: [chartA, chartC] }));
  });
});

describe("buildReport: the fill-risk example from the verification round", () => {
  it("a planned 2R long that fills just above its stop is about +3R and +$2,990, not +157R", () => {
    const bars = [b(100, 100.5, 99.5, 100), b(100, 100.5, 99.5, 100), b(95.05, 96, 95.02, 95.8), b(96, 111, 95.9, 110.5)];
    const four = ["2015-01-01", "2015-01-02", "2015-01-05", "2015-01-06"];
    const c = { cursor: "a", k: 2, nCtx: 2, nReplay: 2, bars, finished: true, finishedAt: 2, drawings: [],
                trades: [trade({ stop: 95, target: 110 })],
                reveal: { symbol: "X", name: "X", kind: "stock", dates: four, scale: 1, start: four[2], end: four[3] } };
    const html = buildReport({ set: "x", charts: [c] }, { generatedAt: GENERATED });
    assert.match(figure(html, "total-r"), /^Total R\+2\.99R/);
    assert.match(figure(html, "dollars"), /account: \+\$2,990, or /);
    assert.ok(!html.includes("157"));
    const [head, row] = parseCsv(buildTradesCsv({ charts: [c] }));
    assert.deepEqual([row[head.indexOf("Planned entry, USD")], row[head.indexOf("Fill, USD")], row[head.indexOf("Risk, USD")],
                      row[head.indexOf("R")]], ["100.00", "95.05", "5.00", "2.990"]);
  });
});

describe("guesses", () => {
  it("each finished chart says what was guessed and how it went", () => {
    assert.equal(guessLine(chartA), "Your guess: 2010s, stock. Era right, type right.");
    assert.equal(guessLine(chartB), "Your guess: 2020s, stock. Era wrong, type wrong.");
    assert.equal(guessLine(chartE), "No guess");
    const text = (n) => /<p class="tt-guess"[^>]*>([\s\S]*?)<\/p>/.exec(section(HTML, n))[1].replace(/<[^>]+>/g, "");
    assert.deepEqual([1, 2, 5].map(text), [guessLine(chartA), guessLine(chartB), "No guess"]);
  });

  it("a partial guess is judged on what was answered", () => {
    assert.equal(guessLine({ ...chartB, guess: { era: null, kind: "etf" } }), "Your guess: ETF. Type right.");
    assert.equal(guessLine({ ...chartE, guess: { era: "2020s", kind: null } }), "Your guess: 2020s. Era right.");
    assert.equal(guessLine({ ...chartE, guess: { era: "2010s" } }), "Your guess: 2010s. Era wrong.");
    assert.equal(guessLine({ ...chartE, guess: { era: null, kind: null } }), "No guess");
  });

  it("only the offered choices count; odd values are no guess", () => {
    for (const guess of [{ era: "1990s", kind: "<b>" }, { era: "constructor", kind: "__proto__" }, "2010s", 7, null]) {
      assert.equal(guessLine({ ...chartA, guess }), "No guess", JSON.stringify(guess));
    }
    // Nothing to judge against without a start date or a kind: the guess is shown, unjudged.
    const bare = { ...chartA, reveal: { ...chartA.reveal, start: "", kind: "" } };
    assert.equal(guessLine(bare), "Your guess: 2010s, stock.");
  });

  it("a chart not finished has no guess line, whatever its state holds", () => {
    assert.equal(guessLine(chartC), "");
    assert.equal(guessLine(null), "");
    assert.equal(guessLine({ ...chartA, bars: [] }), "");
  });

  it("the chart-by-chart table counts the guesses right", () => {
    const rows = tableRows(HTML, "tt-overview");
    assert.ok(rows[0].includes("<td>2010s, stock<small>Era right, type right</small></td>"));
    assert.ok(rows[1].includes("<td>2020s, stock<small>Era wrong, type wrong</small></td>"));
    const quiet = /<tr><td><a href="#chart-5">[\s\S]*?<\/tr>/.exec(HTML)[0];
    assert.ok(quiet.endsWith('<td><span class="tt-muted">No guess</span></td></tr>'));
    assert.match(HTML, /<tr class="tt-total">[\s\S]*?<td>1 of 2 eras, 1 of 2 types<small>guessed right<\/small><\/td><\/tr>/);
    const none = buildReport({ set: "n", charts: [chartE] }, { generatedAt: GENERATED });
    assert.match(none, /<tr class="tt-total">[\s\S]*?<td><span class="tt-muted">No guesses<\/span><\/td><\/tr>/);
    const one = buildReport({ set: "o", charts: [chartA] }, { generatedAt: GENERATED });
    assert.match(one, /<td>1 of 1 era, 1 of 1 type<small>guessed right<\/small><\/td>/);
  });
});

describe("resultLine", () => {
  const pct = (x) => `${Math.round(100 * x)}%`;

  it("is one line with the score, the win rate, the random comparison and the link", () => {
    const charts = [chartA, chartB, chartE], line = resultLine({ charts });
    const sum = sim.summarize(sim.scoredResults(charts)), base = sim.randomBaseline(charts);
    assert.equal(line, `TradeTest: ${sim.fmtR(sum.totalR)} on 3 charts, ${pct(sum.winRate)} won, ahead of ` +
      `${pct(base.percentile)} of random-entry runs. ${SITE}`);
    assert.match(line, new RegExp("^TradeTest: [+−]\\d+\\.\\d\\dR on \\d+ charts?, \\d+% won, ahead of \\d+% of random-entry " +
      "runs\\. https://stratlib\\.blxnksy\\.dev/tradetest$"));
    assert.ok(!/[\r\n]/.test(line));
  });

  it("adds the trades closed on unfinished charts, and matches the report's figures", () => {
    assert.equal(resultLine(STATE), `TradeTest: ${sim.fmtR(SUM.totalR)} on 3 charts, ${pct(SUM.winRate)} won, ahead of ` +
      `${pct(BASE.percentile)} of random-entry runs (also 2 trades closed on unfinished charts: ${sim.fmtR(ALSO.totalR)}). ${SITE}`);
    // The same words as the report's comparison, and as the page's score line.
    assert.ok(resultLine(STATE).includes(figure(HTML, "beat").replace("Ahead of ", "ahead of ")));
  });

  it("says so when nothing is finished or nothing closed", () => {
    assert.equal(resultLine({ charts: [chartD, chartF] }), `TradeTest: no charts finished yet. ${SITE}`);
    assert.equal(resultLine({ charts: [chartC] }),
      `TradeTest: no charts finished yet (2 trades closed on unfinished charts: ${sim.fmtR(ALSO.totalR)}). ${SITE}`);
    assert.equal(resultLine({ charts: [chartE] }), `TradeTest: no closed trades on 1 chart. ${SITE}`);
    for (const state of [undefined, null, {}, { charts: [null] }]) {
      assert.equal(resultLine(state), `TradeTest: no charts finished yet. ${SITE}`);
    }
  });

  it("names one chart in the singular", () => {
    assert.match(resultLine({ charts: [chartA] }), /^TradeTest: [+−][\d.]+R on 1 chart, \d+% won, ahead of \d+% of random-entry runs\. /);
  });

  it("says when trades on unfinished charts couldn't be loaded, beside those that were", () => {
    const head = resultLine({ charts: [chartA] }).replace(/\. https:.*$/, "");
    const noRows = { ...chartC, bars: [] };
    assert.equal(resultLine({ charts: [chartA, noRows] }),
      `${head} (trades on 1 unfinished chart couldn't be loaded and aren't counted). ${SITE}`);
    assert.equal(resultLine({ charts: [chartA, chartC, noRows, { ...noRows }] }),
      `${head} (also 2 trades closed on unfinished charts: ${sim.fmtR(ALSO.totalR)}; trades on 2 other unfinished charts ` +
      `couldn't be loaded and aren't counted). ${SITE}`);
    assert.equal(resultLine({ charts: [noRows] }),
      `TradeTest: no charts finished yet (trades on 1 unfinished chart couldn't be loaded and aren't counted). ${SITE}`);
  });
});

describe("buildReport: chart headings and singular wording", () => {
  it("name each chart in one heading line, with no label above it", () => {
    has(section(HTML, 1), '<h2><span>Chart 1 · Acme &quot;Rockets&quot; &amp; Co &lt;b&gt; <span class="tt-sym">(ACME)</span></span>' +
      '<span class="tt-tag">Stock</span><span class="tt-tag">NYSE</span></h2>');
    has(section(HTML, 2), '<h2><span>Chart 2 · Gold Things Fund <span class="tt-sym">(GLDX)</span></span><span class="tt-tag">ETF</span></h2>');
    // A name that is only the symbol isn't said twice.
    const bare = buildReport({ set: "s", charts: [{ ...chartE, reveal: { ...chartE.reveal, name: "" } }] }, { generatedAt: GENERATED });
    has(section(bare, 1), "<h2><span>Chart 1 · QUIET</span>");
    assert.ok(!HTML.includes("tt-kicker"));
    assert.ok(!/<p[^>]*>Chart \d+<\/p>/.test(HTML));
  });

  it("one closed trade, one chart and one bar left are worded in the singular", () => {
    // Finished one bar before the end, with a market long that hits its target on bar 2.
    const late = { ...chartE, k: N_REPLAY - 1, finishedAt: N_REPLAY - 1, trades: [trade({ stop: 98, target: 103.5 })] };
    const html = buildReport({ set: "one", charts: [late] }, { generatedAt: GENERATED });
    has(html, "In each of 2,000 runs, your one closed trade was re-entered as a market order on a random bar of its own " +
      "replay, with the same side and the same stop and target distances from its planned entry.");
    has(section(html, 1), "the bar after it is shown dimmed and was not traded.");
    has(html, "<b>1</b> of 1 chart finished · <b>1</b> closed trade");
    assert.match(figure(html, "beat"), /^Ahead of \d+% of random-entry runs$/);
    for (const bad of ["1 closed trades", "1 bars", "Each of your 1", "1 charts"]) assert.ok(!html.includes(bad), bad);
  });
});

describe("buildReport: the curve's chart labels", () => {
  // A set of finished charts on rising bars, with counts[c] closed trades (all winners) on chart c + 1.
  const BARS_UP = [b(99, 100.5, 98.5, 100), b(100, 100.5, 99.5, 100),
                   ...Array.from({ length: 30 }, (_, i) => b(100 + i, 101.2 + i, 99.8 + i, 101 + i))];
  const DAYS = BARS_UP.map((_, i) => new Date(Date.UTC(2015, 0, 5 + i)).toISOString().slice(0, 10));
  const setOf = (counts) => ({ set: "curve", charts: counts.map((count, c) => ({
    cursor: `c${c}`, k: 30, nCtx: 2, nReplay: 30, bars: BARS_UP, finished: true, finishedAt: 30, drawings: [], view: null,
    trades: Array.from({ length: count }, (_, j) => trade({ id: j + 1, placedAt: j % 30, stop: 90 + (j % 30), target: 101 + (j % 30) })),
    reveal: { symbol: "X", name: "X", kind: "stock", dates: DAYS, scale: 1, start: DAYS[2], end: DAYS[DAYS.length - 1] } })) });
  // The x labels: the chart each names, its left (% of the plot), and its custom properties per layout.
  function axis(state) {
    const html = buildReport(state, { generatedAt: GENERATED });
    const plot = /<div class="tt-plot"[^>]*>/.exec(html)[0];
    const labels = [...html.matchAll(/<span class="tt-x" style="left:([\d.]+)%([^"]*)"><span class="tt-x-word">Chart <\/span>(\d+)<\/span>/g)]
      .map(([, left, rest, chart]) => ({ chart: +chart, left: +left,
                                         vars: Object.fromEntries(rest.split(";").filter(Boolean).map((v) => v.split(":"))) }));
    assert.equal(html.match(/class="tt-x"/g).length, labels.length, "every label parsed");
    return { html, plot, labels };
  }

  it("label every chart, even two trades after thirty-eight (the verification round's set)", () => {
    const { labels } = axis(setOf([10, 9, 9, 10, 2]));
    assert.deepEqual(labels.map((l) => l.chart), [1, 2, 3, 4, 5]);
    // Each label sits under the middle of its band, between the dividers: chart 5 runs from 38.5 to 40 of 40.
    assert.equal(labels[4].left, 98.1);
    assert.equal(labels[0].left, 13.1);
  });

  it("say Chart N when every band holds it, and the number alone after a first Chart 1 when one doesn't", () => {
    const even = axis(setOf([10, 10, 10]));
    assert.ok(even.labels.every((l) => !Object.keys(l.vars).length));
    assert.ok(!even.plot.includes("style="));
    const { labels } = axis(setOf([10, 9, 9, 10, 2]));
    assert.deepEqual(labels[0].vars, {});
    for (const l of labels.slice(1)) assert.deepEqual(l.vars, { "--ww": "none", "--wm": "none", "--wn": "none" });
  });

  it("put labels that would touch on rows below, and the plot makes room for them", () => {
    const { plot, labels } = axis(setOf([1, 1, 1, 1, 1, 1, 1, 1, 1, 30]));
    assert.deepEqual(labels.map((l) => l.chart), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    for (const [key, px] of [["w", 820], ["m", 420], ["n", 200]]) {
      const row = (l) => +(l.vars[`--r${key}`] ?? 0), most = Math.max(...labels.map(row));
      assert.equal(plot.includes(`--x${key}:`), most > 0, key);
      if (most) assert.ok(plot.includes(`--x${key}:${most}`), key);
      // Within a row, one-digit labels (7px) keep 6px apart at that layout's plot width; "10" is two digits.
      for (let r = 0; r <= most; r++) {
        const inRow = labels.filter((l) => row(l) === r);
        for (let j = 1; j < inRow.length; j++) {
          const gap = ((inRow[j].left - inRow[j - 1].left) / 100) * px - (inRow[j].chart > 9 ? 10.5 : 7);
          assert.ok(gap >= 5.5, `${key} row ${r}: charts ${inRow[j - 1].chart} and ${inRow[j].chart} are ${gap}px apart`);
        }
      }
    }
    assert.ok(labels.some((l) => l.vars["--rn"]), "the phone layout needs a second row");
  });

  it("never drop a label, whatever the bands", () => {
    const rand = sim.mulberry32(7);
    for (let t = 0; t < 40; t++) {
      const counts = Array.from({ length: 1 + Math.floor(rand() * 10) }, () => 1 + Math.floor(rand() * (rand() < 0.3 ? 2 : 25)));
      assert.deepEqual(axis(setOf(counts)).labels.map((l) => l.chart), counts.map((_, c) => c + 1), counts.join(","));
    }
  });

  it("switch layouts with the page's width in the CSS", () => {
    const block = (query) => {
      const at = HTML.indexOf(`@media (${query}) {`);
      return HTML.slice(at, HTML.indexOf("\n}", at));
    };
    has(HTML, ".tt-x { --x-row: var(--rw, 0); --x-word: var(--ww, inline);");
    has(HTML, ".tt-plot { position: relative; height: 240px; margin: 4px 52px calc(28px + var(--xw, 0) * 15px) 0; }");
    has(block("max-width: 960px"), ".tt-plot { margin-bottom: calc(28px + var(--xm, 0) * 15px); }");
    has(block("max-width: 960px"), ".tt-x { --x-row: var(--rm, 0); --x-word: var(--wm, inline); }");
    has(block("max-width: 560px"), "margin-bottom: calc(28px + var(--xn, 0) * 15px); }");
    has(block("max-width: 560px"), ".tt-x { --x-row: var(--rn, 0); --x-word: var(--wn, inline); }");
    assert.ok(!HTML.includes("tt-x-tight"));
  });
});

describe("file names", () => {
  it("name the set", () => {
    assert.equal(reportFileName(STATE), "tradetest-report-3fa2c1d0.html");
    assert.equal(tradesCsvFileName(STATE), "tradetest-trades-3fa2c1d0.csv");
  });

  it("keep only safe characters", () => {
    assert.equal(reportFileName({ set: "../A b/c" }), "tradetest-report-abc.html");
    assert.equal(tradesCsvFileName({}), "tradetest-trades-set.csv");
    assert.equal(reportFileName(null), "tradetest-report-set.html");
  });
});
