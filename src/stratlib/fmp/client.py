"""Client for Financial Modeling Prep's stable API.

Endpoint paths and parameters follow FMP's docs at
https://site.financialmodelingprep.com/developer/docs/stable (checked
2026-09-28). The key travels in the ``apikey`` header, never in the URL, so
it cannot leak into logs, cache keys, or exception messages.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Protocol

import requests

from .errors import (
    FMPAuthError,
    FMPError,
    FMPPlanRestrictedError,
    FMPRequestError,
    FMPServerError,
)
from .ratelimit import SharedRateLimiter

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"

# How long a stored response is served instead of calling the API. Price
# history is not listed: the price store keeps its own per-symbol state.
TTL_UNIVERSE = timedelta(hours=20)
TTL_FUNDAMENTALS = timedelta(days=1)
TTL_CALENDAR = timedelta(hours=12)
TTL_REFERENCE = timedelta(days=1)

# Failures worth retrying: the request may succeed on a second attempt.
_NETWORK_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


class ResponseCache(Protocol):
    def get_response(self, key: str, max_age: timedelta) -> Any | None: ...

    def put_response(self, key: str, data: Any) -> None: ...


class CallStats:
    """Thread-safe counters for one client session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.api_calls = 0
        self.cache_hits = 0
        self.retries = 0

    def increment(self, name: str) -> int:
        with self._lock:
            value = getattr(self, name) + 1
            setattr(self, name, value)
            return value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "api_calls": self.api_calls,
                "cache_hits": self.cache_hits,
                "retries": self.retries,
            }


def _param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def clean_params(params: dict[str, Any] | None) -> dict[str, str]:
    return {k: _param_value(v) for k, v in (params or {}).items() if v is not None}


def cache_key(path: str, params: dict[str, str]) -> str:
    return path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


class FMPClient:
    def __init__(
        self,
        api_key: str,
        limiter: SharedRateLimiter,
        *,
        cache: ResponseCache | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_call: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.limiter = limiter
        self.cache = cache
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self.on_call = on_call
        self.stats = CallStats()
        self._session = session or requests.Session()
        self._session.headers["apikey"] = api_key

    # ------------------------------------------------------------------
    # Core request path

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, cache_ttl: timedelta | None = None
    ) -> Any:
        """GET a stable-API path and return the decoded JSON.

        With ``cache_ttl``, a stored response younger than the TTL is returned
        without calling the API, and fresh responses are stored.
        """
        path = path.strip("/")
        query = clean_params(params)
        key = cache_key(path, query)
        if cache_ttl is not None and self.cache is not None:
            cached = self.cache.get_response(key, cache_ttl)
            if cached is not None:
                self.stats.increment("cache_hits")
                log.debug("FMP cache hit %s", key)
                return cached
        data = self._request(path, query)
        if cache_ttl is not None and self.cache is not None:
            self.cache.put_response(key, data)
        return data

    def _request(self, path: str, query: dict[str, str]) -> Any:
        url = f"{self.base_url}/{path}"
        attempt = 0
        while True:
            self.limiter.acquire()
            n = self.stats.increment("api_calls")
            if n % 100 == 0:
                log.info("FMP API calls this session: %d", n)
            if self.on_call:
                self.on_call(self.stats.snapshot())
            try:
                resp = self._session.get(url, params=query, timeout=self.timeout)
            except _NETWORK_ERRORS as exc:
                log.debug("FMP call #%d %s %s -> %s", n, path, query, type(exc).__name__)
                reason = type(exc).__name__
                retry_after = None
                status = None
            else:
                log.debug("FMP call #%d %s %s -> %d", n, path, query, resp.status_code)
                status = resp.status_code
                if status == 200:
                    return self._decode(resp, path)
                if status != 429 and status < 500:
                    raise self._client_error(resp, path)
                reason = f"HTTP {status}"
                retry_after = _retry_after_seconds(resp)

            if attempt >= self.max_retries:
                raise FMPServerError(
                    f"{path}: gave up after {attempt + 1} attempts ({reason})",
                    path=path,
                    status=status,
                )
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            attempt += 1
            self.stats.increment("retries")
            log.warning(
                "FMP %s on %s; retry %d/%d in %.1fs", reason, path, attempt, self.max_retries, delay
            )
            self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_max, self.backoff_base * (2**attempt))
        return random.uniform(ceiling / 2, ceiling)

    @staticmethod
    def _decode(resp: requests.Response, path: str) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise FMPRequestError(f"{path}: response was not JSON", path=path, status=200) from None
        if isinstance(data, dict) and "Error Message" in data:
            raise FMPRequestError(f"{path}: {data['Error Message']}", path=path, status=200)
        return data

    @staticmethod
    def _client_error(resp: requests.Response, path: str) -> FMPError:
        status = resp.status_code
        detail = resp.text.strip().replace("\n", " ")[:300]
        if status == 402:
            return FMPPlanRestrictedError(
                f"{path}: not available on this data plan: {detail}", path=path, status=status
            )
        if status in (401, 403):
            return FMPAuthError(
                f"{path}: the data API rejected the key (HTTP {status}): {detail}", path=path, status=status
            )
        return FMPRequestError(f"{path}: HTTP {status}: {detail}", path=path, status=status)

    # ------------------------------------------------------------------
    # Endpoints

    def company_screener(
        self,
        *,
        exchange: str,
        is_etf: bool | None = False,
        is_fund: bool | None = False,
        is_actively_trading: bool | None = True,
        include_all_share_classes: bool = False,
        page: int = 0,
        limit: int = 1000,
        cache_ttl: timedelta | None = TTL_UNIVERSE,
    ) -> list[dict]:
        """Company Screener: symbol, companyName, sector, industry, price,
        volume, exchangeShortName, isEtf, isFund, isActivelyTrading, ..."""
        return self.get(
            "company-screener",
            {
                "exchange": exchange,
                "isEtf": is_etf,
                "isFund": is_fund,
                "isActivelyTrading": is_actively_trading,
                "includeAllShareClasses": include_all_share_classes,
                "page": page,
                "limit": limit,
            },
            cache_ttl=cache_ttl,
        )

    def historical_prices(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> list[dict]:
        """Split-adjusted daily OHLCV, newest first. At most 5,000 rows per call."""
        return self.get("historical-price-eod/full", {"symbol": symbol, "from": start, "to": end})

    def income_statement(
        self, symbol: str, period: str = "quarter", limit: int = 20,
        cache_ttl: timedelta | None = TTL_FUNDAMENTALS,
    ) -> list[dict]:
        return self.get(
            "income-statement", {"symbol": symbol, "period": period, "limit": limit},
            cache_ttl=cache_ttl,
        )

    def balance_sheet(
        self, symbol: str, period: str = "quarter", limit: int = 20,
        cache_ttl: timedelta | None = TTL_FUNDAMENTALS,
    ) -> list[dict]:
        return self.get(
            "balance-sheet-statement", {"symbol": symbol, "period": period, "limit": limit},
            cache_ttl=cache_ttl,
        )

    def cash_flow(
        self, symbol: str, period: str = "quarter", limit: int = 20,
        cache_ttl: timedelta | None = TTL_FUNDAMENTALS,
    ) -> list[dict]:
        return self.get(
            "cash-flow-statement", {"symbol": symbol, "period": period, "limit": limit},
            cache_ttl=cache_ttl,
        )

    def splits(self, symbol: str, cache_ttl: timedelta | None = TTL_REFERENCE) -> list[dict]:
        return self.get("splits", {"symbol": symbol}, cache_ttl=cache_ttl)

    def splits_calendar(
        self, start: date, end: date, cache_ttl: timedelta | None = TTL_CALENDAR
    ) -> list[dict]:
        return self.get("splits-calendar", {"from": start, "to": end}, cache_ttl=cache_ttl)

    def earnings_calendar(
        self, start: date, end: date, cache_ttl: timedelta | None = TTL_CALENDAR
    ) -> list[dict]:
        """FMP caps the range at 90 days and 4,000 rows per call."""
        return self.get("earnings-calendar", {"from": start, "to": end}, cache_ttl=cache_ttl)

    def delisted_companies(
        self, page: int = 0, limit: int = 100, cache_ttl: timedelta | None = TTL_REFERENCE
    ) -> list[dict]:
        return self.get(
            "delisted-companies", {"page": page, "limit": limit}, cache_ttl=cache_ttl
        )

    def profile(self, symbol: str, cache_ttl: timedelta | None = TTL_REFERENCE) -> list[dict]:
        return self.get("profile", {"symbol": symbol}, cache_ttl=cache_ttl)

    def batch_quote(self, symbols: Iterable[str]) -> list[dict]:
        return self.get("batch-quote", {"symbols": ",".join(symbols)})


def _retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
