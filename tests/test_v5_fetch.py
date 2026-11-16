"""v5 tests (PROMPT_05): `fetch` = one index, N weeks back from today, into Download/BOYU_Data/<INDEX>/.
Mocked HTTP only: zero real network calls, zero real tokens."""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote_plus

from updl.client import HttpClient, RateLimiter, RawResponse
from updl.config import load_settings
from updl.fetch import (MenuStop, choose, cursor_path, load_fetch_cursor, mark_fetch_repair, max_weeks,
                        parse_weeks, plan_windows, run_fetch)
from updl.probe import run_probe
from updl.sessions import load_calendar
from updl.state import State

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v4_week import TODAY, Wrap

D = dt.date
FLOOR = D(2024, 10, 1)
STOCKS = {}
with open(REF_DIR / "stocks_default.csv", encoding="utf-8") as _fh:
    for _r in csv.DictReader(_fh):
        STOCKS.setdefault(_r["group"], set()).add(_r["isin"])
OTHER = {"BANKNIFTY": ("Nifty 50", "SENSEX", "NSE_FO|5380", "NSE_FO|9000", "BSE_FO", "GLOBAL"),
         "NIFTY": ("Nifty Bank", "SENSEX", "NSE_FO|6380", "BSE_FO", "GLOBAL"),
         "SENSEX": ("Nifty", "NSE_FO", "GLOBAL")}


class IndexWrap(Wrap):
    """The base fixture gives every index the same expired contract keys; real ones differ per index."""

    def get(self, url, headers=None, timeout=None):
        r = super().get(url, headers, timeout)
        plain = unquote_plus(url)
        if "/expired-instruments/" in url and "/contract" in url and "Bank" in plain:
            body = r.content.replace(b"NSE_FO|63800|", b"NSE_FO|73800|").replace(b"NSE_FO|5380", b"NSE_FO|6380")
            return RawResponse(r.status_code, r.headers, body)
        if "/expired-instruments/future/contract" in url and "BSE_INDEX" in plain:      # BSE futures are BSE_FO
            return RawResponse(r.status_code, r.headers, r.content.replace(b"NSE_FO|63800|", b"BSE_FO|83800|"))
        return r


def _client(tr):
    """No real waits, and a 30-minute cap far above what these scenarios use (the cap itself is tested in v4)."""
    return HttpClient(transport=tr, limiter=RateLimiter(60000, max_per_30min=10 ** 7, sleep=lambda s: None),
                      sleep=lambda s: None)


def _urls(tr):
    return [unquote_plus(u) for u in tr.urls]


def _candle_urls(tr, index_key_day=None):
    """1-minute / daily candle downloads (the one-day 'is the newest day served' check is not a download)."""
    out = []
    for u in _urls(tr):
        if "historical-candle" not in u:
            continue
        parts = u.split("/")
        if index_key_day and parts[-1] == parts[-2] == index_key_day[1] and index_key_day[0] in u:
            continue
        out.append(u)
    return out


def _settings(tmp):
    s = load_settings(MANIFEST_PATH, workdir=str(Path(tmp) / "wd"), external_dir=str(Path(tmp) / "ext"))
    s.workers = 1          # the fake "kill" is raised inside the transport: keep it on the main thread
    return s


def _probe(tmp, cli, index):
    s = _settings(tmp)
    s.restrict_to(index)
    run_probe(s, cli, calendar=load_calendar(REF_DIR), probe_today=D(2026, 11, 14))
    cli.budget = None


def _fetch(tmp, cli, index, weeks, today=TODAY):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = run_fetch(_settings(tmp), cli, index, weeks, calendar=load_calendar(REF_DIR), today_override=today,
                        phone_dir=str(Path(tmp) / "phone"))
    res["_out"] = buf.getvalue()
    return res


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class TestMenu(unittest.TestCase):
    def _choose(self, answers, **kw):
        s = load_settings(MANIFEST_PATH)
        said, it = [], iter(answers)

        def ask(prompt):
            said.append(prompt)
            item = next(it)
            if isinstance(item, BaseException):
                raise item
            return item
        res = choose(s, ask=ask, say=said.append, today=D(2026, 11, 14), calendar=load_calendar(REF_DIR),
                     phone_dir="/x/BOYU_Data", **kw)
        return res, said

    def test_index_numbers_weeks_and_summary_line(self):
        for n, name in (("1", "BANKNIFTY"), ("2", "NIFTY"), ("3", "SENSEX")):
            (idx, weeks), said = self._choose([n, "15", ""])
            self.assertEqual((idx, weeks), (name, 15))
        (idx, weeks), said = self._choose(["1", "15", ""])
        self.assertIn("Kis index ka data chahiye?", said)
        self.assertIn("  1 = BANK NIFTY", said)
        self.assertIn("  2 = NIFTY 50", said)
        self.assertIn("BANK NIFTY | 15 hafte | 2026-08-01 se 2026-11-13", said)
        self.assertIn("Folder: /x/BOYU_Data/BANKNIFTY/", said)
        (_i, weeks), _s = self._choose(["2", "1", ""])
        self.assertEqual(weeks, 1)

    def test_wrong_answers_are_asked_again_and_three_stop_cleanly(self):
        (idx, _w), said = self._choose(["7", "abc", "1", "4", ""])
        self.assertEqual(idx, "BANKNIFTY")
        self.assertEqual(sum("Galat jawab" in x for x in said), 2)
        with self.assertRaises(MenuStop) as ctx:
            self._choose(["9", "", "x"])
        self.assertEqual(ctx.exception.code, 2)
        for bad in ("0", "-2", "abc", "", "2.5", " ", "1,5"):
            self.assertIsNone(parse_weeks(bad)[0], bad)
            self.assertTrue(parse_weeks(bad)[1], bad)
        self.assertEqual(parse_weeks("15"), (15, None))
        with self.assertRaises(MenuStop) as ctx:
            self._choose(["1", "0", "-2", "2.5"])
        self.assertEqual(ctx.exception.code, 2)
        (_i, weeks), said = self._choose(["1", "abc", "", "6", ""])
        self.assertEqual(weeks, 6)

    def test_too_many_weeks_are_clamped_and_ctrl_c_and_no(self):
        top = max_weeks(D(2026, 11, 13), FLOOR)
        (_i, weeks), said = self._choose(["1", "500", "", ""])
        self.assertEqual(weeks, top)
        self.assertTrue(any(f"sirf {top} hafte milenge" in x for x in said))
        with self.assertRaises(MenuStop) as ctx:
            self._choose(["1", "500", "n"])
        self.assertEqual(ctx.exception.code, 0)
        with self.assertRaises(MenuStop) as ctx:
            self._choose([KeyboardInterrupt()])
        self.assertEqual(ctx.exception.code, 130)
        self.assertIn("Ruk gaya", ctx.exception.message)
        with self.assertRaises(MenuStop) as ctx:
            self._choose(["1", EOFError()])
        self.assertEqual(ctx.exception.code, 130)
        with self.assertRaises(MenuStop) as ctx:
            self._choose(["1", "4", "n"])
        self.assertEqual(ctx.exception.code, 0)

    def test_flags_and_non_interactive(self):
        (idx, weeks), said = self._choose([], index_arg="banknifty", weeks_arg="2", yes=True, interactive=False)
        self.assertEqual((idx, weeks), ("BANKNIFTY", 2))
        (idx, weeks), said = self._choose(["3", ""], index_arg="NIFTY 50")
        self.assertEqual((idx, weeks), ("NIFTY", 3))
        for kw in ({"index_arg": "BANKNIFTY"}, {"weeks_arg": "2", "yes": True}, {"index_arg": "NIFTY", "weeks_arg": "2"}):
            with self.assertRaises(MenuStop) as ctx:
                self._choose([], interactive=False, **kw)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("--index BANKNIFTY --weeks 15 --yes", ctx.exception.message)
        with self.assertRaises(MenuStop):
            self._choose([], index_arg="BANK", weeks_arg="2", yes=True, interactive=False)
        with self.assertRaises(MenuStop):
            self._choose([], index_arg="BANKNIFTY", weeks_arg="0", yes=True, interactive=False)


class TestWindows(unittest.TestCase):
    def test_exact_dates_contiguous_and_floor(self):
        self.assertEqual(plan_windows(None, D(2026, 11, 13), 2, FLOOR),
                         [(D(2026, 11, 7), D(2026, 11, 13)), (D(2026, 10, 31), D(2026, 11, 6))])
        w15 = plan_windows(None, D(2026, 11, 13), 15, FLOOR)
        self.assertEqual(len(w15), 15)
        self.assertEqual((w15[0][1], w15[-1][0]), (D(2026, 11, 13), D(2026, 8, 1)))
        for (s1, e1), (s2, e2) in zip(w15, w15[1:]):
            self.assertEqual(s1 - dt.timedelta(days=1), e2)          # no gap, no overlap
            self.assertEqual((e1 - s1).days, 6)
        top = max_weeks(D(2026, 11, 13), FLOOR)
        wall = plan_windows(None, D(2026, 11, 13), 999, FLOOR)
        self.assertEqual(len(wall), top)
        self.assertEqual(wall[-1][0], FLOOR)
        # days served after the anchor come first, the grid stays where it was
        w = plan_windows(D(2026, 11, 13), D(2026, 11, 17), 2, FLOOR)
        self.assertEqual(w[0], (D(2026, 11, 14), D(2026, 11, 17)))
        self.assertEqual(w[1], (D(2026, 11, 7), D(2026, 11, 13)))
        self.assertEqual(w[-1][0], D(2026, 10, 31))


class TestFetchRuns(unittest.TestCase):
    def test_banknifty_then_more_weeks_then_nifty_then_next_day_and_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = IndexWrap()
            cli = _client(tr)
            # 8. the probe of a BANKNIFTY fetch touches only BANKNIFTY and VIX
            _probe(tmp, cli, "BANKNIFTY")
            pu = _urls(tr)
            for word in ("Nifty 50", "SENSEX", "GLOBAL", "USDINR", "GIFT"):
                self.assertFalse([u for u in pu if word in u], word)
            self.assertTrue(any("Nifty Bank" in u for u in pu))
            rep = json.loads((Path(tmp) / "wd" / "probe_report.json").read_text())
            self.assertEqual(rep["probed_indices"], ["BANKNIFTY"])
            self.assertIsNone(rep["sensex_option_history_available"])
            tr.urls.clear()

            # 2 + 3 + 6: two weeks, exact dates, only BANKNIFTY, per-index phone folder
            r1 = _fetch(tmp, cli, "BANKNIFTY", 2)
            self.assertEqual((r1["start"], r1["end"], r1["complete"], r1["stopped"]),
                             ("2026-10-31", "2026-11-13", 2, False))
            self.assertIn("FETCH BANK NIFTY: POORA (COMPLETE)", r1["_out"])
            urls = _urls(tr)
            for word in OTHER["BANKNIFTY"]:
                self.assertFalse([u for u in urls if word in u], word)
            eq = {u.split("NSE_EQ|")[1].split("/")[0] for u in urls if "NSE_EQ|" in u}
            self.assertTrue(eq and eq <= STOCKS["BANK"], eq)
            self.assertFalse(eq & (STOCKS["HEAVY"] - STOCKS["BANK"]))            # no RELIANCE / INFY
            wd = Path(tmp) / "wd"
            self.assertEqual(sorted({p.parent.parent.name for p in (wd / "store").rglob("part-0.parquet")
                                     if p.parent.parent.name.startswith("underlying=") and
                                     p.parent.parent.parent.name in ("spot_1m", "opt_1m", "fut_1m")}),
                             ["underlying=BANKNIFTY"])
            for rep_name in ("health.csv", "day_status.csv", "strategy_ready_days.csv", "holes.csv"):
                unds = {r["underlying"] for r in _rows(wd / "reports" / rep_name)}
                self.assertFalse(unds & {"NIFTY", "SENSEX"}, rep_name)
            not_ok = [r for r in _rows(wd / "reports" / "health.csv") if r["status"] == "NOT OK"]
            self.assertFalse([r for r in not_ok if r["underlying"] != "BANKNIFTY"])
            phone = Path(tmp) / "phone" / "BANKNIFTY"
            weeks = sorted(p.name for p in phone.iterdir() if p.is_dir())
            self.assertEqual(weeks, ["week_01_2026-11-07_to_2026-11-13", "week_02_2026-10-31_to_2026-11-06"])
            w1 = phone / weeks[0]
            self.assertTrue((w1 / "BANKNIFTY" / "spot_1m.csv").exists())
            self.assertFalse((w1 / "NIFTY").exists())
            self.assertTrue((w1 / "README_FOR_AI.md").exists())
            self.assertFalse([p for p in (w1 / "store").rglob("*") if "underlying=NIFTY" in str(p)])
            index_lines = (phone / "INDEX.txt").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(index_lines), 2)
            self.assertTrue(index_lines[0].startswith("week_01_2026-11-07_to_2026-11-13 | 2026-11-07..2026-11-13"))
            cur = load_fetch_cursor(_settings(tmp), "BANKNIFTY")
            self.assertEqual(cur["anchor_end"], "2026-11-13")
            self.assertFalse((wd / "state" / "week_cursor.json").exists())     # `continue` keeps its own cursor

            # 4. the same choice again: no download at all
            tr.urls.clear()
            r2 = _fetch(tmp, cli, "BANKNIFTY", 2)
            self.assertEqual(_candle_urls(tr, ("Nifty Bank", "2026-11-13")), [])
            self.assertEqual((r2["skipped"], r2["complete"]), (2, 2))
            # first 2 then 15 weeks: only the 13 missing weeks are downloaded
            tr.urls.clear()
            r3 = _fetch(tmp, cli, "BANKNIFTY", 15)
            self.assertEqual((r3["skipped"], r3["complete"], r3["start"]), (2, 15, "2026-08-01"))
            self.assertEqual(len([w for w in r3["windows"] if not w.get("skipped")]), 13)
            got = _candle_urls(tr, ("Nifty Bank", "2026-11-13"))
            self.assertTrue(got)
            for u in got:                      # nothing after 30 Oct, except a whole expiry period of one contract
                to_s = u.split("/")[-2]
                self.assertTrue(to_s <= "2026-10-30" or "expired-instruments" in u, u)
            self.assertEqual(len(got), len(set(got)))

            # second index: its own calls, India VIX not again, its own folder, its own probe
            st = State(wd / "state")
            vix_before = {(c["from_date"], c["to_date"]) for c in st.chunks() if "India VIX" in c["instrument_key"]}
            st.close()
            tr.urls.clear()
            r4 = _fetch(tmp, cli, "NIFTY", 1)
            self.assertEqual((r4["start"], r4["end"], r4["complete"]), ("2026-11-07", "2026-11-13", 1))
            urls = _urls(tr)
            for word in OTHER["NIFTY"]:
                self.assertFalse([u for u in urls if word in u and "probe" not in u], word)
            self.assertFalse([u for u in _candle_urls(tr) if "India VIX" in u and u.split("/")[-1] != u.split("/")[-2]])
            st = State(wd / "state")
            vix_after = {(c["from_date"], c["to_date"]) for c in st.chunks() if "India VIX" in c["instrument_key"]}
            st.close()
            self.assertEqual(vix_before, vix_after)
            eq = {u.split("NSE_EQ|")[1].split("/")[0] for u in urls if "NSE_EQ|" in u}
            nifty50 = {r["isin"] for r in _rows(REF_DIR / "stocks_nifty50_sensex.csv") if r["group"] == "NIFTY50"}
            self.assertFalse(eq & (STOCKS["BANK"] - STOCKS["HEAVY"] - nifty50))      # PNB, AUBANK ... only for BANK NIFTY
            self.assertTrue(eq & (nifty50 - STOCKS["HEAVY"]))                        # D10: the Nifty 50 list
            self.assertTrue(STOCKS["HEAVY"] - STOCKS["BANK"] <= eq)     # RELIANCE, INFY; HDFC/ICICI came with BANK NIFTY
            self.assertFalse(eq & (STOCKS["HEAVY"] & STOCKS["BANK"]))     # ... and are not downloaded twice
            self.assertEqual(json.loads((wd / "probe_report.json").read_text())["probed_indices"], ["NIFTY"])
            nphone = Path(tmp) / "phone" / "NIFTY"
            self.assertEqual([p.name for p in nphone.iterdir() if p.is_dir()], ["week_01_2026-11-07_to_2026-11-13"])
            self.assertEqual(len([p for p in phone.iterdir() if p.is_dir()]), 15)      # BANKNIFTY untouched
            nweek = nphone / "week_01_2026-11-07_to_2026-11-13"      # the shared reports hold BANKNIFTY rows too
            self.assertTrue({r["underlying"] for r in _rows(wd / "reports" / "day_status.csv")} >= {"BANKNIFTY", "NIFTY"})
            for rep_name in ("day_status.csv", "strategy_ready_days.csv", "health.csv"):
                self.assertFalse({r["underlying"] for r in _rows(nweek / rep_name)} & {"BANKNIFTY", "SENSEX"}, rep_name)
            self.assertNotIn("BANKNIFTY", (nweek / "health_summary.txt").read_text(encoding="utf-8"))
            self.assertFalse([p for p in (nweek / "store").rglob("*") if "underlying=BANKNIFTY" in str(p)])

            # the next week: only the new days are downloaded, the old weeks stay as they are
            tr.urls.clear()
            r5 = _fetch(tmp, cli, "BANKNIFTY", 2, today="2026-11-17")
            self.assertEqual(r5["end"], "2026-11-16")
            self.assertEqual(r5["windows"][0]["start"], "2026-11-14")
            self.assertTrue(r5["windows"][1].get("skipped"))
            for u in _candle_urls(tr, ("Nifty Bank", "2026-11-16")):
                self.assertTrue(u.split("/")[-1] >= "2026-11-14" or "expired-instruments" in u, u)

            # repair: a re-queued day makes its fetch week come back once (only missing chunks are asked)
            self.assertEqual(mark_fetch_repair(_settings(tmp), ["2026-11-04"]), 1)
            tr.urls.clear()
            r6 = _fetch(tmp, cli, "BANKNIFTY", 4)
            done = [w for w in r6["windows"] if not w.get("skipped")]
            self.assertEqual([w["start"] for w in done], ["2026-10-31"])
            self.assertEqual(_candle_urls(tr, ("Nifty Bank", "2026-11-13")), [])

    def test_kill_in_the_middle_then_the_same_command_finishes_without_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = IndexWrap()
            cli = _client(tr)
            _probe(tmp, cli, "BANKNIFTY")
            tr.urls.clear()
            tr.kill_after = tr.n_candle + 12
            r1 = _fetch(tmp, cli, "BANKNIFTY", 2)
            self.assertTrue(r1["stopped"])
            self.assertIn("RUK GAYA", r1["_out"])
            first = _candle_urls(tr, ("Nifty Bank", "2026-11-13"))
            cur = load_fetch_cursor(_settings(tmp), "BANKNIFTY")
            self.assertIn("STARTED", {w["status"] for w in cur["windows"].values()})
            tr.kill_after = None
            tr.urls.clear()
            r2 = _fetch(tmp, cli, "BANKNIFTY", 2)
            self.assertEqual((r2["complete"], r2["stopped"]), (2, False))
            second = _candle_urls(tr, ("Nifty Bank", "2026-11-13"))
            self.assertEqual(len(second), len(set(second)), "a chunk was asked twice after the restart")
            self.assertFalse(set(first) & set(second), "a chunk fetched before the kill was asked again")
            phone = Path(tmp) / "phone" / "BANKNIFTY"
            self.assertEqual(len([p for p in phone.iterdir() if p.is_dir()]), 2)
            self.assertFalse([p for p in phone.iterdir() if p.name.endswith(".tmp")])

    def test_sensex_only_and_a_day_not_served_yet_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = IndexWrap(empty_days={"2026-11-13"})
            cli = _client(tr)
            _probe(tmp, cli, "SENSEX")
            pu = _urls(tr)
            self.assertFalse([u for u in pu if "Nifty 50" in u or "Nifty Bank" in u or "GLOBAL" in u])
            tr.urls.clear()
            r = _fetch(tmp, cli, "SENSEX", 1)
            self.assertEqual((r["end"], r["data_through"], r["start"]), ("2026-11-12", "2026-11-12", "2026-11-06"))
            urls = _urls(tr)
            for word in OTHER["SENSEX"]:
                self.assertFalse([u for u in urls if word in u and "NSE_INDEX|India VIX" not in u], word)
            self.assertTrue(any("BSE_INDEX|SENSEX" in u for u in urls))
            st = State(Path(tmp) / "wd" / "state")
            self.assertFalse([c for c in st.chunks() if c["to_date"] >= "2026-11-13"])     # pending, never empty
            st.close()
            self.assertIn("2026-11-13 ka data Upstox par abhi nahi aaya", r["_out"])
            self.assertTrue((Path(tmp) / "phone" / "SENSEX" / "week_01_2026-11-06_to_2026-11-12").exists())


class TestCli(unittest.TestCase):
    def _main(self, tmp, tr, argv, **kw):
        from updl.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["fetch", "--manifest", str(MANIFEST_PATH), "--workdir", str(Path(tmp) / "wd"),
                       "--today", TODAY, "--phone-dir", str(Path(tmp) / "phone")] + argv,
                      transport=tr, environ={}, sleep=lambda s: None, **kw)
        return rc, buf.getvalue()

    def test_flags_run_without_questions_and_incomplete_flags_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = IndexWrap()

            def no_ask(_p):
                raise AssertionError("asked a question")
            rc, out = self._main(tmp, tr, ["--index", "BANKNIFTY", "--weeks", "1", "--yes"], ask=no_ask,
                                 interactive=False)
            self.assertEqual(rc, 0, out)
            self.assertTrue((Path(tmp) / "phone" / "BANKNIFTY" / "week_01_2026-11-07_to_2026-11-13").exists())
            self.assertFalse((Path(tmp) / "wd" / "state" / "run.lock").exists())
            self.assertNotIn("Traceback", out)
            n = len(tr.urls)
            rc, out = self._main(tmp, tr, ["--index", "BANKNIFTY"], ask=no_ask, interactive=False)
            self.assertEqual(rc, 2)
            self.assertIn("--weeks 15 --yes", out)
            self.assertEqual(len(tr.urls), n)                  # nothing was asked from the network
            asked = []

            def ask(p):
                asked.append(p)
                return {"Hafte: ": "1"}.get(p, "")
            rc, out = self._main(tmp, tr, ["--index", "BANKNIFTY"], ask=ask, interactive=True)
            self.assertEqual(rc, 0, out)
            self.assertEqual(asked, ["Hafte: ", "Enter = shuru, n = ruk jao: "])
            self.assertIn("FETCH BANK NIFTY: POORA (COMPLETE)", out)

            def ctrl_c(_p):
                raise KeyboardInterrupt()
            rc, out = self._main(tmp, tr, [], ask=ctrl_c, interactive=True)
            self.assertEqual(rc, 130)
            self.assertIn("Ruk gaya", out)
            self.assertNotIn("Traceback", out)
            # Ctrl+C after the menu, in the middle of the run: a plain line, exit 130, the lock is gone
            tr.kill_after = tr.n_candle + 3
            rc, out = self._main(tmp, tr, ["--index", "SENSEX", "--weeks", "1", "--yes", "--workers", "1"],
                                 ask=no_ask, interactive=False)
            tr.kill_after = None
            self.assertIn(rc, (0, 130))
            self.assertNotIn("Traceback", out)
            self.assertFalse((Path(tmp) / "wd" / "state" / "run.lock").exists())
            rc, out = self._main(tmp, tr, ["--index", "SENSEX", "--weeks", "1", "--yes"], ask=no_ask,
                                 interactive=False)
            self.assertEqual(rc, 0, out)
            self.assertIn("FETCH SENSEX: POORA (COMPLETE)", out)
            buf = io.StringIO()
            from updl.cli import main
            with contextlib.redirect_stdout(buf):
                rc = main(["fetch", "--index", "NIFTY", "--weeks", "2", "--yes", "--dry-run", "--today", TODAY,
                           "--manifest", str(MANIFEST_PATH), "--workdir", str(Path(tmp) / "dry")], environ={},
                          interactive=False)
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertIn("fetch --dry-run: 2 hafte, kareeb", buf.getvalue())
            self.assertFalse((Path(tmp) / "dry").exists())

    def test_termux_script_defaults_to_fetch_and_keeps_stdin(self):
        script = (Path(__file__).resolve().parent.parent / "termux_run.sh").read_text(encoding="utf-8")
        self.assertIn('ACTION="${1:-fetch}"', script)
        self.assertRegex(script, r"\|history\|fetch\) wake_lock")
        self.assertIn("bash termux_run.sh fetch", script)
        self.assertNotIn("exec \"$PYTHON_BIN\" -m updl", script)
        from updl.cli import LOCKED_COMMANDS
        self.assertIn("fetch", LOCKED_COMMANDS)


class TestRestrictHelper(unittest.TestCase):
    def test_restrict_to_rules_from_the_manifest(self):
        s = load_settings(MANIFEST_PATH)
        self.assertEqual(s.menu_indices(), ["BANKNIFTY", "NIFTY", "SENSEX"])
        self.assertEqual(s.restrict_to("bank nifty"), "BANKNIFTY")
        self.assertEqual((list(s.instruments), s.instrument_names()), (["BANKNIFTY", "INDIAVIX"], ["BANKNIFTY"]))
        self.assertEqual((s.stock_groups, s.wide_stock_groups), (("BANK",), ()))
        self.assertFalse(s.item_selected("D11"))
        self.assertTrue(s.item_selected("D06"))
        s = load_settings(MANIFEST_PATH)
        s.restrict_to("NIFTY", with_globals=True)
        self.assertEqual((s.stock_groups, s.wide_stock_groups), (("HEAVY",), ("NIFTY50",)))
        self.assertTrue(s.item_selected("D11"))
        s = load_settings(MANIFEST_PATH)
        s.restrict_to("SENSEX")
        self.assertEqual((s.stock_groups, s.wide_stock_groups), (("HEAVY",), ("SENSEX",)))
        with self.assertRaises(ValueError):
            load_settings(MANIFEST_PATH).restrict_to("FINNIFTY")
        full = load_settings(MANIFEST_PATH)          # without restrict_to nothing changes
        self.assertEqual((full.stock_groups, full.wide_stock_groups), (("HEAVY", "BANK"), ("NIFTY50", "SENSEX")))
        self.assertTrue(full.item_selected("D11"))
        self.assertEqual(cursor_path(full, "NIFTY").name, "fetch_NIFTY.json")


if __name__ == "__main__":
    unittest.main()
