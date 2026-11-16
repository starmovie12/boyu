"""Derived volatility extras (PROMPT_02 9.4 / 9.5, PROMPT_03 C4): vega, ATM IV, skew, term spread, gamma flip.

All values use only rows of the same minute (the caller passes one row per contract-minute, with IV already
solved from that minute's close), so `available_from_ts` = the minute close, as for every other derived column.

* vega_DERIVED       Black-76 vega per 1 IV point (undiscounted, like the price model): F * phi(d1) * sqrt(T) / 100
* atm_iv_DERIVED     mean IV of the CE and PE at the strike nearest the forward, per (minute, expiry)
* skew_DERIVED       IV of the put nearest -0.25 delta minus IV of the call nearest +0.25 delta (both within
                     0.10 of that delta; otherwise null: a narrow band does not reach the 25-delta strikes)
* vix_term_DERIVED   near-expiry ATM IV minus next-expiry ATM IV in the same minute (null without a next expiry)
* gamma_flip_PROXY   the spot level where the summed gamma exposure (calls +, puts -, OI in units, the same
                     dealer-side assumption as gex_proxy_PROXY) changes sign, searched within +/-5% of spot;
                     null if it does not change sign there. PROXY: only contracts with a candle in that minute.
* iv_daily_DERIVED   ATM IV of the nearest expiry known at 10:00 and at 14:00 (one row each, with the DTE bucket)
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .timeutil import at_ist

SQRT_2PI = float(np.sqrt(2.0 * np.pi))
FLIP_GRID = np.linspace(-0.05, 0.05, 21)
SKEW_DELTA, SKEW_TOLERANCE = 0.25, 0.10
IV_DAILY_TIMES = ("10:00", "14:00")


def vega_vec(F, K, T, sigma) -> np.ndarray:
    F, K, T, sigma = (np.asarray(x, dtype=float) for x in (F, K, T, sigma))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        sqrt_t = np.sqrt(np.maximum(T, 1e-9))
        d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
        vega = F * np.exp(-0.5 * d1 * d1) / SQRT_2PI * sqrt_t / 100.0
    vega[~np.isfinite(sigma)] = np.nan
    return vega


def atm_and_skew(grp, n_g: int, K, F, iv, delta, is_call) -> tuple:
    """Per (minute, expiry) group: ATM IV and 25-delta skew. Arrays of length n_g, null where unknown."""
    atm, skew = np.full(n_g, np.nan), np.full(n_g, np.nan)
    ok = np.isfinite(iv) & np.isfinite(F)
    if not ok.any():
        return atm, skew
    df = pd.DataFrame({"g": grp[ok], "dist": np.abs(K[ok] - F[ok]), "iv": iv[ok], "delta": delta[ok],
                       "call": np.asarray(is_call)[ok]})
    nearest = df.groupby("g")["dist"].transform("min")
    a = df[df["dist"] <= nearest + 1e-9].groupby("g")["iv"].mean()
    atm[a.index.to_numpy()] = a.to_numpy()
    d = df[np.isfinite(df["delta"])]
    picks = []
    for is_c, target in ((False, -SKEW_DELTA), (True, SKEW_DELTA)):
        side = d[d["call"] == is_c].assign(k=lambda x, t=target: (x["delta"] - t).abs())
        side = side[side["k"] <= SKEW_TOLERANCE]
        picks.append(side.loc[side.groupby("g")["k"].idxmin()].set_index("g")["iv"] if len(side)
                     else pd.Series(dtype=float))
    puts, calls = picks
    both = puts.index.intersection(calls.index)
    if len(both):
        skew[both.to_numpy()] = (puts[both] - calls[both]).to_numpy()
    return atm, skew


def term_spread(g_time, g_exp, atm) -> np.ndarray:
    """Per group: (nearest expiry ATM IV) - (next expiry ATM IV) of the same minute, among expiries with an ATM IV."""
    out = np.full(len(atm), np.nan)
    df = pd.DataFrame({"t": np.asarray(g_time), "e": [str(x) for x in g_exp], "a": atm})
    df = df[np.isfinite(df["a"])].sort_values(["t", "e"], kind="stable")
    if df.empty:
        return out
    near = df.groupby("t").nth(0).set_index("t")["a"]
    nxt = df.groupby("t").nth(1).set_index("t")["a"]
    term = (near - nxt).dropna()
    if len(term):
        out = pd.Series(np.asarray(g_time)).map(term).to_numpy(dtype=float)
    return out


def gamma_flip(grp, n_g: int, F, K, T, iv, is_call, oi, spot, g_spot, grid=FLIP_GRID) -> np.ndarray:
    """Spot level of the zero crossing of summed gamma exposure nearest today's spot (null if none in the grid)."""
    out = np.full(n_g, np.nan)
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(iv) & np.isfinite(oi) & np.isfinite(spot) & np.isfinite(F) & (iv > 0) & (T > 0)
    if not ok.any():
        return out
    g, Fo, Ko, To, so, oio, s0 = grp[ok], F[ok], K[ok], T[ok], iv[ok], oi[ok], spot[ok]
    sign = np.where(np.asarray(is_call)[ok], 1.0, -1.0)
    sqrt_t = np.sqrt(To)
    G = np.zeros((n_g, len(grid)))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for k, x in enumerate(grid):
            fk, sk = Fo * (1.0 + x), s0 * (1.0 + x)
            d1 = (np.log(fk / Ko) + 0.5 * so * so * To) / (so * sqrt_t)
            gam = np.exp(-0.5 * d1 * d1) / SQRT_2PI / (fk * so * sqrt_t)
            G[:, k] = np.bincount(g, weights=np.nan_to_num(sign * gam * oio * sk * sk * 0.01), minlength=n_g)
    has = np.bincount(g, minlength=n_g) > 0
    s = np.sign(G)
    cross = (s[:, :-1] * s[:, 1:]) < 0
    mid = np.abs((grid[:-1] + grid[1:]) / 2.0)
    dist = np.where(cross, mid[None, :], np.inf)
    j = dist.argmin(axis=1)
    rows = np.arange(n_g)
    found = np.isfinite(dist[rows, j]) & has & np.isfinite(g_spot)
    g0, g1 = G[rows, j], G[rows, j + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = grid[j] - g0 * (grid[j + 1] - grid[j]) / (g1 - g0)
    out[found] = (np.asarray(g_spot, dtype=float) * (1.0 + xs))[found]
    return out


def iv_daily_rows(metrics_df: pd.DataFrame, und: str, d: dt.date, times=IV_DAILY_TIMES) -> pd.DataFrame:
    """ATM IV of the nearest expiry known at each check time (latest minute that closed at or before it)."""
    rows = []
    m_all = metrics_df[metrics_df["atm_iv_DERIVED"].notna()] if len(metrics_df) else metrics_df
    for hhmm in times:
        cut = at_ist(d, hhmm)
        m = m_all[pd.to_datetime(m_all["ts_close"]) <= pd.Timestamp(cut)] if len(m_all) else m_all
        row = {"underlying": und, "trading_date": d, "check_time": hhmm, "expiry_date": None, "dte_bucket": None,
               "iv_daily_DERIVED": np.nan, "asof_ts": pd.NaT, "status": "NO_ATM_IV_YET", "available_from_ts": cut}
        if len(m):
            last = m[m["ts_close"] == m["ts_close"].max()].sort_values("expiry_date", kind="stable").iloc[0]
            row.update(expiry_date=last["expiry_date"], dte_bucket=last.get("dte_bucket"),
                       iv_daily_DERIVED=float(last["atm_iv_DERIVED"]), asof_ts=last["ts_close"], status="OK")
        rows.append(row)
    return pd.DataFrame(rows)
