"""What the app's engine backtests read from the database: the stock panel, the benchmarks, earnings dates and
sector classes. The same cached data the research uses (research/benchmarks.py, earnings.py, nash_fundamentals.py),
read from the store without an FMP connection."""

from __future__ import annotations

from datetime import timedelta

from ..backtest_approx import PROFILE
from ..fmp.client import cache_key, clean_params
from .panel import MIN_DOLLAR_VOLUME, MIN_PRICE, STUDY_FROM, Panel, build

EARNINGS_PATH, EARNINGS_LIMIT = "earnings", 100   # research/earnings.py's request, cached in http_cache
# research/nash_fundamentals.py: classifications FMP has wrong or missing for these delisted companies.
SECTOR_FIXES = {"BPR": ("Real Estate", "REIT - Retail"), "CELG": ("Healthcare", "Biotechnology"),
                "CLDR": ("Technology", "Software - Infrastructure"), "GRA": ("Basic Materials", "Chemicals - Specialty"),
                "LDL": ("Industrials", "Industrial - Machinery"), "RPAI": ("Real Estate", "REIT - Retail"),
                "XEC": ("Energy", "Oil & Gas Exploration & Production")}


def stock_panel(store, settings, *, through: str | None = None, progress=None, min_price: float = MIN_PRICE,
                min_dollar_volume: float = MIN_DOLLAR_VOLUME) -> Panel:
    """The research panel without ETFs: today's common stocks and those delisted since 2016, as the screens see
    them, at the given liquidity floor. SPY and QQQ are included for the benchmark and the market filters, but
    never traded."""
    return build(through, progress=progress or (lambda *a: None), store=store, settings=settings, include_etfs=False,
                 min_price=min_price, min_dollar_volume=min_dollar_volume)


def benchmarks(store) -> dict:
    """SPY's dividends (for the total-return benchmark) and the 3-month T-bill yield (for Sharpe ratios)."""
    dividends, bills = store.document("research:spy_dividends"), store.document("research:tbill3m")
    if not dividends or not bills:
        raise ValueError("SPY dividends and T-bill yields are not cached. Run research/benchmarks.py --refresh.")
    return {"spy_dividends": dividends["rows"], "tbill3m": bills["rows"]}


def earnings_dates(store, panel: Panel) -> dict[str, list[str]]:
    """Kept stock ticker -> sorted release dates, including those of renamed tickers merged into it."""
    groups = {str(s): [str(s)] for s, k in zip(panel.symbols, panel.kind) if k == "stock"}
    for d in panel.notes.get("duplicates", []):
        if d["keep"] in groups:
            groups[d["keep"]] += d["merged"]
    result = {}
    for keep, group in groups.items():
        dates = set()
        for symbol in group:
            key = cache_key(EARNINGS_PATH, clean_params({"symbol": symbol, "limit": EARNINGS_LIMIT}))
            rows = store.get_response(key, timedelta(days=36500)) or []
            dates.update(str(r["date"])[:10] for r in rows if r.get("date"))
        result[keep] = sorted(dates)
    return result


def classifications(store) -> dict[str, tuple[str, str]]:
    """Symbol -> (sector, industry): the stored universe, then delisted companies' profiles, then the fixes."""
    out = store.sectors()
    for key in store.document_keys(PROFILE):
        symbol, profile = key[len(PROFILE):], (store.document(key) or {}).get("profile")
        if profile and not out.get(symbol, ("", ""))[0]:
            out[symbol] = (profile.get("sector") or "", profile.get("industry") or "")
    out.update(SECTOR_FIXES)
    return out


def rebalance_rows(panel: Panel) -> list[int]:
    """The first session of each calendar quarter from 2016, when the quarterly strategies rebalance."""
    return [i for i, d in enumerate(panel.dates)
            if i and d >= STUDY_FROM and d[5:7] in ("01", "04", "07", "10") and panel.dates[i - 1][:7] != d[:7]]
