"""v4.2 PROMPT_04 C4 remainder: vol extras, spread report, adjusted_status verdict, real bhavcopy units,
Nifty 50 list for D10, recorder files in the per-index layout, ai_ready loader."""
from __future__ import annotations

import csv
import datetime as dt
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from updl.config import load_settings
from updl.corpactions import run_adjusted_status
from updl.derive import black76_price
from updl.layout import SplitCsv, load_ai_ready, write_layout
from updl.sessions import load_calendar
from updl.spreadreport import run_spread_report
from updl.store import list_parts, parse_candles, part_path, partition_of, read_parquet, rows_to_frame, write_parquet
from updl.validate import reconcile_bhavcopy
from updl.volextras import atm_and_skew, gamma_flip, iv_daily_rows, term_spread, vega_vec

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v4_week import Wrap, _setup, _week

ROOT = Path(__file__).resolve().parent.parent
REAL_BHAV_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
                    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
                    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
                    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


class TestVolExtras(unittest.TestCase):
    def test_vega_matches_a_finite_difference(self):
        F, K, T, s = 25000.0, 25200.0, 7 / 365, 0.14
        num = (black76_price(F, K, T, s + 0.005, "CE") - black76_price(F, K, T, s - 0.005, "CE")) / 1.0
        self.assertAlmostEqual(float(vega_vec([F], [K], [T], [s])[0]), num, places=2)
        self.assertTrue(np.isnan(vega_vec([F], [K], [T], [np.nan])[0]))

    def test_atm_skew_term_and_gamma_flip(self):
        # one minute, two expiries (groups 0 and 1); puts carry a higher IV than calls
        K = np.array([24500, 25000, 25000, 25500, 24500, 25000, 25000, 25500], dtype=float)
        call = np.array([False, True, False, True, False, True, False, True])
        iv = np.array([0.18, 0.14, 0.16, 0.12, 0.19, 0.15, 0.17, 0.13])
        delta = np.array([-0.24, 0.52, -0.48, 0.26, -0.26, 0.51, -0.49, 0.30])
        grp = np.array([0, 0, 0, 0, 1, 1, 1, 1])
        F = np.full(8, 25010.0)
        atm, skew = atm_and_skew(grp, 2, K, F, iv, delta, call)
        self.assertAlmostEqual(atm[0], 0.15)
        self.assertAlmostEqual(skew[0], 0.18 - 0.12)
        self.assertAlmostEqual(skew[1], 0.19 - 0.13)
        term = term_spread(np.array([5, 5]), [dt.date(2026, 11, 17), dt.date(2026, 11, 24)], atm)
        self.assertAlmostEqual(term[0], 0.15 - 0.16)
        self.assertAlmostEqual(term[1], term[0])
        # a big put OI at 25000 and a big call OI at 25500: exposure is negative at spot and turns positive
        # on the way up, so the flip lies between the two strikes
        Kf = np.array([25000.0, 25500.0])
        flip = gamma_flip(np.array([0, 0]), 1, np.full(2, 25000.0), Kf, np.full(2, 7 / 365), np.full(2, 0.14),
                          np.array([False, True]), np.full(2, 1e6), np.full(2, 25000.0), np.array([25000.0]))
        self.assertTrue(25100 < flip[0] < 25400, flip)
        none = gamma_flip(np.array([0]), 1, np.array([25000.0]), np.array([25000.0]), np.array([0.02]),
                          np.array([0.14]), np.array([True]), np.array([1e6]), np.array([25000.0]),
                          np.array([25000.0]))
        self.assertTrue(np.isnan(none[0]))           # one call only: never changes sign

    def test_iv_daily_uses_only_minutes_closed_by_the_check_time(self):
        from updl.timeutil import at_ist
        d = dt.date(2026, 11, 12)
        m = pd.DataFrame({"ts_close": [at_ist(d, "09:59"), at_ist(d, "10:00"), at_ist(d, "10:01"),
                                       at_ist(d, "10:00")],
                          "expiry_date": [dt.date(2026, 11, 17)] * 3 + [dt.date(2026, 11, 24)],
                          "atm_iv_DERIVED": [0.10, 0.11, 0.99, 0.12], "dte_bucket": ["3-4"] * 4})
        rows = iv_daily_rows(m, "NIFTY", d)
        r10 = rows[rows["check_time"] == "10:00"].iloc[0]
        self.assertAlmostEqual(r10["iv_daily_DERIVED"], 0.11)       # 10:01 is not known at 10:00
        self.assertEqual(r10["expiry_date"], dt.date(2026, 11, 17))
        self.assertAlmostEqual(rows[rows["check_time"] == "14:00"].iloc[0]["iv_daily_DERIVED"], 0.99)
        self.assertEqual(iv_daily_rows(m.iloc[0:0], "NIFTY", d)["status"].tolist(), ["NO_ATM_IV_YET"] * 2)


class TestWeekExtras(unittest.TestCase):
    def test_derived_columns_recorder_quotes_and_ai_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap(), items="D12,D01,D03,D07,D05,D06,D08")
            _week(settings, cli, tmp)
            ivg = [p for part in ("dev", "holdout") for p in list_parts(settings.store_dir, part, "iv_greeks_1m", "NIFTY")]
            self.assertIn("vega_DERIVED", read_parquet(ivg[0]).columns)
            met = read_parquet([p for part in ("dev", "holdout")
                                for p in list_parts(settings.store_dir, part, "chain_metrics_1m", "NIFTY")][0])
            for col in ("atm_iv_DERIVED", "skew_DERIVED", "vix_term_DERIVED", "gamma_flip_PROXY", "dte_bucket"):
                self.assertIn(col, met.columns)
            self.assertTrue((pd.to_datetime(met["available_from_ts"]) >= pd.to_datetime(met["ts_close"])).all())
            ivd = [p for part in ("dev", "holdout") for p in list_parts(settings.store_dir, part, "iv_daily_DERIVED", "NIFTY")]
            self.assertTrue(ivd)
            self.assertEqual(read_parquet(ivd[0])["check_time"].tolist(), ["10:00", "14:00"])

            rec = settings.workdir / "recorder" / "NIFTY"
            rec.mkdir(parents=True)
            lines = ["local_ts,underlying,strike,bid,ask"] + [f"2026-11-12T09:{m:02d}:00.000+05:30,NIFTY,25000,10,11"
                                                            for m in range(15, 45)]
            (rec / "2026-11-12.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (rec / "2026-11-12_ltp.csv").write_text("local_ts,role,ltp\nx,SPOT,25000\n", encoding="utf-8")
            (rec / "2026-11-20.csv").write_text("local_ts,bid\nx,1\n", encoding="utf-8")      # outside the window
            out = Path(tmp) / "out"
            res = write_layout(settings, "2026-11-09", "2026-11-13", out)
            q = out / "NIFTY" / "recorder_quotes"
            self.assertEqual(sorted(p.name for p in q.iterdir()), ["2026-11-12.csv", "2026-11-12_ltp.csv"])
            self.assertEqual(res["NIFTY"]["recorder_rows"], 31)
            self.assertEqual((q / "2026-11-12.csv").read_text(encoding="utf-8"), "\n".join(lines) + "\n")
            # the whole window is holdout: the default loader gives nothing, the final-test switch gives the rows
            self.assertTrue(load_ai_ready(out, "NIFTY").empty)
            self.assertEqual(len(load_ai_ready(out, "NIFTY", include_holdout=True)), res["NIFTY"]["option_rows"])

    def test_split_csv_never_reuses_a_name_on_one_big_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = SplitCsv(Path(tmp), "x", ["a"], max_bytes=40)
            for _ in range(3):
                w.append(pd.DataFrame({"a": ["y" * 30]}), dt.date(2026, 11, 12))
            self.assertEqual([p.name for p in w.files], ["x.csv", "x_from_2026-11-12.csv", "x_from_2026-11-12_2.csv"])
            self.assertEqual(sum(len(p.read_text().splitlines()) - 1 for p in w.files), 3)


class TestSpreadAndAdjusted(unittest.TestCase):
    def test_spread_report_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(MANIFEST_PATH, workdir=tmp)
            rec = Path(tmp) / "recorder" / "NIFTY"
            rec.mkdir(parents=True)
            rows = ["local_ts,underlying,expiry_date,strike,option_type,bid,ask,bid_qty,ask_qty,underlying_spot",
                    "2026-11-16T09:20:00.123+05:30,NIFTY,2026-11-17,25000,CE,100,101,650,1300,25010",
                    "2026-11-16T09:21:00.123+05:30,NIFTY,2026-11-17,25000,CE,100,102,650,1300,25010",
                    "2026-11-16T09:20:00.123+05:30,NIFTY,2026-11-17,26500,CE,1.0,1.5,65,65,25010",
                    "2026-11-16T10:05:00.000+05:30,NIFTY,2026-11-17,25400,PE,0,5,0,65,25010"]
            (rec / "2026-11-16.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            out = []
            res = run_spread_report(s, say=out.append)
            self.assertEqual(res["days"], 1)
            rep = {(r["moneyness"], r["option_type"], r["hour"]): r
                   for r in csv.DictReader(open(s.reports_dir / "spread_report.csv", encoding="utf-8"))}
            atm = rep[("ATM", "CE", "09")]
            self.assertEqual((atm["quotes"], atm["median_spread"], atm["days_to_expiry"]), ("2", "1.5", "1"))
            self.assertAlmostEqual(float(rep[("DEEP", "CE", "09")]["median_spread_pct"]), 40.0)
            near = rep[("NEAR", "PE", "10")]
            self.assertEqual((near["quotes_without_bid_ask"], near["median_spread"]), ("1", ""))
            self.assertTrue(any("ATM median" in x for x in out))
            run_spread_report(s, say=out.append)          # a second run replaces that day, it does not double it
            self.assertEqual(len(list(csv.DictReader(open(s.reports_dir / "spread_report.csv", encoding="utf-8")))), 3)

    def test_adjusted_status_verdict(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(MANIFEST_PATH, workdir=tmp, external_dir=str(Path(tmp) / "ext"))
            s.external_dir.mkdir(parents=True)
            ex = dt.date(2025, 8, 26)
            prev = cal.shift(ex, -1)
            for sym, pc, eo in (("HDFCBANK", 2000.0, 1001.0), ("INFY", 1500.0, 1495.0)):
                for d, o, c in ((prev, pc, pc), (ex, eo, eo)):
                    df = pd.DataFrame({"ts_open": [pd.Timestamp(f"{d}T09:15:00+05:30")], "o": [o], "c": [c]})
                    write_parquet(df, part_path(s.store_dir, partition_of(d, s.holdout_start), "stock_1m", sym, d))
            (s.external_dir / "corporate_actions.csv").write_text(
                "symbol,ex_date,type,ratio,source_url\nHDFCBANK,2025-08-26,BONUS,1:1,https://x\n"
                "INFY,2025-08-26,SPLIT,5:1,https://x\nTCS,2025-08-26,SPLIT,2:1,https://x\n"
                "ITC,2025-08-26,DIVIDEND,,https://x\n", encoding="utf-8")
            res = run_adjusted_status(s, cal, say=lambda *_: None)
            got = {r["symbol"]: r["verdict"] for r in csv.DictReader(open(s.reports_dir / "adjusted_status.csv",
                                                                           encoding="utf-8"))}
            self.assertEqual(got, {"HDFCBANK": "UNADJUSTED", "INFY": "ADJUSTED", "TCS": "NO_DATA"})
            self.assertEqual(res["verdict"], "MIXED")


class TestRealBhavcopyAndStocks(unittest.TestCase):
    def test_reconcile_uses_contracts_times_lot_and_matches_futures(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(MANIFEST_PATH, workdir=tmp, external_dir=Path(tmp) / "ext")
            s.ensure_dirs()
            d = dt.date(2026, 11, 13)
            part = partition_of(d, s.holdout_start)
            for table, meta, candles in (
                    ("opt_1m", {"instrument_key": "NSE_FO|1", "expiry_date": "2026-11-17", "strike": 25000,
                                "option_type": "CE"},
                     [["2026-11-13T15:28:00+05:30", 99, 101, 98, 100, 650, 3250],
                      ["2026-11-13T15:29:00+05:30", 100, 103, 99, 102, 1300, 6500]]),
                    ("fut_1m", {"instrument_key": "NSE_FO|2", "expiry_date": "2026-11-23", "strike": None,
                                "option_type": "FUT"},
                     [["2026-11-13T15:29:00+05:30", 25170, 25175, 25165, 25171, 6500, 11790350]])):
                rows, _ = parse_candles({"data": {"candles": candles}})
                df = rows_to_frame(rows, {"table": table, "underlying": "NIFTY", **meta}, cal, "r1", "0" * 64)
                write_parquet(df, part_path(s.store_dir, part, table, "NIFTY", d))
            bdir = s.external_dir / "bhavcopy"
            bdir.mkdir(parents=True)
            body = [REAL_BHAV_HEADER,
                    "2026-11-13,2026-11-13,FO,NSE,IDO,1,,NIFTY,,2026-11-17,2026-11-17,25000.00,CE,X,1,1,1,102.00,"
                    "102,1,25171,102.00,6500,0,30,0,0,F1,65,,,,,",
                    "2026-11-13,2026-11-13,FO,NSE,IDF,2,,NIFTY,,2026-11-23,2026-11-23,,,Y,1,1,1,25171.00,25171,1,"
                    "25171,25171.00,11790350,0,100,0,0,F1,65,,,,,",
                    "2026-11-13,2026-11-13,FO,NSE,STO,3,,ABCAPITAL,,2026-12-29,2026-12-29,365.00,PE,Z,0,0,0,27.2,0,"
                    "27.2,408,3.76,0,0,0,0,0,F1,1550,,,,,"]
            (bdir / "bhav_20261113.csv").write_text("\n".join(body) + "\n", encoding="utf-8")
            res = reconcile_bhavcopy(s, cal)
            self.assertEqual(res["status"], "OK", res)
            rec = list(csv.DictReader(open(s.reports_dir / "reconciliation.csv", encoding="utf-8")))
            self.assertEqual(sorted(r["option_type"] for r in rec), ["CE", "FUT"])
            fut = next(r for r in rec if r["option_type"] == "FUT")
            self.assertEqual((float(fut["bhav_vol"]), float(fut["bhav_contracts"]), float(fut["volume_ratio"])),
                             (6500.0, 100.0, 1.0))
            self.assertEqual(float(fut["oi_abs_diff"]), 0.0)

    def test_official_nifty50_list_feeds_d10(self):
        rows = list(csv.DictReader(open(ROOT / "reference" / "stocks_nifty50_sensex.csv", encoding="utf-8")))
        self.assertEqual(len(rows), 50)
        self.assertEqual(len({r["symbol"] for r in rows}), 50)
        self.assertTrue(all(re.match(r"^IN[A-Z0-9]{9}[0-9]$", r["isin"]) and r["group"] == "NIFTY50" for r in rows))
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, "-m", "updl", "plan", "--items", "D10", "--workdir", tmp],
                                 cwd=ROOT, capture_output=True, text=True, timeout=300)
            text = out.stdout + out.stderr
            self.assertNotIn("SKIPPED_NO_LIST", text)
            self.assertRegex(text, r"D10: \d+ calls")


class WeeklyExpiries(Wrap):
    """The fixture lists expiries months apart; the real list is weekly. Add the weekly expiry before 9 Nov."""

    def get(self, url, headers=None, timeout=None):
        r = super().get(url, headers, timeout)
        if "/v2/expired-instruments/expiries" in url:
            import json
            from updl.client import RawResponse
            body = json.loads(r.content)
            body["data"] = sorted(set(body["data"]) | {"2026-11-02"})
            return RawResponse(r.status_code, r.headers, json.dumps(body).encode())
        return r


class TestWholeExpiryPeriod(unittest.TestCase):
    def test_period_maths(self):
        cal = load_calendar(REF_DIR)
        from updl.selector import expiry_period_days
        exps = [dt.date(2026, 11, 3), dt.date(2026, 11, 9), dt.date(2026, 11, 17)]
        days = expiry_period_days(dt.date(2026, 11, 17), exps, 90, cal)
        self.assertEqual((days[0], days[-1]), (dt.date(2026, 11, 9), dt.date(2026, 11, 17)))
        self.assertEqual(expiry_period_days(dt.date(2026, 11, 3), exps, 90, cal), [])     # no previous expiry
        self.assertEqual(expiry_period_days(dt.date(2026, 11, 17), exps, 1, cal), [])     # span not verified
        self.assertEqual(expiry_period_days(dt.date(2026, 11, 17), exps, 5, cal), [])     # does not fit one call

    def test_two_batches_need_fewer_calls_and_lose_nothing(self):
        import updl.plan as plan_mod
        out = {}
        for mode in ("old", "new"):
            with tempfile.TemporaryDirectory() as tmp:
                tr = WeeklyExpiries()
                settings, cli = _setup(tmp, tr, items="D12,D01,D03,D07,D06,D08")
                orig = plan_mod.expiry_period_days
                if mode == "old":
                    plan_mod.expiry_period_days = lambda *a, **k: []
                try:
                    _week(settings, cli, tmp, weeks=2)
                finally:
                    plan_mod.expiry_period_days = orig
                calls = sum(1 for u in tr.urls if "/expired-instruments/historical-candle/" in u)
                keys = set()
                for t in ("opt_1m", "opt_wide_1m"):
                    for part in ("dev", "holdout"):
                        for p in list_parts(settings.store_dir, part, t):
                            if "2026-10-31" <= p.parent.name[13:] <= "2026-11-13":
                                df = read_parquet(p, columns=["instrument_key", "ts_open"])
                                keys |= set(zip(df["instrument_key"], df["ts_open"].astype(str)))
                out[mode] = (calls, keys)
        print("expired option calls, two batches: old", out["old"][0], "new", out["new"][0])
        self.assertLess(out["new"][0], out["old"][0])
        self.assertTrue(out["old"][1] <= out["new"][1])


if __name__ == "__main__":
    unittest.main()
