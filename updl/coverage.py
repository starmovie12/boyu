"""`python -m updl coverage`: what is really on disk, and which days are ready for a strategy backtest.

Driven only by files on disk (Parquet parts, reports/day_status.csv, recorder files, owner files), never by docs.

* reports/data_coverage.csv     one row per data set: files, rows, first and last day, DELIVERED / MISSING /
                                OWNER-FILE-MISSING, and the reason
* reports/strategy_ready_days.csv  one row per (index, trading day): SPOT, FUT, OPT_BAND, OPT_WIDE, EVENT, SPREAD
                                as READY / PARTIAL / MISSING with a reason, plus two verdicts:
                                PRICE_READY (spot + future + option band) and FULL_READY (+ events + bid/ask spread)
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Optional

from .config import Settings
from .sessions import Calendar, instrument_class
from .store import list_parts, part_path, partition_of, read_parquet
from .timeutil import iso, to_date

READY, PARTIAL, MISSING = "READY", "PARTIAL", "MISSING"
COMPONENTS = ("SPOT", "FUT", "OPT_BAND", "OPT_WIDE", "EVENT", "SPREAD")
DAY_FIELDS = ["trading_date", "underlying", "PRICE_READY", "FULL_READY"] + \
    [f"{c}{suffix}" for c in COMPONENTS for suffix in ("", "_reason")]
DATA_SETS = [("D01", "spot_1m", "index 1-minute"), ("D02", "spot_1d", "index daily"), ("D03", "vix_1m", "VIX 1-minute"),
             ("D04", "vix_1d", "VIX daily"), ("D05", "fut_1m", "index futures 1-minute"),
             ("D06", "opt_1m", "options 1-minute, band"), ("D08", "opt_wide_1m", "options 1-minute, wide chain"),
             ("D09", "stock_1m", "stocks 1-minute"), ("D11", "global_1m", "global instruments 1-minute"),
             ("DER", "iv_greeks_1m", "derived IV / greeks"), ("DER", "chain_metrics_1m", "derived PCR / walls / max pain")]
OWNER_FILES = [("D13", "bhavcopy", "NSE F&O bhavcopy (external/bhavcopy/)"),
               ("D14", "participant_oi.csv", "participant-wise OI (external/participant_oi.csv)"),
               ("D18", "events.csv", "event calendar (external/events.csv)"),
               ("D19", "constituents.csv", "index constituents and weights (external/constituents.csv)"),
               ("CA", "corporate_actions.csv", "corporate actions (external/corporate_actions.csv)")]
SPOT_READY_FRACTION, FUT_READY_FRACTION = 0.99, 0.90


def _rows_in(path: Path) -> int:
    import pyarrow.parquet as pq
    try:
        return int(pq.read_metadata(path).num_rows)
    except (OSError, ValueError):
        return 0


def _day_status(settings: Settings) -> dict:
    p = Path(settings.reports_dir) / "day_status.csv"
    out = {}
    if p.exists():
        with open(p, "r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                out[(r["trading_date"], r["underlying"], r["table"])] = (r["status"], r.get("detail", ""))
    return out


def _events_range(settings: Settings):
    p = Path(settings.external_dir) / "events.csv"
    if not p.exists():
        return None
    days = []
    with open(p, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                days.append(to_date(str(r.get("event_ts_ist") or "")[:10]))
            except ValueError:
                continue
    return (min(days), max(days)) if days else None


def _options_status(settings, status_map, und, d, table) -> tuple:
    p = part_path(settings.store_dir, partition_of(d, settings.holdout_start), table, und, d)
    st, detail = status_map.get((iso(d), und, table), ("", ""))
    if st == "NOT_YET_SERVED":
        return MISSING, "Upstox ne yeh din abhi diya nahi; agla continue lega"
    if not p.exists():
        return MISSING, ("chunks fail hue: bash termux_run.sh repair" if st == "MISSING_ERROR"
                         else "download nahi hua (continue / repair)")
    if st == "MISSING_ERROR":
        return PARTIAL, "kuch contracts fail hue: bash termux_run.sh repair"
    return READY, f"{_rows_in(p)} rows"


def day_rows(settings: Settings, calendar: Calendar, start, end) -> list:
    start, end = to_date(start), to_date(end)
    status_map = _day_status(settings)
    events = _events_range(settings)
    rows = []
    for d in calendar.trading_days(start, end):
        for und in settings.instrument_names():
            exch = settings.exchange_of(und)
            part = partition_of(d, settings.holdout_start)
            r = {"trading_date": iso(d), "underlying": und}
            sp = part_path(settings.store_dir, part, "spot_1m", und, d)
            # expected bars come only from the session tables / API calendar (never a constant, test T02)
            exp_spot = calendar.expected_bars(instrument_class("spot", und, exchange=exch), d)
            n = _rows_in(sp) if sp.exists() else 0
            if not exp_spot:
                r["SPOT"], r["SPOT_reason"] = (PARTIAL if n else MISSING), f"{n} bars, session unknown (TABLE_GAP)"
            else:
                r["SPOT"] = READY if n >= SPOT_READY_FRACTION * exp_spot else PARTIAL if n else MISSING
                r["SPOT_reason"] = f"{n}/{exp_spot} bars"
            fp = part_path(settings.store_dir, part, "fut_1m", und, d)
            exp_fo = calendar.expected_bars(instrument_class("option", und, exchange=exch), d)
            if fp.exists():
                best = int(read_parquet(fp, columns=["instrument_key"])["instrument_key"].value_counts().max())
                r["FUT"] = READY if exp_fo and best >= FUT_READY_FRACTION * exp_fo else PARTIAL
                r["FUT_reason"] = f"best contract {best}/{exp_fo} bars"
            else:
                r["FUT"], r["FUT_reason"] = MISSING, "futures file nahi hai"
            r["OPT_BAND"], r["OPT_BAND_reason"] = _options_status(settings, status_map, und, d, "opt_1m")
            r["OPT_WIDE"], r["OPT_WIDE_reason"] = _options_status(settings, status_map, und, d, "opt_wide_1m")
            if events is None:
                r["EVENT"], r["EVENT_reason"] = MISSING, "owner file external/events.csv nahi hai"
            elif events[0] <= d <= events[1]:
                r["EVENT"], r["EVENT_reason"] = READY, "events.csv is din ko cover karti hai"
            else:
                r["EVENT"], r["EVENT_reason"] = MISSING, "events.csv is din tak nahi pahunchti"
            rec = Path(settings.workdir) / "recorder" / und / f"{iso(d)}.csv"
            if rec.exists():
                r["SPREAD"], r["SPREAD_reason"] = READY, "Recorder file hai (bid/ask)"
            else:
                r["SPREAD"], r["SPREAD_reason"] = MISSING, "Recorder ne record nahi kiya; purana bid/ask kabhi nahi milta"
            core = (r["SPOT"], r["FUT"], r["OPT_BAND"])
            r["PRICE_READY"] = READY if all(x == READY for x in core) else MISSING if r["SPOT"] == MISSING else PARTIAL
            if r["PRICE_READY"] == READY:
                r["FULL_READY"] = READY if r["EVENT"] == READY and r["SPREAD"] == READY else PARTIAL
            else:
                r["FULL_READY"] = r["PRICE_READY"]
            rows.append(r)
    return rows


def data_sets(settings: Settings) -> list:
    out = []
    for item, table, what in DATA_SETS:
        days, n_rows, files = set(), 0, 0
        for part in ("dev", "holdout"):
            for p in list_parts(settings.store_dir, part, table):
                days.add(to_date(p.parent.name.split("=", 1)[1]))
                n_rows += _rows_in(p)
                files += 1
        out.append({"item": item, "data": what, "files": files, "rows": n_rows,
                    "first_day": iso(min(days)) if days else "", "last_day": iso(max(days)) if days else "",
                    "status": "DELIVERED" if files else "MISSING",
                    "reason": "" if files else "abhi download nahi hua"})
    xc = Path(settings.workdir) / "xcheck_csv"
    xfiles = sorted(xc.rglob("*.csv")) if xc.exists() else []
    out.append({"item": "X01", "data": "exchange PCR / max pain / OI / FII-DII", "files": len(xfiles), "rows": "",
                "first_day": "", "last_day": "", "status": "DELIVERED" if xfiles else "MISSING",
                "reason": "" if xfiles else "continue chalao (sirf ~2 hafte peeche tak milta hai)"})
    rec = Path(settings.workdir) / "recorder"
    rdays = sorted({p.stem for p in rec.glob("*/????-??-??.csv")}) if rec.exists() else []
    out.append({"item": "REC", "data": "live bid/ask chain (Recorder)", "files": len(rdays), "rows": "",
                "first_day": rdays[0] if rdays else "", "last_day": rdays[-1] if rdays else "",
                "status": "DELIVERED" if rdays else "MISSING",
                "reason": "" if rdays else "market time mein: bash termux_run.sh record"})
    for item, name, what in OWNER_FILES:
        p = Path(settings.external_dir) / name
        have = p.exists() and (any(p.glob("*.csv")) if p.is_dir() else True)
        out.append({"item": item, "data": what, "files": int(have), "rows": "", "first_day": "", "last_day": "",
                    "status": "DELIVERED" if have else "OWNER-FILE-MISSING",
                    "reason": "" if have else "Upstox se nahi milta; aapko file deni hai (README section 18)"})
    return out


def _write(path: Path, fields: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_strategy_ready(settings: Settings, calendar: Calendar, start, end) -> dict:
    """Merge the window's day rows into reports/strategy_ready_days.csv. Returns counts for the window."""
    rows = day_rows(settings, calendar, start, end)
    path = Path(settings.reports_dir) / "strategy_ready_days.csv"
    touched = {(r["trading_date"], r["underlying"]) for r in rows}
    keep = []
    if path.exists():
        with open(path, "r", encoding="utf-8", newline="") as fh:
            keep = [r for r in csv.DictReader(fh) if (r.get("trading_date"), r.get("underlying")) not in touched]
    _write(path, DAY_FIELDS, sorted(keep + rows, key=lambda r: (r["trading_date"], r["underlying"])))
    counts = {"price": {}, "full": {}}
    for r in rows:
        counts["price"][r["PRICE_READY"]] = counts["price"].get(r["PRICE_READY"], 0) + 1
        counts["full"][r["FULL_READY"]] = counts["full"].get(r["FULL_READY"], 0) + 1
    return {"rows": len(rows), "counts": counts}


def summary_line(res: dict) -> str:
    p, f = res["counts"]["price"], res["counts"]["full"]
    return (f"Strategy-ready (index-din): price-data READY {p.get(READY, 0)}, PARTIAL {p.get(PARTIAL, 0)}, "
            f"MISSING {p.get(MISSING, 0)} | events + bid/ask ke saath READY {f.get(READY, 0)}")


def run_coverage(settings: Settings, calendar: Calendar, start=None, end=None, say=print) -> dict:
    sets = data_sets(settings)
    _write(Path(settings.reports_dir) / "data_coverage.csv",
           ["item", "data", "files", "rows", "first_day", "last_day", "status", "reason"], sets)
    days = sorted({to_date(p.parent.name.split("=", 1)[1]) for part in ("dev", "holdout")
                   for p in list_parts(settings.store_dir, part, "spot_1m")})
    first: Optional[dt.date] = to_date(start) if start else (days[0] if days else None)
    last: Optional[dt.date] = to_date(end) if end else (days[-1] if days else None)
    say("DATA COVERAGE (reports/data_coverage.csv)")
    for s in sets:
        span = f" {s['first_day']}..{s['last_day']}" if s["first_day"] else ""
        rows = f", {s['rows']} rows" if s["rows"] != "" else ""
        say(f"  {s['item']:<4} {s['status']:<18} {s['data']}{span} ({s['files']} files{rows}) {s['reason']}")
    if first is None:
        say("Store khali hai: pehle bash termux_run.sh continue chalao.")
        return {"data_sets": sets, "days": None}
    res = write_strategy_ready(settings, calendar, first, last)
    say(f"{iso(first)} se {iso(last)}: " + summary_line(res) + " (reports/strategy_ready_days.csv)")
    return {"data_sets": sets, "days": res}
