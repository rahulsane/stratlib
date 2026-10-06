"""Earnings-driven statement refreshes. Completed bundles are durable."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .fmp import FMPClient, FMPRequestError
from .store import Store

STATEMENTS = (
    ("income_quarter", "income_statement", "quarter", 12),
    ("income_annual", "income_statement", "annual", 4),
    ("balance_quarter", "balance_sheet", "quarter", 4),
    ("balance_annual", "balance_sheet", "annual", 4),
    ("cash_quarter", "cash_flow", "quarter", 4),
    ("cash_annual", "cash_flow", "annual", 4),
)


def _retain_vintage(store: Store, symbol: str, bundle: dict) -> None:
    body = {"source": "FMP standardized statements, observed in the local cache", "bundle": bundle}
    previous = store.vintages("fundamentals", symbol)
    if previous and previous[-1]["body"] == body:
        return
    store.save_vintages([{"kind": "fundamentals", "item": symbol,
                          "observed_at": datetime.now(timezone.utc).isoformat(), "body": body}])


def refresh_due(cached: dict | None, symbol: str, earnings: list[dict], today: date) -> bool:
    if cached is None:
        return True
    fetched = date.fromisoformat(cached["fetched_on"])
    monday = today - timedelta(days=today.weekday())
    # Recheck once a day after an announcement this week, including the day
    # after it, because a calendar date does not guarantee statements arrived.
    return fetched < today and any(
        row.get("symbol") == symbol and monday.isoformat() <= str(row.get("date", ""))[:10] <= today.isoformat()
        for row in earnings
    )


def load_fundamentals(client: FMPClient | None, store: Store, symbol: str,
                      earnings: list[dict], today: date) -> dict | None:
    cached = store.document(f"fundamentals:{symbol}")
    if client is None:
        return cached
    if refresh_due(cached, symbol, earnings, today):
        result = {"fetched_on": today.isoformat(), "share_basis_date": today.isoformat()}
        for key, method, period, limit in STATEMENTS:
            # Daily HTTP cache also deduplicates partial/retried bundle loads.
            # Persistent bundles, rather than this TTL, control refresh policy.
            rows = getattr(client, method)(symbol, period, limit, cache_ttl=timedelta(hours=12))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise FMPRequestError(f"{symbol}: unexpected {key} response", path=method)
            result[key] = rows
        result["splits"] = client.splits(symbol)
        if not isinstance(result["splits"], list):
            raise FMPRequestError(f"{symbol}: unexpected split response", path="splits")
        store.save_document(f"fundamentals:{symbol}", result)
        _retain_vintage(store, symbol, result)
        return result
    # Splits can occur outside earnings week. This inexpensive reference call
    # has a shared 24-hour cache, while the six statement calls remain skipped.
    splits = client.splits(symbol)
    if not isinstance(splits, list):
        raise FMPRequestError(f"{symbol}: unexpected split response", path="splits")
    cached = {**cached, "splits": splits}
    store.save_document(f"fundamentals:{symbol}", cached)
    _retain_vintage(store, symbol, cached)
    return cached
