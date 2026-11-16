"""`python -m updl fetch` (`bash termux_run.sh fetch`, or `bash termux_run.sh` with no word): ONE index,
N weeks back from the newest day Upstox serves, into Download/BOYU_Data/<INDEX>/.

* The menu (or --index / --weeks / --yes) runs BEFORE the run lock and the live progress line.
* Settings.restrict_to(index) narrows every loop to that index + India VIX (D09/D10/D11 rules in the manifest).
* Windows: 7 calendar days each, newest first, no gap, no overlap. The first fetch of an index anchors a fixed
  grid at the newest served day (state/fetch_<INDEX>.json); later fetches reuse that grid, so a finished week
  is never processed again, and days served after the anchor come as one more (newest) window.
* Each window goes through the same machinery as `continue` (weekly.process_window); `week_cursor.json` is
  never touched. Coverage is per instrument key, so nothing covered is downloaded twice (VIX included).
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from .calsync import cached_calendar, sync_calendar
from .client import HttpClient
from .config import Settings
from .sessions import Calendar, load_calendar
from .store import atomic_write_bytes
from .timeutil import iso, to_date, today_ist, utc_iso
from .weekly import (HISTORY_FLOOR, PHONE_FOLDER, WARMUP_ITEMS, _days, detect_data_through, phone_root,
                     process_window, start_of_run)

CURSOR_FMT = "fetch_{index}.json"
MAX_TRIES = 3
EXIT_MENU_INPUT = 2          # 3 wrong answers, or a non-interactive terminal without all flags
EXIT_CTRL_C = 130
DONE_STATES = ("COMPLETE", "INCOMPLETE")      # INCOMPLETE comes back only after `repair` (status REPAIR)


class MenuStop(Exception):
    """The menu ended without a run: message for the owner and the exit code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.message, self.code = message, code


# ---- menu ---------------------------------------------------------------------------------------------------
def max_weeks(newest: dt.date, floor: dt.date) -> int:
    """How many 7-day windows fit between the history floor and the newest day (the last one may be short)."""
    return max(1, ((newest - floor).days + 7) // 7)


def parse_weeks(text) -> tuple:
    """(weeks, None) or (None, reason). Only a plain whole number above 0 counts."""
    t = str(text if text is not None else "").strip()
    if not t:
        return None, "Kuch nahi likha. Hafton ka number likho, jaise 4."
    if t.startswith("-"):
        return None, "Minus nahi chalega. 1 ya usse bada number likho."
    if "." in t or "," in t:
        return None, "Poora number likho (2.5 jaisa nahi)."
    if not t.isdigit():
        return None, "Sirf number likho, jaise 15."
    if int(t) == 0:
        return None, "0 hafte nahi ho sakte. 1 ya usse bada number likho."
    return int(t), None


def parse_index(settings: Settings, text) -> Optional[str]:
    t = str(text if text is not None else "").strip()
    menu = settings.menu_indices()
    if t.isdigit() and 1 <= int(t) <= len(menu):
        return menu[int(t) - 1]
    return settings.resolve_index(t) if t else None


def _ask(ask: Callable, prompt: str) -> str:
    try:
        return ask(prompt)
    except (KeyboardInterrupt, EOFError):
        raise MenuStop("\nRuk gaya. Kuch download nahi hua.", EXIT_CTRL_C)


def ask_index(settings: Settings, ask: Callable, say: Callable) -> str:
    menu = settings.menu_indices()
    say("Kis index ka data chahiye?")
    for n, k in enumerate(menu, 1):
        say(f"  {n} = {settings.index_label(k)}")
    choices = "/".join(str(n) for n in range(1, len(menu) + 1))
    for _ in range(MAX_TRIES):
        got = parse_index(settings, _ask(ask, f"Number likho ({choices}): "))
        if got:
            return got
        say(f"Galat jawab. Sirf {choices} mein se ek likho.")
    raise MenuStop("3 baar galat jawab. Band kar raha hoon; dobara chalao: bash termux_run.sh fetch", EXIT_MENU_INPUT)


def ask_weeks(ask: Callable, say: Callable) -> int:
    say("Kitne hafte ka data chahiye (aaj se peeche)?")
    for n in range(1, 5):
        say(f"  {n} = {n} hafta" if n == 1 else f"  {n} = {n} hafte")
    say("  Ya koi bhi number likho (jaise 15 = 15 hafte)")
    for _ in range(MAX_TRIES):
        weeks, why = parse_weeks(_ask(ask, "Hafte: "))
        if weeks:
            return weeks
        say(why)
    raise MenuStop("3 baar galat jawab. Band kar raha hoon; dobara chalao: bash termux_run.sh fetch", EXIT_MENU_INPUT)


def _yes(ask: Callable, prompt: str) -> bool:
    return _ask(ask, prompt).strip().lower() in ("", "y", "yes", "h", "haan", "ha")


def choose(settings: Settings, index_arg=None, weeks_arg=None, yes: bool = False, interactive: bool = True,
           ask: Callable = input, say: Callable = print, today: Optional[dt.date] = None,
           calendar: Optional[Calendar] = None, phone_dir: Optional[str] = None,
           floor: Optional[dt.date] = None) -> tuple:
    """Return (index, weeks) or raise MenuStop. No network, no lock, no progress line."""
    calendar = calendar or load_calendar(settings.reference_dir)
    floor = floor or to_date(HISTORY_FLOOR)
    if not interactive and (index_arg is None or weeks_arg is None or not yes):
        raise MenuStop("Terminal interactive nahi hai, isliye sawal nahi pooch sakta. Aise chalao:\n"
                       "  python -m updl fetch --index BANKNIFTY --weeks 15 --yes", EXIT_MENU_INPUT)
    index = parse_index(settings, index_arg) if index_arg is not None else None
    if index_arg is not None and index is None:
        names = ", ".join(settings.menu_indices())
        if not interactive:
            raise MenuStop(f"--index '{index_arg}' samajh nahi aaya. Inme se do: {names}", EXIT_MENU_INPUT)
        say(f"--index '{index_arg}' samajh nahi aaya ({names}).")
    weeks = None
    if weeks_arg is not None:
        weeks, why = parse_weeks(weeks_arg)
        if weeks is None:
            if not interactive:
                raise MenuStop(f"--weeks: {why}", EXIT_MENU_INPUT)
            say(f"--weeks: {why}")
    if index is None:
        index = ask_index(settings, ask, say)
    if weeks is None:
        weeks = ask_weeks(ask, say)

    newest = calendar.last_completed_trading_day(to_date(today) if today else today_ist())   # estimate: no network
    top = max_weeks(newest, floor)
    if weeks > top:
        msg = f"Itna peeche data nahi hai ({iso(floor)} se pehle nahi milta), sirf {top} hafte milenge."
        if yes or not interactive:
            say(msg)
        elif not _yes(ask, msg + " Chalaun? (Enter = haan, n = nahi) "):
            raise MenuStop("Theek hai, kuch download nahi kiya.", 0)
        weeks = top
    first = max(floor, newest - dt.timedelta(days=7 * weeks - 1))
    root = phone_root(phone_dir)
    folder = str(root / index) if root is not None else f"Download/{PHONE_FOLDER}/{index}/ (pehle termux-setup-storage)"
    say(f"{settings.index_label(index)} | {weeks} {'hafta' if weeks == 1 else 'hafte'} | {iso(first)} se {iso(newest)}")
    say(f"Folder: {folder}/")
    if not yes and not _yes(ask, "Enter = shuru, n = ruk jao: "):
        raise MenuStop("Theek hai, kuch download nahi kiya.", 0)
    return index, weeks


# ---- windows and the per-index cursor -------------------------------------------------------------------------
def plan_windows(anchor: Optional[dt.date], data_through: dt.date, weeks: int, floor: dt.date) -> list:
    """[(start, end)] newest first. Days served after the anchor come first (7-day steps forward from it, the
    newest one possibly shorter), then the fixed grid back from the anchor until `weeks` weeks before
    data_through are covered. Never before the floor; no gap, no overlap."""
    a = min(anchor, data_through) if anchor else data_through
    target = max(floor, data_through - dt.timedelta(days=7 * weeks - 1))
    fwd, s = [], a + dt.timedelta(days=1)
    while s <= data_through:
        fwd.append((s, min(s + dt.timedelta(days=6), data_through)))
        s += dt.timedelta(days=7)
    out = list(reversed(fwd))
    e = a
    while e >= target and e >= floor:
        out.append((max(e - dt.timedelta(days=6), floor), e))
        e -= dt.timedelta(days=7)
    return out


def cursor_path(settings: Settings, index: str) -> Path:
    return settings.state_dir / CURSOR_FMT.format(index=index)


def load_fetch_cursor(settings: Settings, index: str) -> dict:
    p = cursor_path(settings, index)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                return data
        except (OSError, ValueError):
            pass
    return {"version": 1, "index": index, "anchor_end": None, "next_no": 1, "windows": {}}


def save_fetch_cursor(settings: Settings, index: str, cur: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(cursor_path(settings, index), json.dumps(cur, indent=2, sort_keys=True).encode("utf-8"),
                       overwrite=True)


def mark_fetch_repair(settings: Settings, days) -> int:
    """`repair`: every fetch window that contains a re-queued day is processed again by the next `fetch`
    (only its missing chunks are downloaded). Returns the number of windows marked."""
    days = {to_date(d) for d in days}
    marked = 0
    for p in sorted(settings.state_dir.glob(CURSOR_FMT.format(index="*"))):
        index = p.stem.split("_", 1)[1]
        cur = load_fetch_cursor(settings, index)
        hit = False
        for rec in cur["windows"].values():
            lo, hi = to_date(rec["start"]), to_date(rec["end"])
            if rec.get("status") in DONE_STATES and any(lo <= d <= hi for d in days):
                rec["status"] = "REPAIR"
                hit = True
                marked += 1
        if hit:
            save_fetch_cursor(settings, index, cur)
    return marked


# ---- the run ------------------------------------------------------------------------------------------------
def run_fetch(settings: Settings, client: HttpClient, index: str, weeks: int, calendar: Optional[Calendar] = None,
              today_override: Optional[str] = None, phone_dir: Optional[str] = None, run_id: str = "r1", log=None,
              secrets=(), progress=None, with_globals: bool = False) -> dict:
    log = log or (lambda *a, **k: None)
    say = progress.note if progress is not None else print
    index = settings.restrict_to(index, with_globals=with_globals)
    label = settings.index_label(index)
    base_calendar = calendar or load_calendar(settings.reference_dir)
    today = to_date(today_override) if today_override else today_ist()
    floor = to_date(HISTORY_FLOOR)
    settings.refetch_last_day = False      # the newest day stays pending until Upstox serves it
    settings.warmup_items = set(WARMUP_ITEMS)
    settings.progress = progress
    t0 = time.monotonic()
    calls0 = client.calls_made

    if not settings.dry_run:
        start_of_run(settings, client, base_calendar, today, secrets, say, log)
    recent = _days(today - dt.timedelta(days=10), today)
    calendar = cached_calendar(settings, base_calendar) if settings.dry_run else sync_calendar(
        settings, client, base_calendar, recent, log)
    if settings.dry_run:
        data_through, pending = calendar.last_completed_trading_day(today), []
    else:
        data_through, pending = detect_data_through(settings, client, calendar, today)
    top = max_weeks(data_through, floor)
    if weeks > top:
        say(f"[{label}] {iso(floor)} se pehle data nahi milta: {weeks} ki jagah {top} hafte.")
        weeks = top

    cur = load_fetch_cursor(settings, index)
    anchor = to_date(cur["anchor_end"]) if cur.get("anchor_end") else None
    windows = plan_windows(anchor, data_through, weeks, floor)
    if not settings.dry_run and anchor is None:
        cur["anchor_end"] = iso(data_through)
        save_fetch_cursor(settings, index, cur)
    say(f"[{label}] {len(windows)} hafte: {iso(windows[-1][0])} se {iso(windows[0][1])} "
        f"(sabse naya din jo Upstox deta hai: {iso(data_through)})")
    for d in pending:
        say(f"[{label}] {iso(d)} ka data Upstox par abhi nahi aaya; agli baar `fetch` par apne aap aayega.")

    root = phone_root(phone_dir)
    folder = (root / index) if root is not None else settings.workdir / "phone_outputs" / index
    out = {"index": index, "weeks": len(windows), "start": iso(windows[-1][0]), "end": iso(windows[0][1]),
           "windows": [], "complete": 0, "incomplete": 0, "skipped": 0, "failed_chunks": 0, "stopped": False,
           "folder": str(folder), "data_through": iso(data_through)}
    for k, (start, end) in enumerate(windows, 1):
        key = iso(start)
        rec = cur["windows"].get(key)
        if rec and rec.get("end") == iso(end) and rec.get("status") in DONE_STATES:
            out["skipped"] += 1
            out["incomplete" if rec["status"] == "INCOMPLETE" else "complete"] += 1
            out["windows"].append({**rec, "skipped": True})
            continue
        old_phone = rec.get("phone_path") if rec and rec.get("end") != iso(end) else None
        # an interrupted window keeps its first start time, so the chunks fetched before the stop get compacted
        started = rec.get("started_utc") if rec and rec.get("status") == "STARTED" else None
        resume = bool(started and rec.get("end") == iso(end))
        no = int(rec["no"]) if rec else int(cur.get("next_no", 1))
        if not rec:
            cur["next_no"] = no + 1
        since = started or utc_iso()
        rec = {"no": no, "start": key, "end": iso(end), "status": "STARTED", "started_utc": since,
               "phone_path": rec.get("phone_path") if rec else None}
        tag = f"week_{no:02d}_{key}_to_{iso(end)}"
        if not settings.dry_run:
            cur["windows"][key] = rec
            save_fetch_cursor(settings, index, cur)
            calendar = sync_calendar(settings, client, calendar, _days(start, end), log)
        say(f"[{label} {k}/{len(windows)}] {iso(start)} se {iso(end)}" + (" (resume)" if resume else ""))
        res = process_window(settings, client, calendar, start, end, tag, since, pending, run_id, log, secrets,
                             progress, say, phone_dir, t_start=time.monotonic(), sub=index)
        if settings.dry_run:
            out["windows"].append({"start": key, "end": iso(end), "plan_summary": res.get("plan_summary")})
            continue
        if not res.get("packaged"):
            out["stopped"] = True
            out["windows"].append({**rec, "stopped": True})
            break
        rec.update(status=res["status"], phone_path=res["pkg"]["phone_path"], calls=res["calls"],
                   failed_chunks=res["failed_chunks"], done_utc=utc_iso())
        cur["windows"][key] = rec
        save_fetch_cursor(settings, index, cur)
        if old_phone and old_phone != rec["phone_path"] and Path(old_phone).parent == folder:
            shutil.rmtree(old_phone, ignore_errors=True)     # the same week, now with more days: old copy goes
        out["complete" if res["status"] == "COMPLETE" else "incomplete"] += 1
        out["failed_chunks"] += res["failed_chunks"]
        out["windows"].append(rec)

    out["calls"] = client.calls_made - calls0
    out["seconds"] = round(time.monotonic() - t0, 1)
    if not settings.dry_run:
        out["trading_days"] = len(calendar.trading_days(windows[-1][0], windows[0][1]))
        box = final_box(label, out)
        if progress is not None:
            progress.box(box)
        else:
            for line in box:
                print(line)
    return out


def final_box(label: str, out: dict) -> list:
    n = out["weeks"]
    if out["stopped"]:
        head = f"FETCH {label}: RUK GAYA (PAUSE)"
    elif out["incomplete"]:
        head = f"FETCH {label}: ADHURA (INCOMPLETE)"
    else:
        head = f"FETCH {label}: POORA (COMPLETE)"
    lines = [head,
             f"Hafte: {n} (poore {out['complete']}, adhure {out['incomplete']}"
             + (f", pehle se the {out['skipped']}" if out["skipped"] else "") + ")",
             f"Data: {out['start']} se {out['end']} ({out.get('trading_days', 0)} trading din)",
             f"Failed chunks: {out['failed_chunks']}",
             f"Calls: {out['calls']} | Time: {int(out['seconds'] // 60)}m {int(out['seconds'] % 60)}s",
             f"Folder: {out['folder']}"]
    if out["stopped"]:
        lines.append("Wahi command, wahi index aur hafte dobara chalao: jahan ruka wahin se aage.")
    elif out["incomplete"]:
        lines.append("Adhure hafte ke liye: bash termux_run.sh repair, phir bash termux_run.sh fetch (wahi choice)")
    return lines
