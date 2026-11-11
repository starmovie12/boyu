"""Phone-friendly CSV export (`python -m updl export`).

Builds, from the downloaded 1-minute candles (Parquet store), an "option chain at time T" view that opens in any
phone spreadsheet app, and zips it. Parquet stays the main store; this is only a readable copy.

Inside the zip (one folder per underlying):
* chain_<UNDERLYING>_<YYYY-MM-DD>.csv    one row per (time, expiry, strike): spot, CE and PE close, volume, OI
* summary_<UNDERLYING>_<YYYY-MM-DD>.csv  one row per (time, expiry): spot, PCR, call/put OI wall, max pain
* README_EXPORT.txt                      what every column means (Hinglish + English)
Values come only from candles closed at or before the row's time; carried values are flagged (see updl.chain).
"""
from __future__ import annotations

import datetime as dt
import shutil
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd

from .chain import asof_chain, chain_metrics, snapshot_times, spot_asof
from .config import Settings
from .sessions import Calendar, instrument_class, load_calendar
from .store import date_of_part, list_parts, part_path, partition_of, read_parquet
from .timeutil import iso, to_date

OPTION_TABLES = ("opt_1m", "opt_wide_1m")
DEFAULT_EVERY_MIN = 15

README_EXPORT = """UPDL EXPORT - option chain at time T (CSV)
=========================================

Yeh files Upstox ke 1-minute candles se banayi gayi hain (expired + live contracts).
Upstox purani option chain (bid/ask/IV/greeks) nahi deta, isliye chain yahan candles se
"time T par jo pata tha" ke hisaab se dobara banayi gayi hai. Koi value future se nahi li gayi.

Files
-----
chain_<INDEX>_<DATE>.csv    har time, har expiry, har strike ki ek line
summary_<INDEX>_<DATE>.csv  har time, har expiry ki ek line (PCR, OI walls, max pain)

chain_*.csv columns
-------------------
time            IST time T (HH:MM). Sirf woh candles jo T tak band ho chuki thi.
expiry_date     contract expiry (YYYY-MM-DD)
strike          strike price
spot_asof       index ka last 1-minute close jo T tak pata tha (khali = abhi tak nahi)
ce_close        CE ka last traded close (T ya usse pehle)
ce_vol_1m       CE volume us minute ka jo T par khatam hua (khali = us minute trade nahi hua)
ce_vol_cum      CE ka din bhar ka volume T tak (units/quantity, lots nahi)
ce_oi           CE open interest, last known (units/quantity, lots nahi)
ce_age_min      last candle kitne minute purani hai (0 = bilkul taaza)
ce_is_filled    true = value purani candle se aage laayi gayi (carried), false = taaza
pe_*            wahi sab PE ke liye
Khali cell = data nahi tha. Kabhi bhi 0 se nahi bhara gaya.

summary_*.csv columns
---------------------
time, expiry_date, spot_asof
pcr             put OI / call OI (sirf downloaded strikes ka band, poori exchange chain nahi)
call_oi_wall    spot ke upar sabse zyada CE OI wala strike
put_oi_wall     spot ke neeche sabse zyada PE OI wala strike
max_pain        woh strike jahan option buyers ka total intrinsic value sabse kam ho (band ke andar)
n_strikes       kitne strikes the
n_oi_known      kitne CE+PE OI values pata thi
n_oi_carried    kitni values purani candle se aage laayi gayi (zyada = kam bharosa)

Important
---------
* Bid/ask, IV aur greeks history Upstox se nahi milti. Woh sirf Recorder (python -m updl record)
  market time mein live save karta hai.
* Volume aur OI Upstox units (quantity) mein deta hai, lots mein nahi. Lots = units / lot size.
* Yeh data sirf apne use ke liye hai. Kisi ko mat do.
"""


def _option_frame(settings: Settings, und: str, d: dt.date) -> pd.DataFrame:
    part = partition_of(d, settings.holdout_start)
    frames = []
    for tbl in OPTION_TABLES:
        p = part_path(settings.store_dir, part, tbl, und, d)
        if p.exists():
            frames.append(read_parquet(p))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(["instrument_key", "ts_open"], keep="last")


def _fmt_time(ts) -> str:
    return pd.Timestamp(ts).strftime("%H:%M")


def export_day(settings: Settings, calendar: Calendar, und: str, d: dt.date,
               every_min: int = DEFAULT_EVERY_MIN) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (chain_rows, summary_rows) for one underlying and day; empty frames when there is no data."""
    odf = _option_frame(settings, und, d)
    if odf.empty:
        return pd.DataFrame(), pd.DataFrame()
    exch = settings.exchange_of(und)
    o_hhmm, c_hhmm = calendar.session_times(instrument_class("option", und, exchange=exch), d)
    if not o_hhmm or not c_hhmm:
        o_hhmm, c_hhmm = "09:15", "15:30"
    times = snapshot_times(d, o_hhmm, c_hhmm, every_min)
    sp = part_path(settings.store_dir, partition_of(d, settings.holdout_start), "spot_1m", und, d)
    spot_df = read_parquet(sp) if sp.exists() else pd.DataFrame()
    spots = {pd.Timestamp(t): v for t, v in zip(times, spot_asof(spot_df, times))}

    snap = asof_chain(odf, times)
    if snap.empty:
        return pd.DataFrame(), pd.DataFrame()
    snap.insert(0, "time", [_fmt_time(t) for t in snap["snapshot_ts"]])
    snap.insert(3, "spot_asof", [spots.get(pd.Timestamp(t), float("nan")) for t in snap["snapshot_ts"]])
    summary = []
    for (ts, exp), g in snap.groupby(["snapshot_ts", "expiry_date"], sort=True):
        spot = spots.get(pd.Timestamp(ts), float("nan"))
        met = chain_metrics(g, None if pd.isna(spot) else spot)
        summary.append({"time": _fmt_time(ts), "expiry_date": iso(exp), "spot_asof": spot,
                        "pcr": met["pcr"], "call_oi_wall": met["call_oi_wall"],
                        "put_oi_wall": met["put_oi_wall"], "max_pain": met["max_pain"],
                        "n_strikes": met["n_strikes"], "n_oi_known": met["n_oi_known"],
                        "n_oi_carried": met["n_oi_carried"]})
    chain = snap.drop(columns=["snapshot_ts", "ce_last_ts", "pe_last_ts"])
    chain["expiry_date"] = [iso(x) for x in chain["expiry_date"]]
    return chain, pd.DataFrame(summary)


def _days_in_store(settings: Settings, und: str, start: dt.date, end: dt.date) -> list:
    days = set()
    for part in ("dev", "holdout"):
        for tbl in OPTION_TABLES:
            for p in list_parts(settings.store_dir, part, tbl, und):
                d = date_of_part(p)
                if start <= d <= end:
                    days.add(d)
    return sorted(days)


def run_export(settings: Settings, calendar: Optional[Calendar] = None, start=None, end=None,
               every_min: int = DEFAULT_EVERY_MIN, out_dir: Optional[Path] = None,
               zip_name: Optional[str] = None, phone_dir: Optional[str] = None, make_zip: bool = True,
               folder_name: Optional[str] = None) -> dict:
    """Write the CSV files and (by default) one zip. Returns paths and counts. Never calls the network."""
    calendar = calendar or load_calendar(settings.reference_dir)
    start = to_date(start) if start else settings.test_start
    end = to_date(end) if end else settings.test_end
    out_root = Path(out_dir) if out_dir else settings.workdir / "exports"
    folder = out_root / (folder_name or f"csv_{iso(start)}_{iso(end)}")
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)

    n_files, n_rows, days_done = 0, 0, 0
    for und in settings.instrument_names():
        for d in _days_in_store(settings, und, start, end):
            chain, summary = export_day(settings, calendar, und, d, every_min)
            if chain.empty:
                continue
            sub = folder / und
            sub.mkdir(parents=True, exist_ok=True)
            chain.to_csv(sub / f"chain_{und}_{iso(d)}.csv", index=False)
            summary.to_csv(sub / f"summary_{und}_{iso(d)}.csv", index=False)
            n_files += 2
            n_rows += len(chain)
            days_done += 1
    (folder / "README_EXPORT.txt").write_text(README_EXPORT, encoding="utf-8")

    if not make_zip:
        return {"zip_path": None, "folder": str(folder), "csv_files": n_files, "chain_rows": n_rows,
                "days": days_done, "phone_path": None}
    zip_path = out_root / (zip_name or f"chain_{iso(start)}_{iso(end)}.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(folder).as_posix())
    phone_copy = copy_to_phone(zip_path, phone_dir)
    if n_files == 0:
        print("Export: is window mein option candles nahi mile (pehle download / continue chalao).")
    else:
        print(f"Export ready: {zip_path} ({days_done} din, {n_rows} chain lines)")
    if phone_copy:
        print(f"Phone Download folder mein bhi copy: {phone_copy}")
    return {"zip_path": str(zip_path), "folder": str(folder), "csv_files": n_files, "chain_rows": n_rows,
            "days": days_done, "phone_path": phone_copy}


def copy_to_phone(path: Path, phone_dir: Optional[str] = None) -> Optional[str]:
    """Copy one file to the phone's Download folder (Termux needs `termux-setup-storage` once).
    copyfile, not copy2: Android shared storage can refuse the metadata copy. Returns the path or None."""
    from .messages import SDCARD_HINT
    target = Path(phone_dir) if phone_dir else Path("/sdcard/Download/updl_months")
    if not phone_dir and not Path("/sdcard").exists():
        return None                       # not a phone: nothing to do, no message
    try:
        target.mkdir(parents=True, exist_ok=True)
        dest = target / Path(path).name
        shutil.copyfile(path, dest)
        return str(dest)
    except OSError:
        print(SDCARD_HINT, path)
        return None
