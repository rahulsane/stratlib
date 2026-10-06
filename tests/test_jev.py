"""Offline transport contracts and advisory isolation, using labeled fixtures."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import requests
import responses

from stratlib.bases import detect_base
from stratlib.config import ConfigError, JevSettings, Thresholds, load_ai_gateway_api_key, load_settings
from stratlib.jev import ENDPOINT, JevError, request_key, review_base, review_request, validate_response
from stratlib.prices import parse_bars
from stratlib.store import Store
from conftest import load_fixture
from test_bases import daily_from_weeks, synthetic_cup, synthetic_flat

KEY = "fake-gateway-key-for-offline-tests"
OPTIONS = JevSettings(enabled=True)


@pytest.fixture
def evidence():
    bars = daily_from_weeks(synthetic_cup())
    as_of = bars[-1].date
    base = detect_base(bars, as_of, Thresholds())
    return bars, base, as_of


@pytest.fixture
def request_body(evidence):
    bars, base, as_of = evidence
    return review_request("TEST", bars, base, as_of, Thresholds(), OPTIONS.model)


@pytest.fixture
def reply():
    return json.loads((Path(__file__).parent / "fixtures/jev/choice_response.json").read_text())


def test_old_configs_default_to_disabled_and_new_settings_are_parsed(settings):
    assert not settings.jev.enabled
    with settings.path.open("a") as f:
        f.write("\njev:\n  enabled: true\n  model: typesafe-ai/jev\n  max_retries: 1\n")
    assert load_settings(settings.path).jev == replace(OPTIONS, max_retries=1)


@pytest.mark.parametrize("values", [{"enabled": "false"}, {"model": "jev-latest"},
    {"model": "openai/other"}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 0},
    {"max_retries": -1}, {"max_retries": True}])
def test_invalid_config(values):
    with pytest.raises(ConfigError):
        JevSettings(**values)


def test_key_from_env_file_and_environment_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AI_GATEWAY_API_KEY=file-test-key\n")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    assert load_ai_gateway_api_key(env) == "file-test-key"
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "environment-test-key")
    assert load_ai_gateway_api_key(env) == "environment-test-key"
    monkeypatch.setenv("AI_GATEWAY_API_KEY", " ")
    with pytest.raises(ConfigError, match="not set"):
        load_ai_gateway_api_key(env)


def test_request_uses_precomputed_dated_evidence_without_mutation(evidence, request_body):
    bars, base, as_of = evidence
    before = copy.deepcopy(base)
    future = replace(bars[-1], date="2030-01-04", close=1000)
    other = review_request("TEST", [*bars, future], base, as_of, Thresholds(), OPTIONS.model)
    assert other == request_body
    assert base == before
    state = request_body["state"]
    assert len(state["weekly_bars"]) == 8
    handle = state["weekly_bars"][-1]
    assert handle["segment"] == "handle" and handle["mean_daily_volume"] == 500
    assert handle["mean_daily_volume_change_pct"] == -50
    assert handle["close_change_pct"] == pytest.approx(100 * (94 / 98 - 1))
    assert state["base_measurements"]["handle_volume_ratio"] == .5
    assert set(request_body["questions"]) == {"wedging", "handle_drift", "handle_volume"}


def test_recorded_fmp_bars_and_nonhandle_questions():
    bars = parse_bars(load_fixture("historical-price-eod_full_AAPL_2025-09-01.json"))
    base = detect_base(bars, "2025-12-26", Thresholds())
    body = review_request("AAPL", bars, base, "2025-12-26", Thresholds(), OPTIONS.model)
    assert base["pattern"] == "Flat base"
    assert set(body["questions"]) == {"wedging"}
    assert all(w["week_end"] <= "2025-12-26" for w in body["state"]["weekly_bars"])


def test_missing_base_or_missing_expected_week_is_not_sent(evidence):
    bars, base, as_of = evidence
    with pytest.raises(JevError, match="required"):
        review_request("TEST", bars, {"pattern": None}, as_of, Thresholds(), OPTIONS.model)
    with pytest.raises(JevError, match="do not match"):
        review_request("TEST", bars[1:], base, as_of, Thresholds(), OPTIONS.model,
                       sessions=[b.date for b in bars])


def test_disabled_never_loads_key_or_calls_network(store, request_body, monkeypatch):
    monkeypatch.setattr("stratlib.jev.load_ai_gateway_api_key", lambda: pytest.fail("loaded a key"))
    with pytest.raises(JevError, match="disabled"):
        review_base(store, JevSettings(), request_body)
    assert store.last_runs("jev:TEST") == []


def test_gateway_contract_cache_persistence_and_rule_isolation(store, mocked, request_body, reply, caplog):
    mocked.add(responses.POST, ENDPOINT, json=reply)
    original = copy.deepcopy(request_body)
    with caplog.at_level("INFO", logger="stratlib.jev"):
        result = review_base(store, OPTIONS, request_body, api_key=KEY)
    sent = mocked.calls[0].request
    assert json.loads(sent.body) == request_body
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in sent.url and "providerOptions" not in request_body
    assert request_body == original
    assert result["answers"]["handle_drift"]["choice"] == "erratic"
    assert result["returned_model"] == "typesafe-ai/jev" and result["returned_version"] is None
    assert result["api_calls"] == 1
    assert "answers=" in caplog.text and "rules=" in caplog.text and KEY not in caplog.text
    reopened = Store(store.db_path)
    try:
        # No key is needed for an exact cached request, even in a fresh process/store.
        cached = review_base(reopened, OPTIONS, request_body)
        assert cached["cache_hit"] and cached["run_id"] == result["run_id"]
        assert len(mocked.calls) == 1
        history = reopened.last_runs("jev:TEST")[0]
        assert history["args"]["request"] == original
        assert history["summary"]["result"]["response"] == reply
        assert KEY not in json.dumps(history)
    finally:
        reopened.close()


def test_refresh_keeps_previous_history_and_records_specific_returned_version(store, mocked, request_body, reply):
    mocked.add(responses.POST, ENDPOINT, json=reply)
    first = review_base(store, OPTIONS, request_body, api_key=KEY)
    reply["model"] = "jev-1.13.0"
    mocked.add(responses.POST, ENDPOINT, json=reply)
    second = review_base(store, OPTIONS, request_body, refresh=True, api_key=KEY)
    assert first["run_id"] != second["run_id"]
    assert second["returned_version"] == "jev-1.13.0"
    assert len(store.last_runs("jev:TEST")) == 2
    assert store.last_runs("jev:TEST")[1]["summary"]["result"] == first


@pytest.mark.parametrize("field", ["bar", "threshold", "base", "question", "model", "date"])
def test_cache_key_covers_all_evidence(request_body, field):
    other = copy.deepcopy(request_body)
    if field == "bar":
        other["state"]["weekly_bars"][-1]["volume"] += 1
    elif field == "threshold":
        other["state"]["rule_thresholds"]["handle_max_depth_pct"] = 8
    elif field == "base":
        other["state"]["base_measurements"]["pivot"] += 0.01
    elif field == "question":
        other["questions"]["wedging"]["instructions"] += " Revised."
    elif field == "model":
        other["model"] = "typesafe-ai/jev-1.13.0"
    else:
        other["state"]["as_of"] = "2026-09-29"
    assert request_key(other) != request_key(request_body)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_retries_respect_retry_after(store, mocked, request_body, reply, status):
    mocked.add(responses.POST, ENDPOINT, status=status, headers={"Retry-After": "3"})
    mocked.add(responses.POST, ENDPOINT, json=reply)
    waits = []
    result = review_base(store, OPTIONS, request_body, api_key=KEY, sleep=waits.append)
    assert waits == [3] and result["api_calls"] == 2


def test_network_errors_retry_without_leaking_exception(store, mocked, request_body, reply, caplog):
    mocked.add(responses.POST, ENDPOINT, body=requests.ConnectionError(KEY))
    mocked.add(responses.POST, ENDPOINT, json=reply)
    result = review_base(store, OPTIONS, request_body, api_key=KEY, sleep=lambda _: None)
    assert result["api_calls"] == 2
    assert KEY not in caplog.text and KEY not in json.dumps(store.last_runs("jev:TEST"))


@pytest.mark.parametrize("status", [302, 400, 401, 402, 403, 404, 422])
def test_nonretryable_errors_and_redirects_are_sanitized(store, mocked, request_body, status, caplog):
    mocked.add(responses.POST, ENDPOINT, status=status, json={"message": KEY},
               headers={"Location": "https://api.typesafe.ai/v1/systemone"})
    with pytest.raises(JevError) as exc:
        review_base(store, OPTIONS, request_body, api_key=KEY)
    assert len(mocked.calls) == 1 and KEY not in str(exc.value) and KEY not in caplog.text
    assert store.document(request_key(request_body)) is None
    assert store.last_runs("jev:TEST")[0]["summary"]["status"] == "error"


def test_long_retry_after_stops_instead_of_retrying_too_early(store, mocked, request_body):
    mocked.add(responses.POST, ENDPOINT, status=429, headers={"Retry-After": "120"})
    with pytest.raises(JevError):
        review_base(store, OPTIONS, request_body, api_key=KEY, sleep=lambda _: pytest.fail("slept"))
    assert len(mocked.calls) == 1


def test_retries_are_bounded(store, mocked, request_body):
    mocked.add(responses.POST, ENDPOINT, status=500)
    with pytest.raises(JevError):
        review_base(store, OPTIONS, request_body, api_key=KEY, sleep=lambda _: None)
    assert len(mocked.calls) == 3


@pytest.mark.parametrize("change", [
    {"choice": "unknown"}, {"choice": {}}, {"type": "noul"}, {"confidence": None},
    {"confidence": float("nan")}, {"confidence": True}, {"confidence": 1.1},
    {"probabilities": {}}, {"probabilities": {"present": .9, "absent": .9, "uncertain": .1}},
    {"probabilities": {"present": .1, "absent": .8, "uncertain": .1}},
])
def test_bad_typed_answers_fail_validation(request_body, reply, change):
    reply["answers"]["wedging"].update(change)
    with pytest.raises(JevError):
        validate_response(reply, request_body["questions"])


@pytest.mark.parametrize("body", [None, [], {}, {"model": "openai/other"}, {"model": "typesafe-ai/jev", "answers": {}}])
def test_malformed_envelopes(store, mocked, request_body, body):
    mocked.add(responses.POST, ENDPOINT, body=json.dumps(body), content_type="application/json")
    with pytest.raises(JevError):
        review_base(store, OPTIONS, request_body, api_key=KEY)
    assert store.document(request_key(request_body)) is None
    assert store.last_runs("jev:TEST")[0]["summary"]["status"] == "error"


def test_failed_refresh_preserves_cache_and_redacts_even_echoed_metadata(store, mocked, request_body, reply):
    mocked.add(responses.POST, ENDPOINT, json=reply)
    first = review_base(store, OPTIONS, request_body, api_key=KEY)
    reply["answers"]["wedging"]["confidence"] = float("nan")
    reply["provider_metadata"]["echo"] = KEY
    mocked.add(responses.POST, ENDPOINT, body=json.dumps(reply), content_type="application/json")
    with pytest.raises(JevError):
        review_base(store, OPTIONS, request_body, api_key=KEY, refresh=True)
    assert store.document(request_key(request_body)) == first
    assert KEY not in json.dumps(store.last_runs("jev:TEST"), allow_nan=False)


def test_recorded_live_apa_response_with_gateway_provider_retry(store, mocked):
    bars = parse_bars(load_fixture("cached-price-bars_APA_2026-09-25.json"))
    base = detect_base(bars, "2026-09-25", Thresholds())
    assert base["pattern"] == "Cup with handle" and base["length_weeks"] == 23
    request = review_request("APA", bars, base, "2026-09-25", Thresholds(), OPTIONS.model)
    recorded = json.loads((Path(__file__).parent / "fixtures/jev/recorded_APA_2026-09-29.json").read_text(encoding="utf-8"))
    mocked.add(responses.POST, ENDPOINT, json=recorded)
    result = review_base(store, OPTIONS, request, api_key=KEY)
    assert result["answers"] == recorded["answers"]
    assert result["returned_version"] is None
    assert result["api_calls"] == 1
    routing = result["response"]["provider_metadata"]["gateway"]["routing"]
    assert routing["totalProviderAttemptCount"] == 2
    assert routing["finalProvider"] == "typesafe-ai"
    assert review_base(store, OPTIONS, request)["cache_hit"]
    assert len(mocked.calls) == 1
    assert base == result["request"]["state"]["base_measurements"]
