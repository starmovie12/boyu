"""Dated session table, holiday table and trading-day calendar. Expected bar counts come ONLY from sessions.yaml."""
from __future__ import annotations

import copy
import datetime as dt
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .config import load_yaml
from .timeutil import to_date

log = logging.getLogger("updl.sessions")


def _d(value) -> Optional[dt.date]:
    if value in (None, "", "null"):
        return None
    return to_date(value)


def _minutes_between(open_hhmm: str, close_hhmm: str) -> int:
    oh, om = (int(x) for x in str(open_hhmm).split(":"))
    ch, cm = (int(x) for x in str(close_hhmm).split(":"))
    return (ch * 60 + cm) - (oh * 60 + om)


def instrument_class(role: str, underlying: str = "", fo_flag: bool = False, exchange: str = "NSE") -> str:
    """Map (role, underlying) to a session class name.
    role: spot | vix | future | option | stock | global."""
    if role == "vix":
        return "india_vix"
    if role == "global":
        return "global_indicator"
    if role == "stock":
        return "cash_fo_stock" if fo_flag else "cash_non_fo_stock"
    if exchange == "BSE":
        return "bse_spot_index" if role == "spot" else "bse_index_fo"
    return "nse_spot_index" if role == "spot" else "nse_index_fo"


# session class -> exchange code used by the Upstox timings API (cash F&O stocks keep the YAML row: the API
# gives the exchange session, not the end of continuous trading before the closing auction)
EXCHANGE_OF_CLASS = {"nse_index_fo": "NFO", "bse_index_fo": "BFO", "nse_spot_index": "NSE", "india_vix": "NSE",
                     "bse_spot_index": "BSE", "cash_non_fo_stock": "NSE"}


class Calendar:
    def __init__(self, sessions: list, holidays: list, special: list, covers_to: Optional[dt.date] = None,
                 covers_from: Optional[dt.date] = None, table_version=2):
        self.rows = []
        for r in sessions:
            self.rows.append({**r, "from": _d(r.get("from")), "to": _d(r.get("to"))})
        self.holidays = {to_date(h["date"] if isinstance(h, dict) else h) for h in holidays}
        self.special = {to_date(s["date"]): s for s in special}
        self.covers_to = covers_to
        self.covers_from = covers_from
        self.table_version = table_version
        self._warned_end = False
        # v4: per-day facts read from the Upstox timings API: {date: {"NFO": ("09:15", "15:40"), ...}};
        # an empty dict means "no exchange open that day". Empty overlay = YAML tables only (offline).
        self.overlay = {}

    def with_overlay(self, overlay: dict) -> "Calendar":
        """A copy that prefers API facts for the days in `overlay` (the shared cached calendar stays untouched)."""
        other = copy.copy(self)
        other.overlay = {to_date(k): v for k, v in (overlay or {}).items()}
        return other

    def has_overlay(self, d) -> bool:
        return to_date(d) in self.overlay

    @classmethod
    def load(cls, reference_dir) -> "Calendar":
        ref = Path(reference_dir)
        ses = load_yaml(ref / "sessions.yaml")
        hol = load_yaml(ref / "holidays.yaml")
        return cls(ses.get("sessions", []), hol.get("holidays", []), ses.get("special_sessions", []),
                   covers_to=_d(hol.get("covers_to")), covers_from=_d(hol.get("covers_from")),
                   table_version=ses.get("table_version", 2))

    # ---- day classification ----------------------------------------------
    def is_holiday(self, d) -> bool:
        return to_date(d) in self.holidays

    def is_trading_day(self, d) -> bool:
        d = to_date(d)
        if d.weekday() >= 5:
            return False            # weekend sessions (Muhurat, Budget Sunday) are special sessions, never normal days
        if d in self.overlay:
            return "NSE" in self.overlay[d]
        if d in self.holidays:
            return False
        if self.covers_to and d > self.covers_to and not self._warned_end:
            self._warned_end = True
            log.warning("holidays.yaml 2026-12-31 ke baad ka data nahi rakhti; weekdays use ho rahe hain")
        return True

    def holiday_list_status(self, d) -> str:
        d = to_date(d)
        if self.covers_from and d < self.covers_from:
            return "HOLIDAY_LIST_2025_UNVERIFIED"
        if self.covers_to and d > self.covers_to:
            return "HOLIDAY_LIST_ENDED"
        return "HOLIDAY_LIST_OK"

    def session_type(self, d) -> str:
        d = to_date(d)
        if d in self.special:
            return self.special[d].get("session_type", "SPECIAL")
        if d.weekday() >= 5:
            return "WEEKEND"
        if d in self.holidays:
            return "HOLIDAY"
        return "NORMAL"

    def trading_days(self, a, b) -> list:
        a, b = to_date(a), to_date(b)
        out, d = [], a
        while d <= b:
            if self.is_trading_day(d):
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def count_trading_days(self, a, b) -> int:
        return len(self.trading_days(a, b))

    def shift(self, d, n: int) -> dt.date:
        """Move n trading days (negative = back). n=0 returns d unchanged."""
        d = to_date(d)
        step = 1 if n > 0 else -1
        left = abs(n)
        while left:
            d += dt.timedelta(days=step)
            if self.is_trading_day(d):
                left -= 1
        return d

    def prev_trading_day(self, d) -> dt.date:
        return self.shift(d, -1)

    def last_completed_trading_day(self, today) -> dt.date:
        return self.prev_trading_day(to_date(today))

    # ---- session rows ------------------------------------------------------
    def session_row(self, klass: str, d) -> Optional[dict]:
        d = to_date(d)
        candidates = [r for r in self.rows if r["class"] == klass]
        if not candidates:
            return None
        for r in candidates:
            if (r["from"] is None or r["from"] <= d) and (r["to"] is None or d <= r["to"]):
                return r
        # before the first row or in a gap: use the closest earlier row, then the earliest
        earlier = [r for r in candidates if r["from"] and r["from"] <= d]
        if earlier:
            return max(earlier, key=lambda r: r["from"])
        return min(candidates, key=lambda r: r["from"] or dt.date.min)

    def session_times(self, klass: str, d):
        d = to_date(d)
        if d in self.special and self.special[d].get("open"):
            return self.special[d]["open"], self.special[d].get("close")
        api = self.overlay.get(d, {}).get(EXCHANGE_OF_CLASS.get(klass, ""))
        if api:
            return api[0], api[1]
        row = self.session_row(klass, d)
        if not row:
            return None, None
        return row.get("open"), row.get("close")

    def expected_bars(self, klass: str, d) -> Optional[int]:
        o, c = self.session_times(klass, d)
        if not o or not c:
            return None
        return _minutes_between(o, c)

    def class_tag(self, klass: str, d) -> str:
        d = to_date(d)
        if d in self.special:
            return self.special[d].get("tag", "UNVERIFIED")
        if self.overlay.get(d, {}).get(EXCHANGE_OF_CLASS.get(klass, "")):
            return "SOURCE"
        row = self.session_row(klass, d)
        return (row or {}).get("tag", "UNVERIFIED")

    def is_provisional(self, klass: str, d) -> bool:
        return self.class_tag(klass, d) in ("UNVERIFIED", "ASSUMPTION") and klass != "global_indicator"

    def close_time_text(self, klass: str, d) -> Optional[str]:
        return self.session_times(klass, d)[1]


@lru_cache(maxsize=4)
def _cached(ref: str) -> Calendar:
    return Calendar.load(ref)


def load_calendar(reference_dir) -> Calendar:
    return _cached(str(Path(reference_dir).resolve()))
