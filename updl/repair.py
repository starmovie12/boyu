"""`python -m updl repair`: put failed chunks back in the queue.

The old month-mode run sent still-listed contracts to the expired endpoint: 1,797 chunks ended as
FAILED_PERMANENT (400 UDAPI1021) while 1,192 chunks were fine. Repair never touches a DONE or EMPTY_OK row.
It removes the failure rows and records the trading days they left uncovered as `repair_days` in the week
cursor; the next `continue` runs those days first. The planner works from coverage, so only the missing
contract-days are requested, now through the right endpoint (a plain listed key through V3; a contract that has
expired since is found under its dated key in the expired-contract list).
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from .calsync import cached_calendar
from .config import Settings
from .sessions import Calendar, load_calendar
from .state import FAILED_PERMANENT, FAILED_RETRYABLE, INCOMPLETE, State, cov_key
from .timeutil import iso, to_date
from .weekly import load_cursor, save_cursor

FAILED = (FAILED_PERMANENT, FAILED_RETRYABLE, INCOMPLETE)
ROUTING_CODE = "UDAPI1021"


def failed_chunks(state: State, calendar: Calendar) -> list:
    """Failure rows with the trading days they still leave uncovered (`_days`; empty = fetched later anyway)."""
    cache, out = {}, []
    for c in state.chunks(status=list(FAILED)):
        ck = cov_key(c["instrument_key"], "1d" if str(c.get("table_name") or "").endswith("_1d") else "1m")
        if ck not in cache:
            cache[ck] = state.covered_dates(ck)
        days = [d for d in calendar.trading_days(c["from_date"], c["to_date"]) if d not in cache[ck]]
        out.append({**c, "_days": days})
    return out


def routing_failures(settings: Settings) -> int:
    """How many failure rows carry the wrong-endpoint code (used by `continue` to suggest `repair`)."""
    if not (settings.state_dir / "progress.sqlite3").exists():
        return 0
    state = State(settings.state_dir)
    try:
        return sum(1 for c in state.chunks(status=list(FAILED)) if ROUTING_CODE in str(c.get("error") or ""))
    finally:
        state.close()


def run_repair(settings: Settings, calendar: Optional[Calendar] = None) -> dict:
    calendar = calendar or cached_calendar(settings, load_calendar(settings.reference_dir))
    if not (settings.state_dir / "progress.sqlite3").exists():
        print("Repair: abhi koi state nahi hai. Pehle `bash termux_run.sh continue` chalao.")
        return {"failed_rows": 0, "retry_chunks": 0, "repair_days": []}
    state = State(settings.state_dir)
    try:
        before = state.chunk_counts()
        rows = failed_chunks(state, calendar)
        pending = [r for r in rows if r["_days"]]
        by_item = Counter((r["item_id"], r["underlying"]) for r in pending)
        days = sorted({d for r in pending for d in r["_days"]})
        n_routing = sum(1 for r in rows if ROUTING_CODE in str(r.get("error") or ""))
        removed = state.delete_chunks(list(FAILED))
        after = state.chunk_counts()
    finally:
        state.close()
    kept = {k: v for k, v in after.items() if k not in FAILED}
    assert kept == {k: v for k, v in before.items() if k not in FAILED}, "repair must never touch good chunks"

    cursor = load_cursor(settings)
    cursor["repair_days"] = sorted(set(cursor.get("repair_days") or []) | {iso(d) for d in days})
    if not settings.dry_run:
        save_cursor(settings, cursor)

    good = sum(kept.values())
    print(f"Repair: {removed} fail hue chunks mile ({n_routing} galat-endpoint UDAPI1021 wale). "
          f"{good} sahi chunks ko haath nahi lagaya, woh dobara download NAHI honge.")
    if pending:
        print(f"Repair: {len(pending)} chunks dobara try honge, {len(days)} din ke liye "
              f"({iso(days[0])} se {iso(days[-1])}).")
        for (item, und), n in sorted(by_item.items()):
            print(f"  {item} {und}: {n} chunks")
        print("Ab chalao: bash termux_run.sh continue   (pehle yahi repair wale din aayenge)")
    else:
        print("Repair: dobara try karne layak kuch nahi bacha (jo fail hue the woh baad mein aa chuke hain).")
    return {"failed_rows": removed, "retry_chunks": len(pending), "routing_failures": n_routing,
            "good_chunks": good, "repair_days": [iso(d) for d in days],
            "by_item": {f"{i} {u}": n for (i, u), n in by_item.items()}}


def drop_repaired_days(cursor: dict, start, end) -> None:
    """A finished batch covered [start, end]: those days leave the repair queue."""
    lo, hi = to_date(start), to_date(end)
    cursor["repair_days"] = [d for d in (cursor.get("repair_days") or []) if not (lo <= to_date(d) <= hi)]
