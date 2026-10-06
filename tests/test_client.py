import io
import logging
from datetime import date, timedelta

import pytest
import requests

from stratlib.fmp import (
    FMPAuthError,
    FMPClient,
    FMPPlanRestrictedError,
    FMPRequestError,
    FMPServerError,
)
from stratlib.logging_setup import RedactSecrets
from stratlib.store import Store

from conftest import BASE, TEST_KEY, load_fixture, query_of


def test_key_travels_in_header_not_url(mocked, client):
    mocked.get(f"{BASE}/quote", json=load_fixture("quote_AAPL.json"))

    data = client.get("quote", {"symbol": "AAPL"})

    assert data[0]["symbol"] == "AAPL"
    request = mocked.calls[0].request
    assert request.headers["apikey"] == TEST_KEY
    assert TEST_KEY not in request.url


def test_params_use_fmp_formats(mocked, client):
    mocked.get(f"{BASE}/company-screener", json=[])
    mocked.get(f"{BASE}/historical-price-eod/full", json=[])

    client.company_screener(exchange="NYSE", cache_ttl=None)
    client.historical_prices("AAPL", date(2026, 1, 2), date(2026, 3, 31))

    screener = query_of(mocked.calls[0].request)
    assert screener["isEtf"] == "false"
    assert screener["isActivelyTrading"] == "true"
    assert screener["exchange"] == "NYSE"
    prices = query_of(mocked.calls[1].request)
    assert prices == {"symbol": "AAPL", "from": "2026-01-02", "to": "2026-03-31"}


def test_retries_429_and_5xx_then_succeeds(mocked, limiter):
    sleeps = []
    client = FMPClient(TEST_KEY, limiter, sleep=sleeps.append, max_retries=5)
    url = f"{BASE}/historical-price-eod/full"
    mocked.get(url, status=429, headers={"Retry-After": "2"})
    mocked.get(url, status=503)
    mocked.get(url, json=load_fixture("historical-price-eod_full_SPY_2026-08-01.json"))

    rows = client.historical_prices("SPY")

    assert rows[0]["symbol"] == "SPY"
    assert sleeps[0] == 2.0  # Retry-After is honoured
    assert 0 < sleeps[1] <= 2.0  # exponential backoff, attempt 2
    assert client.stats.snapshot() == {"api_calls": 3, "cache_hits": 0, "retries": 2}


def test_retries_network_errors(mocked, limiter):
    client = FMPClient(TEST_KEY, limiter, sleep=lambda s: None)
    url = f"{BASE}/quote"
    mocked.get(url, body=requests.ConnectionError("connection reset"))
    mocked.get(url, json=load_fixture("quote_AAPL.json"))

    assert client.get("quote", {"symbol": "AAPL"})[0]["symbol"] == "AAPL"
    assert client.stats.retries == 1


def test_gives_up_after_max_retries(mocked, limiter):
    sleeps = []
    client = FMPClient(TEST_KEY, limiter, sleep=sleeps.append, max_retries=2, backoff_max=5)
    mocked.get(f"{BASE}/quote", status=500)

    with pytest.raises(FMPServerError, match="gave up after 3 attempts"):
        client.get("quote", {"symbol": "AAPL"})
    assert client.stats.api_calls == 3
    assert len(sleeps) == 2 and all(s <= 5 for s in sleeps)


@pytest.mark.parametrize(
    "fixture",
    ["error_402_restricted_endpoint.txt", "error_402_limit_parameter.txt", "error_402_symbol.txt"],
)
def test_402_is_a_plan_restriction_and_not_retried(mocked, client, fixture):
    mocked.get(f"{BASE}/batch-exchange-quote", status=402, body=load_fixture(fixture))

    with pytest.raises(FMPPlanRestrictedError) as err:
        client.get("batch-exchange-quote", {"exchange": "AMEX"})
    assert err.value.status == 402
    assert "subscription" in str(err.value)
    assert client.stats.api_calls == 1


def test_401_is_an_auth_error(mocked, client):
    mocked.get(f"{BASE}/quote", status=401, json={"Error Message": "Invalid API KEY."})

    with pytest.raises(FMPAuthError):
        client.get("quote", {"symbol": "AAPL"})


def test_error_message_in_a_200_body_raises(mocked, client):
    mocked.get(f"{BASE}/quote", json={"Error Message": "Something went wrong."})

    with pytest.raises(FMPRequestError, match="Something went wrong"):
        client.get("quote", {"symbol": "AAPL"})


def test_repeat_requests_are_served_from_cache(mocked, client):
    mocked.get(f"{BASE}/company-screener", json=load_fixture("company-screener_NYSE.json"))

    first = client.company_screener(exchange="NYSE")
    second = client.company_screener(exchange="NYSE")

    assert first == second
    assert len(mocked.calls) == 1
    assert client.stats.snapshot() == {"api_calls": 1, "cache_hits": 1, "retries": 0}


def test_cached_responses_expire(mocked, limiter, tmp_path):
    now = [1_800_000_000.0]
    store = Store(tmp_path / "db.sqlite", clock=lambda: now[0])
    client = FMPClient(TEST_KEY, limiter, cache=store, sleep=lambda s: None)
    mocked.get(f"{BASE}/splits", json=load_fixture("splits_NVDA.json"))

    client.splits("NVDA", cache_ttl=timedelta(hours=1))
    now[0] += 3601
    client.splits("NVDA", cache_ttl=timedelta(hours=1))

    assert len(mocked.calls) == 2
    store.close()


def test_logs_running_call_count_without_the_key(mocked, client, caplog):
    mocked.get(f"{BASE}/quote", json=load_fixture("quote_AAPL.json"))
    caplog.set_level(logging.DEBUG, logger="stratlib")

    for _ in range(100):
        client.get("quote", {"symbol": "AAPL"})

    assert "FMP API calls this session: 100" in caplog.text
    assert "FMP call #57 quote" in caplog.text
    assert TEST_KEY not in caplog.text


def test_redaction_filter_scrubs_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecrets([TEST_KEY]))
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("url was https://x/?apikey=%s", TEST_KEY)
        try:
            raise ValueError(f"bad key {TEST_KEY}")
        except ValueError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert TEST_KEY not in output
    assert output.count("[REDACTED]") == 2
