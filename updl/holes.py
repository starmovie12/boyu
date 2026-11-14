"""Per-day status of every (index, day, table) and the list of holes (`reports/day_status.csv`, `holes.csv`).

Statuses:
* COMPLETE        every planned chunk of that table and day is DONE or EMPTY_OK;
* NOT_YET_SERVED  the newest session is not in Upstox's historical API yet (retried automatically by the next
                  `continue`; an allowed hole, never a failure);
* MISSING_ERROR   at least one chunk failed and its day is still not covered (`repair` queues it again);
* NOT_DOWNLOADED  nothing was requested for that table and day (item not selected, or not reached yet).
PENDING_UNTIL_EXPIRY is not used: V3 serves still-listed contracts at 1 minute (verified live 14 Nov 2026).
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from .config import Settings
from .sessions import Calendar
from .state import COVERING, State
from .store import ITEM_TABLE
from .timeutil import iso, to_date

TABLE_ITEMS = {"spot_1m": ("D01",), "fut_1m": ("D05",), "opt_1m": ("D06",), "opt_wide_1m": ("D08",)}
FIELDS = ["trading_date", "underlying", "table", "status", "detail"]
COMPLETE, NOT_YET_SERVED, MISSING_ERROR, NOT_DOWNLOADED = "COMPLETE", "NOT_YET_SERVED", "MISSING_ERROR", "NOT_DOWNLOADED"


def day_status_rows(settings: Settings, calendar: Calendar, start, end, pending_days=()) -> list:
    from .repair import failed_chunks       # local import: repair imports weekly, weekly imports this module
    start, end = to_date(start), to_date(end)
    pending = {to_date(d) for d in pending_days}
    days = sorted(set(calendar.trading_days(start, end)) | pending)
    if not days:
        return []
    state = State(settings.state_dir)
    try:
        failed, done = {}, set()
        for c in failed_chunks(state, calendar):
            table = c.get("table_name") or ITEM_TABLE.get(c["item_id"], "")
            for d in c["_days"]:
                key = (c["underlying"], d, table)
                failed.setdefault(key, []).append(str(c.get("error") or "error"))
        lo, hi = iso(days[0]), iso(days[-1])
        for c in state.chunks(status=list(COVERING)):
            if c["to_date"] < lo or c["from_date"] > hi:
                continue
            table = c.get("table_name") or ITEM_TABLE.get(c["item_id"], "")
            d = max(to_date(c["from_date"]), days[0])
            while d <= min(to_date(c["to_date"]), days[-1]):
                done.add((c["underlying"], d, table))
                d += dt.timedelta(days=1)
    finally:
        state.close()
    rows = []
    for d in days:
        for und in settings.instrument_names():
            for table in TABLE_ITEMS:
                key = (und, d, table)
                if d in pending:
                    status, detail = NOT_YET_SERVED, "Upstox has not served this session yet; the next continue retries it"
                elif key in failed:
                    errs = failed[key]
                    status, detail = MISSING_ERROR, f"{len(errs)} failed chunk(s), e.g. {errs[0]}"
                elif key in done:
                    status, detail = COMPLETE, ""
                else:
                    status, detail = NOT_DOWNLOADED, "nothing requested for this table and day"
                rows.append({"trading_date": iso(d), "underlying": und, "table": table, "status": status,
                             "detail": detail})
    return rows


def write_day_status(settings: Settings, calendar: Calendar, start, end, pending_days=()) -> dict:
    """Merge the window's rows into reports/day_status.csv and rewrite holes.csv (every row not COMPLETE)."""
    rows = day_status_rows(settings, calendar, start, end, pending_days)
    path = Path(settings.reports_dir) / "day_status.csv"
    keep = []
    touched = {(r["trading_date"], r["underlying"], r["table"]) for r in rows}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                keep = [r for r in csv.DictReader(fh)
                        if (r.get("trading_date"), r.get("underlying"), r.get("table")) not in touched]
        except (OSError, csv.Error):
            keep = []
    merged = sorted(keep + rows, key=lambda r: (r["trading_date"], r["underlying"], r["table"]))
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("day_status.csv", merged), ("holes.csv", [r for r in merged if r["status"] != COMPLETE])):
        with open(Path(settings.reports_dir) / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"rows": len(rows), "counts": counts,
            "holes": [r for r in rows if r["status"] not in (COMPLETE, NOT_DOWNLOADED)]}
