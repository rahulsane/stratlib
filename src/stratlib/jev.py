"""Optional, cached Jev chart judgments. No scoring or trading code reads these answers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .bases import weekly_bars
from .config import JevSettings, Thresholds, load_ai_gateway_api_key

log = logging.getLogger(__name__)
ENDPOINT = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
PROMPT_VERSION = 1


class JevError(RuntimeError):
    pass


def _change(current, previous):
    return 100 * (current / previous - 1) if current is not None and previous else None


def review_request(symbol, bars, base, as_of, thresholds: Thresholds, model, *, sessions=None):
    """Prepare dated JSON evidence and atomic Choice questions, without I/O or mutation."""
    if not base.get("available") or not base.get("pattern"):
        raise JevError("A detected, completed base is required for a Jev review.")
    weeks = weekly_bars(bars, as_of, sessions=sessions)
    selected = [w for w in weeks if base["start"] <= w.start and w.end <= base["end"]]
    if (not selected or selected[0].start != base["start"] or selected[-1].end != base["end"]
            or len(selected) != base["length_weeks"]):
        raise JevError("The completed weekly bars do not match this base. Recalculate the stock setup.")
    evidence = []
    previous = None
    for week in selected:
        avg_volume = week.volume / week.sessions if week.volume is not None else None
        previous_avg = (previous.volume / previous.sessions
                        if previous and previous.volume is not None else None)
        evidence.append({**asdict(week), "mean_daily_volume": avg_volume,
                         "close_change_pct": _change(week.close, previous.close) if previous else None,
                         "open_to_close_pct": _change(week.close, week.open),
                         "high_change_pct": _change(week.high, previous.high) if previous else None,
                         "low_change_pct": _change(week.low, previous.low) if previous else None,
                         "mean_daily_volume_change_pct": _change(avg_volume, previous_avg),
                         "segment": "handle" if base.get("handle_start") and week.start >= base["handle_start"] else "base"})
        previous = week
    instruction = ("Use the supplied computed measurements and weekly sequence for a qualitative judgment only. "
                   "Do not calculate returns, ratios, pivots, depths or durations, remeasure the chart, "
                   "or recommend trades. Choose uncertain when evidence is missing or mixed. ")
    questions = {
        "wedging": {"type": "choice", "instructions": instruction +
                    "Does the base's recovery show upward wedging on weakening volume support?",
                    "criteria": {"present": "A persistent upward recovery accompanied by weakening volume support.",
                                 "absent": "The recovery does not show that qualitative price-volume pattern.",
                                 "uncertain": "The evidence is insufficient or mixed."}},
    }
    if base.get("handle_start"):
        questions["handle_drift"] = {
            "type": "choice", "instructions": instruction +
            "Does the handle segment look like an orderly downward drift rather than erratic selling?",
            "criteria": {"orderly": "A controlled downward drift in the supplied handle sequence.",
                         "erratic": "Erratic selling rather than a controlled drift.",
                         "uncertain": "Too little evidence to distinguish the two."},
        }
        questions["handle_volume"] = {
            "type": "choice", "instructions": instruction +
            "Does the handle's supplied volume sequence support a convincing drying-up interpretation? "
            "Use mean daily volume to account for shortened weeks.",
            "criteria": {"drying": "Consistent contraction supports the drying-volume interpretation.",
                         "mixed": "The sequence does not convincingly support drying volume.",
                         "uncertain": "Too little evidence to judge the sequence."},
        }
    state = {"symbol": symbol, "as_of": as_of, "price_basis": "split-adjusted",
             "weekly_bars": evidence, "base_measurements": dict(base),
             "rule_thresholds": {k: v for k, v in asdict(thresholds).items()
                                 if k.startswith(("base_", "cup_", "handle_", "flat_", "double_bottom_", "buy_zone_", "breakout_"))},
             "evidence_note": "Completed weeks inside the detected base only. Null measurements are unavailable."}
    request = {"model": model, "state": state, "questions": questions}
    # Bound input well below the documented 32k state-plus-question context.
    if len(json.dumps(request, allow_nan=False).encode("utf-8")) > 90_000:
        raise JevError("This base exceeds the Jev review size limit. Reduce the base lookback and recalculate.")
    return request


def request_key(request):
    body = json.dumps({"prompt_version": PROMPT_VERSION, "request": request},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "jev:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _probability(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def validate_response(response, questions):
    """Reject incomplete or untyped answers instead of presenting them as judgments."""
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not response["model"]:
        raise JevError("Jev returned no model identity. Retry the review.")
    if not re.fullmatch(r"(?:typesafe-ai/)?jev(?:-\d+\.\d+\.\d+)?", response["model"]):
        raise JevError("Gateway returned an unexpected model. Check the Gateway routing configuration.")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("Jev returned an incomplete set of answers. Retry the review.")
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict):
            raise JevError("Jev returned an invalid Choice answer. Retry the review.")
        probs = answer.get("probabilities")
        if (answer.get("type") != "choice" or not isinstance(answer.get("choice"), str)
                or answer["choice"] not in question["criteria"]
                or not isinstance(probs, dict) or set(probs) != set(question["criteria"])
                or not all(_probability(p) for p in probs.values())
                or not math.isclose(sum(probs.values()), 1.0, abs_tol=0.001)
                or not _probability(answer.get("confidence"))
                or probs[answer["choice"]] < max(probs.values()) - 1e-9):
            raise JevError("Jev returned an invalid Choice distribution or confidence. Retry the review.")
    return answers


def returned_version(model):
    """An alias is not evidence of the underlying model version."""
    return model if isinstance(model, str) and re.fullmatch(r"(?:typesafe-ai/)?jev-\d+\.\d+\.\d+", model) else None


def _safe_response(value, key):
    """Exclude non-JSON numbers and redact echoed credentials before persistence."""
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_safe_response(v, key) for v in value]
    if isinstance(value, dict):
        return {str(k).replace(key, "[REDACTED]"): _safe_response(v, key) for k, v in value.items()}
    return value


def _retry_delay(response, attempt):
    delay = min(2 ** attempt, 30)
    if response is not None:
        value = response.headers.get("Retry-After", "")
        try:
            delay = max(delay, float(value))
        except ValueError:
            try:
                delay = max(delay, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    # A long requested delay is reported instead of retrying too early or hanging the GUI.
    return delay if math.isfinite(delay) and delay <= 60 else None


def review_base(store, options: JevSettings, request, *, refresh=False, api_key=None,
                session=None, sleep=time.sleep):
    """Cache exact evidence; append each run with rules, answers and response identity.

    Disabled mode exits before loading credentials or using the network. The
    endpoint is fixed to Vercel; redirects and model fallbacks are not requested.
    """
    if not options.enabled:
        raise JevError("Jev reviews are disabled. Set jev.enabled to true in config.yaml.")
    if request.get("model") != options.model:
        raise JevError("The review model changed. Recalculate the stock setup.")
    cache_key = request_key(request)
    cached = store.document(cache_key)
    if cached and not refresh:
        return {**cached, "cache_hit": True}
    key = api_key or load_ai_gateway_api_key()
    symbol = request["state"]["symbol"]
    run_id = store.start_run(f"jev:{symbol}", {"request_key": cache_key, "request": request,
                                             "prompt_version": PROMPT_VERSION})
    attempts = []
    http = session or requests.Session()
    try:
        for attempt in range(options.max_retries + 1):
            response = None
            try:
                response = http.post(ENDPOINT, headers={"Authorization": f"Bearer {key}"},
                                     json=request, timeout=options.timeout_seconds, allow_redirects=False)
            except requests.RequestException:
                attempts.append({"status": "network_error", "returned_model": None})
                error = "Jev could not reach Vercel AI Gateway. Check the connection and retry."
                retryable = True
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                model = body.get("model") if isinstance(body, dict) else None
                metadata = body.get("provider_metadata") if isinstance(body, dict) else None
                entry = _safe_response({"status": response.status_code, "returned_model": model,
                                        "returned_version": returned_version(model),
                                        "provider_metadata": metadata}, key)
                attempts.append(entry)
                log.info("Jev response run=%s attempt=%s identity=%s", run_id, attempt + 1, json.dumps(entry))
                if response.status_code == 200:
                    # Keep invalid response evidence in history, but never in the result cache.
                    entry["response"] = _safe_response(body, key)
                    answers = validate_response(body, request["questions"])
                    result = _safe_response({"run_id": run_id, "request_key": cache_key,
                                            "created_at": datetime.now(timezone.utc).isoformat(),
                                            "request": request, "response": body, "answers": answers,
                                            "returned_model": model, "returned_version": returned_version(model),
                                            "api_calls": len(attempts), "cache_hit": False}, key)
                    store.finish_run(run_id, {"status": "ok", "attempts": attempts, "result": result})
                    store.save_document(cache_key, result)
                    log.info("Jev advisory symbol=%s run=%s rules=%s answers=%s", symbol, run_id,
                             json.dumps(request["state"]["base_measurements"]), json.dumps(result["answers"]))
                    return result
                retryable = response.status_code == 429 or 500 <= response.status_code <= 599
                if response.status_code in (401, 403):
                    error = "Gateway authorization failed. Check AI_GATEWAY_API_KEY and your Gateway model access."
                elif response.status_code == 402:
                    error = "Gateway credits are unavailable. Check your Vercel AI Gateway balance."
                else:
                    error = f"Jev review failed with Gateway HTTP {response.status_code}. Check model access and retry."
            delay = _retry_delay(response, attempt)
            if not retryable or attempt == options.max_retries or delay is None:
                raise JevError(error)
            sleep(delay)
        raise JevError("Jev review did not complete.")
    except JevError as exc:
        store.finish_run(run_id, {"status": "error", "error": str(exc), "attempts": attempts})
        raise
    finally:
        if session is None:
            http.close()
