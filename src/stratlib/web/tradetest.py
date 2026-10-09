"""TradeTest: ten blind daily charts to mark up and trade forward one bar at a time, with no going back.

The page is only a mount point: the browser app in assets/tradetest/ draws the charts, keeps the visitor's drawings
and trades, and writes the report. It reaches the bars through the four POST endpoints below, which hand them out
under sealed cursors (tradetest.py) and limit how often one address may call them. The public site is open to
anyone, so every request is checked and every refusal is a plain JSON error, never a server error. The endpoints take
only application/json, which a page on another site cannot send without a preflight that fails, so other sites
cannot spend a visitor's budget.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from nicegui import ui
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..tradetest import TradeTestError, TradeTestService, bad_request
from .data import Data, Workspace

log = logging.getLogger("stratlib.web")

ASSETS = Path(__file__).with_name("assets") / "tradetest"
ASSET_URL = "/tradetest-assets/"
BANK_ENV = "STRATLIB_TRADETEST_BANK"
MAX_BODY = 8192
NO_STORE = {"Cache-Control": "no-store"}
# Requests one address may make: (most, per seconds). Bars and steps share one budget.
LIMITS = {"new": (60, 3600), "reveal": (150, 3600), "replay": (40, 1)}
LIMIT_MESSAGES = {
    "new": "You’ve started many sets from this connection in the last hour. Try again in {wait}.",
    "reveal": "You’ve finished many charts from this connection in the last hour. Try again in {wait}.",
    "replay": "Too many requests at once. Wait a moment, then try again.",
}
MAX_CLIENTS = 10_000
FIELD_MESSAGES = {"avoid": "The list of recent charts must be text or null.",
                  "cursor": "Send the chart’s cursor as text.",
                  "n": "Step forward by a whole number of bars from 1 to 20."}

_services: dict[Path, TradeTestService] = {}
_services_lock = threading.Lock()
_build: tuple[tuple, str] | None = None


def bank_path(settings: Settings) -> Path:
    """public/tradetest_bank.npz beside config.yaml, unless STRATLIB_TRADETEST_BANK names another file."""
    override = os.environ.get(BANK_ENV)
    return Path(override) if override else settings.path.parent / "public" / "tradetest_bank.npz"


def service_for(settings: Settings) -> TradeTestService:
    """One service per bank file, shared by the page and the API, so the bank is read into memory once."""
    path = bank_path(settings).resolve()
    with _services_lock:
        if path not in _services:
            _services[path] = TradeTestService(path)
        return _services[path]


def build_hash() -> str:
    """A short hash of the browser app's files, so a new release is never served from a stale cache. Files that do
    not exist yet are simply left out."""
    global _build
    files = sorted(p for p in ASSETS.iterdir() if p.is_file()) if ASSETS.is_dir() else []
    signature = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _build is None or _build[0] != signature:
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
        _build = (signature, digest.hexdigest()[:10])
    return _build[1]


# Rate limiting ----------------------------------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window request counts per client address, in memory. Addresses are kept in order of last use, so
    idle ones are pruned from the front; past MAX_CLIENTS the longest unused is forgotten."""

    def __init__(self, limits: dict[str, tuple[int, float]] | None = None, *, clock=time.monotonic,
                 max_clients: int = MAX_CLIENTS):
        self.limits, self.clock, self.max_clients = LIMITS if limits is None else limits, clock, max_clients
        self._clients: OrderedDict[str, dict[str, deque]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, client: str, bucket: str) -> float:
        """Count one request and return 0, or refuse it and return the seconds until the oldest counted one expires."""
        most, seconds = self.limits[bucket]
        now = self.clock()
        with self._lock:
            self._prune(now)
            buckets = self._clients.get(client)
            if buckets is None:
                while len(self._clients) >= self.max_clients:
                    self._clients.popitem(last=False)
                buckets = self._clients[client] = {}
            self._clients.move_to_end(client)
            times = buckets.setdefault(bucket, deque())
            while times and times[0] <= now - seconds:
                times.popleft()
            if len(times) >= most:
                return times[0] + seconds - now
            times.append(now)
            return 0.0

    def allow(self, client: str, bucket: str) -> bool:
        return self.take(client, bucket) == 0

    def _prune(self, now: float) -> None:
        while self._clients:
            client, buckets = next(iter(self._clients.items()))
            if any(times and times[-1] > now - self.limits[name][1] for name, times in buckets.items()):
                return
            del self._clients[client]

    def __len__(self) -> int:
        return len(self._clients)


def client_address(request: Request) -> str:
    """The visitor's address. Behind the reverse proxy every request comes from loopback, and the proxy appends the
    address it saw to X-Forwarded-For, so the right-most entry is the one to trust."""
    host = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if loopback:
        forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
        if forwarded:
            return forwarded[:64]
    return host or "unknown"


# The API ----------------------------------------------------------------------------------------------------------

def sends_json(request: Request) -> bool:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json"


def rate_limited(bucket: str, wait: float) -> JSONResponse:
    seconds = max(1, math.ceil(wait))
    minutes = max(1, math.ceil(seconds / 60))
    message = LIMIT_MESSAGES[bucket].format(wait="1 minute" if minutes == 1 else f"{minutes} minutes")
    return JSONResponse({"error": "rate_limited", "message": message}, status_code=429,
                        headers={**NO_STORE, "Retry-After": str(seconds)})


async def read_json(request: Request) -> dict:
    """The request body as a JSON object; an empty body is an empty object. Anything else, nesting too deep for the
    parser included, is a bad request."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise bad_request("The request is too large.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise bad_request("Send the request as a JSON object.") from None
    if not isinstance(body, dict):
        raise bad_request("Send the request as a JSON object.")
    return body


def field(body: dict, name: str, kind: type, *, optional: bool = False):
    value = body.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, kind) or isinstance(value, bool):
        raise bad_request(FIELD_MESSAGES[name])
    return value


def api_router(service: TradeTestService, limiter: RateLimiter | None = None) -> APIRouter:
    """POST /api/tradetest/new, /bars, /step and /reveal over one service."""
    limiter = RateLimiter() if limiter is None else limiter
    router = APIRouter()

    def route(name: str, bucket: str, handle):
        async def endpoint(request: Request):
            # The content type is checked before the budget, so a request no page of ours sends never spends it.
            if not sends_json(request):
                return JSONResponse({"error": "bad_request", "message": "Send the request as JSON."},
                                    status_code=415, headers=NO_STORE)
            try:
                wait = limiter.take(client_address(request), bucket)
                if wait:
                    return rate_limited(bucket, wait)
                body = await read_json(request)
                result = await run_in_threadpool(handle, body)
            except TradeTestError as exc:
                return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status,
                                    headers=NO_STORE)
            except Exception:
                log.exception("TradeTest %s failed", name)
                return JSONResponse({"error": "server", "message": "Something went wrong. Try again in a moment."},
                                    status_code=500, headers=NO_STORE)
            return JSONResponse(result, headers=NO_STORE)
        router.add_api_route(f"/api/tradetest/{name}", endpoint, methods=["POST"], include_in_schema=False)

    route("new", "new", lambda body: service.new_set(field(body, "avoid", str, optional=True)))
    route("bars", "replay", lambda body: service.bars(field(body, "cursor", str)))
    route("step", "replay", lambda body: service.step(field(body, "cursor", str), field(body, "n", int)))
    route("reveal", "reveal", lambda body: service.reveal(field(body, "cursor", str)))
    return router


# The page ---------------------------------------------------------------------------------------------------------

class TradeTestPage:
    """One tab's TradeTest page: the browser app's mount point, or why it cannot open."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace = data, workspace

    def build(self):
        if service_for(self.data.settings).available:
            ui.html(f'<div class="tt-root" data-tradetest data-assets="{ASSET_URL}" data-build="{build_hash()}"></div>'
                    "<noscript>TradeTest needs JavaScript to draw its charts.</noscript>", sanitize=False)
            return
        with ui.element("section").classes("d-head"):
            ui.html("TradeTest", sanitize=False, tag="h1").classes("d-title")
        ui.html("<h2>TradeTest is not available right now</h2>" + (
            "<p>Its practice charts could not be loaded. Please check back later.</p>" if self.data.public else
            '<p>Build its chart bank from the database first:</p><pre class="d-code">.venv\\Scripts\\stratlib '
            "tradetest-bank</pre>"), sanitize=False).classes("d-card d-empty")
