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
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

from .chain import asof_chain, chain_metrics, spot_asof_age, spot_flags
from .config import Settings, load_yaml
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


def run_derive(settings: Settings, calendar: Optional[Calendar] = None) -> dict:
    """Derive all Stage 3 tables across dev and holdout in strict date sequence without look-ahead."""
    calendar = calendar or load_calendar(settings.reference_dir)
    iv_cfg = load_yaml(settings.reference_dir / "iv_model.yaml")
    specs_cfg = load_yaml(settings.reference_dir / "specs.yaml")
    # v3: lot sizes are no longer needed here. Upstox reports OI in units (quantity), not lots (staff reply,
    # community.upstox.com/t/18087, 2026-10-19), so GEX must not multiply OI by the lot size again.
    _ = specs_cfg
    r_rate = float(iv_cfg.get("risk_free_rate", 0.065))
    q_yield = float(iv_cfg.get("dividend_yield", 0.0))
    mins_per_yr = float(iv_cfg.get("minutes_per_year", 525600.0))
    iv_bounds = tuple(iv_cfg.get("iv_bounds", [0.0001, 5.0]))
    tick_sz = 0.05

    # Gather all daily VIX and daily Spot across dev and holdout (filtered per day < D during loop!)
    vix_1d_parts = list_parts(settings.store_dir, "dev", "vix_1d") + list_parts(settings.store_dir, "holdout", "vix_1d")
    vix_1d_df = pd.concat([read_parquet(p) for p in vix_1d_parts], ignore_index=True) if vix_1d_parts else pd.DataFrame()
    if not vix_1d_df.empty:
        vix_1d_df["trading_date"] = [to_date(d) for d in vix_1d_df["trading_date"]]

    written_files = 0

    for und in settings.instrument_names():
        default_step = float(settings.instruments[und].get("strike_step_default", 50.0))

        # Load daily spot for RV20
        s1d_parts = list_parts(settings.store_dir, "dev", "spot_1d", und) + \
                    list_parts(settings.store_dir, "holdout", "spot_1d", und)
        s1d_df = pd.concat([read_parquet(p) for p in s1d_parts], ignore_index=True) if s1d_parts else pd.DataFrame()
        if not s1d_df.empty:
            s1d_df["trading_date"] = [to_date(d) for d in s1d_df["trading_date"]]

        # Collect all 1m spot days in chronological sequence
        s1m_parts = list_parts(settings.store_dir, "dev", "spot_1m", und) + \
                    list_parts(settings.store_dir, "holdout", "spot_1m", und)
        days_sorted = sorted({date_of_part(p) for p in s1m_parts})

        # Rolling history for volume baselines (strictly from earlier days < d):
        # Keyed by (hhmm, moneyness_bucket) -> list of (trading_date, dte_bucket, volume)
        vol_history = defaultdict(list)

        for d in days_sorted:
            part_name = partition_of(d, settings.holdout_start)
            # Causality rule: when deriving a dev day, never read any holdout row
            allowed_Daily_vix = vix_1d_df[vix_1d_df["trading_date"] < settings.holdout_start] \
                if (not vix_1d_df.empty and part_name == "dev") else vix_1d_df
            allowed_Daily_spot = s1d_df[s1d_df["trading_date"] < settings.holdout_start] \
                if (not s1d_df.empty and part_name == "dev") else s1d_df

            sp_path = part_path(settings.store_dir, part_name, "spot_1m", und, d)
            if not sp_path.exists():
                continue
            sdf = read_parquet(sp_path)
            sdf["ts_open"] = pd.to_datetime(sdf["ts_open"]).dt.tz_convert(IST)
            sdf["ts_close"] = pd.to_datetime(sdf["ts_close"]).dt.tz_convert(IST)

            # 9.1 Derive bars_3m and bars_15m
            b3 = derive_MultiMinute_bars(sdf, 3)
            b15 = derive_MultiMinute_bars(sdf, 15)
            if not b3.empty:
                write_parquet(b3, part_path(settings.store_dir, part_name, "bars_3m", und, d),
                              {**TABLE_METADATA, "table": "bars_3m"})
                written_files += 1
            if not b15.empty:
                write_parquet(b15, part_path(settings.store_dir, part_name, "bars_15m", und, d),
                              {**TABLE_METADATA, "table": "bars_15m"})
                written_files += 1

            # 9.4 & 9.5 Daily RV20 and IVP VIX Proxy
            rv_rows = compute_rv20_for_day(allowed_Daily_spot, d, calendar) if not allowed_Daily_spot.empty else [
                {"trading_date": d, "version": "INTRADAY_PREV_CLOSE", "rv20_DERIVED": np.nan,
                 "available_from_ts": at_ist(d, "09:15")}
            ]
            ivp_row = compute_ivp_vix_for_day(allowed_Daily_vix, d) if not allowed_Daily_vix.empty else {
                "trading_date": d, "ivp_vix_PROXY": np.nan, "ivp_status": "INSUFFICIENT_HISTORY",
                "available_from_ts": at_ist(d, "09:15"),
            }
            daily_df = pd.DataFrame([{**r, "underlying": und, "ivp_vix_PROXY": ivp_row["ivp_vix_PROXY"],
                                      "ivp_status": ivp_row["ivp_status"]} for r in rv_rows])
            write_parquet(daily_df, part_path(settings.store_dir, part_name, "daily_metrics_DERIVED", und, d),
                          {**TABLE_METADATA, "table": "daily_metrics_DERIVED"})
            written_files += 1

            # Option chain, IV/Greeks, Baselines, Fill assumptions (9.2, 9.3, 9.5, 9.6, 9.7)
            opt_path = part_path(settings.store_dir, part_name, "opt_1m", und, d)
            if not opt_path.exists():
                continue
            odf = read_parquet(opt_path)
            if odf.empty:
                continue
            odf["ts_open"] = pd.to_datetime(odf["ts_open"]).dt.tz_convert(IST)
            odf["ts_close"] = pd.to_datetime(odf["ts_close"]).dt.tz_convert(IST)

            step = strike_step(odf["strike"].dropna().unique(), default_step)
            # v3 (no look-ahead): the spot used for minute T is the last spot candle closed at or before T.
            # Before, a missing spot minute fell back to the day's LAST close (a value from the future).
            minute_ts = sorted(set(odf["ts_open"]))
            spot_pairs = dict(zip(minute_ts, spot_asof_age(sdf, [t + dt.timedelta(minutes=1) for t in minute_ts])))
            spot_known = {k: v[0] for k, v in spot_pairs.items()}
            # Chain "as of" each minute close: OI and prices carried from older candles are flagged, never 0.
            snap_all = asof_chain(odf, [t + dt.timedelta(minutes=1) for t in minute_ts])
            snap_groups = {k: g for k, g in snap_all.groupby(["snapshot_ts", "expiry_date"], sort=False)}

            chain_rows = []
            iv_rows = []
            minute_summary_rows = []
            day_vol_records = []

            for (ts_o, exp_raw), mgrp in odf.groupby(["ts_open", "expiry_date"], sort=True):
                exp_d = to_date(exp_raw)
                ts_c = ts_o + dt.timedelta(minutes=1)
                spot_c = spot_known.get(ts_o, float("nan"))
                # v3.1 (Rule 4): flag a carried spot (index closes 15:30, F&O trades on to 15:40)
                spot_age, spot_filled = spot_flags(spot_pairs.get(ts_o, (np.nan, np.nan))[1])
                settle_dt = _settlement_time_ist(exp_d, iv_cfg)
                t_mins = max(1.0, (settle_dt - ts_c).total_seconds() / 60.0)
                T_yrs = t_mins / mins_per_yr
                tdte = calendar.count_trading_days(d, exp_d) - (1 if calendar.is_trading_day(d) else 0)
                dte_bkt = dte_bucket_name(max(0, tdte))
                hhmm = ts_o.strftime("%H:%M")

                # Reconstruct CE/PE per strike
                by_k = defaultdict(dict)
                for _, orow in mgrp.iterrows():
                    k_val = float(orow["strike"])
                    ot = orow["option_type"]
                    by_k[k_val][ot] = orow

                # Forward F: PARITY_THEN_COST_OF_CARRY (Section 9.3)
                best_diff = None
                F_val = None
                for k_val, pair in sorted(by_k.items()):
                    if "CE" in pair and "PE" in pair:
                        ce_r, pe_r = pair["CE"], pair["PE"]
                        if not bool(ce_r["is_stale"]) and not bool(pe_r["is_stale"]):
                            diff = abs(float(ce_r["c"]) - float(pe_r["c"]))
                            if best_diff is None or diff < best_diff:
                                best_diff = diff
                                F_val = k_val + float(ce_r["c"]) - float(pe_r["c"])
                if F_val is not None and F_val > 0:
                    fwd_source = "PARITY"
                elif not math.isnan(spot_c):
                    F_val = spot_c * math.exp((r_rate - q_yield) * T_yrs)
                    fwd_source = "COST_OF_CARRY"
                else:
                    F_val = float("nan")
                    fwd_source = "NONE_NO_SPOT_YET"

                gex_total = 0.0

                for k_val, pair in sorted(by_k.items()):
                    ce_r = pair.get("CE")
                    pe_r = pair.get("PE")
                    ce_c = float(ce_r["c"]) if ce_r is not None else np.nan
                    pe_c = float(pe_r["c"]) if pe_r is not None else np.nan
                    # v3 (Rule 4): a contract without a candle in this minute keeps null volume / OI, never 0
                    ce_v = float(ce_r["v"]) if (ce_r is not None and pd.notna(ce_r["v"])) else np.nan
                    pe_v = float(pe_r["v"]) if (pe_r is not None and pd.notna(pe_r["v"])) else np.nan
                    ce_oi = float(ce_r["oi"]) if (ce_r is not None and pd.notna(ce_r["oi"])) else np.nan
                    pe_oi = float(pe_r["oi"]) if (pe_r is not None and pd.notna(pe_r["oi"])) else np.nan
                    ce_stale = bool(ce_r["is_stale"]) if ce_r is not None else True
                    pe_stale = bool(pe_r["is_stale"]) if pe_r is not None else True

                    chain_rows.append({
                        "underlying": und, "trading_date": d, "expiry_date": exp_d,
                        "ts_open": ts_o, "ts_close": ts_c, "strike": k_val,
                        "ce_close_DERIVED": ce_c, "ce_v_DERIVED": ce_v, "ce_oi_DERIVED": ce_oi, "ce_is_stale": ce_stale,
                        "pe_close_DERIVED": pe_c, "pe_v_DERIVED": pe_v, "pe_oi_DERIVED": pe_oi, "pe_is_stale": pe_stale,
                        "available_from_ts": ts_c,
                    })

                    if math.isnan(spot_c):
                        m_bkt = None
                    else:
                        m_bkt = int(round((k_val - spot_c) / step))
                    for ot, r_opt in (("CE", ce_r), ("PE", pe_r)):
                        if r_opt is None:
                            continue
                        opt_c = float(r_opt["c"])
                        opt_v = float(r_opt["v"]) if pd.notna(r_opt["v"]) else np.nan
                        opt_oi = float(r_opt["oi"]) if pd.notna(r_opt["oi"]) else np.nan
                        opt_stale = bool(r_opt["is_stale"])
                        iv_res = solve_black76_iv(opt_c, F_val, k_val, T_yrs, ot, is_stale=opt_stale, bounds=iv_bounds)
                        if pd.notna(iv_res["gamma_DERIVED"]) and not math.isnan(opt_oi) and not math.isnan(spot_c):
                            sign = 1.0 if ot == "CE" else -1.0
                            # OI is already in units (quantity): no lot-size factor (v3 fix)
                            gex_total += sign * float(iv_res["gamma_DERIVED"]) * opt_oi * (spot_c ** 2) * 0.01

                        # Volume baselines (Section 9.6): match by (hhmm, ot, m_bkt), NEVER by strike name!
                        hist_entries = vol_history.get((hhmm, ot, m_bkt), [])
                        any_5 = [x[2] for x in hist_entries[-5:]]
                        same_5 = [x[2] for x in hist_entries if x[1] == dte_bkt][-5:]

                        if len(any_5) >= 5:
                            vb_any = float(np.mean(any_5))
                            st_any = "OK"
                        else:
                            vb_any = np.nan
                            st_any = "INSUFFICIENT_HISTORY"

                        if settings.is_monthly(und) and dte_bkt == "0" and len(same_5) == 0:
                            vb_same = np.nan
                            st_same = "NO_HISTORY"
                        elif len(same_5) >= 5:
                            vb_same = float(np.mean(same_5))
                            st_same = "OK"
                        else:
                            vb_same = np.nan
                            st_same = "INSUFFICIENT_HISTORY"

                        if m_bkt is not None and not math.isnan(opt_v):
                            day_vol_records.append(((hhmm, ot, m_bkt), (d, dte_bkt, opt_v)))

                        # Fill assumptions (Section 9.7)
                        half_sp = max(tick_sz, 0.005 * opt_c)
                        mod_ask = opt_c + half_sp
                        fillable = bool(not math.isnan(opt_v) and opt_v > 0 and not opt_stale)

                        iv_rows.append({
                            "instrument_key": r_opt["instrument_key"],
                            "underlying": und, "trading_date": d, "expiry_date": exp_d,
                            "ts_open": ts_o, "ts_close": ts_c,
                            "strike": k_val, "option_type": ot,
                            "moneyness_bucket": m_bkt, "dte_bucket": dte_bkt,
                            "close": opt_c, "v": opt_v, "oi": opt_oi, "is_stale": opt_stale,
                            "spot_asof_DERIVED": spot_c, "spot_age_min": spot_age, "spot_is_filled": spot_filled,
                            "forward_DERIVED": F_val, "forward_source": fwd_source,
                            "iv_DERIVED": iv_res["iv_DERIVED"],
                            "delta_DERIVED": iv_res["delta_DERIVED"],
                            "gamma_DERIVED": iv_res["gamma_DERIVED"],
                            "theta_DERIVED": iv_res["theta_DERIVED"],
                            "iv_status": iv_res["iv_status"],
                            "volume_baseline_same_dte_DERIVED": vb_same,
                            "baseline_same_dte_status": st_same,
                            "volume_baseline_any_dte_DERIVED": vb_any,
                            "baseline_any_dte_status": st_any,
                            "half_spread_PROXY": half_sp,
                            "modelled_ask_PROXY": mod_ask,
                            "fillable": fillable,
                            "available_from_ts": ts_c,
                        })

                snap = snap_groups.get((pd.Timestamp(ts_c), exp_raw))
                if snap is None:
                    snap = snap_all.iloc[0:0]
                met = chain_metrics(snap, None if math.isnan(spot_c) else spot_c) if not snap.empty else {
                    "pcr": np.nan, "call_oi_wall": np.nan, "put_oi_wall": np.nan, "max_pain": np.nan,
                    "n_strikes": 0, "n_oi_known": 0, "n_oi_carried": 0}
                minute_summary_rows.append({
                    "underlying": und, "trading_date": d, "expiry_date": exp_d,
                    "ts_open": ts_o, "ts_close": ts_c,
                    "pcr_DERIVED": met["pcr"],
                    "call_oi_wall_DERIVED": met["call_oi_wall"],
                    "put_oi_wall_DERIVED": met["put_oi_wall"],
                    "max_pain_DERIVED": met["max_pain"],
                    "n_strikes_DERIVED": met["n_strikes"],
                    "n_oi_known_DERIVED": met["n_oi_known"],
                    "n_oi_carried_DERIVED": met["n_oi_carried"],
                    "gex_proxy_PROXY": gex_total,
                    "spot_asof_DERIVED": spot_c, "spot_age_min": spot_age, "spot_is_filled": spot_filled,
                    "available_from_ts": ts_c,
                })

            # Update rolling volume history AFTER finishing day d so day d never sees its own future or same-day bars
            for k_hist, val_hist in day_vol_records:
                vol_history[k_hist].append(val_hist)

            if chain_rows:
                write_parquet(pd.DataFrame(chain_rows), part_path(settings.store_dir, part_name, "chain_1m", und, d),
                              {**TABLE_METADATA, "table": "chain_1m"})
                written_files += 1
            if iv_rows:
                write_parquet(pd.DataFrame(iv_rows), part_path(settings.store_dir, part_name, "iv_greeks_1m", und, d),
                              {**TABLE_METADATA, "table": "iv_greeks_1m"})
                written_files += 1
            if minute_summary_rows:
                write_parquet(pd.DataFrame(minute_summary_rows),
                              part_path(settings.store_dir, part_name, "chain_metrics_1m", und, d),
                              {**TABLE_METADATA, "table": "chain_metrics_1m"})
                written_files += 1

    return {"files_written": written_files}
