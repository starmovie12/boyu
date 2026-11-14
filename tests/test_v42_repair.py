"""v4.2 tests: repair, failure policy (routing breaker, INCOMPLETE), day status / holes, weekday log, run lock.
Mocked HTTP only: zero real network calls, zero real tokens."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from updl.client import RawResponse
from updl.config import load_settings
from updl.errors import AlreadyRunning, RoutingSuspected
from updl.repair import run_repair
from updl.sessions import load_calendar
from updl.state import DONE, FAILED_PERMANENT, FAILED_RETRYABLE, State
from updl.weekly import load_cursor, next_batch

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v4_week import Wrap, _setup, _week


class TestRepair(unittest.TestCase):
    def test_1000_failures_are_queued_and_100_good_chunks_are_untouched(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            settings.ensure_dirs()
            days = cal.trading_days("2026-10-27", "2026-11-13")
            st = State(settings.state_dir)
            try:
                st.autocommit = False
                for i in range(100):
                    key = f"NSE_FO|{48000 + i}|27-10-2026"
                    st.set_chunk("D06", key, "2026-10-14", "2026-10-27", DONE, raw_path=f"raw/{i}", sha256="0" * 64,
                                 table_name="opt_1m", underlying="NIFTY")
                    st.mark_coverage(key, cal.trading_days("2026-10-14", "2026-10-27"), "D06", DONE, f"raw/{i}")
                for i in range(1000):
                    d = days[i % len(days)]
                    st.set_chunk("D06" if i % 4 else "D08", f"NSE_FO|{61000 + i // len(days)}", d, d, FAILED_PERMANENT,
                                 error="400:UDAPI1021", table_name="opt_1m", underlying="BANKNIFTY" if i % 3 else "NIFTY")
                st.flush()
                done_before = st.db.execute("SELECT * FROM chunk_status WHERE status='DONE' ORDER BY 2").fetchall()
                cov_before = st.db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]
            finally:
                st.close()
            buf = io.StringIO()
            with redirect_stdout(buf):
                res = run_repair(settings, calendar=cal)
            self.assertEqual((res["failed_rows"], res["retry_chunks"], res["routing_failures"], res["good_chunks"]),
                             (1000, 1000, 1000, 100))
            self.assertEqual(res["repair_days"], [d.isoformat() for d in days])
            self.assertIn("dobara download NAHI honge", buf.getvalue())
            st = State(settings.state_dir)
            try:
                self.assertEqual(st.chunk_counts(), {"DONE": 100})
                self.assertEqual(st.db.execute("SELECT * FROM chunk_status WHERE status='DONE' ORDER BY 2").fetchall(),
                                 done_before)
                self.assertEqual(st.db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0], cov_before)
            finally:
                st.close()
            cur = load_cursor(settings)
            self.assertEqual(cur["repair_days"][0], "2026-10-27")
            # the queue is served before newer days or the walk back, and only up to what Upstox serves
            cur.update({"newest_done": "2026-11-13", "next_end": "2026-11-06"})
            nb = next_batch(cur, dt.date(2026, 11, 14), dt.date(2026, 11, 13))
            self.assertEqual((nb["kind"], nb["start"], nb["end"], nb["next_end"]),
                             ("repair", dt.date(2026, 10, 27), dt.date(2026, 11, 2), dt.date(2026, 11, 6)))

    def test_a_hole_is_refetched_with_exactly_the_missing_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = Wrap()
            settings, cli = _setup(tmp, tr)
            r1 = _week(settings, cli, tmp)
            self.assertEqual((r1["status"], r1["failed"]), ("COMPLETE", {}))
            key, day = "NSE_FO|90002", "2026-11-13"       # a still-listed contract of the fixture
            st = State(settings.state_dir)
            try:
                self.assertIn(dt.date(2026, 11, 13), st.covered_dates(key))
                st.drop_coverage(key, [day])
                st.set_chunk("D06", key, day, day, FAILED_PERMANENT, error="400:UDAPI1021", table_name="opt_1m",
                             underlying="NIFTY")
                done_before = st.chunk_counts().get("DONE")
            finally:
                st.close()
            from updl.holes import day_status_rows
            hole = [r for r in day_status_rows(settings, load_calendar(REF_DIR), day, day)
                    if r["status"] == "MISSING_ERROR"]
            self.assertEqual([(r["underlying"], r["table"]) for r in hole], [("NIFTY", "opt_1m")])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rep = run_repair(settings, calendar=load_calendar(REF_DIR))
            self.assertEqual((rep["retry_chunks"], rep["repair_days"]), (1, [day]))
            tr.urls.clear()
            buf = io.StringIO()
            with redirect_stdout(buf):
                r2 = _week(settings, cli, tmp)
            self.assertEqual((r2["kind"], r2["start_date"], r2["end_date"], r2["status"]),
                             ("repair", day, day, "COMPLETE"))
            fo_calls = [u for u in tr.urls if "historical-candle" in u and "NSE_FO" in u]
            self.assertEqual(len(fo_calls), 1, fo_calls)                 # only the missing contract-day
            self.assertIn("/v3/historical-candle/NSE_FO%7C90002/", fo_calls[0])
            cur = load_cursor(settings)
            self.assertEqual((cur["repair_days"], cur["newest_done"], cur["next_end"]), ([], "2026-11-13", "2026-11-06"))
            st = State(settings.state_dir)
            try:
                self.assertEqual(st.chunk_counts().get("DONE"), done_before + 1)
                self.assertNotIn(FAILED_PERMANENT, st.chunk_counts())
            finally:
                st.close()
            r3 = _week(settings, cli, tmp)                              # then the normal walk back continues
            self.assertEqual((r3["kind"], r3["start_date"]), ("back", "2026-10-31"))


class RoutingBroken(Wrap):
    """Every still-listed F&O key is rejected, as if it had been sent to the wrong endpoint."""

    def get(self, url, headers=None, timeout=None):
        if "/v3/historical-candle/NSE_FO" in url:
            self.urls.append(url)
            body = {"status": "error", "errors": [{"errorCode": "UDAPI1021", "message": "key format"}]}
            return RawResponse(400, {}, json.dumps(body).encode())
        return super().get(url, headers, timeout)


class TestFailurePolicy(unittest.TestCase):
    def test_five_routing_errors_in_a_row_stop_the_run_and_are_never_permanent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = RoutingBroken()
            settings = load_settings(MANIFEST_PATH, workdir=str(Path(tmp) / "wd"), items="D12,D01,D03,D07,D05,D06")
            settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            import tests.test_v4_week as t4
            from unittest import mock
            from updl.probe import run_probe
            cli = t4._client(tr)
            run_probe(settings, cli, calendar=load_calendar(REF_DIR), probe_today=dt.date(2026, 11, 14))
            cli.budget = None
            tr.urls.clear()                     # the probe's own two F&O test calls do not count
            # the fixture master lists 3 NIFTY contracts (1 future, 2 options): use a limit of 2 for the test
            patcher = mock.patch("updl.plan.ROUTING_BREAKER_LIMIT", 2)
            patcher.start()
            self.addCleanup(patcher.stop)
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(RoutingSuspected):
                _week(settings, cli, tmp)
            self.assertIn("UDAPI1021", buf.getvalue())
            bad_calls = [u for u in tr.urls if "/v3/historical-candle/NSE_FO" in u]
            self.assertEqual(len(bad_calls), 2)                         # stopped at the limit: no range splitting
            st = State(settings.state_dir)
            try:
                counts = st.chunk_counts()
            finally:
                st.close()
            self.assertNotIn(FAILED_PERMANENT, counts)
            self.assertGreaterEqual(counts.get(FAILED_RETRYABLE, 0), 1)
            self.assertIsNotNone(load_cursor(settings)["in_progress"])   # the batch is not marked finished

    def test_newest_day_not_served_is_an_allowed_hole_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap(empty_days={"2026-11-13"}))
            res = _week(settings, cli, tmp)
            self.assertEqual(res["status"], "COMPLETE")
            self.assertEqual(res["day_status"].get("NOT_YET_SERVED"), 4)       # 1 index x 4 tables
            holes = (settings.reports_dir / "holes.csv").read_text(encoding="utf-8")
            self.assertIn("2026-11-13,NIFTY,spot_1m,NOT_YET_SERVED", holes)
            self.assertNotIn("MISSING_ERROR", holes)
            self.assertTrue((Path(res["phone_path"]) / "holes.csv").exists())


class TestWeekdayLog(unittest.TestCase):
    def test_one_line_per_expiry_and_holiday_moves_are_not_warnings(self):
        from updl.master import listed_contracts
        from updl.timeutil import IST
        cal = load_calendar(REF_DIR)
        ms = lambda d: int(dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp() * 1000)
        rows = []
        for exp in (dt.date(2026, 11, 23), dt.date(2026, 11, 17), dt.date(2026, 12, 2)):     # Mon (24 Nov holiday), Tue, Wed
            for i in range(50):
                rows.append({"underlying_key": "NSE_INDEX|Nifty 50", "instrument_type": "CE", "expiry": ms(exp),
                             "instrument_key": f"NSE_FO|{exp.day}{i:03d}", "strike_price": 25000.0 + i})
        logs = []
        out = listed_contracts(rows, "NSE_INDEX|Nifty 50", logger=lambda ev, **kw: logs.append(ev),
                               rule="weekly_tuesday", calendar=cal)
        self.assertEqual(len(out), 150)
        self.assertEqual(sorted(logs), ["expiry_moved_holiday", "weekday_mismatch"])


class TestRunLock(unittest.TestCase):
    def test_second_run_is_refused_stale_lock_is_replaced_and_broken_db_is_detected(self):
        from updl.runlock import RunLock, sqlite_ok
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            with RunLock(state, "continue"):
                self.assertTrue((state / "run.lock").exists())
                # the same lock held by another live process (our parent) blocks a second writer
                rec = json.loads((state / "run.lock").read_text())
                rec["pid"] = os.getppid()
                (state / "run.lock").write_text(json.dumps(rec))
                with self.assertRaises(AlreadyRunning) as ctx:
                    RunLock(state, "repair").__enter__()
                self.assertIn("pehle se ek updl chal raha hai", ctx.exception.message)
            self.assertFalse((state / "run.lock").exists())
            # a lock left by a process that died (phone switched off) or that is a day old is stale
            (state / "run.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "started": 1.0, "command": "continue"}))
            with RunLock(state, "continue"):
                self.assertEqual(json.loads((state / "run.lock").read_text())["pid"], os.getpid())
            good, bad = state / "good.sqlite3", state / "bad.sqlite3"
            import sqlite3
            db = sqlite3.connect(good)
            db.execute("CREATE TABLE t(x)")
            db.commit()
            db.close()
            bad.write_bytes(b"this is not a database file at all" * 50)
            self.assertTrue(sqlite_ok(good))
            self.assertFalse(sqlite_ok(bad))


if __name__ == "__main__":
    unittest.main()
