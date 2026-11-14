"""Vectorised Black-76 implied volatility and greeks (numpy, whole columns at once).

Same model, bounds and status rules as derive.solve_black76_iv / black76_greeks (which stay as the scalar
reference): undiscounted Black-76 on the forward, statuses OK / BELOW_INTRINSIC / NO_SOLVE / STALE_PRICE.
A phone cannot afford one root-finder call per option row; bisection on arrays gives the same root.
"""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr

SQRT_2PI = float(np.sqrt(2.0 * np.pi))


def black76_price_vec(F, K, T, sigma, is_call):
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        sqrt_t = np.sqrt(T)
        d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        call = F * ndtr(d1) - K * ndtr(d2)
        put = K * ndtr(-d2) - F * ndtr(-d1)
    return np.where(is_call, call, put)


def solve_iv_vec(price, F, K, T, is_call, is_stale, bounds=(0.0001, 5.0), iters: int = 44):
    """Return (iv, status) arrays. 44 bisection steps on [lo, hi] leave an interval far below 1e-6."""
    price = np.asarray(price, dtype=float)
    F = np.asarray(F, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)
    stale = np.asarray(is_stale, dtype=bool)
    n = len(price)
    iv = np.full(n, np.nan)
    status = np.full(n, "NO_SOLVE", dtype=object)
    status[stale] = "STALE_PRICE"
    with np.errstate(invalid="ignore"):
        valid = ~stale & np.isfinite(price) & (price > 0) & np.isfinite(F) & (F > 0) & (K > 0) & (T > 0)
        intrinsic = np.where(is_call, np.maximum(0.0, F - K), np.maximum(0.0, K - F))
        below = valid & (price < intrinsic - 1e-6)
    status[below] = "BELOW_INTRINSIC"
    idx = np.flatnonzero(valid & ~below)
    if idx.size:
        p, f, k, t, c = price[idx], F[idx], K[idx], T[idx], is_call[idx]
        lo = np.full(idx.size, float(bounds[0]))
        hi = np.full(idx.size, float(bounds[1]))
        f_lo = black76_price_vec(f, k, t, lo, c) - p
        f_hi = black76_price_vec(f, k, t, hi, c) - p
        low_side = f_lo > 0                      # price below the value at the lowest volatility
        no_root = ~low_side & (f_hi < 0)         # price above the value at the highest volatility
        ok = ~low_side & ~no_root
        status[idx[low_side]] = "BELOW_INTRINSIC"
        for _ in range(iters):
            mid = 0.5 * (lo + hi)
            above = black76_price_vec(f, k, t, mid, c) - p >= 0     # >=: the smallest root, like the scalar solver
            hi = np.where(above, mid, hi)
            lo = np.where(above, lo, mid)
        iv[idx[ok]] = (0.5 * (lo + hi))[ok]
        status[idx[ok]] = "OK"
    return iv, status


def greeks_vec(F, K, T, sigma, is_call):
    """(delta, gamma, theta per day); NaN where sigma is NaN."""
    F = np.asarray(F, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        sqrt_t = np.sqrt(np.maximum(T, 1e-9))
        d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
        pdf = np.exp(-0.5 * d1 * d1) / SQRT_2PI
        delta = np.where(np.asarray(is_call, dtype=bool), ndtr(d1), ndtr(d1) - 1.0)
        gamma = pdf / (F * sigma * sqrt_t)
        theta = -(F * pdf * sigma) / (2.0 * sqrt_t) / 365.0
    bad = ~np.isfinite(sigma)
    for arr in (delta, gamma, theta):
        arr[bad] = np.nan
    return delta, gamma, theta
