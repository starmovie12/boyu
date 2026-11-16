"""`python -m updl spread-report`: real bid/ask spreads from the Recorder's files (the only bid/ask Upstox gives;
there is no bid/ask history for days that were not recorded).

reports/spread_report.csv, one row per (trading_date, underlying, days_to_expiry, moneyness, option_type, hour):
quotes, quotes_without_bid_ask, median and p90 spread in rupees, median and p90 spread in % of the mid price,
median bid_qty and ask_qty. Moneyness = |strike - spot| / spot: ATM <= 0.5%, NEAR <= 2%, FAR <= 5%, DEEP > 5%.
A backtest's slippage assumption can be checked against these numbers; they describe quotes, not fills.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Settings
from .timeutil import IST, iso, to_date

READ_COLUMNS = ["local_ts", "underlying", "expiry_date", "strike", "option_type", "bid", "ask", "bid_qty", "ask_qty",
                "underlying_spot"]
FIELDS = ["trading_date", "underlying", "days_to_expiry", "moneyness", "option_type", "hour", "quotes",
          "quotes_without_bid_ask", "median_spread", "p90_spread", "median_spread_pct", "p90_spread_pct",
          "median_bid_qty", "median_ask_qty"]
BUCKETS = ((0.005, "ATM"), (0.02, "NEAR"), (0.05, "FAR"))


def _moneyness(strike: pd.Series, spot: pd.Series) -> pd.Series:
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = (strike - spot).abs() / spot
    out = pd.Series("DEEP", index=strike.index, dtype=object)
    for limit, name in reversed(BUCKETS):
        out[dist <= limit] = name
    out[~np.isfinite(dist)] = "UNKNOWN_SPOT"
    return out


def day_spreads(path: Path) -> pd.DataFrame:
    """Aggregate one recorder file (recorder/<INDEX>/<YYYY-MM-DD>.csv)."""
    head = pd.read_csv(path, nrows=0)
    df = pd.read_csv(path, usecols=[c for c in READ_COLUMNS if c in head.columns], low_memory=False)
    if df.empty:
        return pd.DataFrame(columns=FIELDS)
    for col in ("strike", "bid", "ask", "bid_qty", "ask_qty", "underlying_spot"):
        df[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else np.nan
    ts = pd.to_datetime(df["local_ts"], utc=True, errors="coerce").dt.tz_convert(IST)
    day = to_date(path.stem)
    df["trading_date"] = iso(day)
    df["hour"] = ts.dt.strftime("%H")
    exp = pd.to_datetime(df["expiry_date"], errors="coerce")
    df["days_to_expiry"] = (exp - pd.Timestamp(day)).dt.days.astype("Int64")
    df["moneyness"] = _moneyness(df["strike"], df["underlying_spot"])
    good = (df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])
    df["spread"] = np.where(good, df["ask"] - df["bid"], np.nan)
    mid = (df["ask"] + df["bid"]) / 2.0
    df["spread_pct"] = np.where(good, df["spread"] / mid * 100.0, np.nan)
    df["no_quote"] = ~good
    keys = ["trading_date", "underlying", "days_to_expiry", "moneyness", "option_type", "hour"]
    g = df.groupby(keys, dropna=False, sort=True)
    out = pd.DataFrame({
        "quotes": g.size(), "quotes_without_bid_ask": g["no_quote"].sum(),
        "median_spread": g["spread"].median(), "p90_spread": g["spread"].quantile(0.9),
        "median_spread_pct": g["spread_pct"].median(), "p90_spread_pct": g["spread_pct"].quantile(0.9),
        "median_bid_qty": g["bid_qty"].median(), "median_ask_qty": g["ask_qty"].median(),
    }).reset_index()
    for col in ("median_spread", "p90_spread", "median_spread_pct", "p90_spread_pct"):
        out[col] = out[col].round(4)
    return out[FIELDS]


def recorder_days(settings: Settings, start=None, end=None) -> list:
    root = Path(settings.workdir) / "recorder"
    files = sorted(root.glob("*/????-??-??.csv")) if root.exists() else []
    lo, hi = (to_date(start) if start else None), (to_date(end) if end else None)
    return [f for f in files if (lo is None or to_date(f.stem) >= lo) and (hi is None or to_date(f.stem) <= hi)]


def run_spread_report(settings: Settings, start=None, end=None, say=print) -> dict:
    files = recorder_days(settings, start, end)
    path = Path(settings.reports_dir) / "spread_report.csv"
    if not files:
        say("Spread report: Recorder ki koi file nahi (market time mein: bash termux_run.sh record).")
        return {"days": 0, "rows": 0, "path": None}
    frames = [day_spreads(f) for f in files]
    new = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) else \
        pd.DataFrame(columns=FIELDS)
    touched = {(f.stem, f.parent.name) for f in files}
    keep = []
    if path.exists():
        with open(path, "r", encoding="utf-8", newline="") as fh:
            keep = [r for r in csv.DictReader(fh) if (r.get("trading_date"), r.get("underlying")) not in touched]
    merged = pd.concat([pd.DataFrame(keep, columns=FIELDS), new], ignore_index=True) if keep else new
    merged = merged.sort_values(["trading_date", "underlying", "days_to_expiry", "moneyness", "option_type", "hour"],
                                kind="stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    merged.to_csv(tmp, index=False)
    os.replace(tmp, path)
    for und, g in new.groupby("underlying"):
        atm = g[g["moneyness"] == "ATM"]["median_spread_pct"]
        far = g[g["moneyness"].isin(["FAR", "DEEP"])]["median_spread_pct"]
        say(f"Spread {und}: ATM median {atm.median():.2f}% of mid, FAR/DEEP median {far.median():.2f}% "
            f"({g['trading_date'].nunique()} din)")
    say(f"Spread report: {len(files)} Recorder file(s) -> {path}")
    return {"days": len(files), "rows": len(new), "path": str(path)}

