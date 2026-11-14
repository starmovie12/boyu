"""Exchange-side cross-check data from Upstox "Market Information" (read-only GET), saved before it expires.

Seen live on 14 Nov 2026: `/v2/market/pcr`, `/max-pain`, `/oi` and `/change-oi` answer for a past day only about
two weeks back (30 Oct had data, 23 Oct returned null), so every day that is not saved is lost. `/fii` and `/dii`
return the last 30 daily rows. This module saves, per index, day and expiry: the 1-minute PCR series, the
1-minute max-pain series, open interest per strike and change in OI per strike; plus FII / DII daily rows.

* Raw answers: `raw/X01/<INDEX>/<day>_<expiry>_<kind>.json.gz`. Tidy tables: `xcheck_csv/<INDEX>/*.csv`.
* A null answer is recorded as null (never as zero). Null on an old day = outside retention: not asked again.
* `usable_from` on every row is the NEXT trading day 09:15 IST: the values of day D are for checking our own
  rebuilt chain, and may only be used as a signal from the next session on.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Optional

from .client import HttpClient
from .config import Settings
from .errors import ApiError, RetryExhausted
from .master import listed_contracts, parse_master_bytes, resolve_spot_key
from .selector import monthly_expiries
from .sessions import Calendar
from .store import atomic_write_bytes
from .timeutil import IST, iso, to_date, utc_iso

PATHS = {"pcr": "/v2/market/pcr", "maxpain": "/v2/market/max-pain", "oi": "/v2/market/oi",
         "chgoi": "/v2/market/change-oi"}
FII_PATH, DII_PATH = "/v2/market/fii", "/v2/market/dii"
FII_TYPES = ("NSE_FO|INDEX_FUTURES", "NSE_FO|INDEX_OPTIONS", "NSE_FO|STOCK_FUTURES", "NSE_FO|STOCK_OPTIONS",
             "NSE_EQ|CASH")
DII_TYPES = ("NSE_EQ|CASH",)
LOOKBACK_CALENDAR_DAYS = 24          # a little more than the retention seen live (15 to 22 days)
MAX_ERRORS_IN_A_ROW = 3              # the endpoint family is down or not available: stop, try again next run
RECENT_DAYS = 3                      # a null this recent may only mean "not published yet": ask again next run
LEDGER = "marketinfo.json"
ITEM = "X01"


def _params(kind: str, key: str, expiry: dt.date, day: dt.date) -> dict:
    p = {"instrument_key": key, "expiry": iso(expiry), "date": iso(day)}
    if kind in ("pcr", "maxpain"):
        p["bucket_interval"] = 1
    elif kind == "chgoi":
        p["interval"] = 5              # minutes ("1D" is rejected with HTTP 400)
    return p


def _ledger_path(settings: Settings) -> Path:
    return settings.state_dir / LEDGER


def load_ledger(settings: Settings) -> dict:
    try:
        return json.loads(_ledger_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _usable_from(calendar: Calendar, day: dt.date) -> str:
    nxt = calendar.shift(day, 1)
    return dt.datetime(nxt.year, nxt.month, nxt.day, 9, 15, tzinfo=IST).isoformat()


def tidy_rows(kind: str, data: dict, und: str, day: dt.date, expiry: dt.date, usable_from: str) -> list:
    """Flatten one answer. Series kinds give one row per minute, strike kinds one row per strike."""
    base = {"date": iso(day), "underlying": und, "expiry": iso(expiry), "usable_from": usable_from}
    if kind in ("pcr", "maxpain"):
        rows = data.get("insights") or []
    else:
        rows = next((v for v in data.values() if isinstance(v, list)), [])
    return [{**base, **{k: v for k, v in r.items() if not isinstance(v, (dict, list))}} for r in rows
            if isinstance(r, dict)]


def _append_csv(path: Path, rows: list) -> None:
    """Append rows; the header is fixed by the first write (missing values stay empty, never 0)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = None
    if path.exists() and path.stat().st_size:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh), None)
    new = header is None
    header = header or list(rows[0].keys())
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerows(rows)


def _known_expiries(settings: Settings, client: HttpClient, und: str, log) -> list:
    """Expiry dates of one index: today's master (listed) plus the expired list (one cheap call)."""
    exch = settings.exchange_of(und)
    out, key = set(), settings.spot_key(und)
    p = settings.raw_dir / "D12" / f"{exch}.json.gz"
    if p.exists():
        try:
            rows = parse_master_bytes(p.read_bytes())
            key, _ = resolve_spot_key(settings.spot_key(und), rows)
            out |= {c["expiry_date"] for c in listed_contracts(rows, key, kinds=("CE", "PE"))}
        except Exception:                       # a broken cached master must not stop the snapshot
            pass
    if key.startswith("RESOLVE_FROM_MASTER:"):
        return key, []
    try:
        res = client.get("/v2/expired-instruments/expiries", {"instrument_key": key}, kind="expired")
        out |= {to_date(x) for x in (res.json.get("data") or []) if x}
    except (ApiError, RetryExhausted) as err:
        log("marketinfo_expiries_failed", underlying=und, error=str(err)[:80])
    exps = sorted(out)
    return key, (monthly_expiries(exps) if settings.is_monthly(und) else exps)


def expiries_for(day: dt.date, expiries: list) -> list:
    """The current expiry of `day`; on the expiry day itself also the next one."""
    cur = [e for e in expiries if e >= day]
    return cur[:2] if cur and cur[0] == day else cur[:1]


def run_marketinfo(settings: Settings, client: HttpClient, calendar: Calendar, today: dt.date, log=None,
                   say=print) -> dict:
    """Save everything still inside retention that is not saved yet. Safe to call on every run."""
    log = log or (lambda *a, **k: None)
    ledger = load_ledger(settings)
    out_dir = settings.workdir / "xcheck_csv"
    last_day = calendar.last_completed_trading_day(today)
    days = calendar.trading_days(today - dt.timedelta(days=LOOKBACK_CALENDAR_DAYS), last_day)
    saved_days, calls, nulls, errors, streak = set(), 0, 0, 0, 0

    for und in settings.instrument_names():
        key, expiries = _known_expiries(settings, client, und, log)
        calls += 1
        if not expiries:
            continue
        for day in reversed(days):
            for expiry in expiries_for(day, expiries):
                for kind, path in PATHS.items():
                    lk = f"{und}|{iso(day)}|{iso(expiry)}|{kind}"
                    if ledger.get(lk) in ("data", "null") or streak >= MAX_ERRORS_IN_A_ROW:
                        continue
                    try:
                        res = client.get(path, _params(kind, key, expiry, day), kind="v3")
                        calls += 1
                        streak = 0
                    except (ApiError, RetryExhausted) as err:
                        errors += 1
                        streak += 1
                        log("marketinfo_failed", key=lk, error=str(err)[:80])
                        continue
                    data = res.json.get("data")
                    if not isinstance(data, dict) or not data:
                        nulls += 1
                        if (today - day).days > RECENT_DAYS:
                            ledger[lk] = "null"         # outside retention: gone for good, do not ask again
                        continue
                    raw = settings.raw_dir / ITEM / und / f"{iso(day)}_{iso(expiry)}_{kind}.json.gz"
                    atomic_write_bytes(raw, gzip.compress(res.body, mtime=0), overwrite=True)
                    _append_csv(out_dir / und / f"{kind}.csv",
                                tidy_rows(kind, data, und, day, expiry, _usable_from(calendar, day)))
                    if kind in ("pcr", "maxpain"):
                        head = {"date": iso(day), "underlying": und, "expiry": iso(expiry),
                                "usable_from": _usable_from(calendar, day),
                                **{k: v for k, v in data.items() if not isinstance(v, (dict, list))}}
                        _append_csv(out_dir / und / f"{kind}_daily.csv", [head])
                    ledger[lk] = "data"
                    saved_days.add(day)

    if streak >= MAX_ERRORS_IN_A_ROW:
        say("[Exchange data] Upstox ke market-info API ne lagataar error diya; is baar chhod diya, agli baar phir "
            "koshish hogi (yeh data ~2 hafte hi milta hai, isliye jaldi dobara chalana).")
        flows = {"calls": 0, "rows": 0}
    else:
        flows = _save_flows(settings, client, calendar, out_dir, log)
    calls += flows["calls"]
    if not settings.dry_run:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        ledger["_updated_utc"] = utc_iso()
        atomic_write_bytes(_ledger_path(settings), json.dumps(ledger, sort_keys=True).encode("utf-8"), overwrite=True)
    if saved_days:
        say(f"[Exchange data] {len(saved_days)} din ka PCR / max pain / OI save hua ({iso(min(saved_days))} se "
            f"{iso(max(saved_days))}), FII/DII {flows['rows']} lines. Folder: {out_dir}")
        say("[Exchange data] Upstox yeh data sirf ~2 hafte rakhta hai: isse purane din ab kabhi nahi mil sakte. "
            "Har hafte `continue` chalate raho. Din D ka data agle session se hi use karna.")
    return {"days_saved": sorted(iso(d) for d in saved_days), "calls": calls, "nulls": nulls, "errors": errors,
            "flow_rows": flows["rows"], "out_dir": str(out_dir)}


def _save_flows(settings: Settings, client: HttpClient, calendar: Calendar, out_dir: Path, log) -> dict:
    """FII and DII daily rows (last 30 per call); the CSV keeps one line per (who, data_type, date)."""
    path = out_dir / "fii_dii_daily.csv"
    rows, calls = {}, 0
    if path.exists():
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                rows[(r.get("who"), r.get("data_type"), r.get("date"))] = r
    before = len(rows)
    for who, api, types in (("FII", FII_PATH, FII_TYPES), ("DII", DII_PATH, DII_TYPES)):
        for data_type in types:
            try:
                res = client.get(api, {"data_type": data_type, "interval": "1D"}, kind="v3")
                calls += 1
            except (ApiError, RetryExhausted) as err:
                log("marketinfo_flow_failed", who=who, data_type=data_type, error=str(err)[:80])
                break                       # same endpoint family: do not keep asking after an error
            data = res.json.get("data")
            for r in ((data or {}).get(data_type) or []) if isinstance(data, dict) else []:
                if not isinstance(r, dict) or not r.get("time_stamp"):
                    continue
                day = dt.datetime.fromtimestamp(int(r["time_stamp"]) / 1000.0, tz=IST).date()
                rows[(who, data_type, iso(day))] = {"who": who, "data_type": data_type, "date": iso(day),
                                                    "usable_from": _usable_from(calendar, day),
                                                    **{k: v for k, v in r.items() if k != "time_stamp"}}
    if rows and not settings.dry_run:
        header = []
        for r in rows.values():
            header += [k for k in r if k not in header]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=header, restval="")
            w.writeheader()
            w.writerows(sorted(rows.values(), key=lambda r: (r["date"], r["who"], r["data_type"])))
    return {"calls": calls, "rows": len(rows) - before}


def compare_with_own(settings: Settings, und: str, day: dt.date) -> Optional[dict]:
    """Exchange-side closing PCR / max pain of `day` next to our rebuilt values (None when either is missing)."""
    import pandas as pd
    from .store import part_path, partition_of, read_parquet
    daily = {}
    for kind in ("pcr", "maxpain"):
        p = settings.workdir / "xcheck_csv" / und / f"{kind}_daily.csv"
        if not p.exists():
            return None
        df = pd.read_csv(p, dtype=str)
        df = df[(df["date"] == iso(day))].sort_values("expiry")
        if df.empty:
            return None
        daily[kind] = df.iloc[0]
    own_p = part_path(settings.store_dir, partition_of(day, settings.holdout_start), "chain_metrics_1m", und, day)
    if not own_p.exists():
        return None
    own = read_parquet(own_p)
    own = own[own["expiry_date"].astype(str) == daily["pcr"]["expiry"]].sort_values("ts_open")
    if own.empty:
        return None
    last = own.iloc[-1]
    return {"date": iso(day), "underlying": und, "expiry": daily["pcr"]["expiry"],
            "exchange_pcr": float(daily["pcr"]["pcr"]), "own_pcr": float(last["pcr_DERIVED"]),
            "exchange_max_pain": float(daily["maxpain"]["max_pain"]), "own_max_pain": float(last["max_pain_DERIVED"]),
            "own_strikes": int(last["n_strikes_DERIVED"])}
