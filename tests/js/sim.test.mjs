// TradeTest's trade simulation (src/stratlib/web/assets/tradetest/sim.js): every fill, exit and statistic rule, for
// longs and mirrored for shorts. Run with `node --test "tests/js/*.test.mjs"`.

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import * as sim from "../../src/stratlib/web/assets/tradetest/sim.js";

const {
  buyAndHold, chartSummary, fmtPct, fmtPrice, fmtR, kindFor, levelsAt, mulberry32, randomBaseline, refClose,
  scopeEnd, scoredResults, simulate, summarize, unfinishedClosed, validateChange, validateOrder,
} = sim;

const MINUS = String.fromCharCode(0x2212);
const NONE = String.fromCharCode(0x2013);

// A bar row as the server sends it: o, h, l, c, relative volume, then the moving averages.
const b = (o, h, l, c) => [o, h, l, c, 1, null, null, null, null];
// Two context bars; the last closes at 100, the price every order at k = 0 is placed at.
const CTX = [b(99, 100.5, 98.5, 99.5), b(99.5, 100.5, 99, 100)];
const N_CTX = CTX.length;

// A chart with these replay bars. Unfinished charts hold the context plus the k revealed bars; finished ones all
// of them, as after the reveal.
function chart(replay, { k = replay.length, nReplay = replay.length, finished = false, finishedAt, trades = [] } = {}) {
  const all = [...CTX, ...replay];
  return { bars: finished ? all : all.slice(0, N_CTX + k), nCtx: N_CTX, nReplay, k, finished,
           finishedAt: finished ? finishedAt ?? k : null, trades };
}

function trade(fields) {
  return { id: 1, side: "long", kind: "market", entry: null, stop: 95, target: 110, placedAt: 0, note: "", changes: [],
           cancelAt: null, closeAt: null, ...fields };
}

const near = (actual, expected, eps = 1e-12) =>
  assert.ok(Math.abs(actual - expected) <= eps, `expected ${expected}, got ${actual}`);

// Every number in a nested value, for the never-NaN checks.
function numbers(value, out = []) {
  if (typeof value === "number") out.push(value);
  else if (value && typeof value === "object") for (const v of Object.values(value)) numbers(v, out);
  return out;
}

// ---------------------------------------------------------------- the module itself

describe("module", () => {
  const source = readFileSync(new URL("../../src/stratlib/web/assets/tradetest/sim.js", import.meta.url), "utf8");
  const code = source.replace(/\/\/.*$/gm, "");

  it("is dependency-free and deterministic: no imports, no DOM, no Math.random", () => {
    assert.doesNotMatch(code, /^\s*import\b|\brequire\(|\bimport\(/m);
    assert.doesNotMatch(code, /\bMath\.random\b/);
    assert.doesNotMatch(code, /\b(document|window|localStorage|navigator|fetch)\b/);
  });

  it("documents every export in the API reference at the top", () => {
    const header = source.slice(0, source.indexOf("\nconst "));
    for (const name of Object.keys(sim)) assert.match(header, new RegExp(`\\b${name}\\b`), name);
  });
});

// ---------------------------------------------------------------- kinds and validation

describe("kindFor", () => {
  it("is a market order at the current price", () => {
    assert.equal(kindFor("long", null, 100), "market");
    assert.equal(kindFor("short", null, 100), "market");
    assert.equal(kindFor("long", 100, 100), "market");
    assert.equal(kindFor("short", 100, 100), "market");
  });

  it("long: an entry below the price is a limit, above it a stop entry", () => {
    assert.equal(kindFor("long", 98, 100), "limit");
    assert.equal(kindFor("long", 102, 100), "stop");
  });

  it("short: an entry above the price is a limit, below it a stop entry", () => {
    assert.equal(kindFor("short", 102, 100), "limit");
    assert.equal(kindFor("short", 98, 100), "stop");
  });
});

describe("validateOrder", () => {
  const order = (fields) => ({ side: "long", entry: null, stop: 95, target: 110, ...fields });

  it("accepts a long and a short of every kind", () => {
    assert.equal(validateOrder(order({}), 100), null);
    assert.equal(validateOrder(order({ entry: 98, stop: 96, target: 104 }), 100), null);
    assert.equal(validateOrder(order({ entry: 102, stop: 99, target: 110 }), 100), null);
    assert.equal(validateOrder(order({ side: "short", stop: 105, target: 90 }), 100), null);
    assert.equal(validateOrder(order({ side: "short", entry: 102, stop: 104, target: 96 }), 100), null);
    assert.equal(validateOrder(order({ side: "short", entry: 98, stop: 101, target: 90 }), 100), null);
  });

  it("long: the stop below and the target above the entry", () => {
    const stop = "The stop must be below the entry for a long.", target = "The target must be above the entry for a long.";
    assert.equal(validateOrder(order({ stop: 101 }), 100), stop);
    assert.equal(validateOrder(order({ stop: 100 }), 100), stop);
    assert.equal(validateOrder(order({ entry: 98, stop: 99, target: 104 }), 100), stop);
    assert.equal(validateOrder(order({ target: 99 }), 100), target);
    assert.equal(validateOrder(order({ target: 100 }), 100), target);
    assert.equal(validateOrder(order({ entry: 102, stop: 99, target: 101 }), 100), target);
  });

  it("short: the stop above and the target below the entry", () => {
    const stop = "The stop must be above the entry for a short.", target = "The target must be below the entry for a short.";
    assert.equal(validateOrder(order({ side: "short", stop: 99, target: 90 }), 100), stop);
    assert.equal(validateOrder(order({ side: "short", stop: 100, target: 90 }), 100), stop);
    assert.equal(validateOrder(order({ side: "short", stop: 105, target: 101 }), 100), target);
    assert.equal(validateOrder(order({ side: "short", stop: 105, target: 100 }), 100), target);
    assert.equal(validateOrder(order({ side: "short", entry: 98, stop: 101, target: 99 }), 100), target);
  });

  it("the stop at least 0.1% from the entry, exactly 0.1% allowed", () => {
    const tight = "The stop must be at least 0.1% away from the entry.";
    assert.equal(validateOrder(order({ stop: 99.9 }), 100), null);
    assert.equal(validateOrder(order({ stop: 99.91 }), 100), tight);
    assert.equal(validateOrder(order({ side: "short", stop: 100.1, target: 90 }), 100), null);
    assert.equal(validateOrder(order({ side: "short", stop: 100.09, target: 90 }), 100), tight);
    // Measured from the entry, not the current price.
    assert.equal(validateOrder(order({ entry: 98, stop: 97.902, target: 104 }), 100), null);
    assert.equal(validateOrder(order({ entry: 98, stop: 97.91, target: 104 }), 100), tight);
  });

  it("needs a side and real prices", () => {
    assert.equal(validateOrder(order({ side: "flat" }), 100), "Choose long or short.");
    assert.equal(validateOrder(order({ stop: null }), 100), "Enter a stop price.");
    assert.equal(validateOrder(order({ stop: NaN }), 100), "Enter a stop price.");
    assert.equal(validateOrder(order({ stop: -1 }), 100), "Enter a stop price.");
    assert.equal(validateOrder(order({ target: undefined }), 100), "Enter a target price.");
    assert.equal(validateOrder(order({ target: "110" }), 100), "Enter a target price.");
    assert.equal(validateOrder(order({ entry: 0 }), 100), "Enter an entry price, or choose the market.");
    assert.equal(validateOrder(order({ entry: Infinity }), 100), "Enter an entry price, or choose the market.");
    assert.equal(validateOrder(order({}), null), "The current price is not available.");
  });
});

describe("validateChange", () => {
  // A long filled at 100 on the first replay bar, which closed at 100.5; and the mirrored short.
  const longChart = chart([b(100, 101, 99, 100.5)]);
  const long = trade({});
  const shortChart = chart([b(100, 101, 99, 99.5)]);
  const short = trade({ side: "short", stop: 105, target: 90 });
  const change = (t, c, levels, close) => validateChange(t, simulate(t, c), levels, close);

  it("an open long keeps its stop below and its target above the current close", () => {
    assert.equal(simulate(long, longChart).status, "open");
    assert.equal(change(long, longChart, { stop: 99, target: 110 }, 100.5), null);
    assert.equal(change(long, longChart, { stop: 100.5, target: 110 }, 100.5), "The stop must be below the current price for a long.");
    assert.equal(change(long, longChart, { stop: 101, target: 110 }, 100.5), "The stop must be below the current price for a long.");
    assert.equal(change(long, longChart, { stop: 95, target: 100.5 }, 100.5), "The target must be above the current price for a long.");
    // Risk is fixed at the fill, so an open trade's stop may sit right under the price.
    assert.equal(change(long, longChart, { stop: 100.49, target: 110 }, 100.5), null);
  });

  it("an open short keeps its stop above and its target below the current close", () => {
    assert.equal(simulate(short, shortChart).status, "open");
    assert.equal(change(short, shortChart, { stop: 100, target: 95 }, 99.5), null);
    assert.equal(change(short, shortChart, { stop: 99.5, target: 90 }, 99.5), "The stop must be above the current price for a short.");
    assert.equal(change(short, shortChart, { stop: 105, target: 99.6 }, 99.5), "The target must be below the current price for a short.");
  });

  it("a pending limit or stop order keeps its levels either side of its entry", () => {
    const c = chart([b(100, 101, 99, 100.5)]);
    const limit = trade({ kind: "limit", entry: 98, stop: 96, target: 104, placedAt: 1 });
    assert.equal(simulate(limit, c).status, "pending");
    assert.equal(change(limit, c, { stop: 98, target: 104 }, 100.5), "The stop must be below the entry for a long.");
    assert.equal(change(limit, c, { stop: 96, target: 98 }, 100.5), "The target must be above the entry for a long.");
    assert.equal(change(limit, c, { stop: 97.95, target: 104 }, 100.5), "The stop must be at least 0.1% away from the entry.");
    // Against the entry, not the price: a target under the current close is fine for a limit buy at 98.
    assert.equal(change(limit, c, { stop: 97, target: 99 }, 100.5), null);
    const shortStop = trade({ side: "short", kind: "stop", entry: 99, stop: 101, target: 95, placedAt: 1 });
    assert.equal(simulate(shortStop, c).status, "pending");
    assert.equal(change(shortStop, c, { stop: 98.5, target: 95 }, 100.5), "The stop must be above the entry for a short.");
    assert.equal(change(shortStop, c, { stop: 101, target: 99.5 }, 100.5), "The target must be below the entry for a short.");
    assert.equal(change(shortStop, c, { stop: 102, target: 96 }, 100.5), null);
  });

  it("a pending market order keeps its levels either side of the current close", () => {
    const c = chart([b(100, 101, 99, 100.5)]);
    const market = trade({ placedAt: 1 });
    assert.equal(simulate(market, c).status, "pending");
    assert.equal(change(market, c, { stop: 100.6, target: 110 }, 100.5), "The stop must be below the entry for a long.");
    assert.equal(change(market, c, { stop: 100.45, target: 110 }, 100.5), "The stop must be at least 0.1% away from the entry.");
    assert.equal(change(market, c, { stop: 99, target: 104 }, 100.5), null);
  });

  it("only pending orders and open trades can change", () => {
    const closing = trade({ closeAt: 1 });
    assert.equal(change(closing, longChart, { stop: 99, target: 110 }, 100.5), "This trade closes at the next open and can't be changed.");
    const only = "Only pending orders and open trades can be changed.";
    const stopped = chart([b(100, 101, 94, 94.5)]);
    assert.equal(simulate(long, stopped).status, "closed");
    assert.equal(change(long, stopped, { stop: 90, target: 110 }, 94.5), only);
    assert.equal(change(trade({ cancelAt: 0 }), longChart, { stop: 90, target: 110 }, 100.5), only);
    assert.equal(change(trade({ stop: 100 }), longChart, { stop: 90, target: 110 }, 100.5), only);   // missed
    const expired = chart([b(100, 101, 99, 100.5)], { finished: true, finishedAt: 0 });
    assert.equal(change(long, expired, { stop: 90, target: 110 }, 100.5), only);
  });

  it("needs real prices", () => {
    assert.equal(change(long, longChart, { stop: null, target: 110 }, 100.5), "Enter a stop price.");
    assert.equal(change(long, longChart, { stop: 99, target: NaN }, 100.5), "Enter a target price.");
    assert.equal(change(long, longChart, { stop: 99, target: 110 }, null), "The current price is not available.");
  });
});

// ---------------------------------------------------------------- positions on the bar index

describe("refClose and scopeEnd", () => {
  it("an order at k is placed at the close of index nCtx + k - 1", () => {
    const c = chart([b(100, 101, 99, 100.5), b(100.5, 102, 100, 101.5)]);
    assert.equal(refClose(c, 0), 100);
    assert.equal(refClose(c, 1), 100.5);
    assert.equal(refClose(c), 101.5);
    assert.equal(refClose(c, 3), null);
  });

  it("walks through the current bar, or the bar before the finish point", () => {
    const replay = [b(100, 101, 99, 100.5), b(100.5, 102, 100, 101.5), b(101.5, 102, 101, 101.8)];
    assert.equal(scopeEnd(chart(replay, { k: 0 })), N_CTX - 1);
    assert.equal(scopeEnd(chart(replay, { k: 2 })), N_CTX + 1);
    assert.equal(scopeEnd(chart(replay, { k: 1, finished: true })), N_CTX);
    assert.equal(scopeEnd(chart(replay, { k: 3, finished: true })), N_CTX + 2);
  });
});

// ---------------------------------------------------------------- market orders

describe("market orders", () => {
  it("wait for the next bar, then fill at its open", () => {
    const placed = simulate(trade({}), chart([], { nReplay: 5 }));
    assert.equal(placed.status, "pending");
    assert.deepEqual([placed.fill, placed.r, placed.risk, placed.pct, placed.barsHeld], [null, null, null, null, null]);
    const res = simulate(trade({}), chart([b(101, 102, 100.5, 101.5)]));
    assert.equal(res.status, "open");
    assert.deepEqual(res.fill, { i: N_CTX, price: 101 });
  });

  it("act from the bar after the one they were placed on", () => {
    // Placed at k = 1: the first bar's gap through the stop came before the order existed.
    const res = simulate(trade({ placedAt: 1 }), chart([b(94, 95, 93, 94.5), b(96, 97, 95.5, 96.5)]));
    assert.deepEqual(res.fill, { i: N_CTX + 1, price: 96 });
  });

  it("long: an open at or below the stop misses (gap_stop), at or above the target misses (gap_target)", () => {
    for (const open of [95, 94]) {
      const res = simulate(trade({}), chart([b(open, open + 1, open - 1, open)]));
      assert.equal(res.status, "missed");
      assert.deepEqual(res.missed, { i: N_CTX, reason: "gap_stop" });
      assert.equal(res.fill, null);
    }
    for (const open of [110, 111]) {
      const res = simulate(trade({}), chart([b(open, open + 1, open - 1, open)]));
      assert.deepEqual([res.status, res.missed], ["missed", { i: N_CTX, reason: "gap_target" }]);
    }
  });

  it("short: an open at or above the stop misses (gap_stop), at or below the target misses (gap_target)", () => {
    const short = trade({ side: "short", stop: 105, target: 90 });
    for (const open of [105, 106]) assert.deepEqual(simulate(short, chart([b(open, open + 1, open - 1, open)])).missed, { i: N_CTX, reason: "gap_stop" });
    for (const open of [90, 89]) assert.deepEqual(simulate(short, chart([b(open, open + 1, open - 1, open)])).missed, { i: N_CTX, reason: "gap_target" });
    assert.deepEqual(simulate(short, chart([b(100.5, 101, 99, 100)])).fill, { i: N_CTX, price: 100.5 });
  });

  it("only ever act on their first bar", () => {
    const res = simulate(trade({}), chart([b(100, 101, 99, 100.5), b(94, 95, 93, 94)]));
    assert.deepEqual(res.fill, { i: N_CTX, price: 100 });
    assert.equal(res.missed, null);
  });
});

// ---------------------------------------------------------------- limit orders

describe("limit orders", () => {
  const long = trade({ kind: "limit", entry: 98, stop: 96, target: 104 });
  const short = trade({ side: "short", kind: "limit", entry: 102, stop: 104, target: 96 });

  it("long: fill when the low reaches the entry, at the lower of the open and the entry", () => {
    const touched = simulate(long, chart([b(100, 100.5, 99, 99.5), b(99, 99.5, 97.5, 98.5)]));
    assert.deepEqual(touched.fill, { i: N_CTX + 1, price: 98 });
    assert.deepEqual(simulate(long, chart([b(97, 98, 96.5, 97.5)])).fill, { i: N_CTX, price: 97 });   // gap below the entry
    assert.deepEqual(simulate(long, chart([b(98, 99, 97.9, 98.5)])).fill, { i: N_CTX, price: 98 });   // opens on it
    assert.deepEqual(simulate(long, chart([b(99, 99.5, 98, 99)])).fill, { i: N_CTX, price: 98 });     // low exactly on it
    const above = simulate(long, chart([b(99, 99.5, 98.0001, 99)]));
    assert.deepEqual([above.status, above.fill], ["pending", null]);
  });

  it("short: fill when the high reaches the entry, at the higher of the open and the entry", () => {
    assert.deepEqual(simulate(short, chart([b(101, 102.5, 100.5, 101)])).fill, { i: N_CTX, price: 102 });
    assert.deepEqual(simulate(short, chart([b(103, 103.5, 102, 102.5)])).fill, { i: N_CTX, price: 103 });
    assert.deepEqual(simulate(short, chart([b(101, 102, 100.5, 101)])).fill, { i: N_CTX, price: 102 });
    assert.equal(simulate(short, chart([b(101, 101.9999, 100.5, 101)])).status, "pending");
  });

  it("a bar that opens through the stop before the fill misses the order (gap_stop)", () => {
    for (const open of [96, 95.5]) {
      const res = simulate(long, chart([b(open, 97, 95, 96.5)]));
      assert.deepEqual([res.status, res.missed, res.fill], ["missed", { i: N_CTX, reason: "gap_stop" }, null]);
    }
    // On any bar while it waits, not just the first.
    assert.deepEqual(simulate(long, chart([b(100, 100.5, 99, 99.5), b(95, 96, 94, 95)])).missed, { i: N_CTX + 1, reason: "gap_stop" });
    for (const open of [104, 105]) assert.deepEqual(simulate(short, chart([b(open, open + 1, 101, open)])).missed, { i: N_CTX, reason: "gap_stop" });
  });

  it("price running to the target without touching the entry leaves the order working", () => {
    const res = simulate(long, chart([b(100, 105, 99, 104.5)]));
    assert.equal(res.status, "pending");
  });
});

// ---------------------------------------------------------------- stop entries

describe("stop entries", () => {
  const long = trade({ kind: "stop", entry: 102, stop: 99, target: 110 });
  const short = trade({ side: "short", kind: "stop", entry: 98, stop: 101, target: 90 });

  it("long: fill when the high reaches the entry, at the higher of the open and the entry", () => {
    assert.deepEqual(simulate(long, chart([b(101, 103, 100.5, 102.5)])).fill, { i: N_CTX, price: 102 });
    assert.deepEqual(simulate(long, chart([b(104, 105, 103.5, 104.5)])).fill, { i: N_CTX, price: 104 });
    assert.deepEqual(simulate(long, chart([b(101, 102, 100.5, 101.5)])).fill, { i: N_CTX, price: 102 });
    assert.equal(simulate(long, chart([b(101, 101.99, 100.5, 101.5)])).status, "pending");
  });

  it("short: fill when the low reaches the entry, at the lower of the open and the entry", () => {
    assert.deepEqual(simulate(short, chart([b(99, 99.5, 97.5, 98)])).fill, { i: N_CTX, price: 98 });
    assert.deepEqual(simulate(short, chart([b(97, 97.5, 96, 96.5)])).fill, { i: N_CTX, price: 97 });
    assert.equal(simulate(short, chart([b(99, 99.5, 98.01, 98.5)])).status, "pending");
  });

  it("a bar that opens at or through the target before the fill misses the order (gap_target)", () => {
    for (const open of [110, 112]) assert.deepEqual(simulate(long, chart([b(open, open + 1, open - 1, open)])).missed, { i: N_CTX, reason: "gap_target" });
    for (const open of [90, 88]) assert.deepEqual(simulate(short, chart([b(open, open + 1, open - 1, open)])).missed, { i: N_CTX, reason: "gap_target" });
  });

  it("have no gap-stop rule: an open through the stop leaves the order working", () => {
    const c = chart([b(98, 98.5, 97, 98), b(99, 102.5, 98.5, 102)]);
    const res = simulate(long, c);
    assert.deepEqual(res.fill, { i: N_CTX + 1, price: 102 });
    // The fill bar's low is under the stop, so it is out at the stop on the same bar.
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 99, reason: "stop" });
  });
});

// ---------------------------------------------------------------- the fill bar

describe("the fill bar", () => {
  it("long: only the stop can exit; a bar through both is the worse outcome", () => {
    const limit = trade({ kind: "limit", entry: 98, stop: 96, target: 104 });
    const both = simulate(limit, chart([b(99, 105, 95.5, 100)]));
    assert.deepEqual(both.fill, { i: N_CTX, price: 98 });
    assert.deepEqual(both.exit, { i: N_CTX, price: 96, reason: "stop" });
    assert.equal(both.status, "closed");
    near(both.r, -1);
    assert.equal(both.barsHeld, 0);
    // Through the target but not the stop: no target exit on the fill bar; the next bar opens at the target.
    const c = chart([b(99, 105, 97, 104.5), b(104, 104.5, 103, 104)]);
    assert.equal(simulate(limit, chart([b(99, 105, 97, 104.5)])).status, "open");
    assert.deepEqual(simulate(limit, c).exit, { i: N_CTX + 1, price: 104, reason: "target" });
  });

  it("a market fill can stop out on its own bar", () => {
    const res = simulate(trade({ stop: 99, target: 110 }), chart([b(100, 101, 98.5, 100)]));
    assert.deepEqual([res.fill, res.exit], [{ i: N_CTX, price: 100 }, { i: N_CTX, price: 99, reason: "stop" }]);
  });

  it("short: only the stop can exit", () => {
    const limit = trade({ side: "short", kind: "limit", entry: 102, stop: 104, target: 96 });
    const res = simulate(limit, chart([b(101, 104.5, 95, 100)]));
    assert.deepEqual([res.fill, res.exit], [{ i: N_CTX, price: 102 }, { i: N_CTX, price: 104, reason: "stop" }]);
    assert.equal(simulate(limit, chart([b(101, 103, 95, 96)])).status, "open");
  });
});

// ---------------------------------------------------------------- later bars

describe("an open position on later bars", () => {
  // Long filled at 100 (stop 98, target 106) and short filled at 100 (stop 102, target 94), each on the first bar.
  const FILL = b(100, 101, 99, 100.5);
  const long = trade({ stop: 98, target: 106 });
  const short = trade({ side: "short", stop: 102, target: 94 });
  const exitOf = (t, next) => simulate(t, chart([FILL, next])).exit;

  it("a close request exits at the next open, before any other rule", () => {
    assert.deepEqual(exitOf({ ...long, closeAt: 1 }, b(101, 107, 97, 105)), { i: N_CTX + 1, price: 101, reason: "close" });
    assert.deepEqual(exitOf({ ...long, closeAt: 1 }, b(97, 98, 96, 97)), { i: N_CTX + 1, price: 97, reason: "close" });
    assert.deepEqual(exitOf({ ...short, closeAt: 1 }, b(99, 103, 93, 95)), { i: N_CTX + 1, price: 99, reason: "close" });
  });

  it("long: an open at or under the stop exits at the open, losing more than 1R on a gap", () => {
    const res = simulate(long, chart([FILL, b(95, 96, 94, 95.5)]));
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 95, reason: "stop" });
    near(res.r, -2.5);
    assert.deepEqual(exitOf(long, b(98, 99, 97.5, 98.5)), { i: N_CTX + 1, price: 98, reason: "stop" });
  });

  it("long: an open at or over the target exits at the open", () => {
    const res = simulate(long, chart([FILL, b(107, 108, 106.5, 107)]));
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 107, reason: "target" });
    near(res.r, 3.5);
    assert.deepEqual(exitOf(long, b(106, 107, 105, 106)), { i: N_CTX + 1, price: 106, reason: "target" });
  });

  it("long: within the bar the stop comes before the target", () => {
    assert.deepEqual(exitOf(long, b(100, 106.5, 97.5, 100)), { i: N_CTX + 1, price: 98, reason: "stop" });
    assert.deepEqual(exitOf(long, b(101, 106, 100, 105)), { i: N_CTX + 1, price: 106, reason: "target" });
    assert.deepEqual(exitOf(long, b(100, 101, 98, 99)), { i: N_CTX + 1, price: 98, reason: "stop" });
    assert.equal(exitOf(long, b(101, 105.99, 98.01, 103)), null);
  });

  it("short: the same rules mirrored", () => {
    const gapStop = simulate(short, chart([FILL, b(105, 106, 104, 105)]));
    assert.deepEqual(gapStop.exit, { i: N_CTX + 1, price: 105, reason: "stop" });
    near(gapStop.r, -2.5);
    const gapTarget = simulate(short, chart([FILL, b(93, 94, 92, 93)]));
    assert.deepEqual(gapTarget.exit, { i: N_CTX + 1, price: 93, reason: "target" });
    near(gapTarget.r, 3.5);
    assert.deepEqual(exitOf(short, b(100, 102.5, 93.5, 100)), { i: N_CTX + 1, price: 102, reason: "stop" });
    assert.deepEqual(exitOf(short, b(99, 100, 94, 95)), { i: N_CTX + 1, price: 94, reason: "target" });
    assert.equal(exitOf(short, b(99, 101.99, 94.01, 97)), null);
  });

  it("stays open until a rule fires, then stops walking", () => {
    const res = simulate(long, chart([FILL, b(100.5, 102, 99.5, 101), b(101, 107, 100.5, 106.5), b(90, 91, 89, 90)]));
    assert.deepEqual(res.exit, { i: N_CTX + 2, price: 106, reason: "target" });
    assert.equal(res.barsHeld, 2);
  });
});

// ---------------------------------------------------------------- changes

describe("stop and target changes", () => {
  it("the latest change made by bar i is in effect at bar i", () => {
    const t = trade({ changes: [{ at: 2, stop: 97, target: 112 }, { at: 4, stop: 99, target: 111 }] });
    assert.deepEqual(levelsAt(t, N_CTX + 1, N_CTX), { stop: 95, target: 110 });
    assert.deepEqual(levelsAt(t, N_CTX + 2, N_CTX), { stop: 97, target: 112 });
    assert.deepEqual(levelsAt(t, N_CTX + 3, N_CTX), { stop: 97, target: 112 });
    assert.deepEqual(levelsAt(t, N_CTX + 4, N_CTX), { stop: 99, target: 111 });
    // Two changes on one bar: the later one.
    const twice = trade({ changes: [{ at: 1, stop: 96, target: 110 }, { at: 1, stop: 97, target: 108 }] });
    assert.deepEqual(levelsAt(twice, N_CTX + 1, N_CTX), { stop: 97, target: 108 });
  });

  it("a change made at k acts from bar nCtx + k, never on bars already shown", () => {
    // Filled at 100 on a bar whose low is 98.8; at k = 1 the stop moves up to 99.
    const replay = [b(100, 101, 98.8, 100.5), b(100, 101, 98.9, 100), b(100, 101, 98.9, 100)];
    const moved = simulate(trade({ changes: [{ at: 1, stop: 99, target: 110 }] }), chart(replay));
    assert.deepEqual(moved.exit, { i: N_CTX + 1, price: 99, reason: "stop" });
    const later = simulate(trade({ changes: [{ at: 2, stop: 99, target: 110 }] }), chart(replay));
    assert.deepEqual(later.exit, { i: N_CTX + 2, price: 99, reason: "stop" });
    const target = simulate(trade({ changes: [{ at: 1, stop: 95, target: 100.9 }] }), chart(replay));
    assert.deepEqual(target.exit, { i: N_CTX + 1, price: 100.9, reason: "target" });
  });

  it("stopNow and targetNow show the latest change, including one made at the current k", () => {
    const t = trade({ changes: [{ at: 1, stop: 99, target: 108 }] });
    const res = simulate(t, chart([b(100, 101, 99.5, 100.5)]));
    assert.equal(res.status, "open");
    assert.deepEqual([res.stopNow, res.targetNow], [99, 108]);
    assert.deepEqual([simulate(trade({}), chart([b(100, 101, 99.5, 100.5)])).stopNow], [95]);
  });

  it("risk is set by the stop in effect on the fill bar; later moves don't change it", () => {
    const limit = trade({ kind: "limit", entry: 98, stop: 97, target: 104, changes: [{ at: 1, stop: 96, target: 104 }] });
    const res = simulate(limit, chart([b(100, 100.5, 99, 99.5), b(99, 99.5, 97.5, 98.5)]));
    assert.deepEqual(res.fill, { i: N_CTX + 1, price: 98 });
    near(res.risk, 2);
    const trailed = simulate(trade({ changes: [{ at: 1, stop: 99, target: 110 }] }), chart([b(100, 101, 99.5, 100.5)]));
    near(trailed.risk, 5);
  });

  it("a change at placement applies to a market order's first bar", () => {
    const res = simulate(trade({ placedAt: 1, changes: [{ at: 1, stop: 99.5, target: 110 }] }), chart([b(100, 101, 99.5, 100.5), b(99.4, 100, 99, 99.5)]));
    assert.deepEqual(res.missed, { i: N_CTX + 1, reason: "gap_stop" });
  });

  it("a change that leaves one level out keeps it", () => {
    assert.deepEqual(levelsAt(trade({ changes: [{ at: 1, stop: 97 }] }), N_CTX + 1, N_CTX), { stop: 97, target: 110 });
  });
});

// ---------------------------------------------------------------- cancel and close

describe("cancel", () => {
  const limit = trade({ kind: "limit", entry: 98, stop: 96, target: 104 });

  it("stops a pending order from the bar after the request", () => {
    const res = simulate({ ...limit, cancelAt: 1 }, chart([b(100, 100.5, 99, 99.5), b(99, 99.5, 97.5, 98.5)]));
    assert.deepEqual([res.status, res.fill, res.cancelled], ["cancelled", null, { i: N_CTX + 1 }]);
    assert.equal(res.r, null);
  });

  it("is too late once the order has filled", () => {
    const res = simulate({ ...limit, cancelAt: 1 }, chart([b(99, 99.5, 97.5, 98.5), b(98.5, 99, 98, 98.5)]));
    assert.equal(res.status, "open");
    assert.deepEqual(res.fill, { i: N_CTX, price: 98 });
    assert.equal(res.cancelled, null);
  });

  it("shows at once, before the next bar is revealed", () => {
    const res = simulate({ ...limit, cancelAt: 1 }, chart([b(100, 100.5, 99, 99.5)]));
    assert.deepEqual([res.status, res.cancelled], ["cancelled", { i: N_CTX + 1 }]);
    const market = simulate(trade({ cancelAt: 0 }), chart([b(100, 101, 99, 100.5)]));
    assert.deepEqual([market.status, market.fill], ["cancelled", null]);
  });

  it("stays cancelled, not expired, when the chart is finished on the same bar", () => {
    const replay = [b(100, 100.5, 99, 99.5), b(99, 99.5, 97.5, 98.5)];
    assert.equal(simulate({ ...limit, cancelAt: 1 }, chart(replay, { k: 1, finished: true })).status, "cancelled");
  });
});

describe("close requests", () => {
  const replay = [b(100, 101, 99, 100.5), b(101, 102, 100.5, 101.5), b(102, 103, 101.5, 102.5)];

  it("show as closing until the next bar, marked to the current close", () => {
    const res = simulate(trade({ closeAt: 1 }), chart(replay, { k: 1, nReplay: 3 }));
    assert.equal(res.status, "closing");
    assert.equal(res.exit, null);
    assert.equal(res.mark, 100.5);
    near(res.r, 0.1);
  });

  it("exit at the next bar's open", () => {
    const res = simulate(trade({ closeAt: 1 }), chart(replay, { k: 2, nReplay: 3 }));
    assert.equal(res.status, "closed");
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 101, reason: "close" });
  });

  it("at the very end of the replay wait for the finish, then go out at the last close", () => {
    assert.equal(simulate(trade({ closeAt: 3 }), chart(replay)).status, "closing");
    const res = simulate(trade({ closeAt: 3 }), chart(replay, { finished: true }));
    assert.deepEqual(res.exit, { i: N_CTX + 2, price: 102.5, reason: "end" });
  });
});

// ---------------------------------------------------------------- finishing a chart

describe("finish", () => {
  const replay = [b(100, 101, 99, 100.5), b(101, 102, 90, 91), b(91, 92, 80, 81)];

  it("an open position exits at the open after the finish point; later bars don't count", () => {
    const res = simulate(trade({}), chart(replay, { k: 1, finished: true }));
    assert.equal(res.status, "closed");
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 101, reason: "finish" });
    near(res.r, 0.2);
    assert.equal(res.barsHeld, 1);
  });

  it("at the end of the replay an open position exits at the last close", () => {
    const up = [b(100, 101, 99, 100.5), b(101, 102, 100, 101.5), b(102, 103, 101, 102.5)];
    const res = simulate(trade({}), chart(up, { finished: true }));
    assert.deepEqual(res.exit, { i: N_CTX + 2, price: 102.5, reason: "end" });
    assert.equal(res.barsHeld, 2);
  });

  it("a close requested on the finish bar keeps its reason", () => {
    const res = simulate(trade({ closeAt: 1 }), chart(replay, { k: 1, finished: true }));
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 101, reason: "close" });
  });

  it("an unfilled order expires", () => {
    const limit = trade({ kind: "limit", entry: 98, stop: 96, target: 104 });
    const res = simulate(limit, chart(replay, { k: 1, finished: true }));
    assert.deepEqual([res.status, res.fill, res.exit], ["expired", null, null]);
    // So does a market order placed on the finish bar, which never saw its next open.
    assert.equal(simulate(trade({ placedAt: 1 }), chart(replay, { k: 1, finished: true })).status, "expired");
  });

  it("a trade already closed is unchanged", () => {
    const res = simulate(trade({ stop: 99.5, target: 110 }), chart(replay, { k: 2, finished: true }));
    assert.deepEqual(res.exit, { i: N_CTX, price: 99.5, reason: "stop" });
  });

  it("waits as closing if the bars after the finish point are not loaded yet", () => {
    const c = chart(replay, { k: 1 });
    const res = simulate(trade({}), { ...c, finished: true, finishedAt: 1 });
    assert.equal(res.status, "closing");
    assert.equal(res.mark, 100.5);
  });
});

// ---------------------------------------------------------------- R arithmetic

describe("R, net R, % and bars held", () => {
  it("long: a 2-point risk exiting 6 points up is +3R", () => {
    const replay = [b(100, 101, 99, 100.5), b(100.5, 102, 100, 101.5), b(101.5, 103, 101, 102.5), b(102.5, 106.5, 102, 106)];
    const res = simulate(trade({ stop: 98, target: 106 }), chart(replay));
    assert.deepEqual(res.exit, { i: N_CTX + 3, price: 106, reason: "target" });
    near(res.risk, 2);
    near(res.r, 3);
    near(res.rNet, 3 - 0.001 * (100 + 106) / 2);
    near(res.pct, 6);
    assert.equal(res.barsHeld, 3);
    assert.equal(res.mark, 106);
  });

  it("long: stopped out is -1R less costs", () => {
    const res = simulate(trade({ stop: 98, target: 106 }), chart([b(100, 101, 99, 100.5), b(100, 100.5, 97, 97.5)]));
    near(res.r, -1);
    near(res.rNet, -1 - 0.001 * (100 + 98) / 2);
    near(res.pct, -2);
  });

  it("short: profit when price falls, in R and in %", () => {
    const c = chart([b(100, 100.5, 99.5, 99.8), b(99.8, 100, 96.5, 97)]);
    const res = simulate(trade({ side: "short", stop: 101, target: 97 }), c);
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 97, reason: "target" });
    near(res.risk, 1);
    near(res.r, 3);
    near(res.rNet, 3 - 0.001 * (100 + 97) / 1);
    near(res.pct, 3);
    assert.equal(res.barsHeld, 1);
  });

  it("an open trade marks to the current close and counts bars to the current bar", () => {
    const res = simulate(trade({ stop: 98, target: 110 }), chart([b(100, 101, 99, 100.5), b(100.5, 102, 100, 101.5)]));
    assert.equal(res.status, "open");
    assert.equal(res.mark, 101.5);
    near(res.r, 0.75);
    near(res.pct, 1.5);
    assert.equal(res.barsHeld, 1);
  });

  it("risk has a floor of 0.1% of the planned entry", () => {
    // A stop 0.05 under a planned 100 can't be placed, but saved state is not validated again.
    const replay = [b(100.02, 100.5, 99.99, 100.3), b(100.3, 100.8, 100.2, 100.7)];
    const res = simulate(trade({ stop: 99.95, target: 100.6 }), chart(replay));
    assert.deepEqual(res.fill, { i: N_CTX, price: 100.02 });
    assert.deepEqual(res.exit, { i: N_CTX + 1, price: 100.6, reason: "target" });
    near(res.risk, 0.1, 1e-12);
    near(res.r, (100.6 - 100.02) / 0.1, 1e-9);
    near(res.rNet, res.r - 0.001 * (100.02 + 100.6) / 0.1, 1e-9);
  });
});

// ---------------------------------------------------------------- risk from the planned entry

// The position is sized when the order is placed: risk runs from the planned entry (the close a market order was
// placed at, the entry of a limit or stop entry) to the stop in effect on the fill bar, so a gap fill shows up as
// slippage in R and never shrinks the risk.
describe("risk from the planned entry", () => {
  it("a market long planned at 100 with a 5% stop that fills at 95.05 is about +3R at its target, not +157R", () => {
    // The fill-risk example from the verification round: planned 2R, the next open lands just above the stop.
    const c = chart([b(95.05, 96, 95.02, 95.8), b(96, 111, 95.9, 110.5)], { finished: true, trades: [trade({ stop: 95, target: 110 })] });
    const res = simulate(c.trades[0], c);
    assert.deepEqual([res.status, res.planned, res.fill, res.exit],
      ["closed", 100, { i: N_CTX, price: 95.05 }, { i: N_CTX + 1, price: 110, reason: "target" }]);
    near(res.risk, 5);
    near(res.r, (110 - 95.05) / 5, 1e-12);   // 2.99R: the planned 2R plus 0.99R of the better fill
    near(res.rNet, res.r - 0.001 * (95.05 + 110) / 5, 1e-12);
    near(res.pct, 100 * (110 / 95.05 - 1), 1e-9);
    assert.ok(res.r < 3);
    near(summarize(scoredResults([c])).dollars, 1000 * res.r, 1e-9);
  });

  it("the mirrored short scores the same", () => {
    const c = chart([b(104.95, 104.98, 104, 104.2), b(104, 104.1, 89, 89.5)]);
    const res = simulate(trade({ side: "short", stop: 105, target: 90 }), c);
    assert.deepEqual([res.planned, res.fill, res.exit], [100, { i: N_CTX, price: 104.95 }, { i: N_CTX + 1, price: 90, reason: "target" }]);
    near(res.risk, 5);
    near(res.r, (104.95 - 90) / 5, 1e-12);
  });

  it("a fill worse than planned is less R: a market long gapping up, a stop entry gapping through its entry", () => {
    const up = simulate(trade({ stop: 95, target: 110 }), chart([b(102, 103, 101.5, 102.5), b(103, 111, 102.5, 110.5)]));
    assert.deepEqual([up.fill.price, up.exit.price], [102, 110]);
    near(up.risk, 5);
    near(up.r, 8 / 5);
    // A buy stop at 102 with the stop at 99: the bar opens at 104, so a stop-out loses 5 points on a planned 3.
    const stopEntry = trade({ kind: "stop", entry: 102, stop: 99, target: 110 });
    const gapped = simulate(stopEntry, chart([b(104, 104.5, 103, 103.5), b(103, 103.5, 98.5, 99)]));
    assert.deepEqual([gapped.planned, gapped.fill.price, gapped.exit], [102, 104, { i: N_CTX + 1, price: 99, reason: "stop" }]);
    near(gapped.risk, 3);
    near(gapped.r, -5 / 3);
  });

  it("a fill better than planned is more R: a limit long that gaps under its entry loses less at the stop", () => {
    const limit = trade({ kind: "limit", entry: 98, stop: 96, target: 104 });
    const res = simulate(limit, chart([b(97, 97.5, 96.8, 97.2), b(97, 97.2, 95.5, 96)]));
    assert.deepEqual([res.planned, res.fill.price, res.exit.price], [98, 97, 96]);
    near(res.risk, 2);
    near(res.r, -0.5);
  });

  it("a market order placed later is planned at the close of the bar it was placed on", () => {
    const c = chart([b(100, 101, 99, 100.5), b(101, 102, 100.8, 101.5)]);
    const res = simulate(trade({ stop: 98.5, target: 105, placedAt: 1 }), c);
    assert.deepEqual([res.planned, res.fill.price], [100.5, 101]);
    near(res.risk, 2);
    near(res.r, 0.25);
  });

  it("planned is there before the fill, and for orders that never fill", () => {
    const c = chart([b(100, 101, 99, 100.5)]);
    assert.equal(simulate(trade({ placedAt: 1 }), c).planned, 100.5);
    assert.equal(simulate(trade({ kind: "limit", entry: 95.5, stop: 94, target: 104 }), c).planned, 95.5);
    assert.equal(simulate(trade({ stop: 100 }), c).planned, 100);   // missed on a gap
    assert.equal(simulate(trade({ kind: "limit", entry: null, stop: 94, target: 104 }), c).planned, null);
  });
});

// ---------------------------------------------------------------- shorts mirror longs

// Random charts and orders, simulated as given and reflected through a price M (high and low swapped, sides
// flipped): every decision must come out the same.
describe("shorts mirror longs", () => {
  const M = 300;
  const round2 = (x) => Math.round(x * 100) / 100;

  function scenario(rand) {
    const nReplay = 30, bars = [];
    let close = 100;
    for (let i = 0; i < N_CTX + nReplay; i++) {
      const o = round2(close * (1 + (rand() - 0.5) * 0.04));
      const c = round2(o * (1 + (rand() - 0.5) * 0.05));
      const h = round2(Math.max(o, c) * (1 + rand() * 0.015)), l = round2(Math.min(o, c) * (1 - rand() * 0.015));
      bars.push([o, h, l, c, 1, null, null, null, null]);
      close = c;
    }
    const k = Math.floor(rand() * (nReplay + 1)), finished = rand() < 0.4;
    const placedAt = Math.floor(rand() * (k + 1)), side = rand() < 0.5 ? "long" : "short", s = side === "long" ? 1 : -1;
    const ref = bars[N_CTX + placedAt - 1][3], pick = rand();
    const kind = pick < 0.34 ? "market" : pick < 0.67 ? "limit" : "stop";
    const entry = kind === "market" ? null : round2(ref * (1 + (kind === "limit" ? -s : s) * (0.002 + rand() * 0.03)));
    const e = entry ?? ref;
    const stop = round2(e * (1 - s * (0.006 + rand() * 0.04))), target = round2(e * (1 + s * (0.006 + rand() * 0.08)));
    const t = { id: 1, side, kind, entry, stop, target, placedAt, note: "", changes: [], cancelAt: null, closeAt: null };
    const later = () => placedAt + Math.floor(rand() * (k - placedAt + 1));
    if (rand() < 0.3) t.changes.push({ at: later(), stop: round2(e * (1 - s * (0.006 + rand() * 0.03))), target: round2(e * (1 + s * (0.006 + rand() * 0.06))) });
    if (rand() < 0.15) t.cancelAt = later();
    if (rand() < 0.2) t.closeAt = later();
    const c = { bars: finished ? bars : bars.slice(0, N_CTX + k), nCtx: N_CTX, nReplay, k, finished, finishedAt: finished ? k : null };
    return [t, c];
  }

  function mirror([t, c]) {
    const flip = (p) => (p == null ? p : M - p);
    return [{ ...t, side: t.side === "long" ? "short" : "long", entry: flip(t.entry), stop: flip(t.stop), target: flip(t.target),
              changes: t.changes.map((ch) => ({ at: ch.at, stop: flip(ch.stop), target: flip(ch.target) })) },
            { ...c, bars: c.bars.map(([o, h, l, cl, ...rest]) => [M - o, M - l, M - h, M - cl, ...rest]) }];
  }

  it("gives the same status, fills, exits and R for 4,000 random trades", () => {
    const rand = mulberry32(7), seen = new Set();
    for (let n = 0; n < 4000; n++) {
      const [t, c] = scenario(rand), [mt, mc] = mirror([t, c]);
      const a = simulate(t, c), m = simulate(mt, mc), why = JSON.stringify([t, a]);
      seen.add(a.status).add(a.exit?.reason).add(a.missed?.reason);
      assert.equal(m.status, a.status, why);
      assert.deepEqual(m.missed, a.missed, why);
      assert.deepEqual(m.cancelled, a.cancelled, why);
      // Fills and exits happen at an open, close, entry, stop or target, so the reflected price is exactly M - price.
      assert.deepEqual(m.fill, a.fill && { i: a.fill.i, price: M - a.fill.price }, why);
      assert.deepEqual(m.exit, a.exit && { ...a.exit, price: M - a.exit.price }, why);
      assert.equal(m.barsHeld, a.barsHeld, why);
      assert.equal(m.planned, M - a.planned, why);
      if (!a.fill) continue;
      // The risk floor is a share of the planned entry, which the reflection changes, so R is compared only above it.
      const stop = levelsAt(t, a.fill.i, N_CTX).stop, floor = 0.001 * Math.max(a.planned, M - a.planned);
      if (Math.abs(a.planned - stop) > floor) near(m.r, a.r, 1e-9);
    }
    for (const what of ["pending", "open", "closing", "closed", "cancelled", "missed", "expired", "stop", "target",
                        "close", "finish", "end", "gap_stop", "gap_target"]) assert.ok(seen.has(what), `never saw ${what}`);
  });

  it("fills and exits only at prices the bar traded, and never yields NaN", () => {
    const rand = mulberry32(11);
    for (let n = 0; n < 4000; n++) {
      const [t, c] = scenario(rand), res = simulate(t, c), why = JSON.stringify([t, res]);
      assert.ok(numbers(res).every((x) => !Number.isNaN(x)), why);
      for (const point of [res.fill, res.exit]) {
        if (!point) continue;
        const [, h, l] = c.bars[point.i];
        assert.ok(l <= point.price && point.price <= h, why);
      }
      if (res.fill) assert.ok(Number.isFinite(res.r) && Number.isFinite(res.rNet) && res.risk > 0, why);
      if (res.exit) assert.ok(res.exit.i >= res.fill.i, why);
    }
  });
});

// ---------------------------------------------------------------- chart statistics

describe("chartSummary", () => {
  const replay = [b(100, 101, 99, 100.5), b(100.5, 104, 100, 103.5), b(104, 104.5, 102.5, 103)];
  const trades = [
    trade({ id: 1, stop: 98, target: 103.5 }),                                       // target on bar 2: +1.75R
    trade({ id: 2, side: "short", stop: 102, target: 95 }),                          // stopped on bar 2: -1R
    trade({ id: 3, stop: 98, placedAt: 1 }),                                         // open from 100.5: +1R
    trade({ id: 4, kind: "limit", entry: 95, stop: 93, target: 110 }),               // never reached
    trade({ id: 5, kind: "limit", entry: 99.5, stop: 97, target: 110, cancelAt: 0 }),
    trade({ id: 6, stop: 101, target: 103.9, placedAt: 2 }),                         // gap through the target
    trade({ id: 7, stop: 95, target: 120, closeAt: 3 }),                             // closing: +0.6R
  ];

  it("counts every status and totals closed and open R", () => {
    const sum = chartSummary(chart(replay, { trades }));
    assert.deepEqual(sum.results.map((res) => [res.id, res.status]),
      [[1, "closed"], [2, "closed"], [3, "open"], [4, "pending"], [5, "cancelled"], [6, "missed"], [7, "closing"]]);
    assert.deepEqual({ ...sum, results: undefined, totalR: undefined, totalRNet: undefined, openR: undefined },
      { trades: 7, closed: 2, wins: 1, losses: 1, open: 2, closing: 1, pending: 1, cancelled: 1, missed: 1, expired: 0,
        results: undefined, totalR: undefined, totalRNet: undefined, openR: undefined });
    near(sum.totalR, 0.75);
    near(sum.totalRNet, sum.results[0].rNet + sum.results[1].rNet);
    near(sum.openR, 1.6);
  });

  it("after the finish, open trades are closed and waiting orders expired", () => {
    const sum = chartSummary(chart(replay, { trades, finished: true }));
    assert.deepEqual([sum.closed, sum.open, sum.pending, sum.expired, sum.cancelled, sum.missed], [4, 0, 0, 1, 1, 1]);
    assert.deepEqual(sum.results.filter((res) => res.exit?.reason === "end").map((res) => res.id), [3, 7]);
    near(sum.totalR, 0.75 + 1 + 0.6);
  });

  it("an empty chart is all zeros", () => {
    const sum = chartSummary(chart(replay));
    assert.deepEqual([sum.trades, sum.closed, sum.totalR, sum.openR, sum.results.length], [0, 0, 0, 0, 0]);
  });

  it("scoredResults takes closed trades from finished charts only, with the chart's index", () => {
    const charts = [chart(replay, { trades }), chart(replay, { trades, finished: true }), chart(replay, { trades: [] })];
    const scored = scoredResults(charts);
    assert.deepEqual(scored.map((res) => [res.chart, res.id]), [[1, 1], [1, 2], [1, 3], [1, 7]]);
    assert.ok(scored.every((res) => res.status === "closed"));
  });
});

describe("unfinishedClosed", () => {
  const replay = [b(100, 101, 99, 100.5), b(100.5, 104, 100, 103.5), b(104, 104.5, 102.5, 103)];
  const trades = [
    trade({ id: 1, stop: 98, target: 103.5 }),                // target on bar 2: +1.75R
    trade({ id: 2, side: "short", stop: 102, target: 95 }),   // stopped on bar 2: -1R
    trade({ id: 3, stop: 98, placedAt: 1 }),                  // still open: not counted
  ];

  it("counts the closed trades on charts not finished, and only those", () => {
    const open = chart(replay, { trades }), done = chart(replay, { trades, finished: true });
    const notLoaded = { ...chart([], { k: 0, nReplay: 3, trades }), bars: [] };
    const out = unfinishedClosed([open, done, null, notLoaded, { ...open, bars: undefined }]);
    const s = chartSummary(open);
    assert.deepEqual([out.trades, Object.keys(out).sort()], [2, ["totalR", "totalRNet", "trades"]]);
    near(out.totalR, 0.75);
    near(out.totalRNet, s.totalRNet);
    // Two unfinished charts add up.
    near(unfinishedClosed([open, open]).totalR, 1.5);
  });

  it("is zeros when nothing closed on an unfinished chart", () => {
    const zero = { trades: 0, totalR: 0, totalRNet: 0 };
    assert.deepEqual(unfinishedClosed([]), zero);
    assert.deepEqual(unfinishedClosed(undefined), zero);
    assert.deepEqual(unfinishedClosed([chart(replay, { trades, finished: true }), chart(replay, { k: 1, trades: trades.slice(2) })]), zero);
  });

  it("keeps a losing chart left unfinished visible beside a finished winner", () => {
    // The verification round's example: the score shows only the winner, +2R; the -1R trade appears here.
    const won = chart([b(100, 101, 99.8, 101), b(101, 104, 100.5, 103.5), b(103.5, 104, 103, 103.8)],
                      { finished: true, trades: [trade({ stop: 98, target: 104 })] });
    const lost = chart([b(100, 100.2, 97, 97.5), b(97.5, 98, 96, 96.5), b(96.5, 97, 96, 96.8)],
                       { trades: [trade({ stop: 98, target: 104 })] });
    near(summarize(scoredResults([won, lost])).totalR, 2);
    const also = unfinishedClosed([won, lost]);
    assert.equal(also.trades, 1);
    near(also.totalR, -1);
  });
});

// ---------------------------------------------------------------- set statistics

describe("summarize", () => {
  const closed = (side, r, rNet, barsHeld) => ({ status: "closed", side, r, rNet, barsHeld });

  it("empty input gives zeros and nulls, never NaN", () => {
    const empty = { trades: 0, wins: 0, losses: 0, flat: 0, winRate: null, totalR: 0, meanR: null, seR: null, medianR: null,
                    bestR: null, worstR: null, profitFactor: null, grossWinR: 0, grossLossR: 0, avgWinR: null, avgLossR: null,
                    avgBarsHeld: null, longs: { trades: 0, wins: 0, totalR: 0, meanR: null },
                    shorts: { trades: 0, wins: 0, totalR: 0, meanR: null }, totalRNet: 0, meanRNet: null, dollars: 0, dollarsNet: 0 };
    assert.deepEqual(summarize([]), empty);
    assert.deepEqual(summarize(undefined), empty);
    assert.deepEqual(summarize([{ status: "open", side: "long", r: 2 }, { status: "pending", r: null }]), empty);
  });

  it("mixed trades: counts, means, spread, profit factor, sides, dollars", () => {
    const res = summarize([closed("long", 2, 1.9, 4), closed("long", -1, -1.1, 2), closed("short", 3, 2.9, 6),
                           closed("short", -0.5, -0.6, 1), { status: "open", side: "long", r: 5, rNet: 5, barsHeld: 9 }]);
    assert.deepEqual([res.trades, res.wins, res.losses, res.flat], [4, 2, 2, 0]);
    near(res.winRate, 0.5);
    near(res.totalR, 3.5);
    near(res.meanR, 0.875);
    const sd = Math.sqrt([2, -1, 3, -0.5].reduce((a, r) => a + (r - 0.875) ** 2, 0) / 3);
    near(res.seR, sd / 2);
    near(res.medianR, 0.75);
    assert.deepEqual([res.bestR, res.worstR], [3, -1]);
    near(res.grossWinR, 5);
    near(res.grossLossR, 1.5);
    near(res.profitFactor, 5 / 1.5);
    near(res.avgWinR, 2.5);
    near(res.avgLossR, -0.75);
    near(res.avgBarsHeld, 3.25);
    assert.deepEqual(res.longs, { trades: 2, wins: 1, totalR: 1, meanR: 0.5 });
    assert.deepEqual(res.shorts, { trades: 2, wins: 1, totalR: 2.5, meanR: 1.25 });
    near(res.totalRNet, 3.1);
    near(res.meanRNet, 0.775);
    near(res.dollars, 3500, 1e-9);
    near(res.dollarsNet, 3100, 1e-9);
    assert.ok(numbers(res).every(Number.isFinite));
  });

  it("one trade has no standard error; an odd count takes the middle value", () => {
    const one = summarize([closed("long", 1.5, 1.4, 3)]);
    assert.deepEqual([one.seR, one.medianR, one.meanR, one.bestR, one.worstR], [null, 1.5, 1.5, 1.5, 1.5]);
    assert.equal(summarize([closed("long", 1, 1, 1), closed("long", -2, -2, 1), closed("short", 5, 5, 1)]).medianR, 1);
  });

  it("profit factor is null without a losing trade and 0 without a winning one; a 0R trade is flat", () => {
    const allWins = summarize([closed("long", 2, 1.9, 1), closed("short", 1, 0.9, 1)]);
    assert.deepEqual([allWins.profitFactor, allWins.grossLossR, allWins.avgLossR, allWins.losses], [null, 0, null, 0]);
    const allLosses = summarize([closed("long", -1, -1.1, 1), closed("short", -1, -1.1, 1)]);
    assert.deepEqual([allLosses.profitFactor, allLosses.winRate, allLosses.avgWinR], [0, 0, null]);
    const flat = summarize([closed("long", 0, -0.05, 1)]);
    assert.deepEqual([flat.wins, flat.losses, flat.flat, flat.winRate, flat.profitFactor], [0, 0, 1, 0, null]);
  });

  it("works on simulated charts end to end", () => {
    const replay = [b(100, 101, 99, 100.5), b(100.5, 104, 100, 103.5)];
    const charts = [chart(replay, { finished: true, trades: [trade({ stop: 98, target: 103.5 }), trade({ id: 2, side: "short", stop: 102, target: 95 })] })];
    const res = summarize(scoredResults(charts));
    assert.equal(res.trades, 2);
    near(res.totalR, 0.75);
  });
});

describe("buyAndHold", () => {
  it("is the % move from the last context close to the last replay close", () => {
    const replay = [b(100, 101, 99, 100.5), b(100.5, 104, 100, 103)];
    near(buyAndHold(chart(replay, { finished: true })), 3);
    near(buyAndHold(chart([b(100, 101, 95, 96)], { finished: true })), -4);
  });

  it("is null until every bar is in", () => {
    assert.equal(buyAndHold(chart([b(100, 101, 99, 100.5), b(100.5, 104, 100, 103)], { k: 1 })), null);
  });
});

// ---------------------------------------------------------------- random-entry baseline

describe("mulberry32", () => {
  it("matches the reference generator", () => {
    const rand = mulberry32(1);
    assert.deepEqual([rand(), rand(), rand()], [0.6270739405881613, 0.002735721180215478, 0.5274470399599522]);
    const other = mulberry32(20261008);
    assert.equal(other(), 0.9406747838947922);
  });

  it("stays in [0, 1)", () => {
    const rand = mulberry32(3);
    for (let n = 0; n < 10000; n++) {
      const x = rand();
      assert.ok(x >= 0 && x < 1);
    }
  });
});

describe("randomBaseline", () => {
  // Entering at the open of replay bar 1 (index 2) the 2%/4% bracket makes +2R; entering at bar 2 it loses 1R, after
  // ignoring a target touch on its own fill bar.
  const TWO = [b(100, 100.5, 99.5, 100), b(100, 105, 99.5, 104.5), b(99, 99.5, 97, 97.5)];
  const twoChart = (extra = {}) => chart(TWO, { finished: true, trades: [trade({ stop: 98, target: 104 })], ...extra });

  it("is empty without closed trades on finished charts", () => {
    const none = { meanR: null, sdTotal: null, totalMean: null, percentile: null, draws: 0, trades: 0, total: 0 };
    assert.deepEqual(randomBaseline([]), none);
    assert.deepEqual(randomBaseline([twoChart({ finished: false, k: 3 })]), none);
    assert.deepEqual(randomBaseline([chart(TWO, { finished: true, trades: [trade({ kind: "limit", entry: 90, stop: 88, target: 110 })] })]), none);
  });

  it("draws entries uniformly from the replay bars and walks them by the same rules", () => {
    const res = randomBaseline([twoChart()], { draws: 2000, seed: 1 });
    assert.deepEqual([res.trades, res.draws, res.total], [1, 2000, 2]);
    // Each draw is +2R (bar 1) or -1R (bar 2), picked by the seeded generator.
    const rand = mulberry32(1);
    let losers = 0;
    for (let d = 0; d < 2000; d++) losers += Math.floor(rand() * 2) === 1 ? 1 : 0;
    near(res.percentile, losers / 2000);
    near(res.totalMean, (2 * (2000 - losers) - losers) / 2000, 1e-9);
    near(res.meanR, res.totalMean);
    assert.ok(Math.abs(res.meanR - 0.5) < 0.15);
    assert.ok(Math.abs(res.sdTotal - 1.5) < 0.05);
  });

  it("is deterministic for a seed and changes with it", () => {
    const charts = [twoChart()];
    assert.deepEqual(randomBaseline(charts, { seed: 5 }), randomBaseline(charts, { seed: 5 }));
    assert.notEqual(randomBaseline(charts, { seed: 5 }).totalMean, randomBaseline(charts, { seed: 6 }).totalMean);
    assert.equal(randomBaseline(charts).draws, 2000);
    assert.equal(randomBaseline(charts, { draws: 300 }).draws, 300);
  });

  it("a flat market gives every draw 0R, and a tie is not a win", () => {
    const flat = Array.from({ length: 10 }, () => b(100, 100, 100, 100));
    const res = randomBaseline([chart(flat, { finished: true, trades: [trade({ stop: 99, target: 102 })] })]);
    assert.deepEqual([res.meanR, res.sdTotal, res.totalMean, res.percentile, res.total], [0, 0, 0, 0, 0]);
  });

  // 90 bars falling 1% a bar, then 10 rising 2%: no gaps, so a trade can make at most +3R and lose at most 1R.
  const vee = [];
  for (let i = 0, close = 100; i < 100; i++) {
    const open = close, move = i < 90 ? 0.99 : 1.02;
    close = open * move;
    vee.push(b(open, Math.max(open, close), Math.min(open, close), close));
  }
  const bottom = 90, ref = vee[bottom - 1][3];

  it("buying the bottom three times beats nearly every random draw", () => {
    const trades = [1, 2, 3].map((id) => trade({ id, stop: ref * 0.98, target: ref * 1.06, placedAt: bottom }));
    const c = chart(vee, { finished: true, trades });
    assert.ok(chartSummary(c).results.every((res) => res.exit.reason === "target"));
    const res = randomBaseline([c]);
    near(res.total, 9, 1e-9);
    assert.ok(res.percentile > 0.97, `percentile ${res.percentile}`);
    assert.ok(res.meanR < 0, `meanR ${res.meanR}`);
    near(res.totalMean, res.meanR * 3, 1e-9);
  });

  it("shorting the bottom beats no random draw", () => {
    const trades = [1, 2, 3].map((id) => trade({ id, side: "short", stop: ref * 1.02, target: ref * 0.94, placedAt: bottom }));
    const c = chart(vee, { finished: true, trades });
    const res = randomBaseline([c]);
    near(res.total, -3, 1e-9);
    assert.equal(res.percentile, 0);
    assert.ok(res.meanR > 0, `meanR ${res.meanR}`);
  });

  it("takes the distances from the planned entry and plans each random entry at the previous close", () => {
    // The fill-risk example: planned at 100 with a 5% stop and a 10% target, filled at 95.05. The one random bar is
    // the same bar, planned at the same close, so every draw scores what the visitor did: 2.99R, not 157R.
    const c = chart([b(95.05, 96, 95.02, 95.8), b(96, 111, 95.9, 110.5)], { finished: true, trades: [trade({ stop: 95, target: 110 })] });
    const res = randomBaseline([c], { draws: 50 });
    near(res.total, (110 - 95.05) / 5, 1e-12);
    near(res.meanR, (110 - 95.05) / 5, 1e-9);
    near(res.sdTotal, 0, 1e-9);
    assert.deepEqual([res.percentile, res.trades], [0, 1]);
  });

  it("draws only bars where the random market order fills; a gap past its stop misses it", () => {
    // Long at 100 with a 2% stop, out at 97 on the gap. A random entry on bar 2 is planned at bar 1's close (100)
    // with its stop at 98, and bar 2 opens at 97: missed, so every draw is the bar 1 entry, -1.5R.
    const c = chart([b(100, 100.5, 99.5, 100), b(97, 98, 96, 97.5), b(97.5, 98, 97, 97.8)],
                    { finished: true, trades: [trade({ stop: 98, target: 104 })] });
    assert.equal(chartSummary(c).results[0].exit.price, 97);
    const res = randomBaseline([c]);
    near(res.total, -1.5);
    near(res.meanR, -1.5, 1e-12);
    assert.deepEqual([res.sdTotal, res.percentile], [0, 0]);
  });

  it("a trade with no bar a random order could fill on adds 0R to every draw", () => {
    // Bought on the last bar; the only random bar opens 3% under the previous close, past the 1.5% stop.
    const c = chart([b(97, 98, 96.5, 97.5), b(97.6, 98.5, 97.2, 98)],
                    { finished: true, trades: [trade({ stop: 96, target: 100, placedAt: 1 })] });
    const res = randomBaseline([c]);
    near(res.total, 0.4 / 1.5, 1e-12);
    assert.deepEqual([res.meanR, res.sdTotal, res.totalMean, res.percentile], [0, 0, 0, 1]);
  });
});

// ---------------------------------------------------------------- formatting

describe("formatting", () => {
  it("fmtR signs R with a true minus sign", () => {
    assert.equal(fmtR(1.25), "+1.25R");
    assert.equal(fmtR(-0.5), `${MINUS}0.50R`);
    assert.equal(fmtR(-0.5).includes("-"), false);
    assert.equal(fmtR(2.456, 1), "+2.5R");
    assert.equal(fmtR(1234.5), "+1,234.50R");
  });

  it("a value that rounds to zero has no sign", () => {
    assert.equal(fmtR(0), "0.00R");
    assert.equal(fmtR(-0.004), "0.00R");
    assert.equal(fmtR(-0), "0.00R");
    assert.equal(fmtPct(0.001), "0.00%");
  });

  it("fmtPct is signed by default", () => {
    assert.equal(fmtPct(2.4), "+2.40%");
    assert.equal(fmtPct(-1.1), `${MINUS}1.10%`);
    assert.equal(fmtPct(55, 0, false), "55%");
    assert.equal(fmtPct(-3.25, 1, false), `${MINUS}3.3%`);
    assert.equal(fmtPct(1234.5, 1), "+1,234.5%");
  });

  it("fmtPrice has two decimals and thousands separators", () => {
    assert.equal(fmtPrice(98.5), "98.50");
    assert.equal(fmtPrice(1234.5), "1,234.50");
    assert.equal(fmtPrice(1234567.891), "1,234,567.89");
    assert.equal(fmtPrice(0.1234, 4), "0.1234");
    assert.equal(fmtPrice(-3), `${MINUS}3.00`);
  });

  it("missing values are an en dash", () => {
    for (const x of [null, undefined, NaN, Infinity, "1"]) {
      assert.equal(fmtR(x), NONE);
      assert.equal(fmtPct(x), NONE);
      assert.equal(fmtPrice(x), NONE);
    }
  });
});
