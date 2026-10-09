"""TradeTest's window bank (``stratlib tradetest-bank``): random stretches of daily bars for the blind replay.

Each window is N_CONTEXT bars of history and N_REPLAY bars to trade forward, 250 consecutive SPY sessions, plus the
LOOKBACK closes before it for the moving averages. Windows are drawn at random from US common stocks (today's and
those in the delisted-company directory) and a list of plain, unleveraged ETFs: 85% stocks and 15% ETFs (fewer when
the ETFs run out of windows), a replay-start year chosen uniformly, then an instrument with data that year, then a
start within it. Windows on one instrument never share a session.

A window is kept only when its bars and the closes before it are complete and clean, the stock is liquid and was not a
penny stock at the prices it traded at then, its history is one security, and it avoids the market events a visitor
would recognise at a glance (CRASHES and SHOCKS). Stored prices are split-adjusted; the database's split records turn
them back into prices as traded, and the reveal quotes those. A record the stored prices contradict (they jump by its
inverse ratio on or near its date, so they were never adjusted for it) is left out and no window crosses the jump; a
record whose date shows some other jump leaves the prices before it unknown; a split document known to be wrong keeps
its symbol out (BAD_SPLITS). Prices as traded sit on whole cents, so a window whose prices as traded miss them while
twice or three times them do (or that sit on even cents, so half of them does) has a split its records miss, or one that
never happened, and is left out too (MISSED). Stored prices adjusted for a separation or special dividend that no split
record carries make the prices as traded before it unknown as well: the database's dividend documents list most of them
(a dividend worth a good part of the price that the stored closes do not fall by), and the rest are listed by hand
(UNRECORDED); no window starts before one. Splits, separations and bad prints no record explains are found in the prices
themselves (an opening gap at a split ratio on a calm bar that holds, or a big one on thin volume; a one-bar spike the
next session undoes on ordinary volume), and no window crosses one either; nor does a window show a bad open, high or
low on an otherwise calm day.

A ticker that passed from one company to another shows as a splice (a big one-day jump with a step change in volume):
no window crosses one, and a window before the last splice of today's ticker belongs to an older company, so it is
named from the delisted directory or dropped. A ticker change the prices do not show is found another way: the
database then holds one series under two tickers, perhaps adjusted differently, so their daily returns match. Only the
ticker that carries the series on after the shared stretch keeps those windows, when the two tickers' split documents
agree on the prices as traded, and they are named from the other ticker's directory record when it has one: a
company's chart is in the bank once, under the name it traded under then, and a reused ticker never lends the new
company's name to the old one's prices. A company formed by a merger or a
separation after the start of the history its ticker carries has no window from before it was formed (FORMED), unless
the company the prices belonged to then is known (PREDECESSORS).
Windows whose prices draw a staircase, or whose price level the web app's dither could not hide without moving prices
visibly, are left out as well (MAX_TICK, MAX_DITHER, RESIDUAL).

The bank is one compressed numpy file, read whole by the web app (tradetest.py). The database is opened read-only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .scoring import number, split_factor
from .tradetest import GRIDS, SPAN_STEPS, VISIBLE, dither, dither_steps, phasors, residual_grid
from .universe import exclusion_reason

log = logging.getLogger("stratlib.tradetest")

N_CONTEXT, N_REPLAY, LOOKBACK = 150, 100, 199
WINDOW = N_CONTEXT + N_REPLAY
SPAN = LOOKBACK + WINDOW                  # what the browser sees, through the bars and the moving averages
CALENDAR = "SPY"
ETF_SHARE = 0.15
EXCHANGES = ("NYSE", "NASDAQ", "AMEX")
DELISTED = "backtest:delisted"
PROFILE = "backtest:approx:profile:"
SPLITS = "backtest:splits:"
# Periods a visitor would recognise from the shape of the chart alone. The crashes block every window they touch,
# context included. The short shocks block a window only when they fall in its replay, except for a broad index ETF
# (INDEX_ETFS), where the chart is the market itself: there they block the whole window.
CRASHES = (("2008-09-15", "2008-11-21"),   # Lehman and the 2008 crash
           ("2020-02-20", "2020-04-30"),   # COVID
           ("2025-04-02", "2025-04-14"))   # the April 2025 tariffs
SHOCKS = (("2010-05-06", "2010-05-07"),    # the flash crash
          ("2015-08-21", "2015-08-26"), ("2016-06-23", "2016-06-27"), ("2016-11-08", "2016-11-10"),
          ("2018-02-02", "2018-02-08"), ("2018-12-20", "2018-12-27"), ("2022-02-24", "2022-03-08"),
          ("2023-03-09", "2023-03-15"), ("2024-08-01", "2024-08-07"), ("2025-01-27", "2025-01-28"))
EVENTS = CRASHES + SHOCKS
FRESH = "2025-10-07"                      # everything from here on is too fresh in memory
INDEX_ETFS = {"SPY", "QQQ", "DIA", "IWM", "MDY", "IJR", "VTI"}
# As traded: the last context close and the lowest close of the window, the highest believable last close (above it
# the split records are wrong; BRK-A really trades there) and the junk prints a ticker leaves after a buyout.
MIN_LAST_CLOSE, MIN_CLOSE, MAX_LAST_CLOSE, MIN_PRINT = 5.0, 2.0, 20_000.0, 0.05
HIGH_PRICED = {"BRK-A"}
MIN_DOLLAR_VOLUME = {"stock": 10e6, "etf": 2e6}
MAX_ZERO_VOLUME, MAX_FLAT, MAX_MOVE = 3, 5, 0.60
# A volume under PLACEHOLDER of the window's median volume is a placeholder, not a trade count (some old series carry
# 100 shares a day for months), so it counts as none.
PLACEHOLDER = 0.001
SPLICE_RATIO, SPLICE_VOLUME, SPLICE_SESSIONS = 2.5, 20.0, 20
# A split record is checked against the stored close on its date: a move of about its inverse ratio (within
# CONTRADICTED of the ratio's size, for a ratio beyond MIN_SPLIT_MOVE in log) means the stored prices were never
# adjusted for it; any other move beyond UNEXPLAINED in log means they were adjusted for something else as well.
MIN_SPLIT_MOVE, CONTRADICTED, UNEXPLAINED = 0.05, 0.35, 0.25
# The stored prices of a record of NEAR_SPLIT or more (in log: a 5-for-4 and up) can jump a few sessions off its date
# (South Jersey Industries' halve on 2015-05-08, its 2-for-1 is dated 2015-05-11): a move of about the inverse ratio
# within NEAR_SESSIONS of the date contradicts the record too, when the move on the date says nothing.
NEAR_SPLIT, NEAR_SESSIONS = math.log(1.25), 3
# Split documents checked by hand and found wrong: splits that never happened (10-for-1 at ON, KMI, PNW, HUN, COR and
# ENS; 1-for-10 at COL; 14-for-1 at MT in 2014; ES in 2013, J in 2017, MDP and SIR in 2021, AIV's 4-for-5 in 2011,
# AXIA's in 2025), made-up ratios (GEN, SNY, RYAAY, CPA, KRC, BKD, GRUB, GOL's depositary ratios, and WT's with its old
# ticker WETF), another company's records on the series (CB, JCI, GL, BFH), another company's merger ratio (FTI in 2017,
# WCN in 2016, DBRG's Colony ratio in 2017), share consolidations the stored prices offset with a separation (SKM in
# 2021, GSK in 2022, SSP in 2015 and 2025) and stored prices at half the prices as traded all through, under records
# that say nothing of it (AET: Aetna's last close, about $212 in the CVS deal, is stored at $106; rounded to cents, so
# MISSED cannot see it), or records missing whole splits that the cent grid only partly sees (FMD's 3:2 in 2006 and its
# later 1-for-10). MTCH's history before the 2020 separation joins old IAC's prices to Match Group's own (from its
# 2015 listing) under old IAC's conversion ratio, which is wrong for Match's. The stored prices and volumes run as
# smoothly through these as through real splits, so nothing in the database tells them apart (not the volumes, which are
# split-adjusted too, nor the database's split calendar, which repeats them): no window of the symbol, or none that
# starts before the date given.
BAD_SPLITS = {**{symbol: None for symbol in ("AET", "BFH", "BKD", "CB", "COL", "COR", "CPA", "ENS", "FMD", "GEN",
                                             "GL", "GOL", "GRUB", "HUN", "JCI", "KMI", "KRC", "MDP", "ON", "PNW",
                                             "RYAAY", "SIR", "SKM", "SNY", "WETF", "WT")},
              "AIV": "2011-06-09", "AXIA": "2025-12-30", "DBRG": "2017-01-11", "ES": "2013-04-30", "FTI": "2017-01-18",
              "GSK": "2022-07-22", "J": "2017-02-15", "MT": "2014-10-02", "MTCH": "2020-07-01", "SSP": "2025-12-08",
              "WCN": "2016-06-01"}
# Separations, special dividends and splits the stored prices are adjusted for that no split record or dividend
# document carries, found by hand (OA's Vista Outdoor separation, SM's 2:1 before March 2020): the prices as traded
# before them are unknown, so no window of the symbol starts before.
UNRECORDED = {"AROC": "2015-11-04", "BKR": "2017-07-05", "KDP": "2018-07-09", "MDLZ": "2012-10-02", "MMM": "2024-04-01",
              "NVS": "2023-10-04", "OA": "2015-02-10", "OVV": "2009-12-01", "SM": "2020-03-09", "TWX": "2009-12-10"}
# The dividend documents list separations and special dividends too (in stored prices, mostly). A dividend worth more
# than BIG_DIVIDEND of the stored close before it takes at least that much off the close as traded on its date; stored
# closes that fall by less than half of that were adjusted for it, so the prices as traded before it are unknown, unless
# a split record within RECORD_DAYS days raises the factor before it by at least half as much (the separation recorded
# as a split, which the factor then carries).
DIVIDENDS = "research:nash:div:"
BIG_DIVIDEND, RECORD_DAYS = 0.10, 7
# Companies formed by a merger or a separation whose ticker carries an older company's prices from before (Alcoa's
# under AA, Coca-Cola Enterprises' under CCEP, CNH Global's under CNH, Crane Holdings' under CR, Praxair's under LIN,
# WWE's under TKO, old IAC/InterActiveCorp's under IAC and PPLI, its spun-off successor; and the companies merged into
# AROC's, BKR's and DBRG's), with nothing in the database to date them: no window of theirs starts before, and no name
# of theirs is given to an earlier window, unless PREDECESSORS knows the company the prices were then.
FORMED = {"AA": "2016-11-01", "AROC": "2007-08-20", "BKR": "2017-07-03", "CCEP": "2016-05-28", "CNH": "2013-09-29",
          "CR": "2023-04-03", "DBRG": "2017-01-10", "IAC": "2020-07-01", "LIN": "2018-10-31", "PPLI": "2020-07-01",
          "TKO": "2023-09-12"}
# The company a ticker's prices were before a date, when it is known: (date, symbol, name, exchange). A window that
# ends before the date is named after it; one that spans the date shows two companies, and is dropped. PPLI (the IAC
# spun off in 2020, under its later name) holds old IAC/InterActiveCorp's whole history up to the separation, at the
# right prices as traded; IAC's ticker holds the same copy, and MTCH only a part of it (BAD_SPLITS).
PREDECESSORS = {"CNH": ("2013-09-29", "CNH", "CNH Global N.V.", "NYSE"),
                "PPLI": ("2020-07-01", "IAC", "IAC/InterActiveCorp", "NASDAQ")}
# Delisted-directory records that carry the wrong company's name: GRUB's names Grubhub's acquirer.
RENAMED = {"GRUB": "Grubhub Inc."}
# A split the stored prices never adjusted for, and no record explains: the bar opens within GAP_MATCH (in log) of a
# split ratio from the close before, trades a calm range (under CALM_RANGE of its close), and the next SETTLE_BARS
# closes stay within SETTLED of its close (or a third of the gap, when that is more); or it opens THIN_GAP or more away
# on a calm range and on less than the median volume of the PRINT_SESSIONS bars before (a share conversion: news moves
# a price that far only on heavy trading). A bad print: a close more than PRINT_MOVE (in log) from the one before that
# the next close undoes (within PRINT_UNDONE), where the next bar already opens back near the close before the spike
# (within PRINT_REOPEN) and the spike traded no more than PRINT_VOLUME times the median volume of the PRINT_SESSIONS
# bars before it (news trades heavily, or takes a session to undo); from CLOSE_PRINT up, when the whole move is the
# close: the bar opened within CALM_CLOSE of the close before and closed at its high or low (a crash day's real swing
# gaps at the open).
SPLIT_GAPS = np.log([1 / 2, 2 / 3, 1 / 3, 3 / 4, 1 / 4, 2, 3 / 2, 3, 4 / 3, 4])
GAP_MATCH, CALM_RANGE, SETTLED, SETTLE_BARS, THIN_GAP = 0.025, 0.06, 0.10, 5, math.log(1.25)
PRINT_MOVE, PRINT_UNDONE, PRINT_REOPEN, PRINT_VOLUME, PRINT_SESSIONS = 0.25, 0.05, 0.05, 3.0, 20
CLOSE_PRINT = 0.15
# A bad bar: a high or low more than WICK_PRINT (in log) outside the bar's open and close while the next bar opens
# within CALM_CLOSE of its close, or an open more than OPEN_PRINT from the close before that is the bar's high or low;
# either on a day whose close stays within CALM_CLOSE of the close before, on under BAR_VOLUME times the median volume
# (news days trade more, and their wild bars are real). A stop or a fill would trigger at a price that never traded.
# The market-wide days of real wild prints (WILD_DAYS: the January 2008 sell-off, the flash crash, the August 2015 open,
# the January 2021 squeezes) are left alone.
WICK_PRINT, OPEN_PRINT, CALM_CLOSE, BAR_VOLUME = 0.12, 0.10, 0.04, 2.5
WILD_DAYS = ("2008-01-22", "2008-01-23", "2010-05-06", "2015-08-24", "2021-01-27", "2021-01-28")
# A stored-price grid (found as tradetest.price_grid finds it) whose step is more than MAX_TICK of the lowest close
# draws staircases: cents on a heavily split stock's adjusted price, or nickels on a cheap one. The web app's dither
# clears that grid and the grids of the prices as traded (tradetest.dither_steps); a sampled window whose dither could
# move a price by more than MAX_DITHER of it (the report tells visitors at most about 0.3%), or whose dithered prices
# still sit on a grid RESIDUAL firmly, is drawn again.
MAX_TICK, MAX_DITHER, RESIDUAL = 0.0025, 0.003, 0.25
# Prices as traded sit on whole cents (the database's prices begin in 2002, after decimalisation). A window whose prices
# as traded (stored x factor, at the database's full precision, over the open, high, low and close of every bar) sit on
# cents less firmly than OFF_CENTS (tradetest.lattice_strength's score) while MISSED times them sit there firmly
# (ON_CENTS or more) has a split its records miss: Celgene's 2-for-1 of 2014, which no record carries, leaves its 2010
# prices on half cents. One whose prices sit firmly on a coarser grid, so that a multiple under 1 of them sits on cents,
# has a reverse split they miss, or a split that never happened: FuelCell Energy's 1-for-30 of 2024, which no record
# carries, leaves its 2021 prices on steps of 30 cents. Shares that trade at COARSE_PRICE or more are often quoted in
# whole dollars, though, so there prices that sit on nickels too (OFF_CENTS or more) are let be (Berkshire's B shares
# near $3,000 before 2010). Rounding explains either on the days when the stored prices sit on cents and the factor
# times the multiple is a whole number, so only the other days count then: the database rounds the prices it adjusts to
# cents, so stored cents times the 2.5 of a 2-for-1 and a 5-for-4 sit on half cents as traded (Brown-Forman's $96.90 in
# 2015, stored at $38.76), and that is no missed split. Prices adjusted for dividends sit on no grid at all and say
# nothing. The whole window is judged, and its replay on its own: the reveal quotes prices as traded on the last day,
# which a split inside the window that no record carries leaves right, but such a split can mix the window's grids and
# hide a wrong one in the replay (First Marblehead's 3-for-2 of 2006, before a 1-for-10 no record carries either).
# A factor times the multiple within WHOLE of a whole number counts as one: stock dividends leave a factor a little off
# (Aimco's 1859:200, 807:1000 and 969:1000 make 7.5011, times 4 30.0044), and stored cents times it sit near cents by
# chance over a narrow range of prices, which says nothing.
MISSED = (2.0, 3.0, 4.0, 1.5, 1 / 2, 1 / 3, 1 / 4, 2 / 3)
OFF_CENTS, ON_CENTS, COARSE_PRICE, WHOLE = 0.3, 0.8, 1000.0, 0.01
# One series under two tickers: every PROBE_EVERY sessions, the PROBE_RETURNS daily returns up to that session (a
# probe whose returns add up to less than PROBE_MOVE says nothing). Two tickers printed the same there when every
# return agrees within what cent rounding of their closes explains, plus RETURN_SLACK, so a copy adjusted differently
# for splits, spin-offs or dividends matches too; the search allows SEARCH_ROUNDING for the other ticker's rounding. A
# stretch is shared when a single miss at most breaks the run of probes; where two copies part, a difference in
# returns under PART_MOVE is a dividend, not another company. The two tickers' split documents must then agree on the
# window's prices as traded (within AGREE), or one of them is wrong.
PROBE_EVERY, PROBE_RETURNS, PROBE_MOVE, RETURN_SLACK, SEARCH_ROUNDING = 25, 5, 0.01, 1e-4, 0.002
MIN_SHARED, PART_MOVE, AGREE = 3, 0.02, 0.01
SHELL = "Shell Companies"                 # blank-check companies sit near $10 for months: nothing to read
# The ETFs, all plain and unleveraged: name and the sector the reveal gives.
ETFS = {
    "SPY": ("SPDR S&P 500 ETF Trust", "Index"), "QQQ": ("Invesco QQQ Trust", "Index"),
    "DIA": ("SPDR Dow Jones Industrial Average ETF Trust", "Index"), "IWM": ("iShares Russell 2000 ETF", "Index"),
    "MDY": ("SPDR S&P MidCap 400 ETF Trust", "Index"), "IJR": ("iShares Core S&P Small-Cap ETF", "Index"),
    "VTI": ("Vanguard Total Stock Market ETF", "Index"),
    "EEM": ("iShares MSCI Emerging Markets ETF", "International"), "EFA": ("iShares MSCI EAFE ETF", "International"),
    "EWJ": ("iShares MSCI Japan ETF", "International"), "EWZ": ("iShares MSCI Brazil ETF", "International"),
    "FXI": ("iShares China Large-Cap ETF", "International"),
    "GLD": ("SPDR Gold Shares", "Commodities"), "SLV": ("iShares Silver Trust", "Commodities"),
    "PPLT": ("abrdn Platinum ETF", "Commodities"), "USO": ("United States Oil Fund", "Commodities"),
    "UNG": ("United States Natural Gas Fund", "Commodities"), "DBA": ("Invesco DB Agriculture Fund", "Commodities"),
    "DBC": ("Invesco DB Commodity Index Tracking Fund", "Commodities"),
    "TLT": ("iShares 20+ Year Treasury Bond ETF", "Bonds"), "IEF": ("iShares 7-10 Year Treasury Bond ETF", "Bonds"),
    "HYG": ("iShares iBoxx $ High Yield Corporate Bond ETF", "Bonds"),
    "LQD": ("iShares iBoxx $ Investment Grade Corporate Bond ETF", "Bonds"),
    "VNQ": ("Vanguard Real Estate ETF", "Real estate"),
    "GDX": ("VanEck Gold Miners ETF", "Industry"), "GDXJ": ("VanEck Junior Gold Miners ETF", "Industry"),
    "IBB": ("iShares Biotechnology ETF", "Industry"), "XBI": ("SPDR S&P Biotech ETF", "Industry"),
    "SMH": ("VanEck Semiconductor ETF", "Industry"), "KRE": ("SPDR S&P Regional Banking ETF", "Industry"),
    "XOP": ("SPDR S&P Oil & Gas Exploration & Production ETF", "Industry"),
    "XHB": ("SPDR S&P Homebuilders ETF", "Industry"), "XRT": ("SPDR S&P Retail ETF", "Industry"),
    **{symbol: (f"{sector} Select Sector SPDR Fund", "Sector ETF") for symbol, sector in (
        ("XLB", "Materials"), ("XLC", "Communication Services"), ("XLE", "Energy"), ("XLF", "Financial"),
        ("XLI", "Industrial"), ("XLK", "Technology"), ("XLP", "Consumer Staples"), ("XLRE", "Real Estate"),
        ("XLU", "Utilities"), ("XLV", "Health Care"), ("XLY", "Consumer Discretionary"))},
}
NASDAQ_ETFS = {"QQQ", "IBB", "SMH", "TLT", "IEF"}
HISTORIES_KEPT = 256
# Listing fields and the bank arrays that hold them, one entry per instrument.
FIELDS = {"symbol": "symbols", "name": "names", "kind": "kinds", "sector": "sectors", "industry": "industries",
          "exchange": "exchanges", "note": "notes"}
# The window checks in the order a rejected start is counted against them (liquidity last: it is the costly one).
CHECKS = ("calendar", "bars", "lookback", "splice", "unadjusted split", "unrecorded split", "glitch", "bad print",
          "bad bar", "junk", "split documents", "unrecorded adjustment", "split records", "cent grid", "price",
          "quantisation", "zero volume", "flat bars", "liquidity")


class BankError(ValueError):
    """The bank cannot be built."""


@dataclass(frozen=True)
class Listing:
    """What the reveal says about an instrument."""
    symbol: str
    name: str
    kind: str          # stock | etf
    sector: str = ""
    industry: str = ""
    exchange: str = ""
    note: str = ""


@dataclass(frozen=True)
class Record:
    """One listing in the delisted-company directory; ``listing`` is None for a fund, warrant or the like."""
    ipo: int           # day numbers
    delisted: int
    listing: Listing | None


def etf_listing(symbol: str) -> Listing:
    name, sector = ETFS[symbol]
    return Listing(symbol, name, "etf", sector, exchange="NASDAQ" if symbol in NASDAQ_ETFS else "NYSE Arca")


def day_numbers(dates) -> np.ndarray:
    """ISO dates as days since 1970-01-01."""
    return np.asarray(dates, dtype="datetime64[D]").astype(np.int64)


def iso(day: int) -> str:
    return str(np.datetime64(int(day), "D"))


def formed_day(symbol: str) -> int:
    """The day FORMED gives for the symbol's company, or the earliest day there is."""
    formed = FORMED.get(symbol)
    return int(day_numbers([formed])[0]) if formed else int(np.iinfo(np.int64).min)


def event_scope(symbol: str) -> str:
    """Where the short shocks may not fall in a window of the instrument: its replay, or anywhere for an index ETF."""
    return "window" if symbol in INDEX_ETFS else "replay"


def _counts(flags: np.ndarray, first: np.ndarray, width: int, dtype=np.int64) -> np.ndarray:
    """The sum of ``flags`` over positions [first, first + width) for each entry of ``first``, clipped to the array."""
    total = np.concatenate([[0], np.cumsum(flags, dtype=dtype)])
    return total[np.clip(first + width, 0, len(flags))] - total[np.clip(first, 0, len(flags))]


def _span_extreme(window: np.ndarray, closes: np.ndarray, valid: np.ndarray, pick) -> np.ndarray:
    """For each window start, ``pick`` (np.max or np.min) over the window's values and the LOOKBACK closes before it
    (a start without a full lookback gets the window's value alone: it fails the lookback check anyway)."""
    empty = -np.inf if pick is np.max else np.inf
    out = pick(sliding_window_view(np.where(valid, window, empty), WINDOW), axis=1)
    before = pick(sliding_window_view(np.where(valid, closes, empty), LOOKBACK), axis=1)[:max(0, len(out) - LOOKBACK)]
    out[LOOKBACK:] = pick(np.stack([out[LOOKBACK:], before]), axis=0)
    return out


def _within(days: np.ndarray, periods) -> np.ndarray:
    inside = np.zeros(len(days), dtype=bool)
    for first, last in periods:
        a, b = day_numbers([first, last])
        inside |= (days >= a) & (days <= b)
    return inside


def calendar_ok(days: np.ndarray, scope: str = "replay") -> np.ndarray:
    """For each window start on the calendar, whether the window clears the crashes and the fresh period anywhere in
    it, and the short shocks in its replay (scope "replay") or anywhere in it (scope "window", an index ETF's)."""
    starts = np.arange(max(0, len(days) - WINDOW + 1))
    whole = (days >= day_numbers([FRESH])[0]) | _within(days, CRASHES)
    shocks = _within(days, SHOCKS)
    if scope == "window":
        return _counts(whole | shocks, starts, WINDOW) == 0
    return (_counts(whole, starts, WINDOW) == 0) & (_counts(shocks, starts + N_CONTEXT, N_REPLAY) == 0)


def repair(bars: np.ndarray) -> np.ndarray:
    """Calendar-aligned [sessions, 5] bars with the high and low widened to cover the open and close."""
    out = bars.astype(np.float64, copy=True)
    prices = out[:, :4]
    with np.errstate(invalid="ignore"):
        out[:, 1], out[:, 2] = prices.max(axis=1), prices.min(axis=1)
    return out


def split_factors(days: np.ndarray, splits: list[dict], as_of: int,
                  closes: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(factor, unadjusted). For each day, what turns a stored (split-adjusted) price into the price as traded then:
    the product of the splits after it, up to ``as_of`` (the day the stored prices were adjusted to); all NaN when a
    split record cannot be read. With the stored ``closes`` (calendar-aligned), each record is checked against the
    stored move on its date: one the move contradicts (about its inverse ratio, then or within NEAR_SESSIONS) is left
    out, and one the move does not explain (beyond UNEXPLAINED) leaves the factor before it unknown (NaN).
    ``unadjusted`` holds the calendar positions of both kinds of jump: no window may cross one."""
    factor = np.ones(len(days))
    if split_factor(splits, date.min, date.max) is None:
        return np.full(len(days), np.nan), np.zeros(0, dtype=np.int64)
    valid = np.zeros(0, dtype=np.int64) if closes is None else np.flatnonzero(np.isfinite(closes) & (closes > 0))
    jumps = set()
    for split in splits:
        day = day_numbers([split["date"]])[0]
        if day > as_of:
            continue
        ratio = number(split["numerator"]) / number(split["denominator"])
        at = int(np.searchsorted(valid, np.searchsorted(days, day)))    # the first stored close on or after the date
        if 0 < at < len(valid):
            t, size = int(valid[at]), abs(math.log(ratio))
            move = math.log(closes[t] / closes[valid[at - 1]])
            if size > MIN_SPLIT_MOVE and abs(move + math.log(ratio)) < CONTRADICTED * size:
                jumps.add(t)                     # never adjusted for: the prices before it are as traded already
                continue
            if abs(move) > UNEXPLAINED:
                jumps.add(t)
                factor[days < day] = np.nan      # adjusted for something else too: as traded before it, unknown
                continue
            if size >= NEAR_SPLIT:
                # The stored prices jump by about its inverse ratio a session or a few off its date: never adjusted.
                around = sorted(range(max(1, at - NEAR_SESSIONS), min(len(valid), at + NEAR_SESSIONS + 1)),
                                key=lambda k: abs(k - at))
                moves = [math.log(closes[valid[k]] / closes[valid[k - 1]]) for k in around]
                near = [k for k, m in zip(around, moves) if abs(m + math.log(ratio)) < CONTRADICTED * size]
                if near:
                    jumps.add(int(valid[near[0]]))
                    continue
        factor[days < day] *= ratio
    return factor, np.asarray(sorted(jumps), dtype=np.int64)


def dividend_adjustments(days: np.ndarray, dividends, as_of: int, closes: np.ndarray, splits=()) -> np.ndarray:
    """For each day, whether the stored prices then carry an adjustment for a later dividend that no split record
    explains: one worth more than BIG_DIVIDEND of the stored close before its date that the stored closes (``closes``,
    calendar-aligned) do not fall by, at least by half, and that no split record within RECORD_DAYS days of it raises
    the factor by half as much. ``dividends`` maps ISO dates to amounts per share; those after ``as_of`` are not in the
    stored prices yet."""
    adjusted = np.zeros(len(days), dtype=bool)
    valid = np.flatnonzero(np.isfinite(closes) & (closes > 0))
    records = []
    for split in splits if isinstance(splits, list) else []:
        try:
            ratio = number(split["numerator"]) / number(split["denominator"])
            records.append((int(day_numbers([split["date"]])[0]), ratio))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    for when, amount in (dividends.items() if isinstance(dividends, dict) else ()):
        try:
            day, amount = int(day_numbers([when])[0]), float(amount)
        except (TypeError, ValueError):
            continue
        if not amount > 0 or day > as_of:
            continue
        at = int(np.searchsorted(valid, np.searchsorted(days, day)))     # the first stored close on or after the date
        if not 0 < at < len(valid):
            continue
        before, after = closes[valid[at - 1]], closes[valid[at]]
        share = amount / before
        if share <= BIG_DIVIDEND:
            continue
        size = -math.log(1 - min(share, 0.95))                           # what it takes off the close, in log
        if share < 1 and math.log(after / before) <= -size / 2:
            continue                                                     # the stored closes fall by it: as traded
        if any(abs(r - day) <= RECORD_DAYS and ratio > 1 and math.log(ratio) >= size / 2 for r, ratio in records):
            continue                                                     # recorded as a split, so the factor has it
        adjusted |= days < day
    return adjusted


def splice_points(bars: np.ndarray) -> np.ndarray:
    """Calendar positions where the history passes to another security: the first bar after a one-day close ratio of
    at least SPLICE_RATIO (or at most its inverse) that comes with a SPLICE_VOLUME-fold change in median volume over
    the SPLICE_SESSIONS bars either side. Splits are already adjusted out, so a real company rarely does this."""
    at = np.flatnonzero(np.isfinite(bars[:, 3]) & (bars[:, 3] > 0))
    if at.size < 2:
        return np.zeros(0, dtype=np.int64)
    closes, volume = bars[at, 3], np.nan_to_num(bars[at, 4], nan=0.0)
    ratio = closes[1:] / closes[:-1]
    found = []
    for t in np.flatnonzero((ratio >= SPLICE_RATIO) | (ratio <= 1 / SPLICE_RATIO)) + 1:
        before = max(float(np.median(volume[max(0, t - SPLICE_SESSIONS):t])), 1.0)
        after = max(float(np.median(volume[t:t + SPLICE_SESSIONS])), 1.0)
        if max(before / after, after / before) >= SPLICE_VOLUME:
            found.append(at[t])
    return np.asarray(found, dtype=np.int64)


def unrecorded_splits(bars: np.ndarray) -> np.ndarray:
    """For each calendar position, whether its bar shows a split, reverse split, separation or share conversion that
    the stored prices never adjusted for: it trades a calm range and either opens at a split ratio from the close before
    (SPLIT_GAPS, within GAP_MATCH) and holds the new level for SETTLE_BARS closes, or opens THIN_GAP or more away from
    it on less than the median volume of the PRINT_SESSIONS bars before."""
    o, h, lo, c = bars[:, 0], bars[:, 1], bars[:, 2], bars[:, 3]
    v = np.nan_to_num(bars[:, 4], nan=0.0)
    found = np.zeros(len(bars), dtype=bool)
    if len(bars) < 2:
        return found
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = np.log(o[1:] / c[:-1])                                          # into bars 1, 2, ...
        near = (np.abs(gap[:, None] - SPLIT_GAPS[None, :]) < GAP_MATCH).any(axis=1)
        calm = (h[1:] - lo[1:]) / c[1:] < CALM_RANGE
        later = sliding_window_view(np.concatenate([c, np.full(SETTLE_BARS, np.nan)]), SETTLE_BARS)[2:len(c) + 1]
        held = (np.abs(np.log(later / c[1:, None])) < np.maximum(SETTLED, np.abs(gap) / 3)[:, None]).all(axis=1)
        wide = calm & (np.abs(gap) >= THIN_GAP)
    found[1:] = near & calm & held
    for t in np.flatnonzero(wide) + 1:
        found[t] |= v[t] < float(np.median(v[max(0, t - PRINT_SESSIONS):t]))
    return found


def bad_prints(bars: np.ndarray) -> np.ndarray:
    """For each calendar position, whether its close is a bad print: a one-bar spike that the next close undoes, the
    next bar opening back where the bar before the spike closed, on no more than ordinary volume (PRINT_*); a smaller
    one (CLOSE_PRINT) only when the bar opened flat and closed at its high or low."""
    o, h, lo, c = bars[:, 0], bars[:, 1], bars[:, 2], bars[:, 3]
    v = np.nan_to_num(bars[:, 4], nan=0.0)
    found = np.zeros(len(bars), dtype=bool)
    if len(bars) < 3:
        return found
    with np.errstate(invalid="ignore", divide="ignore"):
        move = np.log(c[1:] / c[:-1])                                         # into bars 1, 2, ...
        size = np.abs(move[:-1])                                              # bars 1 .. n - 2
        undone = np.abs(move[:-1] + move[1:]) < PRINT_UNDONE
        flat = np.abs(np.log(o[1:-1] / c[:-2])) < CALM_CLOSE
        extreme = np.isclose(c[1:-1], h[1:-1], rtol=1e-9, atol=0) | np.isclose(c[1:-1], lo[1:-1], rtol=1e-9, atol=0)
        spike = undone & ((size > PRINT_MOVE) | (size > CLOSE_PRINT) & flat & extreme)
        reopened = np.abs(np.log(o[2:] / c[:-2])) < PRINT_REOPEN
    for t in np.flatnonzero(spike & reopened) + 1:
        found[t] = v[t] <= PRINT_VOLUME * float(np.median(v[max(0, t - PRINT_SESSIONS):t]))
    return found


def bad_bars(bars: np.ndarray, wild: np.ndarray | None = None) -> np.ndarray:
    """For each calendar position, whether its bar holds a bad open, high or low on an otherwise calm day: a high or
    low more than WICK_PRINT outside the open and close, the next bar opening within CALM_CLOSE of the close; or an open
    more than OPEN_PRINT from the close before that is the bar's high or low. Either way the close stays within
    CALM_CLOSE of the close before, on under BAR_VOLUME times the median volume of the PRINT_SESSIONS bars before.
    Positions in ``wild`` (WILD_DAYS on the calendar) are left alone: those prints were real."""
    o, h, lo, c = bars[:, 0], bars[:, 1], bars[:, 2], bars[:, 3]
    v = np.nan_to_num(bars[:, 4], nan=0.0)
    found = np.zeros(len(bars), dtype=bool)
    if len(bars) < 3:
        return found
    with np.errstate(invalid="ignore", divide="ignore"):
        calm = np.abs(np.log(c[1:] / c[:-1])) < CALM_CLOSE                    # bars 1, 2, ...
        reopened = np.concatenate([np.abs(np.log(o[2:] / c[1:-1])) < CALM_CLOSE, [False]])
        wick = np.maximum(np.log(h[1:] / np.maximum(o[1:], c[1:])), np.log(np.minimum(o[1:], c[1:]) / lo[1:]))
        extreme = np.isclose(o[1:], h[1:], rtol=1e-9, atol=0) | np.isclose(o[1:], lo[1:], rtol=1e-9, atol=0)
        gapped = (np.abs(np.log(o[1:] / c[:-1])) > OPEN_PRINT) & extreme
        suspect = calm & ((wick > WICK_PRINT) & reopened | gapped)
    if wild is not None:
        suspect &= ~np.asarray(wild, dtype=bool)[1:]
    for t in np.flatnonzero(suspect) + 1:
        found[t] = v[t] < BAR_VOLUME * float(np.median(v[max(0, t - PRINT_SESSIONS):t]))
    return found


def window_checks(bars: np.ndarray, kind: str, ok: np.ndarray, factor: np.ndarray | None = None,
                  splices: np.ndarray | None = None, *, max_last: float = MAX_LAST_CLOSE,
                  breaks: np.ndarray | None = None, untrusted: np.ndarray | None = None,
                  adjusted: np.ndarray | None = None, wild: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Each of CHECKS as a pass mask over window starts (calendar positions), for repaired calendar-aligned bars.

    ``ok`` is calendar_ok for the same calendar and the instrument's event scope, ``factor`` the split_factors (as
    traded = stored x factor), ``splices`` the splice_points, ``breaks`` the jumps split_factors found at split
    records, ``untrusted`` the days a known-bad split document covers (BAD_SPLITS), ``adjusted`` the days whose stored
    prices carry an adjustment no split record explains (dividend_adjustments and UNRECORDED) and ``wild`` the days of
    real wild prints (WILD_DAYS). A missing session is a NaN row, so it fails the window. The span checks (lookback,
    splice, splits, glitch, bad print, junk, split documents, adjustments and records) cover the LOOKBACK closes before
    the window too, which feed the averages; a bad bar only matters where the chart shows it, in the window, and the
    cent grid (MISSED) is judged on the window's bars, whose prices the reveal quotes."""
    starts = np.arange(len(ok))
    if len(bars) < WINDOW or not len(ok):
        return {name: np.zeros(len(ok), dtype=bool) for name in CHECKS}
    factor = np.ones(len(bars)) if factor is None else factor
    o, h, lo, c, v = bars.T
    checks = {"calendar": ok.copy()}

    def marked(positions) -> np.ndarray:
        flags = np.zeros(len(c), dtype=bool)
        flags[np.asarray(positions if positions is not None else [], dtype=np.int64)] = True
        return flags

    with np.errstate(invalid="ignore", divide="ignore"):
        valid = np.isfinite(bars[:, :4]).all(axis=1) & (bars[:, :4] > 0).all(axis=1)
        closes = np.where(valid, c, 0.0)
        traded = np.where(valid, c * factor, np.nan)
        checks["bars"] = _counts(~valid, starts, WINDOW) == 0
        checks["lookback"] = _counts(valid, starts - LOOKBACK, LOOKBACK) == LOOKBACK
        # Moves and jumps are between two bars of the span, so its first bar's move from the day before is not one.
        between = lambda flags: _counts(flags, starts - LOOKBACK + 1, SPAN - 1) == 0
        checks["splice"] = between(marked(splices))
        checks["unadjusted split"] = between(marked(breaks))
        checks["unrecorded split"] = between(unrecorded_splits(bars))
        move = np.zeros(len(c), dtype=bool)
        move[1:] = valid[1:] & valid[:-1] & (np.abs(closes[1:] / np.where(valid[:-1], closes[:-1], 1.0) - 1) > MAX_MOVE)
        checks["glitch"] = between(move)
        checks["bad print"] = between(bad_prints(bars))
        checks["bad bar"] = _counts(bad_bars(bars, wild), starts, WINDOW) == 0
        checks["junk"] = _counts(valid & (traded < MIN_PRINT), starts - LOOKBACK, SPAN) == 0
        distrusted = np.zeros(len(c), dtype=bool) if untrusted is None else np.asarray(untrusted, dtype=bool)
        checks["split documents"] = _counts(distrusted, starts - LOOKBACK, SPAN) == 0
        unexplained = np.zeros(len(c), dtype=bool) if adjusted is None else np.asarray(adjusted, dtype=bool)
        checks["unrecorded adjustment"] = _counts(unexplained, starts - LOOKBACK, SPAN) == 0
        last = traded[starts + N_CONTEXT - 1]
        checks["split records"] = (_counts(~(np.isfinite(factor) & (factor > 0)), starts - LOOKBACK, SPAN) == 0) & (
            last <= max_last)
        # How firmly the prices sit on a grid (lattice_strength's score, unfloored) over the days in ``days``, in the
        # whole window and in its replay (rows, in that order).
        def strength(prices, size=0.01, days=True):
            per_day = np.nan_to_num(phasors(prices, size).real.sum(axis=1)) * days
            return np.stack([_counts(per_day, starts + first, width, np.float64) / (4 * width)
                             for first, width in ((0, WINDOW), (N_CONTEXT, N_REPLAY))])

        as_traded = bars[:, :4] * factor[:, None]
        firm, rounded = strength(as_traded), strength(bars[:, :4]) >= ON_CENTS
        coarse = (strength(as_traded, 0.05) >= OFF_CENTS) & (last >= COARSE_PRICE)
        missed = np.zeros(len(starts), dtype=bool)
        for multiple in MISSED:
            scaled = factor * multiple
            explained = (np.abs(scaled - np.round(scaled)) < WHOLE) & (scaled > 0.5)    # stored cents x a whole number
            moved = as_traded * multiple
            hit = np.where(rounded, strength(moved, days=~explained), strength(moved)) >= ON_CENTS
            missed |= (hit & ((firm < OFF_CENTS) if multiple > 1 else ~coarse)).any(axis=0)
        checks["cent grid"] = ~missed
        lowest = sliding_window_view(np.where(valid, traded, 0.0), WINDOW).min(axis=1)
        checks["price"] = (last >= MIN_LAST_CLOSE) & (lowest >= MIN_CLOSE)
        # The stored prices' grid, found as tradetest.price_grid finds it (over the window's prices and the closes
        # before it): the least common multiple of the grids found.
        spread = _span_extreme(h, c, valid, np.max) - _span_extreme(lo, c, valid, np.min)
        units = np.ones(len(starts), dtype=np.int64)
        for size in GRIDS:
            sums = _counts(np.nan_to_num(phasors(bars[:, :4], size).real.sum(axis=1)), starts, WINDOW, np.float64) + \
                _counts(np.nan_to_num(phasors(c, size).real), starts - LOOKBACK, LOOKBACK, np.float64)
            firm = (sums / (4 * WINDOW + LOOKBACK) >= VISIBLE) & ((size <= 0.01) | (size * SPAN_STEPS <= spread))
            units = np.where(firm, np.lcm(units, round(size * 1e5)), units)
        stored_low = sliding_window_view(np.where(valid, c, np.nan), WINDOW).min(axis=1)
        checks["quantisation"] = ~(np.where(units > 1, units / 1e5, 0.0) / stored_low > MAX_TICK)
        checks["zero volume"] = _counts(~(v > 0), starts, WINDOW) <= MAX_ZERO_VOLUME
        checks["flat bars"] = _counts(valid & (h == lo), starts, WINDOW) <= MAX_FLAT
        passed = np.logical_and.reduce([checks[name] for name in CHECKS[:-1]])
        found = np.flatnonzero(passed)
        if found.size:
            # A placeholder volume counts as none: judged against the window's median, where everything else passed.
            shares = sliding_window_view(np.where(v > 0, v, np.nan), WINDOW)[found]
            typical = np.nanmedian(shares, axis=1)
            none = _counts(~(v > 0), found, WINDOW) + (shares < PLACEHOLDER * typical[:, None]).sum(axis=1)
            checks["zero volume"][found] = none <= MAX_ZERO_VOLUME
            passed[found] &= checks["zero volume"][found]
        liquid = np.zeros(len(starts), dtype=bool)
        found = np.flatnonzero(passed)
        if found.size:
            dollars = np.where(valid & (v > 0), closes * v, 0.0)
            liquid[found] = np.median(sliding_window_view(dollars, WINDOW)[found], axis=1) >= MIN_DOLLAR_VOLUME[kind]
        checks["liquidity"] = liquid | ~passed     # only judged where everything else passed
    return checks


def first_failure(checks: dict[str, np.ndarray]) -> np.ndarray:
    """For each start, the index into CHECKS of the first check it fails, or len(CHECKS) when it passes them all."""
    failed = np.full(len(next(iter(checks.values()))), len(CHECKS))
    for index in reversed(range(len(CHECKS))):
        failed = np.where(checks[CHECKS[index]], failed, index)
    return failed


def eligible_starts(bars: np.ndarray, kind: str, ok: np.ndarray, factor: np.ndarray | None = None,
                    splices: np.ndarray | None = None, **options) -> np.ndarray:
    """Window starts (calendar positions) that pass every check (``options`` as for window_checks)."""
    checks = window_checks(bars, kind, ok, factor, splices, **options)
    return np.flatnonzero(first_failure(checks) == len(CHECKS))


@dataclass
class Candidate:
    """An instrument in the pool: where its bars come from and how to name a window of it."""
    symbol: str
    kind: str
    first: int                      # first and last calendar position with a bar
    last: int
    listing: Listing | None = None  # today's stocks and the ETFs; None for a stock known only to the directory
    records: tuple = ()             # the directory's Records for this ticker, oldest first: a ticker can be reused
    cached: bool = False            # bars from the ETF cache rather than the database


@dataclass
class History:
    bars: np.ndarray                # repaired [sessions, 5] on the SPY calendar, NaN where there is no bar
    factor: np.ndarray              # stored -> as traded, per session
    splices: np.ndarray             # splice_points
    breaks: np.ndarray              # jumps at split records the stored prices contradict or leave unexplained
    untrusted: np.ndarray           # sessions a known-bad split document covers (BAD_SPLITS)
    adjusted: np.ndarray            # sessions adjusted for something no split record carries (dividends, UNRECORDED)


class BankBuilder:
    """Reads the calendar, the pool and each instrument's bars; samples and writes the bank."""

    def __init__(self, db_path: str | Path, *, etf_cache: str | Path | None = None, progress=None):
        path = Path(db_path)
        if not path.is_file():
            raise BankError(f"No database at {path}. Run the backfill first.")
        self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        self.progress = progress or (lambda message: None)
        dates = [row[0] for row in self.conn.execute(
            "SELECT date FROM prices WHERE symbol = ? ORDER BY date", (CALENDAR,))]
        if len(dates) < WINDOW:
            raise BankError(f"{CALENDAR} has {len(dates)} sessions in the database; a window needs {WINDOW}.")
        self.days = day_numbers(dates)
        self.ok = {scope: calendar_ok(self.days, scope) for scope in ("replay", "window")}
        self.wild = _within(self.days, [(day, day) for day in WILD_DAYS])
        self.start_year = np.asarray(self.days[N_CONTEXT:N_CONTEXT + len(self.ok["replay"])],
                                     dtype="datetime64[D]").astype("datetime64[Y]").astype(np.int64) + 1970
        self.etf_bars = self._read_etf_cache(etf_cache)
        # The last stored day of every symbol: the day its stored prices are adjusted to.
        self.as_of: dict[str, int] = {symbol: int(day_numbers([last])[0]) for symbol, last in self.conn.execute(
            "SELECT symbol, last_date FROM price_state WHERE last_date IS NOT NULL")}
        self.current: set[str] = set()
        self.directory: dict[str, list[Record]] = {}
        self.pool = self._read_pool()
        self._read_probes()
        self._eligible: dict[str, np.ndarray] = {}
        self._histories: OrderedDict[str, History] = OrderedDict()
        self._profiles: dict[str, dict] = {}
        self._shares: dict[str, tuple[list, list]] = {}
        self.rejected: Counter = Counter()

    def close(self) -> None:
        self.conn.close()

    # The pool ---------------------------------------------------------------------------------------------------

    def _read_etf_cache(self, etf_cache) -> dict[str, dict]:
        """Bars for the listed ETFs the database lacks, from the cache file (already split-adjusted)."""
        if etf_cache is None:
            return {}
        path = Path(etf_cache)
        if not path.is_file():
            log.warning("No ETF cache at %s; the ETFs come from the database only.", path)
            return {}
        bars = json.loads(path.read_text(encoding="utf-8")).get("bars") or {}
        return {symbol: rows for symbol, rows in bars.items() if symbol in ETFS}

    def _position(self, iso_date: str | None, *, side: str) -> int:
        return int(np.searchsorted(self.days, day_numbers([iso_date])[0], side=side)) - (1 if side == "right" else 0)

    def _directory(self) -> dict[str, list[Record]]:
        """The delisted directory's US listings by ticker, oldest first. A fund, warrant or the like keeps a Record
        with no listing, so a window of its prices under a reused ticker is recognised and dropped."""
        directory = defaultdict(list)
        row = self.conn.execute("SELECT body FROM screening_data WHERE key = ?", (DELISTED,)).fetchone()
        for record in (json.loads(row[0]).get("rows") or []) if row else []:
            symbol, name, exchange = record.get("symbol"), record.get("companyName"), record.get("exchange")
            try:
                delisted = date.fromisoformat(record["delistedDate"])
                ipo = date.fromisoformat(record["ipoDate"]) if record.get("ipoDate") else date.min
            except (KeyError, TypeError, ValueError):
                continue
            if not symbol or exchange not in EXCHANGES or ipo > delisted:
                continue
            # The directory's delisting dates run late (a buyout is often recorded years after), so no date is given.
            name = RENAMED.get(symbol, name)
            listing = None if exclusion_reason(symbol, name, exchange=exchange, is_etf=False, is_fund=False) else \
                Listing(symbol, name or symbol, "stock", exchange=exchange, note="Since delisted")
            first = day_numbers([ipo.isoformat()])[0] if ipo != date.min else np.iinfo(np.int64).min
            directory[symbol].append(Record(int(first), int(day_numbers([delisted.isoformat()])[0]), listing))
        return {symbol: sorted(records, key=lambda r: r.delisted) for symbol, records in directory.items()}

    def _read_pool(self) -> list[Candidate]:
        symbols = {row[0]: row[1:] for row in self.conn.execute(
            "SELECT symbol, is_common, name, exchange, sector, industry FROM symbols")}
        self.current = set(symbols)
        self.directory = self._directory()
        pool = []
        for symbol, first, last in self.conn.execute("SELECT symbol, first_date, last_date FROM price_state "
                                                     "WHERE first_date IS NOT NULL AND last_date IS NOT NULL ORDER BY symbol"):
            if symbol.startswith("^"):
                continue
            a, b = self._position(first, side="left"), self._position(last, side="right")
            if b - a + 1 < WINDOW:
                continue
            records = tuple(self.directory.get(symbol, ()))
            if symbol in ETFS:
                pool.append(Candidate(symbol, "etf", a, b, etf_listing(symbol)))
                self.etf_bars.pop(symbol, None)          # the database's longer history wins over the cache
            elif symbol in symbols:
                is_common, name, exchange, sector, industry = symbols[symbol]
                if is_common and industry != SHELL:
                    pool.append(Candidate(symbol, "stock", a, b, Listing(symbol, name or symbol, "stock", sector or "",
                                                                         industry or "", exchange or ""), records))
            elif any(r.listing is not None for r in records):
                pool.append(Candidate(symbol, "stock", a, b, records=records))
        for symbol, rows in sorted(self.etf_bars.items()):
            dates = sorted(rows)
            if dates:
                a, b = self._position(dates[0], side="left"), self._position(dates[-1], side="right")
                if b - a + 1 >= WINDOW:
                    pool.append(Candidate(symbol, "etf", a, b, etf_listing(symbol), cached=True))
        return pool

    # One series under two tickers -------------------------------------------------------------------------------

    def _read_probes(self) -> None:
        """For every symbol in the database (one pass over the prices), the PROBE_RETURNS daily returns up to every
        PROBE_EVERY-th session and how far cent rounding of the closes could move each, and per probe the first
        returns in order, so the tickers whose returns match a given ticker's are found by bisection."""
        self.probe_at = np.arange(PROBE_RETURNS, len(self.days), PROBE_EVERY)
        sessions = self.probe_at[:, None] + np.arange(-PROBE_RETURNS, 1)[None, :]
        slot = {iso(day): (j, k) for j, row in enumerate(sessions) for k, day in enumerate(self.days[row])}
        self.probe_symbols = [row[0] for row in self.conn.execute("SELECT symbol FROM price_state ORDER BY symbol")]
        self.probe_row = {symbol: i for i, symbol in enumerate(self.probe_symbols)}
        closes = np.full((len(self.probe_symbols), len(self.probe_at), PROBE_RETURNS + 1), np.nan, dtype=np.float32)
        marks = ",".join("?" * len(slot))
        for symbol, day, close in self.conn.execute(f"SELECT symbol, date, close FROM prices WHERE date IN ({marks})",
                                                    list(slot)):
            i = self.probe_row.get(symbol)
            if i is not None and close and close > 0:
                closes[(i, *slot[day])] = close
        self.probe_seen = np.isfinite(closes).any(axis=2)              # [symbols, probes]: any close there
        with np.errstate(invalid="ignore", divide="ignore"):
            returns = np.log(closes[:, :, 1:] / closes[:, :, :-1])
            self.probe_rounding = 0.005 / closes[:, :, 1:] + 0.005 / closes[:, :, :-1]
            # Prices too low for cent rounding to stay within SEARCH_ROUNDING would match almost anything.
            moved = np.isfinite(returns).all(axis=2) & (np.abs(returns).sum(axis=2) >= PROBE_MOVE) & (
                self.probe_rounding <= SEARCH_ROUNDING).all(axis=2)
            self.probe_returns = np.where(moved[:, :, None], returns, np.float32(np.nan))
        first = self.probe_returns[:, :, 0]
        self.probe_order = [np.argsort(first[:, j]) for j in range(first.shape[1])]   # NaN last
        self.probe_sorted = [first[order, j] for j, order in enumerate(self.probe_order)]

    def _same(self, i: int, k) -> np.ndarray:
        """Per probe, whether tickers i and k (one row number, or an array of them) printed the same returns."""
        with np.errstate(invalid="ignore"):
            gap = np.abs(self.probe_returns[i] - self.probe_returns[k])
            return (gap <= self.probe_rounding[i] + self.probe_rounding[k] + RETURN_SLACK).all(axis=-1)

    def shares(self, candidate: Candidate) -> tuple[list[tuple[int, int]], list[tuple[int, int, str]]]:
        """The calendar stretches (first, last) of the candidate's prices that another ticker holds too: those the
        other ticker carries on after, which are its own (no window of the candidate may touch them), and those the
        candidate carries on, with the other ticker's symbol (whose directory record may name them)."""
        found = self._shares.get(candidate.symbol)
        if found is not None:
            return found
        foreign, inherited, own = [], [], self.probe_row.get(candidate.symbol)
        if own is not None and not candidate.cached:
            x, hits = self.probe_returns[own], Counter()
            for j in np.flatnonzero(np.isfinite(x[:, 0])):
                reach = float(self.probe_rounding[own, j, 0]) + SEARCH_ROUNDING + RETURN_SLACK
                lo = np.searchsorted(self.probe_sorted[j], x[j, 0] - reach)
                hi = np.searchsorted(self.probe_sorted[j], x[j, 0] + reach, side="right")
                ids = self.probe_order[j][lo:hi]
                with np.errstate(invalid="ignore"):
                    match = (np.abs(self.probe_returns[ids, j] - x[j]) <= self.probe_rounding[ids, j]
                             + self.probe_rounding[own, j] + RETURN_SLACK).all(axis=1)
                hits.update(ids[match].tolist())
            for other, n in hits.items():
                if other != own and n >= MIN_SHARED:
                    theirs, ours = self._shared(candidate, own, other)
                    foreign += theirs
                    inherited += ours
        found = self._shares[candidate.symbol] = (foreign, inherited)
        return found

    def _shared(self, candidate: Candidate, own: int, other: int) -> tuple[list, list]:
        same = np.flatnonzero(self._same(own, other))
        theirs, ours = [], []
        for run in np.split(same, np.flatnonzero(np.diff(same) > 2) + 1):   # a single missed probe joins two runs
            if len(run) < MIN_SHARED:
                continue
            first = int(self.probe_at[run[0] - 1]) + 1 if run[0] > 0 else 0
            last = int(self.probe_at[run[-1] + 1]) - 1 if run[-1] + 1 < len(self.probe_at) else len(self.days) - 1
            if self._carries_on(candidate, own, other, int(run[-1])) == own:
                ours.append((first, last, self.probe_symbols[other]))
            else:
                theirs.append((first, last))
        return theirs, ours

    def _carries_on(self, candidate: Candidate, own: int, other: int, j: int) -> int | None:
        """Which of two tickers sharing a series up to probe ``j`` carries it on: never one whose company was formed
        after it (FORMED); else the only one with prices after it; when both have, the one whose close does not jump
        where their returns part (the other passed to a new company, or kept a separation its stored prices never
        adjusted for), or the one whose prices go on longer when the returns never part; when neither has, today's
        listing. None when that cannot be told."""
        shared = int(self.days[self.probe_at[j]])
        late = [formed_day(self.probe_symbols[i]) > shared for i in (own, other)]
        if late[0] != late[1]:
            return other if late[0] else own
        after = [bool(self.probe_seen[i, j + 1:].any()) for i in (own, other)]
        if after[0] != after[1]:
            return own if after[0] else other
        if not after[0]:
            listed = [self.probe_symbols[i] in self.current for i in (own, other)]
            return (own if listed[0] else other) if listed[0] != listed[1] else min(own, other)
        a = int(self.probe_at[j]) - PROBE_RETURNS
        b = min(len(self.days) - 1, int(self.probe_at[min(j + 1, len(self.probe_at) - 1)]) + PROBE_EVERY)
        mine = self.history(candidate).bars[a:b + 1, 3]
        theirs = np.full(b - a + 1, np.nan)
        for day, close in self.conn.execute("SELECT date, close FROM prices WHERE symbol = ? AND date BETWEEN ? AND ? "
                                            "ORDER BY date", (self.probe_symbols[other], iso(self.days[a]),
                                                              iso(self.days[b]))):
            at = int(np.searchsorted(self.days, day_numbers([day])[0]))
            if at <= b and self.days[at] == day_numbers([day])[0] and close and close > 0:
                theirs[at - a] = close
        with np.errstate(invalid="ignore", divide="ignore"):
            ours, others = np.log(mine[1:] / mine[:-1]), np.log(theirs[1:] / theirs[:-1])
            rounding = 0.005 * (1 / mine[1:] + 1 / mine[:-1] + 1 / theirs[1:] + 1 / theirs[:-1]) + RETURN_SLACK
        both = np.flatnonzero(np.isfinite(ours) & np.isfinite(others))
        parted = [t for t in both if abs(ours[t] - others[t]) > max(rounding[t], PART_MOVE)]
        if not parted and both.size:
            # They never part: one ticker stopped soon after (a ticker change), and the other goes on with the series.
            ends = [self.as_of.get(self.probe_symbols[i], 0) for i in (own, other)]
            return None if ends[0] == ends[1] else own if ends[0] > ends[1] else other
        if not parted or parted[0] == both[0]:
            return None
        jumps = abs(ours[parted[0]]), abs(others[parted[0]])
        if jumps[0] > 2 * jumps[1] + 0.02:
            return other
        if jumps[1] > 2 * jumps[0] + 0.02:
            return own
        return None

    # One instrument's bars --------------------------------------------------------------------------------------

    def history(self, candidate: Candidate) -> History:
        """Repaired bars on the SPY calendar with their as-traded factors, splice points and split-record checks."""
        symbol = candidate.symbol
        if symbol in self._histories:
            self._histories.move_to_end(symbol)
            return self._histories[symbol]
        splits, dividends = [], {}
        if candidate.cached:
            rows = sorted(self.etf_bars[symbol].items())
            dates, values = [d for d, _ in rows], [(list(bar) + [None] * 5)[:5] for _, bar in rows]
        else:
            rows = self.conn.execute("SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ? "
                                     "ORDER BY date", (symbol,)).fetchall()
            dates, values = [r[0] for r in rows], [r[1:] for r in rows]
            row = self.conn.execute("SELECT body FROM screening_data WHERE key = ?", (SPLITS + symbol,)).fetchone()
            splits = (json.loads(row[0]).get("rows") or []) if row else []   # no record: no known splits
            row = self.conn.execute("SELECT body FROM screening_data WHERE key = ?", (DIVIDENDS + symbol,)).fetchone()
            dividends = (json.loads(row[0]).get("rows") or {}) if row else {}
        bars = np.full((len(self.days), 5), np.nan)
        if dates:
            days, values = day_numbers(dates), np.array(values, dtype=np.float64).reshape(-1, 5)
            at = np.minimum(np.searchsorted(self.days, days), len(self.days) - 1)
            keep = self.days[at] == days        # bars on days SPY did not trade are dropped
            bars[at[keep]] = values[keep]
        bars = repair(bars)
        # The stored prices are adjusted for the splits up to their last day; a later split is not in them yet.
        as_of = self.as_of.get(symbol, int(self.days[-1]))
        factor, breaks = split_factors(self.days, splits if isinstance(splits, list) else [{}], as_of, bars[:, 3])
        untrusted = np.zeros(len(self.days), dtype=bool)
        if symbol in BAD_SPLITS:
            cutoff = BAD_SPLITS[symbol]
            untrusted = np.ones(len(self.days), dtype=bool) if cutoff is None else self.days < day_numbers([cutoff])[0]
        adjusted = dividend_adjustments(self.days, dividends, as_of, bars[:, 3], splits)
        if symbol in UNRECORDED:
            adjusted |= self.days < day_numbers([UNRECORDED[symbol]])[0]
        found = History(bars, factor, splice_points(bars), breaks, untrusted, adjusted)
        self._histories[symbol] = found
        if len(self._histories) > HISTORIES_KEPT:
            self._histories.popitem(last=False)
        return found

    def eligible(self, candidate: Candidate) -> np.ndarray:
        """The candidate's eligible and nameable window starts, remembered for the rest of the build."""
        found = self._eligible.get(candidate.symbol)
        if found is None:
            history = self.history(candidate)
            ok = self.ok[event_scope(candidate.symbol)]
            checks = window_checks(history.bars, candidate.kind, ok, history.factor, history.splices,
                                   max_last=np.inf if candidate.symbol in HIGH_PRICED else MAX_LAST_CLOSE,
                                   breaks=history.breaks, untrusted=history.untrusted, adjusted=history.adjusted,
                                   wild=self.wild)
            failed = first_failure(checks)
            # Starts outside the instrument's own history say nothing about it.
            inside = (np.arange(len(ok)) >= candidate.first) & (np.arange(len(ok)) + WINDOW - 1 <= candidate.last)
            self.rejected.update({CHECKS[i]: int(n) for i, n in enumerate(np.bincount(failed[inside],
                                                                                    minlength=len(CHECKS) + 1)[:-1])})
            found = np.flatnonzero(failed == len(CHECKS))
            named = np.array([self.listing(candidate, int(s)) is not None for s in found], dtype=bool)
            self.rejected["naming"] += int((~named).sum())
            found = self._eligible[candidate.symbol] = found[named]
        return found

    def _profile(self, symbol: str) -> dict:
        if symbol not in self._profiles:
            row = self.conn.execute("SELECT body FROM screening_data WHERE key = ?", (PROFILE + symbol,)).fetchone()
            self._profiles[symbol] = (json.loads(row[0]).get("profile") or {}) if row else {}
        return self._profiles[symbol]

    def listing(self, candidate: Candidate, start: int) -> Listing | None:
        """What a window of the candidate is named, or None when the ticker's history there may be another company's.

        A window whose prices another ticker carries on (shares) is dropped, and so is one whose prices another ticker
        holds too when the two tickers' split documents disagree on its prices as traded. A window from before the date
        PREDECESSORS gives is named after the company the prices were then, or dropped when it reaches past the date.
        Today's stock: a directory record of the same ticker that covers the window names it (an earlier company); one
        that only overlaps it, or a window before the last splice, is dropped. Else a retired ticker whose prices the
        stock carries on names the window from its directory record when that covers it (the company the chart was
        then) and the retired ticker's company was formed by then; else a window before the listing date of the saved
        profile, or before the company was formed (FORMED), is dropped. A directory stock: the record that covers the
        window, if the window lies in the stretch of history its listing began in (between the same splices), the
        saved profile is not a fund's and the company was formed by then."""
        if candidate.kind == "etf":
            return candidate.listing
        first, last = start - LOOKBACK, start + WINDOW - 1
        foreign, inherited = self.shares(candidate)
        if any(a <= last and first <= b for a, b in foreign):
            return None
        if any(a <= last and first <= b and not self._agrees(candidate, partner, start, last)
               for a, b, partner in inherited):
            return None
        d0, d1 = self.days[max(first, 0)], self.days[last]
        before = PREDECESSORS.get(candidate.symbol)
        if before is not None and d0 < day_numbers([before[0]])[0]:
            return Listing(before[1], before[2], "stock", exchange=before[3]) if d1 < day_numbers([before[0]])[0] \
                else None
        splices = self.history(candidate).splices
        segment = int(np.searchsorted(splices, first, side="right"))
        covering = [r for r in candidate.records if r.ipo <= d0 and d1 <= r.delisted]
        if candidate.listing is not None:
            if covering:
                # A record says an older company; a splice before the window says the ticker changed hands since.
                return None if segment == len(splices) > 0 else covering[-1].listing
            if any(r.ipo <= d1 and d0 <= r.delisted for r in candidate.records) or segment < len(splices):
                return None
            for a, b, partner in inherited:
                if a <= first and last <= b and self.as_of.get(partner, -1) >= d1 and formed_day(partner) <= d0:
                    named = [r.listing for r in self.directory.get(partner, ())
                             if r.listing is not None and r.ipo <= d0 and d1 <= r.delisted]
                    if named:
                        return named[-1]
            listed = self._profile(candidate.symbol).get("ipoDate")
            try:
                if listed and day_numbers([str(listed)[:10]])[0] > d0:
                    return None
            except ValueError:
                pass
            return None if formed_day(candidate.symbol) > d0 else candidate.listing
        profile = self._profile(candidate.symbol)
        if profile.get("isEtf") or profile.get("isFund") or profile.get("industry") == SHELL or \
                formed_day(candidate.symbol) > d0:
            return None
        for record in reversed(covering):
            began = max(candidate.first, int(np.searchsorted(self.days, record.ipo)))
            if record.listing is not None and segment == int(np.searchsorted(splices, began, side="right")):
                return record.listing
        return None

    def _agrees(self, candidate: Candidate, partner: str, start: int, last: int) -> bool:
        """Whether another ticker that holds the window's prices too quotes them as traded as the candidate does
        (within AGREE), on the last bar of the window both have, in prices as traded on the window's last day. When
        it does not, the split documents of one of them are wrong, and nothing says which."""
        mine, theirs = self.history(candidate), self.history(Candidate(partner, "stock", 0, 0))
        both = np.flatnonzero(np.isfinite(mine.bars[start:last + 1, 3]) & np.isfinite(theirs.bars[start:last + 1, 3]))
        if not both.size:
            return True
        t = start + int(both[-1])
        ours, others = mine.bars[t, 3] * mine.factor[last], theirs.bars[t, 3] * theirs.factor[last]
        return bool(np.isfinite(ours) and np.isfinite(others) and abs(ours / others - 1) <= AGREE)

    def window(self, candidate: Candidate, start: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, np.ndarray]:
        """(bars [250, 5], lookback closes [199], days [250], basis, grid [449]) of one window: basis turns a stored
        price into the price as traded on its last bar, and grid is one as-traded cent in stored prices on each day
        (the closes before the window first), which a split inside the window changes."""
        history = self.history(candidate)
        closes = history.bars[start - LOOKBACK:start, 3].copy()
        window = history.bars[start:start + WINDOW].copy()
        window[:, 4] = np.where(window[:, 4] > 0, window[:, 4], 0.0)   # a missing volume counts as none
        return (window, closes, self.days[start:start + WINDOW].copy(), float(history.factor[start + WINDOW - 1]),
                0.01 / history.factor[start - LOOKBACK:start + WINDOW])

    def dither_fault(self, candidate: Candidate, start: int) -> str | None:
        """Why the web app's dither (tradetest.dither_steps, on the window as the bank stores it) does not suit the
        window, or None: it would move a price (a bar's low, the least of them) by more than MAX_DITHER of it, or the
        dithered prices still sit on a grid (tradetest.residual_grid at RESIDUAL or more), which means a scaling the
        split records miss."""
        bars, closes, _, _, cent = self.window(candidate, start)
        bars, closes = bars.astype(np.float32).astype(np.float64), closes.astype(np.float32).astype(np.float64)
        step, second = dither_steps(bars, closes, cent.astype(np.float32))
        widest = (step + np.where(np.isclose(second, step, rtol=1e-5, atol=0), 0.0, second)) / 2   # as dither adds them
        if not (widest <= MAX_DITHER * np.concatenate([closes, bars[:, :4].min(axis=1)])).all():
            return "dither width"
        seed = hmac.new(b"tradetest-bank", b"dither|%s|%d" % (candidate.symbol.encode(), start), hashlib.sha256)
        moved, before = dither(bars, closes, step, second, seed.digest())
        if residual_grid(np.concatenate([before, moved[:, :4].ravel()]))[0] >= RESIDUAL:
            return "residual grid"
        return None

    # Sampling ---------------------------------------------------------------------------------------------------

    def sample(self, kind: str, count: int, rng: random.Random, taken: dict[str, list[int]]) -> list[tuple]:
        """Up to ``count`` windows of one kind: (candidate, start, listing). Stops early when the pool runs dry."""
        members = [c for c in self.pool if c.kind == kind]
        firsts, lasts = np.array([c.first for c in members]), np.array([c.last for c in members])
        ok = self.ok["replay"]                  # every scope's starts are among these
        years = sorted({int(y) for y in self.start_year[ok]})
        by_year: dict[int, list[Candidate]] = {}
        for year in years:
            # An instrument has data in a year when its bars span at least one of the year's possible windows.
            positions = np.flatnonzero(ok & (self.start_year == year))
            at = np.searchsorted(positions, firsts)
            spans = (at < positions.size) & (positions[np.minimum(at, positions.size - 1)] + WINDOW - 1 <= lasts)
            by_year[year] = [c for c, has in zip(members, spans) if has] if members else []
        open_years = [y for y in years if by_year[y]]
        chosen, last_note = [], time.monotonic()
        while len(chosen) < count and open_years:
            year = rng.choice(open_years)
            members = by_year[year]
            candidate = members[rng.randrange(len(members))]
            starts = self.eligible(candidate)
            starts = starts[self.start_year[starts] == year]
            if starts.size and taken.get(candidate.symbol):
                used = np.array(taken[candidate.symbol])
                starts = starts[np.all(np.abs(starts[:, None] - used[None, :]) >= WINDOW, axis=1)]
            if not starts.size:
                # Taken windows only ever grow, so an instrument that fails a year never passes it later.
                members.remove(candidate)
                if not members:
                    open_years.remove(year)
                continue
            start = int(starts[rng.randrange(starts.size)])
            fault = self.dither_fault(candidate, start)
            if fault:
                self._eligible[candidate.symbol] = self._eligible[candidate.symbol][self._eligible[candidate.symbol]
                                                                                    != start]
                self.rejected[fault] += 1
                continue
            taken.setdefault(candidate.symbol, []).append(start)
            chosen.append((candidate, start, self.listing(candidate, start)))
            if time.monotonic() - last_note >= 5:
                last_note = time.monotonic()
                self.progress(f"{kind} windows {len(chosen)}/{count}, {len(self._eligible)} instruments read")
        return chosen

    def build(self, output: str | Path, *, windows: int = 4000, seed: int = 20261008) -> dict:
        if windows < 10:
            raise BankError("A bank needs at least 10 windows, one set of charts.")
        started = time.monotonic()
        rng, taken = random.Random(seed), {}
        etf_target = round(windows * ETF_SHARE)
        etfs = self.sample("etf", etf_target, rng, taken)
        stocks = self.sample("stock", windows - len(etfs), rng, taken)
        chosen = etfs + stocks
        if len(chosen) < 10:
            raise BankError(f"Only {len(chosen)} eligible windows were found; a bank needs at least 10.")
        rng.shuffle(chosen)
        listings: dict[Listing, int] = {}
        bars = np.zeros((len(chosen), WINDOW, 5), dtype=np.float32)
        lookback = np.zeros((len(chosen), LOOKBACK), dtype=np.float32)
        days = np.zeros((len(chosen), WINDOW), dtype=np.int32)
        instrument = np.zeros(len(chosen), dtype=np.int32)
        basis, grid = np.zeros(len(chosen)), np.zeros((len(chosen), SPAN), dtype=np.float32)
        for i, (candidate, start, listing) in enumerate(chosen):
            bars[i], lookback[i], days[i], basis[i], grid[i] = self.window(candidate, start)
            instrument[i] = listings.setdefault(listing, len(listings))
        year = (days[:, N_CONTEXT].astype("datetime64[D]").astype("datetime64[Y]").astype(np.int64) + 1970).astype(np.int16)
        names = list(listings)
        arrays = {"bars": bars, "lookback": lookback, "day": days, "instrument": instrument, "year": year,
                  "basis": basis, "grid": grid}
        for field, plural in FIELDS.items():
            arrays[plural] = np.array([getattr(item, field) for item in names], dtype=str)
        version = bank_version(arrays)
        through = iso(self.days[-1])
        meta = {"version": version, "n_context": N_CONTEXT, "n_replay": N_REPLAY, "lookback": LOOKBACK,
                "built": datetime.now(timezone.utc).date().isoformat(), "windows": len(chosen), "data_through": through}
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".partial")
        with open(partial, "wb") as handle:
            np.savez_compressed(handle, meta=np.array(json.dumps(meta)), **arrays)
        os.replace(partial, output)
        counts: dict[str, dict[str, int]] = {}
        for i, (candidate, _, _) in enumerate(chosen):
            entry = counts.setdefault(str(int(year[i])), {"stock": 0, "etf": 0})
            entry[candidate.kind] += 1
        by_symbol = Counter(candidate.symbol for candidate, _, _ in etfs)
        return {"path": str(output), "version": version, "windows": len(chosen),
                "stock_windows": len(stocks), "etf_windows": len(etfs), "etf_target": etf_target,
                "etf_share": round(len(etfs) / len(chosen), 3), "etfs": dict(sorted(by_symbol.items())),
                "instruments": len(names), "symbols": len({item.symbol for item in names}),
                "delisted_windows": sum(1 for _, _, listing in chosen if listing.note),
                "by_year": dict(sorted(counts.items())), "data_through": through,
                "instruments_read": len(self._eligible), "rejected_starts": dict(self.rejected),
                "size_mb": round(output.stat().st_size / 1e6, 1), "seconds": round(time.monotonic() - started, 1)}


def bank_version(arrays: dict[str, np.ndarray]) -> str:
    """Twelve hex digits of a SHA-256 over every array, so a rebuilt bank never accepts the old bank's tokens."""
    digest = hashlib.sha256()
    for name in sorted(arrays):
        value = np.ascontiguousarray(arrays[name])
        digest.update(f"{name}|{value.dtype.str}|{value.shape}|".encode())
        digest.update("\x00".join(value.tolist()).encode() if value.dtype.kind == "U" else value.tobytes())
    return digest.hexdigest()[:12]


def build_bank(db_path: str | Path, output: str | Path, *, windows: int = 4000, seed: int = 20261008,
               etf_cache: str | Path | None = None, progress=None) -> dict:
    """Build the bank from the database (read-only) and the optional ETF cache, and return its summary."""
    builder = BankBuilder(db_path, etf_cache=etf_cache, progress=progress)
    try:
        return builder.build(output, windows=windows, seed=seed)
    finally:
        builder.close()
