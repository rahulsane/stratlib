"""Wiring shared by the CLI and the GUI: settings, database, API client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings, load_fmp_api_key, load_settings
from .fmp import FMPClient, SharedRateLimiter
from .store import Store


@dataclass
class AppContext:
    settings: Settings
    store: Store
    client: FMPClient
    limiter: SharedRateLimiter

    def close(self) -> None:
        self.store.close()


def open_context(
    config_path: str | Path | None = None,
    *,
    api_key: str | None = None,
    settings: Settings | None = None,
) -> AppContext:
    settings = settings or load_settings(config_path)
    store = Store(settings.data.db_path)
    limiter = SharedRateLimiter(settings.data.ratelimit_db_path, settings.fmp.calls_per_minute)
    fmp = settings.fmp
    client = FMPClient(
        api_key or load_fmp_api_key(),
        limiter,
        cache=store,
        base_url=fmp.base_url,
        timeout=fmp.timeout_seconds,
        max_retries=fmp.max_retries,
        backoff_base=fmp.backoff_base_seconds,
        backoff_max=fmp.backoff_max_seconds,
    )
    return AppContext(settings=settings, store=store, client=client, limiter=limiter)
