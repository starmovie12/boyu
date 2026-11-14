"""v4.2 tests: exchange-side market-info snapshots (PCR, max pain, OI, change in OI, FII/DII) and their paths.
Mocked HTTP only."""
from __future__ import annotations

import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from updl.client import RawResponse, path_allowed
from updl.config import load_settings
from updl.marketinfo import load_ledger, run_marketinfo
from updl.sessions import load_calendar

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v3_fixes import _client

TODAY = dt.date(2026, 11, 14)
RETENTION_FROM = "2026-10-30"          # what the live API showed on 14 Nov 2026


class InfoTransport:
    def __init__(self):
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok = lambda data: RawResponse(200, {}, json.dumps({"status": "success", "data": data}).encode())
        if u.path == "/v2/expired-instruments/expiries":
            return ok(["2026-10-20", "2026-10-27", "2026-11-03", "2026-11-09"])
        if u.path in ("/v2/market/fii", "/v2/market/dii"):
            ts = int(dt.datetime(2026, 11, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp() * 1000)
            return ok({q["data_type"]: [{"time_stamp": ts, "buy_amount": 10.5, "sell_amount": 9.5, "oi_contracts": 7}]})
        if u.path.startswith("/v2/market/"):
            if q["date"] < RETENTION_FROM:
                return ok(None)                                   # outside retention: null, not an error
            kind = u.path.rsplit("/", 1)[-1]
            if kind == "pcr":
                assert q["bucket_interval"] == "1"
                return ok({"pcr": 0.85, "spot_closing_price": 25299.35, "expiry_date": "x",
                           "insights": [{"pcr": 0.77, "spot_price": 25193.6, "time": "09:15"},
                                        {"pcr": 0.85, "spot_price": 25299.35, "time": "15:40"}]})
            if kind == "max-pain":
                return ok({"max_pain": 25200.0, "spot_closing_price": 25299.35,
                           "insights": [{"max_pain": 25100.0, "spot_price": 25193.6, "time": "09:15"}]})
            if kind == "oi":
                return ok({"total_puts": 5, "total_calls": 6, "call_put_oi_data_list": [
                    {"call_oi": 0, "put_oi": 829530, "strike_price": 20850.0},
                    {"call_oi": 506675, "put_oi": 3879265, "strike_price": 24400.0}]})
            if kind == "change-oi":
                assert q["interval"] == "5"
                return ok({"total_put_change_oi": 1, "call_put_oi_data_list": [
                    {"call_oi_change": 5, "put_oi_change": -3, "strike_price": 24400.0}]})
        return RawResponse(404, {}, b"{}")


class TestMarketInfo(unittest.TestCase):
    def test_paths_allowed_and_neighbours_not(self):
        for ok in ("/v2/market/pcr", "/v2/market/max-pain", "/v2/market/oi", "/v2/market/change-oi",
                   "/v2/market/fii", "/v2/market/dii", "/v3/market-quote/ltp"):
            self.assertTrue(path_allowed(ok), ok)
        for bad in ("/v2/market/pcr/x", "/v2/market/smartlist/options", "/v2/market/status/NSE", "/v2/market-quote/ltp",
                    "/v3/market-quote/quotes", "/v3/market-quote/ohlc", "/v2/charges/brokerage", "/v2/user/profile"):
            self.assertFalse(path_allowed(bad), bad)

    def test_saves_inside_retention_records_null_outside_and_never_asks_twice(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            settings.ensure_dirs()
            # today's instrument master (as the probe saves it): one still-listed expiry, 24 Nov 2026
            import gzip
            exp_ms = int(dt.datetime(2026, 11, 24, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp() * 1000)
            master = [{"segment": "NSE_FO", "instrument_type": "CE", "underlying_key": "NSE_INDEX|Nifty 50",
                       "instrument_key": "NSE_FO|90002", "expiry": exp_ms, "strike_price": 25000.0}]
            (settings.raw_dir / "D12").mkdir(parents=True)
            (settings.raw_dir / "D12" / "NSE.json.gz").write_bytes(gzip.compress(json.dumps(master).encode()))
            tr = InfoTransport()
            lines = []
            res = run_marketinfo(settings, _client(tr), cal, TODAY, say=lines.append)
            want = [d.isoformat() for d in cal.trading_days(RETENTION_FROM, "2026-11-13")]
            self.assertEqual(res["days_saved"], want)
            self.assertGreater(res["nulls"], 0)
            self.assertTrue(any("kabhi nahi mil sakte" in x for x in lines))
            out = Path(tmp) / "xcheck_csv"
            pcr = list(csv.DictReader(open(out / "NIFTY" / "pcr.csv", encoding="utf-8")))
            self.assertEqual({r["date"] for r in pcr}, set(want))
            row = next(r for r in pcr if r["date"] == "2026-11-12" and r["time"] == "15:40")
            self.assertEqual((row["expiry"], row["pcr"], row["usable_from"]),
                             ("2026-11-24", "0.85", "2026-11-13T09:15:00+05:30"))     # usable from the next session
            # 9 Nov is an expiry day: current and next expiry are both saved
            self.assertEqual({r["expiry"] for r in pcr if r["date"] == "2026-11-09"}, {"2026-11-09", "2026-11-24"})
            oi = list(csv.DictReader(open(out / "NIFTY" / "oi.csv", encoding="utf-8")))
            self.assertIn(("20850.0", "0", "829530"), {(r["strike_price"], r["call_oi"], r["put_oi"]) for r in oi})
            self.assertTrue((out / "NIFTY" / "chgoi.csv").exists() and (out / "NIFTY" / "maxpain_daily.csv").exists())
            flows = list(csv.DictReader(open(out / "fii_dii_daily.csv", encoding="utf-8")))
            self.assertEqual(len(flows), 6)                      # 5 FII data types + 1 DII, one day each
            self.assertEqual({r["usable_from"] for r in flows}, {"2026-11-13T09:15:00+05:30"})
            raw = list((Path(tmp) / "raw" / "X01" / "NIFTY").glob("*.json.gz"))
            self.assertTrue(raw and all(len(p.name) < 40 for p in raw))
            led = load_ledger(settings)
            self.assertEqual(led["NIFTY|2026-10-29|2026-11-03|pcr"], "null")
            self.assertEqual(led["NIFTY|2026-11-12|2026-11-24|oi"], "data")
            n = len([u for u in tr.urls if "/v2/market/pcr" in u or "/v2/market/oi" in u])
            res2 = run_marketinfo(settings, _client(tr), cal, TODAY, say=lines.append)
            self.assertEqual(len([u for u in tr.urls if "/v2/market/pcr" in u or "/v2/market/oi" in u]), n)
            self.assertEqual(res2["days_saved"], [])
            self.assertEqual(len(list(csv.DictReader(open(out / "fii_dii_daily.csv", encoding="utf-8")))), 6)


if __name__ == "__main__":
    unittest.main()
