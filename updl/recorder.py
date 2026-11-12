"""Live option-chain Recorder (`python -m updl record`).

Why: Upstox serves expired-contract CANDLES (Upstox Plus) but no historical option-chain snapshot: no past
bid/ask, IV or greeks ("We do not currently offer historical option chain data", Upstox staff 2025-04-21).
The only way to have them later is to save them now, during market hours. This module does that, read-only:

* one GET /v2/option/chain call per underlying and expiry every `every_sec` seconds (default 60);
* nearest expiry from today's instrument master (plus the next one on an expiry day);
* rows appended to <workdir>/recorder/<UNDERLYING>/<YYYY-MM-DD>.csv (flushed and fsync'd every snapshot), so a
  killed run loses at most the snapshot in progress; running the command again simply continues the file;
* columns follow PROMPT_02 6.5 (bid, ask, bid_qty, ask_qty, iv_source, exchange_ts, local_ts) plus the
  greeks Upstox returns. exchange_ts stays empty: the option-chain response carries no exchange timestamp.
Works with the free Analytics Token (option chain is in its supported list). Small: about 3-6 calls a minute.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .client import MASTER_URLS, HttpClient
from .config import Settings
from .errors import ApiError, RetryExhausted, TokenExpired
from .master import listed_contracts, parse_master_bytes, resolve_spot_key
from .sessions import Calendar, instrument_class, load_calendar
from .store import atomic_write_bytes
from .timeutil import IST, at_ist, iso, now_ist

OPTION_CHAIN_PATH = "/v2/option/chain"
RECORD_COLUMNS = ["local_ts", "exchange_ts", "underlying", "expiry_date", "strike", "option_type",
                  "instrument_key", "ltp", "close_price", "volume", "oi", "prev_oi", "bid", "bid_qty", "ask",
                  "ask_qty", "iv", "delta", "gamma", "theta", "vega", "pop", "underlying_spot", "pcr",
                  "iv_source"]


def _masters(settings: Settings, client: HttpClient, today: dt.date) -> dict:
    """Today's instrument masters (cached in raw/D12; downloaded again when the cached copy is from an
    earlier day, because expiries roll)."""
    out = {}
    for exch in ("NSE", "BSE"):
        p = settings.raw_dir / "D12" / f"{exch}.json.gz"
        fresh = p.exists() and dt.datetime.fromtimestamp(p.stat().st_mtime, tz=IST).date() >= today
        if not fresh:
            raw = client.download_file(MASTER_URLS[exch])
            atomic_write_bytes(p, raw, overwrite=True)
        out[exch] = parse_master_bytes(p.read_bytes())
    return out


def expiries_to_record(contracts: list, today: dt.date) -> list:
    """Nearest expiry on or after today; on its expiry day also the next one (the bot shifts to it)."""
    exps = sorted({c["expiry_date"] for c in contracts if c["expiry_date"] >= today})
    if not exps:
        return []
    return exps[:2] if exps[0] == today else exps[:1]


def _num(value):
    if value is None or value == "":
        return ""
    return value


def chain_rows(payload: dict, underlying: str, expiry: dt.date, local_ts: str) -> list:
    """Flatten one /v2/option/chain response into CSV rows (one per strike and side). Missing fields stay empty."""
    rows = []
    for item in payload.get("data") or []:
        strike = item.get("strike_price")
        for side, key in (("CE", "call_options"), ("PE", "put_options")):
            leg = item.get(key) or {}
            md = leg.get("market_data") or {}
            gk = leg.get("option_greeks") or {}
            if not leg:
                continue
            rows.append({
                "local_ts": local_ts, "exchange_ts": "", "underlying": underlying, "expiry_date": iso(expiry),
                "strike": _num(strike), "option_type": side, "instrument_key": leg.get("instrument_key") or "",
                "ltp": _num(md.get("ltp")), "close_price": _num(md.get("close_price")),
                "volume": _num(md.get("volume")), "oi": _num(md.get("oi")), "prev_oi": _num(md.get("prev_oi")),
                "bid": _num(md.get("bid_price")), "bid_qty": _num(md.get("bid_qty")),
                "ask": _num(md.get("ask_price")), "ask_qty": _num(md.get("ask_qty")),
                "iv": _num(gk.get("iv")), "delta": _num(gk.get("delta")), "gamma": _num(gk.get("gamma")),
                "theta": _num(gk.get("theta")), "vega": _num(gk.get("vega")), "pop": _num(gk.get("pop")),
                "underlying_spot": _num(item.get("underlying_spot_price")), "pcr": _num(item.get("pcr")),
                "iv_source": "upstox_option_chain",
            })
    return rows


def append_rows(path: Path, rows: list) -> None:
    """Append rows to a day file; header only for a new file; flush + fsync so a kill loses little."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RECORD_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())


def run_record(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
               underlyings: Optional[list] = None, every_sec: int = 60, until: Optional[str] = None,
               once: bool = False, max_snapshots: Optional[int] = None,
               now_fn: Callable[[], dt.datetime] = now_ist, sleep: Callable[[float], None] = time.sleep,
               log=None) -> dict:
    """Record live option-chain snapshots until the session close (or `until` HH:MM IST, or Ctrl+C)."""
    log = log or (lambda *a, **k: None)
    calendar = calendar or load_calendar(settings.reference_dir)
    known = settings.instrument_names()
    wanted = [str(u).strip().upper() for u in (underlyings or known)]
    unknown = [u for u in wanted if u not in known]
    if unknown:
        print(f"Recorder: yeh naam nahi mile: {', '.join(unknown)}. Sahi naam: {', '.join(known)}")
    unds = [u for u in wanted if u in known]
    if not unds:
        return {"snapshots": 0, "rows": 0, "reason": "NO_UNDERLYING"}
    now = now_fn()
    today = now.date()
    if not once and not calendar.is_trading_day(today):
        print("Aaj market band hai (weekend/holiday). Recorder trading din par market time mein chalao.")
        return {"snapshots": 0, "rows": 0, "reason": "NOT_A_TRADING_DAY"}
    if not once:
        closes = [calendar.session_times(instrument_class("option", u, exchange=settings.exchange_of(u)), today)[1]
                  for u in unds]
        closes = [at_ist(today, c) for c in closes if c]
        if closes and now >= max(closes):
            print("Aaj ka market band ho chuka hai. Recorder kal market time mein chalao.")
            return {"snapshots": 0, "rows": 0, "reason": "AFTER_CLOSE"}

    masters = _masters(settings, client, today)
    plan = []
    for und in unds:
        exch = settings.exchange_of(und)
        sk, _ = resolve_spot_key(settings.spot_key(und), masters.get(exch, []))
        exps = expiries_to_record(listed_contracts(masters.get(exch, []), sk, kinds=("CE", "PE")), today)
        for e in exps:
            plan.append((und, sk, e))
    if not plan:
        print("Recorder: master mein koi chalu expiry nahi mili.")
        return {"snapshots": 0, "rows": 0, "reason": "NO_EXPIRY"}

    # session window: the latest F&O close among the chosen underlyings (from sessions.yaml)
    opens, closes = [], []
    for und in unds:
        o, c = calendar.session_times(instrument_class("option", und, exchange=settings.exchange_of(und)), today)
        if o and c:
            opens.append(at_ist(today, o))
            closes.append(at_ist(today, c))
    start_t = min(opens) if opens else at_ist(today, "09:15")
    stop_t = max(closes) if closes else at_ist(today, "15:30")
    if until:
        stop_t = min(stop_t, at_ist(today, until)) if not once else at_ist(today, until)

    out_dir = settings.workdir / "recorder"
    snapshots, total_rows, errors = 0, 0, 0
    print(f"Recorder chalu: {', '.join(f'{u} {iso(e)}' for u, _, e in plan)} | har {every_sec} sec | "
          f"band hoga {stop_t.strftime('%H:%M')} par. Rokne ke liye Ctrl+C.")
    try:
        while True:
            now = now_fn()
            if not once:
                if now >= stop_t:
                    break
                if now < start_t:
                    sleep(min(60.0, (start_t - now).total_seconds()))
                    continue
            local_ts = now.astimezone(IST).replace(microsecond=0).isoformat()
            for und, sk, exp in plan:
                try:
                    res = client.get(OPTION_CHAIN_PATH, {"instrument_key": sk, "expiry_date": iso(exp)}, kind="v3")
                except (ApiError, RetryExhausted) as err:
                    errors += 1
                    log("record_error", underlying=und, expiry=iso(exp), error=str(err)[:120])
                    continue
                rows = chain_rows(res.json, und, exp, local_ts)
                if rows:
                    append_rows(out_dir / und / f"{iso(today)}.csv", rows)
                    total_rows += len(rows)
            snapshots += 1
            if once or (max_snapshots is not None and snapshots >= max_snapshots):
                break
            sleep(float(every_sec))
    except TokenExpired:
        print(f"Recorder ruk gaya: token problem. {snapshots} snapshots save hue. Token theek karke dobara chalao.")
        raise
    except KeyboardInterrupt:
        print("Ctrl+C: Recorder ruk gaya. Jo save hua woh safe hai.")
    print(f"Recorder: {snapshots} snapshots, {total_rows} lines -> {out_dir}")
    return {"snapshots": snapshots, "rows": total_rows, "errors": errors, "out_dir": str(out_dir)}
