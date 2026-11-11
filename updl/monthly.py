"""Termux Month-by-Month Rolling Backwards Downloader (`python -m updl continue` / `month`).

Designed for mobile execution on Android Termux:
* Never pulls all 6 months at once when run via `continue` / `month`.
* On first run, automatically detects today's date in IST (e.g. 2026-11-11), determines the latest
  completed trading day (e.g. 2026-11-09), and downloads the most recent 1-month window
  (e.g. 2026-10-11 to 2026-11-09).
* Compacts, validates, derives, and packages that 1 month's output into a dedicated folder and `.zip`
  inside `<workdir>/phone_outputs/` (and copies to `/sdcard/Download/updl_months/` on Android if writable).
* Saves progress in `<workdir>/state/monthly_cursor.json`.
* Every time the owner runs `python -m updl continue` (or `bash termux_run.sh continue`) again,
  it automatically steps 1 month further back in time (e.g. 2026-09-11..2026-10-09, then
  2026-08-11..2026-09-10, and so on) and saves the new month's output file on the phone!
"""
from __future__ import annotations

import calendar as pycal
import datetime as dt
import json
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from .client import HttpClient
from .config import Settings
from .derive import run_derive
from .errors import ProbeMissing
from .export import DEFAULT_EVERY_MIN, copy_to_phone, run_export
from .plan import run_download
from .probe import check_probe_fresh, run_probe
from .sessions import Calendar, load_calendar
from .state import State
from .store import atomic_write_bytes, compact, date_of_part, list_parts
from .timeutil import iso, today_ist, to_date, utc_iso
from .validate import run_validate


def subtract_one_month(d: dt.date) -> dt.date:
    """Subtract 1 calendar month from date d (e.g. 2026-11-11 -> 2026-10-11)."""
    year = d.year
    month = d.month - 1
    if month == 0:
        month = 12
        year -= 1
    max_day = pycal.monthrange(year, month)[1]
    return dt.date(year, month, min(d.day, max_day))


def compute_month_window(anchor_end: dt.date, cal: Calendar, is_first_today: bool = False) -> tuple[dt.date, dt.date, dt.date]:
    """Given an anchor end date (e.g. 2026-11-11 on first run, or 2026-10-10 on second run), return:
    (window_start, window_end_trading_day, next_anchor_end)."""
    if is_first_today:
        raw_start = subtract_one_month(anchor_end)
        end_td = cal.last_completed_trading_day(anchor_end)
    else:
        raw_start = subtract_one_month(anchor_end + dt.timedelta(days=1))
        end_td = anchor_end if cal.is_trading_day(anchor_end) else cal.prev_trading_day(anchor_end)
    next_anchor = raw_start - dt.timedelta(days=1)
    return raw_start, end_td, next_anchor


def _cursor_path(settings: Settings) -> Path:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings.state_dir / "monthly_cursor.json"


def load_monthly_cursor(settings: Settings) -> dict:
    p = _cursor_path(settings)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "anchor_today": None,
        "next_anchor_end": None,
        "in_progress_batch": None,
        "completed_batches": [],
    }


def save_monthly_cursor(settings: Settings, cursor: dict):
    p = _cursor_path(settings)
    atomic_write_bytes(p, json.dumps(cursor, indent=2, sort_keys=True).encode("utf-8"), overwrite=True)


def _package_month_output(settings: Settings, batch_no: int, start_d: dt.date, end_d: dt.date,
                          val_res: dict, phone_dir: Optional[Path] = None) -> dict:
    out_root = settings.workdir / "phone_outputs"
    out_root.mkdir(parents=True, exist_ok=True)
    tag = f"month_{batch_no:02d}_{iso(start_d)}_to_{iso(end_d)}"
    month_dir = out_root / tag
    if month_dir.exists():
        shutil.rmtree(month_dir)
    month_dir.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    for part in ("dev", "holdout"):
        for p in list_parts(settings.store_dir, part):
            d = date_of_part(p)
            if start_d <= d <= end_d:
                rel = p.relative_to(settings.store_dir)
                dest = month_dir / "store" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                copied_files += 1

    for rep in ("health.csv", "health_summary.txt", "reconciliation.csv"):
        if (settings.reports_dir / rep).exists():
            shutil.copyfile(settings.reports_dir / rep, month_dir / rep)
    # v3 (Q4): phone-readable CSV "option chain at time T" for this month, next to the Parquet files
    exp = run_export(settings, start=start_d, end=end_d, out_dir=month_dir, make_zip=False,
                     folder_name="chain_csv")

    summary_lines = [
        f"UPDL MONTH BATCH #{batch_no:02d} COMPLETE",
        f"Mahina Window : {iso(start_d)} se {iso(end_d)}",
        f"Parquet Files : {copied_files}",
        f"CSV Files     : {exp['csv_files']} (chain_csv/: option chain har {DEFAULT_EVERY_MIN} minute, README_EXPORT.txt padho)",
        f"Valid Days    : {val_res.get('valid_trading_days', 0)}",
        f"Saved Folder  : {month_dir}",
        "Zip ke andar  : store/ (Parquet), chain_csv/ (CSV), health.csv, health_summary.txt, month_summary.txt",
        "Agla pichhla mahina nikalne ke liye chalao:",
        "  python -m updl continue",
    ]
    summary_text = "\n".join(summary_lines) + "\n"
    atomic_write_bytes(month_dir / "month_summary.txt", summary_text.encode("utf-8"), overwrite=True)

    zip_path = out_root / f"{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(month_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out_root).as_posix())

    # v3: copyfile (not copy2: Android shared storage can refuse the metadata copy) and a Hinglish hint
    # to run `termux-setup-storage` when the phone folder is not writable (it used to fail silently).
    sdcard_copied = copy_to_phone(zip_path, str(phone_dir) if phone_dir else None)
    if sdcard_copied:
        summary_copy = month_dir / "month_summary.txt"
        tmp_name = Path(sdcard_copied).with_suffix(".txt")
        try:
            shutil.copyfile(summary_copy, tmp_name)
        except OSError:
            pass

    return {
        "batch_no": batch_no,
        "start_date": iso(start_d),
        "end_date": iso(end_d),
        "parquet_files": copied_files,
        "month_dir": str(month_dir),
        "zip_path": str(zip_path),
        "sdcard_path": sdcard_copied,
    }


def run_monthly_continue(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
                         today_override: Optional[str] = None, with_warmup: bool = False,
                         phone_dir: Optional[str] = None, run_id: str = "r1",
                         log=None, secrets=()) -> dict:
    """Execute one 1-month step backwards in time (or finish an interrupted 1-month batch)."""
    calendar = calendar or load_calendar(settings.reference_dir)
    today_d = to_date(today_override) if today_override else today_ist()
    cursor = load_monthly_cursor(settings)

    if cursor.get("in_progress_batch"):
        ip = cursor["in_progress_batch"]
        batch_no = int(ip["batch_no"])
        start_d = to_date(ip["start_date"])
        end_d = to_date(ip["end_date"])
        next_anchor = to_date(ip["next_anchor_end"])
        print(f"[Termux Continue] Adhura Mahina Batch #{batch_no} resume ho raha hai: {iso(start_d)} se {iso(end_d)}")
    else:
        batch_no = len(cursor.get("completed_batches", [])) + 1
        if cursor.get("next_anchor_end"):
            anchor_end = to_date(cursor["next_anchor_end"])
            start_d, end_d, next_anchor = compute_month_window(anchor_end, calendar, is_first_today=False)
        else:
            cursor["anchor_today"] = iso(today_d)
            start_d, end_d, next_anchor = compute_month_window(today_d, calendar, is_first_today=True)
        cursor["in_progress_batch"] = {
            "batch_no": batch_no,
            "start_date": iso(start_d),
            "end_date": iso(end_d),
            "next_anchor_end": iso(next_anchor),
            "started_utc": utc_iso(),
        }
        save_monthly_cursor(settings, cursor)
        print(f"[Termux Month #{batch_no}] Aaj ki tarikh: {iso(today_d)} | "
              f"Nikal raha hai 1 mahina: {iso(start_d)} se {iso(end_d)}")

    settings.set_param("TEST_START", iso(start_d))
    settings.set_param("TEST_END", iso(end_d))
    if batch_no > 1:
        # an older month is already settled: the "re-fetch the last day next run" safeguard only costs calls
        settings.refetch_last_day = False

    if not settings.dry_run:
        try:
            check_probe_fresh(settings, secrets=secrets)
        except ProbeMissing as err:
            if "token" in (err.message or "").lower():
                raise
            # v3: the probe report is valid for 7 days; a month-by-month run spans weeks. Re-run it here
            # (at most 40 calls) instead of stopping the owner with exit 30.
            print("[Termux Continue] probe_report purana ya missing hai. Pehle probe chala raha hoon (max 40 calls)...")
            saved_budget = client.budget
            run_probe(settings, client, calendar=calendar)
            client.budget = saved_budget          # the probe set its own 40-call budget; restore the caller's

    if settings.dry_run:
        res = run_download(settings, client, calendar, run_id=run_id, log=log,
                           include_warmup=with_warmup, secrets=secrets)
        return {
            "dry_run": True,
            "batch_no": batch_no,
            "start_date": iso(start_d),
            "end_date": iso(end_d),
            "next_anchor_end": iso(next_anchor),
            "plan_summary": res,
        }

    dl_res = run_download(settings, client, calendar, run_id=run_id, log=log,
                          include_warmup=with_warmup, secrets=secrets)
    if dl_res.get("interrupted") or dl_res.get("stopped_by_limit"):
        print(f"[Termux Pause] Mahina #{batch_no} ({iso(start_d)}..{iso(end_d)}) abhi adhura hai. "
              f"Dobara `python -m updl continue` chalao.")
        return dl_res

    state = State(settings.state_dir)
    try:
        still_pending = state.retryable()
    finally:
        state.close()

    if still_pending:
        print(f"[Termux Retry Needed] {len(still_pending)} chunks retry baaki hain. "
              f"Dobara `python -m updl continue` chalao.")
        return {"incomplete": True, "batch_no": batch_no, "pending_chunks": len(still_pending)}

    compact(settings, calendar, log=log)
    val_res = run_validate(settings, calendar, log=log)
    run_derive(settings, calendar)

    pkg = _package_month_output(settings, batch_no, start_d, end_d, val_res,
                                phone_dir=Path(phone_dir) if phone_dir else None)

    cursor["completed_batches"].append({
        **pkg,
        "completed_utc": utc_iso(),
    })
    cursor["next_anchor_end"] = iso(next_anchor)
    cursor["in_progress_batch"] = None
    save_monthly_cursor(settings, cursor)

    next_s, next_e, _ = compute_month_window(next_anchor, calendar, is_first_today=False)
    print(f"Mahina #{batch_no} ({iso(start_d)} se {iso(end_d)}) save ho gaya: {pkg['zip_path']}")
    if pkg.get("sdcard_path"):
        print(f"Phone Download folder mein bhi copy ho gaya: {pkg['sdcard_path']}")
    print(f"Agla mahina ({iso(next_s)} se {iso(next_e)}) nikalne ke liye fir se chalao: python -m updl continue")
    return pkg
