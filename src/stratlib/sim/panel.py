"""Daily price panel for the ground-rules backtests, shared by the app and the research scripts.

One row per SPY session, one column per security. Prices are FMP's
split-adjusted bars. `factor` turns them back into the price that traded
that day (as_traded = adjusted * factor), by undoing only the splits that
happened after that day and before the bars were downloaded.

Universe: today's common stocks, common stocks delisted since 2016 (FMP's
directory), and ETFs (current and delisted) whose 20-day average dollar
volume ever reached the floor. Only securities that pass the liquidity rule
on at least one day of the study are kept.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..app import open_context
from ..backtest_approx import SPLITS, members
from ..backtest_data import ET

PANEL_FROM = "2014-01-01"  # two years of warm-up before the study
STUDY_FROM = "2016-01-01"
MIN_PRICE = 5.0           # the research's liquidity floor; the app's backtests pass their own (sim/runs.py)
MIN_DOLLAR_VOLUME = 20e6
ADV_SESSIONS = 20
RANK_SESSIONS = 63  # "3-month return" used to choose between signals
FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class Panel:
    dates: np.ndarray  # ISO strings, SPY sessions
    symbols: np.ndarray
    kind: np.ndarray  # "stock" | "etf"
    until: np.ndarray  # delisting date or ""
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    factor: np.ndarray  # adjusted price * factor = as-traded price
    notes: dict = field(default_factory=dict)
    min_price: float = MIN_PRICE              # the liquidity floor that sets ``eligible``
    min_dollar_volume: float = MIN_DOLLAR_VOLUME

    def __post_init__(self):
        self.index = {s: j for j, s in enumerate(self.symbols)}
        self.day = {d: i for i, d in enumerate(self.dates)}
        valid = np.isfinite(self.close) & (self.close > 0)
        self.valid = valid
        n_days = len(self.dates)
        self.first_bar = np.where(valid.any(axis=0), valid.argmax(axis=0), n_days)
        self.last_bar = np.where(valid.any(axis=0), n_days - 1 - valid[::-1].argmax(axis=0), -1)
        closes = pd.DataFrame(np.where(valid, self.close, np.nan))
        self.close_ff = closes.ffill(limit=5).to_numpy()
        dollar = pd.DataFrame(np.where(valid, self.close * np.nan_to_num(self.volume), np.nan))
        self.adv20 = dollar.rolling(ADV_SESSIONS, min_periods=15).mean().to_numpy()
        self.as_traded_close = self.close * self.factor
        with np.errstate(invalid="ignore", divide="ignore"):
            lag = np.full_like(self.close_ff, np.nan)
            lag[RANK_SESSIONS:] = self.close_ff[:-RANK_SESSIONS]
            self.ret63 = self.close_ff / lag - 1
        self.eligible = self.liquid(self.min_price, self.min_dollar_volume)

    def liquid(self, min_price: float, min_dollar_volume: float) -> np.ndarray:
        """Sessions a security clears a liquidity floor: an as-traded close of at least min_price and a 20-session
        average dollar volume of at least min_dollar_volume."""
        with np.errstate(invalid="ignore"):
            return self.valid & (self.as_traded_close >= min_price) & (self.adv20 >= min_dollar_volume)

    def slice_from(self, day: str) -> int:
        return int(np.searchsorted(self.dates, day))

    def session_index(self, day: str) -> int:
        """Index of the first session on or after day."""
        return int(np.searchsorted(self.dates, day))


def _split_rows(store, symbols, calendar):
    """Per-symbol split histories plus the market-wide calendar, deduplicated by date."""
    by_symbol = {}
    for row in calendar.get("rows", []):
        by_symbol.setdefault(row.get("symbol"), {})[row.get("date")] = row
    missing = []
    for symbol in symbols:
        doc = store.document(f"backtest:splits:{symbol}")
        rows = by_symbol.setdefault(symbol, {})
        if doc and not doc.get("error"):
            for row in doc.get("rows", []):
                rows[row.get("date")] = row
        else:
            missing.append(symbol)
    return by_symbol, missing


def _factor_column(dates: np.ndarray, splits: dict, basis: str) -> np.ndarray:
    factor = np.ones(len(dates))
    for day, row in splits.items():
        try:
            num, den = float(row.get("numerator") or 0), float(row.get("denominator") or 0)
            date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if num <= 0 or den <= 0 or day > basis:
            continue
        factor[dates < day] *= num / den
    return factor


def _blank(arrays, rows, cols):
    for f in FIELDS:
        arrays[f][rows, cols] = np.nan


def clean_bad_bars(arrays, until, factor) -> dict:
    """Remove prints that are data errors rather than trading.

    - A one-day jump beyond x2.5 (or below /2.5) that reverses the next
      session (back within x1.5 of the prior close) is a bad bar.
    - Stretches where x10 jumps cluster are blanked, and a ticker that turns
      into a penny series after an acquisition ends at its last real bar.
    - A delisted security's final bar below 1/5 or above 5x the prior close
      (for example $72 to $0.03 on the day of an acquisition) is a bad bar.
    - A low below half of min(open, close), or a high above twice
      max(open, close), is clipped to the body of the bar.
    """
    close = arrays["close"]
    valid = np.isfinite(close) & (close > 0)
    # Two series interleaved under one ticker: x10 jumps in both directions,
    # four or more within 60 sessions. The whole stretch is unusable.
    c = pd.DataFrame(np.where(valid, close, np.nan))
    with np.errstate(invalid="ignore", divide="ignore"):
        huge = np.abs(np.log(close / c.ffill(limit=5).shift(1).to_numpy())) > np.log(10)
    interleaved = 0
    for j in np.flatnonzero(huge.sum(axis=0) >= 4):
        days = np.flatnonzero(huge[:, j])
        start = 0
        for k in range(1, len(days) + 1):
            if k == len(days) or days[k] - days[k - 1] > 60:
                if k - start >= 4:
                    rows = np.arange(max(days[start] - 1, 0), days[k - 1] + 1)
                    interleaved += int(valid[rows, j].sum())
                    _blank(arrays, rows, j)
                start = k
    close = arrays["close"]
    valid = np.isfinite(close) & (close > 0)
    c = pd.DataFrame(np.where(valid, close, np.nan))
    prev = c.ffill(limit=5).shift(1).to_numpy()
    nxt = c.bfill(limit=5).shift(-1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        jump, back = close / prev, nxt / prev
        spikes = valid & ((jump > 2.5) | (jump < 0.4)) & (back > 1 / 1.5) & (back < 1.5)
    rows, cols = np.nonzero(spikes)
    _blank(arrays, rows, cols)
    final = 0
    for j in np.flatnonzero(until != ""):
        for _ in range(3):
            idx = np.flatnonzero(np.isfinite(arrays["close"][:, j]) & (arrays["close"][:, j] > 0))
            if len(idx) < 2:
                break
            ratio = arrays["close"][idx[-1], j] / arrays["close"][idx[-2], j]
            if 0.2 <= ratio <= 5:
                break
            _blank(arrays, [idx[-1]], [j])
            final += 1
    # An acquired company's ticker continuing as another, penny-priced series:
    # a fall of more than 97% in one session, from $10 or more (as traded) to
    # under $2. The record ends at the last real bar.
    changed = []
    close = arrays["close"]
    valid = np.isfinite(close) & (close > 0)
    prev = pd.DataFrame(np.where(valid, close, np.nan)).ffill(limit=5).shift(1).to_numpy()
    traded, traded_prev = close * factor, prev * pd.DataFrame(factor).shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        cut = valid & (close / prev < 0.03) & (traded_prev >= 10) & (traded < 2)
    for j in np.flatnonzero(cut.any(axis=0)):
        i = int(np.flatnonzero(cut[:, j])[0])
        _blank(arrays, np.arange(i, close.shape[0]), j)
        changed.append((j, i))
    o, h, lo, cl = arrays["open"], arrays["high"], arrays["low"], arrays["close"]
    with np.errstate(invalid="ignore"):
        body_low, body_high = np.fmin(o, cl), np.fmax(o, cl)
        bad_low = np.isfinite(lo) & (lo < 0.5 * body_low)
        bad_high = np.isfinite(h) & (h > 2 * body_high)
    arrays["low"] = np.where(bad_low, body_low, lo)
    arrays["high"] = np.where(bad_high, body_high, h)
    return {"interleaved_bars_removed": interleaved, "spike_bars_removed": int(len(rows)), "final_bars_removed": final,
            "instrument_changes": changed,
            "lows_clipped": int(bad_low.sum()), "highs_clipped": int(bad_high.sum())}


def merge_duplicates(arrays, dates, symbols, until) -> list[dict]:
    """Tickers that carry the same company's history (renames kept under the old
    and new ticker). Identical daily moves (to 0.01%) on at least 15 days of 2%+
    moves, and on 30% of the shorter record's such days, mark a duplicate. Each
    group keeps the ticker whose record runs latest (then the longest); the
    others lose the overlapping days.
    """
    close = arrays["close"]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = close[1:] / close[:-1] - 1
    moved = np.isfinite(r) & (np.abs(r) > 0.02)
    by_move = {}
    for i, j in zip(*np.nonzero(moved)):
        by_move.setdefault((i, round(float(r[i, j]), 4)), []).append(j)
    pairs = {}
    for js in by_move.values():
        if 1 < len(js) < 6:
            for a in range(len(js)):
                for b in range(a + 1, len(js)):
                    pairs[js[a], js[b]] = pairs.get((js[a], js[b]), 0) + 1
    moves = moved.sum(axis=0)
    parent = list(range(len(symbols)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), n in pairs.items():
        if n >= 15 and n >= 0.3 * min(moves[a], moves[b]):
            parent[find(a)] = find(b)
    groups = {}
    for j in range(len(symbols)):
        groups.setdefault(find(j), []).append(j)
    valid = np.isfinite(close) & (close > 0)
    result = []
    for js in groups.values():
        if len(js) < 2:
            continue
        last = {j: (np.flatnonzero(valid[:, j])[-1] if valid[:, j].any() else -1) for j in js}
        keeper = max(js, key=lambda j: (last[j], valid[:, j].sum(), until[j] == "", symbols[j]))
        for j in js:
            if j != keeper:
                _blank(arrays, np.flatnonzero(valid[:, keeper] & valid[:, j]), j)
        result.append({"keep": symbols[keeper], "merged": sorted(symbols[j] for j in js if j != keeper)})
    return result


def build(through: str | None = None, *, progress=print, store=None, settings=None, include_etfs: bool = True,
          min_price: float = MIN_PRICE, min_dollar_volume: float = MIN_DOLLAR_VOLUME) -> Panel:
    """The panel through `through` (default: the latest SPY session). The research opens its own context;
    the app passes its store and settings. Without ETFs only SPY and QQQ are added, for the benchmark and the
    market filters, and the panel marks them ineligible so no strategy trades them. Only securities that clear
    min_dollar_volume at some point from 2016 are kept, and ``eligible`` applies both floors."""
    ctx = open_context() if store is None else None
    if ctx is not None:
        store, settings = ctx.store, ctx.settings
    try:
        sessions = [b.date for b in store.price_history("SPY", since=PANEL_FROM)]
        if through:
            sessions = [d for d in sessions if d <= through]
        dates = np.array(sessions)
        end = sessions[-1]

        stock_members, counts = members(store, settings, STUDY_FROM, end)
        etf_doc = (store.document("research:etf_universe") if include_etfs else None) or {"qualifying": []}
        # A delisted row that matched an ETF-like name but whose profile says it is
        # not an ETF (WisdomTree's own stock, WETF) is left out.
        etfs = {row["symbol"]: row for row in etf_doc["qualifying"] if row.get("profile_is_etf") is not False}
        # A delisted stock whose ticker is now an ETF would carry the ETF's prices.
        clashes = [s for s, m in stock_members.items() if s in etfs and m["source"] == "delisted"]
        for s in clashes:
            stock_members.pop(s)
        meta = {s: ("stock", m["until"] or "") for s, m in stock_members.items() if s not in etfs}
        meta.update({s: ("etf", row.get("until") or "") for s, row in etfs.items()})
        for s in ("SPY",) if include_etfs else ("SPY", "QQQ"):
            meta.setdefault(s, ("etf", ""))
        symbols = sorted(meta)
        progress(f"Loading prices for {len(symbols):,} securities, {len(dates):,} sessions ({dates[0]}..{end})")

        index = {d: i for i, d in enumerate(sessions)}
        shape = (len(dates), len(symbols))
        arrays = {f: np.full(shape, np.nan, dtype=np.float64) for f in FIELDS}
        conn = sqlite3.connect(settings.data.db_path)
        try:
            for start in range(0, len(symbols), 400):
                chunk = symbols[start:start + 400]
                marks = ",".join("?" * len(chunk))
                frame = pd.read_sql_query(
                    f"SELECT symbol, date, open, high, low, close, volume FROM prices"
                    f" WHERE symbol IN ({marks}) AND date >= ? AND date <= ?",
                    conn, params=[*chunk, PANEL_FROM, end])
                rows = frame["date"].map(index)
                keep = rows.notna()
                frame, rows = frame[keep], rows[keep].astype(int).to_numpy()
                cols = frame["symbol"].map({s: start + k for k, s in enumerate(chunk)}).to_numpy()
                for f in FIELDS:
                    arrays[f][rows, cols] = frame[f].astype(float).to_numpy()
                if start % 2000 == 0:
                    progress(f"  {start + len(chunk):,}/{len(symbols):,}")
        finally:
            conn.close()

        calendar = store.document(SPLITS) or {"rows": [], "calendar_from": "9999-12-31"}
        splits, no_history = _split_rows(store, symbols, calendar)
        states = store.price_states(symbols)
        factor = np.ones((len(dates), len(symbols)))
        for j, s in enumerate(symbols):
            state = states.get(s)
            basis = state.checked_at.astimezone(ET).date().isoformat() if state and state.checked_at else end
            factor[:, j] = _factor_column(dates, splits.get(s, {}), basis)

        cleaning = clean_bad_bars(arrays, np.array([meta[s][1] for s in symbols]), factor)
        cleaning["instrument_changes"] = [f"{symbols[j]} from {dates[i]}" for j, i in cleaning["instrument_changes"]]
        progress(f"Cleaning: {cleaning}")

        # Liquidity is split-invariant, so keep only securities that pass it once.
        close, volume = arrays["close"], arrays["volume"]
        valid = np.isfinite(close) & (close > 0)
        adv = pd.DataFrame(np.where(valid, close * np.nan_to_num(volume), np.nan)).rolling(
            ADV_SESSIONS, min_periods=15).mean().to_numpy()
        study = dates >= STUDY_FROM
        liquid = (adv[study] >= min_dollar_volume).any(axis=0)
        liquid[symbols.index("SPY")] = True
        keep = np.flatnonzero(liquid)
        symbols = [symbols[j] for j in keep]
        factor = factor[:, keep]
        for f in FIELDS:
            arrays[f] = arrays[f][:, keep]
        progress(f"Securities ever at or above ${min_dollar_volume / 1e6:g}M average dollar volume: {len(symbols):,}")
        duplicates = merge_duplicates(arrays, dates, symbols, np.array([meta[s][1] for s in symbols]))
        progress(f"Duplicate histories merged: {len(duplicates):,} groups")
        first_dates = {s: (states[s].first_date if s in states else None) or "9999" for s in symbols}
        uncovered = sorted(s for s in set(no_history) & set(symbols) if first_dates[s] < calendar.get("calendar_from", "9999"))
        notes = {
            "through": end,
            "members": dict(counts),
            "delisted_stock_ticker_now_etf": clashes,
            "cleaning": cleaning,
            "duplicates": duplicates,
            "securities": len(symbols),
            "stocks": sum(1 for s in symbols if meta[s][0] == "stock"),
            "etfs": sum(1 for s in symbols if meta[s][0] == "etf"),
            "delisted_stocks": sum(1 for s in symbols if meta[s][0] == "stock" and meta[s][1]),
            "delisted_etfs": sum(1 for s in symbols if meta[s][0] == "etf" and meta[s][1]),
            "split_history_missing": uncovered,
        }
        panel = Panel(dates=dates, symbols=np.array(symbols), kind=np.array([meta[s][0] for s in symbols]),
                      until=np.array([meta[s][1] for s in symbols]), factor=factor, notes=notes, min_price=min_price,
                      min_dollar_volume=min_dollar_volume, **arrays)
        if not include_etfs:
            panel.eligible[:, panel.kind != "stock"] = False
        return panel
    finally:
        if ctx is not None:
            ctx.close()
