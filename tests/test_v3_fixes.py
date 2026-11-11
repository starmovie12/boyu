"""v3 regression tests (audit 2026-11). Mocked HTTP only: zero real network calls, zero real tokens."""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from updl.client import HttpClient, RateLimiter
from updl.config import load_settings
from updl.probe import run_probe
from updl.sessions import load_calendar
from updl.state import Catalog
from updl.store import list_parts, date_of_part

from tests.test_updl import MANIFEST_PATH, REF_DIR, _build_fixture_transport


def _client(transport, tokens=None):
    kw = {"transport": transport, "limiter": RateLimiter(6000, sleep=lambda s: None), "sleep": lambda s: None}
    if tokens is not None:
        kw["tokens"] = tokens
    return HttpClient(**kw)


class TestFirstSeen(unittest.TestCase):
    def test_first_seen_needs_evidence_and_only_moves_earlier(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(Path(tmp))
            try:
                # first candle on the requested start day: no evidence about earlier days -> nothing recorded
                cat.note_first_seen("K1", "2026-10-12", requested_from="2026-10-12")
                self.assertNotIn("K1", cat.first_seen())
                # first candle after the requested start: the contract showed nothing earlier -> recorded
                cat.note_first_seen("K2", "2026-10-15", requested_from="2026-10-12")
                self.assertEqual(cat.first_seen()["K2"], dt.date(2026, 10, 15))
                # a later request that finds an earlier candle lowers the record (a candle proves existence)
                cat.note_first_seen("K2", "2026-10-13", requested_from="2026-10-13")
                self.assertEqual(cat.first_seen()["K2"], dt.date(2026, 10, 13))
                # a later date never raises it
                cat.note_first_seen("K2", "2026-10-20", requested_from="2026-10-01")
                self.assertEqual(cat.first_seen()["K2"], dt.date(2026, 10, 13))
                # legacy call without requested_from records nothing new
                cat.note_first_seen("K3", "2026-10-20")
                self.assertNotIn("K3", cat.first_seen())
            finally:
                cat.close()

    def test_continue_mode_keeps_contracts_that_straddle_a_month_boundary(self):
        """Batch 1 downloads the 2026-11-09 expiry from its window start; batch 2 (earlier month) must still
        select that same contract for its own days. Before v3 the batch-1 first candle was stored as the
        contract's first-seen date and batch 2 silently skipped every option for those days."""
        from updl.monthly import run_monthly_continue
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp, items="D12,D01,D03,D07,D06")
            settings.instruments = {k: v for k, v in settings.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            cli = _client(_build_fixture_transport("A"))
            run_probe(settings, cli, calendar=cal, probe_today=dt.date(2026, 11, 11))
            cli.budget = None
            phone = Path(tmp) / "phone"
            # derive (IV solving) is not under test here and is slow; skip it to keep the suite fast
            patcher = mock.patch("updl.monthly.run_derive", lambda *a, **k: {"files_written": 0})
            patcher.start()
            self.addCleanup(patcher.stop)
            b1 = run_monthly_continue(settings, cli, calendar=cal, today_override="2026-11-11", phone_dir=str(phone))
            b2 = run_monthly_continue(settings, cli, calendar=cal, today_override="2026-11-11", phone_dir=str(phone))
            self.assertEqual(b1["batch_no"], 1)
            self.assertEqual(b2["batch_no"], 2)
            opt_days = {date_of_part(p) for p in list_parts(settings.store_dir, "dev", "opt_1m", "NIFTY")}
            opt_days |= {date_of_part(p) for p in list_parts(settings.store_dir, "holdout", "opt_1m", "NIFTY")}
            b2_days = set(cal.trading_days(b2["start_date"], b2["end_date"]))
            self.assertTrue(b2_days, "batch 2 has trading days")
            self.assertEqual(sorted(b2_days - opt_days), [], "batch-2 days without option candles")


if __name__ == "__main__":
    unittest.main()


class TestCompactLatestFetchWins(unittest.TestCase):
    def test_next_morning_refetch_wins_even_when_its_run_id_sorts_first(self):
        import json as _json
        from updl.state import DONE, State
        from updl.store import compact, read_parquet, write_raw
        from tests.test_updl import _make_full_day_candles
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            settings.ensure_dirs()
            d = dt.date(2026, 5, 12)
            key = "NSE_INDEX|Nifty 50"
            st = State(settings.state_dir)
            try:
                for run_id, frm, price, fetched in (("r220000", dt.date(2026, 5, 11), 100.0, "2026-05-12T16:30:00Z"),
                                                    ("r080000", d, 200.0, "2026-05-13T02:30:00Z")):
                    body = _json.dumps({"status": "success", "data": {
                        "candles": _make_full_day_candles(d, "09:15", "09:20", base_price=price)}}).encode()
                    meta = {"item_id": "D01", "table": "spot_1m", "instrument_key": key, "underlying": "NIFTY",
                            "expiry_date": None, "strike": None, "option_type": "SPOT", "interval": "1m",
                            "source": "upstox_v3"}
                    w = write_raw(settings.raw_dir, "D01", key, frm, d, run_id, body, meta)
                    st.set_chunk("D01", key, frm, d, DONE, raw_path=w["path"], sha256=w["sha256"], run_id=run_id,
                                 table_name="spot_1m", underlying="NIFTY")
                    st.db.execute("UPDATE chunk_status SET fetched_utc=? WHERE fetch_run_id=?", (fetched, run_id))
                    st.db.commit()
            finally:
                st.close()
            compact(settings, cal)
            parts = list_parts(settings.store_dir, "dev", "spot_1m", "NIFTY")
            self.assertEqual(len(parts), 1)
            df = read_parquet(parts[0])
            self.assertEqual(len(df), 5)
            self.assertEqual(set(df["fetch_run_id"]), {"r080000"})
            self.assertTrue((df["o"] >= 200.0).all())
            self.assertNotIn("_fetched_utc", df.columns)
            # re-running compact is stable
            compact(settings, cal)
            df2 = read_parquet(parts[0])
            self.assertEqual(set(df2["fetch_run_id"]), {"r080000"})
