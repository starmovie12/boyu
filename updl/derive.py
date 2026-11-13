"""Stage 3 Derived Table Builder (`python -m updl derive`).

Enforces Section 9 and Rule 8 (NO LOOK-AHEAD IN DERIVED DATA):
* Every derived column ends with _DERIVED, every proxy column with _PROXY.
* Every row carries available_from_ts (IST): the first minute at which a live bot could know it.
* Dev and holdout trees are derived in chronological date sequence; dev days never read holdout rows.
* 9.1 bars_3m, bars_15m from 1m spot bars (buckets start at 09:15; missing minute -> is_partial=True).
* 9.2 chain_1m: reconstructed chain per minute with CE/PE close, volume, OI, is_stale.
* 9.3 iv_greeks_1m: Black-76 on forward F (PARITY_THEN_COST_OF_CARRY), bracketed root-finding,
  iv_DERIVED, delta_DERIVED, gamma_DERIVED, theta_DERIVED, iv_status, forward_source.
* 9.4 iv_daily_DERIVED (ATM IV at 10:00 and 14:00) and ivp_vix_PROXY (percentile over PREVIOUS 252 daily closes,
  never including current day D).
* 9.5 rv20_DERIVED, pcr_DERIVED, oi_walls_DERIVED, max_pain_DERIVED, gex_proxy_PROXY, gamma_flip_PROXY,
  skew_DERIVED, vix_term_DERIVED.
* 9.6 Volume baselines: volume_baseline_same_dte_DERIVED and volume_baseline_any_dte_DERIVED by moneyness bucket.
* 9.7 fill_assumptions_PROXY: modelled_ask_PROXY, half_spread_PROXY, fillable.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

from .chain import _to_ns, metrics_matrix, oi_matrices, spot_asof_age
from .config import Settings, load_yaml
from .ivfast import greeks_vec, solve_iv_vec
from .selector import strike_step
from .sessions import Calendar, load_calendar
from .store import (TABLE_METADATA, date_of_part, list_parts, part_path, partition_of,
                    read_parquet, write_parquet)
from .timeutil import IST, at_ist, iso, to_date


# ---- Black-76 pricing, IV solver & Greeks (Section 9.3) ---------------------------------------------------------
def black76_price(F: float, K: float, T_years: float, sigma: float, option_type: str) -> float:
    """Undiscounted Black-76 option price (matches iv_model.yaml: price = F*N(d1) - K*N(d2) so parity F = K+C-P is exact)."""
    if T_years <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return max(0.0, F - K) if option_type == "CE" else max(0.0, K - F)
    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if option_type == "CE":
        return F * float(norm.cdf(d1)) - K * float(norm.cdf(d2))
    return K * float(norm.cdf(-d2)) - F * float(norm.cdf(-d1))


def black76_greeks(F: float, K: float, T_years: float, sigma: float, option_type: str) -> tuple[float, float, float]:
    """Return (delta_DERIVED, gamma_DERIVED, theta_DERIVED per day)."""
    sqrt_T = math.sqrt(max(T_years, 1e-9))
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T_years) / (sigma * sqrt_T)
    pdf_d1 = float(norm.pdf(d1))
    delta = float(norm.cdf(d1)) if option_type == "CE" else float(norm.cdf(d1) - 1.0)
    gamma = pdf_d1 / (F * sigma * sqrt_T)
    theta_annual = -(F * pdf_d1 * sigma) / (2.0 * sqrt_T)
    theta_daily = theta_annual / 365.0
    return delta, gamma, theta_daily


def solve_black76_iv(price: float, F: float, K: float, T_years: float, option_type: str,
                     is_stale: bool = False, bounds=(0.0001, 5.0)) -> dict:
    """Solve Black-76 implied volatility using bracketed root finding (brentq)."""
    if is_stale:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "STALE_PRICE"}
    if price is None or np.isnan(price) or price <= 0 or F <= 0 or K <= 0 or T_years <= 0:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "NO_SOLVE"}

    intrinsic = max(0.0, F - K) if option_type == "CE" else max(0.0, K - F)
    if price < intrinsic - 1e-6:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "BELOW_INTRINSIC"}

    lo, hi = float(bounds[0]), float(bounds[1])
    f_lo = black76_price(F, K, T_years, lo, option_type) - price
    f_hi = black76_price(F, K, T_years, hi, option_type) - price
    if f_lo > 0:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "BELOW_INTRINSIC"}
    if f_hi < 0:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "NO_SOLVE"}
    try:
        iv = float(brentq(lambda s: black76_price(F, K, T_years, s, option_type) - price, lo, hi, xtol=1e-6))
        delta, gamma, theta = black76_greeks(F, K, T_years, iv, option_type)
        return {"iv_DERIVED": iv, "delta_DERIVED": delta, "gamma_DERIVED": gamma,
                "theta_DERIVED": theta, "iv_status": "OK"}
    except ValueError:
        return {"iv_DERIVED": np.nan, "delta_DERIVED": np.nan, "gamma_DERIVED": np.nan,
                "theta_DERIVED": np.nan, "iv_status": "NO_SOLVE"}


def dte_bucket_name(trading_dte: int) -> str:
    if trading_dte <= 0:
        return "0"
    if trading_dte == 1:
        return "1"
    if trading_dte == 2:
        return "2"
    if trading_dte in (3, 4):
        return "3-4"
    if 5 <= trading_dte <= 7:
        return "5-7"
    if 8 <= trading_dte <= 14:
        return "8-14"
    if 15 <= trading_dte <= 30:
        return "15-30"
    return "GT_30"


# ---- 9.1 Resampled 3m and 15m spot bars -------------------------------------------------------------------------
def derive_MultiMinute_bars(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate 1-minute spot bars into 3m or 15m buckets starting at 09:15.
    A bucket is emitted only up to its last 1m bar's ts_close; if len < minutes, is_partial=True."""
    if df_1m.empty:
        return pd.DataFrame()
    df = df_1m.sort_values("ts_open").copy()
    ts_open = pd.to_datetime(df["ts_open"]).dt.tz_convert(IST)
    df["ts_open"] = ts_open
    out_rows = []

    for (und, t_date), grp in df.groupby(["underlying", "trading_date"], sort=False):
        anchor = at_ist(t_date, "09:15")
        offsets = ((grp["ts_open"] - anchor).dt.total_seconds() // 60).astype(int)
        grp = grp.assign(_bucket_idx=offsets // minutes)
        for b_idx, b_grp in grp.groupby("_bucket_idx", sort=True):
            if b_idx < 0:
                continue
            b_start = anchor + dt.timedelta(minutes=int(b_idx) * minutes)
            b_end = b_start + dt.timedelta(minutes=minutes)
            is_partial = len(b_grp) < minutes
            # Rule 8 & Section 9: Derived bar is available at b_end (after its last 1m bar exists)
            avail_ts = b_end if not is_partial else pd.to_datetime(b_grp["ts_close"]).max()
            out_rows.append({
                "underlying": und,
                "trading_date": to_date(t_date),
                "interval": f"{minutes}m",
                "ts_open": b_start,
                "ts_close": b_end,
                "o_DERIVED": float(b_grp["o"].iloc[0]),
                "h_DERIVED": float(b_grp["h"].max()),
                "l_DERIVED": float(b_grp["l"].min()),
                "c_DERIVED": float(b_grp["c"].iloc[-1]),
                "bar_count_DERIVED": int(len(b_grp)),
                "is_partial": bool(is_partial),
                "available_from_ts": avail_ts,
            })
    return pd.DataFrame(out_rows)


def _settlement_time_ist(expiry_date: dt.date, iv_cfg: dict) -> dt.datetime:
    """Time at which the expiring contract's settlement value is fixed (from iv_model.yaml `settlement`).
    v3: no hard-coded date here any more; 15:30 is only the fallback when the table has no matching row."""
    hhmm = "15:30"
    for r in iv_cfg.get("settlement", []):
        f_d = to_date(r["from"]) if r.get("from") else None
        t_d = to_date(r["to"]) if r.get("to") else None
        if (f_d is None or f_d <= expiry_date) and (t_d is None or expiry_date <= t_d):
            hhmm = r["time"]
            break
    return at_ist(expiry_date, hhmm)


def compute_ivp_vix_for_day(vix_daily_df: pd.DataFrame, target_day: dt.date,
                            intraday_vix_val: Optional[float] = None,
                            decision_ts: Optional[dt.datetime] = None) -> dict:
    """Section 9.4 & T18: percentile rank of the India VIX value known at decision time
    among the PREVIOUS 252 daily closes (strictly trading_date < target_day, never including target_day)."""
    hist = vix_daily_df[vix_daily_df["trading_date"] < target_day].sort_values("trading_date")
    avail = decision_ts if decision_ts is not None else at_ist(target_day, "09:15")
    if len(hist) < 252:
        return {
            "trading_date": target_day,
            "ivp_vix_PROXY": np.nan,
            "ivp_status": "INSUFFICIENT_HISTORY",
            "available_from_ts": avail,
        }
    window_252 = hist["c"].tail(252).to_numpy(dtype=float)
    ref_val = float(intraday_vix_val) if intraday_vix_val is not None else float(window_252[-1])
    pct = float(np.mean(window_252 <= ref_val) * 100.0)
    return {
        "trading_date": target_day,
        "ivp_vix_PROXY": round(pct, 4),
        "ivp_status": "OK",
        "available_from_ts": avail,
    }


def compute_rv20_for_day(spot_daily_df: pd.DataFrame, target_day: dt.date, calendar: Calendar) -> list:
    """Section 9.5 & Rule 8: RV20 from daily closes, annualised with sqrt(252).
    Emits two rows for target_day:
    1) intraday version using closes strictly up to D-1 (available_from_ts = D 09:15)
    2) EOD version including D's close (available_from_ts = D close time, e.g. 15:30)."""
    rows = []
    # Intraday version (up to D-1)
    prev_df = spot_daily_df[spot_daily_df["trading_date"] < target_day].sort_values("trading_date")
    if len(prev_df) >= 21:
        closes = prev_df["c"].tail(21).to_numpy(dtype=float)
        log_rets = np.diff(np.log(closes))
        rv_prev = float(np.std(log_rets, ddof=1) * math.sqrt(252.0))
    else:
        rv_prev = np.nan
    rows.append({
        "trading_date": target_day,
        "version": "INTRADAY_PREV_CLOSE",
        "rv20_DERIVED": rv_prev,
        "available_from_ts": at_ist(target_day, "09:15"),
    })

    # EOD version (up to D inclusive)
    incl_df = spot_daily_df[spot_daily_df["trading_date"] <= target_day].sort_values("trading_date")
    close_hhmm = calendar.close_time_text("nse_spot_index", target_day) or "15:30"
    if len(incl_df) >= 21 and (incl_df["trading_date"] == target_day).any():
        closes = incl_df["c"].tail(21).to_numpy(dtype=float)
        log_rets = np.diff(np.log(closes))
        rv_eod = float(np.std(log_rets, ddof=1) * math.sqrt(252.0))
    else:
        rv_eod = np.nan
    rows.append({
        "trading_date": target_day,
        "version": "EOD_INCLUSIVE",
        "rv20_DERIVED": rv_eod,
        "available_from_ts": at_ist(target_day, close_hhmm),
    })
    return rows


HIST_KEY3 = ["hhmm", "ot", "m_bkt"]
HIST_KEY4 = HIST_KEY3 + ["dte_bkt"]
HIST_COLS = HIST_KEY4 + ["v"]
OPTION_TABLES = ("opt_1m", "opt_wide_1m")


def _empty_hist() -> pd.DataFrame:
    return pd.DataFrame({"hhmm": pd.Series(dtype=object), "ot": pd.Series(dtype=object),
                         "m_bkt": pd.Series(dtype=float), "dte_bkt": pd.Series(dtype=object),
                         "v": pd.Series(dtype=float)})


def _trim_hist(hist: pd.DataFrame) -> pd.DataFrame:
    """Keep the last 5 entries per (minute, side, moneyness bucket, DTE bucket): enough for both baselines."""
    return hist.groupby(HIST_KEY4, sort=False).tail(5) if len(hist) else hist


def _baselines(hist: pd.DataFrame, keys: pd.DataFrame, is_monthly: bool):
    """Section 9.6 volume baselines for today's rows from EARLIER days only (hist never holds today)."""
    n = len(keys)
    cnt_a, mean_a = np.zeros(n), np.full(n, np.nan)
    cnt_s, mean_s = np.zeros(n), np.full(n, np.nan)
    if len(hist):
        any5 = hist.groupby(HIST_KEY3, sort=False).tail(5).groupby(HIST_KEY3, sort=False)["v"] \
            .agg(["count", "mean"]).reset_index()
        same5 = hist.groupby(HIST_KEY4, sort=False)["v"].agg(["count", "mean"]).reset_index()
        ma = keys[HIST_KEY3].merge(any5, how="left", on=HIST_KEY3)
        ms = keys[HIST_KEY4].merge(same5, how="left", on=HIST_KEY4)
        cnt_a, mean_a = ma["count"].fillna(0).to_numpy(), ma["mean"].to_numpy(dtype=float)
        cnt_s, mean_s = ms["count"].fillna(0).to_numpy(), ms["mean"].to_numpy(dtype=float)
    ok_a, ok_s = cnt_a >= 5, cnt_s >= 5
    st_any = np.where(ok_a, "OK", "INSUFFICIENT_HISTORY").astype(object)
    st_same = np.where(ok_s, "OK", "INSUFFICIENT_HISTORY").astype(object)
    if is_monthly:
        st_same[(keys["dte_bkt"].to_numpy() == "0") & (cnt_s == 0)] = "NO_HISTORY"
    return np.where(ok_a, mean_a, np.nan), st_any, np.where(ok_s, mean_s, np.nan), st_same


def read_option_day(settings: Settings, part_name: str, und: str, d: dt.date) -> pd.DataFrame:
    """Band (opt_1m) plus wide chain (opt_wide_1m) of one underlying-day; a contract-minute appears once.
    v4: the downloader no longer fetches a contract-day twice, so the two tables complement each other."""
    frames = []
    for tbl in OPTION_TABLES:
        p = part_path(settings.store_dir, part_name, tbl, und, d)
        if p.exists():
            frames.append(read_parquet(p))
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0]
    return pd.concat(frames, ignore_index=True).drop_duplicates(["instrument_key", "ts_open"], keep="last")


def _derive_option_day(und: str, d: dt.date, odf: pd.DataFrame, sdf: pd.DataFrame, calendar: Calendar,
                       iv_cfg: dict, default_step: float, is_monthly: bool, hist: pd.DataFrame, consts: tuple):
    """Chain, IV/greeks, baselines, fill assumptions and chain metrics for one underlying-day, on whole
    columns (v4; the v3 code looped over every row). Returns (chain, iv, metrics, day_hist) or None."""
    r_rate, q_yield, mins_per_yr, iv_bounds, tick_sz = consts
    odf = odf[odf["option_type"].isin(["CE", "PE"]) & odf["strike"].notna()].copy()
    if odf.empty:
        return None
    odf["ts_open"] = pd.to_datetime(odf["ts_open"]).dt.tz_convert(IST)
    odf = odf.sort_values(["ts_open", "expiry_date", "strike", "option_type"], kind="stable").reset_index(drop=True)
    ts_open = odf["ts_open"]
    ts_close = ts_open + pd.Timedelta(minutes=1)
    tc_ns = _to_ns(ts_close)
    grp = odf.groupby(["ts_open", "expiry_date"], sort=True).ngroup().to_numpy()     # one id per (minute, expiry)
    n_g = int(grp.max()) + 1
    first_idx = np.unique(grp, return_index=True)[1]

    exp_codes, exp_uniques = pd.factorize(odf["expiry_date"])
    exp_dates = np.array([to_date(e) for e in exp_uniques], dtype=object)
    settle_ns = np.array([pd.Timestamp(_settlement_time_ist(e, iv_cfg)).tz_convert("UTC").value for e in exp_dates])
    is_td = 1 if calendar.is_trading_day(d) else 0
    dte_names = np.array([dte_bucket_name(max(0, calendar.count_trading_days(d, e) - is_td)) for e in exp_dates],
                         dtype=object)
    T = np.maximum(1.0, (settle_ns[exp_codes] - tc_ns) / 60e9) / mins_per_yr
    dte_row = dte_names[exp_codes]

    # spot known at each minute close (no look-ahead); a carried spot is flagged (Rule 4)
    u_ns, inv = np.unique(tc_ns, return_inverse=True)
    u_times = pd.to_datetime(u_ns, utc=True)
    pairs = spot_asof_age(sdf, u_times)
    spot_u = np.array([x[0] for x in pairs], dtype=float)
    age_u = np.array([x[1] for x in pairs], dtype=float)
    spot, spot_age = spot_u[inv], age_u[inv]
    spot_filled = pd.array(np.where(np.isnan(spot_age), None, spot_age >= 1.0), dtype="boolean")

    K = odf["strike"].to_numpy(dtype=float)
    ot = odf["option_type"].to_numpy()
    is_call = ot == "CE"
    c = odf["c"].to_numpy(dtype=float)
    v = odf["v"].to_numpy(dtype="float64", na_value=np.nan)
    oi = odf["oi"].to_numpy(dtype="float64", na_value=np.nan)
    stale = odf["is_stale"].to_numpy(dtype=bool)

    # reconstructed chain per (minute, expiry, strike): missing legs stay null, never 0
    def leg(mask, pfx):
        return pd.DataFrame({"g": grp[mask], "K": K[mask], f"{pfx}_c": c[mask], f"{pfx}_v": v[mask],
                             f"{pfx}_oi": oi[mask], f"{pfx}_stale": stale[mask]}).drop_duplicates(["g", "K"], keep="last")
    pair = leg(is_call, "ce").merge(leg(~is_call, "pe"), on=["g", "K"], how="outer", sort=True)
    for pfx in ("ce", "pe"):
        col = pair[f"{pfx}_stale"]
        pair[f"{pfx}_stale"] = col.where(col.notna(), True).astype(bool)

    # forward: put-call parity at the strike with the smallest |C - P| among non-stale pairs, else cost of carry
    cand = pair[pair["ce_c"].notna() & pair["pe_c"].notna() & ~pair["ce_stale"] & ~pair["pe_stale"]].copy()
    cand["diff"] = (cand["ce_c"] - cand["pe_c"]).abs()
    best = cand.sort_values(["g", "diff", "K"], kind="stable").drop_duplicates("g", keep="first")
    f_par = np.full(n_g, np.nan)
    f_par[best["g"].to_numpy()] = (best["K"] + best["ce_c"] - best["pe_c"]).to_numpy()
    g_spot, g_T = spot[first_idx], T[first_idx]
    with np.errstate(invalid="ignore"):
        par_ok = np.isfinite(f_par) & (f_par > 0)
    has_spot = np.isfinite(g_spot)
    f_g = np.where(par_ok, f_par, np.where(has_spot, g_spot * np.exp((r_rate - q_yield) * g_T), np.nan))
    src_g = np.where(par_ok, "PARITY", np.where(has_spot, "COST_OF_CARRY", "NONE_NO_SPOT_YET")).astype(object)
    F = f_g[grp]

    iv, status = solve_iv_vec(c, F, K, T, is_call, stale, iv_bounds)
    delta, gamma, theta = greeks_vec(F, K, T, iv, is_call)

    step = strike_step(np.unique(K), default_step)
    with np.errstate(invalid="ignore"):
        m_bkt = np.where(np.isfinite(spot), np.rint((K - spot) / step), np.nan)
        # OI is already in units (quantity): no lot-size factor
        contrib = np.where(np.isfinite(gamma) & np.isfinite(oi) & np.isfinite(spot),
                           np.where(is_call, 1.0, -1.0) * gamma * oi * spot ** 2 * 0.01, 0.0)
    gex_g = np.bincount(grp, weights=contrib, minlength=n_g)

    keys = pd.DataFrame({"hhmm": ts_open.dt.strftime("%H:%M").to_numpy(), "ot": ot, "m_bkt": m_bkt, "dte_bkt": dte_row})
    vb_any, st_any, vb_same, st_same = _baselines(hist, keys, is_monthly)
    half_sp = np.maximum(tick_sz, 0.005 * c)
    with np.errstate(invalid="ignore"):
        fillable = np.isfinite(v) & (v > 0) & ~stale

    iv_df = pd.DataFrame({
        "instrument_key": odf["instrument_key"].to_numpy(), "underlying": und, "trading_date": d,
        "expiry_date": exp_dates[exp_codes], "ts_open": ts_open, "ts_close": ts_close,
        "strike": K, "option_type": ot, "moneyness_bucket": m_bkt, "dte_bucket": dte_row,
        "close": c, "v": v, "oi": oi, "is_stale": stale,
        "spot_asof_DERIVED": spot, "spot_age_min": spot_age, "spot_is_filled": spot_filled,
        "forward_DERIVED": F, "forward_source": src_g[grp],
        "iv_DERIVED": iv, "delta_DERIVED": delta, "gamma_DERIVED": gamma, "theta_DERIVED": theta,
        "iv_status": status,
        "volume_baseline_same_dte_DERIVED": vb_same, "baseline_same_dte_status": st_same,
        "volume_baseline_any_dte_DERIVED": vb_any, "baseline_any_dte_status": st_any,
        "half_spread_PROXY": half_sp, "modelled_ask_PROXY": c + half_sp, "fillable": fillable,
        "available_from_ts": ts_close,
    })

    g_open = ts_open.iloc[first_idx].reset_index(drop=True)
    g_close = ts_close.iloc[first_idx].reset_index(drop=True)
    g_exp = exp_dates[exp_codes[first_idx]]
    pg = pair["g"].to_numpy()
    chain_df = pd.DataFrame({
        "underlying": und, "trading_date": d, "expiry_date": g_exp[pg],
        "ts_open": g_open.iloc[pg].reset_index(drop=True), "ts_close": g_close.iloc[pg].reset_index(drop=True),
        "strike": pair["K"].to_numpy(),
        "ce_close_DERIVED": pair["ce_c"].to_numpy(), "ce_v_DERIVED": pair["ce_v"].to_numpy(),
        "ce_oi_DERIVED": pair["ce_oi"].to_numpy(), "ce_is_stale": pair["ce_stale"].to_numpy(),
        "pe_close_DERIVED": pair["pe_c"].to_numpy(), "pe_v_DERIVED": pair["pe_v"].to_numpy(),
        "pe_oi_DERIVED": pair["pe_oi"].to_numpy(), "pe_is_stale": pair["pe_stale"].to_numpy(),
        "available_from_ts": g_close.iloc[pg].reset_index(drop=True),
    })

    # chain metrics as of each minute close: OI carried from each contract's latest closed candle
    mats = oi_matrices(odf, u_times)
    g_time, g_code = inv[first_idx], exp_codes[first_idx]
    names = ("pcr", "call_oi_wall", "put_oi_wall", "max_pain", "n_strikes", "n_oi_known", "n_oi_carried")
    met = {nm: np.full(n_g, np.nan) for nm in names}
    for code, e in enumerate(exp_uniques):
        sel = np.flatnonzero(g_code == code)
        if e not in mats or not sel.size:
            continue
        mm = metrics_matrix(mats[e], spot_u)
        for nm in names:
            met[nm][sel] = mm[nm][g_time[sel]]
    g_age = spot_age[first_idx]
    metrics_df = pd.DataFrame({
        "underlying": und, "trading_date": d, "expiry_date": g_exp, "ts_open": g_open, "ts_close": g_close,
        "pcr_DERIVED": met["pcr"], "call_oi_wall_DERIVED": met["call_oi_wall"],
        "put_oi_wall_DERIVED": met["put_oi_wall"], "max_pain_DERIVED": met["max_pain"],
        "n_strikes_DERIVED": np.nan_to_num(met["n_strikes"]).astype("int64"),
        "n_oi_known_DERIVED": np.nan_to_num(met["n_oi_known"]).astype("int64"),
        "n_oi_carried_DERIVED": np.nan_to_num(met["n_oi_carried"]).astype("int64"),
        "gex_proxy_PROXY": gex_g, "spot_asof_DERIVED": g_spot, "spot_age_min": g_age,
        "spot_is_filled": pd.array(np.where(np.isnan(g_age), None, g_age >= 1.0), dtype="boolean"),
        "available_from_ts": g_close,
    })
    with np.errstate(invalid="ignore"):
        keep = np.isfinite(m_bkt) & np.isfinite(v)
    day_hist = keys[keep].assign(v=v[keep])[HIST_COLS]
    return chain_df, iv_df, metrics_df, day_hist


def _load_hist(settings: Settings, und: str, before: dt.date, n_days: int = 40) -> pd.DataFrame:
    """Baseline history for a windowed derive: volumes of up to n_days derived days BEFORE the window."""
    parts = []
    for part in ("dev", "holdout"):
        parts += [(date_of_part(p), p) for p in list_parts(settings.store_dir, part, "iv_greeks_1m", und)]
    frames = []
    for _d, p in sorted(x for x in parts if x[0] < before)[-n_days:]:
        df = read_parquet(p, columns=["ts_open", "option_type", "moneyness_bucket", "dte_bucket", "v"])
        df = df[df["moneyness_bucket"].notna() & df["v"].notna()]
        if len(df):
            frames.append(pd.DataFrame({
                "hhmm": pd.to_datetime(df["ts_open"]).dt.tz_convert(IST).dt.strftime("%H:%M").to_numpy(),
                "ot": df["option_type"].to_numpy(), "m_bkt": df["moneyness_bucket"].to_numpy(dtype=float),
                "dte_bkt": df["dte_bucket"].to_numpy(), "v": df["v"].to_numpy(dtype=float)}))
    return _trim_hist(pd.concat(frames, ignore_index=True)) if frames else _empty_hist()


def run_derive(settings: Settings, calendar: Optional[Calendar] = None, window: Optional[tuple] = None,
               tick=None) -> dict:
    """Derive the Stage 3 tables in strict date sequence without look-ahead.
    window=(first_day, last_day) derives only those days (week mode); volume baselines then start from the
    already derived earlier days on disk. Without a window every day in the store is rebuilt."""
    calendar = calendar or load_calendar(settings.reference_dir)
    iv_cfg = load_yaml(settings.reference_dir / "iv_model.yaml")
    consts = (float(iv_cfg.get("risk_free_rate", 0.065)), float(iv_cfg.get("dividend_yield", 0.0)),
              float(iv_cfg.get("minutes_per_year", 525600.0)), tuple(iv_cfg.get("iv_bounds", [0.0001, 5.0])), 0.05)
    w0, w1 = (to_date(window[0]), to_date(window[1])) if window else (None, None)

    def daily_frame(table, und=None):
        parts = list_parts(settings.store_dir, "dev", table, und) + list_parts(settings.store_dir, "holdout", table, und)
        df = pd.concat([read_parquet(p) for p in parts], ignore_index=True) if parts else pd.DataFrame()
        if not df.empty:
            df["trading_date"] = [to_date(x) for x in df["trading_date"]]
        return df

    # daily VIX and daily spot across dev and holdout (filtered per day < D inside the loop)
    vix_1d_df = daily_frame("vix_1d")
    written_files = 0

    for und in settings.instrument_names():
        default_step = float(settings.instruments[und].get("strike_step_default", 50.0))
        s1d_df = daily_frame("spot_1d", und)
        s1m_parts = list_parts(settings.store_dir, "dev", "spot_1m", und) + \
            list_parts(settings.store_dir, "holdout", "spot_1m", und)
        days_sorted = sorted({date_of_part(p) for p in s1m_parts})
        if window:
            days_sorted = [x for x in days_sorted if w0 <= x <= w1]
        # volume history holds EARLIER days only; a day is added after it has been derived
        hist = _load_hist(settings, und, w0) if window else _empty_hist()

        for d in days_sorted:
            if tick:
                tick(f"{und} {iso(d)}")
            part_name = partition_of(d, settings.holdout_start)
            # causality rule: when deriving a dev day, never read any holdout row
            allowed_daily_vix = vix_1d_df[vix_1d_df["trading_date"] < settings.holdout_start] \
                if (not vix_1d_df.empty and part_name == "dev") else vix_1d_df
            allowed_daily_spot = s1d_df[s1d_df["trading_date"] < settings.holdout_start] \
                if (not s1d_df.empty and part_name == "dev") else s1d_df

            sp_path = part_path(settings.store_dir, part_name, "spot_1m", und, d)
            if not sp_path.exists():
                continue
            sdf = read_parquet(sp_path)
            sdf["ts_open"] = pd.to_datetime(sdf["ts_open"]).dt.tz_convert(IST)
            sdf["ts_close"] = pd.to_datetime(sdf["ts_close"]).dt.tz_convert(IST)

            # 9.1 bars_3m and bars_15m
            for minutes, table in ((3, "bars_3m"), (15, "bars_15m")):
                bars = derive_MultiMinute_bars(sdf, minutes)
                if not bars.empty:
                    write_parquet(bars, part_path(settings.store_dir, part_name, table, und, d),
                                  {**TABLE_METADATA, "table": table})
                    written_files += 1

            # 9.4 and 9.5 daily RV20 and the IVP VIX proxy
            rv_rows = compute_rv20_for_day(allowed_daily_spot, d, calendar) if not allowed_daily_spot.empty else [
                {"trading_date": d, "version": "INTRADAY_PREV_CLOSE", "rv20_DERIVED": np.nan,
                 "available_from_ts": at_ist(d, "09:15")}
            ]
            ivp_row = compute_ivp_vix_for_day(allowed_daily_vix, d) if not allowed_daily_vix.empty else {
                "trading_date": d, "ivp_vix_PROXY": np.nan, "ivp_status": "INSUFFICIENT_HISTORY",
                "available_from_ts": at_ist(d, "09:15"),
            }
            daily_df = pd.DataFrame([{**r, "underlying": und, "ivp_vix_PROXY": ivp_row["ivp_vix_PROXY"],
                                      "ivp_status": ivp_row["ivp_status"]} for r in rv_rows])
            write_parquet(daily_df, part_path(settings.store_dir, part_name, "daily_metrics_DERIVED", und, d),
                          {**TABLE_METADATA, "table": "daily_metrics_DERIVED"})
            written_files += 1

            # 9.2, 9.3, 9.5, 9.6, 9.7: chain, IV/greeks, chain metrics, baselines, fill assumptions
            odf = read_option_day(settings, part_name, und, d)
            if odf.empty:
                continue
            res = _derive_option_day(und, d, odf, sdf, calendar, iv_cfg, default_step,
                                     settings.is_monthly(und), hist, consts)
            if res is None:
                continue
            chain_df, iv_df, metrics_df, day_hist = res
            for table, df in (("chain_1m", chain_df), ("iv_greeks_1m", iv_df), ("chain_metrics_1m", metrics_df)):
                write_parquet(df, part_path(settings.store_dir, part_name, table, und, d),
                              {**TABLE_METADATA, "table": table})
                written_files += 1
            hist = _trim_hist(pd.concat([hist, day_hist], ignore_index=True)) if len(day_hist) else hist

    return {"files_written": written_files}
