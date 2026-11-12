"""Stage 1 Planner and Downloader (`python -m updl plan` and `python -m updl download`).

Implements:
* strict dependency sequence: D12 -> D01..D04 -> D09 -> D07 -> D05 -> D06 -> D08 -> D10 -> D11
* point-in-time contract selection with delta-aware strike bands (from D01 spot high/low/close and D03 VIX high)
* selection_plan.parquet generation and runtime estimate before any network call
* dry-run mode (zero network calls, zero writes outside a temp folder)
* resumable chunk execution, HTTP 400 range split fallback, UDAPI100500 span split retry, --stop-at and --max-calls
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

from .client import MASTER_URLS, HttpClient, expired_candle_path, v3_candle_path
from .config import ITEM_ORDER, PLANS, Settings, load_yaml
from .errors import (ApiError, BudgetExceeded, PlusRequired, RetryExhausted, RightsNotAllowed,
                     TokenExpired)
from .master import (equity_key, find_global, fo_underlying_symbols, listed_contracts,
                     parse_master_bytes, resolve_spot_key)
from .messages import (CTRL_C, DATA_RIGHTS_WARNING, PLAN_A_PARTIAL, PLAN_B, PLAN_C, PROBE_TOKEN_BLOCKED,
                       RIGHTS_NOT_ALLOWED, TOKEN_EXPIRED)
from .probe import check_probe_fresh
from .selector import (Contract, DayStats, chunk_days, contract_chunks, first_seen_hides,
                       futures_expiries_for_day, monthly_expiries, select_day_options)
from .sessions import Calendar, load_calendar
from .state import (DONE, EMPTY_OK, FAILED_PERMANENT, FAILED_RETRYABLE, Catalog,
                    State, cov_key)
from .store import (ITEM_TABLE, SOURCE_EXPIRED, SOURCE_V3, atomic_write_bytes, cleanup_temp_files,
                    parse_candles, read_raw_payload, write_parquet, write_raw)
from .timeutil import iso, now_ist, to_date, utc_iso


def filter_contracts_point_in_time(expired_list: list, master_list: list, ref_today: dt.date,
                                   log=None) -> dict:
    """Combine expired contracts and today's master contracts into {expiry_date: [Contract]}.
    A contract in today's master whose expiry belongs to an already-expired expiry (or < ref_today)
    but is absent from the expired contract list is logged MASTER_ONLY and excluded."""
    log = log or (lambda *a, **k: None)
    by_exp = defaultdict(list)
    expired_keys_by_exp = defaultdict(set)

    for c in expired_list:
        exp = to_date(c["expiry_date"])
        ek = c.get("instrument_key") or c.get("expired_instrument_key")
        expired_keys_by_exp[exp].add(ek)
        by_exp[exp].append(Contract(
            instrument_key=ek,
            expiry_date=exp,
            strike=float(c["strike"]) if c.get("strike") is not None else None,
            option_type=c["option_type"],
            source="expired",
            trading_symbol=str(c.get("trading_symbol") or ""),
        ))

    for c in master_list:
        exp = to_date(c["expiry_date"])
        ik = c["instrument_key"]
        if exp < ref_today or exp in expired_keys_by_exp:
            if ik not in expired_keys_by_exp.get(exp, set()):
                log("MASTER_ONLY", instrument_key=ik, expiry_date=iso(exp))
            continue
        by_exp[exp].append(Contract(
            instrument_key=ik,
            expiry_date=exp,
            strike=float(c["strike"]) if c.get("strike") is not None else None,
            option_type=c["option_type"],
            source="listed",
            trading_symbol=str(c.get("trading_symbol") or ""),
        ))
    return dict(by_exp)


def _ensure_stocks_csv(settings: Settings, master_nse: list, log=None) -> list:
    log = log or (lambda *a, **k: None)
    ext_csv = settings.external_dir / "stocks.csv"
    if not ext_csv.exists():
        default_csv = settings.reference_dir / "stocks_default.csv"
        if not settings.dry_run and default_csv.exists():
            ext_csv.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(ext_csv, default_csv.read_bytes(), overwrite=True)
        elif default_csv.exists():
            with open(default_csv, "r", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            return _resolve_stock_rows(rows, master_nse, log)
        else:
            return []
    with open(ext_csv, "r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return _resolve_stock_rows(rows, master_nse, log)


def _resolve_stock_rows(rows: list, master_nse: list, log) -> list:
    fo_syms = fo_underlying_symbols(master_nse)
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        isin_val = (r.get("isin") or "").strip()
        if isin_val:
            r["instrument_key"] = isin_val if "|" in isin_val else f"NSE_EQ|{isin_val}"
        elif master_nse:
            ek = equity_key(master_nse, sym)
            if ek:
                r["instrument_key"] = ek
                r["isin"] = ek.split("|", 1)[1] if "|" in ek else ""
            else:
                r["instrument_key"] = None
                log("stock_unresolved", symbol=sym)
        else:
            r["instrument_key"] = f"NSE_EQ|{sym}"
        if not r.get("fo_flag"):
            r["fo_flag"] = "true" if sym in fo_syms else "false"
    return rows


def _load_cached_masters(settings: Settings) -> dict:
    out = {}
    for exch in ("NSE", "BSE", "global"):
        p = settings.raw_dir / "D12" / f"{exch}.json.gz"
        if p.exists():
            try:
                out[exch] = parse_master_bytes(p.read_bytes())
            except Exception:
                out[exch] = []
        else:
            out[exch] = []
    return out


def _effective_plan(settings: Settings, probe_data: Optional[dict]) -> str:
    if settings.plan_override:
        print(f"WARNING: plan override active ({settings.plan_override})")
        return settings.plan_override
    if probe_data and probe_data.get("selected_plan"):
        if probe_data["selected_plan"] not in PLANS:
            # v3.1: BLOCKED_TOKEN report -> say so; estimate with plan A (download refuses anyway)
            print(PROBE_TOKEN_BLOCKED)
            return "A"
        return probe_data["selected_plan"]
    return "A"


def _window_bounds(settings: Settings, calendar: Calendar, win_spec: list, underlying: str = "NIFTY",
                   include_warmup: bool = True, oldest_opt_date: Optional[dt.date] = None):
    end = settings.test_end
    start = settings.test_start
    if include_warmup:
        if "warmup_daily" in win_spec:
            start = settings.warmup_daily_start
        elif "warmup_1m" in win_spec:
            start = settings.warmup_1m_start
        elif "warmup_option" in win_spec:
            w_days = settings.option_warmup_days(underlying)
            start = calendar.shift(settings.test_start, -w_days)
    if oldest_opt_date and ("warmup_option" in win_spec or "test" in win_spec):
        if oldest_opt_date > start:
            start = oldest_opt_date
    return start, end


def _spot_and_vix_stats(settings: Settings, state: Optional[State]) -> tuple[dict, dict]:
    spot_by_und_day = defaultdict(dict)
    vix_high_by_day = {}
    if state is None:
        return spot_by_und_day, vix_high_by_day

    for c in state.chunks(status=[DONE], item_id="D03"):
        if not c["raw_path"] or not Path(c["raw_path"]).exists():
            continue
        rows, _ = parse_candles(read_raw_payload(c["raw_path"]))
        by_d = defaultdict(list)
        for r in rows:
            by_d[r["ts_open"].date()].append(r["h"])
        for d, hs in by_d.items():
            vix_high_by_day[d] = max(hs)

    for c in state.chunks(status=[DONE], item_id="D01"):
        if not c["raw_path"] or not Path(c["raw_path"]).exists():
            continue
        und = c["underlying"]
        rows, _ = parse_candles(read_raw_payload(c["raw_path"]))
        by_d = defaultdict(list)
        for r in rows:
            by_d[r["ts_open"].date()].append(r)
        for d, rlist in by_d.items():
            spot_by_und_day[und][d] = (
                min(x["l"] for x in rlist),
                max(x["h"] for x in rlist),
                rlist[-1]["c"],
            )
    return spot_by_und_day, vix_high_by_day


def _default_day_stats(und: str) -> DayStats:
    defaults = {
        "NIFTY": DayStats(low=24850.0, high=25150.0, close=25000.0, vix_high=14.0),
        "BANKNIFTY": DayStats(low=56600.0, high=57400.0, close=57000.0, vix_high=14.0),
        "SENSEX": DayStats(low=81500.0, high=82500.0, close=82000.0, vix_high=14.0),
    }
    return defaults.get(und, DayStats(low=24850.0, high=25150.0, close=25000.0, vix_high=14.0))


def _stop_time_reached(stop_at: Optional[str]) -> bool:
    if not stop_at:
        return False
    try:
        hh, mm = (int(x) for x in stop_at.split(":"))
        cur = now_ist()
        return (cur.hour, cur.minute) >= (hh, mm)
    except ValueError:
        return False


def build_selection_plan(settings: Settings, calendar: Optional[Calendar] = None,
                         probe_data: Optional[dict] = None, include_warmup: bool = True,
                         out_parquet_path: Optional[Path] = None) -> dict:
    """Build the full chunk and contract selection plan without making any network calls."""
    calendar = calendar or load_calendar(settings.reference_dir)
    plan_name = _effective_plan(settings, probe_data)
    v3_cap = int((probe_data or {}).get("v3_1m_max_span_days") or 28)
    v3_cap = min(v3_cap, 28)
    exp_cap = int((probe_data or {}).get("expired_1m_max_span_days") or 1)
    if exp_cap <= 0:
        exp_cap = 1
    oldest_opt = None
    if plan_name == "A_PARTIAL" and (probe_data or {}).get("option_history_oldest_date"):
        oldest_opt = to_date(probe_data["option_history_oldest_date"])

    masters = _load_cached_masters(settings)
    state = None
    catalog = None
    if not settings.dry_run and (settings.state_dir / "progress.sqlite3").exists():
        state = State(settings.state_dir)
    if not settings.dry_run and (settings.state_dir / "catalog.sqlite3").exists():
        catalog = Catalog(settings.state_dir)

    try:
        spot_map, vix_map = _spot_and_vix_stats(settings, state)
        plan_rows = []
        calls_by_item = defaultdict(int)
        skipped_items = {}

        for item_id in ITEM_ORDER:
            if item_id not in settings.items or not settings.item_selected(item_id):
                continue
            item = settings.items[item_id]
            if plan_name not in item.get("plan", []) and not (plan_name == "A_PARTIAL" and "A" in item.get("plan", [])):
                skipped_items[item_id] = f"SKIPPED_PLAN_{plan_name}"
                continue

            if item_id == "D12":
                calls_by_item["D12"] = 0
                continue

            if item_id in ("D01", "D02"):
                is_daily = item_id == "D02"
                cap = 3000 if is_daily else v3_cap
                intv = "1d" if is_daily else "1m"
                for und in [u for u in item.get("instruments", []) if u in settings.instruments]:
                    exch = settings.exchange_of(und)
                    try:
                        sk, _ = resolve_spot_key(settings.spot_key(und), masters.get(exch, []))
                    except Exception:
                        sk = settings.spot_key(und)
                    w0, w1 = _window_bounds(settings, calendar, item.get("window", []), und, include_warmup)
                    tdays = calendar.trading_days(w0, w1)
                    cov = state.covered_dates(cov_key(sk, intv)) if state else set()
                    uncov = [d for d in tdays if d not in cov]
                    chks = chunk_days(uncov, cap)
                    calls_by_item[item_id] += len(chks)
                    plan_rows.append({
                        "item_id": item_id, "underlying": und, "expiry": None,
                        "instrument_key": sk, "strike": None, "option_type": "SPOT",
                        "days": len(uncov), "chunks": len(chks), "source": SOURCE_V3,
                        "interval": intv, "chunk_list": chks,
                    })

            elif item_id in ("D03", "D04"):
                is_daily = item_id == "D04"
                cap = 3000 if is_daily else v3_cap
                intv = "1d" if is_daily else "1m"
                sk = settings.spot_key("INDIAVIX") if "INDIAVIX" in settings.instruments else "NSE_INDEX|India VIX"
                w0, w1 = _window_bounds(settings, calendar, item.get("window", []), "INDIAVIX", include_warmup)
                tdays = calendar.trading_days(w0, w1)
                cov = state.covered_dates(cov_key(sk, intv)) if state else set()
                uncov = [d for d in tdays if d not in cov]
                chks = chunk_days(uncov, cap)
                calls_by_item[item_id] += len(chks)
                plan_rows.append({
                    "item_id": item_id, "underlying": "INDIAVIX", "expiry": None,
                    "instrument_key": sk, "strike": None, "option_type": "SPOT",
                    "days": len(uncov), "chunks": len(chks), "source": SOURCE_V3,
                    "interval": intv, "chunk_list": chks,
                })

            elif item_id == "D09":
                stock_rows = _ensure_stocks_csv(settings, masters.get("NSE", []))
                p0_rows = [r for r in stock_rows if r.get("group") in ("HEAVY", "BANK")]
                seen_syms = set()
                w0, w1 = _window_bounds(settings, calendar, item.get("window", []), "NIFTY", include_warmup)
                tdays = calendar.trading_days(w0, w1)
                for r in p0_rows:
                    sym = (r.get("symbol") or "").strip()
                    if not sym or sym in seen_syms:
                        continue
                    seen_syms.add(sym)
                    ik = r.get("instrument_key")
                    if not ik:
                        continue
                    cov = state.covered_dates(cov_key(ik, "1m")) if state else set()
                    uncov = [d for d in tdays if d not in cov]
                    chks = chunk_days(uncov, v3_cap)
                    calls_by_item[item_id] += len(chks)
                    plan_rows.append({
                        "item_id": item_id, "underlying": sym, "expiry": None,
                        "instrument_key": ik, "strike": None, "option_type": "EQ",
                        "days": len(uncov), "chunks": len(chks), "source": SOURCE_V3,
                        "interval": "1m", "chunk_list": chks,
                    })

            elif item_id == "D10":
                stock_rows = _ensure_stocks_csv(settings, masters.get("NSE", []))
                p0_syms = {(r.get("symbol") or "").strip() for r in stock_rows if r.get("group") in ("HEAVY", "BANK")}
                p1_rows = [r for r in stock_rows if r.get("group") in ("NIFTY50", "SENSEX")
                           and (r.get("symbol") or "").strip() not in p0_syms]
                if not p1_rows:
                    skipped_items["D10"] = "SKIPPED_NO_LIST"
                    print("D10: SKIPPED_NO_LIST (NIFTY50/SENSEX stock rows absent in external/stocks.csv)")
                    continue
                seen_syms = set()
                w0, w1 = _window_bounds(settings, calendar, item.get("window", []), "NIFTY", False)
                tdays = calendar.trading_days(w0, w1)
                for r in p1_rows:
                    sym = (r.get("symbol") or "").strip()
                    if not sym or sym in seen_syms:
                        continue
                    seen_syms.add(sym)
                    ik = r.get("instrument_key")
                    if not ik:
                        continue
                    cov = state.covered_dates(cov_key(ik, "1m")) if state else set()
                    uncov = [d for d in tdays if d not in cov]
                    chks = chunk_days(uncov, v3_cap)
                    calls_by_item[item_id] += len(chks)
                    plan_rows.append({
                        "item_id": item_id, "underlying": sym, "expiry": None,
                        "instrument_key": ik, "strike": None, "option_type": "EQ",
                        "days": len(uncov), "chunks": len(chks), "source": SOURCE_V3,
                        "interval": "1m", "chunk_list": chks,
                    })

            elif item_id == "D11":
                g_cfg = load_yaml(settings.reference_dir / "globals.yaml")
                g_rows = masters.get("global", [])
                w0, w1 = _window_bounds(settings, calendar, item.get("window", []), "NIFTY", False)
                tdays = calendar.trading_days(w0, w1)
                for gname in g_cfg.get("names", []):
                    gk = find_global(g_rows, gname) or f"GLOBAL|{gname}"
                    cov = state.covered_dates(cov_key(gk, "1m")) if state else set()
                    uncov = [d for d in tdays if d not in cov]
                    chks = chunk_days(uncov, v3_cap)
                    calls_by_item[item_id] += len(chks)
                    plan_rows.append({
                        "item_id": item_id, "underlying": gname.replace(" ", "_"), "expiry": None,
                        "instrument_key": gk, "strike": None, "option_type": "GLOBAL",
                        "days": len(uncov), "chunks": len(chks), "source": SOURCE_V3,
                        "interval": "1m", "chunk_list": chks,
                    })

            elif item_id == "D07":
                for und in [u for u in item.get("instruments", []) if u in settings.instruments]:
                    exps = catalog.expiries(und) if catalog else []
                    n_calls = 1 + (len(exps) * 2 if exps else 12)
                    calls_by_item["D07"] += n_calls

            elif item_id in ("D05", "D06", "D08"):
                listed_only = (plan_name == "B" and item.get("b_mode") == "still_listed_only")
                min_steps = float(settings.params["WIDE_CHAIN_STEPS"]) if item_id == "D08" else None
                margin_steps = float(settings.params["STRIKE_MARGIN_STEPS"])
                delta_k = float(settings.params["STRIKE_DELTA_MARGIN_K"])
                first_seen = catalog.first_seen() if catalog else {}

                for und in [u for u in item.get("instruments", []) if u in settings.instruments]:
                    if (probe_data or {}).get("sensex_option_history_available") is False and und == "SENSEX" and not listed_only:
                        skipped_items[f"{item_id}:{und}"] = "SENSEX_UNAVAILABLE"
                        continue
                    exch = settings.exchange_of(und)
                    try:
                        sk, _ = resolve_spot_key(settings.spot_key(und), masters.get(exch, []))
                    except Exception:
                        sk = settings.spot_key(und)
                    w0, w1 = _window_bounds(settings, calendar, item.get("window", []), und, include_warmup, oldest_opt)
                    tdays = calendar.trading_days(w0, w1)
                    m_contracts = listed_contracts(masters.get(exch, []), sk,
                                                   kinds=("FUT",) if item_id == "D05" else ("CE", "PE"))
                    e_contracts = catalog.contracts(und, kind="FUT" if item_id == "D05" else "OPT") if catalog else []
                    by_exp = filter_contracts_point_in_time(e_contracts, m_contracts, settings.test_end)
                    all_exps = sorted(by_exp.keys())

                    if not all_exps:
                        est_days = len(tdays)
                        if item_id == "D05":
                            n_chks = max(1, (est_days * 2) // max(1, exp_cap))
                        else:
                            strikes_per_day = 42 if item_id == "D08" else 18
                            n_chks = max(1, (est_days * strikes_per_day * 2) // max(1, exp_cap))
                        calls_by_item[item_id] += n_chks
                        plan_rows.append({
                            "item_id": item_id, "underlying": und, "expiry": None,
                            "instrument_key": f"ESTIMATE|{und}|{item_id}", "strike": None,
                            "option_type": "FUT" if item_id == "D05" else "OPT",
                            "days": est_days, "chunks": n_chks,
                            "source": SOURCE_V3 if listed_only else SOURCE_EXPIRED,
                            "interval": "1m", "chunk_list": [],
                        })
                        continue

                    selected_days_by_contract = defaultdict(list)
                    contract_obj = {}

                    if item_id == "D05":
                        m_exps = monthly_expiries(all_exps)
                        for d in tdays:
                            for e in futures_expiries_for_day(d, m_exps):
                                for c in by_exp.get(e, []):
                                    if listed_only and c.source != "listed":
                                        continue
                                    if first_seen_hides(first_seen.get(c.instrument_key), d):
                                        continue
                                    selected_days_by_contract[c.instrument_key].append(d)
                                    contract_obj[c.instrument_key] = c
                    else:
                        default_step = float(settings.instruments[und].get("strike_step_default", 50))
                        is_m = settings.is_monthly(und)
                        for d in tdays:
                            if d in spot_map.get(und, {}):
                                lo, hi, cl = spot_map[und][d]
                                vh = vix_map.get(d, 14.0)
                                st_d = DayStats(low=lo, high=hi, close=cl, vix_high=vh)
                            else:
                                st_d = _default_day_stats(und)
                            chosen = select_day_options(
                                d, all_exps, by_exp, st_d,
                                margin_steps=margin_steps, delta_k=delta_k,
                                default_step=default_step, first_seen=first_seen,
                                min_steps=min_steps, listed_only=listed_only, monthly=is_m
                            )
                            for c in chosen:
                                selected_days_by_contract[c.instrument_key].append(d)
                                contract_obj[c.instrument_key] = c

                    for ck, cdays in sorted(selected_days_by_contract.items()):
                        c = contract_obj[ck]
                        cap_c = v3_cap if c.source == "listed" else exp_cap
                        cov = state.covered_dates(cov_key(ck, "1m")) if state else set()
                        chks = contract_chunks(cdays, cov, cap_c, calendar)
                        calls_by_item[item_id] += len(chks)
                        plan_rows.append({
                            "item_id": item_id, "underlying": und, "expiry": iso(c.expiry_date),
                            "instrument_key": ck, "strike": c.strike, "option_type": c.option_type,
                            "days": len(set(cdays) - cov), "chunks": len(chks),
                            "source": SOURCE_V3 if c.source == "listed" else SOURCE_EXPIRED,
                            "interval": "1m", "chunk_list": chks,
                        })

        df_rows = [{
            "item_id": r["item_id"],
            "underlying": r["underlying"],
            "expiry": r["expiry"],
            "key": r["instrument_key"],
            "strike": r["strike"],
            "option_type": r["option_type"],
            "days": int(r["days"]),
            "chunks": int(r["chunks"]),
        } for r in plan_rows]
        df_plan = pd.DataFrame(df_rows, columns=["item_id", "underlying", "expiry", "key", "strike",
                                                 "option_type", "days", "chunks"])
        if out_parquet_path is not None:
            write_parquet(df_plan, out_parquet_path, {"plan": plan_name})

        total_calls = sum(calls_by_item.values())
        est_minutes = total_calls / max(1.0, settings.rate_per_min)
        return {
            "plan": plan_name,
            "calls_by_item": dict(calls_by_item),
            "total_calls": total_calls,
            "estimated_minutes": round(est_minutes, 1),
            "estimated_hours": round(est_minutes / 60.0, 2),
            "rows": plan_rows,
            "skipped": skipped_items,
            "df": df_plan,
        }
    finally:
        if state:
            state.close()
        if catalog:
            catalog.close()


def print_plan_summary(summary: dict, settings: Settings):
    print(f"Plan: {summary['plan']} | Window: {iso(settings.test_start)} .. {iso(settings.test_end)}")
    for item_id in ITEM_ORDER:
        if item_id in summary["calls_by_item"]:
            print(f"  {item_id}: {summary['calls_by_item'][item_id]} calls")
    for k, reason in summary.get("skipped", {}).items():
        print(f"  {k}: {reason}")
    print(f"Total API calls: {summary['total_calls']} | Rate: {settings.rate_per_min}/min | "
          f"Est. time: {summary['estimated_minutes']} min ({summary['estimated_hours']} hrs)")


def run_plan_command(settings: Settings, calendar: Optional[Calendar] = None,
                     include_warmup: bool = True) -> dict:
    if settings.data_rights == "NOT_ALLOWED":
        print(RIGHTS_NOT_ALLOWED)
        raise RightsNotAllowed(RIGHTS_NOT_ALLOWED)
    if settings.data_rights == "UNVERIFIED":
        print(DATA_RIGHTS_WARNING)

    probe_data = None
    if settings.probe_json.exists():
        try:
            probe_data = json.loads(settings.probe_json.read_text(encoding="utf-8"))
        except Exception:
            probe_data = None

    if settings.dry_run:
        with tempfile.TemporaryDirectory(prefix="updl_dry_") as tmp:
            p = Path(tmp) / "selection_plan.parquet"
            summary = build_selection_plan(settings, calendar, probe_data, include_warmup, p)
            print_plan_summary(summary, settings)
            return summary
    else:
        settings.ensure_dirs()
        p = settings.workdir / "selection_plan.parquet"
        summary = build_selection_plan(settings, calendar, probe_data, include_warmup, p)
        print_plan_summary(summary, settings)
        return summary


def _fetch_and_store_chunk(settings: Settings, client: HttpClient, state: State, catalog: Catalog,
                           item_id: str, und: str, ikey: str, opt_type: str, expiry: Optional[str],
                           strike: Optional[float], interval: str, source: str,
                           frm: dt.date, to: dt.date, days_in_chunk: list, run_id: str, log):
    table = ITEM_TABLE.get(item_id, "other")
    ck = cov_key(ikey, interval)

    def _do_one(f_d: dt.date, t_d: dt.date, sub_days: list, allow_split: bool = True):
        if _stop_time_reached(settings.stop_at):
            raise BudgetExceeded("stop-at time reached")
        try:
            if source == SOURCE_V3:
                unit = "days" if interval == "1d" else "minutes"
                res = client.get(v3_candle_path(ikey, unit, 1, iso(t_d), iso(f_d)), kind="v3")
            else:
                intv_name = "day" if interval == "1d" else "1minute"
                res = client.get(expired_candle_path(ikey, intv_name, iso(t_d), iso(f_d)), kind="expired")
        except ApiError as err:
            if (err.status == 400 or err.code == "UDAPI100500") and allow_split and len(sub_days) > 1:
                log("split_retry", item_id=item_id, key=ikey, from_date=iso(f_d), to_date=iso(t_d), code=err.code)
                for single_d in sub_days:
                    _do_one(single_d, single_d, [single_d], allow_split=False)
                return
            st_flag = FAILED_RETRYABLE if err.code == "UDAPI100500" else FAILED_PERMANENT
            state.set_chunk(item_id, ikey, f_d, t_d, st_flag, error=f"{err.status}:{err.code}",
                            run_id=run_id, table_name=table, underlying=und)
            return
        except RetryExhausted as err:
            state.set_chunk(item_id, ikey, f_d, t_d, FAILED_RETRYABLE, error=str(err),
                            run_id=run_id, table_name=table, underlying=und)
            return

        rows, _ = parse_candles(res.json)
        if rows:
            catalog.note_first_seen(ikey, rows[0]["ts_open"].date(), requested_from=f_d)
        meta = {
            "item_id": item_id,
            "table": table,
            "instrument_key": ikey,
            "underlying": und,
            "expiry_date": expiry,
            "strike": strike,
            "option_type": opt_type,
            "interval": interval,
            "source": source,
            "from_date": iso(f_d),
            "to_date": iso(t_d),
            "request_path": res.path,
            "http_status": res.status,
            "fetched_utc": utc_iso(),
            "header_hints": res.header_hints,
        }
        w_info = write_raw(settings.raw_dir, item_id, ikey, f_d, t_d, run_id, res.body, meta)
        status = DONE if rows else EMPTY_OK
        state.set_chunk(item_id, ikey, f_d, t_d, status, raw_path=w_info["path"],
                        sha256=w_info["sha256"], run_id=run_id, table_name=table, underlying=und)
        state.mark_coverage(ck, sub_days, item_id, status, w_info["path"])

    _do_one(frm, to, days_in_chunk, allow_split=True)


def run_download(settings: Settings, client: HttpClient, calendar: Optional[Calendar] = None,
                 run_id: str = "r1", log=None, include_warmup: bool = True,
                 secrets=()) -> dict:
    log = log or (lambda *a, **k: None)
    if settings.data_rights == "NOT_ALLOWED":
        print(RIGHTS_NOT_ALLOWED)
        raise RightsNotAllowed(RIGHTS_NOT_ALLOWED)
    if settings.data_rights == "UNVERIFIED":
        print(DATA_RIGHTS_WARNING)

    if settings.dry_run:
        return run_plan_command(settings, calendar, include_warmup=include_warmup)

    probe_data = check_probe_fresh(settings, secrets=secrets)
    settings.ensure_dirs()
    cleanup_temp_files(settings.workdir)

    if settings.data_rights == "UNVERIFIED":
        unv_path = settings.store_dir / "DATA_RIGHTS_UNVERIFIED.txt"
        atomic_write_bytes(unv_path, (DATA_RIGHTS_WARNING + "\n").encode("utf-8"), overwrite=True)

    calendar = calendar or load_calendar(settings.reference_dir)
    if settings.max_calls is not None:
        client.budget = int(settings.max_calls)
    if probe_data.get("analytics_accepted_for_expired"):
        client.analytics_ok_for_expired = True

    plan_name = _effective_plan(settings, probe_data)
    if plan_name == "A_PARTIAL":
        print(PLAN_A_PARTIAL)
    elif plan_name == "B":
        print(PLAN_B)
    elif plan_name == "C":
        print(PLAN_C)

    if settings.items_filter is not None:
        if any(x in settings.items_filter for x in ("D06", "D08")):
            state_chk = State(settings.state_dir)
            try:
                has_d01 = bool(state_chk.chunks(status=[DONE], item_id="D01"))
                has_d03 = bool(state_chk.chunks(status=[DONE], item_id="D03"))
            finally:
                state_chk.close()
            if not has_d01 or not has_d03:
                for req_id in ("D12", "D01", "D03", "D07"):
                    if req_id not in settings.items_filter:
                        settings.items_filter.insert(0, req_id)

    state = State(settings.state_dir)
    catalog = Catalog(settings.state_dir)
    try:
        if settings.refetch_last_day and catalog.get_kv("completed_once") == "1":
            last_td = settings.test_end if calendar.is_trading_day(settings.test_end) else calendar.prev_trading_day(settings.test_end)
            refetch_tag = f"{iso(last_td)}:{run_id}"
            if catalog.get_kv("last_refetched") != refetch_tag and not state.retryable():
                for k in state.covered_keys_on(last_td):
                    state.drop_coverage(k, [last_td])
                catalog.set_kv("last_refetched", refetch_tag)

        if settings.item_selected("D12"):
            for exch, url in MASTER_URLS.items():
                p = settings.raw_dir / "D12" / f"{exch}.json.gz"
                if not p.exists():
                    raw = client.download_file(url)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(p, raw, overwrite=True)

        masters = _load_cached_masters(settings)

        early_filter = [x for x in ("D01", "D02", "D03", "D04", "D09") if settings.item_selected(x)]
        saved_filter = settings.items_filter
        if early_filter:
            settings.items_filter = early_filter
            early_plan = build_selection_plan(settings, calendar, probe_data, include_warmup)
            for row in early_plan["rows"]:
                for frm, to, cdays in row["chunk_list"]:
                    _fetch_and_store_chunk(
                        settings, client, state, catalog,
                        row["item_id"], row["underlying"], row["instrument_key"],
                        row["option_type"], row["expiry"], row["strike"],
                        row["interval"], row["source"], frm, to, cdays, run_id, log
                    )
        settings.items_filter = saved_filter

        if settings.item_selected("D07") and plan_name in ("A", "A_PARTIAL"):
            oldest_opt = to_date(probe_data["option_history_oldest_date"]) if (
                plan_name == "A_PARTIAL" and probe_data.get("option_history_oldest_date")) else None
            for und in [u for u in settings.items["D07"].get("instruments", []) if u in settings.instruments]:
                if probe_data.get("sensex_option_history_available") is False and und == "SENSEX":
                    continue
                exch = settings.exchange_of(und)
                sk, _ = resolve_spot_key(settings.spot_key(und), masters.get(exch, []))
                m_list = listed_contracts(masters.get(exch, []), sk, kinds=("CE", "PE", "FUT"),
                                          logger=log, rule=settings.instruments[und].get("expiry"))
                if m_list:
                    catalog.add_contracts(und, m_list, "listed")
                    catalog.add_expiries(und, {c["expiry_date"] for c in m_list}, "listed")
                try:
                    res_e = client.get("/v2/expired-instruments/expiries", {"instrument_key": sk}, kind="expired")
                    exp_dates = sorted({to_date(x) for x in (res_e.json.get("data") or []) if x})
                    catalog.add_expiries(und, exp_dates, "expired")
                except (PlusRequired, ApiError, RetryExhausted):
                    exp_dates = []
                w0, w1 = _window_bounds(settings, calendar, ["warmup_option", "test"], und, include_warmup, oldest_opt)
                rel_exps = [e for e in exp_dates if w0 <= e <= w1 + dt.timedelta(days=35)]
                # v3: index futures are monthly, so the future/contract list is only asked for the last expiry
                # of each month (saves about one call per weekly expiry for NIFTY and SENSEX).
                fut_exps = set(monthly_expiries(exp_dates))
                for exp_d in rel_exps:
                    for ep, kind_tag in (("/v2/expired-instruments/option/contract", "OPT"),
                                         ("/v2/expired-instruments/future/contract", "FUT")):
                        if kind_tag == "FUT" and exp_d not in fut_exps:
                            continue
                        try:
                            rc = client.get(ep, {"instrument_key": sk, "expiry_date": iso(exp_d)}, kind="expired")
                            raw_c = rc.json.get("data") or []
                            parsed_c = []
                            for r in raw_c:
                                ek = r.get("instrument_key") or r.get("expired_instrument_key")
                                if not ek:
                                    continue
                                parsed_c.append({
                                    "instrument_key": ek,
                                    "expiry_date": exp_d,
                                    "strike": r.get("strike_price") if kind_tag == "OPT" else None,
                                    "option_type": r.get("instrument_type", "FUT" if kind_tag == "FUT" else "CE"),
                                    "trading_symbol": r.get("trading_symbol", ""),
                                    "lot_size": r.get("lot_size"),
                                    "freeze_quantity": r.get("freeze_quantity"),
                                    "tick_size": r.get("tick_size"),
                                })
                            if parsed_c:
                                catalog.add_contracts(und, parsed_c, "expired")
                        except (PlusRequired, ApiError, RetryExhausted):
                            continue
        else:
            for und in settings.instrument_names():
                exch = settings.exchange_of(und)
                try:
                    sk, _ = resolve_spot_key(settings.spot_key(und), masters.get(exch, []))
                    m_list = listed_contracts(masters.get(exch, []), sk, kinds=("CE", "PE", "FUT"),
                                              logger=log, rule=settings.instruments[und].get("expiry"))
                    if m_list:
                        catalog.add_contracts(und, m_list, "listed")
                        catalog.add_expiries(und, {c["expiry_date"] for c in m_list}, "listed")
                except Exception:
                    continue

        full_plan = build_selection_plan(settings, calendar, probe_data, include_warmup,
                                         settings.workdir / "selection_plan.parquet")
        print_plan_summary(full_plan, settings)

        for row in full_plan["rows"]:
            if row["item_id"] in ("D01", "D02", "D03", "D04", "D09"):
                continue
            for frm, to, cdays in row["chunk_list"]:
                _fetch_and_store_chunk(
                    settings, client, state, catalog,
                    row["item_id"], row["underlying"], row["instrument_key"],
                    row["option_type"], row["expiry"], row["strike"],
                    row["interval"], row["source"], frm, to, cdays, run_id, log
                )

        if not state.retryable():
            catalog.set_kv("completed_once", "1")

        return {
            "plan": plan_name,
            "calls_made": client.calls_made,
            "chunk_counts": state.chunk_counts(),
        }
    except TokenExpired:
        print(TOKEN_EXPIRED)
        raise
    except KeyboardInterrupt:
        print(CTRL_C)
        return {"interrupted": True, "calls_made": client.calls_made}
    except BudgetExceeded as err:
        log("stopped_by_limit", detail=str(err))
        return {"stopped_by_limit": True, "calls_made": client.calls_made}
    finally:
        state.close()
        catalog.close()
