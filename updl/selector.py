"""Point-in-time contract selection, delta-aware strike bands and request chunking (pure functions, no network)."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Optional

from .timeutil import to_date


@dataclass(frozen=True)
class Contract:
    instrument_key: str
    expiry_date: dt.date
    strike: Optional[float]
    option_type: str          # CE | PE | FUT
    source: str               # expired | listed
    trading_symbol: str = ""


# ---- expiry handling ---------------------------------------------------------------------------------------------
def monthly_expiries(expiries: Iterable) -> list:
    """The last expiry of each calendar month (Bank Nifty keeps monthly contracts only)."""
    last = {}
    for e in sorted({to_date(x) for x in expiries}):
        last[(e.year, e.month)] = e
    return sorted(last.values())


def expiries_for_day(day, expiries: Iterable) -> list:
    """Current expiry = smallest expiry >= day. On the expiry day itself also take the next expiry
    (the bot shifts to it at 13:30). Expiry dates always come from lists, never from weekday arithmetic."""
    day = to_date(day)
    ex = sorted({to_date(x) for x in expiries})
    cur = [e for e in ex if e >= day]
    if not cur:
        return []
    out = [cur[0]]
    if cur[0] == day and len(cur) > 1:
        out.append(cur[1])
    return out


def futures_expiries_for_day(day, monthly_list: Iterable) -> list:
    """Current and next month future."""
    day = to_date(day)
    cur = [e for e in sorted({to_date(x) for x in monthly_list}) if e >= day]
    return cur[:2]


def strike_step(strikes: Iterable, default: float) -> float:
    s = sorted({float(x) for x in strikes if x is not None})
    diffs = [b - a for a, b in zip(s, s[1:]) if b - a > 0]
    return min(diffs) if diffs else float(default)


def first_seen_hides(fs, day) -> bool:
    """True when a contract must not be selected on `day` because it showed no candle then.
    fs is a date (first seen: hide every earlier day) or, from the catalog, (first_date, evidence_from):
    hide only days inside [evidence_from, first_date), the range a request actually covered without a
    candle. Days before evidence_from are unknown (for example an earlier month in `continue` mode)."""
    if fs is None:
        return False
    day = to_date(day)
    if isinstance(fs, tuple):
        first, ev = fs
        return (ev is None or to_date(ev) <= day) and day < to_date(first)
    return day < to_date(fs)


# ---- delta-aware strike band -----------------------------------------------------------------------------------
def band_margin_points(step: float, spot_close: float, vix_day_high: float, cal_dte: float,
                       margin_steps: float, delta_k: float, min_steps: Optional[float] = None) -> float:
    """margin_pts = max(min_steps x step, K x spot x (VIX_high/100) x sqrt(cal_dte/365)).
    min_steps defaults to STRIKE_MARGIN_STEPS (D08 passes WIDE_CHAIN_STEPS)."""
    steps = margin_steps if min_steps is None else min_steps
    cal = max(0.5, float(cal_dte))
    delta_pts = delta_k * float(spot_close) * (float(vix_day_high) / 100.0) * math.sqrt(cal / 365.0)
    return max(steps * float(step), delta_pts)


def strikes_in_band(strikes: Iterable, day_low: float, day_high: float, margin_pts: float) -> list:
    lo, hi = day_low - margin_pts, day_high + margin_pts
    return sorted({float(k) for k in strikes if k is not None and lo <= float(k) <= hi})


@dataclass
class DayStats:
    low: float
    high: float
    close: float
    vix_high: float


def select_day_options(day, underlying_expiries: Iterable, contracts_by_expiry: dict, stats: DayStats,
                       margin_steps: float, delta_k: float, default_step: float, first_seen: Optional[dict] = None,
                       min_steps: Optional[float] = None, listed_only: bool = False, monthly: bool = False) -> list:
    """Contracts (CE and PE) to download for one trading day. Point in time:
    * a contract with a known first-seen date later than `day` is never selected;
    * listed_only (Plan B) keeps only contracts that are still in today's master."""
    day = to_date(day)
    first_seen = first_seen or {}
    ex = monthly_expiries(underlying_expiries) if monthly else sorted({to_date(x) for x in underlying_expiries})
    chosen = []
    for e in expiries_for_day(day, ex):
        contracts = [c for c in contracts_by_expiry.get(e, []) if c.option_type in ("CE", "PE")]
        if not contracts:
            continue
        step = strike_step((c.strike for c in contracts), default_step)
        margin = band_margin_points(step, stats.close, stats.vix_high, (e - day).days, margin_steps, delta_k, min_steps)
        wanted = set(strikes_in_band((c.strike for c in contracts), stats.low, stats.high, margin))
        for c in contracts:
            if c.strike is None or float(c.strike) not in wanted:
                continue
            if listed_only and c.source != "listed":
                continue
            if first_seen_hides(first_seen.get(c.instrument_key), day):
                continue
            chosen.append(c)
    return chosen


# ---- chunking ---------------------------------------------------------------------------------------------------------
def chunk_days(days: Iterable, cap_calendar_days: int) -> list:
    """Group sorted trading days into chunks. A chunk starts at the next wanted day and spans at most
    cap_calendar_days calendar days (cap 1 = one call per day). Returns [(from, to, [days in chunk])].
    The chunk's `to` is its last wanted day, so it never passes the last requested day (e.g. TEST_END)."""
    ds = sorted({to_date(d) for d in days})
    out, i = [], 0
    cap = max(1, int(cap_calendar_days))
    while i < len(ds):
        start = ds[i]
        limit = start + dt.timedelta(days=cap - 1)
        j = i
        while j + 1 < len(ds) and ds[j + 1] <= limit:
            j += 1
        out.append((ds[i], ds[j], ds[i:j + 1]))
        i = j + 1
    return out


def contract_chunks(selected_days: Iterable, covered: set, cap_calendar_days: int, calendar) -> list:
    """Chunks for one contract. cap 1: only selected, uncovered days. Larger cap: first..last selected
    uncovered day in cap-sized windows; days inside a window come for free (all trading days are marked covered)."""
    wanted = sorted({to_date(d) for d in selected_days} - set(covered))
    if not wanted:
        return []
    if cap_calendar_days <= 1:
        return [(d, d, [d]) for d in wanted]
    out = []
    for frm, to, _ in chunk_days(wanted, cap_calendar_days):
        out.append((frm, to, calendar.trading_days(frm, to)))
    return out
