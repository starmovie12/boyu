"""Time helpers. All market times are Asia/Kolkata (IST, +05:30, no daylight saving)."""
from __future__ import annotations

import datetime as dt

try:  # zoneinfo needs the tz database; fall back to a fixed offset if it is missing
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - depends on the machine
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

UTC = dt.timezone.utc
IST_OFFSET_TEXT = "+05:30"


def to_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def iso(d) -> str:
    return to_date(d).isoformat()


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


def parse_ts(text: str) -> dt.datetime:
    """Parse a candle timestamp such as 2026-05-11T09:15:00+05:30 into an aware IST datetime."""
    value = dt.datetime.fromisoformat(str(text))
    if value.tzinfo is None:
        raise ValueError("timestamp has no offset")
    return value.astimezone(IST)


def has_ist_offset(text: str) -> bool:
    return str(text).endswith(IST_OFFSET_TEXT)


def ms_to_ist_date(ms: int) -> dt.date:
    """Epoch milliseconds -> IST calendar date (NOT UTC: UTC can give the previous day)."""
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).astimezone(IST).date()


def at_ist(d, hhmm: str) -> dt.datetime:
    d = to_date(d)
    h, m = hhmm.split(":")
    return dt.datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST)


def end_of_day_ist(d) -> dt.datetime:
    d = to_date(d)
    return dt.datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=IST)


def utc_stamp() -> str:
    return dt.datetime.now(UTC).strftime("%H%M%S")


def utc_iso() -> str:
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def daterange(a, b):
    a, b = to_date(a), to_date(b)
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)
