"""v4.2 tests: Recorder v2 (multi-expiry, LTP, gaps file, summary line, interval floor, shared rate limit)."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from updl.client import MASTER_URLS, RawResponse
from updl.config import load_settings
from updl.recorder import expiries_to_record, run_record
from updl.sessions import load_calendar
from updl.timeutil import IST

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v3_fixes import _client


def _ms(d):
    return int(dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp() * 1000)


class RecTransport:
    """Master with three weekly NIFTY expiries and a future; live chain and LTP answers; one chain error."""

    def __init__(self):
        self.urls, self.fail_next_chain = [], 0
        nifty = "NSE_INDEX|Nifty 50"
        rows = [{"segment": "NSE_INDEX", "name": "Nifty 50", "trading_symbol": "Nifty 50", "instrument_key": nifty},
                {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": nifty, "instrument_key": "NSE_FO|70001",
                 "expiry": _ms(dt.date(2026, 11, 23))}]
        for i, e in enumerate((dt.date(2026, 11, 17), dt.date(2026, 11, 23), dt.date(2026, 12, 1))):
            rows.append({"segment": "NSE_FO", "instrument_type": "CE", "underlying_key": nifty, "strike_price": 25000.0,
                         "instrument_key": f"NSE_FO|8000{i}", "expiry": _ms(e)})
        self.master = gzip.compress(json.dumps(rows).encode())

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        if url in MASTER_URLS.values():
            return RawResponse(200, {}, self.master if url == MASTER_URLS["NSE"] else gzip.compress(b"[]"))
        if "/v3/market-quote/ltp" in url:
            data = {"NSE_INDEX:Nifty 50": {"last_price": 25144.3, "instrument_token": "NSE_INDEX|Nifty 50", "cp": 25299.35},
                    "NSE_INDEX:India VIX": {"last_price": 15.5, "instrument_token": "NSE_INDEX|India VIX", "cp": 16.6},
                    "NSE_FO:NIFTY26NOVFUT": {"last_price": 25190.0, "instrument_token": "NSE_FO|70001", "volume": 9}}
            return RawResponse(200, {}, json.dumps({"status": "success", "data": data}).encode())
        if "/v2/option/chain" in url:
            if self.fail_next_chain:
                self.fail_next_chain -= 1
                return RawResponse(400, {}, b'{"status":"error","errors":[{"errorCode":"UDAPI100011","message":"x"}]}')
            leg = {"instrument_key": "NSE_FO|80000", "market_data": {"ltp": 120.5, "bid_price": 120.0, "ask_price": 121.0},
                   "option_greeks": {"iv": 13.4}}
            return RawResponse(200, {}, json.dumps({"status": "success", "data": [
                {"strike_price": 25000.0, "underlying_spot_price": 25144.3, "call_options": leg, "put_options": leg}]}).encode())
        return RawResponse(404, {}, b"{}")


class TestRecorderV2(unittest.TestCase):
    def test_expiry_choice(self):
        d = dt.date
        cs = [{"expiry_date": e} for e in (d(2026, 11, 17), d(2026, 11, 23), d(2026, 12, 1), d(2026, 12, 29))]
        self.assertEqual(expiries_to_record(cs, d(2026, 11, 16)), [d(2026, 11, 17), d(2026, 11, 23)])
        self.assertEqual(expiries_to_record(cs, d(2026, 11, 16), monthly=True), [d(2026, 11, 23)])
        self.assertEqual(expiries_to_record(cs, d(2026, 11, 23), monthly=True), [d(2026, 11, 23), d(2026, 12, 29)])

    def test_snapshots_ltp_gaps_and_summary(self):
        cal = load_calendar(REF_DIR)
        clock = [dt.datetime(2026, 11, 16, 9, 10, tzinfo=IST)]       # the owner starts at 09:10 on Monday
        sleeps = []

        def sleep(sec):
            sleeps.append(sec)
            clock[0] += dt.timedelta(seconds=sec)
            if len(sleeps) == 7:                                      # the phone dozes once for five minutes
                clock[0] += dt.timedelta(minutes=5)

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            tr = RecTransport()
            tr.fail_next_chain = 1
            res = run_record(settings, _client(tr), calendar=cal, underlyings=["nifty"], every_sec=5,
                             max_snapshots=4, now_fn=lambda: clock[0], sleep=sleep)
            self.assertEqual(sleeps[:5], [60.0] * 5)                 # waits for the 09:15 open in visible steps
            self.assertEqual(sleeps[5], 15.0)                        # --every 5 is raised to the 15 s floor
            self.assertEqual((res["snapshots"], res["gaps"], res["errors"]), (4, 2, 1))
            day = Path(tmp) / "recorder" / "NIFTY"
            chain = list(csv.DictReader(open(day / "2026-11-16.csv", encoding="utf-8")))
            self.assertEqual({r["expiry_date"] for r in chain}, {"2026-11-17", "2026-11-23"})   # current + next
            self.assertEqual(len(chain), 4 * 2 * 2 - 2)              # one chain call failed (2 rows lost, recorded)
            self.assertEqual(chain[0]["local_ts"], "2026-11-16T09:15:00.000+05:30")      # milliseconds
            ltp = list(csv.DictReader(open(day / "2026-11-16_ltp.csv", encoding="utf-8")))
            self.assertEqual(len(ltp), 4 * 3)
            self.assertEqual({(r["role"], r["last_price"]) for r in ltp},
                             {("spot", "25144.3"), ("future", "25190.0"), ("vix", "15.5")})
            gaps = list(csv.DictReader(open(Path(tmp) / "recorder" / "gaps_2026-11-16.csv", encoding="utf-8")))
            self.assertEqual(sorted(g["kind"] for g in gaps), ["CHAIN_ERROR", "MISSED_SNAPSHOTS"])
            summary = list(csv.DictReader(open(Path(tmp) / "recorder" / "summary.csv", encoding="utf-8")))
            self.assertEqual((summary[0]["snapshots"], summary[0]["gaps"], summary[0]["every_sec"],
                              summary[0]["expiries"]), ("4", "2", "15", "2026-11-17 2026-11-23"))
            calls = [u for u in tr.urls if "api.upstox.com" in u]
            self.assertEqual(len(calls), 4 * 3)                      # per snapshot: 1 LTP + 2 chains

    def test_a_running_recorder_lowers_the_download_cap(self):
        from updl.runlock import RECORD_LOCK_NAME
        from tests.test_v4_week import Wrap, _setup, _week
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap())
            cli.limiter.max_per_30min = 1800
            (settings.state_dir / RECORD_LOCK_NAME).write_text(json.dumps(
                {"pid": os.getppid(), "started": __import__("time").time(), "command": "record", "calls_per_30min": 210}))
            _week(settings, cli, tmp)
            self.assertEqual(cli.limiter.max_per_30min, 1590)


if __name__ == "__main__":
    unittest.main()
