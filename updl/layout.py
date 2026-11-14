"""Per-underlying export layout: plain 1-minute files that a phone, pandas or an AI can read directly.

For a window [start, end] this writes, next to (not instead of) the Parquet store:

    <INDEX>/spot_1m.csv              time, open, high, low, close            (index volume is always 0: left out)
    <INDEX>/futures_1m.csv           time, expiry, instrument_key, open, high, low, close, volume, oi
    <INDEX>/contracts.csv            one row per option contract seen in the window
    <INDEX>/options_1m/<expiry>.csv  LONG format: one row = one minute of one contract
                                     (time, strike, option_type, open, high, low, close, volume, oi, instrument_key)
    ai_ready/<INDEX>_options_1m.parquet          the same long table, development days only
    ai_ready/holdout/<INDEX>_options_1m.parquet  holdout days, kept apart: do not use while building a strategy
    raw_index.csv                    raw/<item>/<hash>/ folder -> item, index, instrument_key, dates

Rules: UTF-8, header row, IST ISO times (`2026-11-12T09:15:00+05:30`), an empty cell = no data (never 0), no file
per contract, a CSV that would pass 40 MB continues in `<name>_from_<YYYY-MM-DD>.csv`, every path part under 40
characters. Data is read day by day and appended, so memory stays small on a phone.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Settings
from .state import COVERING, Catalog, State
from .store import ParquetAppender, list_parts, part_path, partition_of, read_parquet
from .timeutil import IST, iso, to_date

MAX_CSV_BYTES = 40 * 1000 * 1000
OPTION_TABLES = ("opt_1m", "opt_wide_1m")
OPTION_COLUMNS = ["time", "strike", "option_type", "open", "high", "low", "close", "volume", "oi", "instrument_key"]
FUTURE_COLUMNS = ["time", "expiry", "instrument_key", "open", "high", "low", "close", "volume", "oi"]
SPOT_COLUMNS = ["time", "open", "high", "low", "close"]
CONTRACT_COLUMNS = ["instrument_key", "expiry", "strike", "option_type", "lot_size", "first_day", "last_day",
                    "minutes_with_data"]

README_LAYOUT = """UPDL BATCH FOLDER - kya kahan hai (what is where)
================================================

Har index ka alag folder: NIFTY/, BANKNIFTY/, SENSEX/
  spot_1m.csv             index ka 1-minute candle: time, open, high, low, close
  futures_1m.csv          index futures 1-minute: time, expiry, instrument_key, OHLC, volume, oi
  contracts.csv           is window ke saare option contracts: expiry, strike, CE/PE, lot_size, pehla/aakhri din
  options_1m/<expiry>.csv har option ka har minute (LONG format: ek line = ek contract ka ek minute):
                          time, strike, option_type (CE/PE), open, high, low, close, volume, oi, instrument_key
                          40 MB se badi file `<expiry>_from_<date>.csv` mein aage chalti hai.
ai_ready/                 wahi options table Parquet mein (pandas: pd.read_parquet). holdout/ folder ALAG hai:
                          strategy banate waqt use mat kholo; sirf aakhri ek test ke liye.
chain_15min/              har 15 minute ki option chain (spot, CE/PE close, OI, PCR, OI walls, max pain)
xcheck_csv/               exchange ka PCR / max pain / OI / FII-DII (Upstox se; din D ka data agle din se use karo)
store/                    poora Parquet store (sab tables, derived IV/greeks bhi)
health.csv, day_status.csv, holes.csv, strategy_ready_days.csv   kaunsa din poora hai, kya missing hai
README_FOR_AI.md          AI ko strategy banwane se pehle yeh file do
raw_index.csv             raw/ folder ke hash-naam -> index, item, instrument_key

Rules
  * time = IST (+05:30), candle ka START minute. 09:15 wali candle 09:15:00-09:15:59 ki hai.
  * Khali cell = data nahi tha. Kabhi 0 se nahi bhara.
  * volume aur oi = units (quantity), lots NAHI. Lots = units / lot_size (contracts.csv).
  * Upstox par TICK data nahi hai: 1 minute sabse chhoti candle hai. Usse chhota (aur bid/ask) sirf
    Recorder (bash termux_run.sh record) aage ke liye save karta hai.
  * Option ka minute tabhi hota hai jab us minute trade hua; patle strikes par minutes chhoot-te hain (normal).
"""


def ist_text(ts: pd.Series) -> pd.Series:
    """Aware timestamps -> '2026-11-12T09:15:00+05:30' (India has no daylight saving: the offset is fixed)."""
    t = pd.to_datetime(ts)
    if t.dt.tz is None:
        t = t.dt.tz_localize(IST)
    return t.dt.tz_convert(IST).dt.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


class SplitCsv:
    """Append CSV text to <stem>.csv; when a write would pass MAX_CSV_BYTES, continue in <stem>_from_<day>.csv."""

    def __init__(self, folder: Path, stem: str, columns: list, max_bytes: int = MAX_CSV_BYTES):
        self.folder, self.stem, self.columns, self.max_bytes = Path(folder), stem, columns, max_bytes
        self.path: Optional[Path] = None
        self.files, self.rows = [], 0

    def append(self, df: pd.DataFrame, day: dt.date) -> None:
        if df.empty:
            return
        buf = io.StringIO()
        df.to_csv(buf, index=False, header=False, columns=self.columns, lineterminator="\n")
        body = buf.getvalue().encode("utf-8")
        header = (",".join(self.columns) + "\n").encode("utf-8")
        if self.path is None or self.path.stat().st_size + len(body) > self.max_bytes:
            name = f"{self.stem}.csv" if self.path is None else f"{self.stem}_from_{iso(day)}.csv"
            self.path = self.folder / name
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(header)
            self.files.append(self.path)
        with open(self.path, "ab") as fh:
            fh.write(body)
        self.rows += len(df)


def _day_parts(settings: Settings, table: str, und: str, start: dt.date, end: dt.date) -> list:
    days = set()
    for part in ("dev", "holdout"):
        for p in list_parts(settings.store_dir, part, table, und):
            d = to_date(p.parent.name.split("=", 1)[1])
            if start <= d <= end:
                days.add(d)
    return sorted(days)


def _read(settings: Settings, table: str, und: str, d: dt.date, columns=None) -> pd.DataFrame:
    p = part_path(settings.store_dir, partition_of(d, settings.holdout_start), table, und, d)
    return read_parquet(p, columns=columns) if p.exists() else pd.DataFrame()


def _options_day(settings: Settings, und: str, d: dt.date) -> pd.DataFrame:
    cols = ["instrument_key", "expiry_date", "strike", "option_type", "ts_open", "o", "h", "l", "c", "v", "oi"]
    frames = [f for f in (_read(settings, t, und, d, cols) for t in OPTION_TABLES) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    df = df[df["option_type"].isin(["CE", "PE"])]
    df = df.drop_duplicates(["instrument_key", "ts_open"], keep="last")
    return df.sort_values(["expiry_date", "strike", "option_type", "ts_open"], kind="stable").reset_index(drop=True)


def _long(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"time": ist_text(df["ts_open"]), "strike": df["strike"].astype(float),
                         "option_type": df["option_type"], "open": df["o"], "high": df["h"], "low": df["l"],
                         "close": df["c"], "volume": df["v"].astype("Int64"), "oi": df["oi"].astype("Int64"),
                         "instrument_key": df["instrument_key"]})


def _ai_frame(df: pd.DataFrame, und: str) -> pd.DataFrame:
    return pd.DataFrame({"time": pd.to_datetime(df["ts_open"]).dt.tz_convert(IST).dt.as_unit("us"),
                         "trading_date": pd.to_datetime(df["ts_open"]).dt.tz_convert(IST).dt.date,
                         "underlying": und, "expiry": [to_date(x) for x in df["expiry_date"]],
                         "strike": df["strike"].astype(float), "option_type": df["option_type"].astype(str),
                         "open": df["o"].astype(float), "high": df["h"].astype(float), "low": df["l"].astype(float),
                         "close": df["c"].astype(float), "volume": df["v"].astype("Int64"),
                         "oi": df["oi"].astype("Int64"), "instrument_key": df["instrument_key"].astype(str)})


def _lot_sizes(settings: Settings) -> dict:
    if not (settings.state_dir / "catalog.sqlite3").exists():
        return {}
    cat = Catalog(settings.state_dir)
    try:
        return {k: v for k, v in cat.db.execute("SELECT instrument_key, lot_size FROM contracts") if v}
    finally:
        cat.close()


def write_raw_index(settings: Settings, start: dt.date, end: dt.date, out_dir: Path) -> int:
    """raw/<item>/<hash>/ folder names are hashes; this maps them back to item, index and instrument key."""
    if not (settings.state_dir / "progress.sqlite3").exists():
        return 0
    state = State(settings.state_dir)
    try:
        rows = {}
        for c in state.chunks(status=list(COVERING)):
            if not c["raw_path"] or c["to_date"] < iso(start) or c["from_date"] > iso(end):
                continue
            folder = Path(c["raw_path"]).parent
            key = (folder.parent.name, folder.name)
            r = rows.setdefault(key, {"raw_folder": f"raw/{folder.parent.name}/{folder.name}", "item_id": c["item_id"],
                                      "underlying": c["underlying"], "instrument_key": c["instrument_key"],
                                      "first_day": c["from_date"], "last_day": c["to_date"], "chunks": 0})
            r["first_day"], r["last_day"] = min(r["first_day"], c["from_date"]), max(r["last_day"], c["to_date"])
            r["chunks"] += 1
    finally:
        state.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "raw_index.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["raw_folder", "item_id", "underlying", "instrument_key", "first_day",
                                           "last_day", "chunks"])
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["underlying"], r["item_id"], r["instrument_key"])))
    return len(rows)


def write_layout(settings: Settings, start, end, out_dir, tick=None) -> dict:
    """Write the per-underlying layout for [start, end] into out_dir. Returns counts per index."""
    start, end, out_dir = to_date(start), to_date(end), Path(out_dir)
    lots = _lot_sizes(settings)
    summary = {}
    for und in settings.instrument_names():
        udir = out_dir / und
        counts = {"spot_rows": 0, "future_rows": 0, "option_rows": 0, "contracts": 0, "csv_files": 0}

        spot = SplitCsv(udir, "spot_1m", SPOT_COLUMNS)
        for d in _day_parts(settings, "spot_1m", und, start, end):
            df = _read(settings, "spot_1m", und, d, ["ts_open", "o", "h", "l", "c"])
            spot.append(pd.DataFrame({"time": ist_text(df["ts_open"]), "open": df["o"], "high": df["h"],
                                      "low": df["l"], "close": df["c"]}), d)
        fut = SplitCsv(udir, "futures_1m", FUTURE_COLUMNS)
        for d in _day_parts(settings, "fut_1m", und, start, end):
            df = _read(settings, "fut_1m", und, d)
            if df.empty:
                continue
            df = df.sort_values(["expiry_date", "ts_open"], kind="stable")
            fut.append(pd.DataFrame({"time": ist_text(df["ts_open"]), "expiry": [iso(x) for x in df["expiry_date"]],
                                     "instrument_key": df["instrument_key"], "open": df["o"], "high": df["h"],
                                     "low": df["l"], "close": df["c"], "volume": df["v"].astype("Int64"),
                                     "oi": df["oi"].astype("Int64")}), d)

        writers, contracts, ai = {}, {}, {}
        try:
            for d in sorted(set(_day_parts(settings, "opt_1m", und, start, end))
                            | set(_day_parts(settings, "opt_wide_1m", und, start, end))):
                if tick:
                    tick(f"{und} {iso(d)}")
                df = _options_day(settings, und, d)
                if df.empty:
                    continue
                part = partition_of(d, settings.holdout_start)
                if part not in ai:
                    folder = out_dir / "ai_ready" / ("holdout" if part == "holdout" else "")
                    ai[part] = ParquetAppender(folder / f"{und}_options_1m.parquet")
                ai[part].append(_ai_frame(df, und))
                for exp, g in df.groupby("expiry_date", sort=True):
                    e = iso(exp)
                    if e not in writers:
                        writers[e] = SplitCsv(udir / "options_1m", e, OPTION_COLUMNS)
                    writers[e].append(_long(g), d)
                stats = df.groupby("instrument_key").agg(expiry=("expiry_date", "first"), strike=("strike", "first"),
                                                         option_type=("option_type", "first"),
                                                         n=("ts_open", "size"))
                for key, r in stats.iterrows():
                    c = contracts.setdefault(key, {"instrument_key": key, "expiry": iso(r["expiry"]),
                                                   "strike": float(r["strike"]), "option_type": r["option_type"],
                                                   "lot_size": lots.get(key, ""), "first_day": iso(d),
                                                   "last_day": iso(d), "minutes_with_data": 0})
                    c["last_day"] = iso(d)
                    c["minutes_with_data"] += int(r["n"])
        finally:
            for w in ai.values():
                w.close()
        if contracts:
            udir.mkdir(parents=True, exist_ok=True)
            with open(udir / "contracts.csv", "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=CONTRACT_COLUMNS)
                w.writeheader()
                w.writerows(sorted(contracts.values(), key=lambda r: (r["expiry"], r["strike"], r["option_type"])))
        counts.update(spot_rows=spot.rows, future_rows=fut.rows,
                      option_rows=sum(w.rows for w in writers.values()), contracts=len(contracts),
                      csv_files=len(spot.files) + len(fut.files) + sum(len(w.files) for w in writers.values())
                      + (1 if contracts else 0))
        summary[und] = counts
    write_raw_index(settings, start, end, out_dir)
    (out_dir / "README_EXPORT.txt").write_text(README_LAYOUT, encoding="utf-8")
    return summary


README_FOR_AI = """# README_FOR_AI: read this before building or testing a strategy on this data

Data: Indian index options (NIFTY, BANKNIFTY, SENSEX) from the Upstox API, downloaded by `updl`.
Window of this folder: {start} to {end}. Holdout starts {holdout} (days on or after it live in `store/holdout/`
and `ai_ready/holdout/`).

## Files (see README_EXPORT.txt for the full list)
- `<INDEX>/options_1m/<expiry>.csv` and `ai_ready/<INDEX>_options_1m.parquet`: one row = one minute of one option
  contract. Columns: time (IST, candle START), strike, option_type (CE/PE), open, high, low, close, volume, oi,
  instrument_key (+ trading_date, underlying, expiry in Parquet).
- `<INDEX>/spot_1m.csv` (index, no volume), `<INDEX>/futures_1m.csv`, `<INDEX>/contracts.csv` (lot_size).
- `store/` all tables, incl. derived `iv_greeks_1m` and `chain_metrics_1m` (PCR, OI walls, max pain). Every
  derived row has `available_from_ts`: never use a value before that time.
- `xcheck_csv/`: exchange-side PCR / max pain / OI / FII-DII; values of day D are usable only from the next session.
- `strategy_ready_days.csv`: which index-days have complete price data (PRICE_READY) and also events and
  recorded bid/ask (FULL_READY). Test only on READY days, or say clearly how you treat the others.

## Units and timing
- volume and oi are in UNITS (quantity), not lots: lots = units / lot_size.
- Times are IST (+05:30). A candle stamped 09:15 covers 09:15:00-09:15:59 and is known at 09:16.
- Index F&O trades until 15:40 from 3 Aug 2026 (15:30 before); the index itself stops at 15:30.
- An option minute exists only if that contract traded in that minute: missing minutes on thin strikes are normal.
  Never fill them silently; if you carry a price forward, mark it.
- Expiry-day contracts and weekly expiry rules changed over time (lot sizes per contract are in contracts.csv).

## Known limits (do not pretend otherwise)
- No tick data and no historical bid/ask: 1 minute is the smallest candle. Fills at the candle close are
  optimistic. Use the full charges (brokerage, STT, exchange, GST, stamp) AND a pessimistic slippage, larger for
  cheap out-of-the-money options and on expiry day. Real spreads exist only for days the Recorder ran
  (`recorder/` files, SPREAD = READY).
- IV / greeks are computed from the last traded price (Black-76): noisy on thin strikes and stale prices.
- Index constituents, event calendar and corporate actions are owner files; when missing, checks that need them
  are not testable.

## How to test honestly
1. Build and tune ONLY on development days. Do not open the holdout while building.
2. Use walk-forward testing (tune on a past block, test on the next block, roll forward).
3. Prefer few parameters; report the number of trades, the worst drawdown and results per month.
4. Run the holdout ONCE at the very end. If it fails, do not re-tune on it.
5. Paper-trade live before any real money. This data and any result from it is not financial advice.
"""


def write_readme_for_ai(settings: Settings, start, end, out_dir) -> Path:
    path = Path(out_dir) / "README_FOR_AI.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(README_FOR_AI.format(start=iso(start), end=iso(end), holdout=iso(settings.holdout_start)),
                    encoding="utf-8")
    return path
