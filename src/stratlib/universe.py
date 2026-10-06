"""Stock universe: which listed symbols count as common stock.

Pure functions only; fetching lives in backfill.py.

FMP's screener flags ETFs and funds but has no share-type field, so
preferreds, warrants, units, rights, and exchange-listed notes are recognised
from symbol conventions and from the security name. The reason a symbol was
excluded is stored, so it can be reviewed.

One listing per company: share classes (BRK-A and BRK-B) and exchange-listed
notes that carry exactly the parent's name (``MGRB`` is listed as "Affiliated
Managers Group, Inc.") would otherwise count as separate companies in RS and
industry ranks, and a portfolio could hold one company twice. Among common
listings with the same company name on the same exchange, the most traded is
kept; the others are excluded as "secondary listing". The exchange keeps apart
different companies with similar names (First BanCorp on NYSE, First Bancorp on
Nasdaq); FMP's market caps are not comparable across notes and shares.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

# FMP writes NYSE and AMEX issue suffixes after a dash: BAC-PL, XYZ-WT, ABC-UN.
_SYMBOL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preferred", re.compile(r"-P[A-Z]?$")),
    ("warrant", re.compile(r"-(W|WS|WT)$")),
    ("unit", re.compile(r"-(U|UN)$")),
    ("right", re.compile(r"-(R|RI|RT)$")),
)

# Nasdaq common stock symbols have at most four letters; a fifth letter is an
# issue-type suffix. Suffixes not listed (A/B/K classes, Y for ADRs, F for
# foreign issuers, and so on) are common stock. "L" (miscellaneous) is left
# out because GOOGL uses it for a common share class.
_NASDAQ_FIFTH_LETTER = {
    "W": "warrant",
    "U": "unit",
    "R": "right",
    "P": "preferred",
    "O": "preferred",
    "N": "preferred",
    "M": "preferred",
    "G": "debt",
    "H": "debt",
    "I": "debt",
    "V": "when-issued",
    "X": "fund",
    "T": "other",
    "Z": "other",
}

_NAME_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preferred", re.compile(
        r"\bpreferred\s+(stock|shares?|securities)\b|\bpfd\b|\bcumulative\b|\bnon-cumulative\b"
        r"|\bdepositary\s+shares?,?\s+each\b",
        re.I,
    )),
    ("warrant", re.compile(r"\bwarrants?\b", re.I)),
    ("unit", re.compile(r"\bunits?\b", re.I)),
    ("right", re.compile(r"\brights?\b", re.I)),
    ("debt", re.compile(
        r"\b(notes?|debentures?)\s+due\b|\bsenior\s+notes?\b|\bdebentures?\b|\bnts?\b|\bjr\s?sub\b"
        # "DTE Energy Company 2025 Series", "Southern Company (The) Series 2"
        r"|\b(19|20)\d\d\s+series\b|\bseries\s+(19|20)?\d{1,2}[a-z]?\s*$",
        re.I,
    )),
    # A coupon rate in the name ("6.00% Series GG") marks a preferred or a note.
    ("fixed-income", re.compile(r"\d+(\.\d+)?\s?%")),
    ("structured", re.compile(
        r"\bstrats\b|\bpplus\b|\btrust\s+certificates\b|\bcorts\b|\bsecurities-backed\b"
        r"|\bcapital\s+obligations\b",
        re.I,
    )),
    ("fund", re.compile(r"\b(fund|etf|etn)\b", re.I)),
)


@dataclass(frozen=True)
class ListedStock:
    symbol: str
    name: str | None
    exchange: str | None
    exchange_full: str | None
    sector: str | None
    industry: str | None
    country: str | None
    market_cap: float | None
    price: float | None
    avg_volume: float | None
    is_etf: bool
    is_fund: bool
    excluded_reason: str | None

    @property
    def is_common(self) -> bool:
        return self.excluded_reason is None


def exclusion_reason(
    symbol: str, name: str | None, *, exchange: str | None = None, is_etf: bool, is_fund: bool
) -> str | None:
    """Why a listed security is not a common stock, or None if it is one."""
    if is_etf:
        return "etf"
    if is_fund:
        return "fund"
    if exchange == "NASDAQ" and len(symbol) == 5 and symbol.isalpha():
        reason = _NASDAQ_FIFTH_LETTER.get(symbol[-1])
        if reason:
            return reason
    for reason, pattern in _SYMBOL_RULES:
        if pattern.search(symbol):
            return reason
    for reason, pattern in _NAME_RULES:
        if name and pattern.search(name):
            return reason
    return None


def company_key(name: str | None) -> str | None:
    """A company name reduced to letters and digits, without a share-class label."""
    if not name:
        return None
    text = re.sub(r"\bclass\s+[a-z]\b", " ", name.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return text or None


def one_listing_per_company(stocks: Sequence[ListedStock]) -> list[ListedStock]:
    """Keep the most traded common listing of each company name and exchange; mark the rest secondary."""
    groups = defaultdict(list)
    for stock in stocks:
        key = company_key(stock.name)
        if stock.is_common and key:
            groups[key, stock.exchange].append(stock)
    secondary = set()
    for members in groups.values():
        if len(members) > 1:
            keep = min(members, key=lambda s: (-(s.avg_volume or 0), len(s.symbol), s.symbol))
            secondary.update(s.symbol for s in members if s is not keep)
    return [replace(s, excluded_reason="secondary listing") if s.symbol in secondary else s for s in stocks]


def parse_listings(rows: Iterable[dict]) -> list[ListedStock]:
    """Company-screener rows to ListedStock, dropping duplicates and blanks."""
    seen: dict[str, ListedStock] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        name = row.get("companyName") or row.get("name")
        exchange = row.get("exchangeShortName") or row.get("exchange")
        is_etf = bool(row.get("isEtf"))
        is_fund = bool(row.get("isFund"))
        seen[symbol] = ListedStock(
            symbol=symbol,
            name=name,
            exchange=exchange,
            exchange_full=row.get("exchange"),
            sector=row.get("sector") or None,
            industry=row.get("industry") or None,
            country=row.get("country") or None,
            market_cap=row.get("marketCap"),
            price=row.get("price"),
            avg_volume=row.get("avgVolume"),
            is_etf=is_etf,
            is_fund=is_fund,
            excluded_reason=exclusion_reason(
                symbol, name, exchange=exchange, is_etf=is_etf, is_fund=is_fund
            ),
        )
    return one_listing_per_company(list(seen.values()))


def sample_symbols(symbols: Sequence[str], n: int, seed: int) -> list[str]:
    """A repeatable sample of ``n`` symbols, so development runs reuse the cache."""
    pool = sorted(set(symbols))
    if n >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, n))
