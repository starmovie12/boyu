"""Instrument-master helpers (files are TODAY's snapshot, never point-in-time truth)."""
from __future__ import annotations

import datetime as dt
import gzip
import json
from typing import Optional

from .errors import AmbiguousKey, UpdlError
from .messages import KEY_AMBIGUOUS
from .timeutil import ms_to_ist_date

RESOLVE_PREFIX = "RESOLVE_FROM_MASTER:"
WEEKDAY_HINTS = {"weekly_tuesday": 1, "weekly_thursday": 3, "monthly_last_tuesday": 1}


def parse_master_bytes(data: bytes) -> list:
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    rows = json.loads(raw.decode("utf-8"))
    if not isinstance(rows, list):
        raise UpdlError("master file is not a list")
    return rows


def _norm(text) -> str:
    return str(text or "").strip().lower()


def index_candidates(rows: list, segment: str, name: str) -> list:
    out = {}
    for r in rows:
        if r.get("segment") != segment:
            continue
        if _norm(r.get("name")) == _norm(name) or _norm(r.get("trading_symbol")) == _norm(name):
            out[r.get("instrument_key")] = {"instrument_key": r.get("instrument_key"), "name": r.get("name"),
                                            "trading_symbol": r.get("trading_symbol"), "segment": segment}
    return list(out.values())


def resolve_spot_key(spot_key: str, rows: Optional[list]):
    """Return (key, candidates). Direct keys pass through. A RESOLVE_FROM_MASTER:<segment>:<name> key is looked
    up by exact name; ambiguity raises AmbiguousKey (the caller prints candidates and exits 60)."""
    if not spot_key.startswith(RESOLVE_PREFIX):
        return spot_key, []
    _, segment, name = spot_key.split(":", 2)
    cands = index_candidates(rows or [], segment, name)
    if len(cands) == 1:
        return cands[0]["instrument_key"], cands
    err = AmbiguousKey(KEY_AMBIGUOUS)
    err.candidates = cands
    raise err


def expiry_date_of(row: dict) -> Optional[dt.date]:
    ms = row.get("expiry")
    if ms in (None, "", 0):
        return None
    return ms_to_ist_date(int(ms))


def weekday_mismatch(expiry: dt.date, rule: str) -> Optional[str]:
    want = WEEKDAY_HINTS.get(rule)
    if want is None or expiry.weekday() == want:
        return None
    return f"expiry {expiry} is weekday {expiry.weekday()} but rule {rule} expects {want}"


def listed_contracts(rows: list, underlying_key: str, kinds=("CE", "PE", "FUT"), logger=None, rule=None) -> list:
    """Contracts in today's master whose underlying_key matches. expiry ms -> IST date.
    A weekday mismatch is logged (never corrected). Monthly rules are only checked on weekday, not on last-ness."""
    out = []
    for r in rows:
        if r.get("underlying_key") != underlying_key or r.get("instrument_type") not in kinds:
            continue
        exp = expiry_date_of(r)
        if exp is None:
            continue
        if rule and logger:
            msg = weekday_mismatch(exp, rule)
            if msg:
                logger("weekday_mismatch", detail=msg)
        out.append({"instrument_key": r["instrument_key"], "expiry_date": exp,
                    "strike": r.get("strike_price") if r.get("instrument_type") != "FUT" else None,
                    "option_type": r["instrument_type"], "trading_symbol": r.get("trading_symbol"),
                    "lot_size": r.get("lot_size"), "freeze_quantity": r.get("freeze_quantity"),
                    "tick_size": r.get("tick_size"), "exchange_token": r.get("exchange_token")})
    return out


def equity_key(rows: list, symbol: str) -> Optional[str]:
    for r in rows:
        if str(r.get("segment", "")).endswith("_EQ") and r.get("instrument_type") == "EQ" \
                and _norm(r.get("trading_symbol")) == _norm(symbol):
            return r.get("instrument_key")
    return None


def fo_underlying_symbols(rows: list) -> set:
    return {r.get("underlying_symbol") for r in rows if r.get("segment") == "NSE_FO" and r.get("underlying_symbol")}


def find_global(rows: list, needle: str) -> Optional[str]:
    n = _norm(needle)
    for r in rows:
        if n in (_norm(r.get("trading_symbol")), _norm(r.get("name")), _norm(r.get("short_name"))):
            return r.get("instrument_key")
    for r in rows:
        if n in _norm(r.get("trading_symbol")) or n in _norm(r.get("name")):
            return r.get("instrument_key")
    return None


def search_global(rows: list) -> list:
    hits = []
    for r in rows:
        text = f"{r.get('name', '')} {r.get('trading_symbol', '')}".lower()
        if "yield" in text or "bond" in text or "10y" in text.replace(" ", "") or "10 y" in text:
            hits.append(r.get("instrument_key"))
    return hits[:5]
