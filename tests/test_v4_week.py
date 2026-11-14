"""v4 tests: week batches, newest-day safety, parallel download, limiter, progress, phone folder, calendar.
Mocked HTTP only: zero real network calls, zero real tokens."""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from updl.client import RateLimiter, RawResponse, estimate_minutes, path_allowed
from updl.config import load_settings
from updl.probe import run_probe
from updl.sessions import load_calendar
from updl.weekly import load_cursor, next_batch, run_week_batches

from tests.test_updl import MANIFEST_PATH, REF_DIR, _build_fixture_transport
from tests.test_v3_fixes import _client, _opt_frame

TODAY = "2026-11-14"        # a Saturday; last completed trading day = Fri 13 Nov


class Wrap:
    """Fixture transport with hooks: empty candles for chosen days, a kill after N candle calls, timings."""

    def __init__(self, empty_days=(), kill_after=None, timings=None):
        self.base = _build_fixture_transport("A")
        self.empty_days, self.kill_after, self.timings = set(empty_days), kill_after, timings or {}
        self.urls, self.n_candle = [], 0
        self._lock = threading.Lock()

    def get(self, url, headers=None, timeout=None):
        with self._lock:
            self.urls.append(url)
        if "/v2/market/timings/" in url:
            day = url.rsplit("/", 1)[-1]
            if day in self.timings:
                return RawResponse(200, {}, json.dumps({"status": "success", "data": self.timings[day]}).encode())
        if "historical-candle" in url:
            # like the real API: expired keys carry |dd-mm-yyyy and only work on the expired endpoint
            dated = re.search(r"%7C\d\d-\d\d-\d{4}/", url) is not None
            if ("/v2/expired-instruments/" in url) != dated:
                body = {"status": "error", "errors": [{"errorCode": "UDAPI1021", "message": "key format"}]}
                return RawResponse(400, {}, json.dumps(body).encode())
            with self._lock:
                self.n_candle += 1
                n = self.n_candle
            if self.kill_after is not None and n > self.kill_after:
                with self._lock:
                    self.urls.pop()          # the killed call never completed; it does not count as a download
                raise KeyboardInterrupt()
            to_s, from_s = url.split("/")[-2], url.split("/")[-1]
            if to_s == from_s and to_s in self.empty_days:
                return RawResponse(200, {}, b'{"status":"success","data":{"candles":[]}}')
        return self.base.get(url, headers, timeout)


def _setup(tmp, tr, workers=1, items="D12,D01,D03,D07,D06"):
    settings = load_settings(MANIFEST_PATH, workdir=str(Path(tmp) / "wd"), items=items)
    settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
    settings.workers = workers
    cli = _client(tr)
    run_probe(settings, cli, calendar=load_calendar(REF_DIR), probe_today=dt.date(2026, 11, 14))
    cli.budget = None
    return settings, cli


def _week(settings, cli, tmp, **kw):
    return run_week_batches(settings, cli, calendar=load_calendar(REF_DIR), today_override=kw.pop("today", TODAY),
                            phone_dir=str(Path(tmp) / "phone"), **kw)


def _candle_days(urls):
    """(instrument, from, to) requested from the 1-minute candle endpoints. The one cheap "is the newest day
    served yet" probe of every run (index, single day 13 Nov) is not a download and is left out."""
    out = []
    for u in urls:
        if "historical-candle" in u and ("/minutes/1/" in u or "/1minute/" in u):
            parts = u.split("/")
            item = (parts[-5] if "/v3/" in u else parts[-4], parts[-1], parts[-2])
            if item != ("NSE_INDEX%7CNifty%2050", "2026-11-13", "2026-11-13"):
                out.append(item)
    return out


class TestWindowMaths(unittest.TestCase):
    def test_three_weeks_are_contiguous_and_cross_month_and_year(self):
        d = dt.date
        cur = {"newest_done": None, "next_end": None}
        b1 = next_batch(cur, d(2026, 11, 14), d(2026, 11, 13))
        self.assertEqual((b1["kind"], b1["start"], b1["end"]), ("first", d(2026, 11, 7), d(2026, 11, 13)))
        cur = {"newest_done": "2026-11-13", "next_end": b1["next_end"].isoformat()}
        b2 = next_batch(cur, d(2026, 11, 14), d(2026, 11, 13))
        self.assertEqual((b2["kind"], b2["start"], b2["end"]), ("back", d(2026, 10, 31), d(2026, 11, 6)))
        cur["next_end"] = b2["next_end"].isoformat()
        b3 = next_batch(cur, d(2026, 11, 14), d(2026, 11, 13))
        self.assertEqual((b3["start"], b3["end"]), (d(2026, 10, 24), d(2026, 10, 30)))
        # year edge: no gap, no overlap
        cur = {"newest_done": "2026-11-13", "next_end": "2026-01-03"}
        b = next_batch(cur, d(2026, 11, 14), d(2026, 11, 13))
        self.assertEqual((b["start"], b["end"], b["next_end"]), (d(2025, 12, 28), d(2026, 1, 3), d(2025, 12, 27)))
        # newer days appeared: catch them up before going further back; the back cursor does not move
        c = next_batch({"newest_done": "2026-11-12", "next_end": "2026-11-06"}, d(2026, 11, 17), d(2026, 11, 16))
        self.assertEqual((c["kind"], c["start"], c["end"], c["next_end"]),
                         ("new", d(2026, 11, 13), d(2026, 11, 16), d(2026, 11, 6)))
        # history floor
        self.assertIsNone(next_batch({"newest_done": "2026-11-13", "next_end": "2024-09-30"}, d(2026, 11, 14),
                                     d(2026, 11, 13), floor=d(2024, 10, 1)))


class TestLimiterAndEstimate(unittest.TestCase):
    def test_four_threads_never_pass_either_cap_on_a_fake_clock(self):
        now = [0.0]
        lock = threading.Lock()

        def sleep(sec):
            with lock:
                now[0] += sec

        lim = RateLimiter(calls_per_min=240, max_per_30min=300, burst=3, clock=lambda: now[0], sleep=sleep)
        stamps = []

        def work():
            for _ in range(100):
                lim.acquire()
                with lock:
                    stamps.append(now[0])

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        self.assertEqual(len(stamps), 400)
        for i, t0 in enumerate(stamps):
            in_min = sum(1 for t in stamps[i:] if t - t0 < 60.0)
            in_30 = sum(1 for t in stamps[i:] if t - t0 < 1800.0)
            self.assertLessEqual(in_min, 240 + 3)          # rate x 1 minute + the burst
            self.assertLessEqual(in_30, 300)
        self.assertGreaterEqual(stamps[-1], 1800.0)         # the 301st call had to wait for the window

    def test_estimate_models_the_30_minute_cap_and_preload_counts_old_calls(self):
        # the owner's real case: 3539 calls at --rate 60 with a 900 cap printed 59 min, really ~100
        self.assertAlmostEqual(estimate_minutes(3539, 60, 900), 900 / 60 + 2639 / 30, places=3)
        self.assertAlmostEqual(estimate_minutes(800, 240, 1800), 800 / 240, places=3)
        lim = RateLimiter(calls_per_min=240, max_per_30min=1800, clock=lambda: 5000.0)
        lim.preload([10, 100, 1700, 1900, -5])
        self.assertEqual(lim.window_used(), 3)


class TestWeekMode(unittest.TestCase):
    def test_three_batches_kill_resume_phone_folder_and_no_repeat_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = Wrap()
            settings, cli = _setup(tmp, tr)
            r1 = _week(settings, cli, tmp)
            self.assertTrue(r1["completed"])
            self.assertEqual((r1["start_date"], r1["end_date"], r1["kind"]), ("2026-11-07", "2026-11-13", "first"))
            phone = Path(r1["phone_path"])
            self.assertEqual(phone.name, "week_01_2026-11-07_to_2026-11-13")
            for name in ("week_summary.txt", "health.csv", "health_summary.txt", "README_EXPORT.txt"):
                self.assertTrue((phone / name).exists(), name)
            self.assertTrue(any(phone.glob("chain_csv/NIFTY/chain_NIFTY_*.csv")))
            self.assertTrue(any(phone.glob("store/*/opt_1m/underlying=NIFTY/trading_date=2026-11-1*/part-0.parquet")))
            self.assertFalse(any(p.name.endswith(".tmp") for p in phone.parent.iterdir()))
            self.assertTrue(Path(r1["zip_path"]).exists())
            for part in phone.rglob("*"):
                self.assertLess(len(part.name), 40, part.name)
            # still-listed contracts (expiry 24 Nov) went through V3, expired ones through the expired API:
            # nothing failed, and the listed option has candles on the days after the 9 Nov expiry
            from updl.state import FAILED_PERMANENT, State
            st = State(settings.state_dir)
            try:
                self.assertEqual(st.chunks(status=[FAILED_PERMANENT]), [])
            finally:
                st.close()
            opt13 = pd.read_parquet(next(phone.glob("store/*/opt_1m/underlying=NIFTY/trading_date=2026-11-13/part-0.parquet")))
            self.assertEqual(set(opt13["source"]), {"upstox_v3"})
            # holiday 10 Nov and the Sunday Muhurat 8 Nov are not trading days of the batch
            days = {p.parent.name for p in phone.glob("store/*/spot_1m/underlying=NIFTY/*/part-0.parquet")}
            self.assertEqual(days, {"trading_date=2026-11-09", "trading_date=2026-11-11",
                                    "trading_date=2026-11-12", "trading_date=2026-11-13"})

            # batch 2 is killed in the middle of its option downloads, then the same command resumes it
            seen1 = set(_candle_days(tr.urls))
            tr.urls.clear()
            tr.kill_after = tr.n_candle + 6
            r2 = _week(settings, cli, tmp)
            self.assertFalse(r2["completed"])
            self.assertTrue(r2.get("interrupted"))
            self.assertEqual(load_cursor(settings)["in_progress"]["start_date"], "2026-10-31")
            tr.kill_after = None
            r2 = _week(settings, cli, tmp)
            self.assertTrue(r2["completed"])
            self.assertEqual((r2["batch_no"], r2["start_date"], r2["end_date"]), (2, "2026-10-31", "2026-11-06"))
            seen2 = _candle_days(tr.urls)
            self.assertEqual(len(seen2), len(set(seen2)), "a chunk was requested twice across kill and resume")
            self.assertFalse(seen1 & set(seen2), "batch 2 asked again for something batch 1 already had")

            r3 = _week(settings, cli, tmp)
            self.assertEqual((r3["batch_no"], r3["start_date"], r3["end_date"]), (3, "2026-10-24", "2026-10-30"))
            index = (phone.parent / "INDEX.txt").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(index), 3)
            cur = load_cursor(settings)
            self.assertEqual((cur["newest_done"], cur["next_end"], cur["in_progress"]), ("2026-11-13", "2026-10-23", None))

    def test_newest_day_not_served_yet_stays_pending_and_is_caught_up_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = Wrap(empty_days={"2026-11-13"})
            settings, cli = _setup(tmp, tr)
            r1 = _week(settings, cli, tmp)
            self.assertEqual((r1["end_date"], r1["pending_days"]), ("2026-11-12", ["2026-11-13"]))
            from updl.state import State
            st = State(settings.state_dir)
            try:
                self.assertEqual(st.covered_keys_on("2026-11-13"), set())      # never recorded as empty
            finally:
                st.close()
            tr.empty_days.clear()               # some hours later Upstox serves the day
            r2 = _week(settings, cli, tmp)
            self.assertEqual((r2["kind"], r2["start_date"], r2["end_date"]), ("new", "2026-11-13", "2026-11-13"))
            r3 = _week(settings, cli, tmp)
            self.assertEqual((r3["kind"], r3["start_date"], r3["end_date"]), ("back", "2026-10-31", "2026-11-06"))

    def test_four_workers_give_the_same_store_as_one(self):
        frames = {}
        for workers in (1, 4):
            with tempfile.TemporaryDirectory() as tmp:
                settings, cli = _setup(tmp, Wrap(), workers=workers)
                res = _week(settings, cli, tmp)
                for table in ("opt_1m", "spot_1m", "chain_metrics_1m"):
                    self.assertFalse(Path(res["week_dir"]).exists())      # copied to the phone: no third copy kept
                    parts = sorted(Path(res["phone_path"]).glob(f"store/*/{table}/underlying=NIFTY/*/part-0.parquet"))
                    self.assertTrue(parts, table)
                    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
                    frames[(workers, table)] = df.drop(columns=[c for c in ("fetch_run_id",) if c in df.columns])
        for table in ("opt_1m", "spot_1m", "chain_metrics_1m"):
            pd.testing.assert_frame_equal(frames[(1, table)], frames[(4, table)])

    def test_old_month_cursor_is_explained_and_unwritable_phone_folder_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap())
            (settings.state_dir / "monthly_cursor.json").write_text(json.dumps(
                {"in_progress_batch": {"batch_no": 1, "start_date": "2026-10-14", "end_date": "2026-11-13"}}))
            blocker = Path(tmp) / "blocker"
            blocker.write_text("a file where the phone folder should be")
            buf = io.StringIO()
            from contextlib import redirect_stdout
            with redirect_stdout(buf):
                res = run_week_batches(settings, cli, calendar=load_calendar(REF_DIR), today_override=TODAY,
                                       phone_dir=str(blocker / "BOYU_Data"))
            out = buf.getvalue()
            self.assertIn("MAHINA", out)
            self.assertIn("termux-setup-storage", out)
            self.assertTrue(res["completed"])
            self.assertIsNone(res["phone_path"])
            self.assertTrue(Path(res["week_dir"]).exists())            # the data is safe on the Termux side


class TestCalendarFromApi(unittest.TestCase):
    def test_new_paths_are_allowed_and_neighbours_are_not(self):
        for ok in ("/v2/market/timings/2026-11-12", "/v2/market/holidays", "/v2/market/holidays/2025-10-21"):
            self.assertTrue(path_allowed(ok), ok)
        for bad in ("/v2/market/status/NSE", "/v2/market/timings", "/v2/market/timings/2026-11-12/x",
                    "/v2/market/holidays/2025/10", "/v2/market/smartlist/futures"):   # /v2/market/fii became allowed in v4.2
            self.assertFalse(path_allowed(bad), bad)

    def test_api_facts_override_the_tables_and_an_unknown_day_is_a_table_gap(self):
        from updl.calsync import parse_timings, sync_calendar
        ms = lambda h, m: int(dt.datetime(2025, 10, 22, h, m, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp() * 1000)
        open_day = [{"exchange": "NSE", "start_time": ms(9, 15), "end_time": ms(15, 30)},
                    {"exchange": "NFO", "start_time": ms(9, 15), "end_time": ms(15, 30)},
                    {"exchange": "MCX", "start_time": ms(9, 0), "end_time": ms(23, 30)}]
        self.assertEqual(parse_timings({"data": open_day}), {"NSE": ["09:15", "15:30"], "NFO": ["09:15", "15:30"]})
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            tr = Wrap(timings={"2025-10-22": [], "2025-10-23": open_day})
            base = load_calendar(REF_DIR)
            cal = sync_calendar(settings, _client(tr), base, [dt.date(2025, 10, 22), dt.date(2025, 10, 23),
                                                              dt.date(2025, 10, 24)])
            self.assertTrue(base.is_trading_day("2025-10-22"))          # the 2026-only table cannot know
            self.assertFalse(cal.is_trading_day("2025-10-22"))          # the API says: closed (Diwali Balipratipada)
            self.assertTrue(cal.is_trading_day("2025-10-23"))
            self.assertEqual(cal.expected_bars("nse_index_fo", "2025-10-23"), 15 * 60 + 30 - (9 * 60 + 15))
            self.assertEqual(cal.class_tag("nse_index_fo", "2025-10-23"), "SOURCE")
            self.assertTrue(cal.has_overlay("2025-10-23"))
            self.assertFalse(cal.has_overlay("2025-10-24"))             # the call failed: no fact, tables decide
            self.assertEqual(base.overlay, {})                          # the shared calendar is untouched
            n = len(tr.urls)
            sync_calendar(settings, _client(tr), base, [dt.date(2025, 10, 22), dt.date(2025, 10, 23)])
            self.assertEqual(len(tr.urls), n)                           # cached: no second call


class TestProgress(unittest.TestCase):
    def test_tty_line_and_plain_lines_and_no_secret(self):
        from updl.progress import Progress

        class Tty(io.StringIO):
            def isatty(self):
                return True

        now = [100.0]
        lim = RateLimiter(calls_per_min=240, max_per_30min=1800, clock=lambda: now[0])
        tty = Tty()
        p = Progress(stream=tty, limiter=lim, clock=lambda: now[0])
        p.stage(1)
        p.add_total(754)
        for _ in range(430):
            now[0] += 0.25
            p.advance("D06 NIFTY")
        line = p.render()
        self.assertRegex(line, r"^\[1/5 Download\] \[#+-+\]  57% 430/754 calls \| D06 NIFTY \| \d+/min \| "
                               r"elapsed \d\d:\d\d \| ETA \d\d:\d\d \| RUNNING$")
        self.assertIn("\r", tty.getvalue())
        self.assertTrue(line.isascii())
        lim.waiting, lim.wait_until = "RATE-CAP", now[0] + 192
        self.assertIn("WAITING-RATE-CAP (resume in 03:12)", p.render())
        lim.waiting, lim.wait_until = "PAUSE-429", now[0] + 8
        self.assertIn("RETRY-429 (8s)", p.render())

        plain = io.StringIO()
        q = Progress(stream=plain, limiter=lim, clock=lambda: now[0])
        q.stage(1)
        q.add_total(100)
        for _ in range(60):
            q.advance("D06 NIFTY")
        q.stage(2)
        q.tick("spot_1m NIFTY")
        q.note("Plan: A")
        text = plain.getvalue()
        self.assertNotIn("\r", text)
        self.assertEqual(text.count("[1/5 Download]"), 3)             # stage start + every 25 calls
        self.assertIn("[2/5 Compact]", text)
        for bad in ("Bearer", "eyJ", "Authorization"):
            self.assertNotIn(bad, text + tty.getvalue())


class TestVectorMaths(unittest.TestCase):
    def test_vector_iv_matches_the_scalar_solver(self):
        from updl.derive import black76_greeks, black76_price, solve_black76_iv
        from updl.ivfast import greeks_vec, solve_iv_vec
        rng = np.random.default_rng(7)
        n = 300
        F = rng.uniform(20000, 26000, n)
        K = np.round(F + rng.uniform(-1500, 1500, n), -1)
        T = rng.uniform(1, 30000, n) / 525600.0
        call = rng.random(n) < 0.5
        sig = rng.uniform(0.05, 0.9, n)
        price = np.array([black76_price(F[i], K[i], T[i], sig[i], "CE" if call[i] else "PE") for i in range(n)])
        price[:10] = 0.0                    # NO_SOLVE
        price[10:20] *= 0.2                 # mostly below intrinsic for in-the-money rows
        stale = np.zeros(n, dtype=bool)
        stale[20:25] = True
        iv, status = solve_iv_vec(price, F, K, T, call, stale)
        d, g, th = greeks_vec(F, K, T, iv, call)
        for i in range(n):
            ref = solve_black76_iv(price[i], F[i], K[i], T[i], "CE" if call[i] else "PE", is_stale=bool(stale[i]))
            self.assertEqual(status[i], ref["iv_status"], i)
            if ref["iv_status"] == "OK":
                self.assertAlmostEqual(iv[i], ref["iv_DERIVED"], delta=2e-5)
                rd, rg, rt = black76_greeks(F[i], K[i], T[i], iv[i], "CE" if call[i] else "PE")
                self.assertAlmostEqual(d[i], rd, places=9)
                self.assertAlmostEqual(g[i], rg, places=12)
                self.assertAlmostEqual(th[i], rt, places=9)
            else:
                self.assertTrue(np.isnan(iv[i]))

    def test_matrix_chain_metrics_match_the_row_version(self):
        from updl.chain import asof_chain, chain_metrics, metrics_matrix, oi_matrices
        from updl.timeutil import IST
        cal = load_calendar(REF_DIR)
        rng = np.random.default_rng(3)
        rows = []
        for strike in (24900.0, 24950.0, 25000.0, 25050.0, 25100.0):
            for side in ("CE", "PE"):
                for minute in rng.choice(np.arange(15, 45), size=12, replace=False):
                    rows.append((f"K{strike}{side}", strike, side, f"09:{int(minute):02d}", 100.0, 5,
                                 int(rng.integers(100, 9000))))
        odf = _opt_frame(rows, cal)
        times = [dt.datetime(2026, 5, 11, 9, m, tzinfo=IST) for m in range(16, 50)]
        spot = [float("nan")] * 3 + [25010.0 + i for i in range(len(times) - 3)]
        mats = oi_matrices(odf, times)
        (exp, m), = mats.items()
        got = metrics_matrix(m, spot)
        snap = asof_chain(odf, times)
        for j, t in enumerate(times):
            ref = chain_metrics(snap[snap["snapshot_ts"] == t], None if np.isnan(spot[j]) else spot[j])
            for name in ("pcr", "call_oi_wall", "put_oi_wall", "max_pain", "n_strikes", "n_oi_known", "n_oi_carried"):
                a, b = float(got[name][j]), float(ref[name])
                self.assertTrue((np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9, (name, j, a, b))


if __name__ == "__main__":
    unittest.main()
