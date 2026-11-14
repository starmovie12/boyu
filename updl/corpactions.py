"""Verdict for `adjusted_status`: are Upstox stock candles adjusted for splits and bonuses?

For each SPLIT / BONUS row in the owner's external/corporate_actions.csv whose stock is in the store (stock_1m),
compare the last close before the ex-date with the first open on the ex-date:

* jump = prev_close / ex_open; expected factor: SPLIT a:b (new:old) = a/b, BONUS a:b (new for held) = (a+b)/b
* jump within 15% of the factor -> UNADJUSTED (raw prices: the action shows as a price gap)
* jump within 10% of 1.0       -> ADJUSTED   (history was rescaled)
* otherwise UNCLEAR; no candles on either side -> NO_DATA

reports/adjusted_status.csv lists every row; the overall verdict is printed. The Parquet column
`adjusted_status` stays `UNVERIFIED` until a real sample says otherwise (nothing is guessed).
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import pandas as pd

from .config import Settings
from .ownerfiles import RATIO_RX
from .sessions import Calendar
from .store import part_path, partition_of, read_parquet
from .timeutil import iso, to_date

FIELDS = ["symbol", "ex_date", "type", "ratio", "factor", "prev_day", "prev_close", "ex_open", "jump", "verdict"]
UNADJ_TOL, ADJ_TOL = 0.15, 0.10


def _factor(kind: str, ratio: str):
    if not RATIO_RX.match(ratio or ""):
        return None
    a, b = (float(x) for x in ratio.split(":"))
    if a <= 0 or b <= 0:
        return None
    return a / b if kind == "SPLIT" else (a + b) / b


def _candles(settings: Settings, sym: str, d) -> pd.DataFrame:
    p = part_path(settings.store_dir, partition_of(d, settings.holdout_start), "stock_1m", sym, d)
    if not p.exists():
        return pd.DataFrame()
    return read_parquet(p, columns=["ts_open", "o", "c"]).sort_values("ts_open", kind="stable")


def verdict_rows(settings: Settings, calendar: Calendar) -> list:
    src = Path(settings.external_dir) / "corporate_actions.csv"
    if not src.exists():
        return []
    out = []
    with open(src, "r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            kind = str(r.get("type") or "").strip().upper()
            if kind not in ("SPLIT", "BONUS"):
                continue
            sym = str(r.get("symbol") or "").strip().upper()
            ratio = str(r.get("ratio") or "").strip()
            ex = to_date(str(r.get("ex_date")).strip()[:10])
            prev = calendar.shift(ex, -1)
            row = {"symbol": sym, "ex_date": iso(ex), "type": kind, "ratio": ratio, "factor": _factor(kind, ratio),
                   "prev_day": iso(prev), "prev_close": None, "ex_open": None, "jump": None, "verdict": "NO_DATA"}
            before, on = _candles(settings, sym, prev), _candles(settings, sym, ex)
            if row["factor"] and len(before) and len(on):
                pc, eo = float(before["c"].iloc[-1]), float(on["o"].iloc[0])
                jump = pc / eo if eo > 0 else None
                row.update(prev_close=pc, ex_open=eo, jump=round(jump, 4) if jump else None)
                if jump and abs(jump / row["factor"] - 1.0) <= UNADJ_TOL:
                    row["verdict"] = "UNADJUSTED"
                elif jump and abs(jump - 1.0) <= ADJ_TOL:
                    row["verdict"] = "ADJUSTED"
                else:
                    row["verdict"] = "UNCLEAR"
            out.append(row)
    return out


def overall(rows: list) -> str:
    seen = {r["verdict"] for r in rows}
    if "UNADJUSTED" in seen and "ADJUSTED" not in seen:
        return "UNADJUSTED"
    if "ADJUSTED" in seen and "UNADJUSTED" not in seen:
        return "ADJUSTED"
    if "ADJUSTED" in seen and "UNADJUSTED" in seen:
        return "MIXED"
    return "UNVERIFIED"


def run_adjusted_status(settings: Settings, calendar: Calendar, say=print) -> dict:
    rows = verdict_rows(settings, calendar)
    if not rows:
        say("adjusted_status: UNVERIFIED (external/corporate_actions.csv mein SPLIT/BONUS ki line nahi)")
        return {"verdict": "UNVERIFIED", "rows": 0}
    path = Path(settings.reports_dir) / "adjusted_status.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    verdict = overall(rows)
    n_data = sum(r["verdict"] != "NO_DATA" for r in rows)
    say(f"adjusted_status: {verdict} ({n_data} of {len(rows)} split/bonus samples have candles; {path})")
    return {"verdict": verdict, "rows": len(rows), "path": str(path)}
