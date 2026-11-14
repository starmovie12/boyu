"""Raw files, candle parsing, normalisation, Parquet compaction and table reading.

All Parquet reading and writing goes through write_parquet / read_parquet / read_parquet_metadata so that
there is exactly one place that touches the Parquet library."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .errors import CorruptFile
from .state import DONE, State
from .timeutil import IST, parse_ts, to_date

COLUMNS = ["instrument_id", "instrument_key", "underlying", "segment", "expiry_date", "strike", "option_type",
           "interval", "ts_open", "ts_close", "o", "h", "l", "c", "v", "oi", "has_volume", "is_stale", "is_filled",
           "session_type", "trading_date", "source", "adjusted_status", "fetch_run_id", "raw_sha256"]
ITEM_TABLE = {"D01": "spot_1m", "D02": "spot_1d", "D03": "vix_1m", "D04": "vix_1d", "D05": "fut_1m",
              "D06": "opt_1m", "D08": "opt_wide_1m", "D09": "stock_1m", "D10": "stock_1m", "D11": "global_1m"}
NO_VOLUME_TABLES = {"spot_1m", "spot_1d", "vix_1m", "vix_1d", "global_1m"}
TABLE_METADATA = {"volume_unit": "UNVERIFIED", "adjusted_status": "UNVERIFIED", "timezone": "Asia/Kolkata",
                  "look_ahead_rule": "uses only bars with ts_close <= decision time", "schema_version": "2"}
SOURCE_V3, SOURCE_EXPIRED = "upstox_v3", "upstox_expired_v2"
FETCHED_COL = "_fetched_utc"   # compaction-only helper column, never written


# ---- generic file helpers --------------------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write_bytes(path, data: bytes, overwrite: bool = False):
    """Temp file in the same folder, fsync, os.replace. A second write to an existing name raises."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite {path.name}")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def cleanup_temp_files(root) -> int:
    n = 0
    if not Path(root).exists():
        return 0
    for p in Path(root).rglob("*.tmp"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def instrument_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def raw_file_path(raw_dir, item_id, key, frm, to, run_id) -> Path:
    return Path(raw_dir) / item_id / instrument_hash(key) / f"{to_date(frm)}_{to_date(to)}.{run_id}.json.gz"


def meta_path_of(raw_path) -> Path:
    p = Path(raw_path)
    return p.with_name(p.name[: -len(".json.gz")] + ".meta.json")


def write_raw(raw_dir, item_id, key, frm, to, run_id, body: bytes, meta: dict) -> dict:
    """Write the gzip'd response and its sidecar. Raw file first, sidecar second."""
    path = raw_file_path(raw_dir, item_id, key, frm, to, run_id)
    gz = gzip.compress(body, mtime=0)
    atomic_write_bytes(path, gz)
    info = dict(meta)
    info.update({"sha256": sha256_bytes(gz), "bytes": len(gz), "body_sha256": sha256_bytes(body)})
    atomic_write_bytes(meta_path_of(path), json.dumps(info, sort_keys=True, default=str).encode("utf-8"))
    return {"path": str(path), "sha256": info["sha256"], "bytes": len(gz)}


def read_raw_payload(path) -> dict:
    return json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))


def verify_raw_integrity(state: State, since_utc=None) -> list:
    """Return the DONE raw files whose sha256 does not match the recorded value (or that are missing).
    since_utc limits the check to chunks fetched since then (week mode: hashing the whole history on every
    batch would take minutes on a phone)."""
    bad = []
    for c in state.chunks(status=[DONE], since_utc=since_utc):
        p = c["raw_path"]
        if not p or c.get("error") == State.RAW_DELETED:
            continue
        if not Path(p).exists() or sha256_file(p) != c["sha256"]:
            bad.append(p)
    return bad


# ---- candle parsing ---------------------------------------------------------------------------------------------
def parse_candles(payload: dict):
    """Accept rows with 6 or 7 elements, tolerate null volume (kept null, never 0), sort ascending by ts_open.
    Returns (rows, bad_row_count). Each row: dict(ts, ts_open, o,h,l,c, v, oi)."""
    raw = (payload.get("data") or {}).get("candles") or []
    rows, bad = [], 0
    for r in raw:
        if not isinstance(r, (list, tuple)) or len(r) < 5 or not isinstance(r[0], str):
            bad += 1
            continue
        try:
            ts_open = parse_ts(r[0])
            o, h, l, c = (None if x is None else float(x) for x in r[1:5])
        except (ValueError, TypeError):
            bad += 1
            continue
        if None in (o, h, l, c):
            bad += 1
            continue
        v = r[5] if len(r) > 5 else None
        oi = r[6] if len(r) > 6 else None
        rows.append({"ts": r[0], "ts_open": ts_open, "o": o, "h": h, "l": l, "c": c,
                     "v": None if v is None else int(v), "oi": None if oi is None else int(oi)})
    rows.sort(key=lambda x: x["ts_open"])
    return rows, bad


def make_instrument_id(meta: dict) -> str:
    und, kind = meta.get("underlying"), meta.get("option_type") or "NA"
    if kind in ("CE", "PE"):
        return f"{und}|{meta.get('expiry_date')}|{float(meta.get('strike')):g}|{kind}"
    if kind == "FUT":
        return f"{und}|{meta.get('expiry_date')}|FUT"
    return f"{und}|{kind}"


def _session_tag(calendar, d) -> str:
    st = calendar.session_type(d)
    return "UNEXPECTED" if st in ("WEEKEND", "HOLIDAY") else st


def rows_to_frame(rows: list, meta: dict, calendar, run_id: str, raw_sha: str) -> pd.DataFrame:
    table = meta["table"]
    has_volume = table not in NO_VOLUME_TABLES
    daily = meta.get("interval") == "1d"
    iid = make_instrument_id(meta)
    n = len(rows)
    ts_open = pd.DatetimeIndex([r["ts_open"] for r in rows]).tz_convert(IST) if n else pd.DatetimeIndex([], tz=IST)
    if daily:
        ts_close = pd.DatetimeIndex([dt.datetime(r["ts_open"].year, r["ts_open"].month, r["ts_open"].day, 23, 59, 59,
                                                 999000, tzinfo=IST) for r in rows]) if n else ts_open
    else:
        ts_close = ts_open + pd.Timedelta(minutes=1)
    dates = [r["ts_open"].date() for r in rows]
    v = pd.array([r["v"] for r in rows], dtype="Int64")
    oi = pd.array([r["oi"] for r in rows], dtype="Int64")
    stale = (v == 0).to_numpy(dtype=bool, na_value=False) if has_volume else [False] * n
    expiry = to_date(meta["expiry_date"]) if meta.get("expiry_date") else None
    strike = meta.get("strike")
    df = pd.DataFrame({
        "instrument_id": [iid] * n, "instrument_key": [meta["instrument_key"]] * n,
        "underlying": [meta["underlying"]] * n,
        "segment": [str(meta["instrument_key"]).split("|")[0]] * n,
        "expiry_date": [expiry] * n, "strike": [float(strike) if strike is not None else float("nan")] * n,
        "option_type": [meta.get("option_type")] * n, "interval": [meta.get("interval", "1m")] * n,
        "ts_open": ts_open, "ts_close": ts_close,
        "o": [r["o"] for r in rows], "h": [r["h"] for r in rows], "l": [r["l"] for r in rows],
        "c": [r["c"] for r in rows], "v": v, "oi": oi,
        "has_volume": [has_volume] * n, "is_stale": list(stale), "is_filled": [False] * n,
        "session_type": [_session_tag(calendar, d) for d in dates], "trading_date": dates,
        "source": [meta.get("source", SOURCE_V3)] * n, "adjusted_status": ["UNVERIFIED"] * n,
        "fetch_run_id": [run_id] * n, "raw_sha256": [raw_sha] * n})
    return df[COLUMNS]


def candles_frame(payload: dict, meta: dict, calendar, run_id: str, raw_sha: str, d0=None, d1=None):
    """parse_candles + rows_to_frame on whole columns (v4): same rows, same columns, far fewer Python
    objects. Returns (frame, bad_row_count); rows outside [d0, d1] are dropped. Any surprise in the payload
    (for example mixed UTC offsets) falls back to the row-by-row reference path."""
    raw = (payload.get("data") or {}).get("candles") or []
    try:
        good = [r for r in raw if isinstance(r, (list, tuple)) and len(r) >= 5 and isinstance(r[0], str)]
        if not good:
            return pd.DataFrame(columns=COLUMNS), len(raw)
        arr = pd.DataFrame(good)
        ts = pd.to_datetime(arr[0], format="ISO8601")
        if ts.dt.tz is None:
            raise ValueError("timestamp has no offset")
        ohlc = [pd.to_numeric(arr[i], errors="raise").astype(float) for i in range(1, 5)]
        keep = ~(ohlc[0].isna() | ohlc[1].isna() | ohlc[2].isna() | ohlc[3].isna())
        ts_open = ts.dt.tz_convert(IST).dt.as_unit("us")
        days = ts_open.dt.date
        if d0 is not None:
            keep &= (days >= to_date(d0)) & (days <= to_date(d1))
        nbad = len(raw) - len(good) + int((~(ohlc[0].notna() & ohlc[1].notna() & ohlc[2].notna() & ohlc[3].notna())).sum())
        seq = ts_open[keep].sort_values(kind="stable").index
        n = len(seq)
        if n == 0:
            return pd.DataFrame(columns=COLUMNS), nbad
        ts_open, days = ts_open.loc[seq].reset_index(drop=True), days.loc[seq].reset_index(drop=True)

        def ints(col):
            if col not in arr.columns:
                return pd.array([pd.NA] * n, dtype="Int64")
            vals = pd.to_numeric(arr[col].loc[seq], errors="raise").astype(float).to_numpy()
            return pd.array(np.where(np.isnan(vals), np.nan, np.trunc(vals)), dtype="Float64").astype("Int64")

        table = meta["table"]
        has_volume = table not in NO_VOLUME_TABLES
        v, oi = ints(5), ints(6)
        if meta.get("interval") == "1d":
            ts_close = pd.Series(pd.DatetimeIndex([dt.datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=IST)
                                                   for d in days]))
        else:
            ts_close = ts_open + pd.Timedelta(minutes=1)
        tags = {d: _session_tag(calendar, d) for d in set(days)}
        strike = meta.get("strike")
        df = pd.DataFrame({
            "instrument_id": make_instrument_id(meta), "instrument_key": meta["instrument_key"],
            "underlying": meta["underlying"], "segment": str(meta["instrument_key"]).split("|")[0],
            "expiry_date": to_date(meta["expiry_date"]) if meta.get("expiry_date") else None,
            "strike": float(strike) if strike is not None else float("nan"),
            "option_type": meta.get("option_type"), "interval": meta.get("interval", "1m"),
            "ts_open": ts_open, "ts_close": ts_close,
            "o": ohlc[0].loc[seq].to_numpy(), "h": ohlc[1].loc[seq].to_numpy(),
            "l": ohlc[2].loc[seq].to_numpy(), "c": ohlc[3].loc[seq].to_numpy(), "v": v, "oi": oi,
            "has_volume": has_volume,
            "is_stale": (v == 0).to_numpy(dtype=bool, na_value=False) if has_volume else False,
            "is_filled": False, "session_type": [tags[d] for d in days], "trading_date": days,
            "source": meta.get("source", SOURCE_V3), "adjusted_status": "UNVERIFIED",
            "fetch_run_id": run_id, "raw_sha256": raw_sha}, index=range(n))
        return df[COLUMNS], nbad
    except Exception:
        rows, nbad = parse_candles(payload)
        if d0 is not None:
            rows = [r for r in rows if to_date(d0) <= r["ts_open"].date() <= to_date(d1)]
        if not rows:
            return pd.DataFrame(columns=COLUMNS), nbad
        return rows_to_frame(rows, meta, calendar, run_id, raw_sha), nbad


# ---- parquet wrappers (the only place that touches the Parquet library) -------------------------------------------
def write_parquet(df: pd.DataFrame, path, metadata: Optional[dict] = None):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    meta = dict(table.schema.metadata or {})
    for k, v in (metadata or {}).items():
        meta[str(k).encode()] = str(v).encode()
    table = table.replace_schema_metadata(meta)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    if path.exists():
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
    os.replace(tmp, path)


def read_parquet(path, columns=None) -> pd.DataFrame:
    return pd.read_parquet(path, engine="pyarrow", columns=columns)


def read_parquet_metadata(path) -> dict:
    import pyarrow.parquet as pq

    meta = pq.read_schema(path).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items() if not k.startswith(b"pandas")}


# ---- store layout -------------------------------------------------------------------------------------------------
def partition_of(d, holdout_start) -> str:
    return "holdout" if to_date(d) >= to_date(holdout_start) else "dev"


def part_path(store_dir, partition, table, underlying, d) -> Path:
    return Path(store_dir) / partition / table / f"underlying={underlying}" / f"trading_date={to_date(d)}" / "part-0.parquet"


def list_parts(store_dir, partition, table=None, underlying=None) -> list:
    base = Path(store_dir) / partition
    if not base.exists():
        return []
    pat_t = table or "*"
    pat_u = f"underlying={underlying}" if underlying else "underlying=*"
    return sorted(base.glob(f"{pat_t}/{pat_u}/trading_date=*/part-0.parquet"))


def date_of_part(path: Path) -> dt.date:
    return dt.date.fromisoformat(path.parent.name.split("=", 1)[1])


def underlying_of_part(path: Path) -> str:
    return path.parent.parent.name.split("=", 1)[1]


def read_table(store_dir, table, underlying=None, start=None, end=None, partitions=("dev", "holdout"),
               columns=None) -> pd.DataFrame:
    """Read store rows. Low-level (no holdout guard): derive and the loader decide which partitions to pass."""
    frames = []
    for part in partitions:
        for p in list_parts(store_dir, part, table, underlying):
            d = date_of_part(p)
            if start and d < to_date(start):
                continue
            if end and d > to_date(end):
                continue
            frames.append(read_parquet(p, columns=columns))
    if not frames:
        return pd.DataFrame(columns=columns or COLUMNS)
    return pd.concat(frames, ignore_index=True)


# ---- compaction -----------------------------------------------------------------------------------------------------
def _window_batches(lo: dt.date, hi: dt.date, step_days: int = 10):
    cur = lo
    while cur <= hi:
        end = min(hi, cur + dt.timedelta(days=step_days - 1))
        yield cur, end
        cur = end + dt.timedelta(days=1)


def compact(settings, calendar, log=None, since_utc=None, tick=None) -> dict:
    """Merge DONE raw chunks into one Parquet file per (table, underlying, trading_date).
    Within one fetch run a repeated (instrument_key, ts_open) is reported and removed; across runs the
    latest fetch run wins. Re-running compact rebuilds the same result.
    since_utc (week mode): only chunks fetched since then are read and merged into the existing files."""
    log = log or (lambda *a, **k: None)
    state = State(settings.state_dir)
    try:
        bad = verify_raw_integrity(state, since_utc)
        if bad:
            raise CorruptFile("sha256 mismatch: " + "; ".join(bad[:5]))
        groups = defaultdict(list)
        for c in state.chunks(status=[DONE], since_utc=since_utc):
            if c["raw_path"] and c["table_name"] and c.get("error") != State.RAW_DELETED:
                groups[(c["table_name"], c["underlying"])].append(c)
        dupes_path = Path(settings.store_dir) / "_compaction_dupes.json"
        dupes = json.loads(dupes_path.read_text()) if dupes_path.exists() else {}
        written = 0
        for (table, und), chunks in sorted(groups.items()):
            lo = min(dt.date.fromisoformat(c["from_date"]) for c in chunks)
            hi = max(dt.date.fromisoformat(c["to_date"]) for c in chunks)
            for w0, w1 in _window_batches(lo, hi):
                if tick:
                    tick(f"{table} {und}")
                frames = []
                for c in chunks:
                    f, t = dt.date.fromisoformat(c["from_date"]), dt.date.fromisoformat(c["to_date"])
                    if t < w0 or f > w1:
                        continue
                    meta = json.loads(meta_path_of(c["raw_path"]).read_text(encoding="utf-8"))
                    frame, nbad = candles_frame(read_raw_payload(c["raw_path"]), meta, calendar,
                                                c["fetch_run_id"] or "r0", c["sha256"], w0, w1)
                    if nbad:
                        log("bad_rows", count=nbad, raw=c["raw_path"])
                    if len(frame):
                        # v3: "latest fetch wins" is decided by the fetch time stored in the state (ISO UTC,
                        # sortable). fetch_run_id is only rHHMMSS, so a next-morning re-fetch (r080000) sorted
                        # BEFORE the previous evening's run (r220000) and the older data won.
                        frames.append(frame.assign(**{FETCHED_COL: str(c.get("fetched_utc") or "")}))
                if not frames:
                    continue
                df = pd.concat(frames, ignore_index=True)
                df = df.sort_values(["instrument_key", "ts_open", FETCHED_COL, "fetch_run_id"], kind="stable")
                same_run = df.duplicated(["instrument_key", "ts_open", "fetch_run_id"], keep="first")
                for d_, n_ in df[same_run].groupby("trading_date").size().items():
                    dupes[f"{table}|{und}|{d_}"] = int(n_)
                    log("duplicate_bar_removed", table=table, underlying=und, trading_date=str(d_), count=int(n_))
                df = df[~same_run]
                df = df.drop_duplicates(["instrument_key", "ts_open"], keep="last")  # latest fetch wins
                for d_, part in df.groupby("trading_date"):
                    p = part_path(settings.store_dir, partition_of(d_, settings.holdout_start), table, und, d_)
                    if p.exists():  # keep rows from raw files that are gone (DATA_RIGHTS=MINIMAL); fresh rows win
                        old = read_parquet(p).assign(**{FETCHED_COL: ""})
                        part = pd.concat([old, part], ignore_index=True)
                        part = part.sort_values(FETCHED_COL, kind="stable").drop_duplicates(
                            ["instrument_key", "ts_open"], keep="last").sort_values(["instrument_key", "ts_open"])
                    part = part.drop(columns=[FETCHED_COL])
                    write_parquet(part.reset_index(drop=True), p, {**TABLE_METADATA, "table": table})
                    written += 1
        dupes_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dupes_path, json.dumps(dupes, sort_keys=True).encode("utf-8"), overwrite=True)
        deleted = 0
        if settings.data_rights == "MINIMAL":   # delete raw files after normalising
            for c in state.chunks(status=[DONE]):
                if c["raw_path"] and c.get("error") != State.RAW_DELETED and Path(c["raw_path"]).exists():
                    rp = Path(c["raw_path"])
                    for f in (rp, meta_path_of(rp)):
                        try:
                            f.unlink()
                        except OSError:
                            pass
                    state.mark_raw_deleted(c["raw_path"])
                    deleted += 1
        return {"files_written": written, "groups": len(groups), "raw_deleted": deleted}
    finally:
        state.close()
