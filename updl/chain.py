"""Option chain "as of time T", rebuilt from 1-minute option candles (no Upstox endpoint serves a past chain).

Rules (Rule 4 and Rule 8):
* A value is "as of T" only if its candle closed at or before T (ts_close <= T). Nothing from after T is used.
* Options do not trade every minute. When the latest candle of a contract closed BEFORE the minute ending at T,
  its close / OI are carried from that older candle and the row says so: `*_is_filled = True` and
  `*_age_min` = minutes since that candle closed. Nothing is interpolated; a contract with no candle yet that
  day has empty (null) values.
* Volume: `*_vol_1m` is the volume of the candle ending exactly at T (null if none); `*_vol_cum` sums the
  known volumes of the day up to T. Upstox reports volume and OI as quantity (units), not lots.
* Metrics (PCR, OI walls, max pain) only use strikes present in the downloaded band, so they describe the
  band, not the whole exchange chain.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .timeutil import IST

SIDES = ("CE", "PE")


def _to_ns(values) -> np.ndarray:
    """Timezone-aware timestamps -> int64 nanoseconds since epoch (UTC), for searchsorted."""
    idx = pd.DatetimeIndex(pd.to_datetime(values))
    if idx.tz is None:
        idx = idx.tz_localize(IST)
    # pandas 2/3 may store ms or us resolution; asi8 is in that unit, so pin nanoseconds first
    return idx.tz_convert("UTC").as_unit("ns").asi8


def _nullable_float(series: pd.Series) -> np.ndarray:
    return pd.to_numeric(series, errors="coerce").astype("float64").to_numpy(na_value=np.nan)


def asof_chain(odf: pd.DataFrame, times: Iterable) -> pd.DataFrame:
    """Snapshot rows (one per snapshot time, expiry and strike) from one underlying's 1-minute option candles.

    odf needs: instrument_key, expiry_date, strike, option_type (CE/PE), ts_close, c, v, oi.
    times: aware datetimes (decision times). Returns columns:
    snapshot_ts, expiry_date, strike, ce_close, ce_vol_1m, ce_vol_cum, ce_oi, ce_last_ts, ce_age_min,
    ce_is_filled, and the same for pe_.
    """
    times = list(times)
    cols = ["snapshot_ts", "expiry_date", "strike"]
    for sd in SIDES:
        p = sd.lower()
        cols += [f"{p}_close", f"{p}_vol_1m", f"{p}_vol_cum", f"{p}_oi", f"{p}_last_ts", f"{p}_age_min",
                 f"{p}_is_filled"]
    if odf is None or odf.empty or not times:
        return pd.DataFrame(columns=cols)

    t_ns = _to_ns(times)
    t_index = pd.DatetimeIndex(pd.to_datetime(times))
    if t_index.tz is None:
        t_index = t_index.tz_localize(IST)
    t_index = t_index.tz_convert(IST)

    df = odf[odf["option_type"].isin(SIDES)].copy()
    df = df[df["strike"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["_tc"] = _to_ns(df["ts_close"])
    df = df.sort_values(["instrument_key", "_tc"], kind="stable")

    records = {}
    one_min = 60 * 10**9
    for key, g in df.groupby("instrument_key", sort=False):
        side = str(g["option_type"].iloc[0]).lower()
        exp = g["expiry_date"].iloc[0]
        strike = float(g["strike"].iloc[0])
        tc = g["_tc"].to_numpy()
        close = _nullable_float(g["c"])
        oi = _nullable_float(g["oi"])
        vol = _nullable_float(g["v"])
        vol_cum = np.cumsum(np.where(np.isnan(vol), 0.0, vol))
        idx = np.searchsorted(tc, t_ns, side="right") - 1
        for j, i in enumerate(idx):
            rk = (j, exp, strike)
            rec = records.setdefault(rk, {})
            if i < 0:
                continue                      # no candle closed yet: stays null
            last = int(tc[i])
            age = (int(t_ns[j]) - last) / one_min
            rec[f"{side}_close"] = close[i]
            rec[f"{side}_oi"] = oi[i]
            rec[f"{side}_vol_cum"] = vol_cum[i]
            rec[f"{side}_vol_1m"] = vol[i] if age < 1.0 else np.nan
            rec[f"{side}_last_ts"] = pd.Timestamp(last, tz="UTC").tz_convert(IST)
            rec[f"{side}_age_min"] = round(age, 3)
            rec[f"{side}_is_filled"] = bool(age >= 1.0)

    rows = []
    for (j, exp, strike), rec in records.items():
        row = {"snapshot_ts": t_index[j], "expiry_date": exp, "strike": strike}
        for sd in SIDES:
            p = sd.lower()
            for name in ("close", "vol_1m", "vol_cum", "oi", "age_min"):
                row[f"{p}_{name}"] = rec.get(f"{p}_{name}", np.nan)
            row[f"{p}_last_ts"] = rec.get(f"{p}_last_ts", pd.NaT)
            row[f"{p}_is_filled"] = rec.get(f"{p}_is_filled", None)
        rows.append(row)
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values(["snapshot_ts", "expiry_date", "strike"], kind="stable").reset_index(drop=True)


def max_pain(strikes: np.ndarray, ce_oi: np.ndarray, pe_oi: np.ndarray) -> float:
    """Strike at which option buyers' total intrinsic value at expiry is smallest. Missing OI is left out."""
    k = np.asarray(strikes, dtype=float)
    ce = np.asarray(ce_oi, dtype=float)
    pe = np.asarray(pe_oi, dtype=float)
    ok_ce, ok_pe = ~np.isnan(ce), ~np.isnan(pe)
    if ok_ce.sum() < 2 or ok_pe.sum() < 2:
        return float("nan")
    best_k, best_pay = float("nan"), None
    for cand in np.unique(k):
        pay = float(np.sum(ce[ok_ce] * np.maximum(0.0, cand - k[ok_ce]))
                    + np.sum(pe[ok_pe] * np.maximum(0.0, k[ok_pe] - cand)))
        if best_pay is None or pay < best_pay:
            best_k, best_pay = float(cand), pay
    return best_k


def chain_metrics(snap: pd.DataFrame, spot: Optional[float] = None) -> dict:
    """PCR, OI walls and max pain for ONE (snapshot time, expiry) slice of asof_chain()."""
    k = snap["strike"].to_numpy(dtype=float)
    ce = pd.to_numeric(snap["ce_oi"], errors="coerce").to_numpy(dtype=float)
    pe = pd.to_numeric(snap["pe_oi"], errors="coerce").to_numpy(dtype=float)
    ce_sum, pe_sum = np.nansum(ce), np.nansum(pe)
    known_ce, known_pe = ~np.isnan(ce), ~np.isnan(pe)
    pcr = float(pe_sum / ce_sum) if known_ce.any() and known_pe.any() and ce_sum > 0 else float("nan")

    def wall(values, mask):
        m = mask & ~np.isnan(values)
        if not m.any():
            return float("nan")
        return float(k[m][int(np.argmax(values[m]))])

    have_spot = spot is not None and not (isinstance(spot, float) and np.isnan(spot))
    call_mask = (k >= spot) if have_spot else np.ones_like(k, dtype=bool)
    put_mask = (k <= spot) if have_spot else np.ones_like(k, dtype=bool)
    filled = pd.concat([snap["ce_is_filled"], snap["pe_is_filled"]]).fillna(False).astype(bool)
    return {
        "pcr": pcr,
        "call_oi_wall": wall(ce, call_mask),
        "put_oi_wall": wall(pe, put_mask),
        "max_pain": max_pain(k, ce, pe),
        "n_strikes": int(len(k)),
        "n_oi_known": int(known_ce.sum() + known_pe.sum()),
        "n_oi_carried": int(filled.sum()),
    }


def spot_asof(spot_df: pd.DataFrame, times: Iterable) -> list:
    """Spot close known at each time T (last 1-minute spot candle with ts_close <= T); NaN if none yet."""
    times = list(times)
    if spot_df is None or spot_df.empty or not times:
        return [float("nan")] * len(times)
    s = spot_df.assign(_tc=_to_ns(spot_df["ts_close"])).sort_values("_tc")
    tc = s["_tc"].to_numpy()
    c = _nullable_float(s["c"])
    idx = np.searchsorted(tc, _to_ns(times), side="right") - 1
    return [float(c[i]) if i >= 0 else float("nan") for i in idx]


def snapshot_times(day: dt.date, open_hhmm: str, close_hhmm: str, every_min: int) -> list:
    """Decision times for one day: open+every, open+2*every, ... and always the session close."""
    oh, om = (int(x) for x in open_hhmm.split(":"))
    ch, cm = (int(x) for x in close_hhmm.split(":"))
    start = dt.datetime(day.year, day.month, day.day, oh, om, tzinfo=IST)
    end = dt.datetime(day.year, day.month, day.day, ch, cm, tzinfo=IST)
    step = dt.timedelta(minutes=max(1, int(every_min)))
    out, t = [], start + step
    while t < end:
        out.append(t)
        t += step
    out.append(end)
    return out
