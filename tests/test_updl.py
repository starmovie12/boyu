"""Complete Test Suite T01..T23 for `updl` (mocked HTTP only, zero real network calls, zero real tokens)."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from updl.client import (MASTER_URLS, HttpClient, RateLimiter, RawResponse,
                         path_allowed, quote_key, v3_candle_path)
from updl.config import load_settings
from updl.derive import (black76_price, compute_ivp_vix_for_day, derive_MultiMinute_bars,
                         run_derive, solve_black76_iv)
from updl.errors import (EXIT_CORRUPT_FILE, EXIT_HOLDOUT_OPENED, EXIT_OK, EXIT_RIGHTS_NOT_ALLOWED,
                         EXIT_TOKEN_EXPIRED, CorruptFile, FillingForbidden, HoldoutAlreadyOpened,
                         HoldoutRefused, RetryExhausted, TokenExpired)
from updl.handoff import run_handoff, run_verify
from updl.load import load_dev, load_holdout
from updl.master import expiry_date_of, listed_contracts
from updl.messages import PLAN_A_PARTIAL, PLAN_B, PLAN_C
from updl.plan import filter_contracts_point_in_time, run_download
from updl.probe import PROBE_BUDGET, run_probe
from updl.secretscan import scan_tree
from updl.selector import (Contract, DayStats, band_margin_points, chunk_days, contract_chunks,
                           expiries_for_day, monthly_expiries, select_day_options, strikes_in_band)
from updl.sessions import load_calendar
from updl.state import DONE, State, cov_key
from updl.store import (compact, list_parts, parse_candles, part_path,
                        read_parquet, rows_to_frame, write_parquet)
from updl.timeutil import IST, at_ist, iso, to_date
from updl.tokens import Tokens
from updl.validate import check_no_filled_rows, forbid_fill, reconcile_bhavcopy, run_validate

REPO_ROOT = Path(__file__).resolve().parent.parent
REF_DIR = REPO_ROOT / "reference"
MANIFEST_PATH = REPO_ROOT / "data_manifest.yaml"


def _make_full_day_candles(d: dt.date, open_hhmm="09:15", close_hhmm="15:30", base_price=25000.0,
                           vol=100, oi=5000, newest_first=False) -> list:
    oh, om = (int(x) for x in open_hhmm.split(":"))
    ch, cm = (int(x) for x in close_hhmm.split(":"))
    n_bars = (ch * 60 + cm) - (oh * 60 + om)
    t0 = dt.datetime(d.year, d.month, d.day, oh, om, tzinfo=IST)
    candles = []
    for i in range(n_bars):
        ts = (t0 + dt.timedelta(minutes=i)).isoformat()
        p = round(base_price + (i % 10) * 0.5, 2)
        candles.append([ts, p, p + 2.0, p - 2.0, p + 0.5, vol, oi + i * 10])
    if newest_first:
        candles.reverse()
    return candles


def _gz_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj).encode("utf-8"), mtime=0)


class FakeTransport:
    """In-memory mocked HTTP transport for deterministic testing with zero network access."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def get(self, url, headers=None, timeout=None) -> RawResponse:
        self.requests.append((url, headers))
        return self.handler(url, headers or {})


def _build_fixture_transport(mode="A", oldest_opt_date="2026-03-01", bse_ok=True):
    """Build a FakeTransport for probe and end-to-end synthetic fixture tests."""
    exp_ms = int(dt.datetime(2026, 11, 24, 15, 30, tzinfo=IST).timestamp() * 1000)
    nse_master = [
        {"segment": "NSE_INDEX", "name": "Nifty 50", "trading_symbol": "Nifty 50",
         "instrument_key": "NSE_INDEX|Nifty 50"},
        {"segment": "NSE_INDEX", "name": "Nifty Bank", "trading_symbol": "Nifty Bank",
         "instrument_key": "NSE_INDEX|Nifty Bank"},
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_INDEX|Nifty 50",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|90001", "expiry": exp_ms,
         "trading_symbol": "NIFTY26NOVFUT", "lot_size": 65, "freeze_quantity": 1800, "tick_size": 0.05},
        {"segment": "NSE_FO", "instrument_type": "CE", "underlying_key": "NSE_INDEX|Nifty 50",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|90002", "expiry": exp_ms,
         "strike_price": 25000.0, "trading_symbol": "NIFTY25000CE", "lot_size": 65,
         "freeze_quantity": 1800, "tick_size": 0.05},
        {"segment": "NSE_FO", "instrument_type": "PE", "underlying_key": "NSE_INDEX|Nifty 50",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|90003", "expiry": exp_ms,
         "strike_price": 25000.0, "trading_symbol": "NIFTY25000PE", "lot_size": 65,
         "freeze_quantity": 1800, "tick_size": 0.05},
        {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "HDFCBANK",
         "instrument_key": "NSE_EQ|INE040A01034"},
    ]
    bse_master = [
        {"segment": "BSE_INDEX", "name": "SENSEX", "trading_symbol": "SENSEX",
         "instrument_key": "BSE_INDEX|SENSEX"},
        {"segment": "BSE_FO", "instrument_type": "CE", "underlying_key": "BSE_INDEX|SENSEX",
         "underlying_symbol": "SENSEX", "instrument_key": "BSE_FO|80001", "expiry": exp_ms,
         "strike_price": 82000.0, "trading_symbol": "SENSEX82000CE", "lot_size": 20,
         "freeze_quantity": 1000, "tick_size": 0.05},
    ]
    global_master = [
        {"name": "USD INR", "trading_symbol": "USDINR", "instrument_key": "GLOBAL|USDINR"},
        {"name": "GIFT NIFTY", "trading_symbol": "GIFT NIFTY", "instrument_key": "GLOBAL|GIFTNIFTY"},
    ]
    masters_gz = {
        MASTER_URLS["NSE"]: _gz_json(nse_master),
        MASTER_URLS["BSE"]: _gz_json(bse_master),
        MASTER_URLS["global"]: _gz_json(global_master),
    }

    def handler(url: str, headers: dict) -> RawResponse:
        if url in masters_gz:
            return RawResponse(200, {"X-RateLimit-Limit": "250"}, masters_gz[url])
        if mode == "C":
            return RawResponse(200, {}, json.dumps({"status": "success", "data": {"candles": []}}).encode())
        if "/v2/expired-instruments/" in url:
            if mode == "UDAPI1149":
                body = {"status": "error", "errors": [{"errorCode": "UDAPI1149", "message": "Plus required"}]}
                return RawResponse(403, {}, json.dumps(body).encode())
            if mode == "B_EMPTY":
                return RawResponse(200, {}, json.dumps({"status": "success", "data": []}).encode())
            if "BSE_INDEX" in url and not bse_ok:
                return RawResponse(200, {}, json.dumps({"status": "success", "data": []}).encode())
            if "/expiries" in url:
                exps = [oldest_opt_date, "2026-05-12", "2026-06-23", "2026-11-09"]
                return RawResponse(200, {"X-RateLimit-Remaining": "240"},
                                   json.dumps({"status": "success", "data": exps}).encode())
            if "/option/contract" in url:
                m = re.search(r"expiry_date=([0-9-]+)", url)
                exp_s = m.group(1) if m else "2026-06-23"
                ddmmyyyy = dt.date.fromisoformat(exp_s).strftime("%d-%m-%Y")
                seg = "BSE_FO" if "BSE_INDEX" in url else "NSE_FO"
                c_list = []
                for idx, (k_val, ot) in enumerate([(24950.0, "CE"), (24950.0, "PE"),
                                                   (25000.0, "CE"), (25000.0, "PE")]):
                    c_list.append({
                        "instrument_key": f"{seg}|5380{idx}|{ddmmyyyy}",
                        "expiry_date": exp_s,
                        "strike_price": k_val,
                        "instrument_type": ot,
                        "trading_symbol": f"NIFTY{int(k_val)}{ot}",
                    })
                return RawResponse(200, {}, json.dumps({"status": "success", "data": c_list}).encode())
            if "/future/contract" in url:
                m = re.search(r"expiry_date=([0-9-]+)", url)
                exp_s = m.group(1) if m else "2026-06-23"
                ddmmyyyy = dt.date.fromisoformat(exp_s).strftime("%d-%m-%Y")
                f_list = [{"instrument_key": f"NSE_FO|63800|{ddmmyyyy}", "expiry_date": exp_s,
                           "instrument_type": "FUT", "trading_symbol": "NIFTYFUT"}]
                return RawResponse(200, {}, json.dumps({"status": "success", "data": f_list}).encode())
            if "/historical-candle/" in url:
                parts = url.split("/")
                from_s, to_s, intv = parts[-1], parts[-2], parts[-3]
                f_d, t_d = to_date(from_s), to_date(to_s)
                if f_d < to_date(oldest_opt_date):
                    return RawResponse(200, {}, json.dumps({"status": "success", "data": {"candles": []}}).encode())
                if intv == "day":
                    c = [[f"{iso(t_d)}T00:00:00+05:30", 120.0, 130.0, 110.0, 125.0, 5000, 8840]]
                    return RawResponse(200, {}, json.dumps({"status": "success", "data": {"candles": c}}).encode())
                cal = load_calendar(REF_DIR)
                all_c = []
                cur = f_d
                while cur <= t_d:
                    if cal.is_trading_day(cur):
                        close_t = "15:40" if cur >= dt.date(2026, 8, 3) else "15:30"
                        all_c.extend(_make_full_day_candles(cur, "09:15", close_t, base_price=120.0,
                                                            vol=50, oi=5000, newest_first=True))
                    cur += dt.timedelta(days=1)
                return RawResponse(200, {}, json.dumps({"status": "success", "data": {"candles": all_c}}).encode())

        if "/v3/historical-candle/" in url:
            parts = url.split("/")
            from_s, to_s, intv, unit = parts[-1], parts[-2], int(parts[-3]), parts[-4]
            f_d, t_d = to_date(from_s), to_date(to_s)
            if unit == "minutes" and intv <= 15 and (t_d - f_d).days >= 31:
                err = {"status": "error", "errors": [{"errorCode": "UDAPI100060", "message": "Invalid date range"}]}
                return RawResponse(400, {}, json.dumps(err).encode())
            if unit == "days":
                cal = load_calendar(REF_DIR)
                c = []
                cur = f_d
                while cur <= t_d:
                    if cal.is_trading_day(cur):
                        c.append([f"{iso(cur)}T00:00:00+05:30", 14.0, 15.0, 13.5, 14.2, 0, 0])
                    cur += dt.timedelta(days=1)
                return RawResponse(200, {}, json.dumps({"status": "success", "data": {"candles": c}}).encode())
            cal = load_calendar(REF_DIR)
            all_c = []
            cur = f_d
            while cur <= t_d:
                if cal.is_trading_day(cur):
                    is_fo = "NSE_FO" in url
                    close_t = "15:40" if (is_fo and cur >= dt.date(2026, 8, 3)) else "15:30"
                    base_p = 14.0 if "India%20VIX" in url else 25000.0
                    v_val = 100 if is_fo else 0
                    all_c.extend(_make_full_day_candles(cur, "09:15", close_t, base_price=base_p, vol=v_val, oi=0))
                cur += dt.timedelta(days=1)
            return RawResponse(200, {"X-RateLimit-Limit": "250"},
                               json.dumps({"status": "success", "data": {"candles": all_c}}).encode())

        return RawResponse(404, {}, b"{}")

    return FakeTransport(handler)


class TestUpdlSuite(unittest.TestCase):

    def test_T01_range_splitting(self):
        cal = load_calendar(REF_DIR)
        settings = load_settings(MANIFEST_PATH)
        tdays = cal.trading_days(settings.test_start, settings.test_end)
        v3_chunks = chunk_days(tdays, 28)
        self.assertGreater(len(v3_chunks), 5)
        for frm, to, _ in v3_chunks:
            self.assertLessEqual((to - frm).days + 1, 28)
        self.assertEqual(v3_chunks[-1][1], settings.test_end)

        span_days = cal.trading_days("2026-06-01", "2026-08-01")
        exp_chunks = contract_chunks(span_days, set(), 1, cal)
        self.assertEqual(len(exp_chunks), len(span_days))
        for frm, to, _ in exp_chunks:
            self.assertEqual(frm, to)
            self.assertLess(frm.weekday(), 5)
            self.assertFalse(cal.is_holiday(frm))

    def test_T02_expected_bars_and_no_hardcoded_constant(self):
        cal = load_calendar(REF_DIR)
        self.assertEqual(cal.expected_bars("nse_index_fo", "2026-07-31"), 375)
        self.assertEqual(cal.expected_bars("nse_index_fo", "2026-08-03"), 385)
        self.assertEqual(cal.expected_bars("cash_fo_stock", "2026-08-03"), 360)
        self.assertEqual(cal.expected_bars("cash_non_fo_stock", "2026-08-03"), 375)

        rx = re.compile(r"\b375\b")
        hits = []
        for py in (REPO_ROOT / "updl").rglob("*.py"):
            if rx.search(py.read_text(encoding="utf-8")):
                hits.append(py.name)
        self.assertEqual(hits, [])

    def test_T03_trading_day_counts_and_holidays(self):
        cal = load_calendar(REF_DIR)
        self.assertEqual(cal.count_trading_days("2026-05-11", "2026-11-09"), 126)
        self.assertEqual(cal.session_type("2026-11-08"), "MUHURAT")
        self.assertFalse(cal.is_trading_day("2026-11-08"))
        self.assertTrue(cal.is_holiday("2026-11-10"))
        self.assertFalse(cal.is_trading_day("2026-11-10"))
        self.assertEqual(cal.count_trading_days("2026-10-05", "2026-11-09"), 25)

    def test_T04_key_handling_sorting_and_coverage(self):
        self.assertEqual(quote_key("NSE_FO|53806|24-04-2025"), "NSE_FO%7C53806%7C24-04-2025")
        self.assertEqual(quote_key("NSE_INDEX|Nifty 50"), "NSE_INDEX%7CNifty%2050")

        payload = {"data": {"candles": [
            ["2026-05-11T09:17:00+05:30", 102, 103, 101, 102, 10, 100],
            ["2026-05-11T09:15:00+05:30", 100, 101, 99, 100, 10, 100],
            ["2026-05-11T09:16:00+05:30", 101, 102, 100, 101, 10, 100],
        ]}}
        rows, bad = parse_candles(payload)
        self.assertEqual(bad, 0)
        self.assertEqual([r["ts"] for r in rows], [
            "2026-05-11T09:15:00+05:30",
            "2026-05-11T09:16:00+05:30",
            "2026-05-11T09:17:00+05:30",
        ])

        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            st = State(tmp)
            try:
                key = cov_key("NSE_FO|123|26-05-2026", "1m")
                days = cal.trading_days("2026-05-11", "2026-05-15")
                st.mark_coverage(key, days[:2], "D06", DONE, "dummy.json.gz")
                cov = st.covered_dates(key)
                chks_1 = contract_chunks(days, cov, 1, cal)
                chks_28 = contract_chunks(days, cov, 28, cal)
                self.assertEqual([c[0] for c in chks_1], days[2:])
                self.assertEqual(chks_28[0][0], days[2])
            finally:
                st.close()

    def test_T05_point_in_time_contract_selection(self):
        exp = dt.date(2026, 6, 16)
        c1 = Contract("NSE_FO|1|16-06-2026", exp, 25000.0, "CE", "expired")
        c2 = Contract("NSE_FO|2|16-06-2026", exp, 25000.0, "PE", "expired")
        by_exp = {exp: [c1, c2]}
        stats = DayStats(low=24900, high=25100, close=25000, vix_high=14.0)
        first_seen = {"NSE_FO|1|16-06-2026": dt.date(2026, 6, 10)}
        sel_early = select_day_options("2026-06-05", [exp], by_exp, stats, 6, 0.6, 50, first_seen=first_seen)
        self.assertEqual([c.instrument_key for c in sel_early], ["NSE_FO|2|16-06-2026"])

        logs = []
        pit = filter_contracts_point_in_time(
            expired_list=[{"instrument_key": "NSE_FO|10|26-05-2026", "expiry_date": "2026-05-26",
                           "strike": 25000, "option_type": "CE"}],
            master_list=[
                {"instrument_key": "NSE_FO|999", "expiry_date": "2026-05-26", "strike": 25000, "option_type": "PE"},
                {"instrument_key": "NSE_FO|888", "expiry_date": "2026-11-23", "strike": 57000, "option_type": "CE"},
            ],
            ref_today=dt.date(2026, 11, 9),
            log=lambda ev, **kw: logs.append((ev, kw)),
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][0], "MASTER_ONLY")
        self.assertEqual([c.instrument_key for c in pit[dt.date(2026, 5, 26)]], ["NSE_FO|10|26-05-2026"])
        self.assertEqual(pit[dt.date(2026, 11, 23)][0].source, "listed")

        exps = [dt.date(2026, 5, 12), dt.date(2026, 5, 19)]
        self.assertEqual(expiries_for_day("2026-05-12", exps), exps)
        self.assertEqual(expiries_for_day("2026-05-11", exps), [dt.date(2026, 5, 12)])

        bn_exps = [dt.date(2026, 5, 5), dt.date(2026, 5, 12), dt.date(2026, 5, 26),
                   dt.date(2026, 6, 2), dt.date(2026, 6, 30), dt.date(2026, 11, 23)]
        self.assertEqual(monthly_expiries(bn_exps),
                         [dt.date(2026, 5, 26), dt.date(2026, 6, 30), dt.date(2026, 11, 23)])

    def test_T06_backoff_circuit_breaker_401_and_ctrl_c(self):
        sleeps = []
        calls = 0

        def fail_500(url, headers):
            nonlocal calls
            calls += 1
            return RawResponse(503, {}, b'{"status":"error","message":"busy"}')

        lim = RateLimiter(calls_per_min=6000, sleep=lambda s: None)
        cli = HttpClient(transport=FakeTransport(fail_500), limiter=lim,
                         sleep=lambda s: sleeps.append(round(s, 2)), rng=lambda: 0.0)
        with self.assertRaises(RetryExhausted):
            cli.get(v3_candle_path("NSE_INDEX|Nifty 50", "days", 1, "2026-05-11", "2026-05-11"))
        self.assertEqual(calls, 7)
        self.assertEqual(sleeps, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0])

        pauses = []
        c429 = 0

        def handler_429(url, headers):
            nonlocal c429
            c429 += 1
            if c429 <= 3:
                return RawResponse(429, {}, b"{}")
            return RawResponse(200, {}, b'{"status":"success","data":{"candles":[]}}')

        lim2 = RateLimiter(calls_per_min=30.0, sleep=lambda s: None)
        cli2 = HttpClient(transport=FakeTransport(handler_429), limiter=lim2,
                          sleep=lambda s: pauses.append(s), rng=lambda: 0.0)
        cli2.get(v3_candle_path("NSE_INDEX|Nifty 50", "days", 1, "2026-05-11", "2026-05-11"))
        self.assertIn(600, pauses)
        self.assertEqual(lim2.calls_per_min, 15.0)

        cli401 = HttpClient(transport=FakeTransport(lambda u, h: RawResponse(401, {}, b"{}")),
                            limiter=lim, sleep=lambda s: None)
        with self.assertRaises(TokenExpired) as ctx:
            cli401.get(v3_candle_path("NSE_INDEX|Nifty 50", "days", 1, "2026-05-11", "2026-05-11"))
        self.assertEqual(ctx.exception.exit_code, EXIT_TOKEN_EXPIRED)

    def test_T07_resume_after_hard_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            wdir = Path(tmp) / "work"
            wdir.mkdir(parents=True, exist_ok=True)
            helper = Path(tmp) / "child_kill.py"
            helper.write_text(f"""
import sys, time
sys.path.insert(0, {repr(str(REPO_ROOT))})
from updl.state import State, DONE
from updl.store import write_raw
wdir = {repr(str(wdir))}
st = State(wdir + "/state")
info = write_raw(wdir + "/raw", "D02", "NSE_INDEX|Nifty 50", "2026-05-11", "2026-05-11", "r1",
                 b'{{"status":"success","data":{{"candles":[]}}}}', {{"table":"spot_1d"}})
st.set_chunk("D02", "NSE_INDEX|Nifty 50", "2026-05-11", "2026-05-11", DONE,
             raw_path=info["path"], sha256=info["sha256"], run_id="r1", table_name="spot_1d", underlying="NIFTY")
st.mark_coverage("NSE_INDEX|Nifty 50#1d", ["2026-05-11"], "D02", DONE, info["path"])
open(wdir + "/raw/partial_chunk.json.gz.tmp", "wb").write(b"partial")
sys.stdout.write("READY\\n")
sys.stdout.flush()
time.sleep(30)
""", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.PIPE, text=True)
            try:
                line = proc.stdout.readline().strip()
                self.assertEqual(line, "READY")
                proc.kill()
                proc.wait()
            finally:
                if proc.stdout:
                    proc.stdout.close()

            self.assertTrue((wdir / "raw" / "partial_chunk.json.gz.tmp").exists())
            from updl.store import cleanup_temp_files
            removed = cleanup_temp_files(wdir)
            self.assertEqual(removed, 1)
            self.assertFalse((wdir / "raw" / "partial_chunk.json.gz.tmp").exists())

            st = State(wdir / "state")
            try:
                self.assertIn(dt.date(2026, 5, 11), st.covered_dates("NSE_INDEX|Nifty 50#1d"))
                self.assertEqual(st.chunk_counts().get(DONE), 1)
            finally:
                st.close()

    def test_T08_corrupted_file_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-05-11", test_end="2026-05-12")
            settings.ensure_dirs()
            from updl.store import write_raw
            info = write_raw(settings.raw_dir, "D01", "NSE_INDEX|Nifty 50", "2026-05-11", "2026-05-11", "r1",
                             b'{"status":"success","data":{"candles":[]}}',
                             {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY"})
            st = State(settings.state_dir)
            st.set_chunk("D01", "NSE_INDEX|Nifty 50", "2026-05-11", "2026-05-11", DONE,
                         raw_path=info["path"], sha256=info["sha256"], run_id="r1",
                         table_name="spot_1m", underlying="NIFTY")
            st.close()

            raw_p = Path(info["path"])
            b = bytearray(raw_p.read_bytes())
            b[-1] ^= 0xFF
            raw_p.write_bytes(bytes(b))

            with self.assertRaises(CorruptFile) as ctx:
                run_validate(settings)
            self.assertEqual(ctx.exception.exit_code, EXIT_CORRUPT_FILE)
            self.assertIn(raw_p.name, str(ctx.exception))

    def test_T09_secret_scan_allowlist_and_word_grep(self):
        forbidden_rx = re.compile(
            r"\b(orders?|place_order|modify_order|cancel_order|funds?|portfolio|positions?|gtt|payout|login)\b",
            re.IGNORECASE
        )
        for py in (REPO_ROOT / "updl").rglob("*.py"):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                code_part = line.split("#", 1)[0]
                self.assertIsNone(forbidden_rx.search(code_part), f"Forbidden word in {py.name}:{lineno}: {line}")
                self.assertNotIn("authorization/token", line.lower())

        fake_tok = "SECRET_ANALYTICS_TOKEN_VALUE_1234567890_ABCDEF"
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-05-11",
                                     test_end="2026-05-12", items="D12,D01,D03")
            tokens = Tokens(analytics=fake_tok, access=fake_tok)
            tr = _build_fixture_transport("A")
            lim = RateLimiter(calls_per_min=6000, sleep=lambda s: None)
            from updl.runlog import RunLog
            rlog = RunLog(logs_dir=settings.logs_dir, secrets=tokens.secret_values())
            cli = HttpClient(tokens=tokens, transport=tr, limiter=lim, logger=rlog, sleep=lambda s: None)

            run_probe(settings, cli, probe_today=dt.date(2026, 11, 11))
            cli.budget = None
            run_download(settings, cli, run_id=rlog.run_id, log=rlog, include_warmup=False,
                         secrets=tokens.secret_values())
            compact(settings, load_calendar(REF_DIR), log=rlog)
            run_validate(settings, log=rlog)

            for seen in cli.seen_paths:
                self.assertTrue(path_allowed(seen) or seen in MASTER_URLS.values(), f"Disallowed path: {seen}")

            hits = scan_tree(tmp, secrets=[fake_tok], skip_suffixes=(".parquet", ".sqlite3"))
            self.assertEqual(hits, [])
            st = State(settings.state_dir)
            try:
                from updl.secretscan import scan_text
                self.assertEqual(scan_text(st.dump_text(), secrets=[fake_tok]), [])
            finally:
                st.close()

    def test_T10_dry_run_makes_zero_calls_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            wdir = Path(tmp) / "nonexistent_workdir"
            from updl.cli import main
            buf = io.StringIO()
            old_out = sys.stdout
            sys.stdout = buf
            try:
                rc = main(["download", "--dry-run", "--plan", "A",
                           "--manifest", str(MANIFEST_PATH), "--workdir", str(wdir)])
            finally:
                sys.stdout = old_out
            self.assertEqual(rc, EXIT_OK)
            self.assertFalse(wdir.exists())
            self.assertIn("Total API calls:", buf.getvalue())

    def test_T11_data_rights_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            settings.set_param("DATA_RIGHTS", "NOT_ALLOWED")
            cli = HttpClient(transport=FakeTransport(lambda u, h: (_ for _ in ()).throw(RuntimeError("called"))))
            from updl.errors import RightsNotAllowed
            with self.assertRaises(RightsNotAllowed) as ctx:
                run_probe(settings, cli)
            self.assertEqual(ctx.exception.exit_code, EXIT_RIGHTS_NOT_ALLOWED)

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-05-11",
                                     test_end="2026-05-11", items="D12,D01")
            tr = _build_fixture_transport("A")
            cli = HttpClient(transport=tr, limiter=RateLimiter(6000, sleep=lambda s: None), sleep=lambda s: None)
            run_probe(settings, cli, probe_today=dt.date(2026, 11, 11))
            cli.budget = None
            run_download(settings, cli, include_warmup=False)
            self.assertTrue((settings.store_dir / "DATA_RIGHTS_UNVERIFIED.txt").exists())

            settings.set_param("DATA_RIGHTS", "MINIMAL")
            res = compact(settings, load_calendar(REF_DIR))
            self.assertGreater(res["raw_deleted"], 0)
            gz_files = [p for p in (settings.raw_dir / "D01").rglob("*.json.gz")]
            self.assertEqual(gz_files, [])

    def test_T12_probe_plan_selection_and_budget(self):
        cases = [
            ("A", "2026-02-03", "A", None),
            ("A", "2026-06-15", "A_PARTIAL", PLAN_A_PARTIAL),
            ("UDAPI1149", "2026-02-03", "B", PLAN_B),
            ("B_EMPTY", "2026-02-03", "B", PLAN_B),
            ("C", "2026-02-03", "C", PLAN_C),
        ]
        for mode, oldest, expected_plan, expected_msg in cases:
            with tempfile.TemporaryDirectory() as tmp:
                settings = load_settings(MANIFEST_PATH, workdir=tmp)
                tr = _build_fixture_transport(mode=mode, oldest_opt_date=oldest)
                cli = HttpClient(transport=tr, limiter=RateLimiter(6000, sleep=lambda s: None), sleep=lambda s: None)
                buf = io.StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    rep = run_probe(settings, cli, probe_today=dt.date(2026, 11, 11))
                finally:
                    sys.stdout = old
                self.assertEqual(rep["selected_plan"], expected_plan)
                self.assertLessEqual(cli.calls_made, PROBE_BUDGET)
                if expected_msg:
                    self.assertIn(expected_msg, buf.getvalue())

    def test_T13_validator_edge_cases(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-08-03", test_end="2026-08-03")
            settings.ensure_dirs()
            d = dt.date(2026, 8, 3)

            c_90 = _make_full_day_candles(d, "09:15", "15:30")[:330]
            rows_90, _ = parse_candles({"data": {"candles": c_90}})
            df_90 = rows_to_frame(rows_90, {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50",
                                            "underlying": "NIFTY", "option_type": "SPOT"}, cal, "r1", "0" * 64)
            write_parquet(df_90, part_path(settings.store_dir, "dev", "spot_1m", "NIFTY", d))

            c_bse = _make_full_day_candles(d, "09:15", "15:30")
            rows_bse, _ = parse_candles({"data": {"candles": c_bse}})
            df_bse = rows_to_frame(rows_bse, {"table": "spot_1m", "instrument_key": "BSE_INDEX|SENSEX",
                                              "underlying": "SENSEX", "option_type": "SPOT"}, cal, "r1", "0" * 64)
            write_parquet(df_bse, part_path(settings.store_dir, "dev", "spot_1m", "SENSEX", d))

            bad_opt = [
                ["2026-08-03T09:15:00+05:30", 100.0, 95.0, 105.0, 100.0, None, 1000],
                ["2026-08-03T15:41:00+05:30", 100.0, 102.0, 99.0, 101.0, 10, 1000],
            ]
            rows_opt, _ = parse_candles({"data": {"candles": bad_opt}})
            self.assertIsNone(rows_opt[0]["v"])
            df_opt = rows_to_frame(rows_opt, {"table": "opt_1m", "instrument_key": "NSE_FO|1",
                                              "underlying": "BANKNIFTY", "expiry_date": "2026-08-25",
                                              "strike": 57000, "option_type": "CE"}, cal, "r1", "0" * 64)
            write_parquet(df_opt, part_path(settings.store_dir, "dev", "opt_1m", "BANKNIFTY", d))
            write_parquet(df_bse.assign(underlying="BANKNIFTY"),
                          part_path(settings.store_dir, "dev", "spot_1m", "BANKNIFTY", d))

            res = run_validate(settings, cal)
            by_u = {r["underlying"]: r for r in res["health_rows"]}
            self.assertEqual(by_u["NIFTY"]["status"], "NOT OK")
            self.assertIn("LOW_BAR_COUNT", by_u["NIFTY"]["reasons"])
            self.assertEqual(by_u["SENSEX"]["status"], "PROVISIONAL")
            self.assertEqual(by_u["BANKNIFTY"]["status"], "NOT OK")
            self.assertIn("OUTSIDE_SESSION", by_u["BANKNIFTY"]["reasons"])
            self.assertIn("OHLC_VIOLATION", by_u["BANKNIFTY"]["reasons"])
            self.assertIn("NULL_VOLUME", by_u["BANKNIFTY"]["reasons"])

    def test_T14_derived_tables_iv_solver_and_baselines(self):
        cal = load_calendar(REF_DIR)
        d = dt.date(2026, 5, 11)
        c_full = _make_full_day_candles(d, "09:15", "09:21")
        del c_full[1]
        rows, _ = parse_candles({"data": {"candles": c_full}})
        df_1m = rows_to_frame(rows, {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50",
                                     "underlying": "NIFTY", "option_type": "SPOT"}, cal, "r1", "0" * 64)
        b3 = derive_MultiMinute_bars(df_1m, 3)
        self.assertTrue(bool(b3.iloc[0]["is_partial"]))
        self.assertFalse(bool(b3.iloc[1]["is_partial"]))
        self.assertEqual(b3.iloc[1]["available_from_ts"], at_ist(d, "09:21"))

        F, T_yrs = 25050.0, 10.0 / 365.0
        for i in range(20):
            true_iv = 0.10 + i * 0.015
            K = 24800.0 + (i % 5) * 100.0
            ot = "CE" if i % 2 == 0 else "PE"
            p = black76_price(F, K, T_yrs, true_iv, ot)
            sol = solve_black76_iv(p, F, K, T_yrs, ot)
            self.assertEqual(sol["iv_status"], "OK")
            self.assertAlmostEqual(sol["iv_DERIVED"], true_iv, delta=0.001)

        bad_sol = solve_black76_iv(10.0, 25100.0, 25000.0, T_yrs, "CE")
        self.assertEqual(bad_sol["iv_status"], "BELOW_INTRINSIC")

    def test_T15_holdout_seal_and_one_time_open(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            settings.ensure_dirs()
            d_dev = dt.date(2026, 5, 11)
            d_ho = dt.date(2026, 10, 6)
            for d_cur, part in ((d_dev, "dev"), (d_ho, "holdout")):
                rows, _ = parse_candles({"data": {"candles": _make_full_day_candles(d_cur, "09:15", "09:20")}})
                df = rows_to_frame(rows, {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50",
                                          "underlying": "NIFTY", "option_type": "SPOT"}, cal, "r1", "0" * 64)
                write_parquet(df, part_path(settings.store_dir, part, "spot_1m", "NIFTY", d_cur))

            run_handoff(settings, synthetic_fixture=True)

            with self.assertRaises(HoldoutRefused):
                load_dev(settings.handoff_dir / "store" / "holdout", "spot_1m")
            with self.assertRaises(HoldoutRefused):
                load_dev(settings.handoff_dir, "spot_1m", end="2026-10-06", holdout_start=settings.holdout_start)

            with self.assertRaises(HoldoutRefused):
                load_holdout(settings.handoff_dir, "spot_1m", environ={})

            df_h = load_holdout(settings.handoff_dir, "spot_1m", environ={"OPEN_HOLDOUT_ONCE": "1"})
            self.assertEqual(len(df_h), 5)
            log_p = settings.handoff_dir / "holdout_log.jsonl"
            self.assertTrue(log_p.exists())
            entry = json.loads(log_p.read_text(encoding="utf-8").strip())
            self.assertEqual(len(entry["hash"]), 64)

            with self.assertRaises(HoldoutAlreadyOpened) as ctx:
                load_holdout(settings.handoff_dir, "spot_1m", environ={"OPEN_HOLDOUT_ONCE": "1"})
            self.assertEqual(ctx.exception.exit_code, EXIT_HOLDOUT_OPENED)

            for hp in list_parts(settings.handoff_dir / "store", "holdout"):
                self.assertFalse(os.access(hp, os.W_OK) if os.geteuid() != 0 else (hp.stat().st_mode & 0o222) == 0)

    def test_T16_no_filling_allowed(self):
        with self.assertRaises(FillingForbidden):
            forbid_fill(pd.DataFrame())
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_no_filled_rows(Path(tmp)), [])

    def test_T17_end_to_end_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-05-11",
                                     test_end="2026-05-12", items="D12,D01,D02,D03,D04,D07,D06")
            settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            tr = _build_fixture_transport("A")
            cli = HttpClient(transport=tr, limiter=RateLimiter(6000, sleep=lambda s: None), sleep=lambda s: None)
            cal = load_calendar(REF_DIR)

            run_probe(settings, cli, calendar=cal, probe_today=dt.date(2026, 11, 11))
            cli.budget = None
            run_download(settings, cli, calendar=cal, include_warmup=False)
            compact(settings, cal)
            run_validate(settings, cal)
            run_derive(settings, cal)
            run_handoff(settings, synthetic_fixture=True)
            v_res = run_verify(settings)

            self.assertEqual(v_res["status"], "OK")
            for fn in ("SCHEMA.md", "MANIFEST.sha256", "HOLDOUT_SEALED.json",
                       "README_OWNER_HINGLISH.md", "SYNTHETIC_FIXTURE_NOT_A_RESULT.txt"):
                self.assertTrue((settings.handoff_dir / fn).exists(), f"Missing {fn}")

    def test_T18_causality_truncation_invariance(self):
        cal = load_calendar(REF_DIR)
        d1, d2 = dt.date(2026, 5, 11), dt.date(2026, 5, 12)
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            s_full = load_settings(MANIFEST_PATH, workdir=tmp1, test_start="2026-05-11", test_end="2026-05-12")
            s_trunc = load_settings(MANIFEST_PATH, workdir=tmp2, test_start="2026-05-11", test_end="2026-05-11")
            for s, days in ((s_full, [d1, d2]), (s_trunc, [d1])):
                for d_cur in days:
                    c_s = _make_full_day_candles(d_cur, "09:15", "09:30", base_price=25000.0)
                    r_s, _ = parse_candles({"data": {"candles": c_s}})
                    df_s = rows_to_frame(r_s, {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50",
                                               "underlying": "NIFTY", "option_type": "SPOT"}, cal, "r1", "0" * 64)
                    write_parquet(df_s, part_path(s.store_dir, "dev", "spot_1m", "NIFTY", d_cur))

                    c_ce = _make_full_day_candles(d_cur, "09:15", "09:30", base_price=150.0, vol=50, oi=1000)
                    c_pe = _make_full_day_candles(d_cur, "09:15", "09:30", base_price=140.0, vol=60, oi=1200)
                    r_ce, _ = parse_candles({"data": {"candles": c_ce}})
                    r_pe, _ = parse_candles({"data": {"candles": c_pe}})
                    df_o = pd.concat([
                        rows_to_frame(r_ce, {"table": "opt_1m", "instrument_key": "NSE_FO|1", "underlying": "NIFTY",
                                             "expiry_date": "2026-05-19", "strike": 25000, "option_type": "CE"},
                                      cal, "r1", "0" * 64),
                        rows_to_frame(r_pe, {"table": "opt_1m", "instrument_key": "NSE_FO|2", "underlying": "NIFTY",
                                             "expiry_date": "2026-05-19", "strike": 25000, "option_type": "PE"},
                                      cal, "r1", "0" * 64),
                    ], ignore_index=True)
                    write_parquet(df_o, part_path(s.store_dir, "dev", "opt_1m", "NIFTY", d_cur))
                run_derive(s, cal)

            for tbl in ("bars_3m", "bars_15m", "chain_1m", "iv_greeks_1m", "chain_metrics_1m"):
                p1 = part_path(s_full.store_dir, "dev", tbl, "NIFTY", d1)
                p2 = part_path(s_trunc.store_dir, "dev", tbl, "NIFTY", d1)
                pd.testing.assert_frame_equal(read_parquet(p1), read_parquet(p2))

        dates_300 = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(300)]
        vix_df1 = pd.DataFrame({"trading_date": dates_300, "c": [12.0 + (i % 10) * 0.5 for i in range(300)]})
        vix_df2 = vix_df1.copy()
        vix_df2.loc[vix_df2["trading_date"] == dates_300[-1], "c"] = 99.9
        res1 = compute_ivp_vix_for_day(vix_df1, dates_300[-1])
        res2 = compute_ivp_vix_for_day(vix_df2, dates_300[-1])
        self.assertEqual(res1["ivp_vix_PROXY"], res2["ivp_vix_PROXY"])

    def test_T19_delta_aware_band_formula(self):
        m_22 = band_margin_points(step=100, spot_close=57000, vix_day_high=14.0, cal_dte=22,
                                  margin_steps=6, delta_k=0.6)
        self.assertGreater(m_22, 600.0)
        strikes = [55000 + i * 100 for i in range(41)]
        band = strikes_in_band(strikes, 56800, 57200, m_22)
        self.assertIn(56000.0, band)

        m_1 = band_margin_points(step=100, spot_close=57000, vix_day_high=14.0, cal_dte=1,
                                 margin_steps=6, delta_k=0.6)
        self.assertEqual(m_1, 600.0)

    def test_T20_master_expiry_ist_and_weekday_mismatch(self):
        dt_ist = dt.datetime(2026, 5, 12, 1, 0, tzinfo=IST)
        ms = int(dt_ist.timestamp() * 1000)
        self.assertEqual(expiry_date_of({"expiry": ms}), dt.date(2026, 5, 12))

        logs = []
        ms_mon = int(dt.datetime(2026, 5, 11, 15, 30, tzinfo=IST).timestamp() * 1000)
        out = listed_contracts(
            [{"underlying_key": "NSE_INDEX|Nifty 50", "instrument_type": "CE",
              "instrument_key": "NSE_FO|1", "expiry": ms_mon, "strike_price": 25000}],
            "NSE_INDEX|Nifty 50", logger=lambda ev, **kw: logs.append((ev, kw)), rule="weekly_tuesday"
        )
        self.assertEqual(out[0]["expiry_date"], dt.date(2026, 5, 11))
        self.assertEqual(logs[0][0], "weekday_mismatch")

    def test_T21_index_vs_option_stale_flag(self):
        cal = load_calendar(REF_DIR)
        rows, _ = parse_candles({"data": {"candles": [["2026-05-11T09:15:00+05:30", 100, 101, 99, 100, 0, 0]]}})
        for tbl in ("spot_1m", "vix_1m", "global_1m"):
            df_idx = rows_to_frame(rows, {"table": tbl, "instrument_key": "K", "underlying": "U"}, cal, "r1", "0" * 64)
            self.assertFalse(bool(df_idx.iloc[0]["has_volume"]))
            self.assertFalse(bool(df_idx.iloc[0]["is_stale"]))

        df_opt = rows_to_frame(rows, {"table": "opt_1m", "instrument_key": "NSE_FO|1", "underlying": "NIFTY",
                                      "expiry_date": "2026-05-12", "strike": 25000, "option_type": "CE"},
                               cal, "r1", "0" * 64)
        self.assertTrue(bool(df_opt.iloc[0]["has_volume"]))
        self.assertTrue(bool(df_opt.iloc[0]["is_stale"]))

    def test_T22_bhavcopy_reconciliation(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, external_dir=Path(tmp) / "ext")
            settings.ensure_dirs()
            res_empty = reconcile_bhavcopy(settings, cal)
            self.assertEqual(res_empty["status"], "NA-DATA")

            d = dt.date(2026, 5, 11)
            rows, _ = parse_candles({"data": {"candles": [["2026-05-11T15:29:00+05:30", 100, 105, 95, 102, 100, 5000]]}})
            df_opt = rows_to_frame(rows, {"table": "opt_1m", "instrument_key": "NSE_FO|1", "underlying": "NIFTY",
                                          "expiry_date": "2026-05-12", "strike": 25000, "option_type": "CE"},
                                   cal, "r1", "0" * 64)
            write_parquet(df_opt, part_path(settings.store_dir, "dev", "opt_1m", "NIFTY", d))

            bdir = settings.external_dir / "bhavcopy"
            bdir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{
                "TckrSymb": "NIFTY", "XpryDt": "2026-05-12", "StrkPric": 25000.0, "OptnTp": "CE",
                "OpnPric": 100.0, "HghPric": 105.0, "LwPric": 95.0, "ClsPric": 102.0,
                "SttlmPric": 102.0, "OpnIntrst": 5000, "TtlTradgVol": 6500, "TradDt": "2026-05-11",
                "FinInstrmTp": "IDO",
            }]).to_csv(bdir / "bhav_20260511.csv", index=False)

            res_bhav = reconcile_bhavcopy(settings, cal)
            self.assertEqual(res_bhav["status"], "VOLUME_UNIT_SUSPECT")
            self.assertIn("2026-05-11", res_bhav["suspect_days"])

    def test_T23_termux_month_by_month_continue_mode(self):
        from updl.monthly import run_monthly_continue
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, items="D12,D02")
            settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            tr = _build_fixture_transport("A")
            cli = HttpClient(transport=tr, limiter=RateLimiter(6000, sleep=lambda s: None), sleep=lambda s: None)
            run_probe(settings, cli, calendar=cal, probe_today=dt.date(2026, 11, 11))
            cli.budget = None

            phone_out = Path(tmp) / "phone_dl"
            pkg1 = run_monthly_continue(settings, cli, calendar=cal, today_override="2026-11-11",
                                        phone_dir=str(phone_out))
            self.assertEqual(pkg1["batch_no"], 1)
            self.assertEqual(pkg1["start_date"], "2026-10-11")
            self.assertEqual(pkg1["end_date"], "2026-11-09")
            self.assertTrue(Path(pkg1["zip_path"]).exists())
            self.assertTrue(Path(pkg1["sdcard_path"]).exists())

            pkg2 = run_monthly_continue(settings, cli, calendar=cal, today_override="2026-11-11",
                                        phone_dir=str(phone_out))
            self.assertEqual(pkg2["batch_no"], 2)
            self.assertEqual(pkg2["start_date"], "2026-09-11")
            self.assertEqual(pkg2["end_date"], "2026-10-09")
            self.assertTrue(Path(pkg2["zip_path"]).exists())


if __name__ == "__main__":
    unittest.main()
