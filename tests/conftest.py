from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses as responses_lib

from stratlib.config import load_settings
from stratlib.fmp import FMPClient, SharedRateLimiter
from stratlib.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "fmp"
BASE = "https://financialmodelingprep.com/stable"
TEST_KEY = "test-key-not-a-real-fmp-key-0001"


def load_fixture(name: str):
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


def query_of(request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Tests never reach the live API: HTTP goes through `responses`, and any
    external socket connection fails loudly. Windows asyncio uses a local
    socket pair internally, so loopback connections stay allowed."""

    original_connect = socket.socket.connect

    def refuse(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address, *args, **kwargs)
        raise RuntimeError("tests must not open external network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setenv("FMP_API_KEY", TEST_KEY)


@pytest.fixture
def mocked():
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def limiter(tmp_path):
    # High enough that spacing between calls is negligible in tests.
    return SharedRateLimiter(tmp_path / "ratelimit.db", max_calls=1_000_000, period=60)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "stratlib.db")
    yield s
    s.close()


@pytest.fixture
def client(limiter, store):
    return FMPClient(TEST_KEY, limiter, cache=store, sleep=lambda seconds: None)


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
fmp:
  max_workers: 4
data:
  db_path: data/stratlib.db
  ratelimit_db_path: data/ratelimit.db
  log_dir: logs
universe:
  exchanges: [NYSE, NASDAQ, AMEX]
  screener_page_size: 1000
prices:
  history_years: 5
  market_symbols: ["^GSPC", "^IXIC", "SPY", "QQQ"]
""",
        encoding="utf-8",
    )
    return load_settings(config)
