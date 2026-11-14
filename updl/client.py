"""Read-only HTTP client for the Upstox GET endpoints and the three public instrument-master files.

Rules enforced here:
* only paths in ALLOWED_PATH_TEMPLATES (and the three master URLs) can be requested;
* one thread-safe rate limiter (calls/min token bucket + rolling 30-minute cap) shared by every worker;
* retries with documented backoff, a circuit breaker for repeated 429, no token in any message.
"""
from __future__ import annotations

import gzip
import json
import random
import re
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .errors import (ApiError, BudgetExceeded, NotAllowedPath, PlusRequired, RetryExhausted, TokenExpired,
                     TokenNotPermitted, TransportError)
from .messages import TOKEN_EXPIRED, TOKEN_MISSING
from .tokens import Tokens

BASE_URL = "https://api.upstox.com"

ALLOWED_PATH_TEMPLATES = (
    "/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}",
    "/v2/expired-instruments/expiries",
    "/v2/expired-instruments/option/contract",
    "/v2/expired-instruments/future/contract",
    "/v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}",
    "/v2/instruments/search",
    # v3: live option chain for the Recorder (read-only GET, Analytics Token supported). Added on purpose:
    # Upstox has no historical chain endpoint, so bid/ask/IV/greeks can only be saved live. See DECISIONS.md.
    "/v2/option/chain",
    # v4: exchange calendar straight from Upstox (read-only GET; verified live 14 Nov 2026). DECISIONS 19.
    "/v2/market/holidays",
    "/v2/market/holidays/{date}",
    "/v2/market/timings/{date}",
    # v4.2: exchange-side cross-check data that Upstox keeps for about two weeks only (updl/marketinfo.py) and
    # the live LTP quote for the Recorder. All read-only GET market data; paths read in the official SDK and
    # called live on 14 Nov 2026. DECISIONS 31.
    "/v2/market/pcr",
    "/v2/market/max-pain",
    "/v2/market/oi",
    "/v2/market/change-oi",
    "/v2/market/fii",
    "/v2/market/dii",
    "/v3/market-quote/ltp",
)
MASTER_URLS = {
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
    "global": "https://assets.upstox.com/market-quote/instruments/exchange/global.json.gz",
}

BACKOFF_SECONDS = (2, 4, 8, 16, 32, 60)
JITTER_FRACTION = 0.25
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
CIRCUIT_PAUSE_SECONDS = 600
HEADER_HINTS = ("rate", "limit", "retry", "quota")
PLUS_REQUIRED_CODE = "UDAPI1149"        # "available exclusively with an Upstox Plus plan subscription"
TOKEN_NOT_PERMITTED_CODE = "UDAPI100067"  # token type not allowed on this endpoint (community report, 2026-06)


def _raise_for_code(status: int, code: str, text: str):
    """Map documented error codes to specific exceptions; otherwise a plain ApiError."""
    if code == PLUS_REQUIRED_CODE:
        raise PlusRequired(status, code, text)
    if code == TOKEN_NOT_PERMITTED_CODE:
        raise TokenNotPermitted(status, code, text)
    raise ApiError(status, code, text)


def _template_regex(template: str):
    parts = re.split(r"(\{[a-z_]+\})", template)
    rx = "".join("[^/?]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + rx + "$")


_ALLOWED_REGEXES = [_template_regex(t) for t in ALLOWED_PATH_TEMPLATES]


def quote_key(key: str) -> str:
    """Pipe becomes %7C and a space becomes %20."""
    return urllib.parse.quote(key, safe="")


def path_allowed(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return any(rx.match(bare) for rx in _ALLOWED_REGEXES)


def build_query(params: Optional[dict]) -> str:
    if not params:
        return ""
    return "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")


def v3_candle_path(instrument_key, unit, interval, to_date, from_date) -> str:
    return f"/v3/historical-candle/{quote_key(instrument_key)}/{unit}/{interval}/{to_date}/{from_date}"


def expired_candle_path(expired_key, interval, to_date, from_date) -> str:
    return f"/v2/expired-instruments/historical-candle/{quote_key(expired_key)}/{interval}/{to_date}/{from_date}"


@dataclass
class RawResponse:
    status_code: int
    headers: dict
    content: bytes


@dataclass
class Result:
    status: int
    body: bytes
    json: dict
    path: str
    header_hints: dict = field(default_factory=dict)


class RequestsTransport:
    """Real network transport. Every failure is reduced to an error-class name: no URL, no header."""

    def __init__(self):
        import requests

        self._requests = requests
        self.session = requests.Session()   # keep-alive: a new connection per call cost ~340 ms vs ~47 ms (live)
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)

    def get(self, url, headers=None, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) -> RawResponse:
        try:
            r = self.session.get(url, headers=headers or {}, timeout=timeout)
        except self._requests.exceptions.Timeout:
            raise TransportError("Timeout") from None
        except self._requests.exceptions.RequestException as exc:
            raise TransportError(type(exc).__name__) from None
        return RawResponse(r.status_code, {k: v for k, v in r.headers.items()}, r.content)


def estimate_minutes(calls: int, rate_per_min: float, max_per_30min: int, used_in_window: int = 0) -> float:
    """Honest run-time estimate: calls go out at rate_per_min until the rolling 30-minute cap is full, and
    after that only as fast as the cap refills (max_per_30min per 30 minutes)."""
    calls = max(0, int(calls))
    rate = max(1.0, float(rate_per_min))
    cap = max(1, int(max_per_30min))
    free = max(0, cap - max(0, int(used_in_window)))
    fast = min(calls, free)
    return fast / rate + (calls - fast) / min(rate, cap / 30.0)


class RateLimiter:
    """Calls per minute (token bucket with burst) plus a rolling 30-minute cap. Thread-safe: every worker
    thread shares this one object, so the account-wide limits hold whatever the worker count."""

    def __init__(self, calls_per_min=30.0, max_per_30min=900, burst=3, clock=time.monotonic, sleep=time.sleep):
        self.calls_per_min = float(calls_per_min)
        self.ceiling_per_min = float(calls_per_min)   # the slow ramp back after a 429 never passes this
        self.max_per_30min = int(max_per_30min)
        self.burst = float(burst)
        self.clock = clock
        self.sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._window = deque()
        self._lock = threading.Lock()
        self._pause_until = 0.0
        self._last_ramp = clock()
        self.waiting = ""            # "" | "RATE-CAP" | "PAUSE-429": read by the progress line
        self.wait_until = 0.0

    def halve(self):
        with self._lock:
            self.calls_per_min = max(1.0, self.calls_per_min / 2.0)
            self._last_ramp = self.clock()

    def pause(self, seconds: float):
        """Make every thread wait (used after a 429 so that nobody keeps hammering)."""
        with self._lock:
            self._pause_until = max(self._pause_until, self.clock() + float(seconds))

    def window_used(self) -> int:
        with self._lock:
            now = self.clock()
            while self._window and now - self._window[0] >= 1800:
                self._window.popleft()
            return len(self._window)

    def recent_ages(self) -> list:
        """Ages (seconds) of the calls still inside the 30-minute window; saved between runs."""
        with self._lock:
            now = self.clock()
            return [round(now - t, 1) for t in self._window if now - t < 1800]

    def preload(self, ages) -> None:
        """Count calls made by an earlier process (ages in seconds) against the rolling 30-minute cap."""
        with self._lock:
            now = self.clock()
            old = sorted((now - float(a) for a in ages if 0 <= float(a) < 1800))
            self._window = deque(sorted(list(self._window) + old))

    def acquire(self):
        paused_once = False
        while True:
            with self._lock:
                now = self.clock()
                if self.calls_per_min < self.ceiling_per_min and now - self._last_ramp >= 300:
                    self.calls_per_min = min(self.ceiling_per_min, self.calls_per_min * 1.25)   # slow ramp back
                    self._last_ramp = now
                pause = self._pause_until - now
                if pause > 0 and not paused_once:
                    self.waiting, self.wait_until = "PAUSE-429", self._pause_until
                else:
                    pause = 0.0
                    self._tokens = min(self.burst, self._tokens + (now - self._last) * self.calls_per_min / 60.0)
                    self._last = now
                    while self._window and now - self._window[0] >= 1800:
                        self._window.popleft()
                    wait, why = 0.0, ""
                    if self._tokens < 1.0:
                        wait = (1.0 - self._tokens) * 60.0 / self.calls_per_min
                    if len(self._window) >= self.max_per_30min:
                        cap_wait = 1800 - (now - self._window[0]) + 0.001
                        if cap_wait > wait:
                            wait, why = cap_wait, "RATE-CAP"
                    if wait <= 0:
                        self._tokens -= 1.0
                        self._window.append(now)
                        self.waiting = ""
                        return
                    self.waiting, self.wait_until = why, now + wait
            if pause > 0:
                paused_once = True      # sleep a 429 pause once per call (a fake test sleep must not loop for ever)
                self.sleep(pause)
            else:
                self.sleep(wait)


def _parse_error(body: bytes):
    code, text = "", ""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        errs = data.get("errors") or []
        if errs and isinstance(errs, list) and isinstance(errs[0], dict):
            code = str(errs[0].get("errorCode", ""))
            text = str(errs[0].get("message", ""))
        elif isinstance(data.get("message"), str):
            text = data["message"]
    except (ValueError, AttributeError):
        text = body[:120].decode("utf-8", "replace")
    return code, text


class HttpClient:
    def __init__(self, tokens: Optional[Tokens] = None, transport=None, limiter: Optional[RateLimiter] = None,
                 budget: Optional[int] = None, sleep=time.sleep, rng=random.random, logger=None,
                 max_retries: int = len(BACKOFF_SECONDS), base_url: str = BASE_URL,
                 analytics_ok_for_expired: bool = False):
        self.tokens = tokens or Tokens()
        self.transport = transport or RequestsTransport()
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self.budget = budget
        self.sleep = sleep
        self.rng = rng
        self.logger = logger
        self.max_retries = max_retries
        self.base_url = base_url
        self.analytics_ok_for_expired = analytics_ok_for_expired
        self._lock = threading.Lock()
        self.retries = 0             # attempts after the first one of a request
        self.count_429 = 0
        self.calls_made = 0          # every API attempt, retries included (master files are counted apart)
        self.file_downloads = 0
        self.consecutive_429 = 0
        self.circuit_pauses = 0
        self.header_log = {}         # header name -> list of distinct values (rate-limit-like only)
        self.longest_ok_streak = 0
        self._streak = 0
        self.seen_paths = []         # every path requested (used by the allowlist test)

    # ---- helpers -----------------------------------------------------------
    def _log(self, event, **kw):
        if self.logger:
            self.logger(event, **kw)

    def _hints(self, headers: dict) -> dict:
        out = {}
        for name, value in (headers or {}).items():
            if any(h in name.lower() for h in HEADER_HINTS):
                out[name] = str(value)[:80]
                vals = self.header_log.setdefault(name, [])
                if str(value)[:80] not in vals and len(vals) < 20:
                    vals.append(str(value)[:80])
        return out

    def _count_call(self, is_file=False):
        with self._lock:
            if is_file:
                self.file_downloads += 1       # public master files are not API calls
                return
            if self.budget is not None and self.calls_made >= self.budget:
                raise BudgetExceeded("call budget used up")
            self.calls_made += 1

    def _backoff_delay(self, attempt_index: int, retry_after: Optional[float] = None) -> float:
        base = BACKOFF_SECONDS[min(attempt_index, len(BACKOFF_SECONDS) - 1)]
        delay = base * (1.0 + JITTER_FRACTION * self.rng())
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    def _backoff(self, attempt_index: int, retry_after: Optional[float] = None, hold_all: bool = False):
        delay = self._backoff_delay(attempt_index, retry_after)
        if hold_all:
            self.limiter.pause(delay)      # a 429 holds every worker, not only this one
        self.sleep(delay)

    def _auth_headers(self, kind: str) -> dict:
        headers = {"Accept": "application/json"}
        if kind == "expired_analytics":      # probe P16 only: force the analytics token on an expired endpoint
            token = self.tokens.analytics
        else:
            token = self.tokens.for_kind(kind, self.analytics_ok_for_expired)
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    # ---- main entry --------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None, kind: str = "v3") -> Result:
        """GET one allowed endpoint. kind: 'v3' (analytics token first) or 'expired' (access token)."""
        full_path = path + build_query(params)
        if not path_allowed(path):
            raise NotAllowedPath("path is not in the allowlist")
        self.seen_paths.append(path.split("?", 1)[0])
        url = self.base_url + full_path
        return self._request(url, full_path, self._auth_headers(kind), want_json=True)

    def download_file(self, url: str) -> bytes:
        """Download one of the three public instrument-master files (no token)."""
        if url not in MASTER_URLS.values():
            raise NotAllowedPath("url is not an instrument-master file")
        self.seen_paths.append(url)
        res = self._request(url, url, {"Accept": "*/*"}, want_json=False, is_file=True)
        return res.body

    def _request(self, url, shown_path, headers, want_json, is_file=False) -> Result:
        last_error = "unknown"
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.retries += 1
            self._count_call(is_file)
            self.limiter.acquire()
            try:
                resp = self.transport.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            except TransportError as exc:
                last_error = f"transport:{exc}"
                self._streak = 0
                self._log("retry", reason=last_error, attempt=attempt + 1)
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            status = resp.status_code
            hints = self._hints(resp.headers)
            if status == 401:
                # v3: read the error code first. UDAPI1149 (Plus required) has been reported with HTTP 401,
                # and treating it as an expired token aborted the probe instead of falling back to Plan B.
                code, text = _parse_error(resp.content)
                if code in (PLUS_REQUIRED_CODE, TOKEN_NOT_PERMITTED_CODE):
                    _raise_for_code(status, code, text)
                sent_token = any(k.lower() == "authorization" for k in (headers or {}))
                raise TokenExpired(TOKEN_EXPIRED if sent_token else TOKEN_MISSING)
            if status == 429:
                self.count_429 += 1
                self.consecutive_429 += 1
                self._streak = 0
                last_error = "http:429"
                self._log("retry", reason="http:429", attempt=attempt + 1)
                if self.consecutive_429 >= 3:
                    self.circuit_pauses += 1
                    self._log("circuit_breaker", pause_seconds=CIRCUIT_PAUSE_SECONDS)
                    self.limiter.pause(CIRCUIT_PAUSE_SECONDS)
                    self.sleep(CIRCUIT_PAUSE_SECONDS)
                    self.limiter.halve()
                    self.consecutive_429 = 0
                elif attempt < self.max_retries:
                    ra = None
                    for name, value in (resp.headers or {}).items():
                        if name.lower() == "retry-after":
                            try:
                                ra = float(value)
                            except ValueError:
                                ra = None
                    self._backoff(attempt, ra, hold_all=True)
                continue
            self.consecutive_429 = 0
            if status >= 500:
                code, text = _parse_error(resp.content)
                last_error = f"http:{status}:{code}"
                self._streak = 0
                self._log("retry", reason=last_error, attempt=attempt + 1)
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            if status >= 400:
                code, text = _parse_error(resp.content)
                _raise_for_code(status, code, text)
            if not want_json:
                self._streak += 1
                self.longest_ok_streak = max(self.longest_ok_streak, self._streak)
                return Result(status, resp.content, {}, shown_path, hints)
            try:
                data = json.loads(resp.content.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                last_error = "bad_json"
                self._streak = 0
                self._log("retry", reason=last_error, attempt=attempt + 1)
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue
            if data.get("status") == "error":
                code, text = _parse_error(resp.content)
                _raise_for_code(status, code, text)
            self._streak += 1
            self.longest_ok_streak = max(self.longest_ok_streak, self._streak)
            return Result(status, resp.content, data, shown_path, hints)
        raise RetryExhausted(f"retries used up ({last_error})")


def gunzip_json(data: bytes):
    return json.loads(gzip.decompress(data).decode("utf-8"))
