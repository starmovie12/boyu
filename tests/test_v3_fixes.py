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


def _expired_override(base, status, code, msg="x", require_header=False):
    """Wrap the fixture: expired endpoints answer with an error (optionally only when no token is sent)."""
    import json as _json
    from updl.client import RawResponse

    class T:
        def __init__(self):
            self.requests = []

        def get(self, url, headers=None, timeout=None):
            self.requests.append((url, headers))
            if "/v2/expired-instruments/" in url:
                has_auth = any(k.lower() == "authorization" for k in (headers or {}))
                if not require_header or not has_auth:
                    body = {"status": "error", "errors": [{"errorCode": code, "message": msg}]}
                    return RawResponse(status, {}, _json.dumps(body).encode())
            return base.get(url, headers, timeout)
    return T()


class TestTokenAndPlanErrors(unittest.TestCase):
    def test_client_reads_error_code_before_treating_401_as_expired_token(self):
        import json as _json
        from updl.client import RawResponse, v3_candle_path
        from updl.errors import PlusRequired, TokenExpired, TokenNotPermitted
        from updl.messages import TOKEN_EXPIRED, TOKEN_MISSING
        from updl.tokens import Tokens

        def answer(status, code):
            body = _json.dumps({"status": "error", "errors": [{"errorCode": code, "message": "m"}]}).encode()
            return type("T", (), {"get": lambda self, u, headers=None, timeout=None: RawResponse(status, {}, body)})()

        path = "/v2/expired-instruments/expiries"
        with self.assertRaises(PlusRequired):
            _client(answer(401, "UDAPI1149"), Tokens(access="dummy_value")).get(path, {"instrument_key": "x"}, "expired")
        with self.assertRaises(PlusRequired):
            _client(answer(403, "UDAPI1149"), Tokens(access="dummy_value")).get(path, {"instrument_key": "x"}, "expired")
        with self.assertRaises(TokenNotPermitted):
            _client(answer(401, "UDAPI100067"), Tokens(analytics="dummy_value")).get(path, {"instrument_key": "x"}, "expired")
        with self.assertRaises(TokenExpired) as ctx:
            _client(answer(401, "UDAPI100050"), Tokens(access="dummy_value")).get(path, {"instrument_key": "x"}, "expired")
        self.assertEqual(ctx.exception.message, TOKEN_EXPIRED)
        with self.assertRaises(TokenExpired) as ctx2:
            _client(answer(401, "UDAPI100050"), Tokens()).get(
                v3_candle_path("NSE_INDEX|Nifty 50", "days", 1, "2026-05-11", "2026-05-11"))
        self.assertEqual(ctx2.exception.message, TOKEN_MISSING)

    def test_analytics_token_on_expired_endpoints_only_after_probe_proof(self):
        from updl.tokens import Tokens
        self.assertIsNone(Tokens(analytics="a_value").for_kind("expired"))
        self.assertEqual(Tokens(analytics="a_value").for_kind("expired", analytics_ok_for_expired=True), "a_value")
        self.assertEqual(Tokens(analytics="a_value", access="b_value").for_kind("expired"), "b_value")
        # probe with only the analytics token: P09 tests it, and a success lets the probe continue with it
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            tr = _expired_override(_build_fixture_transport("A"), 401, "UDAPI100050", require_header=True)
            rep = run_probe(settings, _client(tr, Tokens(analytics="dummy_value")), calendar=cal,
                            probe_today=dt.date(2026, 11, 11))
            self.assertTrue(rep["analytics_accepted_for_expired"])
            self.assertIn(rep["selected_plan"], ("A", "A_PARTIAL"))

    def test_probe_falls_back_to_plan_b_when_plus_error_comes_as_401(self):
        from updl.tokens import Tokens
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            tr = _expired_override(_build_fixture_transport("A"), 401, "UDAPI1149", "Plus only")
            rep = run_probe(settings, _client(tr, Tokens(analytics="dummy_value")), calendar=cal,
                            probe_today=dt.date(2026, 11, 11))
            self.assertEqual(rep["selected_plan"], "B")
            self.assertEqual(rep["tests"]["P09"]["reason"], "PLUS_REQUIRED")
            self.assertTrue(settings.probe_txt.exists())

    def test_probe_without_token_writes_report_and_blocks_download(self):
        from updl.errors import ProbeMissing, TokenExpired
        from updl.probe import check_probe_fresh
        from updl.tokens import Tokens
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(MANIFEST_PATH, workdir=tmp)
            tr = _expired_override(_build_fixture_transport("A"), 401, "UDAPI100050", require_header=True)
            with self.assertRaises(TokenExpired):
                run_probe(settings, _client(tr, Tokens()), calendar=cal, probe_today=dt.date(2026, 11, 11))
            self.assertTrue(settings.probe_txt.exists())
            self.assertIn("BLOCKED_TOKEN", settings.probe_txt.read_text(encoding="utf-8"))
            with self.assertRaises(ProbeMissing):
                check_probe_fresh(settings)


def _opt_frame(rows, cal):
    """rows: (key, strike, side, hhmm, close, vol, oi) for 2026-05-11, expiry 2026-05-12."""
    import pandas as pd
    from updl.store import parse_candles, rows_to_frame
    frames = []
    for key, strike, side, hhmm, close, vol, oi in rows:
        ts = f"2026-05-11T{hhmm}:00+05:30"
        r, _ = parse_candles({"data": {"candles": [[ts, close, close, close, close, vol, oi]]}})
        frames.append(rows_to_frame(r, {"table": "opt_1m", "instrument_key": key, "underlying": "NIFTY",
                                        "expiry_date": "2026-05-12", "strike": strike, "option_type": side},
                                    cal, "r1", "0" * 64))
    return pd.concat(frames, ignore_index=True)


class TestChainAsOf(unittest.TestCase):
    def test_asof_snapshot_uses_only_closed_candles_and_flags_carried_values(self):
        import math
        from updl.chain import asof_chain, chain_metrics
        from updl.timeutil import IST
        cal = load_calendar(REF_DIR)
        odf = _opt_frame([
            ("K1", 25000.0, "CE", "09:15", 100.0, 10, 1000),
            ("K1", 25000.0, "CE", "09:16", 101.0, 5, 1100),
            ("K2", 25000.0, "PE", "09:15", 90.0, 7, 3000),
            ("K3", 25100.0, "CE", "09:16", 60.0, 4, 500),
        ], cal)
        t916 = dt.datetime(2026, 5, 11, 9, 16, tzinfo=IST)
        t917 = dt.datetime(2026, 5, 11, 9, 17, tzinfo=IST)
        snap = asof_chain(odf, [t916, t917])
        s916 = snap[snap["snapshot_ts"] == t916].set_index("strike")
        s917 = snap[snap["snapshot_ts"] == t917].set_index("strike")
        # at 09:16 only the 09:15 candles have closed: the 09:16 candle (close 101) is NOT visible yet
        self.assertEqual(s916.loc[25000.0, "ce_close"], 100.0)
        self.assertFalse(s916.loc[25000.0, "ce_is_filled"])
        self.assertTrue(math.isnan(s916.loc[25100.0, "ce_close"]))
        self.assertTrue(math.isnan(s916.loc[25100.0, "ce_oi"]))
        # at 09:17 the PE value is carried from 09:15 and says so; nothing is turned into 0
        self.assertEqual(s917.loc[25000.0, "ce_close"], 101.0)
        self.assertEqual(s917.loc[25000.0, "pe_oi"], 3000.0)
        self.assertTrue(s917.loc[25000.0, "pe_is_filled"])
        self.assertEqual(s917.loc[25000.0, "pe_age_min"], 1.0)
        self.assertTrue(math.isnan(s917.loc[25000.0, "pe_vol_1m"]))
        self.assertEqual(s917.loc[25000.0, "ce_vol_cum"], 15.0)
        met = chain_metrics(s917.reset_index(), spot=25010.0)
        self.assertAlmostEqual(met["pcr"], 3000.0 / (1100.0 + 500.0))
        self.assertEqual(met["call_oi_wall"], 25100.0)
        self.assertEqual(met["put_oi_wall"], 25000.0)
        self.assertGreaterEqual(met["n_oi_carried"], 1)

    def test_max_pain_small_example(self):
        import numpy as np
        from updl.chain import max_pain
        k = np.array([100.0, 110.0, 120.0])
        # heavy put OI at 120 and heavy call OI at 100 -> buyers lose most around 110
        self.assertEqual(max_pain(k, np.array([500.0, 10.0, 10.0]), np.array([10.0, 10.0, 500.0])), 110.0)

    def test_derive_never_uses_a_later_spot_when_a_spot_minute_is_missing(self):
        import json as _json
        from updl.derive import run_derive
        from updl.store import parse_candles, part_path, read_parquet, rows_to_frame, write_parquet
        from tests.test_updl import _make_full_day_candles
        cal = load_calendar(REF_DIR)
        d = dt.date(2026, 5, 11)
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(MANIFEST_PATH, workdir=tmp, test_start="2026-05-11", test_end="2026-05-11")
            candles = [c for c in _make_full_day_candles(d, "09:15", "09:30", base_price=25000.0)
                       if "T09:20:" not in c[0]]
            candles[-1] = [candles[-1][0], 26000.0, 26002.0, 25998.0, 26000.0, 0, 0]   # a far later close
            r_s, _ = parse_candles({"data": {"candles": candles}})
            write_parquet(rows_to_frame(r_s, {"table": "spot_1m", "instrument_key": "NSE_INDEX|Nifty 50",
                                              "underlying": "NIFTY", "option_type": "SPOT"}, cal, "r1", "0" * 64),
                          part_path(s.store_dir, "dev", "spot_1m", "NIFTY", d))
            odf = _opt_frame([("K1", 25000.0, "CE", "09:20", 120.0, 5, 1000),
                              ("K2", 25000.0, "PE", "09:20", 110.0, 5, 1200)], cal)
            write_parquet(odf, part_path(s.store_dir, "dev", "opt_1m", "NIFTY", d))
            run_derive(s, cal)
            iv = read_parquet(part_path(s.store_dir, "dev", "iv_greeks_1m", "NIFTY", d))
            self.assertEqual(set(iv["moneyness_bucket"].dropna().astype(int)), {0})
            met = read_parquet(part_path(s.store_dir, "dev", "chain_metrics_1m", "NIFTY", d))
            self.assertAlmostEqual(float(met["pcr_DERIVED"].iloc[0]), 1.2)
            self.assertIn("max_pain_DERIVED", met.columns)


class TestReferenceTables(unittest.TestCase):
    def test_bse_fo_close_and_settlement_times_follow_the_sebi_circulars(self):
        from updl.config import load_yaml
        from updl.derive import _settlement_time_ist
        cal = load_calendar(REF_DIR)
        self.assertEqual(cal.session_times("bse_index_fo", "2026-07-31")[1], "15:30")
        self.assertEqual(cal.session_times("bse_index_fo", "2026-08-03")[1], "15:40")
        self.assertEqual(cal.session_times("nse_index_fo", "2026-12-07")[1], "15:45")
        self.assertEqual(cal.session_times("bse_index_fo", "2026-12-07")[1], "15:45")
        iv_cfg = load_yaml(REF_DIR / "iv_model.yaml")
        self.assertEqual(_settlement_time_ist(dt.date(2026, 7, 28), iv_cfg).strftime("%H:%M"), "15:30")
        self.assertEqual(_settlement_time_ist(dt.date(2026, 11, 17), iv_cfg).strftime("%H:%M"), "15:35")
        self.assertEqual(_settlement_time_ist(dt.date(2026, 12, 8), iv_cfg).strftime("%H:%M"), "15:30")
