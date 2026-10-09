"""TradeTest's server side: the window bank, sets of ten blind charts, and the sealed cursors that deliver their bars.

The browser receives no bar past the visitor's position, nor the instrument or the dates, until the visitor finishes
the chart, and finishing is one way: a revealed chart steps no further. Each chart's position in its replay travels as
a sealed cursor token: the payload is encrypted with an HMAC-SHA256 keystream and authenticated with a separate HMAC,
so it can be neither read nor changed. Hand-made API calls can still step ahead and come back to an older cursor; that
only fools the caller's own report, which is scored in the browser.

Prices are rescaled so the last bar before the replay closes at 100, and volume is relative to the context's median.
Real prices sit on grids (whole cents, the cent divided by later splits, round numbers), and a rescaled grid would give
the price level away, so every price first moves by a fixed random amount that spans each grid the window's prices sit
on, the same on every request. No share count leaves the server: each bar's volume is sent as a multiple of the
context's median volume. Volumes sit on round lots, though, and relative volumes of round lots would let the median be
solved for in shares (and with it the price level of a thinly traded, high-priced stock), so each bar's volume first
moves by a fixed random amount within half a lot either way, which leaves no trace of the lots.

Pure Python and numpy; the web layer (web/tradetest.py) maps the handlers to HTTP.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("stratlib.tradetest")

CHARTS = 10
MAX_STEP = 20
MAX_PER_YEAR = 2
MIN_FRESH = 50          # below this many unseen windows, a new set draws from the whole bank again
AVOID_KEPT = 300
MAX_CURSOR = 600        # characters
MAX_AVOID = 4096        # an avoid token lists up to AVOID_KEPT window ids, so it is longer than a cursor
REVEALED_KEPT = 200_000  # finished charts remembered, oldest forgotten first (lost on restart: best effort)
KEY_ENV = "STRATLIB_TRADETEST_KEY"
MACHINE_ID = Path("/etc/machine-id")
_PROCESS_KEY = secrets.token_bytes(32)
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_SET = re.compile(r"[0-9a-f]{8}")
# Grids a window's prices may sit on, coarsest first. Stored (split-adjusted) prices are mostly whole cents, some series
# move in even cents, nickels or dimes and some keep more decimals; prices as traded are cents, and highs and lows gather
# at round quarters, halves and dollars. A grid counts when the prices sit on it at least VISIBLE firmly (what the grid
# attack would still see); one coarser than a cent must also be spanned SPAN_STEPS times, or a price that merely
# lingers at one level would pass for one (and widen the dither for nothing). Stored prices that carry dividend
# adjustments as well as splits turn into prices as traded by a factor that drifts, so a grid of theirs can hold in
# each stretch of DRIFT_BARS bars without lining up across the window: it counts when those stretches average DRIFTING.
GRIDS = (1.0, 0.5, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01, 0.001, 0.0001, 0.00001)
TRADED_GRIDS = GRIDS[:8]
VISIBLE, SPAN_STEPS = 0.1, 20
DRIFT_BARS, DRIFTING = 50, 0.12
# The volume dither: up to LOT_DITHER shares either way on each bar, half a round lot.
LOT_DITHER = 50.0
MESSAGES = {
    "bad_token": "This chart link is not valid. Start a new set.",
    "expired": "TradeTest’s charts were updated since you started this set. Download your report, then start a new "
               "set.",
    "unavailable": "TradeTest is not available right now. Please try again later.",
    "rate_limited": "Too many requests at once. Wait a moment, then try again.",
    "finished": "This chart has already been finished.",
}


class TradeTestError(Exception):
    """A request the service refuses: a code for the browser, a sentence for the visitor and an HTTP status."""

    def __init__(self, code: str, message: str | None = None, status: int = 400):
        self.code, self.status = code, status
        self.message = message or MESSAGES.get(code, "The request could not be completed.")
        super().__init__(self.message)


class TradeTestUnavailable(TradeTestError):
    """The bank file is missing or cannot be read."""

    def __init__(self, detail: str = ""):
        super().__init__("unavailable", status=503)
        self.detail = detail


def bad_request(message: str) -> TradeTestError:
    return TradeTestError("bad_request", message, 400)


def base_key() -> bytes:
    """The secret every token key derives from: the environment's, else the machine's id, else one per process.

    A key that changes (a restart without either) turns visitors' saved sets into expired ones."""
    value = os.environ.get(KEY_ENV)
    if value:
        return value.encode()
    try:
        machine = MACHINE_ID.read_bytes().strip()
    except OSError:
        machine = b""
    return machine or _PROCESS_KEY


# Sealed tokens ----------------------------------------------------------------------------------------------------

def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = (hmac.new(key, nonce + j.to_bytes(4, "big"), hashlib.sha256).digest() for j in range(-(-length // 32)))
    return b"".join(blocks)[:length]


class Sealer:
    """Seals and opens JSON payloads under one key. The nonce starts with four bytes naming the key, so a token
    sealed under another key (another bank version, or a changed secret) is recognised as expired rather than forged."""

    def __init__(self, key: bytes):
        self.enc = hmac.new(key, b"enc", hashlib.sha256).digest()
        self.mac = hmac.new(key, b"mac", hashlib.sha256).digest()
        self.key_id = hmac.new(key, b"id", hashlib.sha256).digest()[:4]

    def seal(self, payload: dict) -> str:
        data = json.dumps(payload, separators=(",", ":")).encode()
        nonce = self.key_id + secrets.token_bytes(8)
        stream = _keystream(self.enc, nonce, len(data))
        sealed = (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")
        tag = hmac.new(self.mac, nonce + sealed, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + sealed + tag).rstrip(b"=").decode("ascii")

    def open(self, token, *, limit: int = MAX_CURSOR) -> dict:
        if not isinstance(token, str) or not 0 < len(token) <= limit or not _TOKEN.fullmatch(token):
            raise TradeTestError("bad_token", status=400)
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (binascii.Error, ValueError):
            raise TradeTestError("bad_token", status=400) from None
        if len(raw) < 12 + 2 + 16:
            raise TradeTestError("bad_token", status=400)
        nonce, sealed, tag = raw[:12], raw[12:-16], raw[-16:]
        if not hmac.compare_digest(nonce[:4], self.key_id):
            raise TradeTestError("expired", status=410)
        if not hmac.compare_digest(tag, hmac.new(self.mac, nonce + sealed, hashlib.sha256).digest()[:16]):
            raise TradeTestError("bad_token", status=400)
        stream = _keystream(self.enc, nonce, len(sealed))
        data = (int.from_bytes(sealed, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(sealed), "big")
        try:
            payload = json.loads(data)
        except ValueError:
            raise TradeTestError("bad_token", status=400) from None
        if not isinstance(payload, dict):
            raise TradeTestError("bad_token", status=400)
        return payload


# The bank ---------------------------------------------------------------------------------------------------------

@dataclass
class Bank:
    version: str
    n_context: int
    n_replay: int
    lookback: int
    bars: np.ndarray          # float32 [N, n_context + n_replay, 5]
    closes_before: np.ndarray  # float32 [N, lookback]
    day: np.ndarray           # int32 [N, n_context + n_replay]
    instrument: np.ndarray    # int32 [N]
    year: np.ndarray          # int16 [N]
    listings: dict            # field -> list of str, one per instrument
    meta: dict
    basis: np.ndarray         # float64 [N]: stored price x basis = as traded on the window's last bar
    grid: np.ndarray          # float32 [N, lookback + n_context + n_replay]: one as-traded cent in stored prices

    @property
    def windows(self) -> int:
        return len(self.instrument)


LISTING_FIELDS = {"symbol": "symbols", "name": "names", "kind": "kinds", "sector": "sectors",
                  "industry": "industries", "exchange": "exchanges", "note": "notes"}


def load_bank(path: Path) -> Bank:
    """Read and check the whole bank; TradeTestUnavailable when it is missing or malformed. A bank without the
    as-traded arrays reads as having no splits."""
    try:
        with np.load(path, allow_pickle=False) as data:
            arrays = {name: data[name] for name in data.files}
        meta = json.loads(str(arrays["meta"]))
        n_context, n_replay, lookback = int(meta["n_context"]), int(meta["n_replay"]), int(meta["lookback"])
        bars, before, day = arrays["bars"], arrays["lookback"], arrays["day"]
        instrument, year = arrays["instrument"].astype(np.int64), arrays["year"]
        listings = {field: [str(v) for v in arrays[plural].tolist()] for field, plural in LISTING_FIELDS.items()}
        count = len(instrument)
        basis = arrays["basis"].astype(np.float64) if "basis" in arrays else np.ones(count)
        grid = arrays["grid"] if "grid" in arrays else np.zeros((count, lookback + n_context + n_replay), np.float32)
    except FileNotFoundError:
        raise TradeTestUnavailable(f"No bank at {path}.") from None
    except (OSError, EOFError, zipfile.BadZipFile, ValueError, KeyError, TypeError) as exc:
        raise TradeTestUnavailable(f"The bank at {path} cannot be read: {exc}") from None
    width = n_context + n_replay
    instruments = len(listings["symbol"])
    if (count < CHARTS or n_context < 1 or n_replay < 1 or bars.shape != (count, width, 5)
            or before.shape != (count, lookback) or day.shape != (count, width) or year.shape != (count,)
            or basis.shape != (count,) or grid.shape != (count, lookback + width)
            or any(len(values) != instruments for values in listings.values())
            or instrument.min() < 0 or instrument.max() >= instruments
            or not isinstance(meta.get("version"), str) or not meta["version"]
            or not np.isfinite(bars[:, :, :4]).all() or (bars[:, :, :4] <= 0).any()
            or not np.isfinite(basis).all() or (basis <= 0).any() or not np.isfinite(grid).all() or (grid < 0).any()):
        raise TradeTestUnavailable(f"The bank at {path} is malformed.")
    return Bank(meta["version"], n_context, n_replay, lookback, bars, before, day, instrument, year, listings, meta,
                basis, grid)


def phasors(values: np.ndarray, period: float) -> np.ndarray:
    """exp(2 pi i v / period) for each value (NaN stays NaN)."""
    return np.exp(2j * np.pi * np.mod(np.asarray(values, dtype=np.float64) / period, 1.0))


def lattice_strength(values: np.ndarray, period: float) -> float:
    """How firmly the values sit on multiples of ``period``: the mean of cos(2 pi v / period), floored at 0. It is 1 on
    a clean grid and near 0 for prices that ignore it. It is the grid attack's own score, so it also sees a grid that
    rounding has smeared, or one that most prices keep while a few odd prints do not."""
    return max(0.0, float(phasors(values, period).real.mean()))


def _clean(values) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).ravel()
    return values[np.isfinite(values) & (values > 0)]


def drifting_strength(blocks, period: float) -> float:
    """How firmly each block of values sits on a grid of ``period`` wherever it is shifted to (the length of its mean
    phasor), averaged over the blocks that span SPAN_STEPS of it; 0 when fewer than three do."""
    lengths = [abs(phasors(block, period).mean()) for block in map(_clean, blocks)
               if block.size and block.max() - block.min() >= SPAN_STEPS * period]
    return float(np.mean(lengths)) if len(lengths) >= 3 else 0.0


def price_grid(values: np.ndarray, grids=GRIDS, blocks=None) -> float:
    """The smallest step that is a whole number of every grid the values sit on (the least common multiple of the
    VISIBLE grids; one coarser than a cent counts only when the values span SPAN_STEPS of its steps), or 0. With
    ``blocks`` (stretches of the same values), a grid that only each stretch keeps (DRIFTING) counts too."""
    values = _clean(values)
    if not values.size:
        return 0.0
    spread = float(values.max() - values.min())
    strong = [round(g * 1e5) for g in grids
              if (g <= 0.01 or g * SPAN_STEPS <= spread) and lattice_strength(values, g) >= VISIBLE
              or blocks is not None and drifting_strength(blocks, g) >= DRIFTING]
    return math.lcm(*strong) / 1e5 if strong else 0.0


def residual_grid(values: np.ndarray) -> tuple[float, float]:
    """The strongest grid the values sit on with a period from two cents to 1/SPAN_STEPS of their range, and that
    period: the largest length of the mean phasor over all those periods at once, read off an FFT of the values'
    histogram. It is the grid attack run on every period, so it also finds a grid no list foresees (prices scaled by
    a share conversion the split records miss, say)."""
    values = _clean(values)
    span = float(values.max() - values.min()) if values.size else 0.0
    if span <= 0:
        return 0.0, 0.0
    width = max(0.005, span / 2 ** 19)
    counts = np.bincount(((values - values.min()) / width).astype(np.int64))
    size = 1 << int(np.ceil(np.log2(len(counts) * 2)))          # padded twice over, so no peak falls between bins
    strength = np.abs(np.fft.rfft(counts, size)) / values.size
    frequency = np.fft.rfftfreq(size, d=width)                    # cycles per dollar
    band = (frequency >= SPAN_STEPS / span) & (frequency <= min(50.0, 0.25 / width))
    if not band.any():
        return 0.0, 0.0
    best = int(np.argmax(np.where(band, strength, 0.0)))
    return float(strength[best]), float(1 / frequency[best])


def dither_steps(bars: np.ndarray, before: np.ndarray, cent: np.ndarray) -> tuple[float, np.ndarray]:
    """The two dither widths of a window (stored prices; the closes before it first, as in the bank): the stored grid
    (price_grid, or a millionth of the median price when there is none) and, day by day, the grid of the prices as
    traded, at least a cent, from ``cent`` (one as-traded cent in stored prices; 0 for a bank that lacks it)."""
    cent = np.asarray(cent, dtype=np.float64)
    factor = np.where(cent > 0, 0.01 / np.where(cent > 0, cent, 1.0), 1.0)
    stored = np.concatenate([before, bars[:, :4].ravel()])
    traded_bars = bars[:, :4] * factor[len(before):, None]
    traded = np.concatenate([before * factor[:len(before)], traded_bars.ravel()])
    blocks = [traded_bars[i:i + DRIFT_BARS] for i in range(0, len(bars), DRIFT_BARS)]
    step = price_grid(stored) or 1e-6 * float(np.nanmedian(stored))
    return step, max(price_grid(traded, TRADED_GRIDS, blocks), 0.01) / factor


def dither(bars: np.ndarray, before: np.ndarray, step: float, second: np.ndarray, seed: bytes):
    """Move every price by a random amount, the same for the same seed, then widen the high and low again.

    The amount is uniform within half a ``step`` either way, plus, where a day's ``second`` step (the closes before the
    window first) is another one, uniform within half of that. A uniform as wide as a grid's step, or a whole number
    of steps, leaves no trace of that grid, whatever else is added. A price that would reach zero keeps its value."""
    rng = np.random.default_rng(int.from_bytes(seed, "big"))
    second = np.asarray(second, dtype=np.float64)
    second = np.where(np.isclose(second, step, rtol=1e-5, atol=0), 0.0, second)   # one term clears the same grid
    noise = (rng.random((len(before) + len(bars), 4)) - 0.5) * step + (
        rng.random((len(before) + len(bars), 4)) - 0.5) * second[:, None]
    moved = bars[:, :4] + noise[len(before):]
    prices = np.where(moved > 0, moved, bars[:, :4])
    out = bars.copy()
    out[:, 0], out[:, 3] = prices[:, 0], prices[:, 3]
    out[:, 1], out[:, 2] = prices.max(axis=1), prices.min(axis=1)
    with np.errstate(invalid="ignore"):
        shifted = before + noise[:len(before), 3]
        return out, np.where(shifted > 0, shifted, before)


def dither_volume(volume: np.ndarray, seed: bytes) -> np.ndarray:
    """Volumes moved off their round lots, the same for the same seed: each bar's by its own uniform amount within
    LOT_DITHER shares either way (a bar that traded stays above zero, one that did not stays at zero). A uniform as
    wide as a lot leaves no trace of the lots, so relative volumes no longer give away the shares behind them. (A
    random scale on the median would add nothing: the median of the relative volumes would show it.)"""
    rng = np.random.default_rng(int.from_bytes(seed, "big"))
    lots = rng.uniform(-LOT_DITHER, LOT_DITHER, len(volume))
    return np.where(volume > 0, np.maximum(volume + lots, 1.0), 0.0)


def ema(values: np.ndarray, span: int) -> list[float | None]:
    """Exponential average seeded with the first available value; a missing value carries the average forward."""
    alpha, out, current = 2 / (span + 1), [], None
    for value in values.tolist():
        if not math.isnan(value):
            current = value if current is None else current + alpha * (value - current)
        out.append(current)
    return out


def sma(values: np.ndarray, length: int) -> list[float | None]:
    """Simple average over the last ``length`` values; None until that many consecutive values exist."""
    valid = ~np.isnan(values)
    filled = np.where(valid, values, 0.0)
    sums = np.concatenate([[0.0], np.cumsum(filled)])
    counts = np.concatenate([[0], np.cumsum(valid)])
    out: list[float | None] = [None] * len(values)
    for i in range(length - 1, len(values)):
        if counts[i + 1] - counts[i + 1 - length] == length:
            out[i] = float(sums[i + 1] - sums[i + 1 - length]) / length
    return out


def window_rows(bank: Bank, w: int, key: bytes | None = None) -> tuple[list[list], float]:
    """One window's rows for the browser, [o, h, l, c, relative volume, ema9, ema21, sma50, sma200], and the scale the
    reveal gives: a chart price divided by it is the price as traded at the end of the window.

    With a key, the prices are dithered first (seeded by the key and the window, so every request gets the same rows)
    across every grid they sit on, as stored and as traded (dither_steps). The cent as traded is always cleared:
    stored prices that also carry dividend adjustments drift off any single grid, yet keep one locally. The moving
    averages follow the dithered closes, and the last context close is still exactly 100. The volumes are moved off
    their lots too (dither_volume, under a seed of their own) before they are divided by their context median."""
    bars = bank.bars[w].astype(np.float64)
    before = bank.closes_before[w].astype(np.float64)
    real_last = float(bars[bank.n_context - 1, 3])
    if key is not None:
        step, second = dither_steps(bars, before, bank.grid[w])
        bars, before = dither(bars, before, step, second, hmac.new(key, b"dither|%d" % w, hashlib.sha256).digest())
    closes = np.concatenate([before, bars[:, 3]])
    chart = 100.0 / float(bars[bank.n_context - 1, 3])
    scale = 100.0 / (real_last * float(bank.basis[w]))
    bars[:, 4] = np.nan_to_num(bars[:, 4], nan=0.0)
    if key is not None:
        bars[:, 4] = dither_volume(bars[:, 4], hmac.new(key, b"volume|%d" % w, hashlib.sha256).digest())
    context = bars[:bank.n_context, 4]
    typical = float(np.median(context[context > 0])) if (context > 0).any() else 0.0
    volume = bars[:, 4] / typical if typical > 0 else np.zeros(len(bars))
    skip = bank.lookback
    averages = [ema(closes, 9)[skip:], ema(closes, 21)[skip:], sma(closes, 50)[skip:], sma(closes, 200)[skip:]]
    rows = []
    for i, (o, h, lo, c, _) in enumerate(bars.tolist()):
        row = [round(o * chart, 4), round(h * chart, 4), round(lo * chart, 4), round(c * chart, 4),
               round(float(volume[i]), 3)]
        row += [None if line[i] is None else round(line[i] * chart, 4) for line in averages]
        rows.append(row)
    return rows, scale


class TradeTestService:
    """Sets, bars, steps and reveals over one bank file. The bank loads on first use and stays in memory; a missing
    or unreadable file is retried when it changes on disk. Revealed charts are remembered in memory, so they step no
    further (forgotten on restart, and the oldest go first past REVEALED_KEPT)."""

    def __init__(self, bank_path: str | Path, key: bytes | None = None):
        self.bank_path = Path(bank_path)
        self._key = key
        self._lock = threading.Lock()
        self._bank: Bank | None = None
        self._sealer: Sealer | None = None
        self._secret = b""
        self._failure: tuple[object, TradeTestUnavailable] | None = None
        self._rows = lru_cache(maxsize=256)(self._window_rows)
        self._random = random.SystemRandom()
        self._revealed: dict[int, int] = {}          # set, chart and window -> k at the first reveal; oldest first
        self._revealed_lock = threading.Lock()

    def _signature(self):
        try:
            stat = self.bank_path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def bank(self) -> Bank:
        if self._bank is not None:
            return self._bank
        with self._lock:
            if self._bank is not None:
                return self._bank
            signature = self._signature()
            if self._failure and self._failure[0] == signature:
                raise TradeTestUnavailable(self._failure[1].detail)   # a fresh error, so tracebacks never pile up
            try:
                bank = load_bank(self.bank_path)
            except TradeTestUnavailable as exc:
                self._failure = (signature, exc)
                log.warning("TradeTest is unavailable: %s", exc.detail)
                raise
            key = hashlib.sha256(b"tradetest|" + (self._key or base_key()) + b"|" + bank.version.encode()).digest()
            self._sealer, self._secret, self._bank, self._failure = Sealer(key), key, bank, None
            return bank

    @property
    def available(self) -> bool:
        try:
            self.bank()
        except TradeTestUnavailable:
            return False
        return True

    def _window_rows(self, w: int):
        bank = self.bank()
        return window_rows(bank, w, self._secret)

    # Finished charts ----------------------------------------------------------------------------------------------

    @staticmethod
    def _chart_key(position: dict) -> int:
        # The window is part of the key, so two sets that happen to share an id never finish each other's charts.
        return (int(position["s"], 16) << 28) | (position["i"] << 24) | position["w"]

    def revealed_at(self, position: dict) -> int | None:
        """The k at which this chart was first revealed, or None."""
        with self._revealed_lock:
            return self._revealed.get(self._chart_key(position))

    def _remember_reveal(self, position: dict) -> None:
        key = self._chart_key(position)
        with self._revealed_lock:
            k = self._revealed.pop(key, position["k"])
            self._revealed[key] = min(k, position["k"])
            while len(self._revealed) > REVEALED_KEPT:
                del self._revealed[next(iter(self._revealed))]

    # Tokens -------------------------------------------------------------------------------------------------------

    def _cursor(self, token) -> dict:
        bank = self.bank()
        payload = self._sealer.open(token)
        if payload.get("v") != bank.version:
            raise TradeTestError("expired", status=410)
        s, i, w, k = (payload.get(name) for name in ("s", "i", "w", "k"))
        if (not isinstance(s, str) or not _SET.fullmatch(s) or not _whole(i) or not 0 <= i < CHARTS
                or not _whole(w) or not 0 <= w < min(bank.windows, 1 << 24) or not _whole(k)
                or not 0 <= k <= bank.n_replay):
            raise TradeTestError("bad_token", status=400)
        return payload

    def _seal_cursor(self, cursor: dict, k: int) -> str:
        return self._sealer.seal({"v": cursor["v"], "s": cursor["s"], "i": cursor["i"], "w": cursor["w"], "k": k})

    def _avoided(self, token) -> list[int]:
        """The windows the visitor's recent sets used. A missing, foreign or stale list is simply ignored."""
        if token is None:
            return []
        bank = self.bank()
        try:
            payload = self._sealer.open(token, limit=MAX_AVOID)
        except TradeTestError:
            return []
        ids = payload.get("ids")
        if payload.get("v") != bank.version or not isinstance(ids, list):
            return []
        return [w for w in ids if _whole(w) and 0 <= w < bank.windows][-AVOID_KEPT:]

    # Handlers -----------------------------------------------------------------------------------------------------

    def new_set(self, avoid: str | None = None) -> dict:
        """Ten windows the visitor has not seen lately: distinct instruments, at most two per replay-start year."""
        if avoid is not None and not isinstance(avoid, str):
            raise bad_request("The list of recent charts must be text or null.")
        bank = self.bank()
        seen = self._avoided(avoid)
        pool = sorted(set(range(bank.windows)) - set(seen))
        if len(pool) < MIN_FRESH:
            pool = list(range(bank.windows))
        chosen = self._choose(bank, pool)
        set_id = secrets.token_hex(4)
        charts = [{"cursor": self._sealer.seal({"v": bank.version, "s": set_id, "i": i, "w": w, "k": 0})}
                  for i, w in enumerate(chosen)]
        avoid_ids = [w for w in seen if w not in chosen] + chosen
        return {"version": bank.version, "set": set_id, "n_context": bank.n_context, "n_replay": bank.n_replay,
                "charts": charts, "avoid": self._sealer.seal({"v": bank.version, "ids": avoid_ids[-AVOID_KEPT:]})}

    def _choose(self, bank: Bank, pool: list[int]) -> list[int]:
        order = list(pool)
        self._random.shuffle(order)
        chosen, symbols, years = [], set(), {}
        for w in order:
            symbol, year = bank.listings["symbol"][bank.instrument[w]], int(bank.year[w])
            if symbol in symbols or years.get(year, 0) >= MAX_PER_YEAR:
                continue
            chosen.append(w)
            symbols.add(symbol)
            years[year] = years.get(year, 0) + 1
            if len(chosen) == CHARTS:
                return chosen
        # A small bank cannot meet both rules: relax the year cap, then distinct instruments, never repeating a window.
        for relaxed in (lambda w: bank.listings["symbol"][bank.instrument[w]] not in symbols, lambda w: True):
            for w in order:
                if len(chosen) == CHARTS:
                    return chosen
                if w not in chosen and relaxed(w):
                    chosen.append(w)
                    symbols.add(bank.listings["symbol"][bank.instrument[w]])
        return chosen

    def bars(self, cursor: str) -> dict:
        """The context and every replay bar revealed so far."""
        position = self._cursor(cursor)
        bank = self.bank()
        rows, _ = self._rows(position["w"])
        return {"k": position["k"], "n_context": bank.n_context, "n_replay": bank.n_replay,
                "bars": rows[:bank.n_context + position["k"]], "cursor": cursor}

    def step(self, cursor: str, n) -> dict:
        """Reveal the next ``n`` replay bars (1 to 20); at the end of the replay nothing more is revealed. A chart
        that has been revealed is finished: 409 for any of its cursors."""
        if not _whole(n) or not 1 <= n <= MAX_STEP:
            raise bad_request(f"Step forward by a whole number of bars from 1 to {MAX_STEP}.")
        position = self._cursor(cursor)
        if self.revealed_at(position) is not None:
            raise TradeTestError("finished", status=409)
        bank = self.bank()
        k = position["k"]
        new_k = min(k + n, bank.n_replay)
        rows, _ = self._rows(position["w"])
        return {"k": new_k, "bars": rows[bank.n_context + k:bank.n_context + new_k],
                "cursor": cursor if new_k == k else self._seal_cursor(position, new_k), "done": new_k == bank.n_replay}

    def reveal(self, cursor: str) -> dict:
        """The instrument, the dates and the bars after the cursor, as the chart is finished. The cursor itself does
        not move and keeps working for bars and reveal, but the chart steps no further."""
        position = self._cursor(cursor)
        bank = self.bank()
        w, k = position["w"], position["k"]
        rows, scale = self._rows(w)
        self._remember_reveal(position)
        listing = {field: bank.listings[field][bank.instrument[w]] for field in LISTING_FIELDS}
        dates = [str(day) for day in bank.day[w].astype("datetime64[D]")]
        return {"k": k, **listing, "dates": dates, "scale": scale, "start": dates[bank.n_context], "end": dates[-1],
                "rest": rows[bank.n_context + k:]}


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
