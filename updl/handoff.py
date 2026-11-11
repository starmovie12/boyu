"""Stage 4 Handoff, Verification and One-Screen Report (`python -m updl handoff | verify | report`).

Implements Section 10 & 11:
* `handoff`: populates `handoff/` with `store/` (dev and holdout trees), `SCHEMA.md`, `MANIFEST.sha256`,
  `HOLDOUT_SEALED.json`, `README_OWNER_HINGLISH.md`, `reports/`, and `reference/`.
  Holdout files are made read-only (os.chmod 0o444; cross-platform).
  If `SYNTHETIC_FIXTURE` marker is requested or fixture mode is active, writes `SYNTHETIC_FIXTURE_NOT_A_RESULT.txt`.
* `verify`: re-checks `MANIFEST.sha256` on any OS using Python's hashlib (no dependency on `sha256sum` CLI).
* `report`: prints the one-screen Hinglish summary (plan, valid trading days, NOT OK days, calls made,
  calls remaining, estimated minutes remaining, disk used, DATA_RIGHTS status, NA-DATA checks).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Optional

from .config import Settings
from .errors import CorruptFile
from .messages import VERIFY_BAD, VERIFY_OK
from .sessions import Calendar, load_calendar
from .state import State
from .store import atomic_write_bytes, list_parts, sha256_file
from .timeutil import iso, utc_iso

SCHEMA_MD_TEXT = """# UPDL DATA STORE SCHEMA (v2)

* Timezone: `Asia/Kolkata` (IST, +05:30, no daylight saving).
* `ts_open`: start timestamp of the candle (inclusive).
* `ts_close`: end timestamp of the candle (`ts_open + interval`; for daily candles `23:59:59.999` IST).
* `has_volume`: `false` for index (`spot_1m`, `spot_1d`), India VIX (`vix_1m`, `vix_1d`), and global instruments (`global_1m`); `true` for futures, options, and cash stocks.
* `is_stale`: `true` ONLY when `has_volume` is `true` AND `v == 0`; always `false` when `has_volume` is `false`.
* `is_filled`: `false` on all raw/normalised rows (synthetic/forward-filled values are forbidden by Rule 4).
* `volume_unit`: `UNVERIFIED` until reconciled against NSE UDiFF bhavcopy (`reports/reconciliation.csv`).
* `holdout_rule`: rows with `trading_date >= HOLDOUT_START` live physically under `store/holdout/`; `updl.load.load_dev` refuses them, and `load_holdout` requires `OPEN_HOLDOUT_ONCE=1` and logs a one-time hash-chained entry to `holdout_log.jsonl`.
* `session_table_version`: 2 (`reference/sessions.yaml`).
* Same-minute stop and target convention: stop first.

## 1. Raw / Normalised Tables (`RAW`)
Tables: `spot_1m`, `spot_1d`, `vix_1m`, `vix_1d`, `fut_1m`, `opt_1m`, `opt_wide_1m`, `stock_1m`, `global_1m`

| Column | Dtype | Label | Description |
|---|---|---|---|
| `instrument_id` | string | RAW | Canonical instrument identifier |
| `instrument_key` | string | RAW | Upstox instrument or expired contract key |
| `underlying` | string | RAW | Underlying symbol (`NIFTY`, `BANKNIFTY`, `SENSEX`, `INDIAVIX`, stock) |
| `segment` | string | RAW | Exchange segment (`NSE_INDEX`, `NSE_FO`, `BSE_FO`, `NSE_EQ`) |
| `expiry_date` | date / null | RAW | Contract expiry date in IST |
| `strike` | float64 / null | RAW | Option strike price |
| `option_type` | string / null | RAW | `CE`, `PE`, `FUT`, `SPOT`, `EQ`, `GLOBAL` |
| `interval` | string | RAW | `1m` or `1d` |
| `ts_open` | timestamp[Asia/Kolkata] | RAW | Candle open time |
| `ts_close` | timestamp[Asia/Kolkata] | RAW | Candle close time |
| `o`, `h`, `l`, `c` | float64 | RAW | Open, High, Low, Close prices |
| `v` | Int64 (nullable) | RAW | Traded volume (null kept null, never turned into 0) |
| `oi` | Int64 (nullable) | RAW | Open interest (`oi_timing = per_candle_UNVERIFIED` until P20 confirms) |
| `has_volume` | bool | RAW | Whether the instrument class carries traded volume |
| `is_stale` | bool | RAW | True when `has_volume` is true and `v == 0` |
| `is_filled` | bool | RAW | Always false in raw tables |
| `session_type` | string | RAW | `NORMAL`, `MUHURAT`, `BUDGET_SUNDAY`, `UNEXPECTED` |
| `trading_date` | date | RAW | IST session date |
| `source` | string | RAW | `upstox_v3` or `upstox_expired_v2` |
| `adjusted_status` | string | RAW | `UNVERIFIED` |
| `fetch_run_id` | string | RAW | Run identifier |
| `raw_sha256` | string | RAW | SHA-256 hex digest of the source `.json.gz` file |

## 2. Derived & Proxy Tables (`DERIVED` / `PROXY`)
Every derived/proxy row carries `available_from_ts` (IST): the first minute at which a live bot could know the value.
* `bars_3m`, `bars_15m`: `o_DERIVED`, `h_DERIVED`, `l_DERIVED`, `c_DERIVED`, `bar_count_DERIVED`, `is_partial`, `available_from_ts`.
* `chain_1m`: `ce_close_DERIVED`, `ce_v_DERIVED`, `ce_oi_DERIVED`, `ce_is_stale`, `pe_close_DERIVED`, `pe_v_DERIVED`, `pe_oi_DERIVED`, `pe_is_stale`, `available_from_ts`.
* `iv_greeks_1m`: `forward_DERIVED`, `forward_source`, `iv_DERIVED`, `delta_DERIVED`, `gamma_DERIVED`, `theta_DERIVED`, `iv_status`, `volume_baseline_same_dte_DERIVED`, `volume_baseline_any_dte_DERIVED`, `half_spread_PROXY`, `modelled_ask_PROXY`, `fillable`, `available_from_ts`.
* `chain_metrics_1m`: `pcr_DERIVED`, `call_oi_wall_DERIVED`, `put_oi_wall_DERIVED`, `gex_proxy_PROXY`, `available_from_ts`.
* `daily_metrics_DERIVED`: `rv20_DERIVED`, `ivp_vix_PROXY`, `ivp_status`, `version`, `available_from_ts`.
"""


def _make_readonly(path: Path):
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _make_writable_tree(root: Path):
    if not root.exists():
        return
    for p in root.rglob("*"):
        if p.is_file():
            try:
                os.chmod(p, 0o666)
            except OSError:
                pass


def run_handoff(settings: Settings, synthetic_fixture: bool = False) -> dict:
    hdir = settings.handoff_dir
    _make_writable_tree(hdir)
    hdir.mkdir(parents=True, exist_ok=True)

    # Copy store/
    dest_store = hdir / "store"
    if dest_store.exists():
        _make_writable_tree(dest_store)
        shutil.rmtree(dest_store)
    if settings.store_dir.exists():
        shutil.copytree(settings.store_dir, dest_store)
    else:
        (dest_store / "dev").mkdir(parents=True, exist_ok=True)
        (dest_store / "holdout").mkdir(parents=True, exist_ok=True)

    # Copy reports/
    dest_rep = hdir / "reports"
    if dest_rep.exists():
        shutil.rmtree(dest_rep)
    if settings.reports_dir.exists():
        shutil.copytree(settings.reports_dir, dest_rep)
    else:
        dest_rep.mkdir(parents=True, exist_ok=True)

    # Copy reference/
    dest_ref = hdir / "reference"
    if dest_ref.exists():
        shutil.rmtree(dest_ref)
    if settings.reference_dir.exists():
        shutil.copytree(settings.reference_dir, dest_ref)

    # Copy README_OWNER_HINGLISH.md
    readme_src = settings.root / "README_OWNER_HINGLISH.md"
    readme_dest = hdir / "README_OWNER_HINGLISH.md"
    if readme_src.exists():
        atomic_write_bytes(readme_dest, readme_src.read_bytes(), overwrite=True)
    else:
        atomic_write_bytes(readme_dest, b"# README_OWNER_HINGLISH\n", overwrite=True)

    # Write SCHEMA.md
    atomic_write_bytes(hdir / "SCHEMA.md", SCHEMA_MD_TEXT.encode("utf-8"), overwrite=True)

    if synthetic_fixture or (settings.workdir / "SYNTHETIC_FIXTURE_NOT_A_RESULT.txt").exists():
        msg = "SYNTHETIC_FIXTURE: machinery proof only. Nothing in this folder is a trading result.\n"
        atomic_write_bytes(hdir / "SYNTHETIC_FIXTURE_NOT_A_RESULT.txt", msg.encode("utf-8"), overwrite=True)
        atomic_write_bytes(settings.workdir / "SYNTHETIC_FIXTURE_NOT_A_RESULT.txt", msg.encode("utf-8"), overwrite=True)

    # Seal holdout files and write HOLDOUT_SEALED.json
    holdout_files = {}
    for hp in list_parts(dest_store, "holdout"):
        rel = hp.relative_to(hdir).as_posix()
        holdout_files[rel] = sha256_file(hp)
        _make_readonly(hp)

    sealed = {
        "holdout_start": iso(settings.holdout_start),
        "created_utc": utc_iso(),
        "holdout_file_count": len(holdout_files),
        "files": holdout_files,
    }
    atomic_write_bytes(hdir / "HOLDOUT_SEALED.json",
                       json.dumps(sealed, indent=2, sort_keys=True).encode("utf-8"), overwrite=True)

    # Build MANIFEST.sha256 for every file in handoff/ except MANIFEST.sha256 itself
    manifest_lines = []
    for f in sorted(hdir.rglob("*")):
        if f.is_file() and f.name != "MANIFEST.sha256":
            rel = f.relative_to(hdir).as_posix()
            manifest_lines.append(f"{sha256_file(f)}  {rel}")
    atomic_write_bytes(hdir / "MANIFEST.sha256",
                       ("\n".join(manifest_lines) + "\n").encode("utf-8"), overwrite=True)

    return {
        "handoff_dir": str(hdir),
        "total_files": len(manifest_lines),
        "holdout_files": len(holdout_files),
    }


def run_verify(settings: Settings) -> dict:
    hdir = settings.handoff_dir
    mpath = hdir / "MANIFEST.sha256"
    if not mpath.exists():
        print(VERIFY_BAD)
        raise CorruptFile("MANIFEST.sha256 not found in handoff directory")

    bad = []
    checked = 0
    for line in mpath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        expected_sha, rel = line.split("  ", 1)
        fpath = hdir / rel
        checked += 1
        if not fpath.exists() or sha256_file(fpath) != expected_sha:
            bad.append(rel)

    if bad:
        print(VERIFY_BAD)
        for b in bad:
            print(f"  {b}")
        raise CorruptFile("Verification failed for: " + ", ".join(bad[:5]))

    print(VERIFY_OK)
    return {"status": "OK", "checked_files": checked}


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def run_report(settings: Settings, calendar: Optional[Calendar] = None) -> dict:
    calendar = calendar or load_calendar(settings.reference_dir)
    plan_name = settings.plan_override or "UNSET"
    if settings.probe_json.exists():
        try:
            pdata = json.loads(settings.probe_json.read_text(encoding="utf-8"))
            plan_name = settings.plan_override or pdata.get("selected_plan", "UNSET")
        except Exception:
            pass

    state = State(settings.state_dir) if (settings.state_dir / "progress.sqlite3").exists() else None
    calls_made = state.total_attempts() if state else 0
    if state:
        state.close()

    total_planned = 0
    plan_p = settings.workdir / "selection_plan.parquet"
    if plan_p.exists():
        try:
            import pandas as pd
            dfp = pd.read_parquet(plan_p)
            total_planned = int(dfp["chunks"].sum())
        except Exception:
            total_planned = 0

    calls_rem = max(0, total_planned - calls_made)
    est_min_rem = round(calls_rem / max(1.0, settings.rate_per_min), 1)

    valid_days, not_ok_days = 0, 0
    hcsv = settings.reports_dir / "health.csv"
    if hcsv.exists():
        import pandas as pd
        hdf = pd.read_csv(hcsv)
        if not hdf.empty:
            valid_days = int(hdf[hdf["status"].isin(["OK", "PROVISIONAL"])]["trading_date"].nunique())
            not_ok_days = int(hdf[hdf["status"] == "NOT OK"]["trading_date"].nunique())

    disk_mb = round(_dir_bytes(settings.workdir) / (1024.0 * 1024.0), 2)
    na_checks = []
    for fn in ("events.csv", "constituents.csv", "participant_oi.csv"):
        if not (settings.external_dir / fn).exists():
            na_checks.append(fn)
    if not (settings.external_dir / "bhavcopy").exists():
        na_checks.append("bhavcopy/")

    lines = [
        "=== UPDL REPORT (Hinglish Summary) ===",
        f"Plan: {plan_name} | Window: {iso(settings.test_start)} se {iso(settings.test_end)}",
        f"Valid trading din: {valid_days} | NOT OK din: {not_ok_days}",
        f"Calls poore huye: {calls_made} | Calls baaki: {calls_rem}",
        f"Baaki samay ({settings.rate_per_min}/min par): ~{est_min_rem} minute",
        f"Disk use hua: {disk_mb} MB | DATA_RIGHTS: {settings.data_rights}",
        f"NA-DATA external files: {', '.join(na_checks) if na_checks else 'Koi nahi'}",
    ]
    print("\n".join(lines))
    return {
        "plan": plan_name,
        "valid_days": valid_days,
        "not_ok_days": not_ok_days,
        "calls_made": calls_made,
        "calls_remaining": calls_rem,
        "estimated_minutes_remaining": est_min_rem,
        "disk_mb": disk_mb,
        "data_rights": settings.data_rights,
        "na_data": na_checks,
    }
