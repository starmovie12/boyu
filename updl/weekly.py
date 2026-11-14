"""Week-by-week downloader (`python -m updl continue`, `bash termux_run.sh continue`).

One run = one batch of `window_days` calendar days (default 7), small enough for a phone:
1. the first run takes [today - 7 days .. newest day Upstox really serves];
2. later runs first catch up days that became available since (new sessions), otherwise step one week
   further back, with no gap and no overlap;
3. each batch is downloaded (parallel, rate limited, live progress), compacted, validated, derived and exported
   on its own days only, then copied as plain files into a new folder under the phone's Download/BOYU_Data.

Why "newest day Upstox really serves": right after midnight the previous session is not in the historical API
yet (seen live: 13 Nov empty at 01:25 on 14 Nov). Such a day stays pending; it is never recorded as empty.
State lives in `<workdir>/state/week_cursor.json`; an interrupted batch is resumed by the same command.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Optional

from .calsync import cached_calendar, sync_calendar
from .client import HttpClient, v3_candle_path
from .config import Settings
from .derive import run_derive
from .errors import ApiError, ProbeMissing, ProbeTokenBlocked, RetryExhausted, UpdlError
from .export import run_export
from .holes import write_day_status
from .marketinfo import run_marketinfo
from .messages import REPAIR_HINT, SDCARD_HINT
from .plan import run_download
from .probe import check_probe_fresh, run_probe
from .runlock import RECORD_LOCK_NAME, RunLock
from .sessions import Calendar, load_calendar
from .state import FAILED_PERMANENT, FAILED_RETRYABLE, INCOMPLETE, State
from .store import atomic_write_bytes, compact, date_of_part, list_parts
from .timeutil import iso, to_date, today_ist, utc_iso
from .validate import run_validate

CURSOR_NAME = "week_cursor.json"
OLD_CURSOR_NAME = "monthly_cursor.json"
RATE_FILE = "rate_window.json"
DEFAULT_WINDOW_DAYS = 7
HISTORY_FLOOR = "2024-10-01"      # expired expiries start 3 Oct 2024 (verified live 14 Nov 2026)
PHONE_FOLDER = "BOYU_Data"
WARMUP_ITEMS = {"D01", "D02", "D03", "D04", "D09"}    # cheap base history; option warm-up comes from older weeks
DATA_THROUGH_MIN_FRACTION = 0.9
DATA_THROUGH_MAX_STEPS = 4


# ---- cursor -----------------------------------------------------------------------------------------------
def _cursor_path(settings: Settings) -> Path:
    return settings.state_dir / CURSOR_NAME


def load_cursor(settings: Settings) -> dict:
    p = _cursor_path(settings)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 2:
                return data
        except (OSError, ValueError):
            pass
    return {"version": 2, "anchor_today": None, "newest_done": None, "next_end": None,
            "in_progress": None, "completed": []}


def save_cursor(settings: Settings, cursor: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(_cursor_path(settings), json.dumps(cursor, indent=2, sort_keys=True).encode("utf-8"),
                       overwrite=True)


def next_batch(cursor: dict, today: dt.date, data_through: dt.date, window_days: int = DEFAULT_WINDOW_DAYS,
               floor: Optional[dt.date] = None) -> Optional[dict]:
    """Pure window maths. Returns {"kind", "start", "end", "next_end"} or None when the history floor is reached.
    kind: first | repair (days queued by `repair`) | new (catch-up of newer days) | back (one window further back)."""
    window_days = max(1, int(window_days))
    newest = to_date(cursor["newest_done"]) if cursor.get("newest_done") else None
    if newest is None:
        start = min(today - dt.timedelta(days=window_days), data_through)
        return {"kind": "first", "start": start, "end": data_through, "next_end": start - dt.timedelta(days=1)}
    queued = sorted(to_date(x) for x in (cursor.get("repair_days") or []))
    if queued and queued[0] <= data_through:
        start = queued[0]
        end = min(data_through, start + dt.timedelta(days=window_days - 1))
        return {"kind": "repair", "start": start, "end": end, "next_end": to_date(cursor["next_end"])}
    if data_through > newest:
        start = newest + dt.timedelta(days=1)
        end = min(data_through, start + dt.timedelta(days=window_days - 1))
        return {"kind": "new", "start": start, "end": end, "next_end": to_date(cursor["next_end"])}
    end = to_date(cursor["next_end"])
    if floor is not None and end < floor:
        return None
    start = end - dt.timedelta(days=window_days - 1)
    return {"kind": "back", "start": start, "end": end, "next_end": start - dt.timedelta(days=1)}


# ---- newest day really served -----------------------------------------------------------------------------
def detect_data_through(settings: Settings, client: HttpClient, calendar: Calendar, today: dt.date) -> tuple:
    """(data_through, pending_days). data_through = the newest completed trading day whose index 1-minute
    session is (almost) complete in the historical API. Costs one call, more only while days are missing."""
    key = settings.spot_key(settings.instrument_names()[0])
    if key.startswith("RESOLVE_FROM_MASTER:"):
        key = "NSE_INDEX|Nifty 50"
    klass = "nse_spot_index"
    d = calendar.last_completed_trading_day(today)
    pending = []
    for _ in range(DATA_THROUGH_MAX_STEPS):
        try:
            res = client.get(v3_candle_path(key, "minutes", 1, iso(d), iso(d)), kind="v3")
            data = res.json.get("data")
            n = len((data.get("candles") or []) if isinstance(data, dict) else [])
        except (ApiError, RetryExhausted):
            n = 0
        if n >= DATA_THROUGH_MIN_FRACTION * (calendar.expected_bars(klass, d) or 1):
            return d, pending
        pending.append(d)
        d = calendar.prev_trading_day(d)
    raise UpdlError("Upstox se pichhle 4 trading din ka index data nahi mila. Internet / token check karo, "
                    "phir wahi command dobara chalao.")


# ---- rolling 30-minute window across runs -------------------------------------------------------------------
def _load_rate_window(settings: Settings, limiter) -> None:
    p = settings.state_dir / RATE_FILE
    try:
        stamps = json.loads(p.read_text(encoding="utf-8")).get("calls", [])
        now = time.time()
        limiter.preload([now - float(t) for t in stamps])
    except (OSError, ValueError, AttributeError, TypeError):
        pass


def _save_rate_window(settings: Settings, limiter) -> None:
    try:
        now = time.time()
        calls = [round(now - a, 1) for a in limiter.recent_ages()]
        atomic_write_bytes(settings.state_dir / RATE_FILE, json.dumps({"calls": calls}).encode("utf-8"),
                           overwrite=True)
    except (OSError, AttributeError):
        pass


# ---- phone folder --------------------------------------------------------------------------------------------
def phone_root(phone_dir: Optional[str] = None, environ=None) -> Optional[Path]:
    """Where batch folders go on the phone: --phone-dir, else UPDL_PHONE_DIR, else Download/BOYU_Data."""
    env = os.environ if environ is None else environ
    if phone_dir:
        return Path(phone_dir)
    if (env.get("UPDL_PHONE_DIR") or "").strip():
        return Path(env["UPDL_PHONE_DIR"].strip())
    for base in (Path("/sdcard/Download"), Path.home() / "storage" / "downloads"):
        if base.exists():
            return base / PHONE_FOLDER
    return None


def _dir_stats(path: Path) -> tuple:
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def copy_folder_to_phone(src: Path, root: Optional[Path], index_line: str) -> Optional[Path]:
    """Copy a finished batch folder to <root>/<name>/ through <name>.tmp + rename, so a half copy never looks
    finished. Returns the phone path, or None (with a Hinglish hint) when the phone folder is not writable."""
    if root is None:
        return None
    final, tmp = root / src.name, root / (src.name + ".tmp")
    try:
        root.mkdir(parents=True, exist_ok=True)
        for old in (tmp, final):
            if old.exists():
                shutil.rmtree(old)
        shutil.copytree(src, tmp, copy_function=shutil.copyfile)
        os.replace(tmp, final)
        with open(root / "INDEX.txt", "a", encoding="utf-8") as fh:
            fh.write(index_line + "\n")
        return final
    except OSError:
        print(SDCARD_HINT, src)
        return None


def package_week(settings: Settings, tag: str, start: dt.date, end: dt.date, val_res: dict, lines: list,
                 phone_dir: Optional[str] = None, tick=None) -> dict:
    """Plain files for one batch: store/ (Parquet), chain_csv/ (CSV), reports, week_summary.txt; plus a zip
    that stays on the Termux side. Then the folder is copied to the phone."""
    out_root = settings.workdir / "phone_outputs"
    week_dir = out_root / tag
    if week_dir.exists():
        shutil.rmtree(week_dir)
    week_dir.mkdir(parents=True, exist_ok=True)
    n_parquet = 0
    for part in ("dev", "holdout"):
        for p in list_parts(settings.store_dir, part):
            if start <= date_of_part(p) <= end:
                dest = week_dir / "store" / p.relative_to(settings.store_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
                n_parquet += 1
                if tick and n_parquet % 20 == 0:
                    tick("store")
    exp = run_export(settings, start=start, end=end, out_dir=week_dir, make_zip=False, folder_name="chain_csv")
    xcheck = settings.workdir / "xcheck_csv"
    if xcheck.exists():                 # small: the whole exchange-side folder travels with every batch
        shutil.copytree(xcheck, week_dir / "xcheck_csv", copy_function=shutil.copyfile)
    for rep in ("health.csv", "health_summary.txt", "reconciliation.csv", "day_status.csv", "holes.csv"):
        if (settings.reports_dir / rep).exists():
            shutil.copyfile(settings.reports_dir / rep, week_dir / rep)
    readme = week_dir / "chain_csv" / "README_EXPORT.txt"
    if readme.exists():
        shutil.copyfile(readme, week_dir / "README_EXPORT.txt")
    summary = [f"UPDL HAFTA {tag}", f"Hafta window  : {iso(start)} se {iso(end)}",
               f"Parquet files : {n_parquet} (store/)",
               f"CSV files     : {exp['csv_files']} (chain_csv/: option chain, README_EXPORT.txt padho)",
               f"Valid days    : {val_res.get('valid_trading_days', 0)} (poore data mein, health.csv dekho)",
               *lines]
    atomic_write_bytes(week_dir / "week_summary.txt", ("\n".join(summary) + "\n").encode("utf-8"), overwrite=True)
    if tick:
        tick("zip")
    zip_path = out_root / f"{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:      # Parquet is already compressed
        for f in sorted(week_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out_root).as_posix())
    n_files, n_bytes = _dir_stats(week_dir)
    if tick:
        tick("phone copy")
    phone = copy_folder_to_phone(week_dir, phone_root(phone_dir),
                                 f"{tag} | {iso(start)}..{iso(end)} | files {n_files} | {n_bytes / 1e6:.1f} MB | {utc_iso()}")
    if phone is not None:
        shutil.rmtree(week_dir, ignore_errors=True)   # the phone has the folder and the zip stays here: no third copy
    return {"tag": tag, "start_date": iso(start), "end_date": iso(end), "week_dir": str(week_dir),
            "zip_path": str(zip_path), "phone_path": str(phone) if phone else None, "parquet_files": n_parquet,
            "csv_files": exp["csv_files"], "files": n_files, "mb": round(n_bytes / 1e6, 1)}


# ---- one batch ---------------------------------------------------------------------------------------------
def _ensure_probe(settings: Settings, client: HttpClient, calendar: Calendar, secrets, say) -> None:
    try:
        check_probe_fresh(settings, secrets=secrets)
    except ProbeTokenBlocked:
        raise
    except ProbeMissing:
        say("[Hafta] probe_report purana ya missing hai. Pehle probe chala raha hoon (max 40 calls)...")
        saved = client.budget
        run_probe(settings, client, calendar=calendar)
        client.budget = saved


def _failures_by_item(settings: Settings, since_utc: str) -> dict:
    """{(item, underlying): failed chunk count} for chunks that failed since `since_utc` (any failure status)."""
    state = State(settings.state_dir)
    try:
        out = {}
        for c in state.chunks(status=[FAILED_RETRYABLE, INCOMPLETE, FAILED_PERMANENT], since_utc=since_utc):
            key = (c["item_id"], c["underlying"])
            out[key] = out.get(key, 0) + 1
        return out
    finally:
        state.close()


def _failed_since(settings: Settings, since_utc: str) -> tuple:
    state = State(settings.state_dir)
    try:
        retry = state.chunks(status=[FAILED_RETRYABLE, INCOMPLETE], since_utc=since_utc)
        perm = state.chunks(status=[FAILED_PERMANENT], since_utc=since_utc)
        return len(retry), len(perm)
    finally:
        state.close()


def run_week_batches(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
                     today_override: Optional[str] = None, window_days: int = DEFAULT_WINDOW_DAYS, weeks: int = 1,
                     phone_dir: Optional[str] = None, run_id: str = "r1", log=None, secrets=(),
                     reset_cursor: bool = False, until: Optional[str] = None, progress=None) -> dict:
    """Run `weeks` batches (default 1). Returns the last batch's result dict."""
    log = log or (lambda *a, **k: None)
    say = progress.note if progress is not None else print
    base_calendar = calendar or load_calendar(settings.reference_dir)
    today = to_date(today_override) if today_override else today_ist()
    floor = to_date(until) if until else to_date(HISTORY_FLOOR)
    settings.refetch_last_day = False      # week mode keeps the newest day pending instead of re-fetching it
    settings.warmup_items = set(WARMUP_ITEMS)
    settings.progress = progress

    if reset_cursor and _cursor_path(settings).exists() and not settings.dry_run:
        _cursor_path(settings).unlink()
        say("[Hafta] Cursor reset ho gaya. Jo din pehle se download hain woh dobara download nahi honge.")
    cursor = load_cursor(settings)
    if not _cursor_path(settings).exists() and (settings.state_dir / OLD_CURSOR_NAME).exists():
        say("[Hafta] Purana MAHINA wala cursor mila. Ab ek baar mein ek HAFTA aata hai, isliye aaj se naya "
            "hafta cursor shuru ho raha hai. Jo din pehle se download hain woh dobara download nahi honge.")

    if not settings.dry_run:
        from .repair import routing_failures
        n_routing = routing_failures(settings)
        if n_routing:
            say(REPAIR_HINT.format(n=n_routing))
        _load_rate_window(settings, client.limiter)
        recorder = RunLock(settings.state_dir, "check", name=RECORD_LOCK_NAME).holder()
        if recorder and recorder.get("calls_per_30min"):
            # one account, one limit: leave the running Recorder its share of the 30-minute cap
            share = int(recorder["calls_per_30min"])
            client.limiter.max_per_30min = max(100, client.limiter.max_per_30min - share)
            say(f"[Hafta] Recorder bhi chal raha hai: download ki 30-minute limit {share} calls kam karke "
                f"{client.limiter.max_per_30min} rakhi (dono ek hi Upstox limit share karte hain).")
        _ensure_probe(settings, client, base_calendar, secrets, say)
        # exchange-side PCR / max pain / OI / FII-DII first: Upstox drops them after about two weeks
        try:
            run_marketinfo(settings, client, cached_calendar(settings, base_calendar), today, log=log, say=say)
        except (ApiError, RetryExhausted) as err:
            say(f"[Exchange data] abhi save nahi ho paya ({type(err).__name__}); agli baar phir koshish hogi.")

    result = {}
    for _ in range(max(1, int(weeks))):
        result = _one_batch(settings, client, base_calendar, cursor, today, window_days, floor, phone_dir,
                            run_id, log, secrets, progress, say)
        if not result.get("completed"):
            break
    return result


def _one_batch(settings, client, base_calendar, cursor, today, window_days, floor, phone_dir, run_id, log,
               secrets, progress, say) -> dict:
    t_start = time.monotonic()
    pending_days = []
    batch = cursor.get("in_progress")
    if batch:
        start, end = to_date(batch["start_date"]), to_date(batch["end_date"])
        calendar = cached_calendar(settings, base_calendar) if settings.dry_run else sync_calendar(
            settings, client, base_calendar, _days(start, end), log)
        say(f"[Hafta #{batch['batch_no']}] Adhura hafta resume ho raha hai: {iso(start)} se {iso(end)}")
    else:
        recent = _days(today - dt.timedelta(days=10), today)
        calendar = cached_calendar(settings, base_calendar) if settings.dry_run else sync_calendar(
            settings, client, base_calendar, recent, log)
        if settings.dry_run:
            data_through = calendar.last_completed_trading_day(today)
        else:
            data_through, pending_days = detect_data_through(settings, client, calendar, today)
        nb = next_batch(cursor, today, data_through, window_days, floor)
        if nb is None:
            say(f"[Hafta] Poori history aa chuki hai ({iso(floor)} tak). Aur peeche jaana ho to --back-to YYYY-MM-DD do.")
            return {"completed": False, "reason": "HISTORY_FLOOR"}
        start, end = nb["start"], nb["end"]
        batch = {"batch_no": len(cursor.get("completed", [])) + 1, "kind": nb["kind"], "start_date": iso(start),
                 "end_date": iso(end), "next_end": iso(nb["next_end"]), "started_utc": utc_iso()}
        if not settings.dry_run:
            calendar = sync_calendar(settings, client, calendar, _days(start, end), log)
            cursor["anchor_today"] = cursor.get("anchor_today") or iso(today)
            cursor["in_progress"] = batch
            save_cursor(settings, cursor)
        kind_txt = {"first": "sabse naya hafta", "new": "naye din", "back": "ek hafta aur peeche",
                    "repair": "repair wale din (sirf jo chunks nahi aaye the)"}[nb["kind"]]
        say(f"[Hafta #{batch['batch_no']}] Aaj: {iso(today)} | {kind_txt}: {iso(start)} se {iso(end)}")
        for d in pending_days:
            say(f"[Hafta] {iso(d)} ka data Upstox par abhi nahi aaya (session ke baad kuch ghante lagte hain). "
                f"Agli baar `continue` chalane par yeh din apne aap aa jayega.")

    tag = f"week_{int(batch['batch_no']):02d}_{iso(start)}_to_{iso(end)}"
    settings.set_param("TEST_START", iso(start))
    settings.set_param("TEST_END", iso(end))

    if settings.dry_run:
        res = run_download(settings, client, calendar, run_id=run_id, log=log, include_warmup=True, secrets=secrets)
        return {"dry_run": True, "completed": False, "batch_no": batch["batch_no"], "start_date": iso(start),
                "end_date": iso(end), "plan_summary": res}

    since = batch["started_utc"]
    calls_before = client.calls_made
    if progress is not None:
        progress.stage(1)
    dl_res = {}
    for attempt in (1, 2):
        attempt_utc = utc_iso()
        dl_res = run_download(settings, client, calendar, run_id=run_id, log=log, include_warmup=True,
                              secrets=secrets)
        _save_rate_window(settings, client.limiter)
        if dl_res.get("interrupted") or dl_res.get("stopped_by_limit"):
            if progress is not None:
                progress.finish()
            say(f"[Hafta Pause] Hafta #{batch['batch_no']} ({iso(start)}..{iso(end)}) abhi adhura hai. "
                f"Wahi command dobara chalao, kaam wahin se aage badhega.")
            return {**dl_res, "completed": False}
        n_retry, _n_perm = _failed_since(settings, attempt_utc)
        if not n_retry:
            break
        if attempt == 2:
            if progress is not None:
                progress.finish()
            say(f"[Hafta Retry] {n_retry} chunks do baar try karke bhi nahi aaye (network / server). "
                f"Thodi der baad wahi command dobara chalao.")
            return {"completed": False, "incomplete": True, "pending_chunks": n_retry}
    fail_by = _failures_by_item(settings, since)
    n_perm = sum(fail_by.values())
    t_dl = time.monotonic()

    tick = progress.tick if progress is not None else None
    for number, fn in ((2, lambda: compact(settings, calendar, log=log, since_utc=since, tick=tick)),
                       (3, lambda: run_validate(settings, calendar, log=log, window=(start, end), since_utc=since,
                                                tick=tick)),
                       (4, lambda: run_derive(settings, calendar, window=(start, end), tick=tick))):
        if progress is not None:
            progress.stage(number)
        out = fn()
        if number == 3:
            val_res = out
    if progress is not None:
        progress.stage(5)
    calls = client.calls_made - calls_before
    dl_min = max(1e-9, (t_dl - t_start) / 60.0)
    lines = [f"API calls     : {calls} (retries {client.retries}, 429 {client.count_429}, "
             f"avg {calls / dl_min:.0f}/min)",
             f"Failed chunks : {n_perm} permanent (health.csv mein INCOMPLETE_CHUNK dekho)" if n_perm else
             "Failed chunks : 0"]
    ds = write_day_status(settings, calendar, start, end, pending_days)
    lines.append("Day status    : " + ", ".join(f"{k} {v}" for k, v in sorted(ds["counts"].items()))
                 + " (day_status.csv, holes.csv)")
    pkg = package_week(settings, tag, start, end, val_res, lines, phone_dir, tick)
    if progress is not None:
        progress.finish()

    from .repair import drop_repaired_days
    if batch.get("kind") != "repair":       # a repair batch re-visits old days: the walk cursors do not move
        newest = to_date(cursor["newest_done"]) if cursor.get("newest_done") else None
        cursor["newest_done"] = iso(max(end, newest) if newest else end)
        cursor["next_end"] = batch["next_end"]
    drop_repaired_days(cursor, start, end)
    cursor["in_progress"] = None
    cursor.setdefault("completed", []).append({**pkg, "batch_no": batch["batch_no"], "kind": batch.get("kind"),
                                                "completed_utc": utc_iso()})
    save_cursor(settings, cursor)

    total = time.monotonic() - t_start
    stage_s = dict(progress.stage_seconds) if progress is not None else {}
    complete = not fail_by
    head = f"HAFTA #{batch['batch_no']} POORA (COMPLETE)" if complete else f"HAFTA #{batch['batch_no']} ADHURA (INCOMPLETE)"
    box = [f"{head}: {iso(start)} se {iso(end)}",
           *([] if complete else [f"  nahi aaya -> {item} {und}: {n} chunks" for (item, und), n in sorted(fail_by.items())]
             + ["  Dobara lene ke liye: bash termux_run.sh repair  (phir continue)"]),
           f"Total time {int(total // 60)}m {int(total % 60)}s | calls {calls} | retries {client.retries} | "
           f"429 {client.count_429} | avg {calls / dl_min:.0f} calls/min",
           "Stage seconds: " + ", ".join(f"{k} {v:.0f}" for k, v in stage_s.items()) if stage_s else
           f"Valid days in store: {val_res.get('valid_trading_days', 0)}",
           f"Files {pkg['files']} | {pkg['mb']} MB",
           f"Phone folder: {pkg['phone_path']}" if pkg["phone_path"] else f"Folder (Termux): {pkg['week_dir']}",
           "Agla hafta: bash termux_run.sh continue"]
    if progress is not None:
        progress.box(box)
    else:
        for line in box:
            print(line)
    return {**pkg, "completed": True, "status": "COMPLETE" if complete else "INCOMPLETE",
            "failed": {f"{i} {u}": n for (i, u), n in fail_by.items()}, "day_status": ds["counts"],
            "batch_no": batch["batch_no"], "kind": batch.get("kind"), "calls": calls,
            "seconds": round(total, 1), "stage_seconds": stage_s, "pending_days": [iso(d) for d in pending_days]}


def _days(a: dt.date, b: dt.date) -> list:
    out, d = [], a
    while d <= b:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


# ---- fast lane: index and VIX history without options -------------------------------------------------------
SPOT_HISTORY_FROM = "2022-01-03"      # V3 1-minute candles start here (verified live 14 Nov 2026)
HISTORY_ITEMS = ["D12", "D01", "D02", "D03", "D04"]


def run_history(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
                today_override: Optional[str] = None, start: Optional[str] = None, run_id: str = "r1", log=None,
                secrets=(), progress=None) -> dict:
    """`python -m updl history`: spot and India VIX, 1-minute and daily, from Jan 2022 to the newest served day.
    About 250 calls for the 3 indices + VIX (one call per instrument per 28 days); days already covered are
    skipped, so a second run costs nothing. Options are not touched (they come week by week)."""
    log = log or (lambda *a, **k: None)
    say = progress.note if progress is not None else print
    base = calendar or load_calendar(settings.reference_dir)
    today = to_date(today_override) if today_override else today_ist()
    settings.refetch_last_day = False
    settings.warmup_items = set()
    settings.progress = progress
    settings.items_filter = list(HISTORY_ITEMS)
    first = to_date(start) if start else to_date(SPOT_HISTORY_FROM)
    if settings.dry_run:
        end = base.last_completed_trading_day(today)
    else:
        _load_rate_window(settings, client.limiter)
        _ensure_probe(settings, client, base, secrets, say)
        end, _pending = detect_data_through(settings, client, cached_calendar(settings, base), today)
    settings.set_param("TEST_START", iso(first))
    settings.set_param("TEST_END", iso(end))
    say(f"[History] Index + VIX ka 1-minute aur daily data: {iso(first)} se {iso(end)} (options nahi).")
    since, before = utc_iso(), client.calls_made
    if progress is not None and not settings.dry_run:
        progress.stage(1)
    res = run_download(settings, client, base, run_id=run_id, log=log, include_warmup=False, secrets=secrets)
    if settings.dry_run:
        return {"dry_run": True, "plan_summary": res}
    _save_rate_window(settings, client.limiter)
    if res.get("interrupted") or res.get("stopped_by_limit"):
        if progress is not None:
            progress.finish()
        say("[History] Adhura ruka. Wahi command dobara chalao, kaam wahin se aage badhega.")
        return {**res, "completed": False}
    if progress is not None:
        progress.stage(2)
    out = compact(settings, base, log=log, since_utc=since, tick=progress.tick if progress is not None else None)
    if progress is not None:
        progress.finish()
    calls = client.calls_made - before
    say(f"[History] Ho gaya: {calls} calls, {out['files_written']} din-files likhi. Dobara chalane par jo aa chuka "
        f"hai woh download nahi hoga.")
    return {"completed": True, "calls": calls, "files_written": out["files_written"], "start": iso(first),
            "end": iso(end)}
