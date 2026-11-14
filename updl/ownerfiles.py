"""Schemas and validators for the files only the owner can supply (Upstox does not serve them).

Each file is checked before `ingest` copies it into external/. A file with errors is NOT copied: the owner gets a
Hinglish list of the problems with row numbers. Nothing is guessed or filled in. Header-only templates live in
reference/owner_templates/.
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd

ISIN_RX = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
RATIO_RX = re.compile(r"^\d+(\.\d+)?:\d+(\.\d+)?$")
EVENT_CLASSES = {"RBI", "FED", "BUDGET", "CPI", "GDP", "JOBS", "RESULTS", "EXPIRY", "ELECTION", "OTHER"}
CA_TYPES = {"SPLIT", "BONUS", "DIVIDEND", "RIGHTS", "MERGER", "DEMERGER", "OTHER"}
CLIENT_TYPES = {"CLIENT", "DII", "FII", "PRO", "TOTAL"}
MAX_ERRORS_SHOWN = 15

SCHEMAS = {
    "events.csv": {"required": ["event_id", "event_ts_ist", "name", "class", "source_url"],
                   "optional": ["importance", "published_ts"]},
    "constituents.csv": {"required": ["index", "symbol", "isin", "weight", "valid_from", "valid_to"], "optional": []},
    "corporate_actions.csv": {"required": ["symbol", "ex_date", "type", "ratio", "source_url"],
                              "optional": ["isin", "amount"]},
    "participant_oi.csv": {"required": ["date", "client_type", "long", "short"], "optional": []},
}


def _ts(value):
    """IST timestamp with an explicit +05:30 offset, e.g. 2026-12-05T10:00:00+05:30."""
    t = dt.datetime.fromisoformat(str(value).strip())
    if t.utcoffset() != dt.timedelta(hours=5, minutes=30):
        raise ValueError("offset must be +05:30")
    return t


def _date(value) -> dt.date:
    return dt.date.fromisoformat(str(value).strip()[:10])


def validate(name: str, df: pd.DataFrame, now: dt.datetime = None) -> list:
    """Return a list of error strings (empty = valid)."""
    schema = SCHEMAS[name]
    missing = [c for c in schema["required"] if c not in df.columns]
    if missing:
        return [f"columns missing: {', '.join(missing)} (template: reference/owner_templates/{name})"]
    if df.empty:
        return ["file has no rows"]
    errors = []
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))

    def err(i, text):
        errors.append(f"row {i + 2}: {text}")      # +2: header line and 1-based numbering, as in a spreadsheet

    optional_empty = {"events.csv": {"importance", "published_ts"}, "constituents.csv": {"valid_to"},
                      "corporate_actions.csv": {"ratio"}}.get(name, set())
    for i, r in df.iterrows():
        for col in schema["required"]:
            if col not in optional_empty and (pd.isna(r[col]) or not str(r[col]).strip()):
                err(i, f"{col} khali hai")

    if name == "events.csv":
        prev = None
        for i, r in df.iterrows():
            try:
                t = _ts(r["event_ts_ist"])
                if prev is not None and t < prev:
                    err(i, "events are not sorted by event_ts_ist")
                prev = t
            except (ValueError, TypeError):
                err(i, f"event_ts_ist '{r['event_ts_ist']}' is not like 2026-12-05T10:00:00+05:30")
            if str(r["class"]).strip().upper() not in EVENT_CLASSES:
                err(i, f"class '{r['class']}' not one of {sorted(EVENT_CLASSES)}")
            if not str(r["source_url"]).startswith(("http://", "https://")):
                err(i, "source_url must be the official page (http...)")
            if "published_ts" in df.columns and pd.notna(r.get("published_ts")) and str(r["published_ts"]).strip():
                try:
                    if _ts(r["published_ts"]) > now:
                        err(i, "published_ts is in the future")
                except (ValueError, TypeError):
                    err(i, "published_ts is not an IST timestamp")
        if df["event_id"].duplicated().any():
            errors.append("event_id values repeat")

    elif name == "constituents.csv":
        for i, r in df.iterrows():
            if not ISIN_RX.match(str(r["isin"]).strip()):
                err(i, f"isin '{r['isin']}' is not a 12-character Indian ISIN")
            try:
                w = float(r["weight"])
                if not 0 < w <= 100:
                    err(i, "weight must be a percent between 0 and 100")
            except (ValueError, TypeError):
                err(i, "weight is not a number")
            try:
                f = _date(r["valid_from"])
                if pd.notna(r["valid_to"]) and str(r["valid_to"]).strip() and _date(r["valid_to"]) < f:
                    err(i, "valid_to is before valid_from")
            except ValueError:
                err(i, "valid_from / valid_to must be YYYY-MM-DD (valid_to may be empty = still in the index)")

    elif name == "corporate_actions.csv":
        for i, r in df.iterrows():
            try:
                _date(r["ex_date"])
            except ValueError:
                err(i, "ex_date must be YYYY-MM-DD")
            kind = str(r["type"]).strip().upper()
            if kind not in CA_TYPES:
                err(i, f"type '{r['type']}' not one of {sorted(CA_TYPES)}")
            if kind in ("SPLIT", "BONUS") and not RATIO_RX.match(str(r["ratio"]).strip()):
                err(i, "ratio for SPLIT/BONUS must look like 1:1 or 5:1 (new:old)")
            if not str(r["source_url"]).startswith(("http://", "https://")):
                err(i, "source_url must be the official page (http...)")

    elif name == "participant_oi.csv":
        for i, r in df.iterrows():
            try:
                _date(r["date"])
            except ValueError:
                err(i, "date must be YYYY-MM-DD")
            if str(r["client_type"]).strip().upper() not in CLIENT_TYPES:
                err(i, f"client_type '{r['client_type']}' not one of {sorted(CLIENT_TYPES)}")
            for col in ("long", "short"):
                try:
                    if float(r[col]) < 0:
                        err(i, f"{col} is negative")
                except (ValueError, TypeError):
                    err(i, f"{col} is not a number")
    return errors


def report(name: str, errors: list) -> str:
    shown = errors[:MAX_ERRORS_SHOWN]
    more = f"\n  ... aur {len(errors) - len(shown)} galtiyan" if len(errors) > len(shown) else ""
    return (f"{name} mein {len(errors)} galti mili, file copy NAHI ki. Theek karke dobara `ingest` chalao:\n  "
            + "\n  ".join(shown) + more)
