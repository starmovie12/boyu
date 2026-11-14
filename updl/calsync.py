"""Exchange calendar facts from the Upstox timings API (`/v2/market/timings/{date}`), cached per day.

Verified live on 14 Nov 2026: the endpoint answers for past dates back to 2022 and lists, per exchange, the
session start and end (NSE/BSE cash 09:15-15:30; NFO/BFO 09:15-15:40 from 3 Aug 2026). A closed day returns no
NSE entry. The cache (`state/calendar_cache.json`) is the per-day SOURCE for trading days and expected bars;
reference/sessions.yaml and holidays.yaml remain the offline fallback.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .errors import ApiError, RetryExhausted
from .sessions import Calendar
from .store import atomic_write_bytes
from .timeutil import IST, iso, to_date, today_ist, utc_iso

CACHE_NAME = "calendar_cache.json"
EXCHANGES = ("NSE", "NFO", "BSE", "BFO")
TIMINGS_PATH = "/v2/market/timings/"


def _hhmm(ms) -> str:
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=IST).strftime("%H:%M")


def parse_timings(payload: dict) -> dict:
    """API answer -> {"NSE": ["09:15", "15:30"], ...}; {} when none of the four exchanges is open."""
    out = {}
    for row in payload.get("data") or []:
        if isinstance(row, dict) and row.get("exchange") in EXCHANGES and row.get("start_time") and row.get("end_time"):
            out[row["exchange"]] = [_hhmm(row["start_time"]), _hhmm(row["end_time"])]
    return out


def load_cache(state_dir) -> dict:
    p = Path(state_dir) / CACHE_NAME
    if not p.exists():
        return {"days": {}, "read_utc": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"days": dict(data.get("days") or {}), "read_utc": dict(data.get("read_utc") or {})}
    except (OSError, ValueError):
        return {"days": {}, "read_utc": {}}


def overlay_from_cache(cache: dict) -> dict:
    return {to_date(k): {ex: tuple(v) for ex, v in day.items()} for k, day in cache.get("days", {}).items()}


def sync_calendar(settings, client, calendar: Calendar, days, log=None) -> Calendar:
    """Ask the timings API for every completed day in `days` that is not cached yet (one small call per day)
    and return a calendar that prefers those facts. A failed call leaves that day to the YAML tables."""
    log = log or (lambda *a, **k: None)
    cache = load_cache(settings.state_dir)
    today = today_ist()
    changed = False
    for d in sorted({to_date(x) for x in days}):
        key = iso(d)
        if key in cache["days"] or d >= today:
            continue
        try:
            res = client.get(TIMINGS_PATH + key, kind="v3")
        except (ApiError, RetryExhausted) as err:
            log("calendar_sync_failed", day=key, error=str(err)[:80])
            continue
        if not isinstance(res.json.get("data"), list):
            continue
        cache["days"][key] = parse_timings(res.json)
        cache["read_utc"][key] = utc_iso()
        changed = True
    if changed and not settings.dry_run:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(Path(settings.state_dir) / CACHE_NAME,
                           json.dumps(cache, sort_keys=True).encode("utf-8"), overwrite=True)
    return calendar.with_overlay(overlay_from_cache(cache))


def cached_calendar(settings, calendar: Calendar) -> Calendar:
    """The calendar with whatever the cache already holds (no network)."""
    cache = load_cache(settings.state_dir)
    return calendar.with_overlay(overlay_from_cache(cache)) if cache["days"] else calendar
