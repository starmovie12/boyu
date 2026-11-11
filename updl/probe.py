"""Stage 0 Probe (`python -m updl probe`).

Runs P01..P21 within a strict budget of at most 40 API calls (including retries) and 3 master-file downloads.
Selects Plan A, A_PARTIAL, B or C and writes probe_report.json and probe_report.txt with zero secrets.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from .client import MASTER_URLS, HttpClient, expired_candle_path, v3_candle_path
from .config import Settings
from .errors import (AmbiguousKey, ApiError, BudgetExceeded, PlusRequired, ProbeMissing, RetryExhausted,
                     RightsNotAllowed)
from .master import (equity_key, find_global, listed_contracts, parse_master_bytes, resolve_spot_key,
                     search_global)
from .messages import (DATA_RIGHTS_WARNING, PLAN_A_PARTIAL, PLAN_B, PLAN_C, PROBE_MISSING,
                       RIGHTS_NOT_ALLOWED)
from .secretscan import scan_file
from .sessions import Calendar, load_calendar
from .store import atomic_write_bytes, parse_candles
from .timeutil import has_ist_offset, iso, today_ist, to_date, utc_iso

PROBE_BUDGET = 40
KEY_FORMAT_PARTS = 3


def _sample_candles(rows: list) -> list:
    out = []
    for r in rows[:2]:
        out.append({
            "ts": r["ts"],
            "o": round(float(r["o"]), 2),
            "h": round(float(r["h"]), 2),
            "l": round(float(r["l"]), 2),
            "c": round(float(r["c"]), 2),
            "v": r["v"],
            "oi": r["oi"],
        })
    return out


def _nearest_date(dates: list, target: dt.date) -> Optional[dt.date]:
    if not dates:
        return None
    return min(dates, key=lambda d: abs((to_date(d) - target).days))


def _check_expired_key_format(key: str) -> bool:
    parts = str(key).split("|")
    if len(parts) != KEY_FORMAT_PARTS:
        return False
    seg, tok, exp = parts
    if seg not in ("NSE_FO", "BSE_FO") or not tok:
        return False
    try:
        dt.datetime.strptime(exp, "%d-%m-%Y")
        return True
    except ValueError:
        return False


def check_probe_fresh(settings: Settings, secrets=()) -> dict:
    """Enforce Section 4: download is refused unless probe_report.json exists, is <7 days old, and holds no secrets."""
    p = settings.probe_json
    if not p.exists():
        raise ProbeMissing(PROBE_MISSING)
    hits = scan_file(p, secrets)
    if hits:
        raise ProbeMissing(PROBE_MISSING)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        created = dt.datetime.fromisoformat(str(data["generated_utc"]).replace("Z", "+00:00"))
        age_days = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 86400.0
        if age_days > 7.0:
            raise ProbeMissing(PROBE_MISSING)
        return data
    except ProbeMissing:
        raise
    except Exception as exc:
        raise ProbeMissing(PROBE_MISSING) from exc


def run_probe(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
              probe_today: Optional[dt.date] = None) -> dict:
    if settings.data_rights == "NOT_ALLOWED":
        print(RIGHTS_NOT_ALLOWED)
        raise RightsNotAllowed(RIGHTS_NOT_ALLOWED)
    if settings.data_rights == "UNVERIFIED":
        print(DATA_RIGHTS_WARNING)

    settings.ensure_dirs()
    calendar = calendar or load_calendar(settings.reference_dir)
    client.budget = PROBE_BUDGET
    today = to_date(probe_today) if probe_today else today_ist()
    recent_day = calendar.last_completed_trading_day(today)

    tests = {}
    masters = {}
    resolved_keys = {}
    plus_required = False
    expired_1m_max_span_days = 0
    option_history_oldest_date = None
    sensex_option_history_available = False
    analytics_accepted_for_expired = False
    v3_1m_max_span_days = 28
    oi_intraday_varies = False
    oi_last_bar_equals_daily = False

    def rec(pid: str, status: str, http_status: int = 0, **info):
        tests[pid] = {"status": status, "http_status": http_status, **info}

    # ---- P01: Instrument masters NSE, BSE, global --------------------------
    try:
        file_info = {}
        for exch, url in MASTER_URLS.items():
            raw = client.download_file(url)
            rows = parse_master_bytes(raw)
            masters[exch] = rows
            mpath = settings.raw_dir / "D12" / f"{exch}.json.gz"
            mpath.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(mpath, raw, overwrite=True)
            file_info[exch] = {"date": iso(today), "rows": len(rows)}

        candidates_map = {}
        for und in settings.instrument_names():
            exch = settings.exchange_of(und)
            sk = settings.spot_key(und)
            key, cands = resolve_spot_key(sk, masters.get(exch, []))
            resolved_keys[und] = key
            candidates_map[und] = cands

        nifty_opts = listed_contracts(masters.get("NSE", []), resolved_keys.get("NIFTY", "NSE_INDEX|Nifty 50"),
                                      kinds=("CE", "PE"))
        sample_opt = nifty_opts[0] if nifty_opts else {}
        rec("P01", "PASS", 200, files=file_info, resolved_keys=resolved_keys,
            candidates=candidates_map,
            nifty_option_spec={
                "lot_size": sample_opt.get("lot_size"),
                "tick_size": sample_opt.get("tick_size"),
                "freeze_quantity": sample_opt.get("freeze_quantity"),
            })
    except AmbiguousKey as err:
        rec("P01", "FAIL", 0, error="AMBIGUOUS_SPOT_KEY", candidates=getattr(err, "candidates", []))
        _write_reports(settings, today, recent_day, "C", tests, client, None, 0, False, False, 28, False, False)
        raise
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P01", "FAIL", getattr(err, "status", 0), error=str(err))
        for und in settings.instrument_names():
            sk = settings.spot_key(und)
            resolved_keys[und] = sk.split(":")[-1] if sk.startswith("RESOLVE_FROM_MASTER:") else sk

    nifty_key = resolved_keys.get("NIFTY", "NSE_INDEX|Nifty 50")
    sensex_key = resolved_keys.get("SENSEX", "BSE_INDEX|SENSEX")
    vix_key = settings.spot_key("INDIAVIX") if "INDIAVIX" in settings.instruments else "NSE_INDEX|India VIX"

    # ---- P02: V3 1-minute Nifty spot, recent day ---------------------------
    p02_rows = []
    try:
        res = client.get(v3_candle_path(nifty_key, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
        p02_rows, _ = parse_candles(res.json)
        if not p02_rows and calendar.covers_to and recent_day > calendar.covers_to:
            for back in range(1, 7):
                cand_d = recent_day - dt.timedelta(days=back)
                if cand_d.weekday() < 5:
                    res = client.get(v3_candle_path(nifty_key, "minutes", 1, iso(cand_d), iso(cand_d)), kind="v3")
                    p02_rows, _ = parse_candles(res.json)
                    if p02_rows:
                        recent_day = cand_d
                        break
        if p02_rows:
            first_ts = p02_rows[0]["ts"]
            last_ts = p02_rows[-1]["ts"]
            rec("P02", "PASS", res.status, bar_count=len(p02_rows), first_ts_open=first_ts,
                last_ts_open=last_ts, stamp_convention="start_time",
                has_ist_offset=has_ist_offset(first_ts), sample=_sample_candles(p02_rows))
        else:
            rec("P02", "FAIL", res.status, bar_count=0)
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P02", "FAIL", getattr(err, "status", 0), error=str(err))

    # ---- P03: V3 1-minute India VIX, recent day ----------------------------
    try:
        res = client.get(v3_candle_path(vix_key, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
        rows, _ = parse_candles(res.json)
        rec("P03", "PASS" if rows else "FAIL", res.status, bar_count=len(rows), sample=_sample_candles(rows))
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P03", "FAIL", getattr(err, "status", 0), error=str(err))

    # ---- P04: V3 daily Nifty, 5 days ---------------------------------------
    try:
        d_from = recent_day - dt.timedelta(days=7)
        res = client.get(v3_candle_path(nifty_key, "days", 1, iso(recent_day), iso(d_from)), kind="v3")
        rows, _ = parse_candles(res.json)
        rec("P04", "PASS" if rows else "FAIL", res.status, bar_count=len(rows), sample=_sample_candles(rows))
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P04", "FAIL", getattr(err, "status", 0), error=str(err))

    # ---- P05: V3 1-minute Nifty spans of 28, 31, 32, 62 days ---------------
    span_results = {}
    for sp in (28, 31, 32, 62):
        d_from = recent_day - dt.timedelta(days=sp - 1)
        try:
            res = client.get(v3_candle_path(nifty_key, "minutes", 1, iso(recent_day), iso(d_from)), kind="v3")
            rows, _ = parse_candles(res.json)
            if rows:
                span_results[str(sp)] = "PASS"
                v3_1m_max_span_days = max(v3_1m_max_span_days, sp)
            else:
                span_results[str(sp)] = "EMPTY"
        except ApiError as err:
            span_results[str(sp)] = f"FAIL_{err.status}"
        except (RetryExhausted, BudgetExceeded) as err:
            span_results[str(sp)] = f"ERR_{type(err).__name__}"
    rec("P05", "PASS" if span_results.get("28") == "PASS" else "FAIL", 200,
        spans=span_results, v3_1m_max_span_days=v3_1m_max_span_days)

    # ---- P06: V3 3-minute and 15-minute Nifty, 28 days ---------------------
    try:
        d_from = recent_day - dt.timedelta(days=27)
        r3 = client.get(v3_candle_path(nifty_key, "minutes", 3, iso(recent_day), iso(d_from)), kind="v3")
        r15 = client.get(v3_candle_path(nifty_key, "minutes", 15, iso(recent_day), iso(d_from)), kind="v3")
        rows3, _ = parse_candles(r3.json)
        rows15, _ = parse_candles(r15.json)
        rec("P06", "PASS" if (rows3 and rows15) else "FAIL", r15.status,
            bars_3m=len(rows3), bars_15m=len(rows15))
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P06", "FAIL", getattr(err, "status", 0), error=str(err))

    # ---- P07: current-month Nifty future, 1-minute, 1 day ------------------
    futs = sorted(listed_contracts(masters.get("NSE", []), nifty_key, kinds=("FUT",)),
                  key=lambda c: c["expiry_date"])
    if futs:
        try:
            fkey = futs[0]["instrument_key"]
            res = client.get(v3_candle_path(fkey, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
            rows, _ = parse_candles(res.json)
            rec("P07", "PASS" if rows else "FAIL", res.status, instrument_key=fkey,
                bar_count=len(rows), sample=_sample_candles(rows))
        except (ApiError, RetryExhausted, BudgetExceeded) as err:
            rec("P07", "FAIL", getattr(err, "status", 0), error=str(err))
    else:
        rec("P07", "FAIL", 0, error="no_listed_future")

    # ---- P08: active Nifty option near the money, 1-minute, 1 day ----------
    p08_passed = False
    p08_rows = []
    opts = listed_contracts(masters.get("NSE", []), nifty_key, kinds=("CE", "PE"))
    spot_ref = p02_rows[-1]["c"] if p02_rows else 25000.0
    if opts:
        nearest_exp = min(c["expiry_date"] for c in opts)
        exp_opts = [c for c in opts if c["expiry_date"] == nearest_exp and c.get("strike") is not None]
        atm_opt = min(exp_opts, key=lambda c: abs(float(c["strike"]) - spot_ref)) if exp_opts else opts[0]
        try:
            okey = atm_opt["instrument_key"]
            res = client.get(v3_candle_path(okey, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
            p08_rows, _ = parse_candles(res.json)
            p08_passed = len(p08_rows) > 0
            rec("P08", "PASS" if p08_passed else "FAIL", res.status, instrument_key=okey,
                bar_count=len(p08_rows), sample=_sample_candles(p08_rows))
        except (ApiError, RetryExhausted, BudgetExceeded) as err:
            rec("P08", "FAIL", getattr(err, "status", 0), error=str(err))
    else:
        rec("P08", "FAIL", 0, error="no_listed_option")

    # ---- P09: expired Get Expiries for Nifty -------------------------------
    nifty_expiries = []
    try:
        res = client.get("/v2/expired-instruments/expiries", {"instrument_key": nifty_key}, kind="expired")
        raw_exps = res.json.get("data") if isinstance(res.json.get("data"), list) else []
        nifty_expiries = sorted({to_date(x) for x in raw_exps if x})
        if nifty_expiries:
            rec("P09", "PASS", res.status, count=len(nifty_expiries),
                oldest_expiry=iso(nifty_expiries[0]), newest_expiry=iso(nifty_expiries[-1]))
        else:
            rec("P09", "FAIL", res.status, count=0)
    except PlusRequired as err:
        plus_required = True
        rec("P09", "FAIL", err.status, reason="PLUS_REQUIRED", code=err.code)
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P09", "FAIL", getattr(err, "status", 0), error=str(err))

    # ---- P10..P16: Expired contract tests ----------------------------------
    p11_passed = False
    p11_rows = []
    p13_rows = []
    target_exp = None
    sample_exp_contract = None

    if plus_required or not nifty_expiries:
        reason = "PLUS_REQUIRED" if plus_required else "NO_EXPIRIES"
        for pid in ("P10", "P11", "P12", "P13", "P14", "P15", "P16"):
            rec(pid, "UNVERIFIED", 0, reason=reason)
    else:
        target_exp = _nearest_date(nifty_expiries, recent_day - dt.timedelta(days=140))
        try:
            res = client.get("/v2/expired-instruments/option/contract",
                             {"instrument_key": nifty_key, "expiry_date": iso(target_exp)}, kind="expired")
            contracts = res.json.get("data") or []
            if contracts:
                sample_exp_contract = min(
                    contracts,
                    key=lambda c: abs(float(c.get("strike_price") or c.get("strike") or 0) - spot_ref)
                )
                ekey = sample_exp_contract.get("instrument_key") or sample_exp_contract.get("expired_instrument_key")
                fmt_ok = _check_expired_key_format(ekey)
                rec("P10", "PASS" if fmt_ok else "FAIL", res.status, expiry=iso(target_exp),
                    count=len(contracts), sample_key=ekey, key_format_ok=fmt_ok)
            else:
                rec("P10", "FAIL", res.status, expiry=iso(target_exp), count=0)
        except (ApiError, RetryExhausted, BudgetExceeded) as err:
            rec("P10", "FAIL", getattr(err, "status", 0), error=str(err))

        if sample_exp_contract:
            ekey = sample_exp_contract.get("instrument_key") or sample_exp_contract.get("expired_instrument_key")
            last_td = target_exp if calendar.is_trading_day(target_exp) else calendar.prev_trading_day(target_exp)
            try:
                res = client.get(expired_candle_path(ekey, "1minute", iso(last_td), iso(last_td)), kind="expired")
                p11_rows, _ = parse_candles(res.json)
                has_vol_oi = any(r["v"] is not None and r["oi"] is not None for r in p11_rows)
                p11_passed = len(p11_rows) > 0 and has_vol_oi
                if p11_passed:
                    expired_1m_max_span_days = 1
                rec("P11", "PASS" if p11_passed else "FAIL", res.status,
                    bar_count=len(p11_rows), has_vol_oi=has_vol_oi, sample=_sample_candles(p11_rows))
            except (ApiError, RetryExhausted, BudgetExceeded) as err:
                rec("P11", "FAIL", getattr(err, "status", 0), error=str(err))

            p12_spans = {}
            for sp in (5, 28):
                d_from = last_td - dt.timedelta(days=sp - 1)
                try:
                    res = client.get(expired_candle_path(ekey, "1minute", iso(last_td), iso(d_from)), kind="expired")
                    rows, _ = parse_candles(res.json)
                    if rows:
                        p12_spans[str(sp)] = "PASS"
                        expired_1m_max_span_days = max(expired_1m_max_span_days, sp)
                    else:
                        p12_spans[str(sp)] = "EMPTY"
                except ApiError as err:
                    p12_spans[str(sp)] = f"FAIL_{err.status}"
                except (RetryExhausted, BudgetExceeded) as err:
                    p12_spans[str(sp)] = f"ERR_{type(err).__name__}"
            rec("P12", "PASS" if expired_1m_max_span_days >= 1 else "FAIL", 200,
                spans=p12_spans, expired_1m_max_span_days=expired_1m_max_span_days)

            try:
                d_from = last_td - dt.timedelta(days=29)
                res = client.get(expired_candle_path(ekey, "day", iso(last_td), iso(d_from)), kind="expired")
                p13_rows, _ = parse_candles(res.json)
                rec("P13", "PASS" if p13_rows else "FAIL", res.status,
                    bar_count=len(p13_rows), sample=_sample_candles(p13_rows))
            except (ApiError, RetryExhausted, BudgetExceeded) as err:
                rec("P13", "FAIL", getattr(err, "status", 0), error=str(err))
        else:
            for pid in ("P11", "P12", "P13"):
                rec(pid, "FAIL", 0, reason="NO_CONTRACT_FROM_P10")

        try:
            res_f = client.get("/v2/expired-instruments/future/contract",
                               {"instrument_key": nifty_key, "expiry_date": iso(target_exp)}, kind="expired")
            f_list = res_f.json.get("data") or []
            if f_list:
                fk = f_list[0].get("instrument_key") or f_list[0].get("expired_instrument_key")
                last_td = target_exp if calendar.is_trading_day(target_exp) else calendar.prev_trading_day(target_exp)
                res_fc = client.get(expired_candle_path(fk, "1minute", iso(last_td), iso(last_td)), kind="expired")
                f_rows, _ = parse_candles(res_fc.json)
                rec("P14", "PASS" if f_rows else "FAIL", res_fc.status,
                    future_key=fk, bar_count=len(f_rows), sample=_sample_candles(f_rows))
            else:
                rec("P14", "FAIL", res_f.status, count=0)
        except (ApiError, RetryExhausted, BudgetExceeded) as err:
            rec("P14", "FAIL", getattr(err, "status", 0), error=str(err))

        try:
            res_bse = client.get("/v2/expired-instruments/expiries", {"instrument_key": sensex_key}, kind="expired")
            bse_exps = sorted({to_date(x) for x in (res_bse.json.get("data") if isinstance(res_bse.json.get("data"), list) else []) if x})
            if bse_exps:
                b_exp = _nearest_date(bse_exps, recent_day - dt.timedelta(days=60))
                res_bc = client.get("/v2/expired-instruments/option/contract",
                                    {"instrument_key": sensex_key, "expiry_date": iso(b_exp)}, kind="expired")
                b_contracts = res_bc.json.get("data") or []
                if b_contracts:
                    bk = b_contracts[0].get("instrument_key") or b_contracts[0].get("expired_instrument_key")
                    b_td = b_exp if calendar.is_trading_day(b_exp) else calendar.prev_trading_day(b_exp)
                    res_bm = client.get(expired_candle_path(bk, "1minute", iso(b_td), iso(b_td)), kind="expired")
                    b_rows, _ = parse_candles(res_bm.json)
                    sensex_option_history_available = len(b_rows) > 0
                    rec("P15", "PASS" if sensex_option_history_available else "FAIL", res_bm.status,
                        bse_supported=sensex_option_history_available, bar_count=len(b_rows))
                else:
                    rec("P15", "FAIL", res_bc.status, bse_supported=False)
            else:
                rec("P15", "FAIL", res_bse.status, bse_supported=False)
        except (ApiError, RetryExhausted, BudgetExceeded) as err:
            rec("P15", "FAIL", getattr(err, "status", 0), bse_supported=False, error=str(err))

        if client.tokens.analytics and client.tokens.access:
            try:
                res_a = client.get("/v2/expired-instruments/expiries",
                                   {"instrument_key": nifty_key}, kind="expired_analytics")
                analytics_accepted_for_expired = res_a.status == 200
                rec("P16", "PASS" if analytics_accepted_for_expired else "FAIL", res_a.status,
                    analytics_accepted_for_expired=analytics_accepted_for_expired)
            except (ApiError, RetryExhausted, BudgetExceeded) as err:
                rec("P16", "FAIL", getattr(err, "status", 0), analytics_accepted_for_expired=False)
        else:
            rec("P16", "UNVERIFIED", 0, reason="BOTH_TOKENS_NOT_SET")

    # ---- P17: Global instruments & 10Y yield check -------------------------
    g_rows = masters.get("global", [])
    usdinr_key = find_global(g_rows, "USD INR")
    gift_key = find_global(g_rows, "GIFT NIFTY")
    yield_hits = search_global(g_rows)
    try:
        u_bars, g_bars = 0, 0
        st_code = 0
        if usdinr_key:
            ru = client.get(v3_candle_path(usdinr_key, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
            u_r, _ = parse_candles(ru.json)
            u_bars, st_code = len(u_r), ru.status
        if gift_key:
            rg = client.get(v3_candle_path(gift_key, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
            g_r, _ = parse_candles(rg.json)
            g_bars, st_code = len(g_r), rg.status
        rec("P17", "PASS" if (u_bars or g_bars) else "UNVERIFIED", st_code,
            usdinr_bars=u_bars, gift_bars=g_bars, india_10y_yield_candidates=yield_hits)
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P17", "UNVERIFIED", getattr(err, "status", 0), india_10y_yield_candidates=yield_hits, error=str(err))

    # ---- P18: Observed last-bar time after 2026-08-03 ----------------------
    obs_last = {
        "nifty_spot": p02_rows[-1]["ts"][11:16] if p02_rows else None,
        "nifty_option": p08_rows[-1]["ts"][11:16] if p08_rows else None,
        "sensex_spot": None,
        "sensex_option": None,
    }
    try:
        rs = client.get(v3_candle_path(sensex_key, "minutes", 1, iso(recent_day), iso(recent_day)), kind="v3")
        s_rows, _ = parse_candles(rs.json)
        if s_rows:
            obs_last["sensex_spot"] = s_rows[-1]["ts"][11:16]
        s_opts = listed_contracts(masters.get("BSE", []), sensex_key, kinds=("CE", "PE"))
        if s_opts:
            rso = client.get(v3_candle_path(s_opts[0]["instrument_key"], "minutes", 1, iso(recent_day), iso(recent_day)),
                             kind="v3")
            so_rows, _ = parse_candles(rso.json)
            if so_rows:
                obs_last["sensex_option"] = so_rows[-1]["ts"][11:16]
        rec("P18", "PASS" if obs_last["nifty_spot"] else "UNVERIFIED", 200, observed_last_bar=obs_last)
    except (ApiError, RetryExhausted, BudgetExceeded) as err:
        rec("P18", "UNVERIFIED", getattr(err, "status", 0), observed_last_bar=obs_last, error=str(err))

    # ---- P19: Depth ladder for expired 1-minute candles --------------------
    if plus_required or not p11_passed or not nifty_expiries:
        rec("P19", "UNVERIFIED", 0, reason="EXPIRED_UNAVAILABLE")
    else:
        max_warm = max(settings.params["WARMUP_OPTION_TRADING_DAYS_BY_INDEX"].values())
        d_warm = calendar.shift(settings.test_start, -int(max_warm))
        d_test = settings.test_start
        d_deep = recent_day - dt.timedelta(days=400)
        probes = [d_deep, d_warm, d_test]
        oldest_ok = None
        if target_exp and p11_rows:
            oldest_ok = p11_rows[0]["ts_open"].date()
        tested_exps = set()
        for target_d in probes:
            exp_d = _nearest_date(nifty_expiries, target_d)
            if not exp_d or exp_d in tested_exps:
                continue
            tested_exps.add(exp_d)
            try:
                rc = client.get("/v2/expired-instruments/option/contract",
                                {"instrument_key": nifty_key, "expiry_date": iso(exp_d)}, kind="expired")
                clist = rc.json.get("data") or []
                if not clist:
                    continue
                ck = clist[0].get("instrument_key") or clist[0].get("expired_instrument_key")
                td = exp_d if calendar.is_trading_day(exp_d) else calendar.prev_trading_day(exp_d)
                rm = client.get(expired_candle_path(ck, "1minute", iso(td), iso(td)), kind="expired")
                mrows, _ = parse_candles(rm.json)
                if mrows:
                    d_got = mrows[0]["ts_open"].date()
                    if oldest_ok is None or d_got < oldest_ok:
                        oldest_ok = d_got
            except (ApiError, RetryExhausted, BudgetExceeded):
                continue
        option_history_oldest_date = iso(oldest_ok) if oldest_ok else None
        rec("P19", "PASS" if option_history_oldest_date else "FAIL", 200,
            option_history_oldest_date=option_history_oldest_date)

    # ---- P20: OI behaviour -------------------------------------------------
    if p11_rows:
        ois = [r["oi"] for r in p11_rows if r["oi"] is not None]
        oi_intraday_varies = len(set(ois)) > 1
        last_1m_oi = ois[-1] if ois else None
        p11_date = p11_rows[-1]["ts_open"].date()
        daily_oi = None
        for r in p13_rows:
            if r["ts_open"].date() == p11_date:
                daily_oi = r["oi"]
                break
        if daily_oi is None and p13_rows:
            daily_oi = p13_rows[-1]["oi"]
        oi_last_bar_equals_daily = (last_1m_oi is not None and daily_oi is not None and last_1m_oi == daily_oi)
        rec("P20", "PASS", 200, oi_intraday_varies=oi_intraday_varies,
            oi_last_bar_equals_daily=oi_last_bar_equals_daily, distinct_oi_count=len(set(ois)))
    else:
        rec("P20", "UNVERIFIED", 0, oi_intraday_varies=False, oi_last_bar_equals_daily=False)

    # ---- P21: Corporate action sample (only if budget >= 1 call left) ------
    if client.calls_made < PROBE_BUDGET:
        eq_k = equity_key(masters.get("NSE", []), "HDFCBANK") or equity_key(masters.get("NSE", []), "RELIANCE")
        if eq_k:
            try:
                re_ca = client.get(v3_candle_path(eq_k, "days", 1, iso(recent_day), iso(recent_day)), kind="v3")
                ca_rows, _ = parse_candles(re_ca.json)
                rec("P21", "UNVERIFIED", re_ca.status, instrument_key=eq_k,
                    adjusted_status="UNVERIFIED", sample=_sample_candles(ca_rows))
            except (ApiError, RetryExhausted, BudgetExceeded) as err:
                rec("P21", "UNVERIFIED", getattr(err, "status", 0), adjusted_status="UNVERIFIED", error=str(err))
        else:
            rec("P21", "UNVERIFIED", 0, adjusted_status="UNVERIFIED", reason="NO_EQUITY_KEY")
    else:
        rec("P21", "UNVERIFIED", 0, reason="BUDGET_SAVED")

    # ---- Plan Selection ----------------------------------------------------
    max_warm = max(settings.params["WARMUP_OPTION_TRADING_DAYS_BY_INDEX"].values())
    required_oldest = calendar.shift(settings.test_start, -int(max_warm))

    if plus_required or not p11_passed:
        selected_plan = "B" if p08_passed else "C"
    else:
        if option_history_oldest_date and to_date(option_history_oldest_date) <= required_oldest:
            selected_plan = "A"
        else:
            selected_plan = "A_PARTIAL"

    if selected_plan == "A_PARTIAL":
        print(PLAN_A_PARTIAL)
    elif selected_plan == "B":
        print(PLAN_B)
    elif selected_plan == "C":
        print(PLAN_C)

    return _write_reports(
        settings, today, recent_day, selected_plan, tests, client,
        option_history_oldest_date, expired_1m_max_span_days,
        sensex_option_history_available, analytics_accepted_for_expired,
        v3_1m_max_span_days, oi_intraday_varies, oi_last_bar_equals_daily
    )


def _write_reports(settings: Settings, today: dt.date, recent_day: dt.date, selected_plan: str,
                   tests: dict, client: HttpClient, option_history_oldest_date: Optional[str],
                   expired_1m_max_span_days: int, sensex_option_history_available: bool,
                   analytics_accepted_for_expired: bool, v3_1m_max_span_days: int,
                   oi_intraday_varies: bool, oi_last_bar_equals_daily: bool) -> dict:
    calendar = load_calendar(settings.reference_dir)
    usable_start = settings.test_start
    if selected_plan == "A_PARTIAL" and option_history_oldest_date:
        usable_start = max(settings.test_start, to_date(option_history_oldest_date))
    usable_trading_days = calendar.count_trading_days(usable_start, settings.test_end)

    report = {
        "generated_utc": utc_iso(),
        "probe_date": iso(today),
        "recent_trading_day": iso(recent_day),
        "selected_plan": selected_plan,
        "option_history_oldest_date": option_history_oldest_date,
        "expired_1m_max_span_days": expired_1m_max_span_days,
        "v3_1m_max_span_days": v3_1m_max_span_days,
        "sensex_option_history_available": sensex_option_history_available,
        "analytics_accepted_for_expired": analytics_accepted_for_expired,
        "oi_intraday_varies": oi_intraday_varies,
        "oi_last_bar_equals_daily": oi_last_bar_equals_daily,
        "oi_timing": "per_candle_VERIFIED" if (oi_intraday_varies and oi_last_bar_equals_daily) else "per_candle_UNVERIFIED",
        "usable_window_start": iso(usable_start),
        "usable_window_end": iso(settings.test_end),
        "usable_trading_days": usable_trading_days,
        "api_calls_made": client.calls_made,
        "file_downloads": client.file_downloads,
        "rate_headers": client.header_log,
        "largest_burst_without_429": client.longest_ok_streak,
        "tests": tests,
    }
    atomic_write_bytes(settings.probe_json, json.dumps(report, indent=2, sort_keys=True).encode("utf-8"), overwrite=True)

    lines = [
        f"UPDL PROBE REPORT ({iso(today)})",
        f"Selected Plan: {selected_plan}",
        f"Recent Trading Day: {iso(recent_day)}",
        f"Option History Oldest Date: {option_history_oldest_date}",
        f"Expired 1m Max Span Days: {expired_1m_max_span_days}",
        f"V3 1m Max Span Days: {v3_1m_max_span_days}",
        f"Sensex Option History Available: {sensex_option_history_available}",
        f"Usable Window: {iso(usable_start)} to {iso(settings.test_end)} ({usable_trading_days} trading days)",
        f"API Calls Used: {client.calls_made} / {PROBE_BUDGET} (Master files: {client.file_downloads})",
        f"Largest Burst Without 429: {client.longest_ok_streak}",
        f"Rate Headers: {json.dumps(client.header_log)}",
    ]
    if selected_plan == "A_PARTIAL":
        lines.append(PLAN_A_PARTIAL)
    elif selected_plan == "B":
        lines.append(PLAN_B)
    elif selected_plan == "C":
        lines.append(PLAN_C)
    for pid in sorted(tests):
        t = tests[pid]
        lines.append(f"{pid}: {t['status']} (HTTP {t.get('http_status', 0)})")
    atomic_write_bytes(settings.probe_txt, ("\n".join(lines) + "\n").encode("utf-8"), overwrite=True)
    return report
