"""Read-only HTTP client for the Upstox GET endpoints and the three public instrument-master files.

Rules enforced here:
* only paths in ALLOWED_PATH_TEMPLATES (and the three master URLs) can be requested;
* a rate limiter (calls/min, rolling 30-minute cap, burst) with one request at a time;
* retries with documented backoff, a circuit breaker for repeated 429, no token in any message.
"""
from __future__ import annotations

import gzip
import json
import random
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .errors import (ApiError, BudgetExceeded, NotAllowedPath, PlusRequired, RetryExhausted, TokenExpired,
                     TransportError)
from .tokens import Tokens

BASE_URL = "https://api.upstox.com"

ALLOWED_PATH_TEMPLATES = (
    "/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}",
    "/v2/expired-instruments/expiries",
    "/v2/expired-instruments/option/contract",
    "/v2/expired-instruments/future/contract",
    "/v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}",
    "/v2/instruments/search",
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
        self.session = requests.Session()

    def get(self, url, headers=None, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) -> RawResponse:
        try:
            r = self.session.get(url, headers=headers or {}, timeout=timeout)
        except self._requests.exceptions.Timeout:
            raise TransportError("Timeout") from None
        except self._requests.exceptions.RequestException as exc:
            raise TransportError(type(exc).__name__) from None
        return RawResponse(r.status_code, {k: v for k, v in r.headers.items()}, r.content)


class RateLimiter:
    """Calls per minute with burst, plus a rolling 30-minute cap. One request at a time (no threads)."""

    def __init__(self, calls_per_min=30.0, max_per_30min=900, burst=3, clock=time.monotonic, sleep=time.sleep):
        self.calls_per_min = float(calls_per_min)
        self.max_per_30min = int(max_per_30min)
        self.burst = float(burst)
        self.clock = clock
        self.sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._window = deque()

    def halve(self):
        self.calls_per_min = max(1.0, self.calls_per_min / 2.0)

    def acquire(self):
        while True:
            now = self.clock()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.calls_per_min / 60.0)
            self._last = now
            while self._window and now - self._window[0] >= 1800:
                self._window.popleft()
            wait = 0.0
            if self._tokens < 1.0:
                wait = max(wait, (1.0 - self._tokens) * 60.0 / self.calls_per_min)
            if len(self._window) >= self.max_per_30min:
                wait = max(wait, 1800 - (now - self._window[0]) + 0.001)
            if wait <= 0:
                self._tokens -= 1.0
                self._window.append(now)
                return
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
        if is_file:
            self.file_downloads += 1       # public master files are not API calls
            return
        if self.budget is not None and self.calls_made >= self.budget:
            raise BudgetExceeded("call budget used up")
        self.calls_made += 1

    def _backoff(self, attempt_index: int, retry_after: Optional[float] = None):
        base = BACKOFF_SECONDS[min(attempt_index, len(BACKOFF_SECONDS) - 1)]
        delay = base * (1.0 + JITTER_FRACTION * self.rng())
        if retry_after is not None:
            delay = max(delay, retry_after)
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
                raise TokenExpired("token expired or not accepted")
            if status == 429:
                self.consecutive_429 += 1
                self._streak = 0
                last_error = "http:429"
                self._log("retry", reason="http:429", attempt=attempt + 1)
                if self.consecutive_429 >= 3:
                    self.circuit_pauses += 1
                    self._log("circuit_breaker", pause_seconds=CIRCUIT_PAUSE_SECONDS)
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
                    self._backoff(attempt, ra)
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
                if code == "UDAPI1149":
                    raise PlusRequired(status, code, text)
                raise ApiError(status, code, text)
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
                if code == "UDAPI1149":
                    raise PlusRequired(status, code, text)
                raise ApiError(status, code, text)
            self._streak += 1
            self.longest_ok_streak = max(self.longest_ok_streak, self._streak)
            return Result(status, resp.content, data, shown_path, hints)
        raise RetryExhausted(f"retries used up ({last_error})")


def gunzip_json(data: bytes):
    return json.loads(gzip.decompress(data).decode("utf-8"))
