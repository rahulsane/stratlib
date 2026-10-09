// TradeTest page: ten blind daily charts to mark up and trade forward one bar at a time. This module builds the page
// inside its mount point, keeps the visitor's work in this browser, talks to the four replay endpoints, and hands the
// chart (chart.js), the scoring (sim.js) and the report (report.js) their parts. The server only ever sends bars up
// to the current one, so nothing here can look ahead, and nothing goes back: orders go in at the newest bar and stay.
//
// mount(root) builds the page inside root and unmounts itself when root leaves the document.
//
// The saved state lives under localStorage "stratlib.tradetest.v1" (see the build spec for its shape), without the
// charts' bars. Each chart's rows have a key of their own ("stratlib.tradetest.v1.bars.<set>.<i>"): a finished
// chart's whole window, written once at the finish, and an unfinished chart's rows up to its current bar (never past
// it), written when the visitor leaves the chart or the page or pauses Play. So the score, the report and Copy result
// count every trade offline and after the charts are updated. Each save, rows included, is mirrored to IndexedDB (see
// Journal), which a crash can't set back; rows missing from both come from the server (/bars). Undo and redo cover
// drawings only and last as long as the page; trades are never undone.

const V = new URL(import.meta.url).search;      // the build's cache key, passed on to every module and the styles
const KEY = "stratlib.tradetest.v1";
const BARS_KEY = `${KEY}.bars.`;                 // + "<set>.<chart index>": a finished chart's rows
const API = "/api/tradetest/";
const N_CHARTS = 10;
const SPEEDS = [1, 2, 4, 8];
const MAX_STEP = 20;
const DISCLAIMER = "Simulated trades on historical prices. Past results are not a forecast, and nothing here is "
  + "investment advice.";
// seenIntro: the guide has been closed once (it opens by itself until then); guideStep: the step it last showed.
const SETTINGS = { volume: true, ma: { e9: false, e21: false, s50: false, s200: false }, magnet: false, speed: 2,
                   autoPause: true, seenIntro: false, guideStep: 0 };

const TYPE_NAMES = { trend: "Trendline", ray: "Ray", channel: "Channel", hline: "Price level", vline: "Time level",
                     zone: "Zone" };
// The demand and supply zones are ordinary zones that start with a colour and a label.
const TOOLS = [
  { tool: "cursor", name: "Cursor", key: "1", also: "Esc" }, { tool: "trend", name: "Trendline", key: "2" },
  { tool: "ray", name: "Ray", key: "3" }, { tool: "channel", name: "Channel", key: "4" },
  { tool: "hline", name: "Price level", key: "5" }, { tool: "vline", name: "Time level", key: "6" },
  { tool: "zone", name: "Zone", key: "7" },
  { tool: "demand", name: "Demand zone", key: "8", zone: { color: "teal", label: "Demand", name: "Demand zone" } },
  { tool: "supply", name: "Supply zone", key: "9", zone: { color: "coral", label: "Supply", name: "Supply zone" } },
];
const COLOUR_NAMES = { text: "Light grey", steel: "Steel", brass: "Brass", teal: "Teal", coral: "Coral",
                       violet: "Violet", amber: "Amber" };
const LINK = "https://stratlib.blxnksy.dev/tradetest";
const END_HINT = "No bars left: finish the chart to see what it was.";
// A tab moving a chart (Play, or a step on its way or just in) says so over the channel at least every BEAT_MS (see
// beat); for MOVING_MS after the last word the chart is that tab's, and read-only in the others until they take it
// over (see heardMoving and takeOver).
const MOVING_MS = 2000;
const BEAT_MS = 250;
const MOVING_HINT = "This chart is moving in another tab. Take over to trade or draw on it here.";
const MOVING_TICKET = "This chart is moving in another tab. Take over to place this order.";
const MOVING_EDIT = "This chart is moving in another tab. Take over to change this trade.";
const UNLOADED_EDIT = "Waiting for this chart’s bars before this trade can be changed.";
// Chart gestures that change nothing (moving the view), as against a drawing, an order or a level being made or moved.
const VIEW_GESTURES = new Set(["pan", "pinch", "price", "time"]);
const STATUS_WORDS = { pending: "Pending", open: "Open", closing: "Closing", closed: "Closed", cancelled: "Cancelled",
                       missed: "Missed", expired: "Expired" };
const EXIT_WORDS = { stop: "stop", target: "target", close: "closed at the open", finish: "closed at finish",
                     end: "end of the replay" };
// The keyboard, for Rules in detail and the guide's last step.
const KEYS = [
  ["Esc or 1", "Cursor, cancelling a half-made drawing or order"],
  ["2 to 7", "Trendline, ray, channel, price level, time level, zone"], ["8 / 9", "Demand zone / supply zone"],
  ["B / S", "Long / short"], ["M", "Magnet"], ["F", "Fit the chart"], ["Ctrl+Z / Ctrl+Y", "Undo / redo a drawing"],
  ["→ or Space", "Next bar"], ["Shift+→", "Next 5 bars"], ["P", "Play or pause"],
  ["Delete", "Delete the selected drawing"], ["Enter", "Place the order in the ticket"], ["H", "How to play"],
  ["?", "These keys and the rules, in How to play"],
];

const ICONS = {
  cursor: '<path d="M5 2.8l8.6 5.4-3.9 1 2.5 4.4-1.8 1-2.5-4.4L5 13.1z"/>',
  trend: '<path d="M5 13l8-8"/><circle cx="3.9" cy="14.1" r="1.5"/><circle cx="14.1" cy="3.9" r="1.5"/>',
  ray: '<path d="M5.1 12.6L16.2 4"/><circle cx="3.9" cy="13.5" r="1.5"/><circle cx="10.4" cy="8.5" r="1.3"/>',
  channel: '<path d="M2.5 11l11-6.5M4.5 15.5l11-6.5"/><path d="M3.5 13.3l11-6.5" stroke-dasharray="1.5 2"/>',
  hline: '<path d="M2 9h5.4M10.6 9H16"/><circle cx="9" cy="9" r="1.6"/>',
  vline: '<path d="M9 2v5.4M9 10.6V16"/><circle cx="9" cy="9" r="1.6"/>',
  zone: '<rect x="2.5" y="5" width="13" height="8" rx="1"/><path d="M5 9h8" stroke-dasharray="1.5 2"/>',
  demand: '<rect x="2.5" y="10.5" width="13" height="5" rx="1" class="tt-ic-zone"/>'
    + '<path d="M9 8.5V2.5M6.5 5L9 2.5 11.5 5"/>',
  supply: '<rect x="2.5" y="2.5" width="13" height="5" rx="1" class="tt-ic-zone"/>'
    + '<path d="M9 9.5v6M6.5 13L9 15.5 11.5 13"/>',
  long: '<path d="M2.5 13.5l4-4 3 2.5 6-6.5"/><path d="M11.8 5.5h3.7v3.7"/>',
  short: '<path d="M2.5 4.5l4 4 3-2.5 6 6.5"/><path d="M11.8 12.5h3.7V8.8"/>',
  magnet: '<path d="M4 3h3v6a2 2 0 0 0 4 0V3h3v6a5 5 0 0 1-10 0z"/><path d="M4 6h3M11 6h3"/>',
  indicators: '<path d="M2 12.5c2.4 0 2.9-7 5.4-7s2.4 7 4.8 7S14.7 8 16 8"/>',
  fit: '<path d="M3 7V3h4M11 3h4v4M15 11v4h-4M7 15H3v-4"/>',
  undo: '<path d="M6.5 4L3 7.5 6.5 11"/><path d="M3.5 7.5h7.2a4 4 0 0 1 0 8H8"/>',
  redo: '<path d="M11.5 4L15 7.5 11.5 11"/><path d="M14.5 7.5H7.3a4 4 0 0 0 0 8H10"/>',
  next: '<path d="M4.5 4.5v9l7-4.5z" fill="currentColor"/><path d="M14 4.5v9"/>',
  play: '<path d="M5.5 3.8v10.4L14 9z" fill="currentColor"/>',
  pause: '<path d="M6.5 4.5v9M11.5 4.5v9" stroke-width="2.2"/>',
  trash: '<path d="M3.5 5h11M7.2 5V3.5h3.6V5M5 5l.7 9.5h6.6L13 5"/>',
  plus: '<path d="M9 4v10M4 9h10"/>',
  caret: '<path d="M5.5 7.5L9 11l3.5-3.5"/>',
  flag: '<path d="M5 15.5V3M5 3.5h8l-1.8 3 1.8 3H5"/>',
  download: '<path d="M9 3v8.5M5.5 8L9 11.5 12.5 8M3.5 14.5h11"/>',
  help: '<circle cx="9" cy="9" r="6.8"/><path d="M7.1 7.2a2 2 0 1 1 2.8 1.8c-.6.3-.9.7-.9 1.3v.3"/>'
    + '<path d="M9 12.8v.1"/>',
  close: '<path d="M4.5 4.5l9 9M13.5 4.5l-9 9"/>',
  locate: '<circle cx="9" cy="9" r="4.6"/><path d="M9 1.8v2.6M9 13.6v2.6M1.8 9h2.6M13.6 9h2.6"/>',
};
const icon = (name) => `<svg viewBox="0 0 18 18" aria-hidden="true">${Object.hasOwn(ICONS, name) ? ICONS[name] : ""}`
  + "</svg>";

// ---------------------------------------------------------------- small helpers

// An element with properties and children. Text goes in as text; "html" is only ever this file's own markup.
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "hidden") el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

// replaceChildren for a list that may hold nothing in places: null and false are left out (the DOM would print
// "null").
const fill = (el, ...children) => el.replaceChildren(
  ...children.flat(Infinity).filter((c) => c != null && c !== false));

// An empty list's hint, and a link after it when there is one: the same elements every time, put back only when they
// change, so a link pressed while the list renders (each bar, during Play) still gets its click.
function listHint(list, hint, text, link) {
  if (hint.textContent !== text) hint.textContent = text;
  const want = link ? [hint, link] : [hint];
  if (list.children.length !== want.length || want.some((el, i) => list.children[i] !== el)) {
    list.replaceChildren(...want);
  }
}

const clone = (x) => JSON.parse(JSON.stringify(x));
const isNum = (x) => typeof x === "number" && Number.isFinite(x);
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
// Whether el has focus the visitor gave it from the keyboard (the browser's own :focus-visible rule).
const keyboardFocus = (el) => {
  try { return !!el?.matches?.(":focus-visible"); } catch { return false; }
};

// A typed price: a plain decimal, with commas only as thousands separators ("1,234.50"). Null when blank, NaN for
// anything else, so "105,5", "0x5A" or "1e3" are refused rather than read as some other number.
const PLAIN = /^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d*)?$/;
function readNumber(text) {
  const s = String(text ?? "").replace(/−/g, "-").trim();
  if (!s) return null;
  if (!PLAIN.test(s) || !/\d/.test(s)) return NaN;
  const n = Number(s.replace(/,/g, ""));
  return Number.isFinite(n) ? n : NaN;
}

// Levels the chart can't sensibly hold: beyond a tenth or ten times the last price. null when they are all fine.
function farLevel(levels, ref) {
  if (!isNum(ref) || ref <= 0) return null;
  const lo = ref / 10, hi = ref * 10;
  if (levels.every((p) => !isNum(p) || (p >= lo && p <= hi))) return null;
  return `Prices on this chart must be between ${fmt2(lo)} and ${fmt2(hi)}, a tenth to ten times the last price.`;
}
const fmt2 = (x) => x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const changed = (chart) => { chart.rev = (chart.rev | 0) + 1; };

// A phone or tablet: no keyboard shortcuts to mention.
const touch = () => typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
const stillMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
// How much of the top of the screen the site's top bar covers: it stays on screen (sticky) over the page.
const barCover = () => {
  const bar = document.querySelector(".d-topbar"), style = bar && getComputedStyle(bar);
  return style && (style.position === "sticky" || style.position === "fixed")
    ? Math.max(0, bar.getBoundingClientRect().height) : 0;
};
const priceText = (p) => (isNum(p) ? p.toFixed(2) : "");
const sideWord = (side) => (side === "short" ? "Short" : "Long");
const toneOf = (x) => (x > 0 ? "d-up" : x < 0 ? "d-down" : "");
const count = (n) => (n ? h("span", { class: "d-count-badge", text: String(n) }) : null);

class ApiError extends Error {
  constructor(code, message, status = 0, extra = {}) {
    super(message);
    this.code = code; this.status = status;
    this.endpoint = extra.endpoint || ""; this.retryAfter = extra.retryAfter ?? null;
  }
}

const STATUS_CODES = { 409: "finished", 410: "expired", 415: "bad_request", 429: "rate_limited", 503: "unavailable" };

// One request to the replay API. Bodies always go as JSON (the server refuses anything else); a refusal carries
// the server's code, its message for visitors, and how long to wait (Retry-After, in seconds) when it gives one.
async function api(name, body) {
  let res;
  try {
    res = await fetch(API + name, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify(body || {}), cache: "no-store", credentials: "same-origin" });
  } catch {
    throw new ApiError("network", "TradeTest couldn't reach the server.", 0, { endpoint: name });
  }
  let data = null;
  try { data = await res.json(); } catch { /* not JSON: handled below */ }
  if (!res.ok || !data || typeof data !== "object" || data.error) {
    const code = typeof data?.error === "string" ? data.error : STATUS_CODES[res.status] || "server";
    const wait = Number(res.headers.get("Retry-After"));
    throw new ApiError(code, typeof data?.message === "string" ? data.message : "", res.status,
                       { endpoint: name, retryAfter: Number.isFinite(wait) && wait >= 0 ? wait : null });
  }
  return data;
}

// A server message as a notice: the first sentence as the title, the rest as the detail.
function sentences(text) {
  const m = /^(.+?[.!?])\s+(.+)$/s.exec(String(text).trim());
  return m ? [m[1], m[2]] : [String(text).trim(), ""];
}

const waitWords = (seconds) => {
  const minutes = Math.max(1, Math.ceil((seconds || 0) / 60));
  return minutes === 1 ? "a minute" : `${minutes} minutes`;
};

function loadState() {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// A saved state this page can resume: schema 1, ten charts each with a cursor and its sizes.
function usable(state) {
  if (!state || state.schema !== 1 || !Array.isArray(state.charts) || state.charts.length !== N_CHARTS) return false;
  return state.charts.every(usableChart);
}

const usableChart = (c) => !!c && typeof c === "object" && typeof c.cursor === "string" && Number.isInteger(c.nCtx)
  && Number.isInteger(c.nReplay) && Number.isInteger(c.k) && Array.isArray(c.bars) && Array.isArray(c.trades)
  && Array.isArray(c.drawings);

// A chart's rows as this page needs them: up to the current bar, or the whole window once finished.
function complete(chart) {
  return chart.bars.length >= chart.nCtx + chart.k
    && (!chart.finished || chart.bars.length >= chart.nCtx + chart.nReplay);
}

// How many rows a chart keeps: the whole window once finished, else up to its current bar.
const rowCount = (chart) => chart.nCtx + (chart.finished ? chart.nReplay : chart.k);

// Rows read back from storage, cut to what the chart keeps: arrays whose open, high, low and close are numbers, and
// as many as it needs. null when they fall short (they were saved at an earlier bar) or are damaged.
function rowsFor(rows, chart) {
  const n = rowCount(chart);
  if (!Array.isArray(rows) || rows.length < n) return null;
  const out = rows.length === n ? rows : rows.slice(0, n);
  return out.every((r) => Array.isArray(r) && r.length >= 5 && r.slice(0, 4).every(isNum)) ? out : null;
}

// The last bar has been shown and the chart is still open: nothing more can fill, so no new orders or closes.
const atEnd = (chart) => !!chart && !chart.finished && chart.k >= chart.nReplay;

// Whether two copies hold the same trade: the same number, placed at the same bar with the same order.
const sameTrade = (a, b) => a.id === b.id && a.placedAt === b.placedAt && a.side === b.side && a.kind === b.kind
  && a.entry === b.entry && a.stop === b.stop && a.target === b.target;

// base with every trade that only other holds added (renumbered when base already uses its number); true when any
// was. Used when two copies of one chart meet at a finish, so a trade placed in either tab is never dropped.
function mergeTrades(base, other) {
  let added = false;
  for (const t of other.trades) {
    if (base.trades.some((b) => sameTrade(b, t))) continue;
    const id = base.trades.some((b) => b.id === t.id) ? Math.max(base.nextId, ...base.trades.map((b) => b.id + 1))
      : t.id;
    base.trades.push({ ...clone(t), id });
    base.nextId = Math.max(base.nextId, id + 1);
    added = true;
  }
  if (added) base.trades.sort((a, b) => a.placedAt - b.placedAt || a.id - b.id);
  return added;
}

// Two copies of one chart's drawings, merged into base: each drawing as it was last changed in either copy (stamp, in
// ms), none that either copy deleted after that (deleted: { id, stamp }), and two drawings that took the same number
// in two tabs at once (told apart by when they were made, born) both kept, the later one renumbered, so both tabs
// come to the same list. Drawings never score, so any two copies can be merged this way. Where both copies hold the
// same drawing alike, own's object is kept (this tab's copy: a label being typed stays tied to the chart). True when
// base changed.
function mergeDrawings(base, other, own = base) {
  const was = JSON.stringify([base.drawings, base.deleted || []]);
  const gone = new Map();
  for (const g of [...(base.deleted || []), ...(other.deleted || [])]) {
    if (!(gone.get(g.id)?.stamp >= g.stamp)) gone.set(g.id, g);
  }
  const later = (a, b) => ((a.stamp || 0) !== (b.stamp || 0) ? (a.stamp || 0) > (b.stamp || 0)
    : JSON.stringify(a) > JSON.stringify(b));
  const kept = new Map();
  for (const d of (own === other ? [...other.drawings, ...base.drawings] : [...base.drawings, ...other.drawings])) {
    if (gone.get(d.id)?.stamp >= (d.stamp || 0)) continue;
    const key = `${d.id}:${d.born || 0}`, had = kept.get(key);
    if (!had || later(d, had)) kept.set(key, d);
  }
  const all = [...kept.values()].sort((a, b) => a.id - b.id || (a.born || 0) - (b.born || 0));
  let top = Math.max(0, ...all.map((d) => d.id), ...gone.keys());
  const used = new Set();
  base.drawings = all.map((d) => {
    if (!used.has(d.id)) { used.add(d.id); return d; }
    top += 1;
    used.add(top);
    return { ...d, id: top };
  });
  base.deleted = [...gone.values()].sort((a, b) => a.stamp - b.stamp || a.id - b.id).slice(-100);
  return JSON.stringify([base.drawings, base.deleted]) !== was;
}

// A note on a trade both copies hold: the one written last (noteStamp, in ms) goes into base. True when base changed.
function mergeNotes(base, other) {
  let touched = false;
  for (const t of other.trades) {
    const mine = base.trades.find((b) => sameTrade(b, t));
    if (!mine || (t.noteStamp || 0) <= (mine.noteStamp || 0) || t.note === mine.note) continue;
    mine.note = t.note; mine.noteStamp = t.noteStamp;
    touched = true;
  }
  return touched;
}

// What a copy behind holds that the copy ahead of it doesn't: trades placed, and closes, cancels and level changes
// asked for, while the other tab was already further along. They would be scored over bars that tab had shown, so
// they don't go in; the list says which, for the visitor. [{ name, placed }].
function unseen(behind, ahead) {
  const out = [];
  for (const t of behind.trades) {
    const a = ahead.trades.find((x) => sameTrade(x, t));
    const name = `${sideWord(t.side)} #${t.id}`;
    if (!a) out.push({ name, placed: true });
    else if ((t.closeAt != null && a.closeAt == null) || (t.cancelAt != null && a.cancelAt == null)
             || (t.changes || []).some((c) => !(a.changes || []).some((d) => d.at === c.at))) {
      out.push({ name, placed: false });
    }
  }
  return out;
}

// "Long #3 wasn't placed: chart 1 had already moved on in another tab." for what unseen found.
function unseenWords(lost, n) {
  const list = (xs) => xs.map((x) => x.name).join(", ");
  const placed = lost.filter((x) => x.placed), other = lost.filter((x) => !x.placed);
  const why = `chart ${n} had already moved on in another tab.`;
  return [placed.length && `${list(placed)} ${placed.length > 1 ? "weren’t" : "wasn’t"} placed: ${why}`,
          other.length && `Your change to ${list(other)} didn’t go in: ${why}`].filter(Boolean).join(" ");
}

// A drawing taken off a chart is noted, by number and when, so a merge with a copy that still has it (another tab's,
// saved a moment before) doesn't bring it back.
function bury(chart, id) {
  chart.deleted = [...(chart.deleted || []).filter((g) => g.id !== id), { id, stamp: Date.now() }].slice(-100);
}

// The state to resume when this browser holds two copies (localStorage and the journal): within one set each chart
// keeps whichever copy is further along, so a crash that set localStorage back costs nothing; across sets, the one
// saved last. Both are cleaned first.
function pickSaved(local, journal) {
  const a = usable(local) ? cleanCharts(local) : null, b = usable(journal) ? cleanCharts(journal) : null;
  if (!a || !b) return a || b;
  const later = (b.saved || 0) > (a.saved || 0) ? b : a, other = later === a ? b : a;
  if (a.set !== b.set) return later;
  later.charts = later.charts.map((chart, i) => newerChart(chart, other.charts[i]));
  return later;
}

// ---------------------------------------------------------------- the crash journal

// A copy of every save in IndexedDB. Chrome writes localStorage to disk in batches, in a new session at most about
// once a minute after its first write, so a crash or a force-quit can take back a minute of bars, trades and finishes;
// an IndexedDB write is on disk once its transaction completes. localStorage stays the copy other tabs listen to.
// Keys: "state" (the saved state's text) and "bars.<set>.<i>" (a chart's rows, as localStorage keeps them). Writes
// made in one go share a transaction, so a chart's rows and the state that needs them land together. Every call is
// best effort: a browser without IndexedDB simply has no journal.
class Journal {
  constructor(name) {
    this.queue = new Map(); this.busy = false; this.db = null; this.soon = false;
    this.opened = new Promise((resolve) => {
      let req;
      try { req = indexedDB.open(name, 1); } catch { resolve(null); return; }
      req.onupgradeneeded = () => { try { req.result.createObjectStore("kv"); } catch { /* already there */ } };
      req.onsuccess = () => {
        this.db = req.result;
        this.db.onversionchange = () => { this.db.close(); this.db = null; };
        resolve(this.db);
        this.flush();
      };
      req.onerror = () => resolve(null);
    });
  }

  // Everything in the journal, or nothing after `wait` ms (a journal that doesn't answer mustn't hold the page up).
  read(wait = 800) {
    const all = this.opened.then((db) => new Promise((resolve) => {
      const out = new Map();
      if (!db) { resolve(out); return; }
      try {
        const req = db.transaction("kv").objectStore("kv").openCursor();
        req.onsuccess = () => {
          const cur = req.result;
          if (!cur) { resolve(out); return; }
          out.set(String(cur.key), cur.value);
          cur.continue();
        };
        req.onerror = () => resolve(out);
      } catch {
        resolve(out);
      }
    }));
    return Promise.race([all, new Promise((resolve) => setTimeout(() => resolve(new Map()), wait))]);
  }

  put(key, value) { this.queue.set(key, value); this.later(); }

  remove(key) { this.queue.set(key, undefined); this.later(); }

  // Once the code that is writing has finished: everything it put goes in one transaction.
  later() {
    if (this.soon) return;
    this.soon = true;
    queueMicrotask(() => { this.soon = false; this.flush(); });
  }

  // One transaction at a time; whatever came in meanwhile goes in the next, the latest value per key.
  flush() {
    if (this.busy || !this.db || !this.queue.size) return;
    const batch = this.queue;
    this.queue = new Map();
    this.busy = true;
    const next = () => { this.busy = false; this.flush(); };
    try {
      const tx = this.db.transaction("kv", "readwrite"), store = tx.objectStore("kv");
      for (const [key, value] of batch) {
        if (value === undefined) store.delete(key); else store.put(value, key);
      }
      tx.oncomplete = next; tx.onabort = next;
    } catch {
      this.busy = false;
    }
  }
}

// Keys that only change another key: they neither end a label waiting to be typed nor close a menu.
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "AltGraph", "Fn", "OS"]);

// Fields that take typing: their keys are never shortcuts, and the drawings list waits while one has focus.
const TEXT_FIELD = 'textarea, select, [contenteditable]:not([contenteditable="false"]), '
  + 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"])';

const POINTS = { trend: 2, ray: 2, channel: 3, hline: 1, vline: 1, zone: 2 };
const COLOURS = ["text", "steel", "brass", "teal", "coral", "violet", "amber"];
const ERAS = ["2000s", "2010s", "2020s"];
const finite = (x) => typeof x === "number" && Number.isFinite(x);

// Saved work comes back from this browser's storage, which anything can edit: keep only well-formed drawings and
// trades, so a damaged entry costs that entry rather than the page.
function cleanCharts(state) {
  for (const chart of state.charts) {
    chart.drawings = chart.drawings.filter((d) => d && Object.hasOwn(POINTS, d.type) && Number.isInteger(d.id)
      && Array.isArray(d.points) && d.points.length === POINTS[d.type]
      && d.points.every((q) => q && Number.isInteger(q.x) && finite(q.p)));
    for (const d of chart.drawings) {
      if (!COLOURS.includes(d.color)) d.color = "steel";
      d.label = typeof d.label === "string" ? d.label.slice(0, 60) : "";
      d.extend = !!d.extend;
      if (!Number.isInteger(d.createdAt)) d.createdAt = 0;
      if (d.movedAt != null && !Number.isInteger(d.movedAt)) delete d.movedAt;
      for (const at of ["born", "stamp"]) if (d[at] != null && !(finite(d[at]) && d[at] >= 0)) delete d[at];
    }
    chart.deleted = Array.isArray(chart.deleted)
      ? chart.deleted.filter((g) => g && Number.isInteger(g.id) && finite(g.stamp)).slice(-100) : [];
    if (chart.guess != null) {
      const g = chart.guess;
      chart.guess = g && typeof g === "object"
        ? { era: ERAS.includes(g.era) ? g.era : null, kind: g.kind === "stock" || g.kind === "etf" ? g.kind : null }
        : null;
      if (!chart.guess?.era && !chart.guess?.kind) delete chart.guess;
    }
    if (!Number.isInteger(chart.rev)) chart.rev = 0;
    chart.trades = chart.trades.filter((t) => t && Number.isInteger(t.id) && (t.side === "long" || t.side === "short")
      && ["market", "limit", "stop"].includes(t.kind) && (t.entry === null || finite(t.entry)) && finite(t.stop)
      && finite(t.target) && Number.isInteger(t.placedAt) && Array.isArray(t.changes || []));
    for (const t of chart.trades) {
      t.changes = (t.changes || []).filter((c) => c && Number.isInteger(c.at) && finite(c.stop) && finite(c.target));
      t.note = typeof t.note === "string" ? t.note.slice(0, 200) : "";
      if (t.noteStamp != null && !finite(t.noteStamp)) delete t.noteStamp;
    }
    if (!Number.isInteger(chart.nextId)) chart.nextId = chart.trades.reduce((m, t) => Math.max(m, t.id), 0) + 1;
  }
  return state;
}

// The move from the close the chart was finished on to the window's last close, in percent; null when the chart was
// played to its last bar or its bars aren't all in.
function afterFinish(chart) {
  const { bars, nCtx, nReplay } = chart, at = chart.finishedAt ?? chart.k;
  if (!chart.finished || at >= nReplay || bars.length < nCtx + nReplay) return null;
  const from = bars[nCtx + at - 1]?.[3], to = bars[nCtx + nReplay - 1]?.[3];
  return isNum(from) && isNum(to) && from > 0 ? 100 * (to / from - 1) : null;
}

// What the chart's prices were in dollars: "100.00 on the chart, the close before the replay, was $39.18. The last
// close was $44.16." Both are as traded on the replay's last day (Rules in detail says so); "" without a scale.
function realWords(chart, fmtPrice) {
  const r = chart.reveal, scale = r?.scale;
  if (!isNum(scale) || scale <= 0) return "";
  const last = chart.bars[chart.nCtx + chart.nReplay - 1]?.[3];
  return `100.00 on the chart, the close before the replay, was $${fmtPrice(100 / scale)}.`
    + (isNum(last) ? ` The last close was $${fmtPrice(last / scale)}.` : "");
}

// "Your guess: 2010s, stock. Era right, type wrong." for a finished chart with a guess; null without one. The era is
// the decade of the first replay bar.
function guessLine(chart) {
  const g = chart.guess, r = chart.reveal;
  if (!g || !r || (!g.era && !g.kind)) return null;
  const year = Number(String(r.start || "").slice(0, 4)), era = year ? `${Math.floor(year / 10) * 10}s` : null;
  const kind = (k) => (k === "etf" ? "ETF" : "stock");
  const said = [g.era, g.kind && kind(g.kind)].filter(Boolean).join(", ");
  const marks = [g.era && `era ${g.era === era ? "right" : "wrong"}`,
                 g.kind && `type ${g.kind === r.kind ? "right" : "wrong"}`].filter(Boolean).join(", ");
  return `Your guess: ${said}. ${marks[0].toUpperCase()}${marks.slice(1)}.`;
}

// Which copy of a chart to keep when another tab saved the same set: never one that is behind (unfinished where
// this one is finished, or fewer bars in), and otherwise the one changed more often (rev counts the visitor's edits).
function newerChart(mine, theirs) {
  if (!!mine.finished !== !!theirs.finished) return theirs.finished ? theirs : mine;
  if (mine.k !== theirs.k) return theirs.k > mine.k ? theirs : mine;
  return (theirs.rev | 0) > (mine.rev | 0) ? theirs : mine;
}

function mergeSettings(saved) {
  const s = { ...clone(SETTINGS), ...(saved && typeof saved === "object" ? saved : {}) };
  s.ma = { ...SETTINGS.ma, ...(saved?.ma || {}) };
  if (!SPEEDS.includes(s.speed)) s.speed = SETTINGS.speed;
  if (!Number.isInteger(s.guideStep) || s.guideStep < 0 || s.guideStep >= GUIDE_STEPS) s.guideStep = 0;
  return s;
}

function freshState(resp, settings) {
  return {
    schema: 1, version: resp.version, set: resp.set, avoid: resp.avoid ?? null, created: new Date().toISOString(),
    active: 0, settings,
    charts: resp.charts.slice(0, N_CHARTS).map((c) => ({
      cursor: c.cursor, k: 0, nCtx: resp.n_context, nReplay: resp.n_replay, bars: [], finished: false, finishedAt: null,
      reveal: null, drawings: [], trades: [], nextId: 1, view: null, rev: 0,
    })),
  };
}

// ---------------------------------------------------------------- the guide

// How to play: eight steps, each a heading, a few numbered things to do and a picture, with Show me on steps 2 to 7
// (see TradeTest.showMe). The pictures are inline SVG in the site's colours (their tt-i- classes are in tradetest.css),
// drawn on whole and half pixels at the size a desktop shows them, so they are crisp there; a phone scales them down.
// Every string here is this file's own markup.
const GUIDE_STEPS = 8;

const keysList = (cls) => h("dl", { class: cls },
  KEYS.map(([k, what]) => h("div", null, h("dt", null, h("kbd", { text: k })), h("dd", { text: what }))));

// A seeded random walk of n daily bars [open, high, low, close] whose last close is last: the same picture every time.
function walkRows(n, seed, last, vol, drift = 0) {
  let s = seed;
  const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  const closes = [0];
  for (let i = 0; i < n; i++) closes.push(closes[i] + drift + (rnd() - 0.5) * 2 * vol);
  const shift = last - closes[n];
  return closes.slice(1).map((c, i) => {
    const o = closes[i] + shift + (rnd() - 0.5) * vol * 0.4, close = c + shift;
    return [o, Math.max(o, close) + rnd() * vol * 0.6, Math.min(o, close) - rnd() * vol * 0.6, close];
  });
}

// Candles, the first centred on x0 and the rest step px apart, prices to pixels by y: 7px bodies on whole pixels,
// wicks on half pixels.
function candleMarks(rows, x0, step, y) {
  return rows.map(([o, hi, lo, c], n) => {
    const x = Math.round(x0 + n * step) + 0.5, tone = c >= o ? "up" : "down";
    const top = Math.round(y(Math.max(o, c))), bottom = Math.max(top + 1, Math.round(y(Math.min(o, c))));
    return `<path class="tt-i-wick-${tone}" d="M${x} ${Math.round(y(hi))}V${Math.round(y(lo))}"/>`
      + `<rect class="tt-i-${tone}" x="${x - 3.5}" y="${top}" width="7" height="${bottom - top}"/>`;
  }).join("");
}

// A picture w x hgt, at most its own size; label says what it shows.
const picture = (w, hgt, label, body) => `<svg class="tt-i" viewBox="0 0 ${w} ${hgt}" style="max-width:${w}px" `
  + `role="img" aria-label="${label}">${body}</svg>`;
// Text: cls picks its colour, size and weight (tt-i-t text, -b bright, -m muted, -g and -r the up and down text
// shades; -s 12px, -l 13px; -w 600). Parts are [text, cls] runs on one line.
const say = (x, y, cls, text, anchor) => `<text x="${x}" y="${y}" class="${cls}"`
  + `${anchor ? ` text-anchor="${anchor}"` : ""}>${Array.isArray(text)
    ? text.map(([t, c]) => `<tspan class="${c}">${t}</tspan>`).join("") : text}</text>`;
// One of the toolbar's icons at 16px, as in its buttons, with its top left at x, y.
const svgIcon = (name, x, y) => `<g class="tt-i-ic" transform="translate(${x} ${y}) scale(0.8889)">${ICONS[name]}</g>`;

// Step 1: the history left of the dashed Replay starts line, the replay hidden to its right.
function picGame() {
  const y = (p) => 80 - (p - 98) * 12;
  const ghosts = [[22, 104], [16, 110], [26, 100]].map(([tall, top], n) => `<rect class="tt-i-ghost" `
    + `x="${247.5 + n * 10}" y="${top + 0.5}" width="6" height="${tall}"/>`).join("");
  const ticks = [[-20, 40.5], [-10, 140.5], [0, 240.5], [10, 340.5]]
    .map(([bar, x]) => say(x, 158, "tt-i-m", bar < 0 ? `−${-bar}` : String(bar), "middle")).join("");
  return picture(400, 164, "A chart: the history left of the dashed Replay starts line, the replay hidden to its "
    + "right until you step into it",
    '<rect class="tt-i-hidden" x="246" y="4" width="154" height="138"/><path class="tt-i-axis" d="M0 142.5H400"/>'
    + candleMarks(walkRows(24, 107, 100, 1.3, 0.3), 10, 10, y) + ghosts
    + '<path class="tt-i-replay" d="M245.5 4V142"/>' + ticks
    + say(10, 16, "tt-i-m", "History") + say(252, 16, "tt-i-m", "Replay starts")
    + say(331, 66, "tt-i-t tt-i-s", "Hidden until you step", "middle"));
}

// Step 2: the replay controls as the toolbar shows them, with their keys under them (none on a touch screen).
function picReplay(touch) {
  const btn = (x, w) => `<rect class="tt-i-btn" x="${x + 0.5}" y="10.5" width="${w - 1}" height="31" rx="5.5"/>`;
  const kbd = (cx, key) => {
    const w = Math.max(18, Math.round(key.length * 6.5 + 8)), x = Math.round(cx - w / 2);
    return `<rect class="tt-i-kbd" x="${x + 0.5}" y="58.5" width="${w - 1}" height="17" rx="3.5"/>`
      + say(cx, 71, "tt-i-t", key, "middle");
  };
  const under = touch ? "" : kbd(28, "→") + kbd(68, "Shift+→") + kbd(108, "P");
  return picture(400, touch ? 52 : 80, "The replay controls: Next bar, +5, Play, the speed, the bar counter and Finish",
    '<rect class="tt-i-card" x="0.5" y="0.5" width="399" height="51" rx="6"/>'
    + btn(12, 32) + svgIcon("next", 20, 18) + btn(48, 40) + say(68, 30.5, "tt-i-t tt-i-l", "+5", "middle")
    + btn(92, 32) + svgIcon("play", 100, 18) + btn(128, 42) + say(149, 30.5, "tt-i-t tt-i-l", "2×", "middle")
    + say(181, 30.5, "tt-i-s", [["Bar 23", "tt-i-b"], [" of 100", "tt-i-m"]])
    + btn(300, 88) + svgIcon("flag", 314, 18) + say(336, 30.5, "tt-i-t tt-i-l", "Finish") + under);
}

// Step 3: the drawing tools with their toolbar icons and how each is placed.
function picTools(touch) {
  const one = touch ? "1 tap" : "1 click";
  const how = { trend: "2 points", ray: "Its start, then its direction", channel: "2 points, then its width",
                hline: `${one} on a price`, vline: `${one} on a bar`, zone: "2 corners",
                demand: "Zone labelled Demand", supply: "Zone labelled Supply" };
  return '<div class="tt-guide-tools">' + TOOLS.filter((t) => how[t.tool]).map((t) => `<div class="is-${t.tool}">`
    + `<span class="tt-guide-tool">${icon(t.tool)}</span><span><b>${t.name}</b><small>${how[t.tool]}</small></span>`
    + (touch ? "" : `<kbd>${t.key}</kbd>`) + "</div>").join("") + "</div>";
}

// Step 4: a long placed with three clicks: entry at the last price, the stop 1R below, the target 2R above.
function picTrade() {
  const y = (p) => 104 - (p - 100) * 9;
  const mark = (cy, n) => `<circle class="tt-i-mark" cx="276" cy="${cy}" r="8"/>`
    + say(276, cy + 4, "tt-i-mk", String(n), "middle");
  const tag = (cy, cls, text, textCls) => `<rect class="${cls}" x="356.5" y="${cy - 8}" width="41" height="16" rx="2"/>`
    + say(377, cy + 4, textCls, text, "middle");
  return picture(400, 168, "A long trade on the chart: the entry at the last price, the stop below it 1R away and the "
    + "target above it 2R away, clicked in that order, 1, 2 and 3",
    candleMarks(walkRows(21, 28, 100, 1.1, -0.2), 8, 9, y) + '<path class="tt-i-last" d="M0 104.5H352"/>'
    + '<rect class="tt-i-reward" x="196" y="33" width="104" height="71"/>'
    + '<rect class="tt-i-risk" x="196" y="105" width="104" height="35"/>'
    + '<path class="tt-i-tline" d="M196 32.5H300"/><path class="tt-i-eline" d="M196 104.5H300"/>'
    + '<path class="tt-i-sline" d="M196 140.5H300"/>'
    + say(201, 26, "tt-i-g", "Target") + say(201, 98, "tt-i-t", "Entry") + say(201, 155, "tt-i-r", "Stop")
    + mark(104.5, 1) + mark(140.5, 2) + mark(32.5, 3)
    + '<path class="tt-i-br-g" d="M304 32.5H308.5V103.5H304"/><path class="tt-i-br-r" d="M304 105.5H308.5V140.5H304"/>'
    + say(314, 72, "tt-i-g tt-i-s tt-i-w", "2R") + say(314, 127, "tt-i-r tt-i-s tt-i-w", "1R")
    + '<path class="tt-i-axis" d="M352.5 0V168"/>'
    + tag(32.5, "tt-i-tag-g", "108.00", "tt-i-g") + tag(104.5, "tt-i-tag-last", "100.00", "tt-i-tag-text")
    + tag(140.5, "tt-i-tag-r", "96.00", "tt-i-r"));
}

// Step 5: a trade as the Trades panel lists it, its stop moved up since it was placed. Its numbers agree: risk is
// 4.00 (the 100.00 it was placed at, to the 96.00 stop in effect at the fill), so 105.00 is +1.20R and +4.79% on the
// 100.20 fill.
function picRow() {
  return picture(400, 140, "A trade in the Trades panel: Open, Long #1 at +1.20R, its stop moved from 96.00 to 97.50, "
    + "with Close at next open, Edit stop/target and a note",
    '<rect class="tt-i-card" x="0.5" y="0.5" width="399" height="139" rx="6"/>'
    + '<rect class="tt-i-pill" x="12" y="13" width="42" height="17" rx="8.5"/>'
    + say(33, 25.5, "tt-i-t tt-i-w", "Open", "middle")
    + say(62, 26, "tt-i-l tt-i-w", [["Long", "tt-i-g"], [" #1", "tt-i-b"]])
    + say(388, 26, "tt-i-l", [["+1.20R", "tt-i-g tt-i-w"], [" +4.79%", "tt-i-m tt-i-s tt-i-4"]], "end")
    + say(12, 49, "tt-i-t tt-i-s tt-i-4", "market · stop 97.50 (was 96.00) · target 108.00")
    + say(12, 67, "tt-i-m tt-i-s tt-i-4", "Placed at bar 12 · filled bar 13 at 100.20")
    + say(12, 91, "tt-i-m tt-i-s", "Close at next open") + say(134, 91, "tt-i-m tt-i-s", "Edit stop/target")
    + '<rect class="tt-i-field" x="12.5" y="103.5" width="375" height="25" rx="5.5"/>'
    + say(22, 120, "tt-i-m tt-i-s tt-i-4", "Note: why this trade, why you got out"));
}

// Step 6: the tabs and the reveal card after a chart is finished. done is the word the tabs use at this width
// ("Finished", or "Done" where ten tabs are narrow).
function picReveal(done) {
  const tab = (x, n, status) => say(x, 21, "tt-i-b tt-i-l", `Chart ${n}`) + say(x, 38, "tt-i-s tt-i-4", status);
  return picture(400, 186, `After a finish: Chart 1’s tab says ${done}, +1.50R, and the reveal card names the stock, `
    + "its dates and what the price did",
    '<rect class="tt-i-card" x="0.5" y="0.5" width="399" height="47" rx="6"/>'
    + '<path class="tt-i-sep" d="M133.5 1V47M266.5 1V47"/><rect class="tt-i-sel" x="1" y="45" width="132" height="2"/>'
    + tab(12, 1, [[`${done} · `, "tt-i-t"], ["+1.50R", "tt-i-g tt-i-w"]]) + tab(145, 2, [["Bar 23 of 100", "tt-i-m"]])
    + tab(278, 3, [["Not started", "tt-i-m"]])
    + '<rect class="tt-i-card" x="0.5" y="60.5" width="399" height="125" rx="6"/>'
    + say(14, 84, "tt-i-l tt-i-4", [["This was ", "tt-i-t"], ["Example Industries (EXMP)", "tt-i-b tt-i-xl"]])
    + say(14, 103, "tt-i-m tt-i-s tt-i-4", "Stock · Industrials, Machinery · NYSE")
    + say(14, 120, "tt-i-t tt-i-s tt-i-4", "Replay: Mar 12, 2019 to Aug 5, 2019, on daily bars.")
    + say(14, 137, "tt-i-s tt-i-4", [["Buy and hold over the replay: ", "tt-i-m"], ["+8.4%", "tt-i-g tt-i-w"]])
    + say(14, 154, "tt-i-s tt-i-4", [["After you finished, the price went ", "tt-i-m"], ["−3.1%", "tt-i-r tt-i-w"],
                                      [" to the last bar.", "tt-i-m"]])
    + say(14, 171, "tt-i-s tt-i-4", [["Your trades: ", "tt-i-m"], ["+1.50R", "tt-i-g tt-i-w"],
                                      [" over 2 closed trades", "tt-i-m"]]));
}

// Step 7: three trades in R adding up to the set's score.
function picScore() {
  const row = (cy, label, from, to, r) => say(12, cy + 4, "tt-i-m tt-i-s tt-i-4", label)
    + `<rect class="tt-i-bar-${r > 0 ? "up" : "down"}" x="${Math.min(from, to)}" y="${cy - 7}" `
    + `width="${Math.abs(to - from)}" height="14"/>`
    + say(388, cy + 4, `tt-i-s tt-i-w ${r > 0 ? "tt-i-g" : "tt-i-r"}`, `${r > 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R`,
          "end");
  const ticks = [["−1R", 150.5], ["0", 200.5], ["+1R", 250.5], ["+2R", 300.5]]
    .map(([t, x]) => `<path class="tt-i-axis" d="M${x} 120V124"/>` + say(x, 137, "tt-i-m", t, "middle")).join("");
  return picture(400, 142, "Three trades in R, +2.00R, −1.00R and +1.40R, for a score of +2.40R so far",
    say(0, 15, "tt-i-l tt-i-4", [["Set so far: ", "tt-i-t"], ["+2.40R", "tt-i-g tt-i-w"],
                                   [" on 1 finished chart · 67% won", "tt-i-t"]])
    + '<path class="tt-i-axis" d="M0 30.5H400M150 120.5H301"/><path class="tt-i-zero" d="M200.5 38V120"/>'
    + row(52, "Long #1 · hit its target", 201, 301, 2) + row(80, "Short #2 · stopped out", 150, 200, -1)
    + row(108, "Long #3 · closed at the open", 201, 271, 1.4) + ticks);
}

// The steps, worded for this screen: touch (tap, no keys), narrow (the panels and the order's confirm bar under the
// chart), phone (the head's buttons and the tool row short of room: Help, Report, CSV and Copy, the row scrolling) and
// done (the word a finished chart's tab uses: "Done" where ten tabs are narrow).
function guideSteps({ touch, narrow, phone, done }) {
  // Key caps, and a control's name kept on one line with them (nb).
  const k = (...keys) => (touch ? "" : ` ${keys.map((key) => `<kbd>${key}</kbd>`).join(" or ")}`);
  const nb = (html) => `<span class="tt-guide-nb">${html}</span>`;
  const ic = (name) => `<span class="tt-guide-ic">${icon(name)}</span>`;
  const press = touch ? "Tap" : "Press", click = touch ? "Tap" : "Click";
  const [report, csv, copy] = phone ? ["Report", "CSV", "Copy"] : ["Download report", "Trades CSV", "Copy result"];
  const kinds = '<dl class="tt-guide-kinds">'
    + "<div><dt>Market</dt><dd>Fills at the next bar’s open.</dd></div>"
    + "<div><dt>Limit</dt><dd>Below the price for a long, above it for a short. Fills when the price comes back to "
    + "it.</dd></div><div><dt>Stop entry</dt><dd>Above the price for a long, below it for a short. Fills when the "
    + "price breaks through.</dd></div></dl>";
  // Step 5's trade labels, folded away: the step is about what to do.
  const statuses = '<details class="d-details tt-guide-rules tt-guide-legend"><summary>What each label means</summary>'
    + '<dl class="tt-guide-status">' + [
      ["pending", "Pending", "waiting to fill"], ["open", "Open", "filled and running"],
      ["closing", "Closing", "exits at the next open"], ["closed", "Closed", "out, its R counted"],
      ["cancelled", "Cancelled", "cancelled before it filled"], ["missed", "Missed", "the price gapped past it"],
      ["expired", "Expired", "unfilled at the finish"],
    ].map(([cls, word, what]) => `<div><dt><span class="d-pill tt-guide-pill is-${cls}">${word}</span></dt>`
      + `<dd>${what}</dd></div>`).join("") + "</dl></details>";
  const note = (html) => `<p class="tt-guide-tip">${html}</p>`;
  const tap = press.toLowerCase();
  return [
    { title: "The game", pic: picGame(),
      do: ["Read the history, every bar left of the dashed <b>Replay starts</b> line. Each chart is a real "
             + "US-listed stock or ETF with its name and dates hidden, rescaled so the last close before the replay "
             + "is 100.00.",
           "Step forward up to 100 bars, drawing and trading as you go. There’s no going back.",
           "<b>Finish</b> a chart to see what it was. Trades score in R, the result divided by the risk.",
           "Play the ten charts in any order from the tabs."] },
    { title: "Move through time", pic: picReplay(touch), show: true,
      do: [`${press} ${nb(`<b>Next bar</b>${k("→", "Space")}`)} for one bar, or `
             + `${nb(`<b>+5</b>${k("Shift+→")}`)} for five.`,
           `${press} ${nb(`<b>Play</b>${k("P")}`)} to step on its own. The menu beside it sets the speed.`,
           touch ? "Pinch to zoom and drag sideways to pan. <b>Latest</b>, at the chart’s right edge, brings back "
               + "the newest bars."
             : "Scroll over the chart to zoom and drag to pan. <b>Latest</b>, "
               + `${nb(`<kbd>F</kbd> or ${ic("fit")}`)} brings back the newest bars.`
               + (narrow ? " Off the chart, the wheel scrolls the page." : ""),
           `${nb(`${ic("indicators")} <b>Indicators</b>`)} adds moving averages or hides volume`
             + `${phone ? ". It’s near the end of the tool row" : ""}.`] },
    { title: "Draw on the chart", pic: picTools(touch), show: true,
      do: [touch ? "Pick a tool above the chart, then tap the chart as shown."
               + (phone ? " Swipe the tool row for the zones, the magnet and undo." : "")
             : "Pick a tool above the chart or press its number, then click or drag on the chart as shown.",
           touch ? `Name a drawing in the <b>Drawings</b> panel${narrow ? " under the chart" : ""}.`
             : "Type right after drawing to label it, or double-click it later.",
           `${click} a drawing to select it, then drag its handles or its body.`,
           touch ? `Recolour, extend or delete it in the <b>Drawings</b> panel. ${ic("undo")} undoes.`
             : "<kbd>Delete</kbd> removes it. Recolour or extend it in the <b>Drawings</b> panel. "
               + "<kbd>Esc</kbd> cancels a half-made drawing and <kbd>Ctrl+Z</kbd> undoes.",
           `The ${nb(`magnet ${ic("magnet")}${k("M")}`)} snaps to each bar’s open, high, low or close.`] },
    { title: "Place a trade", pic: picTrade(), show: true, more: kinds,
      do: [`${press} ${nb(`<b>Long</b>${k("B")}`)} or ${nb(`<b>Short</b>${k("S")}.`)}`,
           `${click} your entry, stop and target, in that order: 1, 2 and 3 in the picture.`,
           `For a market order, ${click.toLowerCase()} the last price or its tag at the right edge.`,
           `Check the ${narrow ? "order in the bar under the chart" : "order ticket"}, then ${tap} `
             + `${nb(`<b>Place</b>${k("Enter")}.`)}`,
           `Or ${tap} <b>New order</b> in the Trades panel and type the prices. Pick <b>At a price</b> for a limit or `
             + "stop entry. Where your price sits decides which."] },
    { title: "Manage your trades", pic: picRow(), show: true, more: statuses,
      do: [`Each trade gets a card in the <b>Trades</b> panel${narrow ? " below the chart" : ""}. ${click} its label `
             + "on the chart to select it.",
           touch ? "Tap a trade, then drag its stop or target line, or tap <b>Edit stop/target</b>."
             : "Drag a stop or target line on the chart to move it, or press <b>Edit stop/target</b>.",
           "<b>Close at next open</b> gets you out, and it can’t be undone. <b>Cancel order</b> works until an order "
             + "fills.",
           "Add a note to remember why you took a trade or got out.",
           "Play pauses when an order fills or a trade exits. Turn that off in the speed menu."] },
    { title: "Finish and reveal", pic: picReveal(done), show: true,
      do: [`${press} <b>Finish</b> at the right of the toolbar when you’re done. At bar 100 the replay waits for you.`,
           `Guess the era and type if you like, then ${tap} <b>Finish and reveal</b>.`,
           "Open trades close at the next bar’s open, or at the last close at bar 100. Unfilled orders expire.",
           "The reveal shows the name, dates, real prices and what came next.",
           `${press} <b>Next</b>, where Finish was, for the next chart. Each tab shows its bar, or ${done} and its `
             + "result."] },
    { title: "Your score", pic: picScore(), show: true,
      more: note("<b>Ahead of 60% of random-entry runs</b> means your trades beat 60% of runs that entered on "
        + "random bars with your stops and targets."),
      do: ["R is a trade’s result divided by its risk, the distance from your entry to your stop.",
           "A win of twice the risk scores +2R. A stopped trade scores about −1R, more on a gap through the stop.",
           "Your set’s score appears above the tabs once you finish a chart.",
           `<b>${report}</b> saves your results as a web page, <b>${csv}</b> saves every trade, and <b>${copy}</b> `
             + "copies a one-line summary.",
           "<b>New set</b> deals ten fresh charts and clears this one, so download your report first."] },
    // A finger has no keys to learn: the rules come first, the keys folded away for a keyboard.
    touch
      ? { title: "Rules", keys: "folded",
          do: ["<b>Rules in detail</b> has exactly how orders fill, exit and score.",
               "With a keyboard, <b>Keyboard</b> lists the shortcuts."] }
      : { title: "Rules and keys", keys: true,
          do: ["These keys work on the chart and its panels, but not while you type in a field.",
               "<b>Rules in detail</b> has exactly how orders fill, exit and score."] },
  ];
}

// The part of els on screen: the box round them, cut to every box they scroll or clip in (a phone's tool row, the
// side panel) and to the screen below the top bar. null when none of it is.
function shownBox(els) {
  const rects = els.map((el) => el.getBoundingClientRect()).filter((r) => r.width || r.height);
  if (!rects.length) return null;
  const box = { left: Math.min(...rects.map((r) => r.left)), top: Math.min(...rects.map((r) => r.top)),
                right: Math.max(...rects.map((r) => r.right)), bottom: Math.max(...rects.map((r) => r.bottom)) };
  const cut = (r) => {
    box.left = Math.max(box.left, r.left); box.top = Math.max(box.top, r.top);
    box.right = Math.min(box.right, r.right); box.bottom = Math.min(box.bottom, r.bottom);
  };
  for (let p = els[0].parentElement; p && p !== document.body; p = p.parentElement) {
    const style = getComputedStyle(p);
    if (style.overflowX !== "visible" || style.overflowY !== "visible") cut(p.getBoundingClientRect());
  }
  cut({ left: 0, top: barCover(), right: innerWidth, bottom: innerHeight });
  return box.right - box.left > 1 && box.bottom - box.top > 1 ? box : null;
}

// Show me's rings round what of each group of controls is on screen, each stopping short of the toast at the foot of
// the screen where it would run behind it, and each ring's tip: under it, else over it, else beside it, wherever is
// first on screen and clear of the rings, the tips placed before it and the toast (under it when nowhere is).
function placeSpots(marks, toast) {
  const t = toast.getBoundingClientRect(), gap = 8, top = barCover() + 4;
  const hits = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  const rings = marks.map(({ els, ring, pad }) => {
    const box = shownBox(els);
    if (box && box.left - pad < t.right && t.left < box.right + pad) box.bottom = Math.min(box.bottom, t.top - 4 - pad);
    const r = box && box.bottom - box.top > 1 ? { left: box.left - pad, top: box.top - pad, right: box.right + pad,
                                                  bottom: box.bottom + pad } : null;
    ring.hidden = !r;
    if (r) {
      Object.assign(ring.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.right - r.left}px`,
                                  height: `${r.bottom - r.top}px` });
    }
    return r;
  });
  const taken = [t, ...rings.filter(Boolean)];
  marks.forEach(({ tip }, i) => {
    const r = rings[i];
    tip.hidden = !r;
    if (!r) return;
    const w = tip.offsetWidth, tall = tip.offsetHeight;
    const x = clamp((r.left + r.right - w) / 2, gap, Math.max(gap, innerWidth - w - gap));
    const y = (r.top + r.bottom - tall) / 2;
    const places = [[x, r.bottom + gap], [x, r.top - gap - tall], [r.left - gap - w, y], [r.right + gap, y]]
      .map(([left, at]) => ({ left, top: at, right: left + w, bottom: at + tall }));
    const fits = (b) => b.left >= gap - 0.5 && b.right <= innerWidth - gap + 0.5 && b.top >= top
      && b.bottom <= innerHeight - gap;
    const place = places.find((b) => fits(b) && !taken.some((a) => hits(a, b)))
      || places.slice(0, 2).find((b) => fits(b) && !hits(b, t)) || places[0];
    Object.assign(tip.style, { left: `${place.left}px`, top: `${place.top}px` });
    taken.push(place);
  });
}

function ensureStyles() {
  if (document.getElementById("tt-styles")) return Promise.resolve();
  const href = new URL(`./tradetest.css${V}`, import.meta.url).href;
  const link = h("link", { id: "tt-styles", rel: "stylesheet", href });
  const loaded = new Promise((resolve) => {
    link.addEventListener("load", resolve, { once: true });
    link.addEventListener("error", resolve, { once: true });
    setTimeout(resolve, 3000);
  });
  document.head.append(link);
  return loaded;
}

export async function mount(root) {
  root.classList.add("tt-root");
  const [sim, chartLib] = await Promise.all([import(`./sim.js${V}`), import(`./chart.js${V}`), ensureStyles()]);
  if (!root.isConnected) return null;
  const app = new TradeTest(root, sim, chartLib);
  // The browser tests reach the running page through its root, when they ask for it before the page loads.
  if (window.__ttTest === true) Object.defineProperty(root, "tradetest", { value: app, configurable: true });
  app.start();
  return app;
}

// ---------------------------------------------------------------- the page

class TradeTest {
  constructor(root, sim, chartLib) {
    this.root = root; this.sim = sim; this.lib = chartLib;
    this.state = null; this.tool = "cursor"; this.selection = null; this.draft = null; this.ticket = null;
    this.undo = new Map(); this.queued = 0; this.stepping = false; this.playing = false; this.finishing = false;
    this.loads = new Map(); this.notices = new Map(); this.frame = 0; this.saveTimer = 0;
    this.listeners = []; this.labelArm = null; this.held = null; this.failedRows = new WeakSet();
    // How many rows of each chart ("<set>.<i>") localStorage and the journal hold, so each is written only when it
    // changes.
    this.rowsLocal = new Map(); this.rowsJournal = new Map();
    this.journal = new Journal(KEY);
    this.tabId = Math.random().toString(36).slice(2);
    // Charts another tab is moving: index -> { set, at (when it last said so), k (its bar), from }.
    this.remote = new Map(); this.beatAt = 0; this.lockShown = null;
  }

  start() {
    const saved = loadState();
    this.settingsOnly = mergeSettings(saved?.settings);
    this.build();
    this.watch();
    this.resume(saved);
  }

  // Go on with the saved set: localStorage's copy or the journal's, chart by chart whichever is further along, with
  // the finished charts' rows from their own keys. Without one, a new set.
  async resume(local) {
    const journal = await this.journal.read();
    if (this.dead) return;
    let journaled = null;
    try { journaled = JSON.parse(journal.get("state") || "null"); } catch { /* a damaged copy: not used */ }
    this.state = pickSaved(local, journaled);
    if (this.state) {
      this.state.settings = mergeSettings(this.state.settings);
      this.state.active = Math.min(N_CHARTS - 1, Math.max(0, this.state.active | 0));
      this.hydrate(this.state, journal);
    }
    this.pruneBars(journal);
    this.resumed = true;
    this.render();
    // A first visit opens the guide at its first step, until it has been closed once.
    if (!this.settings.seenIntro) this.openGuide(0, { opener: null, first: true });
    if (this.state) this.openChart(this.state.active, { initial: true }).then(() => this.checkSet());
    else this.newSet({ confirm: false });
  }

  // Saved rows let a set open without the server. When nothing was asked of it, one quiet /bars for an open chart
  // still finds out at once that the charts were updated, so the visitor hears it before trying to step.
  async checkSet() {
    const state = this.state;
    if (!state || state.expired || this.contacted || this.dead) return;
    const i = state.charts[state.active].finished ? state.charts.findIndex((c) => !c.finished) : state.active;
    if (i < 0) return;
    try {
      await this.call("bars", { cursor: state.charts[i].cursor }, { quiet: true });
    } catch (err) {
      if (this.state === state && (err.code === "expired" || err.code === "bad_token")) this.failedNotice(err);
    }
  }

  // Rows for a saved state's charts, from their own keys in localStorage or the journal, whichever holds enough for
  // the chart's bar (rows that a state saved by an earlier version still carries come first). A chart with none
  // gets them from the server when it is shown, or in the background.
  hydrate(state, journal = null) {
    state.charts.forEach((chart, i) => {
      chart.bars = rowsFor(chart.bars, chart) || this.storedRows(state.set, i, chart, journal) || [];
    });
    return state;
  }

  // A chart's rows from this browser's storage, or null. What each store holds is noted, so the next save writes
  // only what differs (rows saved past the chart's bar, after a crash, are written again cut to it).
  storedRows(set, i, chart, journal = null) {
    const id = `${set}.${i}`;
    const read = (text, held) => {
      try {
        const raw = JSON.parse(text || "null"), rows = rowsFor(raw, chart);
        if (rows) held.set(id, raw.length);
        return rows;
      } catch {
        return null;
      }
    };
    let text = null;
    try { text = localStorage.getItem(BARS_KEY + id); } catch { /* storage blocked */ }
    return read(text, this.rowsLocal) || (journal ? read(journal.get(`bars.${id}`), this.rowsJournal) : null);
  }

  // Rows kept for sets this page no longer holds go, here and in the journal.
  pruneBars(journal) {
    const keep = this.state ? `${this.state.set}.` : null;
    const stale = (key, prefix) => key.startsWith(prefix) && !(keep && key.startsWith(prefix + keep));
    try {
      for (let n = localStorage.length - 1; n >= 0; n--) {
        const key = localStorage.key(n);
        if (key && stale(key, BARS_KEY)) localStorage.removeItem(key);
      }
    } catch { /* storage blocked */ }
    for (const key of journal.keys()) if (stale(key, "bars.")) this.journal.remove(key);
  }

  // A set's chart rows, gone with the set.
  forgetBars(set) {
    for (let i = 0; i < N_CHARTS; i++) {
      try { localStorage.removeItem(`${BARS_KEY}${set}.${i}`); } catch { /* storage blocked */ }
      this.journal.remove(`bars.${set}.${i}`);
      this.rowsLocal.delete(`${set}.${i}`); this.rowsJournal.delete(`${set}.${i}`);
    }
  }

  get settings() { return this.state?.settings || this.settingsOnly; }
  get chart() { return this.state ? this.state.charts[this.state.active] : null; }

  // ------------------------------------------------ lifetime

  on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    this.listeners.push([target, type, fn, opts]);
  }

  // After a pointer's click: a button, tab, link, summary or checkbox inside TradeTest that kept focus lets it go
  // (also the button a closed dialog or the guide gave focus back to). Focus a click put somewhere on purpose (a
  // field, an open dialog or the guide) stays.
  dropFocus() {
    const el = document.activeElement;
    if (!el || el === document.body || this.el.dialog.open || this.el.guide.open || !this.root.contains(el)) return;
    if (el.matches('button, summary, a, [role="tab"], input[type="checkbox"], input[type="radio"]')) el.blur();
  }

  endPress() {
    if (!this.pressing) return;
    // After the click that ends this press has run.
    setTimeout(() => { this.pressing = false; this.flushDeferred(); }, 0);
  }

  watch() {
    this.on(document, "keydown", (e) => this.key(e));
    this.on(document, "keydown", (e) => this.guideKey(e));
    this.on(document, "pointerdown", (e) => this.outside(e), true);
    this.on(window, "storage", (e) => this.storage(e));
    // A save still waiting (during Play) goes now when the page is put away, closed or reloaded, the charts' rows too.
    this.on(window, "beforeunload", () => this.saveNow({ rows: true }));
    this.on(window, "pagehide", () => this.saveNow({ rows: true }));
    this.on(document, "visibilitychange", () => {
      if (document.visibilityState === "hidden") this.saveNow({ rows: true });
    });
    this.on(window, "resize", () => { this.closeMenu(); this.fitStage(); this.toolFade(); });
    // A label typed from the chart into a field out of sight keeps the page where it was (see focusLabel): the
    // browser's scroll to the field's caret, just after a key, is undone; the visitor's own scrolling is kept.
    this.on(window, "scroll", () => {
      const hold = this.labelHold;
      if (!hold) return;
      if (document.activeElement !== hold.input) this.labelHold = null;
      else if (performance.now() - hold.typed < 250) scrollTo({ top: hold.y, behavior: "instant" });
      else hold.y = scrollY;
    }, { passive: true });
    // A drawing just made waits for its label until the pointer moves away or clicks (see key).
    const at = (e) => { this.pointerAt = { x: e.clientX, y: e.clientY }; };
    this.on(document, "pointermove", (e) => {
      at(e);
      const arm = this.labelArm;
      if (arm && Math.hypot(e.clientX - arm.x, e.clientY - arm.y) > 8) this.labelArm = null;
    }, { capture: true, passive: true });
    this.on(document, "pointerdown", (e) => { at(e); this.labelArm = null; }, true);
    this.on(document, "pointerup", at, true);
    // Tabs of this page tell each other when one finishes a chart (see finish and heard).
    try {
      this.channel = new BroadcastChannel(KEY);
      this.channel.onmessage = (e) => this.heard(e.data);
    } catch {
      this.channel = null;
    }
    this.observer = new MutationObserver(() => { if (!this.root.isConnected) this.destroy(); });
    this.observer.observe(document.body, { childList: true, subtree: true });
  }

  destroy() {
    if (this.dead) return;
    this.dead = true;
    this.saveNow({ rows: true });
    this.pause();
    try { this.channel?.close(); } catch { /* already closed */ }
    for (const [target, type, fn, opts] of this.listeners) target.removeEventListener(type, fn, opts);
    this.observer?.disconnect();
    this.view?.destroy();
    this.closeMenu();
    this.clearSpot();
    this.tip?.remove();
    cancelAnimationFrame(this.frame);
    clearTimeout(this.saveTimer); clearTimeout(this.prefetchTimer); clearTimeout(this.lockTimer);
  }

  // Another tab saved. A different, newer set replaces this one; within the same set each chart keeps whichever copy
  // is further along (see newerChart), so a late save from an idle tab can't take back a bar or a finish. Nothing is
  // saved back (but see below), and play, the ticket and the selection stay unless the chart on screen really changed.
  // The saved state carries no bars: a chart's come from this tab's own rows when they reach its bar, else from its
  // own key (a finished chart's is written before the state), else from the server when it is shown. A chart the
  // other tab finished keeps the trades only this tab had (placed in the moments before), and this tab saves it back
  // so both hold them. Drawings and notes always merge (see mergeDrawings), whichever copy is kept. A chart the other
  // tab moved on is read-only here for a moment, as its own word over the channel would make it (see heardMoving),
  // and anything traded here meanwhile on the bars behind it doesn't go in: the visitor is told.
  storage(e) {
    if (e.key !== KEY || !e.newValue || this.dead || !this.resumed) return;
    let next;
    try { next = JSON.parse(e.newValue); } catch { return; }
    if (!usable(next)) return;
    cleanCharts(next);
    if (!this.state || next.set !== this.state.set) {
      // An older set saved late by a tab that hadn't heard of this one: keep ours and put it back.
      if (this.state && String(next.created || "") < String(this.state.created || "")) { this.saveNow(); return; }
      this.hydrate(next);
      this.pause(); this.queued = 0; this.held = null;
      this.state = next;
      this.state.settings = mergeSettings(next.settings);
      this.undo.clear(); this.remote.clear();
      this.selection = null; this.closeTicket(false);
      this.view.cancel();
      this.render();
      this.openChart(this.state.active, { initial: true, save: false });
      return;
    }
    const state = this.state, active = state.active, shown = state.charts[active];
    const half = this.halfMade();
    let merged = false;
    const lost = [];
    state.charts = state.charts.map((mine, i) => {
      const theirs = next.charts[i], keep = newerChart(mine, theirs);
      if (keep === mine) {
        // A finished chart's rows from its key save this one a request.
        if (mine.finished && !complete(mine)) {
          mine.bars = this.storedRows(next.set, i, mine) || mine.bars;
          if (theirs.finished) mine.reveal ||= theirs.reveal;
        }
        const drawn = mergeDrawings(mine, theirs), noted = mergeNotes(mine, theirs);
        if (drawn || noted) { changed(mine); merged = true; }
        return mine;
      }
      theirs.view = mine.view;
      theirs.bars = rowsFor(mine.bars, theirs) || this.fedRows(i, theirs) || this.storedRows(next.set, i, theirs) || [];
      if (theirs.finished && !mine.finished) {
        if (this.held?.index === i) this.held = null;
        if (mergeTrades(theirs, mine)) { changed(theirs); merged = true; }
      } else if (!theirs.finished && theirs.k > mine.k) {
        this.remote.set(i, { set: next.set, at: Date.now(), k: theirs.k, from: "storage" });
        const gone = unseen(mine, theirs);
        if (gone.length) lost.push(unseenWords(gone, i + 1));
      }
      const drawn = mergeDrawings(theirs, mine, mine), noted = mergeNotes(theirs, mine);
      if (drawn || noted) { changed(theirs); merged = true; }
      return theirs;
    });
    const seen = state.settings.seenIntro;
    state.settings = mergeSettings(next.settings);
    state.settings.seenIntro = seen || state.settings.seenIntro;
    if (next.expired) state.expired = true;
    const now = state.charts[active];
    if (now !== shown) {
      const moved = now.k !== shown.k || !!now.finished !== !!shown.finished
        || JSON.stringify(now.trades) !== JSON.stringify(shown.trades);
      if (moved) { this.pause(); this.queued = 0; }
      // An order ticket stays (it goes in at the newest bar, once this tab may trade the chart again); a chart finished
      // there takes it away, and the notice below says why.
      if (now.finished && !shown.finished) this.closeTicket(false);
      this.view.cancel();
      if (half && !now.finished) {
        this.toast([`Chart ${active + 1} changed in another tab, so what you were drawing was dropped.`]);
      }
      const sel = this.selection, list = sel?.kind === "trade" ? now.trades : now.drawings;
      if (sel && !list.some((x) => x.id === sel.id)) this.selection = null;
      const loaded = this.ensureBars(active, { quiet: false });
      // Finished in the other tab: shown whole, as a finish here would be, and said so.
      if (now.finished && !shown.finished) {
        this.clearToasts();
        this.notice("finished", { tone: "info", title: `Chart ${active + 1} was finished in another tab.` });
        loaded.then(() => { if (this.state?.charts[active] === now) this.revealed(active); });
      }
    }
    for (const words of lost) this.toast([words]);
    if (merged) this.saveNow();
    this.lockCheck();
  }

  // ------------------------------------------------ saving

  // The state as saved: every chart without its bars, stamped with the time (the newer copy wins on load).
  serialise() {
    const s = this.state;
    return JSON.stringify({ ...s, saved: Date.now(), charts: s.charts.map((c) => ({ ...c, bars: [] })) });
  }

  // rows: the unfinished charts' rows go to localStorage too (the visitor is leaving the chart or the page, or pausing
  // Play); a finished chart's go there once, with any save. The journal takes every chart's rows as they change.
  saveNow({ rows = false } = {}) {
    // A zoom or pan still on the chart's debounce goes in with this save (a reload mid-play keeps the view).
    this.view?.flushView();
    clearTimeout(this.saveTimer); this.saveTimer = 0;
    if (!this.state) return;
    const text = this.serialise();
    // Rows go before the state that needs them, in both stores, so another tab reading the state finds them.
    this.journalRows();
    this.journal.put("state", text);
    if (rows) this.localRows(false);
    try {
      this.localRows(true);
      localStorage.setItem(KEY, text);
      if (this.notices.has("storage")) this.clearNotice("storage");
    } catch {
      this.notice("storage", { tone: "caution", title: "This browser isn't saving your progress.",
        detail: "Storage is blocked or full. Download your report before you leave the page." });
    }
  }

  // Each chart's rows in the journal, when they changed: one IndexedDB write, so on every bar.
  journalRows() {
    this.state.charts.forEach((chart, i) => {
      const id = `${this.state.set}.${i}`, n = rowCount(chart);
      if (!complete(chart) || this.rowsJournal.get(id) === n) return;
      this.journal.put(`bars.${id}`, JSON.stringify(chart.bars.slice(0, n)));
      this.rowsJournal.set(id, n);
    });
  }

  // Rows in localStorage under each chart's own key, when they changed: the finished charts' (a failure there fails the
  // save), or the unfinished ones' (best effort: the journal and the server have them too).
  localRows(finished) {
    this.state.charts.forEach((chart, i) => {
      const id = `${this.state.set}.${i}`, n = rowCount(chart);
      if (!!chart.finished !== finished || !complete(chart) || this.rowsLocal.get(id) === n) return;
      const text = JSON.stringify(chart.bars.slice(0, n));
      if (finished) localStorage.setItem(BARS_KEY + id, text);
      else {
        try { localStorage.setItem(BARS_KEY + id, text); } catch { return; }
      }
      this.rowsLocal.set(id, n);
    });
  }

  // Changes are saved at once, typing within 300 ms. During Play the journal takes every bar at once and localStorage
  // at most once a second (other tabs hear about each write), and pausing saves what is left.
  save(soon = false) {
    if (this.playing && this.state) {
      this.journalRows();
      this.journal.put("state", this.serialise());
      if (!this.saveTimer) this.saveTimer = setTimeout(() => this.saveNow(), 1000);
      return;
    }
    if (!soon) { this.saveNow(); return; }
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.saveNow(), 300);
  }

  // ------------------------------------------------ building the page

  build() {
    const root = this.root;
    root.replaceChildren();
    this.el = {};
    const e = this.el;
    // A toolbar button: an icon (and optional label) with a tooltip that names its shortcut.
    const tool = (name, glyph, onclick, { key, cls = "tt-tool", label, html } = {}) => h("button", {
      type: "button", class: cls, "aria-label": label ? null : name, "data-tip": name, "data-key": key, onclick,
      html: html ?? icon(glyph) + (label ? `<span>${label}</span>` : ""),
    });
    const sep = (cls = "") => h("span", { class: `tt-sep ${cls}`.trim(), "aria-hidden": "true" });

    e.csv = h("button", { type: "button", class: "d-btn d-btn-quiet", title: "Every scored trade as a spreadsheet",
                          onclick: () => this.download("csv"),
                          html: '<span><span class="tt-wide">Trades </span>CSV</span>' });
    e.copy = h("button", { type: "button", class: "d-btn d-btn-quiet", title: "Copy a one-line summary to share",
                           onclick: () => this.copyResult(),
                           html: '<span><span class="tt-wide">Copy result</span>'
                             + '<span class="tt-narrow">Copy</span></span>' });
    e.newSet = h("button", { type: "button", class: "d-btn", text: "New set",
                             onclick: () => this.newSet({ confirm: true }) });
    e.report = h("button", { type: "button", class: "d-btn", onclick: () => this.download("report"),
                             html: icon("download") + '<span class="tt-wide">Download report</span>'
                               + '<span class="tt-narrow">Report</span>' });
    // How to play, the guide; on a phone it says Help and sits at the end of the title's row.
    e.help = h("button", { type: "button", class: "d-btn tt-help", "data-tip": "How to play", "data-key": "H",
                           "aria-haspopup": "dialog",
                           onclick: (ev) => this.openGuide(undefined, { opener: ev.currentTarget }),
                           html: icon("help") + '<span class="tt-wide">How to play</span>'
                             + '<span class="tt-narrow">Help</span>' });
    // A press on the report, the CSV or Copy result before they have anything to give says why (a finger gets no
    // tooltip); a disabled button still hears the press, only its click is held back.
    const waiting = h("div", { class: "d-actions" }, e.help, e.csv, e.copy, e.newSet, e.report);
    this.on(waiting, "pointerdown", (ev) => {
      const b = ev.target.closest?.("button");
      if (b?.disabled && [e.report, e.csv, e.copy].includes(b)) {
        this.toast(["Finish a chart first. The report, the CSV and Copy cover finished charts."]);
      }
    });
    const head = h("section", { class: "d-head tt-head" },
      h("div", { class: "d-head-left" }, h("h1", { class: "d-title", text: "TradeTest" })), waiting);

    e.notices = h("div", { class: "d-notices tt-notices", "aria-live": "polite" });
    // The set's running score, once a chart is finished.
    e.score = h("p", { class: "tt-score", hidden: true, "aria-live": "polite" });
    // The tabs take arrow keys among themselves (one tab stop, as tabs do); the page's shortcuts ignore them.
    e.tabs = h("div", { class: "d-card tt-tabs", role: "tablist", "aria-label": "Charts" });
    e.tabButtons = [];
    for (let i = 0; i < N_CHARTS; i++) {
      const tab = h("button", { type: "button", class: "tt-tab", role: "tab", "aria-selected": "false",
                                tabindex: i ? "-1" : "0", onclick: () => this.openChart(i) },
        h("b", { text: `Chart ${i + 1}` }), h("span", { text: "Not started" }));
      e.tabButtons.push(tab);
      e.tabs.append(tab);
    }
    this.on(e.tabs, "keydown", (ev) => this.tabKey(ev));
    this.on(e.tabs, "scroll", () => this.toolFade(), { passive: true });

    // The chart card: drawing and trading tools, the replay controls, the chart.
    e.tools = {};
    const tools = h("div", { class: "tt-tools" });
    for (const t of TOOLS) {
      const key = t.also ? `${t.also} or ${t.key}` : t.key;
      e.tools[t.tool] = tool(t.name, t.tool, () => this.setTool(t.tool), { key, cls: `tt-tool is-${t.tool}` });
      tools.append(e.tools[t.tool]);
    }
    // On a phone the tool row scrolls: Long and Short move to its start (with this separator after them).
    tools.append(sep("tt-sep-trade"));
    for (const side of ["long", "short"]) {
      e.tools[side] = tool(`${sideWord(side)} trade`, side, () => this.setTool(side), {
        key: side === "long" ? "B" : "S", cls: `tt-tool is-wide is-${side}`, label: sideWord(side),
      });
      tools.append(e.tools[side]);
    }
    for (const b of Object.values(e.tools)) b.setAttribute("aria-pressed", "false");
    e.magnet = tool("Magnet: snap to open, high, low or close", "magnet", () => this.toggleMagnet(), { key: "M" });
    e.magnet.setAttribute("aria-pressed", "false");
    e.indicators = tool("Indicators", "", (ev) => this.indicatorMenu(ev),
                        { cls: "tt-tool tt-menu-btn", html: icon("indicators") + icon("caret") });
    e.indicators.setAttribute("aria-haspopup", "true");
    e.fit = tool("Fit the chart", "fit", () => this.view.fit(), { key: "F" });
    e.undo = tool("Undo drawing", "undo", () => this.undoRedo(false), { key: "Ctrl+Z" });
    e.redo = tool("Redo drawing", "redo", () => this.undoRedo(true), { key: "Ctrl+Y" });
    tools.append(sep(), e.magnet, e.indicators, e.fit, sep(), e.undo, e.redo);
    // A row that scrolls sideways fades at the edge that has more, so the tools past it can be found.
    e.toolRow = tools;
    this.on(tools, "scroll", () => this.toolFade(), { passive: true });

    e.next = tool("Next bar", "next", () => this.step(1), { key: "→ or Space", cls: "d-btn d-btn-icon" });
    e.next5 = tool("Next 5 bars (+5)", "", () => this.step(5), { key: "Shift+→", cls: "d-btn tt-five", html: "+5" });
    e.next5.dataset.tip = "Next 5 bars";
    e.play = tool("Play", "play", () => this.togglePlay(), { key: "P", cls: "d-btn d-btn-icon" });
    e.speed = tool("Replay speed", "", (ev) => this.speedMenu(ev), { cls: "d-btn tt-speed", html: "" });
    e.speed.setAttribute("aria-haspopup", "true");
    e.progress = h("span", { class: "tt-progress" });
    e.finish = h("button", { type: "button", class: "d-btn tt-finish",
                             onclick: () => (this.chart?.finished ? this.nextChart() : this.finish()) });
    const replay = h("div", { class: "tt-replay" },
      h("div", { class: "d-group" }, e.next, e.next5, e.play, e.speed), e.progress, e.finish);
    const toolbar = h("div", { class: "tt-toolbar", role: "toolbar", "aria-label": "Chart tools" }, tools, replay);

    e.host = h("div", { class: "tt-chart-host" });
    e.toasts = h("div", { class: "tt-toasts", "aria-live": "polite" });
    e.overlay = h("div", { class: "tt-overlay", hidden: true });
    e.draftbar = h("div", { class: "tt-draftbar", hidden: true });
    e.stage = h("div", { class: "tt-stage" }, e.host, e.toasts, e.overlay);
    e.replayNotice = h("div", { class: "tt-replay-notice", hidden: true });
    // While another tab moves this chart: what that means here, and the way to take it over.
    e.movingTitle = h("b"); e.movingDetail = h("span");
    e.takeOver = h("button", { type: "button", class: "d-btn d-btn-quiet", text: "Take over",
                               onclick: () => this.takeOver() });
    e.movingNotice = h("div", { class: "tt-moving-notice", role: "status", hidden: true },
      h("i", { "aria-hidden": "true" }),
      h("p", null, e.movingTitle, " ", e.movingDetail), e.takeOver);
    const card = h("section", { class: "d-card tt-chart-card", "aria-label": "Chart" },
      toolbar, e.movingNotice, e.replayNotice, e.stage, e.draftbar);

    // The side panel: status, the order ticket, trades, drawings, the rules.
    e.status = h("section", { class: "tt-sec tt-status" });
    e.ticket = this.buildTicket();
    // The list heads are made once: a button rebuilt on every render (each bar, during Play) would lose clicks.
    e.tradesTitle = h("h3", null, "Trades");
    e.newOrder = h("button", { type: "button", class: "d-btn d-btn-quiet", html: icon("plus") + "New order",
                               hidden: true, onclick: () => this.openTicket() });
    e.tradesHead = h("div", { class: "tt-sec-head" }, e.tradesTitle, e.newOrder);
    e.trades = h("div", { class: "tt-list" });
    e.drawingsTitle = h("h3", null, "Drawings");
    e.drawingsHead = h("div", { class: "tt-sec-head" }, e.drawingsTitle);
    e.drawings = h("div", { class: "tt-list" });
    // The empty lists' hints, each with its way into the guide; made once for the same reason.
    const guideLink = (text, step) => h("button", {
      type: "button", class: "tt-link", text, onclick: (ev) => this.openGuide(step, { opener: ev.currentTarget }) });
    e.tradesHint = h("p", { class: "d-hint" });
    e.tradesHelp = guideLink("How do I place a trade?", 3);
    // Under a list with trades in it while they can still be changed: the guide's step on managing them.
    e.tradesManage = guideLink("How do I manage a trade?", 4);
    e.tradesManage.hidden = true;
    e.drawingsHint = h("p", { class: "d-hint" });
    e.drawingsHelp = guideLink("How do I draw?", 2);
    e.side = h("aside", { class: "d-card tt-side", "aria-label": "Chart status, orders, trades and drawings" },
      e.status, e.ticket, h("section", { class: "tt-sec" }, e.tradesHead, e.trades, e.tradesManage),
      h("section", { class: "tt-sec" }, e.drawingsHead, e.drawings), this.buildHowTo());
    // The drawings list held back while a label was typed comes back once focus leaves it, but never in the middle of
    // a press: a list rebuilt between pointerdown and click would swallow the click (a swatch, a checkbox).
    this.on(e.side, "focusout", () => setTimeout(() => { if (!this.pressing) this.flushDeferred(); }, 0));
    this.on(e.side, "pointerdown", () => { this.pressing = true; }, true);
    this.on(document, "pointerup", () => this.endPress(), true);
    this.on(document, "pointercancel", () => this.endPress(), true);

    const body = h("div", { class: "tt-body" }, card, e.side);
    e.dialog = h("dialog", { class: "d-card tt-dialog" });
    e.guide = this.buildGuide();
    root.append(head, e.notices, e.score, e.tabs, body, e.dialog, e.guide);
    // A control clicked or tapped hands focus back to the page, so the next key is a shortcut (Space is Next bar)
    // rather than that control again. A click from the keyboard (Enter or Space: detail 0) keeps its focus.
    this.on(root, "click", (ev) => { if (ev.detail > 0) this.dropFocus(); });

    this.view = new this.lib.ChartView(e.host, {
      create: (d) => this.addDrawing(d),
      edit: (d, before) => this.editedDrawing(d, before),
      select: (sel) => this.select(sel, { fromChart: true }),
      draft: (d) => this.fromChartDraft(d),
      level: (trade, levels) => this.changeLevels(trade, levels),
      label: (d) => this.focusLabel(d.id),
      tool: (name) => this.setTool(name),
      // A drawing or order started on the chart holds the replay still: a new bar would cancel it half made.
      started: () => this.pause(),
      view: (v) => {
        const c = this.chart;
        if (c && this.view.model?.chart === c) { c.view = v; this.save(true); }
      },
    });
    this.setupTips();
  }

  buildTicket() {
    const t = {};
    const seg = (cls, text, onclick) => h("button", { type: "button", class: `tt-seg-btn ${cls}`, text, onclick });
    t.long = seg("is-long", "Long", () => this.ticketSide("long"));
    t.short = seg("is-short", "Short", () => this.ticketSide("short"));
    t.market = seg("", "Market", () => this.ticketMarket(true));
    t.priced = seg("", "At a price", () => this.ticketMarket(false));
    const field = (name, label) => {
      const input = h("input", { class: "tt-input", type: "text", inputmode: "decimal", autocomplete: "off",
                                 spellcheck: "false", "aria-label": label,
                                 oninput: () => this.ticketInput(name, input.value) });
      t[name] = input;
      return h("label", { class: "tt-field" }, h("span", { text: label }), input);
    };
    t.facts = h("dl", { class: "tt-facts" });
    t.msg = h("p", { class: "tt-msg", role: "alert" });
    t.place = h("button", { type: "button", class: "d-btn tt-place", onclick: () => this.placeOrder() });
    t.discard = h("button", { type: "button", class: "d-btn", text: "Discard", onclick: () => this.closeTicket(true) });
    t.title = h("h3", { text: "New order" });
    this.t = t;
    return h("section", { class: "tt-sec tt-ticket", hidden: true, "aria-label": "Order ticket" },
      h("div", { class: "tt-sec-head" }, t.title,
        h("div", { class: "tt-seg", role: "group", "aria-label": "Side" }, t.long, t.short)),
      h("div", { class: "tt-entry" }, h("span", { class: "tt-field-label", text: "Entry" }),
        h("div", { class: "tt-seg", role: "group", "aria-label": "Entry type" }, t.market, t.priced)),
      h("div", { class: "tt-fields" }, field("entry", "Entry price"), field("stop", "Stop"), field("target", "Target")),
      t.facts, t.msg, h("div", { class: "tt-ticket-actions" }, t.discard, t.place));
  }

  // The side panel's foot: the rules in full and the keyboard, closed.
  buildHowTo() {
    return h("div", { class: "tt-side-foot" },
      h("details", { class: "d-details tt-how" }, h("summary", { text: "Rules in detail" }),
        h("div", { class: "d-details-body tt-how-body" }, this.rulesBody(), h("h4", { text: "Keyboard" }),
          keysList("tt-keys"), h("p", { class: "tt-how-note", text: DISCLAIMER }))));
  }

  // How the game works, in full: for Rules in detail here and in the guide.
  rulesBody() {
    const p = (...parts) => h("p", { text: parts.join(" ") });
    return [
      h("h4", { text: "Blind charts" }),
      p("Each chart is a real US-listed stock or ETF over a random stretch of daily history. Its name and dates",
        "stay hidden until you finish it. Prices are rescaled so the last bar before the replay closes at 100,",
        "and volume is shown against its average. The reveal gives the real price of the close before the replay",
        "and of the last close, both as traded on the replay’s last day."),
      h("h4", { text: "The replay" }),
      p("The first 150 bars are history. Next bar reveals the replay one bar at a time, up to 100 bars. Your",
        "browser gets the name, the dates and the bars you haven’t reached only when you finish the chart.",
        "There’s no going back, and every order goes in at the newest bar."),
      h("h4", { text: "Orders and fills" }),
      p("Every trade needs an entry, a stop and a target. A market order fills at the next bar’s open. A limit",
        "entry sits below the price for a long and above it for a short; a stop entry sits beyond the price. Both",
        "fill when the price trades through them, at the open if a bar gaps past. If a bar opens past your stop",
        "before an order fills, the order is missed. So is a market or stop entry when a bar gaps past the target."),
      h("h4", { text: "Exits" }),
      p("A trade exits at its stop or its target. When one bar could have hit both, the stop is assumed, and on",
        "the bar a trade fills only its stop counts. A gap through a level exits at the open, which can cost more",
        "than 1R. You can move an open trade’s stop or target, or close it at the next open."),
      h("h4", { text: "Scoring" }),
      p("R is the result divided by the risk. The risk is the distance from your planned entry to the stop in",
        "effect when the trade fills. The planned entry is your limit or stop price, or for a market order the close",
        "you placed it at, so a gap fill better or worse than planned shows up as more or less R. The report also",
        "shows net R, after costs of 0.10% of the price on each fill."),
      p("Finishing a chart reveals it. Open trades close at the next bar’s open, or at the last close once the",
        "replay has ended, and unfilled orders expire. You can guess the era and whether it’s a stock or an ETF",
        "first. The set’s score counts finished charts only. Trades closed on unfinished charts show beside it."),
      h("h4", { text: "The report" }),
      p("Download report saves one web page with your results on the finished charts, compared with random entries",
        "using the same stops and targets and with buying and holding. Trades CSV saves each scored trade, and Copy",
        "result copies a one-line summary. Your drawings and trades stay in this browser. The server only hands",
        "out bars."),
    ];
  }

  // Tooltips with the shortcut, for mouse users; touch gets none.
  setupTips() {
    this.tip = h("div", { class: "tt-tip", role: "tooltip", hidden: true });
    document.body.append(this.tip);
    let timer = 0;
    const hide = () => { clearTimeout(timer); this.tip.hidden = true; };
    this.on(this.root, "pointerover", (e) => {
      const b = e.target.closest?.("[data-tip]");
      if (!b || e.pointerType !== "mouse" || !this.root.contains(b)) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!b.isConnected || this.menu) return;
        const key = b.dataset.key ? [h("kbd", { text: b.dataset.key })] : [];
        this.tip.replaceChildren(h("span", { text: b.dataset.tip }), ...key);
        this.tip.hidden = false;
        const r = b.getBoundingClientRect(), w = this.tip.offsetWidth;
        this.tip.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2))}px`;
        this.tip.style.top = `${r.bottom + 6}px`;
      }, 350);
    });
    this.on(this.root, "pointerout", (e) => { if (e.target.closest?.("[data-tip]")) hide(); });
    this.on(this.root, "pointerdown", hide);
  }

  // ------------------------------------------------ rendering

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.renderNow(); });
  }

  // The chart fills what is left of the first screen below its top, which the score line and the notices above it
  // (and the end-of-replay notice in its card) push down: between 420 and 760px, so its time axis and the toasts at its
  // foot stay in view. Phones keep the fixed height the styles give them.
  fitStage() {
    const stage = this.el?.stage;
    if (!stage || this.dead) return;
    if (innerWidth <= 720) {
      if (stage.style.height) stage.style.removeProperty("height");
      return;
    }
    const top = stage.getBoundingClientRect().top - document.documentElement.getBoundingClientRect().top;
    const height = `${Math.round(clamp(innerHeight - top - 4, 420, 760))}px`;
    if (stage.style.height !== height) stage.style.height = height;
  }

  // The tool row's and the tab strip's edge fade: on the side (or sides) where they are scrolled out of view.
  toolFade() {
    for (const row of [this.el?.toolRow, this.el?.tabs]) {
      if (!row) continue;
      const more = row.scrollWidth - row.clientWidth > 1;
      row.classList.toggle("is-more-left", more && row.scrollLeft > 1);
      row.classList.toggle("is-more-right", more && row.scrollLeft + row.clientWidth < row.scrollWidth - 1);
    }
  }

  renderNow() {
    if (this.dead) return;
    const chart = this.chart;
    this.summary = chart ? this.summaryOf(chart) : null;
    this.renderHead();
    this.renderScore();
    this.renderTabs();
    this.renderToolbar();
    this.renderStage();
    this.renderStatus();
    this.renderTicket();
    this.renderTrades();
    this.renderDrawings();
    this.fitStage();
    this.toolFade();
    if (chart) {
      this.view.update({ chart, results: this.summary.results, settings: this.settings, locked: this.locked(chart),
                         selection: this.selection, draft: this.draft });
    }
  }

  // sim's summary of a chart, or, while its rows aren't in, an empty one (sim would read the missing bars as an order
  // waiting to fill).
  summaryOf(chart) {
    if (complete(chart)) return this.sim.chartSummary(chart);
    return { trades: chart.trades.length, closed: 0, wins: 0, losses: 0, totalR: 0, totalRNet: 0, open: 0, closing: 0,
             openR: 0, pending: 0, cancelled: 0, missed: 0, expired: 0, results: [], unloaded: true };
  }

  // Whether this chart's rows are on their way or can still come (rather than failed, or unreachable for good).
  rowsComing(chart) {
    return !this.state?.expired && !this.failedRows.has(chart) && navigator.onLine !== false;
  }

  // No trading or drawing here: no chart, finished, the set expired, its rows not in, or another tab is moving it.
  locked(chart = this.chart) {
    return !chart || chart.finished || !!this.state?.expired || !complete(chart) || this.movingElsewhere(chart);
  }

  // Another tab is moving this chart: it said so within MOVING_MS, or it said it is further along than this tab's copy
  // (until that copy comes in, by a save or a take over).
  movingElsewhere(chart = this.chart) {
    if (!chart || chart.finished || !this.state || this.state.expired) return false;
    const rec = this.remote.get(this.state.charts.indexOf(chart));
    if (!rec || rec.set !== this.state.set) return false;
    return Date.now() - rec.at < MOVING_MS || rec.k > chart.k;
  }

  // This tab is moving chart index: playing it, or a step asked for or on its way.
  movingHere(index) {
    return this.state?.active === index && (this.playing || this.stepping || this.queued > 0);
  }

  // A drawing, an order or a level being made or moved on the chart right now (not just the view being panned).
  halfMade() {
    const v = this.view, g = v?.gesture?.type;
    return !!v && !!(v.pending || v.placing?.stage > 0 || (g && !VIEW_GESTURES.has(g)));
  }

  // The chart on screen started or stopped moving in another tab. As it starts, this tab stops stepping it, puts its
  // tools down and drops a drawing or an order half made on it (and says so); an order ticket stays, unplaceable,
  // until the chart is this tab's again. A timer looks again when the other tab's word runs out.
  lockCheck() {
    if (this.dead || !this.state) return;
    clearTimeout(this.lockTimer);
    const chart = this.chart, index = this.state.active, moving = this.movingElsewhere(chart);
    const rec = this.remote.get(index);
    if (moving && rec) {
      const wait = rec.at + MOVING_MS - Date.now();
      if (wait > 0) this.lockTimer = setTimeout(() => this.lockCheck(), wait + 20);
    }
    const key = moving ? `${this.state.set}.${index}` : null;
    if (key && this.lockShown !== key) {
      this.pause(); this.queued = 0;
      if (this.halfMade()) {
        this.view.cancel();
        this.toast([`Chart ${index + 1} is moving in another tab, so what you were drawing was dropped.`]);
      }
      this.labelArm = null;
      if (this.tool !== "cursor") this.setTool("cursor");
    }
    this.lockShown = key;
    this.render();
  }

  // The report, the CSV and Copy result have something to give once a chart is finished; until then the report is a
  // plain button, so the first visit's one blue button is the guide's Next.
  renderHead() {
    const e = this.el, scored = !!this.state?.charts.some((c) => c.finished);
    e.report.disabled = !scored; e.csv.disabled = !scored; e.copy.disabled = !scored;
    e.report.classList.toggle("d-btn-primary", scored);
    e.newSet.disabled = this.busyNew;
  }

  // The set so far, from the finished charts: sim's summary and random-entry baseline, as the report scores it, plus
  // the trades closed on charts not finished yet (kept out of the score, never out of sight). missing counts the
  // unfinished charts with trades whose rows aren't in; coming is true while they still can be.
  setScore() {
    const state = this.state, sim = this.sim;
    if (!state) return null;
    const finished = state.charts.filter((c) => c.finished && complete(c));
    if (!finished.length) return null;
    // A finished chart's trades can't change any more (only their notes), so its score is worked out once.
    const key = state.set + state.charts.map((c) => (c.finished ? `${c.finishedAt}:${c.trades.length}:${c.bars.length}`
      : "-")).join("|");
    if (this.scoreKey !== key) {
      const scored = state.charts.map((c) => (c.finished && complete(c) ? c : null));
      const sum = sim.summarize(sim.scoredResults(scored));
      this.scoreKey = key;
      this.scoreCache = { sum, base: sum.trades ? sim.randomBaseline(scored) : null, charts: finished.length };
    }
    const open = state.charts.filter((c) => !c.finished), loaded = open.filter(complete);
    const missing = open.filter((c) => !complete(c) && c.trades.length);
    const other = typeof sim.unfinishedClosed === "function" ? sim.unfinishedClosed(loaded) : null;
    return { ...this.scoreCache, other: other?.trades ? other : null, missing: missing.length,
             coming: missing.every((c) => this.rowsComing(c)) };
  }

  // "Trades on 2 unfinished charts couldn’t be loaded and aren’t counted." (or aren't loaded yet), for the score line
  // and Copy result; null when every unfinished chart's trades are counted.
  missingWords(score, other = !!score?.other) {
    if (!score?.missing) return null;
    const charts = plural(score.missing, `${other ? "other " : ""}unfinished chart`);
    return score.coming ? `Trades on ${charts} aren’t loaded yet, so they aren’t counted.`
      : `Trades on ${charts} couldn’t be loaded and aren’t counted.`;
  }

  renderScore() {
    const el = this.el.score, score = this.setScore(), sim = this.sim;
    el.hidden = !score;
    if (!score) { el.replaceChildren(); return; }
    const { sum, base, charts, other } = score, missing = this.missingWords(score);
    const parts = ["Set so far: "];
    if (sum.trades) {
      parts.push(h("b", { class: toneOf(sum.totalR), text: sim.fmtR(sum.totalR) }),
        ` on ${plural(charts, "finished chart")}`);
      if (sum.winRate != null) parts.push(` · ${Math.round(100 * sum.winRate)}% won`);
      if (base?.percentile != null) {
        parts.push(` · ahead of ${Math.round(100 * base.percentile)}% of random-entry runs`);
      }
    } else parts.push(`${plural(charts, "finished chart")}, no closed trades`);
    if (other) {
      parts.push(h("span", { class: "tt-score-other" }, ` · Also closed on charts you didn’t finish: `
        + `${plural(other.trades, "trade")}, `, h("b", { class: toneOf(other.totalR), text: sim.fmtR(other.totalR) })));
    }
    if (missing) {
      parts.push(h("span", { class: "tt-score-other", "data-missing": String(score.missing) }, ` · ${missing}`));
    }
    el.replaceChildren(...parts);
  }

  renderTabs() {
    const state = this.state;
    // On a phone the tabs scroll sideways; keep the current one in view without moving the page.
    if (state && this.shownTab !== state.active) {
      this.shownTab = state.active;
      const strip = this.el.tabs, tab = this.el.tabButtons[state.active];
      requestAnimationFrame(() => {
        if (strip.scrollWidth <= strip.clientWidth) return;
        const left = tab.offsetLeft - strip.offsetLeft, right = left + tab.offsetWidth;
        if (left < strip.scrollLeft) strip.scrollLeft = left - 8;
        else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth + 8;
      });
    }
    const focused = this.el.tabButtons.indexOf(document.activeElement);
    this.el.tabButtons.forEach((tab, i) => {
      const chart = state?.charts[i];
      const current = state && state.active === i;
      tab.classList.toggle("is-current", !!current);
      tab.setAttribute("aria-selected", current ? "true" : "false");
      // One tab stop: the tab with focus, else the current chart's.
      tab.tabIndex = (focused >= 0 ? focused === i : current || (!state && i === 0)) ? 0 : -1;
      tab.disabled = !state;
      const status = tab.lastElementChild;
      status.className = "";
      let text;
      if (!chart) text = "–";
      else if (chart.finished) {
        const sum = i === state.active ? this.summary : this.summaryOf(chart);
        // "Finished" alone when nothing closed; narrower screens say "Done", so the R fits ten tabs across. The R is
        // in its direction colour (flat is "0.00R"), the word stays quiet.
        const r = sum.closed ? this.sim.fmtR(sum.totalR) : null, sep = r ? " · " : "";
        text = `Finished${sep}${r || ""}`;
        status.className = r ? toneOf(sum.totalR) || "is-flat" : "";
        fill(status, h("span", { class: "tt-tab-long", text: `Finished${sep}` }),
          h("span", { class: "tt-tab-short", text: `Done${sep}` }), r);
      } else if (state.expired) text = "Unavailable";
      else if (chart.k === 0 && !chart.trades.length && !chart.drawings.length) text = "Not started";
      else text = `Bar ${chart.k} of ${chart.nReplay}`;
      if (!chart?.finished) status.textContent = text;
      tab.title = `Chart ${i + 1}: ${text}`;
    });
  }

  // Arrow keys, Home and End move between the chart tabs; Enter or Space opens the one with focus. Only for a tab
  // reached from the keyboard: after a click the keys are the chart's shortcuts.
  tabKey(e) {
    const tabs = this.el.tabButtons, at = tabs.indexOf(document.activeElement);
    if (at < 0 || e.altKey || e.ctrlKey || e.metaKey || !keyboardFocus(tabs[at])) return;
    const to = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0,
                 End: N_CHARTS - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    const tab = tabs[(to + N_CHARTS) % N_CHARTS];
    tabs.forEach((t) => { t.tabIndex = t === tab ? 0 : -1; });
    tab.focus();
  }

  renderToolbar() {
    const e = this.el, chart = this.chart, locked = this.locked(chart), end = atEnd(chart);
    const moving = this.movingElsewhere(chart);
    for (const [name, b] of Object.entries(e.tools)) {
      const on = this.tool === name, trade = name === "long" || name === "short";
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.disabled = name !== "cursor" && (locked || (trade && end));
      // After the last bar, or while another tab moves the chart, the trade tools say why they are off.
      if (trade) {
        const why = moving ? MOVING_HINT : end ? END_HINT : null;
        b.dataset.tip = why || `${sideWord(name)} trade`;
        if (why) delete b.dataset.key; else b.dataset.key = name === "long" ? "B" : "S";
      }
    }
    e.magnet.classList.toggle("is-on", !!this.settings.magnet);
    e.magnet.setAttribute("aria-pressed", this.settings.magnet ? "true" : "false");
    const stack = chart ? this.undoStack() : null;
    e.undo.disabled = locked || !stack?.done.length;
    e.redo.disabled = locked || !stack?.undone.length;
    e.fit.disabled = !chart || !chart.bars.length;
    const canStep = this.canStep(chart);
    e.next.disabled = !canStep; e.next5.disabled = !canStep;
    e.play.disabled = !canStep && !this.playing;
    e.play.innerHTML = icon(this.playing ? "pause" : "play");
    e.play.setAttribute("aria-label", this.playing ? "Pause" : "Play");
    e.play.dataset.tip = this.playing ? "Pause" : "Play";
    e.play.classList.toggle("is-on", this.playing);
    const speed = this.settings.speed;
    e.speed.textContent = `${speed}×`;
    e.speed.setAttribute("aria-label", `Replay speed ${speed}×, ${speed} bar${speed > 1 ? "s" : ""} a second`);
    // Once a chart is finished the button moves on to the next one still open.
    const next = chart?.finished ? this.nextOpen() : null;
    const mode = chart?.finished ? (next == null ? "done" : "next") : "finish";
    if (e.finish.dataset.mode !== mode) {
      e.finish.dataset.mode = mode;
      e.finish.innerHTML = mode === "finish" ? icon("flag") + '<span>Finish<span class="tt-xwide"> chart</span></span>'
        : mode === "next" ? '<span>Next<span class="tt-xwide"> chart</span></span>' + icon("next") : "All finished";
      e.finish.setAttribute("aria-label", mode === "finish" ? "Finish chart" : mode === "next" ? "Next chart"
        : "All finished");
    }
    e.finish.disabled = !chart || mode === "done"
      || (mode === "finish" && (!!this.state?.expired || this.finishing || !complete(chart)));
    e.progress.replaceChildren();
    if (chart) {
      if (chart.finished) e.progress.append("Finished at ", h("b", { text: `bar ${chart.finishedAt}` }));
      else e.progress.append(h("b", { text: `Bar ${chart.k}` }), ` of ${chart.nReplay}`);
    }
    e.movingNotice.hidden = !moving;
    if (moving) {
      // Moving: the other tab is stepping it now. Moved on: it got further than this tab's copy and fell silent.
      const rec = this.remote.get(this.state.active), now = Date.now() - rec.at < MOVING_MS;
      const title = now ? "This chart is moving in another tab." : "This chart moved on in another tab.";
      const detail = now ? "Watch it here; trading and drawing come back when it stops."
        : "Take it over to go on from where it got to.", label = this.takingOver ? "Taking over…" : "Take over";
      if (e.movingTitle.textContent !== title) e.movingTitle.textContent = title;
      if (e.movingDetail.textContent !== detail) e.movingDetail.textContent = detail;
      if (e.takeOver.textContent !== label) e.takeOver.textContent = label;
      e.takeOver.disabled = !!this.takingOver;
    }
    const ended = chart && !chart.finished && !this.state?.expired && chart.k >= chart.nReplay && !moving;
    e.replayNotice.hidden = !ended;
    if (ended && !e.replayNotice.childElementCount) {
      e.replayNotice.append(h("i", { "aria-hidden": "true" }),
        h("p", null, h("b", { text: "End of the replay." }), " ",
          h("span", { text: "Finish the chart to reveal it." })),
        h("button", { type: "button", class: "d-btn d-btn-quiet", text: "Finish chart",
                      onclick: () => this.finish() }));
    }
    if (!ended) e.replayNotice.replaceChildren();
  }

  // The next chart after the current one (wrapping round) that isn't finished; null when all are.
  nextOpen() {
    if (!this.state || this.state.expired) return null;
    for (let step = 1; step < N_CHARTS; step++) {
      const i = (this.state.active + step) % N_CHARTS;
      if (!this.state.charts[i].finished) return i;
    }
    return null;
  }

  nextChart() {
    const i = this.nextOpen();
    if (i != null) this.openChart(i);
  }

  // No steps while a chart is being finished, here or in another tab: it finishes on the bar the visitor was looking
  // at.
  canStep(chart = this.chart) {
    return !!chart && !chart.finished && !this.finishing && !this.state?.expired && complete(chart)
      && chart.k < chart.nReplay && !this.isHeld(this.state.charts.indexOf(chart)) && !this.movingElsewhere(chart);
  }

  // Another tab is finishing chart i and asked this one to stand still on it.
  isHeld(i) {
    const held = this.held;
    if (!held || held.set !== this.state?.set || held.index !== i) return false;
    if (Date.now() < held.until) return true;
    this.held = null;
    return false;
  }

  // The layer over the chart: loading, or why a chart can't be shown.
  renderStage() {
    const o = this.el.overlay, chart = this.chart;
    let want = null;
    if (!chart) want = this.setFailed ? "failed" : "loading";
    else if (!complete(chart)) {
      // An expired set can't load at all, so it says so (and offers a new set) rather than offering a retry.
      want = this.state.expired ? "gone" : this.loadFailed === this.state.active ? "failed" : "loading";
    }
    // A failure the server asked to wait over a minute for offers no Try again.
    if (want === "failed" && this.waitUntil > Date.now()) want = "waiting";
    if (o.dataset.kind === want) return;
    o.dataset.kind = want || "";
    o.hidden = !want;
    o.replaceChildren();
    if (want === "loading") {
      o.append(h("div", { class: "tt-loading" }, h("i"), h("span", { text: "Loading the chart…" })));
    } else if (want === "failed" || want === "waiting") {
      const retry = () => (this.state ? this.openChart(this.state.active, { initial: true })
        : this.newSet({ confirm: false }));
      o.append(h("div", { class: "tt-loading" }, h("span", { text: "The chart couldn’t be loaded." }),
        want === "failed" ? h("button", { type: "button", class: "d-btn", text: "Try again", onclick: retry }) : null));
    } else if (want === "gone") {
      o.append(h("div", { class: "tt-loading" },
        h("span", { text: "TradeTest’s charts were updated since you started this set, so this chart can’t be "
                          + "loaded." }),
        h("button", { type: "button", class: "d-btn", text: "Start a new set",
                      onclick: () => this.newSet({ confirm: true }) })));
    }
  }

  renderStatus() {
    const el = this.el.status, chart = this.chart, sim = this.sim;
    el.replaceChildren();
    if (!chart) {
      el.append(h("div", { class: "tt-sec-head" }, h("h3", { text: "TradeTest" })),
        h("p", { class: "d-hint", text: "Getting a set of charts ready…" }));
      return;
    }
    // The toolbar and the tab say which bar the chart is on, and the Trades panel what's been placed: this card holds
    // the progress, the chart's result once there is one, and the reveal.
    const n = this.state.active + 1, sum = this.summary;
    const done = chart.finished ? chart.finishedAt : chart.k;
    el.append(h("div", { class: "tt-sec-head" }, h("h3", { text: `Chart ${n}` })),
      h("div", { class: "d-progress-bar tt-bar", role: "progressbar", "aria-label": "Replay progress",
                 "aria-valuemin": "0", "aria-valuemax": String(chart.nReplay), "aria-valuenow": String(done) },
        h("i", { style: `width:${(100 * done / chart.nReplay).toFixed(1)}%` })));
    const line = h("p", { class: "tt-pnl" });
    if (sum.unloaded && chart.trades.length) {
      // Without its rows the chart's trades can't be worked out: never shown as waiting to fill.
      line.append(this.rowsComing(chart) ? "Loading this chart’s bars…"
        : "This chart’s bars couldn’t be loaded, so its trades aren’t counted.");
    } else if (sum.closed || sum.open) {
      if (sum.closed) {
        line.append("Closed ", h("b", { class: toneOf(sum.totalR), text: sim.fmtR(sum.totalR) }),
          ` (${sum.wins} won, ${sum.losses} lost)`);
      }
      if (sum.closed && sum.open) line.append(" · ");
      if (sum.open) line.append("Open ", h("b", { class: toneOf(sum.openR), text: sim.fmtR(sum.openR) }));
      if (sum.pending) line.append(` · ${sum.pending} pending`);
    } else if (sum.pending) line.append(`${sum.pending} order${sum.pending > 1 ? "s" : ""} waiting to fill.`);
    else if (chart.finished && chart.trades.length) line.append("No closed trades.");
    if (line.childNodes.length) el.append(line);
    if (chart.finished && chart.reveal) {
      // The card is rebuilt on every render; it fades in only the first time it shows for this chart.
      const card = this.revealCard(chart, sum);
      if (this.revealShown !== chart) { this.revealShown = chart; card.classList.add("is-new"); }
      el.append(card);
    }
    else if (atEnd(chart) && !this.state.expired) el.append(h("p", { class: "d-hint", text: END_HINT }));
  }

  revealCard(chart, sum) {
    const r = chart.reveal, sim = this.sim, fmtDate = this.lib.fmtDate;
    const name = [r.name, r.symbol ? `(${r.symbol})` : ""].filter(Boolean).join(" ");
    const where = [r.kind === "etf" ? "ETF" : "Stock", [r.sector, r.industry].filter(Boolean).join(", "), r.exchange]
      .filter(Boolean).join(" · ");
    const hold = sim.buyAndHold(chart), after = afterFinish(chart), guess = guessLine(chart);
    const prices = realWords(chart, sim.fmtPrice);
    return h("div", { class: "tt-reveal" },
      h("p", { class: "tt-reveal-name" }, "This was ", h("b", { text: name || "an unnamed instrument" })),
      h("p", { text: where }),
      h("p", { text: `Replay: ${fmtDate(r.start)} to ${fmtDate(r.end)}.` }),
      prices ? h("p", { text: prices }) : null,
      guess ? h("p", { class: "tt-reveal-guess", text: guess }) : null,
      hold == null ? null
        : h("p", null, "Buy and hold over the replay: ", h("b", { class: toneOf(hold), text: sim.fmtPct(hold, 1) })),
      after == null ? null : h("p", null, "From your finish to the last bar: ",
        h("b", { class: toneOf(after), text: sim.fmtPct(after, 1) })),
      sum.closed ? h("p", null, "Your trades: ", h("b", { class: toneOf(sum.totalR), text: sim.fmtR(sum.totalR) }),
        ` over ${plural(sum.closed, "closed trade")}, ${sim.fmtR(sum.totalRNet)} after costs`) : null,
      r.note ? h("p", { class: "tt-reveal-note", text: r.note }) : null);
  }

  // The drawings list holds label inputs; while one has focus the list waits, so typing is never interrupted. A
  // checkbox or a swatch with focus doesn't hold it: an undo shows at once.
  deferred(el, name) {
    const typing = el.contains(document.activeElement) && document.activeElement.matches(TEXT_FIELD);
    if (typing) {
      (this.pendingLists ||= new Set()).add(name);
      return true;
    }
    return false;
  }

  flushDeferred() {
    if (!this.pendingLists?.size) return;
    const names = [...this.pendingLists];
    this.pendingLists.clear();
    for (const name of names) if (name === "drawings") this.renderDrawings();
  }

  // Trade rows are kept and updated in place as bars come in, so a note being typed or an open edit form keeps its
  // text and focus while the pill, the result and the actions stay current. New trades go on top.
  renderTrades() {
    const e = this.el, list = e.trades, chart = this.chart, end = atEnd(chart);
    fill(e.tradesTitle, "Trades", count(chart?.trades.length));
    // New order stays the same button, shown or hidden; after the last bar, or while another tab moves the chart, it
    // is off and says why.
    const moving = this.movingElsewhere(chart), why = moving ? MOVING_HINT : end ? END_HINT : null;
    e.newOrder.hidden = !chart || (this.locked(chart) && !moving);
    e.newOrder.disabled = !!why;
    if (why) e.newOrder.dataset.tip = why; else delete e.newOrder.dataset.tip;
    e.tradesManage.hidden = !chart?.trades.length || chart.finished || moving;
    if (!chart || !chart.trades.length) {
      this.rowsFor = null;
      if (!chart) { list.replaceChildren(); return; }
      const none = chart.finished || atEnd(chart);
      // The hint, and while trades can still be placed the way into the guide's step on placing one.
      listHint(list, e.tradesHint, none ? "No trades on this chart." : "No trades yet.",
        !none && !moving && e.tradesHelp);
      return;
    }
    const where = `${this.state.set}.${this.state.active}`;
    let carry = null;
    if (this.rowsFor !== chart) {
      // A newer copy of the same chart (another tab saved it): what was open or being typed in its rows comes along.
      carry = this.rowsWhere === where ? this.openRows(list) : null;
      this.rowsFor = chart; this.rowsWhere = where; this.rows = new Map(); list.replaceChildren();
    }
    const results = this.summary?.results || [], loaded = complete(chart);
    chart.trades.forEach((trade, n) => {
      let row = this.rows.get(trade);
      if (!row) { row = this.tradeRow(chart, trade); this.rows.set(trade, row); list.prepend(row.el); }
      // Without the chart's rows a trade's outcome can't be known: it says so rather than "Pending".
      if (!loaded) this.unloadedRow(row, chart, trade);
      else this.fillTradeRow(row, chart, trade, results[n] || this.sim.simulate(trade, chart));
    });
    if (carry?.length) this.reopenRows(carry, chart);
  }

  // The trade rows' open edit forms (their prices as typed) and the field with focus, by trade number.
  openRows(list) {
    const active = document.activeElement;
    return [...list.querySelectorAll(".tt-trade")].map((el) => {
      const form = el.querySelector(".tt-edit"), fields = form ? [...form.querySelectorAll("input")] : [];
      const focus = !el.contains(active) ? null : active.classList.contains("tt-note") ? "note"
        : fields.indexOf(active);
      return { id: Number(el.dataset.id), edit: form ? fields.map((x) => x.value) : null, focus,
               range: focus == null ? null : [active.selectionStart, active.selectionEnd] };
    }).filter((x) => x.edit || (x.focus != null && x.focus !== -1));
  }

  // Those forms open again on the new rows, with what was typed, and focus goes back where it was.
  reopenRows(carry, chart) {
    const moving = this.movingElsewhere(chart);
    for (const { id, edit, focus, range } of carry) {
      const trade = chart.trades.find((t) => t.id === id), row = trade && this.rows.get(trade);
      if (!row) continue;
      let target = focus === "note" ? row.note : null;
      if (edit) {
        const form = this.editLevels(row.el, trade, { focus: false });
        if (form) {
          const fields = [...form.querySelectorAll("input")];
          fields.forEach((x, i) => { if (edit[i] != null) x.value = edit[i]; });
          this.holdForm(form, moving ? MOVING_EDIT : complete(chart) ? null : UNLOADED_EDIT);
          if (Number.isInteger(focus)) target = fields[focus] || null;
        }
      }
      if (target) {
        target.focus({ preventScroll: true });
        try { if (range) target.setSelectionRange(range[0], range[1]); } catch { /* not a text field */ }
      }
    }
  }

  unloadedRow(row, chart, trade) {
    const sim = this.sim, levels = sim.levelsAt(trade, chart.nCtx + chart.k, chart.nCtx);
    const kind = trade.entry == null ? "market"
      : `${trade.kind === "limit" ? "limit" : "stop entry"} ${sim.fmtPrice(trade.entry)}`;
    row.what.textContent = `${kind} · stop ${sim.fmtPrice(levels.stop)} · target ${sim.fmtPrice(levels.target)}`;
    row.when.textContent = `Placed at bar ${trade.placedAt} · the chart’s bars aren’t loaded`;
    fill(row.head, h("span", { class: "d-pill tt-pill is-unloaded", text: "Not loaded" }),
      h("b", { class: "tt-trade-name" },
        h("span", { class: trade.side === "short" ? "d-down" : "d-up", text: sideWord(trade.side) }), ` #${trade.id}`));
    row.actionsKey = ""; row.actions.replaceChildren(); row.actions.hidden = true;
    // An edit form open on it waits, as typed, until the rows are in (another tab's save may have just replaced them).
    const form = row.el.querySelector(".tt-edit");
    if (form) this.holdForm(form, this.movingElsewhere(chart) ? MOVING_EDIT : UNLOADED_EDIT);
  }

  tradeRow(chart, trade) {
    const row = {};
    const pick = (e) => { if (!e.target.closest("button, input, form")) this.select({ kind: "trade", id: trade.id }); };
    row.head = h("div", { class: "tt-trade-head" });
    row.what = h("p", { class: "tt-trade-what" });
    row.when = h("p", { class: "tt-trade-when" });
    row.actions = h("div", { class: "tt-row-actions", hidden: true });
    row.note = h("input", { class: "tt-input tt-note", type: "text", maxlength: "200", value: trade.note || "",
                            placeholder: "Note: why this trade, why you got out",
                            "aria-label": `Note for trade ${trade.id}` });
    row.note.addEventListener("input", () => {
      trade.note = row.note.value; trade.noteStamp = Date.now(); changed(chart); this.save(true);
    });
    row.el = h("article", { class: "tt-trade", "data-id": String(trade.id), onclick: pick },
      row.head, row.what, row.when, row.actions, row.note);
    return row;
  }

  fillTradeRow(row, chart, trade, res) {
    const sim = this.sim, bar = (i) => this.lib.barNumber(chart, i).toLowerCase();
    row.el.classList.toggle("is-selected", this.selection?.kind === "trade" && this.selection.id === trade.id);
    const kind = res.kind === "market" ? "market"
      : `${res.kind === "limit" ? "limit" : "stop entry"} ${sim.fmtPrice(trade.entry)}`;
    // A level moved since placement shows where it started.
    const level = (name, now, was) => `${name} ${sim.fmtPrice(now)}`
      + (Math.abs(now - was) > 1e-9 ? ` (was ${sim.fmtPrice(was)})` : "");
    row.what.textContent = [kind, level("stop", res.stopNow, trade.stop), level("target", res.targetNow, trade.target)]
      .join(" · ");
    const lines = [`Placed at bar ${trade.placedAt}`];
    if (res.fill) lines.push(`filled ${bar(res.fill.i)} at ${sim.fmtPrice(res.fill.price)}`);
    if (res.exit) {
      const why = EXIT_WORDS[res.exit.reason] || res.exit.reason;
      lines.push(`exit ${bar(res.exit.i)} at ${sim.fmtPrice(res.exit.price)} (${why})`);
    }
    if (res.missed) {
      const past = res.missed.reason === "gap_stop" ? "stop" : "target";
      lines.push(`missed: ${bar(res.missed.i)} opened past the ${past}`);
    }
    // The bar the visitor cancelled on, as the report says it.
    if (res.cancelled) lines.push(`cancelled at bar ${trade.cancelAt}`);
    if (res.status === "expired") lines.push("not filled before the chart was finished");
    if (res.status === "closing") lines.push("closes at the next open");
    row.when.textContent = lines.join(" · ");
    fill(row.head,
      h("span", { class: `d-pill tt-pill is-${res.status}`, text: STATUS_WORDS[res.status] || res.status }),
      h("b", { class: "tt-trade-name" },
        h("span", { class: trade.side === "short" ? "d-down" : "d-up", text: sideWord(trade.side) }), ` #${trade.id}`),
      res.r == null ? null : h("span", { class: "tt-trade-r" }, h("b", { class: toneOf(res.r), text: sim.fmtR(res.r) }),
        h("small", { text: ` ${sim.fmtPct(res.pct)}` })));
    // The actions are rebuilt only when they change, so a button is never swapped out from under a click. After the
    // last bar there is no next open: Close is off and says why. While another tab moves the chart they are all off,
    // and an edit form open stays, its prices kept, until the chart is this tab's again.
    const moving = this.movingElsewhere(chart), live = !this.locked(chart) || moving;
    const editable = live && (res.status === "open" || res.status === "pending");
    const acts = [live && res.status === "open" && (atEnd(chart) ? "noClose" : "close"),
                  live && res.status === "pending" && "cancel", editable && "edit"].filter(Boolean);
    const key = acts.join() + (moving ? ":moving" : "");
    if (row.actionsKey !== key) {
      row.actionsKey = key;
      const action = (text, onclick, extra) => h("button", { type: "button", class: "d-btn d-btn-quiet", text,
        ...(moving ? { disabled: true, "data-tip": MOVING_HINT } : { onclick, ...extra }) });
      const make = {
        close: () => action("Close at next open", () => this.closeTrade(trade)),
        noClose: () => action("Close at next open", null, { disabled: true, "data-tip": END_HINT }),
        cancel: () => action("Cancel order", () => this.cancelTrade(trade)),
        edit: () => action("Edit stop/target", () => this.editLevels(row.el, trade)),
      };
      row.actions.replaceChildren(...acts.map((name) => make[name]()));
    }
    row.actions.hidden = !acts.length;
    // An edit form whose trade can't be changed any more (it closed, or the chart was finished) goes away.
    const form = row.el.querySelector(".tt-edit");
    if (form && !editable) form.remove();
    else if (form) this.holdForm(form, moving ? MOVING_EDIT : null);
    row.note.readOnly = moving;
    if (document.activeElement !== row.note && row.note.value !== (trade.note || "")) row.note.value = trade.note || "";
  }

  // An open edit form that can't be saved for now (why: another tab moves the chart, or its rows aren't in): its prices
  // kept but not editable, Save off, and why. why null: as usual again.
  holdForm(form, why) {
    for (const input of form.querySelectorAll("input")) input.readOnly = !!why;
    const save = form.querySelector('button[type="submit"]'), msg = form.querySelector(".tt-msg");
    if (save) save.disabled = !!why;
    if (!msg) return;
    if (why) { msg.textContent = why; msg.classList.add("is-note"); }
    else if (msg.classList.contains("is-note")) { msg.textContent = ""; msg.classList.remove("is-note"); }
  }

  // The row's form for a new stop and target; focus goes to its stop unless told otherwise. The form, or null.
  editLevels(row, trade, { focus = true } = {}) {
    const chart = this.chart;
    if (row.querySelector(".tt-edit") || !chart || (this.locked(chart) && !this.movingElsewhere(chart))) return null;
    const res = this.sim.simulate(trade, chart);
    const input = (label, price) => h("input", { class: "tt-input", type: "text", inputmode: "decimal",
                                                 value: priceText(price), "aria-label": label });
    const stop = input("Stop", res.stopNow), target = input("Target", res.targetNow);
    const msg = h("p", { class: "tt-msg", role: "alert" });
    const form = h("form", { class: "tt-edit" },
      h("label", { class: "tt-field" }, h("span", { text: "Stop" }), stop),
      h("label", { class: "tt-field" }, h("span", { text: "Target" }), target),
      h("div", { class: "tt-row-actions" },
        h("button", { type: "button", class: "d-btn d-btn-quiet", text: "Cancel", onclick: () => this.endEdit(form) }),
        h("button", { type: "submit", class: "d-btn", text: "Save" })), msg);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const message = this.changeLevels(trade, { stop: readNumber(stop.value), target: readNumber(target.value) });
      if (message) { msg.textContent = message; msg.classList.toggle("is-note", message === MOVING_EDIT); return; }
      this.endEdit(form);
    });
    row.querySelector(".tt-note").before(form);
    if (focus) { stop.focus(); stop.select(); }
    return form;
  }

  endEdit(form) {
    form.remove();
    this.renderTrades();
  }

  // Drawing rows are kept by drawing, newest first, and rebuilt only when that drawing changes (or the chart locks), so
  // a swatch, a checkbox or Delete stays put under a click while Play renders every bar; the label and the selection
  // are updated in place.
  renderDrawings() {
    const list = this.el.drawings, chart = this.chart;
    fill(this.el.drawingsTitle, "Drawings", count(chart?.drawings.length));
    // A label being typed when another tab starts moving the chart keeps its text but takes no more.
    const moving = this.movingElsewhere(chart);
    for (const input of list.querySelectorAll(".tt-label")) if (!input.disabled) input.readOnly = moving;
    if (this.deferred(list, "drawings")) return;
    if (!chart || !chart.drawings.length) {
      this.drawRows = null;
      if (!chart) { list.replaceChildren(); return; }
      listHint(list, this.el.drawingsHint, chart.finished ? "No drawings on this chart." : "No drawings yet.",
        !chart.finished && !moving && this.el.drawingsHelp);
      return;
    }
    if (!this.drawRows || this.drawRowsFor !== chart) {
      this.drawRowsFor = chart; this.drawRows = new Map();
      list.replaceChildren();
    }
    const locked = this.locked(chart), rows = this.drawRows, ids = new Set(chart.drawings.map((d) => d.id));
    for (const [id, row] of rows) if (!ids.has(id)) { row.el.remove(); rows.delete(id); }
    [...chart.drawings].reverse().forEach((d, n) => {
      const { label, stamp, ...rest } = d, key = JSON.stringify([rest, locked]);
      let row = rows.get(d.id);
      if (!row || row.drawing !== d || row.key !== key) {
        const el = this.drawingRow(chart, d, locked);
        if (row) row.el.replaceWith(el);
        row = { el, key, drawing: d, label: el.querySelector(".tt-label") };
        rows.set(d.id, row);
      }
      row.el.classList.toggle("is-selected", this.selection?.kind === "drawing" && this.selection.id === d.id);
      if (document.activeElement !== row.label && row.label.value !== (label || "")) row.label.value = label || "";
      if (list.children[n] !== row.el) list.insertBefore(row.el, list.children[n] || null);
    });
  }

  drawingRow(chart, d, locked) {
    const sim = this.sim, selected = this.selection?.kind === "drawing" && this.selection.id === d.id;
    const [a, b, c] = d.points, bar = (x) => this.lib.barNumber(chart, x).toLowerCase();
    const price = sim.fmtPrice, type = TYPE_NAMES[d.type], lower = type.toLowerCase();
    let where = `${price(a.p)} to ${price(b?.p)}, ${bar(a.x)} to ${bar(b?.x)}`;
    if (d.type === "hline") where = `at ${price(a.p)}`;
    else if (d.type === "vline") where = `at ${bar(a.x)}`;
    else if (d.type === "zone") {
      where = `${price(Math.min(a.p, b.p))} to ${price(Math.max(a.p, b.p))} from ${bar(Math.min(a.x, b.x))}`;
    } else if (d.type === "channel") {
      const base = a.x === b.x ? a.p : a.p + (b.p - a.p) * (c.x - a.x) / (b.x - a.x);
      where += `, width ${price(Math.abs(c.p - base))}`;
    }
    const label = h("input", { class: "tt-input tt-label", type: "text", maxlength: "60", placeholder: `${type} label`,
                               "aria-label": `Label for ${lower}`, value: d.label || "", disabled: locked,
                               "data-id": String(d.id) });
    // A label edit is one undo step, from focus to the change.
    let before = null;
    label.addEventListener("focus", () => {
      before = clone(d);
      this.select({ kind: "drawing", id: d.id }, { quiet: true });
    });
    label.addEventListener("input", () => {
      d.label = label.value; d.stamp = Date.now(); changed(chart); this.save(true); this.renderChartOnly();
    });
    // A key in a label held off screen (see focusLabel): the scroll the browser makes for it is undone.
    label.addEventListener("keydown", () => {
      if (this.labelHold?.input === label) this.labelHold.typed = performance.now();
    });
    label.addEventListener("blur", () => { if (this.labelHold?.input === label) this.labelHold = null; });
    label.addEventListener("change", () => {
      if (before && before.label !== d.label) this.record({ op: "update", id: d.id, before, after: clone(d) });
      before = clone(d);
    });
    const swatches = h("div", { class: "tt-swatches", role: "group", "aria-label": "Colour" },
      Object.entries(this.lib.PALETTE).map(([name, hex]) => h("button", {
        type: "button", class: "tt-swatch" + (d.color === name ? " is-on" : ""), style: `--c:${hex}`,
        "aria-label": COLOUR_NAMES[name] || name, title: COLOUR_NAMES[name] || name, "data-color": name,
        "aria-pressed": d.color === name ? "true" : "false", disabled: locked,
        onclick: () => this.patchDrawing(d, { color: name }),
      })));
    const extend = this.lib.EXTENDABLE.has(d.type) ? h("label", { class: "tt-check" },
      h("input", { type: "checkbox", checked: !!d.extend, disabled: locked,
                   onchange: (e) => this.patchDrawing(d, { extend: e.target.checked }) }),
      h("span", { text: "Extend right" })) : null;
    const remove = locked ? null : h("button", { type: "button", class: "tt-tool tt-del",
                                                 "aria-label": `Delete ${lower}`, html: icon("trash"),
                                                 onclick: () => this.deleteDrawing(d.id) });
    const pick = (e) => { if (!e.target.closest("button, input, label")) this.select({ kind: "drawing", id: d.id }); };
    return h("div", { class: "tt-drawing" + (selected ? " is-selected" : ""), "data-id": String(d.id), onclick: pick },
      h("div", { class: "tt-drawing-head" },
        h("span", { class: "tt-dicon", html: icon(d.type), title: type }), label, remove),
      h("div", { class: "tt-drawing-foot" }, swatches, extend),
      h("p", { class: "tt-drawing-where", text: `${type} ${where}` }));
  }

  renderChartOnly() {
    const chart = this.chart;
    if (!chart) return;
    this.view.update({ chart, results: this.summary?.results || [], settings: this.settings, locked: this.locked(chart),
                       selection: this.selection, draft: this.draft });
  }

  // ------------------------------------------------ notices and toasts

  notice(key, { tone = "info", title, detail, actions = [] }) {
    const buttons = actions.map(([text, fn]) => h("button", { type: "button", class: "d-btn d-btn-quiet", text,
                                                              onclick: fn }));
    const role = tone === "error" || tone === "caution" ? "alert" : "status";
    const box = h("div", { class: `d-notice tone-${tone}`, role },
      h("i", { "aria-hidden": "true" }),
      h("p", null, h("b", { text: title }), detail ? " " : null, detail ? h("span", { text: detail }) : null),
      buttons.length ? h("div", { class: "tt-notice-actions" }, buttons) : null);
    const old = this.notices.get(key);
    if (old) old.replaceWith(box); else this.el.notices.append(box);
    this.notices.set(key, box);
    this.render();       // the chart's height makes room for it
  }

  clearNotice(key) {
    if (!this.notices.has(key)) return;
    this.notices.get(key).remove();
    this.notices.delete(key);
    this.render();
  }

  toast(parts, { tone } = {}) {
    const box = h("div", { class: "tt-toast" + (tone ? ` tone-${tone}` : "") }, parts);
    this.el.toasts.append(box);
    while (this.el.toasts.childElementCount > (innerWidth <= 720 ? 2 : 3)) this.el.toasts.firstElementChild.remove();
    setTimeout(() => { box.classList.add("is-out"); setTimeout(() => box.remove(), 200); }, 4200);
  }

  clearToasts() {
    this.el.toasts.replaceChildren();
  }

  // A failed request: say what happened, and offer to try again where that can help. The notice remembers which
  // request it is about, and only that request succeeding clears it.
  failedNotice(err, retry) {
    const code = err?.code || "server";
    const again = retry ? [["Try again", () => { this.clearNotice("api"); retry(); }]] : [];
    this.noticeFor = err?.endpoint || "";
    if (code === "expired" || code === "bad_token") {
      if (this.state) { this.state.expired = true; this.save(); }
      this.pause();
      this.loadFailed = null;
      // There is a report to download only once a chart is finished.
      const report = !!this.state?.charts.some((c) => c.finished);
      this.notice("expired", { tone: "caution", title: "TradeTest’s charts were updated since you started this set.",
                               detail: report ? "Download your report, then start a new set." : "Start a new set.",
                               actions: [["New set", () => this.newSet({ confirm: true })]] });
    } else if (code === "rate_limited") {
      // The server says which budget ran out and for how long; these are its words when it sends them.
      const fallback = err.endpoint === "new"
        ? `You’ve started many sets from this connection in the last hour. Try again in ${waitWords(err.retryAfter)}.`
        : "Too many requests at once. Wait a moment, then try again.";
      const [title, detail] = sentences(err.message || fallback);
      // A wait of more than a minute can't be cut short: no Try again (here or over the chart) to fail meanwhile.
      this.waitUntil = err.retryAfter > 60 ? Date.now() + 1000 * err.retryAfter : 0;
      this.notice("api", { tone: "caution", title, detail, actions: this.waitUntil ? [] : again });
    } else if (code === "network") {
      this.notice("api", { tone: "error", title: "TradeTest couldn’t reach the server.",
                           detail: "Your work is saved in this browser.", actions: again });
    } else if (code === "unavailable") {
      this.notice("api", { tone: "error", title: "TradeTest is not available right now.",
                           detail: err.message || "Try again in a moment.", actions: again });
    } else {
      this.notice("api", { tone: "error", title: "Something went wrong.",
                           detail: err?.message || "Try again in a moment.", actions: again });
    }
    this.render();
  }

  // A request. A quiet one (the background prefetch) never touches the notices; any other clears the request notice
  // once the same kind of request goes through again.
  async call(name, body, { quiet = false } = {}) {
    let data;
    try {
      data = await api(name, body);
    } catch (err) {
      if (err.status) this.contacted = true;
      throw err;
    }
    this.contacted = true; this.waitUntil = 0;
    if (!quiet && this.notices.has("api") && (!this.noticeFor || this.noticeFor === name)) this.clearNotice("api");
    return data;
  }

  // ------------------------------------------------ sets and charts

  hasProgress() {
    return !!this.state?.charts.some((c) => c.k > 0 || c.trades.length || c.drawings.length || c.finished);
  }

  async newSet({ confirm }) {
    if (this.busyNew) return;
    if (confirm && this.hasProgress()) {
      const ok = await this.ask({ title: "Start a new set?", confirm: "Start a new set", cancel: "Keep this set",
                                  body: "Your ten charts, drawings and trades will be cleared from this browser. "
                                    + "Download your report first if you want to keep it." });
      if (!ok) return;
    }
    this.pause();
    this.busyNew = true; this.setFailed = false; this.render();
    let resp;
    try {
      resp = await this.call("new", { avoid: this.state?.avoid ?? null });
    } catch (err) {
      this.busyNew = false;
      if (!this.state) this.setFailed = true;
      this.failedNotice(err, () => this.newSet({ confirm: false }));
      return;
    }
    this.busyNew = false;
    if (!Array.isArray(resp.charts) || resp.charts.length < N_CHARTS) {
      if (!this.state) this.setFailed = true;
      this.failedNotice(new ApiError("server", "The server sent an incomplete set."),
                        () => this.newSet({ confirm: false }));
      return;
    }
    const old = this.state?.set;
    this.state = freshState(resp, this.settings);
    if (old != null && old !== this.state.set) this.forgetBars(old);
    this.clearNotice("expired"); this.clearNotice("done"); this.clearNotice("finished");
    this.undo.clear(); this.selection = null; this.closeTicket(false); this.queued = 0;
    this.loadFailed = null; this.remote.clear();
    this.save();
    this.openChart(0, { initial: true });
  }

  async openChart(i, { initial = false, save = true } = {}) {
    if (!this.state) return;
    if (!initial && i === this.state.active) return;
    this.pause(); this.queued = 0;
    this.closeTicket(false);
    this.selection = null;
    if (this.tool !== "cursor") this.setTool("cursor");
    // What was said about the last chart (its trades are numbered from #1 too) goes with it.
    if (i !== this.state.active) { this.clearNotice("finished"); this.clearToasts(); }
    this.labelArm = null;
    this.state.active = i;
    // Leaving a chart: its rows go to localStorage too (see saveNow).
    if (save) this.saveNow({ rows: true });
    this.lockCheck();
    await this.ensureBars(i, { quiet: false });
    this.prefetch();
  }

  // The chart's rows, from the server when this browser doesn't have them; a finished chart also gets the rest.
  ensureBars(i, { quiet }) {
    const chart = this.state.charts[i];
    if (complete(chart) || this.state.expired) { this.render(); return Promise.resolve(); }
    if (this.loads.has(chart)) return this.loads.get(chart);
    const set = this.state.set;
    // Another tab's save may have replaced this chart while the request was out; its copy then stands.
    const current = () => this.state?.set === set && this.state.charts[i] === chart;
    const job = (async () => {
      try {
        const resp = await this.call("bars", { cursor: chart.cursor }, { quiet });
        if (!current()) return;
        chart.bars = resp.bars; chart.k = resp.k;
        chart.nCtx = resp.n_context ?? chart.nCtx; chart.nReplay = resp.n_replay ?? chart.nReplay;
        if (typeof resp.cursor === "string") chart.cursor = resp.cursor;
        if (chart.finished) {
          const rest = await this.call("reveal", { cursor: chart.cursor }, { quiet });
          if (!current()) return;
          chart.bars = chart.bars.slice(0, chart.nCtx + rest.k).concat(rest.rest || []);
          chart.reveal ||= this.revealOf(rest);
        }
        if (this.loadFailed === i) this.loadFailed = null;
        this.failedRows.delete(chart);
        this.save();
      } catch (err) {
        this.failedRows.add(chart);
        // A set the server no longer has is said at once, even by a background load: none of it can load now.
        if (err?.code === "expired" || err?.code === "bad_token") this.failedNotice(err);
        else if (!quiet) { this.loadFailed = i; this.failedNotice(err, () => this.openChart(i, { initial: true })); }
      } finally {
        this.loads.delete(chart);
        this.render();
      }
    })();
    this.loads.set(chart, job);
    return job;
  }

  // Load the other charts quietly, one at a time, so switching tabs is instant: first the unfinished charts with
  // trades (the score line counts their closed trades), and none that already failed.
  prefetch() {
    clearTimeout(this.prefetchTimer);
    if (!this.state || this.state.expired) return;
    const charts = this.state.charts, want = (c) => !complete(c) && !this.failedRows.has(c);
    let next = charts.findIndex((c) => want(c) && !c.finished && c.trades.length);
    if (next < 0) next = charts.findIndex(want);
    if (next < 0) return;
    this.prefetchTimer = setTimeout(async () => {
      if (this.dead || !this.state) return;
      await this.ensureBars(next, { quiet: true });
      this.prefetch();
    }, 500);
  }

  // Rows for the charts the report and Copy result count (unfinished charts with trades, finished ones), from the
  // server where this browser lacks them; whatever doesn't come within wait ms is left out, and they say so.
  async rowsForScoring(wait) {
    if (!this.state || this.state.expired) return;
    const jobs = this.state.charts.map((c, i) => (!complete(c) && (c.finished || c.trades.length)
      ? this.ensureBars(i, { quiet: true }) : null)).filter(Boolean);
    if (jobs.length) await Promise.race([Promise.all(jobs), new Promise((r) => setTimeout(r, wait))]);
  }

  revealOf(r) {
    return { symbol: r.symbol, name: r.name, kind: r.kind, sector: r.sector, industry: r.industry, exchange: r.exchange,
             note: r.note, dates: r.dates, scale: r.scale, start: r.start, end: r.end };
  }

  // ------------------------------------------------ the replay

  step(n) {
    const chart = this.chart;
    if (!this.canStep(chart)) {
      if (this.movingElsewhere(chart)) this.view.showHint(MOVING_HINT);
      return;
    }
    const room = chart.nReplay - chart.k - this.queued;
    if (room <= 0) return;
    this.queued += Math.min(n, room);
    this.pump();
  }

  // Steps go to the server one request at a time; more presses wait in the queue, so no bar is asked for twice. The
  // other tabs hear before each request, every so often while it is out, and as each lands (see beat).
  async pump() {
    if (this.stepping) return;
    const index = this.state.active, chart = this.chart, set = this.state.set;
    // The chart still stands: no new set, and no other tab's save has put its own copy in its place.
    const current = () => this.state?.set === set && this.state.charts[index] === chart;
    this.stepping = true;
    try {
      while (this.queued > 0 && current() && this.chart === chart && !chart.finished && !this.finishing
             && !this.state.expired && !this.isHeld(index) && !this.movingElsewhere(chart)) {
        const n = Math.min(this.queued, MAX_STEP);
        this.queued -= n;
        const before = this.sim.chartSummary(chart).results, sentAt = performance.now();
        this.beat(index);
        const alive = setInterval(() => this.beat(index, true), MOVING_MS / 4);
        const resp = await this.call("step", { cursor: chart.cursor, n }).finally(() => clearInterval(alive));
        // Bars that land after Finish was confirmed are dropped: the chart finishes where the visitor was.
        if (!current() || chart.finished || this.finishing) break;
        if (!Array.isArray(resp.bars) || resp.k !== chart.k + resp.bars.length) {
          // Out of step with the server (another tab?): take its rows as they are.
          this.queued = 0;
          chart.bars = chart.bars.slice(0, chart.nCtx);
          chart.k = 0;
          await this.ensureBars(index, { quiet: false });
          break;
        }
        chart.bars.length = chart.nCtx + chart.k;
        chart.bars.push(...resp.bars);
        chart.k = resp.k; chart.cursor = resp.cursor;
        this.beat(index, true, resp.bars);
        this.afterStep(chart, before, sentAt);
      }
    } catch (err) {
      this.queued = 0;
      this.pause();
      if (err?.code === "finished") this.autoFinish(index, chart);
      else this.failedNotice(err, () => this.step(1));
    } finally {
      this.stepping = false;
      if (this.chart !== chart) this.queued = 0;
    }
  }

  afterStep(chart, before, sentAt) {
    const after = this.sim.chartSummary(chart).results, events = [];
    chart.trades.forEach((trade, n) => {
      // A trade placed while the step was on its way has no "before": it was an order waiting to fill.
      const a = before[n] || { fill: null, status: "pending" }, b = after[n];
      if (!b) return;
      const name = `${sideWord(trade.side)} #${trade.id}`;
      if (!a.fill && b.fill) {
        const parts = [`${name} filled at ${this.sim.fmtPrice(b.fill.price)}`];
        if (b.status === "closed") parts.push(...this.exitWords(b, " and "));
        events.push(parts);
      } else if (a.status !== "closed" && b.status === "closed") events.push([name, ...this.exitWords(b, " ")]);
      else if (a.status === "pending" && b.status === "missed") {
        const past = b.missed.reason === "gap_stop" ? "stop" : "target";
        events.push([`${name} missed: the bar opened past its ${past}`]);
      }
    });
    const shown = this.chart === chart, ended = chart.k >= chart.nReplay;
    if (shown) for (const parts of events) this.toast(parts);
    if (this.playing && (ended || (events.length && this.settings.autoPause))) this.pause();
    if (shown) {
      // A drawing or order half made when this step was asked for was made against the bars before it: it goes. The
      // last bar closes the ticket and puts a trade tool down too: nothing more can fill (the card's end notice says
      // what to do).
      this.view.dropHalfMade(sentAt);
      if (ended && this.ticket) this.closeTicket(false);
      if (ended && (this.tool === "long" || this.tool === "short")) this.setTool("cursor");
    }
    this.save();
    this.render();
  }

  exitWords(res, joint) {
    const r = h("b", { class: toneOf(res.r), text: this.sim.fmtR(res.r) });
    const words = { target: "hit its target", stop: "was stopped out", close: "closed at the open" };
    const what = words[res.exit.reason] || "closed";
    return [`${joint.trim() ? joint : " "}${what}: `, r];
  }

  togglePlay() { if (this.playing) this.pause(); else this.play(); }

  play() {
    if (!this.canStep()) {
      if (this.movingElsewhere()) this.view.showHint(MOVING_HINT);
      return;
    }
    this.playing = true;
    clearInterval(this.timer);
    this.timer = setInterval(() => this.playTick(), 1000 / this.settings.speed);
    this.playTick();
    this.render();
  }

  pause() {
    if (!this.playing) return;
    this.playing = false;
    clearInterval(this.timer);
    this.saveNow({ rows: true });            // what Play left for the once-a-second save, and the chart's rows
    this.render();
  }

  playTick() {
    if (!this.canStep()) { this.pause(); return; }
    this.beat();
    if (this.stepping || this.queued) return;
    this.step(1);
  }

  // This tab is moving chart index (Play, or a step asked for, out or just in): the other tabs hear it, with its bar,
  // at most every BEAT_MS unless forced, and keep their hands off that chart while it lasts (see heardMoving). A step
  // that just landed sends the rows it brought, so they can show this tab's next save without asking the server.
  beat(index = this.state?.active, force = false, rows = null) {
    if (!this.state || !this.channel || !Number.isInteger(index)) return;
    const now = Date.now();
    if (!force && now - this.beatAt < BEAT_MS) return;
    this.beatAt = now;
    this.post({ type: "moving", set: this.state.set, index, k: this.state.charts[index].k, rows });
  }

  // Another tab is moving chart index of this set (see beat): the chart is read-only here while it lasts (see
  // movingElsewhere). Two tabs that started moving it at the same moment settle it at once: the one behind, or at the
  // same bar the one with the larger id, stops.
  heardMoving({ from, index, k, rows }) {
    const mine = this.state.charts[index], theirs = Number.isInteger(k) ? k : 0;
    if (this.movingHere(index) && !(theirs > mine.k || (theirs === mine.k && String(from) < this.tabId))) return;
    if (this.movingHere(index)) { this.pause(); this.queued = 0; }
    this.remote.set(index, { set: this.state.set, at: Date.now(), k: theirs, from });
    if (Array.isArray(rows) && rows.length) this.feedRows(index, theirs, rows);
    if (index === this.state.active) this.lockCheck();
  }

  // The rows another tab's steps brought in for chart index, joined on to the ones this tab has: up to bar k, the
  // other tab's bar. Its saves carry no rows during Play, so they come from here rather than from the server.
  feedRows(index, k, rows) {
    const set = this.state.set, chart = this.state.charts[index], from = chart.nCtx + k - rows.length;
    let have = this.feed?.set === set && this.feed.index === index ? this.feed.rows : null;
    if (!have || have.length < from) have = chart.bars.length >= from ? chart.bars : null;
    this.feed = have && from >= chart.nCtx ? { set, index, rows: have.slice(0, from).concat(rows) } : null;
  }

  // Those rows, cut to what chart (another tab's copy of chart index) needs; null when they fall short.
  fedRows(index, chart) {
    const feed = this.feed;
    return feed && feed.set === this.state?.set && feed.index === index ? rowsFor(feed.rows, chart) : null;
  }

  // Take over: the tabs moving this chart stop and hand over their copies (see heard), and the newest copy in this
  // browser, theirs, the journal's or this tab's own, becomes this tab's (see freshest), with this tab's drawings and
  // notes in it. Then the chart is this tab's to trade, draw on and step. When no copy reaches the bar another tab said
  // it had shown (that tab is gone without saving it), the chart stays read-only: trading on from an earlier bar would
  // trade bars already seen.
  async takeOver() {
    const set = this.state?.set, index = this.state?.active;
    if (set == null || this.takingOver || this.finishing) return;
    this.takingOver = true;
    this.render();
    try {
      await this.freshest(set, index, "takeover");
    } finally {
      this.takingOver = false;
      const chart = this.state?.set === set ? this.state.charts[index] : null, rec = this.remote.get(index);
      if (chart && rec && rec.set === set && !chart.finished && rec.k > chart.k) {
        rec.at = 0;
        this.toast([`Chart ${index + 1} can’t be taken over yet: the tab that moved it on to bar ${rec.k} isn’t `
          + "answering, and this browser has no copy that far along."]);
      } else if (chart) this.remote.delete(index);
      this.lockCheck();
    }
  }

  async finish() {
    const chart = this.chart, index = this.state?.active, set = this.state?.set;
    if (!chart || chart.finished || this.finishing || this.state.expired || !complete(chart)) return;
    // Presses still queued are dropped now, so bars don't keep coming in behind the dialog.
    this.pause(); this.queued = 0;
    const n = index + 1;
    const answer = await this.askFinish(n, chart);
    if (!answer) return;
    // While the dialog was open another tab may have saved its own copy of this chart, further along (it was
    // playing): that copy is the one finished, at its bar. A chart finished there, or a new set, ends it here.
    const target = () => this.finishable(set, index, chart);
    const moved = () => this.toast([`Chart ${n} changed in another tab, so it wasn’t finished. Finish it again.`]);
    if (!target()) { if (this.state?.set === set && !this.state.charts[index].finished) moved(); return; }
    this.finishing = true; this.queued = 0;
    this.render();
    let done = null;
    try {
      while (this.stepping) await new Promise((r) => setTimeout(r, 50));
      // Another tab may be further along this chart (playing it): it stops and hands its copy over, and the chart is
      // finished where that tab stands, with the trades of every copy.
      await this.freshest(set, index);
      const copy = target();
      if (!copy) return;
      let resp;
      try {
        resp = await this.call("reveal", { cursor: copy.cursor });
      } catch (err) {
        this.failedNotice(err, () => this.finish());
        return;
      }
      done = this.applyReveal(set, index, copy, resp, answer.guess);
      if (!done) { if (this.state?.set === set && !this.state.charts[index].finished) moved(); return; }
      const sum = this.sim.chartSummary(done);
      // The first finish below 1100px, where the page scrolls to the chart: the score and the report it just
      // brought are up out of sight.
      const first = innerWidth <= 1100 && this.state.charts.filter((c) => c.finished).length === 1;
      this.toast([`Chart ${n} was ${done.reveal.symbol || "revealed"}. `,
        ...(sum.closed ? [h("b", { class: toneOf(sum.totalR), text: this.sim.fmtR(sum.totalR) }), " on this chart."]
          : [done.trades.length ? "No closed trades." : "No trades."]),
        first ? " Your set’s score and report are at the top of the page." : null]);
      this.revealed(index);
    } finally {
      this.finishing = false;
      // The tabs asked to stand still go on, when this finish didn't happen.
      if (!done) this.post({ type: "released", set, index });
      this.render();
    }
  }

  // The most advanced copy of chart index in this browser becomes this tab's: its own, one another tab hands over
  // (see askTabs; type says why: "finishing" or "takeover") or the journal's, whichever is furthest along with its
  // rows. Trades only a copy at that same bar holds are added to it, so a trade placed in either tab is never dropped
  // (one placed at the finish bar shows as expired). A copy behind it was traded on bars the newest copy had already
  // shown: what only it holds doesn't go in, and when that is this tab's, the visitor is told (see unseen). Drawings
  // and notes come from every copy (see mergeDrawings).
  async freshest(set, index, type = "finishing") {
    const wait = type === "takeover" ? 1500 : 400;
    const [copies, journal] = await Promise.all([this.askTabs(set, index, type), this.journal.read(wait)]);
    const mine = this.state?.set === set ? this.state.charts[index] : null;
    if (!mine || mine.finished) return;
    try {
      const saved = JSON.parse(journal.get("state") || "null");
      if (usable(saved) && saved.set === set) {
        const copy = saved.charts[index];
        copy.bars = JSON.parse(journal.get(`bars.${set}.${index}`) || "null") || [];
        copies.push(copy);
      }
    } catch { /* no usable copy */ }
    const others = copies.filter((c) => usableChart(c) && !c.finished && c.nCtx === mine.nCtx
      && c.nReplay === mine.nReplay).map((c) => {
      cleanCharts({ charts: [c] });
      c.bars = rowsFor(c.bars, c) || [];
      return c;
    }).filter(complete);
    const best = others.reduce((a, c) => (c.k > a.k ? c : a), mine);
    let touched = false;
    for (const c of [mine, ...others]) {
      if (c === best) continue;
      if (c.k === best.k && mergeTrades(best, c)) touched = true;
      const drawn = mergeDrawings(best, c, mine), noted = mergeNotes(best, c);
      if (drawn || noted) touched = true;
    }
    if (best === mine && !touched) return;
    const lost = best === mine ? [] : unseen(mine, best);
    best.view = mine.view;
    changed(best);
    this.state.charts[index] = best;
    if (lost.length) this.toast([unseenWords(lost, index + 1)]);
    this.save();
    this.render();
  }

  // Asks the other tabs of this page to stand still on chart index of this set and hand over their copies; resolves
  // with them. A tab with the set answers "holding" at once and sends its copy once a step it had on its way has
  // landed (see heard). No answer within a moment: no other tab has the set open.
  askTabs(set, index, type = "finishing") {
    const channel = this.channel;
    if (!channel) return Promise.resolve([]);
    const ask = Math.random().toString(36).slice(2);
    return new Promise((resolve) => {
      const holding = new Set(), copies = [];
      let timer = 0;
      const hear = (e) => {
        const m = e.data;
        if (!m || m.to !== this.tabId || m.ask !== ask) return;
        if (m.type === "holding") {
          holding.add(m.from);
          clearTimeout(timer); timer = setTimeout(done, 3000);
        } else if (m.type === "held") {
          holding.delete(m.from);
          if (m.chart) copies.push(m.chart);
          if (!holding.size) done();
        }
      };
      function done() { clearTimeout(timer); channel.removeEventListener("message", hear); resolve(copies); }
      channel.addEventListener("message", hear);
      timer = setTimeout(done, 250);
      this.post({ type, set, index, ask });
    });
  }

  post(message) {
    try { this.channel?.postMessage({ ...message, from: this.tabId }); } catch { /* the channel is closed */ }
  }

  // A message from another tab of this page. "moving": it is moving chart index of this set (see heardMoving).
  // "finishing": it is finishing chart index. This tab answers at once, stops stepping that chart (until the finish
  // arrives, or for 8 s), lets a step already on its way land, saves, and hands its copy over. "takeover": the visitor
  // took the chart over there; the same, without the wait, and said so here if this tab was moving it. "released":
  // that finish didn't happen.
  async heard(message) {
    if (!message || typeof message !== "object" || message.from === this.tabId || this.dead || !this.state) return;
    const { set, index } = message;
    if (set !== this.state.set || !Number.isInteger(index) || index < 0 || index >= N_CHARTS) return;
    if (message.type === "moving") { this.heardMoving(message); return; }
    if (message.type === "released") {
      if (this.held?.index === index) { this.held = null; this.render(); }
      return;
    }
    if (message.type !== "finishing" && message.type !== "takeover") return;
    const finishing = message.type === "finishing", was = this.movingHere(index);
    const reply = { set, index, to: message.from, ask: message.ask };
    this.post({ ...reply, type: "holding" });
    if (finishing) this.held = { set, index, until: Date.now() + 8000 };
    if (this.state.active === index) { this.pause(); this.queued = 0; }
    this.render();
    while (this.stepping) await new Promise((r) => setTimeout(r, 30));
    const chart = !this.dead && this.state?.set === set ? this.state.charts[index] : null;
    if (chart) this.saveNow({ rows: true });
    this.post({ ...reply, type: "held", chart: chart && !chart.finished ? { ...chart, view: null } : null });
    // The controls come back by themselves if the finish never arrives.
    if (finishing) setTimeout(() => this.render(), 8100);
    else if (was && this.state?.active === index) {
      this.toast([`Chart ${index + 1} was taken over in another tab, so it stopped here.`]);
    }
  }

  // The server says the chart was already finished, in another tab or window: finish it here too, at the bar this
  // tab is on, and say why.
  async autoFinish(index, chart) {
    const set = this.state?.set;
    if (!set || chart.finished || this.finishing) return;
    this.finishing = true;
    this.render();
    try {
      let resp;
      try {
        resp = await this.call("reveal", { cursor: chart.cursor });
      } catch (err) {
        this.failedNotice(err, () => this.autoFinish(index, chart));
        return;
      }
      if (!this.applyReveal(set, index, chart, resp, null)) return;
      this.notice("finished", { tone: "info", title: `Chart ${index + 1} was already finished.`,
                                detail: "It was finished in another tab or window, so it is finished here too, at "
                                  + "the bar you were on." });
      this.revealed(index);
    } finally {
      this.finishing = false;
      this.render();
    }
  }

  // The copy of a chart to finish: the one the visitor confirmed it on, or the same chart that another tab's save
  // put in its place further along (same set and index, at least as many bars in, not finished); else null.
  finishable(set, index, chart) {
    const now = this.state?.set === set ? this.state.charts[index] : null;
    if (!now || now.finished || this.state.expired) return null;
    return now === chart || now.k >= chart.k ? now : null;
  }

  // Put the server's reveal on the chart as it stands now (another tab may have moved it on since the reveal was
  // asked for: it finishes at its own bar, the rows after the reveal's bar coming from the reveal). Returns the
  // chart, or null when it was finished meanwhile, belongs to another set, or the rows don't join up.
  applyReveal(set, index, chart, resp, guess) {
    const current = this.state?.set === set ? this.state.charts[index] : null;
    const k = isNum(resp.k) ? resp.k : chart.k;
    if (!current || current.finished || current.k < k) return null;
    const rows = [current.bars, chart.bars].find((b) => b.length >= current.nCtx + k);
    if (!rows) return null;
    current.bars = rows.slice(0, current.nCtx + k).concat(resp.rest || []);
    if (current.bars.length < current.nCtx + current.k) return null;
    current.finished = true; current.finishedAt = current.k; current.reveal = this.revealOf(resp);
    if (guess) current.guess = guess;
    changed(current);
    if (index === this.state.active) {
      this.closeTicket(false);
      if (this.tool !== "cursor") this.setTool("cursor");
    }
    this.save();
    return current;
  }

  // After a reveal: the whole window comes into view, with the reveal card beside it (below 1100px, where the card
  // sits under the chart, the page scrolls to the chart, so what happened next is the first thing seen).
  revealed(index) {
    if (this.state.charts.every((c) => c.finished)) {
      this.notice("done", { tone: "done", title: "All ten charts finished.",
                            detail: "Download your report to see how you did against random entries and buying "
                              + "and holding.",
                            actions: [["Download report", () => this.download("report")],
                                      ["New set", () => this.newSet({ confirm: true })]] });
    }
    if (index !== this.state.active) return;
    this.renderNow();
    this.view.showWhole();
    if (innerWidth <= 1100) setTimeout(() => this.scrollUnderBar(this.el.stage.closest(".tt-chart-card")), 120);
    else this.el.side.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Scroll the page so el's top sits just under the site's top bar, which stays on screen (sticky) and would
  // otherwise cover it (on a phone, the chart's toolbar).
  scrollUnderBar(el) {
    if (this.dead || !el.isConnected) return;
    const top = el.getBoundingClientRect().top + scrollY - barCover() - 8;
    scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }

  // ------------------------------------------------ tools and drawings

  // Nothing to change on a chart another tab is moving: the chart says so, and how to take it over. True when so.
  refuseMoving(chart = this.chart) {
    if (!this.movingElsewhere(chart)) return false;
    this.view.showHint(MOVING_HINT);
    return true;
  }

  setTool(name) {
    const chart = this.chart;
    if (name !== "cursor" && this.locked(chart)) { this.refuseMoving(chart); return; }
    if ((name === "long" || name === "short") && atEnd(chart)) { this.view.showHint(END_HINT); return; }
    if ((name === "long" || name === "short") && this.draft) this.closeTicket(false);
    if (name !== "cursor") this.selection = null;
    this.tool = name;
    const preset = TOOLS.find((t) => t.tool === name)?.zone;
    this.view.setTool(preset ? "zone" : name, preset || null);
    this.closeMenu();
    this.render();
  }

  toggleMagnet() {
    this.settings.magnet = !this.settings.magnet;
    this.save();
    this.render();
  }

  undoStack() {
    const key = `${this.state.set}:${this.state.active}`;
    if (!this.undo.has(key)) this.undo.set(key, { done: [], undone: [] });
    return this.undo.get(key);
  }

  record(entry) {
    const stack = this.undoStack();
    stack.done.push(entry);
    if (stack.done.length > 200) stack.done.shift();
    stack.undone = [];
    this.render();
  }

  undoRedo(redo) {
    const chart = this.chart;
    if (!chart || this.locked(chart) || this.view.busy()) { if (chart) this.refuseMoving(chart); return; }
    const stack = this.undoStack(), from = redo ? stack.undone : stack.done, to = redo ? stack.done : stack.undone;
    const entry = from.pop();
    if (!entry) return;
    const list = chart.drawings, at = (id) => list.findIndex((d) => d.id === id);
    // What comes back is a change made now (stamp), so it outlasts the other tabs' copies; what goes is noted as gone.
    const restore = (d, i) => {
      list.splice(Math.min(i, list.length), 0, { ...clone(d), stamp: Date.now() });
      chart.deleted = (chart.deleted || []).filter((g) => g.id !== d.id);
    };
    const remove = (id) => { const i = at(id); if (i >= 0) { list.splice(i, 1); bury(chart, id); } };
    if (entry.op === "add") {
      if (redo) restore(entry.drawing, entry.index);
      else remove(entry.drawing.id);
    } else if (entry.op === "delete") {
      if (redo) remove(entry.drawing.id);
      else restore(entry.drawing, entry.index);
    } else if (entry.op === "update") {
      const i = at(entry.id);
      if (i >= 0) list[i] = { ...clone(redo ? entry.after : entry.before), stamp: Date.now() };
    }
    to.push(entry);
    this.view.clearFlash();
    if (this.selection?.kind === "drawing" && at(this.selection.id) < 0) this.selection = null;
    changed(chart);
    this.save();
    this.render();
  }

  // A drawing made on the chart. Its number is new to this chart (deleted ones' included); born and stamp (ms) let two
  // tabs' copies of the chart merge (see mergeDrawings).
  addDrawing(d) {
    const chart = this.chart;
    if (!chart || this.locked(chart)) return;
    const id = [...chart.drawings, ...(chart.deleted || [])].reduce((m, x) => Math.max(m, x.id), 0) + 1;
    const label = typeof d.label === "string" ? d.label.slice(0, 60) : "", now = Date.now();
    const drawing = { id, type: d.type, points: d.points, color: COLOURS.includes(d.color) ? d.color : "steel", label,
                      extend: !!d.extend, createdAt: chart.k, born: now, stamp: now };
    chart.drawings.push(drawing);
    this.record({ op: "add", drawing: clone(drawing), index: chart.drawings.length - 1 });
    this.selection = { kind: "drawing", id };
    changed(chart);
    this.save();
    this.render();
    this.scrollRow("drawings", id);
    // Made with a mouse, it takes a label typed straight away (see key) until the pointer moves away or clicks.
    const at = this.pointerAt;
    this.labelArm = this.view.lastPointer === "mouse" && at ? { id, chart, x: at.x, y: at.y } : null;
  }

  // A drawing moved on the chart. The bar it was moved on is kept, so the report can say a line drawn before the
  // replay was moved at bar 40.
  editedDrawing(d, before) {
    const chart = this.chart;
    if (chart && JSON.stringify(d.points) !== JSON.stringify(before.points)) d.movedAt = chart.k;
    d.stamp = Date.now();
    this.record({ op: "update", id: d.id, before: clone(before), after: clone(d) });
    if (chart) changed(chart);
    this.save();
    this.render();
  }

  patchDrawing(d, patch) {
    if (this.locked()) { this.refuseMoving(); return; }
    const before = clone(d);
    Object.assign(d, patch, { stamp: Date.now() });
    this.record({ op: "update", id: d.id, before, after: clone(d) });
    changed(this.chart);
    this.save();
    this.render();
  }

  deleteDrawing(id) {
    const chart = this.chart;
    if (!chart || this.locked(chart)) { if (chart) this.refuseMoving(chart); return; }
    const index = chart.drawings.findIndex((d) => d.id === id);
    if (index < 0) return;
    const [drawing] = chart.drawings.splice(index, 1);
    bury(chart, id);
    this.record({ op: "delete", drawing: clone(drawing), index });
    if (this.selection?.kind === "drawing" && this.selection.id === id) this.selection = null;
    changed(chart);
    this.save();
    this.render();
  }

  select(sel, { fromChart = false, quiet = false } = {}) {
    const same = (a, b) => (a && b ? a.kind === b.kind && a.id === b.id : a === b);
    if (same(sel, this.selection)) return;
    if (this.labelArm && !(sel?.kind === "drawing" && sel.id === this.labelArm.id)) this.labelArm = null;
    this.selection = sel;
    this.render();
    if (fromChart && sel && !quiet) this.scrollRow(sel.kind === "trade" ? "trades" : "drawings", sel.id);
    // A live trade picked on the chart, where its card is out of sight below it: what can be done, and where.
    const chart = this.chart, n = fromChart && sel?.kind === "trade" && innerWidth <= 1100
      ? chart?.trades.findIndex((t) => t.id === sel.id) : -1;
    const status = n >= 0 ? this.summary?.results?.[n]?.status : null;
    if (status === "open" || status === "pending") {
      const trade = chart.trades[n], name = `${sideWord(trade.side)} #${trade.id}`;
      this.view.showHint(`${name}: drag its stop or target line here, or ${status === "open" ? "close" : "cancel"} `
        + "or edit it on its card in the Trades panel below.");
    }
  }

  // Bring a row into view inside the side panel when the panel scrolls on its own (desktop), never the page.
  scrollRow(list, id) {
    requestAnimationFrame(() => {
      const side = this.el.side;
      if (side.scrollHeight <= side.clientHeight + 1 || getComputedStyle(side).overflowY === "visible") return;
      const row = this.el[list].querySelector(`[data-id="${id}"]`);
      if (row) this.showInPanel(row);
    });
  }

  // Scroll the side panel (when it scrolls on its own) so el is in view.
  showInPanel(el) {
    const side = this.el.side;
    if (side.scrollHeight <= side.clientHeight + 1 || getComputedStyle(side).overflowY === "visible") return;
    const top = el.getBoundingClientRect().top - side.getBoundingClientRect().top + side.scrollTop;
    const bottom = top + el.offsetHeight;
    if (top < side.scrollTop) side.scrollTop = top - 8;
    else if (bottom > side.scrollTop + side.clientHeight) {
      side.scrollTop = Math.min(top - 8, bottom - side.clientHeight + 8);
    }
  }

  // The page stays on the chart, which shows the label as it is typed; where the field is out of sight (the panels
  // under the chart) the chart says what the keys are doing, and the page is held where it is while they type (the
  // browser would scroll to the field's caret with each key; see drawingRow).
  focusLabel(id) {
    this.select({ kind: "drawing", id });
    this.renderNow();
    const input = this.el.drawings.querySelector(`.tt-label[data-id="${id}"]`);
    if (input && !input.disabled) {
      input.focus({ preventScroll: true });
      input.select();
      this.scrollRow("drawings", id);
      const r = input.getBoundingClientRect();
      if (r.top < barCover() || r.bottom > innerHeight) {
        this.labelHold = { input, y: scrollY, typed: performance.now() };
        this.view.showHint("Type its label, then press Enter.");
      }
    }
    return input && document.activeElement === input ? input : null;
  }

  // The first key typed after a drawing is made: its label field opens with that character in it, in place of what
  // was there (a space only opens it). Enter or Esc there gives the keys back to the page.
  typeLabel(id, ch) {
    const input = this.focusLabel(id);
    if (!input || !ch.trim()) return;
    input.setRangeText(ch, input.selectionStart ?? 0, input.selectionEnd ?? input.value.length, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // ------------------------------------------------ orders

  fromChartDraft(d) {
    if (!d || !d.done) return;
    const t = this.ticket || {}, fresh = !this.ticket;
    const entry = d.entry == null ? (t.entry || "") : priceText(d.entry);
    this.ticket = { side: d.side, market: d.entry == null, entry,
                    stop: priceText(d.stop), target: priceText(d.target), tried: false };
    this.syncDraft();
    this.render();
    // Stacked below 1100px, the chart's confirm bar may sit below the fold once the order is drawn: bring it up.
    if (fresh && innerWidth <= 1100) {
      requestAnimationFrame(() => requestAnimationFrame(() => this.el.draftbar.scrollIntoView({ block: "nearest" })));
    }
  }

  openTicket() {
    const chart = this.chart;
    if (!chart || this.locked(chart)) { if (chart) this.refuseMoving(chart); return; }
    if (atEnd(chart)) { this.view.showHint(END_HINT); return; }
    if (this.tool !== "cursor") this.setTool("cursor");
    // typed: the ticket is where this order is placed from, so the chart's confirm bar stays away (see renderDraftbar).
    this.ticket = { side: "long", market: true, entry: "", stop: "", target: "", tried: false, typed: true };
    this.syncDraft();
    this.render();
    requestAnimationFrame(() => this.t.stop.focus({ preventScroll: innerWidth > 1100 }));
  }

  closeTicket(render = true) {
    this.ticket = null; this.draft = null;
    // Focus left in the hidden ticket would keep the next key (→ to step) as typing; give it back to the page.
    if (this.el?.ticket.contains(document.activeElement)) document.activeElement.blur();
    if (render) this.render();
  }

  ticketSide(side) { if (this.ticket) { this.ticket.side = side; this.syncDraft(); this.render(); } }

  ticketMarket(market) {
    if (!this.ticket) return;
    this.ticket.market = market;
    if (!market && !this.ticket.entry) this.ticket.entry = priceText(this.sim.refClose(this.chart));
    this.syncDraft();
    this.render();
    if (!market) requestAnimationFrame(() => { this.t.entry.focus(); this.t.entry.select(); });
  }

  ticketInput(name, value) {
    if (!this.ticket) return;
    this.ticket[name] = value;
    this.syncDraft();
    this.renderTicketFacts();
    this.renderChartOnly();
  }

  // The ticket as an order: numbers where the fields hold them, NaN where they hold something else.
  ticketOrder() {
    const t = this.ticket;
    const entry = t.market ? null : readNumber(t.entry);
    return { side: t.side, entry: entry === null && !t.market ? NaN : entry, stop: readNumber(t.stop) ?? NaN,
             target: readNumber(t.target) ?? NaN };
  }

  syncDraft() {
    if (!this.ticket) { this.draft = null; return; }
    const o = this.ticketOrder();
    this.draft = { side: o.side, entry: o.entry, stop: o.stop, target: o.target, done: true };
  }

  // The order ticket. While another tab moves the chart it stays as typed but takes no changes and can't be placed,
  // and says why; it can still be discarded.
  renderTicket() {
    const sec = this.el.ticket, t = this.ticket, chart = this.chart;
    const moving = !!t && this.movingElsewhere(chart);
    const open = !!t && !!chart && (!this.locked(chart) || moving);
    // A ticket that just opened scrolls into view inside the side panel (desktop); on a phone the chart's own
    // confirm bar does the job, so the page stays put.
    if (open && sec.hidden) requestAnimationFrame(() => this.showInPanel(sec));
    sec.hidden = !open;
    this.renderDraftbar(open);
    if (!open) return;
    const els = this.t;
    const press = (b, on) => { b.classList.toggle("is-on", on); b.setAttribute("aria-pressed", String(on)); };
    press(els.long, t.side === "long"); press(els.short, t.side === "short");
    press(els.market, t.market); press(els.priced, !t.market);
    for (const b of [els.long, els.short, els.market, els.priced]) b.disabled = moving;
    for (const name of ["entry", "stop", "target"]) {
      if (document.activeElement !== els[name]) els[name].value = t[name];
      els[name].readOnly = moving;
    }
    els.entry.disabled = t.market;
    els.entry.placeholder = t.market ? "Next open" : "Price";
    els.title.textContent = `New ${t.side} order`;
    this.renderTicketFacts();
  }

  renderTicketFacts() {
    const t = this.ticket, els = this.t, chart = this.chart, sim = this.sim;
    if (!t || !chart) return;
    const ref = sim.refClose(chart), o = this.ticketOrder();
    const e = o.entry == null ? ref : o.entry;
    const kind = o.entry == null ? "market" : isNum(o.entry) ? sim.kindFor(o.side, o.entry, ref) : null;
    const kindText = { market: "Market, fills at the next open",
                       limit: `Limit, fills at ${sim.fmtPrice(o.entry)} or better`,
                       stop: `Stop entry, fills once the price reaches ${sim.fmtPrice(o.entry)}` }[kind] || "–";
    const rr = isNum(e) && isNum(o.stop) && isNum(o.target) && Math.abs(e - o.stop) > 0
      ? Math.abs(o.target - e) / Math.abs(e - o.stop) : null;
    const risk = isNum(e) && isNum(o.stop) ? Math.abs(e - o.stop) / e * 100 : null;
    const fact = (k, v, cls) => h("div", null, h("dt", { text: k }), h("dd", { class: cls || null, text: v }));
    els.facts.replaceChildren(fact("Order", kindText), fact("Reward to risk", rr == null ? "–" : rr.toFixed(2)),
      fact("Risk", risk == null ? "–" : `${risk.toFixed(2)}% of the price`), fact("Last price", sim.fmtPrice(ref)));
    const message = this.orderProblem(o, ref);
    const blank = !t.stop.trim() || !t.target.trim(), moving = this.movingElsewhere(chart);
    const shown = message && (!blank || t.tried);
    els.msg.textContent = moving ? MOVING_TICKET : shown ? message : "";
    els.msg.className = "tt-msg" + (moving ? " is-note" : shown ? " is-error" : "");
    els.place.className = `d-btn tt-place is-${t.side}`;
    els.place.replaceChildren(`Place ${t.side} order`, ...(touch() ? [] : [h("kbd", { text: "Enter" })]));
    els.place.disabled = moving;
    this.renderDraftbar(true);
  }

  // On narrow screens the ticket sits below the chart, so the chart carries a small confirm bar of its own for an
  // order drawn on it; one opened with New order is placed from the ticket, so there is one Place, not two (an order
  // then dragged on the chart is the chart's again: fromChartDraft makes a new ticket).
  renderDraftbar(open) {
    const bar = this.el.draftbar, t = this.ticket;
    bar.hidden = !open || !t || !!t.typed;
    if (bar.hidden) { bar.replaceChildren(); return; }
    const o = this.ticketOrder(), sim = this.sim, ref = sim.refClose(this.chart);
    // The same kind the ticket shows: an entry typed at the last price is a market order.
    const kind = o.entry == null ? "market" : isNum(o.entry) ? sim.kindFor(o.side, o.entry, ref) : null;
    const what = kind === "market" ? "market" : kind ? `${kind === "limit" ? "limit" : "stop entry"} `
      + sim.fmtPrice(o.entry) : "entry ?";
    const price = (x) => (isNum(x) ? sim.fmtPrice(x) : "?");
    bar.replaceChildren(h("span", { class: "tt-draftbar-what" },
        h("b", { class: o.side === "short" ? "d-down" : "d-up", text: sideWord(o.side) }),
        ` ${what} · stop ${price(o.stop)} · target ${price(o.target)}`),
      h("button", { type: "button", class: "d-btn", text: "Discard", onclick: () => this.closeTicket(true) }),
      h("button", { type: "button", class: `d-btn tt-place is-${o.side}`, text: "Place",
                    disabled: this.movingElsewhere(this.chart), onclick: () => this.placeOrder() }));
  }

  // What is wrong with the ticket's order, or null: prices typed in a form the page doesn't read, levels too far
  // from the price for this chart, then sim's own rules.
  orderProblem(o, ref) {
    const t = this.ticket;
    const typed = ["stop", "target", ...(t && !t.market ? ["entry"] : [])];
    if (t && typed.some((name) => String(t[name] ?? "").trim() && Number.isNaN(readNumber(t[name])))) {
      return "Type prices as plain numbers, like 105.50 or 1,250.00.";
    }
    return farLevel([o.entry, o.stop, o.target], ref)
      || this.sim.validateOrder(o.entry == null ? { ...o, entry: null } : o, ref);
  }

  // The ticket's order goes in at this tab's newest bar, priced from its close: a chart another tab is moving (or is
  // further along on) is locked, so that bar is always the newest any tab has shown.
  placeOrder() {
    const chart = this.chart, t = this.ticket, sim = this.sim;
    if (!t || !chart || this.locked(chart)) { if (t && chart) this.refuseMoving(chart); return; }
    if (atEnd(chart)) { this.closeTicket(false); this.view.showHint(END_HINT); this.render(); return; }
    t.tried = true;
    const ref = sim.refClose(chart), o = this.ticketOrder();
    const message = this.orderProblem(o, ref);
    if (message) { this.renderTicketFacts(); this.view.showHint(message, "error"); return; }
    const kind = sim.kindFor(o.side, o.entry, ref);
    const trade = { id: chart.nextId++, side: o.side, kind, entry: kind === "market" ? null : o.entry, stop: o.stop,
                    target: o.target, placedAt: chart.k, note: "", changes: [], cancelAt: null, closeAt: null };
    chart.trades.push(trade);
    changed(chart);
    this.closeTicket(false);
    this.selection = { kind: "trade", id: trade.id };
    this.save();
    const what = kind === "market" ? "fills at the next open"
      : `${kind === "limit" ? "limit" : "stop entry"} at ${sim.fmtPrice(trade.entry)}`;
    this.toast([`${sideWord(trade.side)} #${trade.id} placed: ${what}`]);
    this.render();
    this.scrollRow("trades", trade.id);
  }

  // Close and Cancel look at the trade as it stands now, not as its row last showed it: an order that filled since
  // can't be cancelled, and a trade that already closed can't be closed again.
  // The chart's own copy of a trade a row or the chart asked about (another tab's save may have replaced the chart
  // since): the same object, else the same trade; null when it isn't on the chart.
  liveTrade(chart, trade) {
    return chart?.trades.includes(trade) ? trade : chart?.trades.find((t) => sameTrade(t, trade)) || null;
  }

  closeTrade(row) {
    const chart = this.chart, trade = this.liveTrade(chart, row), name = `${sideWord(row.side)} #${row.id}`;
    if (!chart || this.locked(chart) || !trade || trade.closeAt != null) {
      if (chart) this.refuseMoving(chart);
      return;
    }
    if (atEnd(chart)) { this.toast([END_HINT]); return; }
    const status = this.sim.simulate(trade, chart).status;
    if (status !== "open") {
      this.toast([status === "pending" ? `${name} hasn’t filled yet; cancel it instead.`
        : `${name} is already ${status}.`]);
      this.render();
      return;
    }
    trade.closeAt = chart.k;
    changed(chart);
    this.save();
    this.toast([`${name} closes at the next open`]);
    this.render();
  }

  cancelTrade(row) {
    const chart = this.chart, trade = this.liveTrade(chart, row), name = `${sideWord(row.side)} #${row.id}`;
    if (!chart || this.locked(chart) || !trade || trade.cancelAt != null) {
      if (chart) this.refuseMoving(chart);
      return;
    }
    const status = this.sim.simulate(trade, chart).status;
    if (status !== "pending") {
      this.toast([status === "open" ? `${name} has already filled; close it at the next open instead.`
        : `${name} is already ${status}.`]);
      this.render();
      return;
    }
    trade.cancelAt = chart.k;
    changed(chart);
    this.save();
    this.toast([`${name} cancelled`]);
    this.render();
  }

  // A new stop or target, from the chart or the row's form. It acts from the next bar; null means it was accepted.
  changeLevels(row, { stop, target }) {
    const chart = this.chart, sim = this.sim, trade = this.liveTrade(chart, row);
    if (this.movingElsewhere(chart)) return MOVING_EDIT;
    if (!chart || this.locked(chart) || !trade) return "This chart is finished.";
    const res = sim.simulate(trade, chart), ref = sim.refClose(chart);
    if (Number.isNaN(stop) || Number.isNaN(target)) return "Type prices as plain numbers, like 105.50 or 1,250.00.";
    const message = farLevel([stop, target], ref)
      || sim.validateChange(trade, res, { stop: stop ?? NaN, target: target ?? NaN }, ref);
    if (message) return message;
    trade.changes = (trade.changes || []).filter((c) => c.at !== chart.k);
    trade.changes.push({ at: chart.k, stop, target });
    changed(chart);
    this.save();
    this.render();
    return null;
  }

  // ------------------------------------------------ menus and dialogs

  // A menu by its button. Opened from the keyboard (opener is the button's click, detail 0), focus goes into it and
  // back to the button when it closes; opened with a pointer, focus stays with the page, so the chart's keys still
  // work, and a pick or a tick inside it doesn't keep focus either.
  openMenu(anchor, fill, opener) {
    if (this.menu?.anchor === anchor) { this.closeMenu(); return; }
    this.closeMenu();
    const keyboard = opener?.detail === 0;
    const menu = h("div", { class: "tt-menu", role: "menu" });
    fill(menu);
    document.body.append(menu);
    const r = anchor.getBoundingClientRect(), w = menu.offsetWidth;
    menu.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.left))}px`;
    menu.style.top = `${r.bottom + 4}px`;
    anchor.setAttribute("aria-expanded", "true");
    this.menu = { el: menu, anchor, keyboard };
    if (!keyboard) {
      menu.addEventListener("click", (e) => {
        if (e.detail > 0 && menu.contains(document.activeElement)) document.activeElement.blur();
      });
    }
    // Up and down move between the menu's controls.
    menu.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const items = [...menu.querySelectorAll("input, button")], at = items.indexOf(document.activeElement);
      e.preventDefault();
      items[(at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    });
    if (keyboard) menu.querySelector("input, button")?.focus();
  }

  // Focus that was in a menu opened from the keyboard goes back to its button; otherwise it falls to the page.
  closeMenu() {
    if (!this.menu) return;
    const { el, anchor, keyboard } = this.menu, inside = el.contains(document.activeElement);
    anchor.setAttribute("aria-expanded", "false");
    el.remove();
    this.menu = null;
    if (inside && keyboard && anchor.isConnected) anchor.focus({ preventScroll: true });
    else if (inside) document.activeElement?.blur?.();
  }

  outside(e) {
    if (this.menu && !this.menu.el.contains(e.target) && !this.menu.anchor.contains(e.target)) this.closeMenu();
  }

  indicatorMenu(opener) {
    this.openMenu(this.el.indicators, (menu) => {
      const s = this.settings;
      const changed = (fn) => (e) => { fn(e.target.checked); this.save(); this.render(); };
      const row = (label, checked, color, onchange) => h("label", { class: "tt-menu-check" },
        h("input", { type: "checkbox", checked, onchange: changed(onchange) }),
        h("i", color ? { class: "tt-menu-swatch", style: `--c:${color}` } : { class: "tt-menu-swatch is-volume" }),
        h("span", { text: label }));
      menu.append(h("p", { class: "d-menu-title", text: "Indicators" }),
        row("Volume", s.volume !== false, null, (v) => { s.volume = v; }),
        ...this.lib.MA_LINES.map((ma) => row(ma.name, !!s.ma[ma.key], ma.color, (v) => { s.ma[ma.key] = v; })),
        h("p", { class: "tt-menu-note",
                 text: "Averages include earlier history, so they’re complete from the first bar." }));
    }, opener);
  }

  speedMenu(opener) {
    this.openMenu(this.el.speed, (menu) => {
      const s = this.settings;
      menu.append(h("p", { class: "d-menu-title", text: "Replay speed" }),
        ...SPEEDS.map((speed) => h("button", {
          type: "button", role: "menuitemradio", "aria-checked": String(s.speed === speed),
          class: "tt-menu-item" + (s.speed === speed ? " is-on" : ""),
          text: `${speed} bar${speed > 1 ? "s" : ""} a second`,
          onclick: () => this.setSpeed(speed),
        })),
        h("label", { class: "tt-menu-check" },
          h("input", { type: "checkbox", checked: !!s.autoPause,
                       onchange: (e) => { s.autoPause = e.target.checked; this.save(); } }),
          h("span", { text: "Pause when an order fills or a trade exits" })));
    }, opener);
  }

  setSpeed(speed) {
    this.settings.speed = speed;
    this.save();
    if (this.playing) { this.pause(); this.play(); }
    this.closeMenu();
    this.render();
  }

  // A confirm dialog; extra is more content between the text and the buttons. Both confirms undo nothing, so focus
  // starts on the safe choice: Enter twice, or a key repeat, keeps the set or the chart.
  ask({ title, body, confirm, cancel, extra = null }) {
    const dialog = this.el.dialog;
    if (dialog.open) dialog.close();
    return new Promise((resolve) => {
      const done = (value) => { dialog.close(); resolve(value); };
      const yes = h("button", { type: "button", class: "d-btn d-btn-primary", text: confirm,
                                onclick: () => done(true) });
      const no = h("button", { type: "button", class: "d-btn", text: cancel, onclick: () => done(false) });
      fill(dialog, h("h3", { text: title }), h("p", { text: body }), extra,
        h("div", { class: "d-form-actions" }, no, yes));
      dialog.onclose = () => resolve(false);
      dialog.oncancel = () => resolve(false);
      this.pause();
      dialog.showModal();
      no.focus();
    });
  }

  // Finish dialog, with an optional one-tap guess at the chart's era and type. Resolves to { guess } (guess null
  // when none was picked) or null when the visitor keeps trading. Open trades close at the next bar's open, or at the
  // last close once the replay has ended.
  async askFinish(n, chart) {
    const guess = { era: null, kind: null };
    const group = (label, key, options) => {
      const buttons = options.map(([value, text]) => h("button", {
        type: "button", class: "tt-seg-btn", text, "aria-pressed": "false", "data-value": value,
        onclick: (e) => {
          guess[key] = guess[key] === value ? null : value;
          for (const b of e.currentTarget.parentElement.children) {
            const on = b.dataset.value === guess[key];
            b.classList.toggle("is-on", on);
            b.setAttribute("aria-pressed", String(on));
          }
        },
      }));
      return h("div", { class: "tt-guess-row" }, h("span", { class: "tt-field-label", text: label }),
        h("div", { class: "tt-seg", role: "group", "aria-label": label }, buttons));
    };
    const extra = h("div", { class: "tt-guess" },
      h("span", { class: "tt-guess-title", text: "Want to guess first?" }),
      group("Era", "era", ERAS.map((era) => [era, era])), group("Type", "kind", [["stock", "Stock"], ["etf", "ETF"]]));
    const close = chart && chart.k >= chart.nReplay ? "the last close" : "the next bar’s open";
    const ok = await this.ask({ title: `Finish chart ${n}?`,
      body: `Open trades close at ${close}, unfilled orders expire, and you see what it was. `
        + "You can’t trade this chart again.",
      confirm: "Finish and reveal", cancel: "Keep trading", extra });
    return ok ? { guess: guess.era || guess.kind ? { ...guess } : null } : null;
  }

  // ------------------------------------------------ the guide

  // The guide's dialog, made once: its title and close button, the steps listed beside the step (on a phone a
  // progress line with dots instead), the step, and Show me (or, at the start of the first visit's guide, Skip), Back
  // and Next. A step is filled in as it is shown.
  buildGuide() {
    const g = {};
    const close = h("button", { type: "button", class: "tt-guide-x", "aria-label": "Close the guide",
                                html: icon("close"), onclick: () => this.closeGuide() });
    const item = (n) => h("button", { type: "button", onclick: () => this.guideStep(n) },
      h("span", { class: "tt-guide-num", text: String(n + 1) }), h("span"));
    g.items = Array.from({ length: GUIDE_STEPS }, (_, n) => item(n));
    g.count = h("span", { "aria-live": "polite" });
    g.dots = h("span", { class: "tt-guide-dots", "aria-hidden": "true" }, g.items.map(() => h("i")));
    g.step = h("section", { class: "tt-guide-step", "aria-labelledby": "tt-guide-h" });
    g.body = h("div", { class: "tt-guide-body" }, g.step);
    g.show = h("button", { type: "button", class: "d-btn tt-guide-show", html: `${icon("locate")}<span>Show me</span>`,
                           onclick: (e) => this.showMe(e.detail === 0) });
    g.skip = h("button", { type: "button", class: "d-btn tt-guide-skip", text: "Skip the guide", hidden: true,
                           onclick: () => this.closeGuide("start") });
    g.back = h("button", { type: "button", class: "d-btn tt-guide-back", text: "Back",
                           onclick: () => this.guideStep(this.guideAt - 1) });
    g.next = h("button", { type: "button", class: "d-btn d-btn-primary tt-guide-next", onclick: () => {
      if (this.guideAt < GUIDE_STEPS - 1) this.guideStep(this.guideAt + 1);
      else this.closeGuide(this.guideFirst ? "start" : "close");
    } });
    // tabindex: a click on its text keeps focus in the guide, rather than on the page behind it.
    const dialog = h("dialog", { class: "d-card tt-guide", role: "dialog", "aria-labelledby": "tt-guide-title",
                                 "aria-modal": "true", tabindex: "-1" },
      h("div", { class: "tt-guide-head" }, h("h2", { id: "tt-guide-title", text: "How to play" }), close),
      h("div", { class: "tt-guide-progress" }, g.count, g.dots),
      h("div", { class: "tt-guide-main" }, h("nav", { class: "tt-guide-nav", "aria-label": "Steps" }, g.items), g.body),
      h("div", { class: "tt-guide-foot" }, g.show, g.skip, g.back, g.next));
    // Esc closes it the browser's way; this hears that too.
    dialog.addEventListener("close", () => this.guideClosed());
    // So does a press on the dimmed page around it (begun there too: a text selection dragged out of it doesn't).
    const outside = (e) => {
      const r = dialog.getBoundingClientRect(), x = e.clientX, y = e.clientY;
      return e.target === dialog && (x < r.left || x > r.right || y < r.top || y > r.bottom);
    };
    let fromOutside = false;
    dialog.addEventListener("pointerdown", (e) => { fromOutside = outside(e); });
    dialog.addEventListener("click", (e) => {
      if (fromOutside && outside(e)) this.closeGuide();
      fromOutside = false;
    });
    // A finger swiped sideways across the step goes to the next step or back one.
    let swipe = null;
    g.body.addEventListener("pointerdown", (e) => {
      swipe = e.pointerType === "touch" ? { x: e.clientX, y: e.clientY } : null;
    });
    g.body.addEventListener("pointercancel", () => { swipe = null; });
    g.body.addEventListener("pointerup", (e) => {
      const from = swipe;
      swipe = null;
      if (!from || e.pointerType !== "touch") return;
      const dx = e.clientX - from.x, dy = e.clientY - from.y;
      if (Math.abs(dx) > 56 && Math.abs(dx) > 2 * Math.abs(dy)) this.guideStep(this.guideAt + (dx < 0 ? 1 : -1));
    });
    this.g = g;
    return dialog;
  }

  // The steps, worded for this screen: a finger or a mouse, where the panels sit, the head's labels and the tabs' word
  // for a finished chart (the widths are tradetest.css's).
  stepsNow() {
    const how = { touch: touch(), narrow: innerWidth <= 1100, phone: innerWidth <= 720,
                  done: innerWidth <= 1340 ? "Done" : "Finished" }, key = JSON.stringify(how);
    if (this.stepsKey !== key) { this.stepsKey = key; this.steps = guideSteps(how); }
    return this.steps;
  }

  // Open the guide at step n, by default the one it showed last. opener gets focus back when it closes (by default
  // whatever has focus now); first is the first visit's guide, which ends on Start playing; remember false leaves the
  // step it opens at next time as it was (? opens at the keys). Play pauses, and a menu or a tooltip gives way; so does
  // a confirm dialog, unless the guide was opened from it: it then waits under the guide, as it was.
  openGuide(n = this.settings.guideStep, { opener = document.activeElement, first = false, remember = true } = {}) {
    const dialog = this.el.guide;
    if (this.dead) return;
    this.clearSpot();
    this.closeMenu();
    if (this.el.dialog.open && !this.el.dialog.contains(opener)) this.el.dialog.close();
    this.pause();
    this.tip.hidden = true;
    this.guideFirst = first;
    this.guideOpener = opener && opener !== document.body && !dialog.contains(opener) ? opener : null;
    this.guideStep(n, { remember });
    if (!dialog.open) { dialog.showModal(); this.guideIsOpen = true; }
    this.g.next.focus();
  }

  // Step n of the open guide (held to the first and last), kept as the step it opens at next time unless remember is
  // false. Over a confirm dialog there is no Show me: what it lights up would be under the dialog.
  guideStep(n, { remember = true } = {}) {
    const g = this.g, steps = this.stepsNow(), at = clamp(n, 0, GUIDE_STEPS - 1), s = steps[at];
    this.guideAt = at;
    g.items.forEach((b, i) => {
      b.lastChild.textContent = steps[i].title;
      if (i === at) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
    });
    g.count.textContent = `Step ${at + 1} of ${GUIDE_STEPS}`;
    [...g.dots.children].forEach((dot, i) => dot.classList.toggle("is-on", i === at));
    const fold = (title, ...body) => h("details", { class: "d-details tt-guide-rules" }, h("summary", { text: title }),
      h("div", { class: "d-details-body tt-guide-rules-body" }, body));
    fill(g.step, h("h3", { id: "tt-guide-h", text: s.title }), s.pic && h("div", { class: "tt-ill", html: s.pic }),
      h("ol", { class: "tt-guide-do" }, s.do.map((html, i) => h("li", null,
        h("span", { class: "tt-guide-num", text: String(i + 1) }), h("span", { html })))),
      s.more && h("div", { class: "tt-guide-more", html: s.more }),
      s.keys === true && keysList("tt-keys tt-guide-keys"),
      s.keys && fold("Rules in detail", this.rulesBody(), h("p", { class: "tt-guide-note", text: DISCLAIMER })),
      s.keys === "folded" && fold("Keyboard", keysList("tt-keys tt-guide-keys")));
    g.body.scrollTop = 0;
    g.show.hidden = !s.show || this.el.dialog.open;
    g.skip.hidden = !(this.guideFirst && at === 0);
    g.back.disabled = at === 0;
    g.next.textContent = at < GUIDE_STEPS - 1 ? "Next" : this.guideFirst ? "Start playing" : "Close";
    // Focus on a button that just went away or off moves on to Next.
    const active = document.activeElement;
    if ([g.show, g.skip].some((b) => active === b && b.hidden) || (active === g.back && g.back.disabled)) {
      g.next.focus();
    }
    if (!remember) return;
    this.settings.guideStep = at;
    this.save(true);
  }

  // how: "close" (the close button, Close, Esc), "start" (Start playing, Skip the guide) or "show" (Show me).
  closeGuide(how = "close") {
    const dialog = this.el.guide;
    if (!dialog.open) return;
    this.guideHow = how;
    dialog.close();
    this.guideClosed();
  }

  // However the guide closed, it won't open by itself again, and the first visit's says where to find it again. Start
  // playing or Skip has it start from the top next time. Focus goes back to what opened it when that can take it (a
  // link in a dialog still open under it, say), else to the page; Show me places it itself.
  guideClosed() {
    if (!this.guideIsOpen) return;
    const how = this.guideHow || "close", opener = this.guideOpener, first = this.guideFirst;
    this.guideIsOpen = false; this.guideHow = null; this.guideOpener = null; this.guideFirst = false;
    this.settings.seenIntro = true;
    if (how === "start") this.settings.guideStep = 0;
    this.save();
    if (how === "show") return;
    if (first) {
      this.toast([`Reopen the guide any time from ${innerWidth <= 720 ? "Help" : "How to play"}`
        + `${touch() ? "" : ", or press H"}.`]);
    }
    const dialog = this.el.dialog;
    if (opener?.isConnected && !opener.disabled && opener.getClientRects().length
        && (dialog.open || !dialog.contains(opener))) {
      opener.focus({ preventScroll: true });
    } else if (this.el.guide.contains(document.activeElement)) document.activeElement.blur();
  }

  // Keys while the guide is open (the page's own are off): ← and → move between steps, and Tab goes round inside it.
  guideKey(e) {
    const dialog = this.el?.guide;
    if (!dialog?.open || e.defaultPrevented) return;
    if (e.key === "Tab") {
      const items = [...dialog.querySelectorAll("button, summary")]
        .filter((el) => !el.disabled && el.getClientRects().length);
      const at = items.indexOf(document.activeElement), last = items.length - 1;
      const to = e.shiftKey ? (at <= 0 ? last : null) : (at < 0 || at === last ? 0 : null);
      if (items.length && to != null) { e.preventDefault(); items[to].focus(); }
      return;
    }
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      this.guideStep(this.guideAt + (e.key === "ArrowRight" ? 1 : -1));
    }
  }

  // Show me: the guide closes and what its step is about lights up on the page (see spotlight). From the keyboard,
  // focus goes to the first of those controls; otherwise to the page.
  showMe(keyboard) {
    const n = this.guideAt;
    this.closeGuide("show");
    const spots = this.spotTargets(n);
    this.spotlight(n, spots);
    const to = keyboard ? spots.flatMap((s) => s.els)
      .map((el) => (el.matches("button") ? el : el.querySelector("button")))
      .find((b) => b && !b.disabled && b.getClientRects().length) : null;
    if (to) to.focus({ preventScroll: true });
    else document.activeElement?.blur?.();
  }

  // What Show me lights up for step n (counted from 0, as guideAt is: 1 is the second step), as groups of controls on
  // screen, each with its tooltip's words ([name, key] pairs, keys only for a keyboard) and, for a line of text, more
  // room round it (pad).
  spotTargets(n) {
    const e = this.el, key = (k) => (touch() ? null : k), shown = (b) => b.innerText.trim();
    const spots = {
      1: [{ els: [e.next, e.next5, e.play, e.speed, e.progress],
            tip: [["Next bar", key("→")], ["+5", key("Shift+→")], ["Play", key("P")], ["Speed"]] },
          { els: [e.fit], tip: [["Fit: the newest bars", key("F")]] }],
      // The Drawings panel only where it sits beside the chart: below it, it can't be on screen with the tools.
      2: [{ els: TOOLS.filter((t) => t.zone || POINTS[t.tool]).map((t) => e.tools[t.tool]),
            tip: [["Drawing tools", key("2 to 9")]] },
          { els: [e.magnet], tip: [["Magnet", key("M")]] },
          ...(innerWidth > 1100 ? [{ els: [e.drawings.parentElement], tip: [["Drawings: label, colour, delete"]] }]
            : [])],
      3: [{ els: [e.tools.long, e.tools.short], tip: [["Long", key("B")], ["Short", key("S")]] },
          { els: [e.newOrder], tip: [["New order: type the prices"]] }],
      4: [{ els: [e.trades.parentElement], tip: [["Trades: close, cancel, edit, notes"]] }],
      5: [{ els: [e.finish], tip: [[e.finish.getAttribute("aria-label") || "Finish chart"]] }],
      6: [{ els: [e.score], pad: 7, tip: [["The set’s score so far"]] },
          { els: [e.csv, e.copy, e.newSet, e.report],
            tip: e.report.disabled ? [["Report, CSV and Copy: once a chart is finished"], ["New set"]]
              : [e.csv, e.copy, e.newSet, e.report].map((b) => [shown(b)]) }],
    }[n] || [];
    return spots.map((s) => ({ ...s, els: s.els.filter((el) => el.getClientRects().length) }))
      .filter((s) => s.els.length);
  }

  // Light up spots ([{ els, tip, pad }]): bring them into view, ring each group in Action Blue (it pulses twice) with
  // its tooltip's words for about 3 s, and offer the way back to step n of the guide in a toast at the foot of the
  // screen. The toast stays until it is used or dismissed (its close button, Esc, the guide opening again), so the
  // visitor can try the controls first.
  spotlight(n, spots) {
    this.clearSpot();
    if (!spots.length) return;
    const spot = { made: [], frame: 0, timers: [] };
    const toast = h("div", { class: "tt-spot-toast", role: "status" },
      h("span", { text: `Step ${n + 1}: ${this.stepsNow()[n].title}` }),
      h("button", { type: "button", class: "tt-link", text: "Back to the guide",
                    onclick: () => this.openGuide(n, { opener: null }) }),
      h("button", { type: "button", class: "tt-spot-x", "aria-label": "Dismiss", html: icon("close"),
                    onclick: () => this.clearSpot() }));
    document.body.append(toast);
    spot.made.push(toast);
    this.revealSpots(spots, toast);
    const marks = spots.map((s) => {
      const ring = h("div", { class: "tt-spot", "aria-hidden": "true" });
      const tip = h("div", { class: "tt-spot-tip", "aria-hidden": "true" }, s.tip.map(([name, k], i) => [
        i ? h("i", { class: "tt-spot-sep" }) : null, h("span", { text: name }), k ? h("kbd", { text: k }) : null]));
      document.body.append(ring, tip);
      spot.made.push(ring, tip);
      return { els: s.els, ring, tip, pad: s.pad || 4 };
    });
    // The rings follow the page while it scrolls to them.
    const until = performance.now() + 3200;
    const frame = () => {
      placeSpots(marks, toast);
      if (performance.now() < until) { spot.frame = requestAnimationFrame(frame); return; }
      for (const m of marks) { m.ring.classList.add("is-out"); m.tip.classList.add("is-out"); }
      spot.timers.push(setTimeout(() => { for (const m of marks) { m.ring.remove(); m.tip.remove(); } }, 220));
    };
    frame();
    this.spot = spot;
  }

  // Bring what Show me lights up into view: a phone's scrolling tool row to them, the side panel (when it scrolls on
  // its own) to them, then the page, clear of the top bar and with room for a tip above the toast.
  revealSpots(spots, toast) {
    const row = this.el.toolRow;
    for (const { els } of spots) {
      if (row.contains(els[0]) && row.scrollWidth > row.clientWidth) {
        const box = row.getBoundingClientRect(), a = els[0].getBoundingClientRect();
        const b = els[els.length - 1].getBoundingClientRect();
        if (a.left < box.left + 8 || b.right > box.right - 8) row.scrollLeft += a.left - box.left - 8;
      }
      if (this.el.side.contains(els[0])) this.showInPanel(els[0]);
    }
    this.toolFade();
    const rects = spots.flatMap((s) => s.els.map((el) => el.getBoundingClientRect()));
    const top = Math.min(...rects.map((r) => r.top)), bottom = Math.max(...rects.map((r) => r.bottom));
    const from = barCover() + 16, to = Math.min(innerHeight - 80, toast.getBoundingClientRect().top - 48);
    const dy = top < from ? top - from : bottom > to ? Math.min(bottom - to, top - from) : 0;
    if (Math.abs(dy) > 1) scrollTo({ top: Math.max(0, scrollY + dy), behavior: stillMotion() ? "auto" : "smooth" });
  }

  clearSpot() {
    const spot = this.spot;
    if (!spot) return;
    cancelAnimationFrame(spot.frame);
    for (const t of spot.timers) clearTimeout(t);
    for (const el of spot.made) el.remove();
    this.spot = null;
  }

  // ------------------------------------------------ sharing

  // Copy result: one line for the clipboard, from report.js when it offers one, else the page's own; when the
  // browser won't copy, a box with the line selected.
  async copyResult() {
    if (!this.state) return;
    // Trades closed on unfinished charts count only once their rows are in (a short wait: the browser's permission to
    // copy doesn't last).
    await this.rowsForScoring(2500);
    let line = null;
    try {
      const mod = await import(`./report.js${V}`);
      if (typeof mod.resultLine === "function") line = mod.resultLine(clone(this.state));
    } catch { /* the page's own line below */ }
    if (typeof line !== "string" || !line.trim()) line = this.resultLine();
    try {
      await navigator.clipboard.writeText(line);
      this.toast(["Copied your result."]);
    } catch {
      this.showCopy(line);
    }
  }

  resultLine() {
    const score = this.setScore(), fmtR = this.sim.fmtR;
    if (!score) return `TradeTest: no finished charts yet. ${LINK}`;
    const { sum, base, charts, other } = score, many = plural(charts, "chart");
    const parts = [sum.trades ? `${fmtR(sum.totalR)} on ${many}` : `${many}, no closed trades`];
    if (sum.winRate != null) parts.push(`${Math.round(100 * sum.winRate)}% won`);
    if (base?.percentile != null) parts.push(`ahead of ${Math.round(100 * base.percentile)}% of random-entry runs`);
    const also = [other && `also ${plural(other.trades, "trade")} closed on unfinished charts: ${fmtR(other.totalR)}`,
                  this.missingWords(score)?.replace(/\.$/, "").replace(/^T/, "t")].filter(Boolean);
    return `TradeTest: ${parts.join(", ")}${also.length ? ` (${also.join("; ")})` : ""}. ${LINK}`;
  }

  showCopy(line) {
    const dialog = this.el.dialog;
    if (dialog.open) dialog.close();
    const box = h("textarea", { class: "tt-input tt-copy-box", rows: "3", readonly: true,
                                "aria-label": "Your result" });
    box.value = line;
    dialog.onclose = null; dialog.oncancel = null;
    fill(dialog, h("h3", { text: "Copy your result" }),
      h("p", { text: "This browser didn’t let the page copy it. The line is selected: copy it from here." }), box,
      h("div", { class: "d-form-actions" },
        h("button", { type: "button", class: "d-btn d-btn-primary", text: "Done", onclick: () => dialog.close() })));
    dialog.showModal();
    box.focus(); box.select();
  }

  // ------------------------------------------------ downloads

  async download(kind) {
    if (!this.state?.charts.some((c) => c.finished)) return;
    // The report counts the trades closed on unfinished charts, so their rows come first where they are missing.
    if (kind === "report") await this.rowsForScoring(4000);
    let mod;
    try {
      mod = await import(`./report.js${V}`);
    } catch {
      this.notice("report", { tone: "error", title: "The report couldn’t be loaded.",
                              detail: "Check your connection, then try again.",
                              actions: [["Try again", () => { this.clearNotice("report"); this.download(kind); }]] });
      return;
    }
    this.clearNotice("report");
    const state = clone(this.state), set = String(state.set || "set");
    try {
      if (kind === "report") {
        const html = mod.buildReport(state, { renderImage: (chart) => this.image(chart), generatedAt: new Date() });
        const name = mod.reportFileName ? mod.reportFileName(state) : `tradetest-report-${set}.html`;
        this.saveFile(name, "text/html;charset=utf-8", html);
      } else {
        const csv = mod.buildTradesCsv(state);
        const name = mod.tradesCsvFileName ? mod.tradesCsvFileName(state) : `tradetest-trades-${set}.csv`;
        this.saveFile(name, "text/csv;charset=utf-8", csv);
      }
    } catch {
      this.notice("report", { tone: "error", title: "The report couldn’t be built.",
                              detail: "Your work is safe in this browser. Try again after the next bar." });
    }
  }

  image(chart) {
    const canvas = document.createElement("canvas");
    this.lib.renderStatic(canvas, chart, { width: 1200, height: 560, scale: 1, settings: this.settings });
    return canvas.toDataURL("image/png");
  }

  saveFile(name, type, text) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = h("a", { href: url, download: name, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 15000);
    // The file is handed to the browser, which may still ask where to put it.
    this.toast([`Downloading ${name}`]);
  }

  // ------------------------------------------------ keyboard

  key(e) {
    if (this.dead || !this.root.isConnected || e.defaultPrevented || this.el.dialog.open || this.el.guide.open) return;
    let target = e.target;
    const k = e.key, lower = k.length === 1 ? k.toLowerCase() : k;
    const mod = e.ctrlKey || e.metaKey;
    if (this.menu && k === "Escape") { this.closeMenu(); e.preventDefault(); return; }
    // ? or H opens the guide from anywhere on the page but a text field, unless a drawing just made is waiting for its
    // label (see below): then it is the label's first character. ? opens it at the keys, H where it was left.
    if ((k === "?" || lower === "h") && !mod && !e.altKey && !this.labelArm && !target?.closest?.(TEXT_FIELD)) {
      e.preventDefault();
      if (k === "?") this.openGuide(GUIDE_STEPS - 1, { remember: false });
      else this.openGuide();
      return;
    }
    // A menu opened with a pointer keeps no keys: one pressed in it closes it, and the key goes to the page.
    if (this.menu && !this.menu.keyboard && this.menu.el.contains(target) && k !== "Tab" && !MODIFIERS.has(k)) {
      this.closeMenu();
      target = document.body;
    }
    // Shortcuts are for the page itself and for TradeTest's own controls: never for a key pressed in the site's
    // navigation, in a menu, or on a chart tab reached from the keyboard (the tabs have their own arrow keys). "The
    // page" is the body or any focusable wrapper around TradeTest (the shell's layout takes focus when the chart is
    // clicked).
    const page = target === document.body || target === document.documentElement || !target?.closest
      || (target.contains(this.root) && target !== this.root);
    const tab = page ? null : target.closest('[role="tablist"]');
    if (!page && (!this.root.contains(target) || target.closest('[role="menu"]') || (tab && keyboardFocus(target)))) {
      return;
    }
    // Text fields keep their keys; a checkbox or radio is a control like a button, so Ctrl+Z still undoes.
    const field = page ? null : target.closest(TEXT_FIELD);
    if (field) {
      if (this.el.ticket.contains(field)) {
        if (k === "Enter") { e.preventDefault(); this.placeOrder(); }
        else if (k === "Escape") { e.preventDefault(); this.closeTicket(true); }
      } else if (k === "Escape" || (k === "Enter" && field.classList.contains("tt-label"))) field.blur();
      return;
    }
    // Just after a drawing is made with the mouse, typing names it (as other chart apps take it): the first character
    // opens its label field with it. Any other key, or the pointer moving or clicking, ends that.
    const arm = this.labelArm;
    if (arm && !MODIFIERS.has(k)) {
      this.labelArm = null;
      const own = this.chart === arm.chart && this.selection?.kind === "drawing" && this.selection.id === arm.id;
      if (own && k.length === 1 && !mod && !e.altKey && !this.view.busy()) {
        e.preventDefault();
        this.typeLabel(arm.id, k);
        return;
      }
    }
    // A control the visitor reached from the keyboard keeps Space and Enter for itself; one a pointer left focus on
    // (or that a dialog gave focus back to) doesn't, so Space is always Next bar for a mouse or finger user.
    const control = page ? null : target.closest('button, a, summary, input, label, [role="tab"]');
    const onButton = control && keyboardFocus(document.activeElement) ? control : null;
    if (mod && !e.altKey) {
      if (lower === "z" && !e.shiftKey) { e.preventDefault(); this.undoRedo(false); }
      else if (lower === "y" || (lower === "z" && e.shiftKey)) { e.preventDefault(); this.undoRedo(true); }
      return;
    }
    if (e.altKey) return;
    // Esc: a drawing or order half made goes, and the tool with it (one press, back to the cursor); else the order
    // waiting to be placed, the selection, then Show me's way back to the guide.
    if (k === "Escape") {
      const half = this.view.cancel();
      if (this.tool !== "cursor") { this.setTool("cursor"); e.preventDefault(); return; }
      if (half) { e.preventDefault(); return; }
      if (this.draft) { this.closeTicket(true); e.preventDefault(); return; }
      if (this.selection) { this.select(null); return; }
      this.clearSpot();
      return;
    }
    if (k === "Enter" && this.draft && !onButton) { e.preventDefault(); this.placeOrder(); return; }
    if (k === "ArrowRight") { e.preventDefault(); this.step(e.shiftKey ? 5 : 1); return; }
    if (k === " " && !onButton) { e.preventDefault(); this.step(1); return; }
    if ((k === "Delete" || k === "Backspace") && this.selection?.kind === "drawing") {
      e.preventDefault();
      this.deleteDrawing(this.selection.id);
      return;
    }
    const tool = TOOLS.find((t) => t.key === k);
    if (tool) { this.setTool(tool.tool); return; }
    if (lower === "b") this.setTool("long");
    else if (lower === "s") this.setTool("short");
    else if (lower === "m") this.toggleMagnet();
    else if (lower === "f") this.view.fit();
    else if (lower === "p") this.togglePlay();
  }
}

