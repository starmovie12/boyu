"""v4.2 tests: per-index export layout (C1), coverage and strategy-ready days (C2). Mocked HTTP only."""
from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from updl.config import load_settings
from updl.coverage import run_coverage
from updl.layout import MAX_CSV_BYTES, SplitCsv, write_layout
from updl.sessions import load_calendar
from updl.store import list_parts, part_path, read_parquet, write_parquet

from tests.test_updl import MANIFEST_PATH, REF_DIR
from tests.test_v3_fixes import _opt_frame
from tests.test_v4_week import Wrap, _setup, _week


def _csv_rows(paths):
    n = 0
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            n += sum(1 for _ in fh) - 1
    return n


class TestLayout(unittest.TestCase):
    def test_week_folder_has_per_index_files_whose_rows_equal_the_candles(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap(), items="D12,D01,D03,D07,D05,D06,D08")
            res = _week(settings, cli, tmp)
            phone = Path(res["phone_path"])
            nifty = phone / "NIFTY"
            for name in ("spot_1m.csv", "contracts.csv", "futures_1m.csv"):
                self.assertTrue((nifty / name).exists(), name)
            for name in ("README_FOR_AI.md", "README_EXPORT.txt", "raw_index.csv", "strategy_ready_days.csv",
                         "day_status.csv"):
                self.assertTrue((phone / name).exists(), name)
            self.assertIn("Upstox par TICK data nahi hai", (phone / "README_EXPORT.txt").read_text(encoding="utf-8"))
            # one row = one minute of one contract, and nothing is lost or doubled on the way
            opt_parts = [p for t in ("opt_1m", "opt_wide_1m") for part in ("dev", "holdout")
                         for p in list_parts(settings.store_dir, part, t, "NIFTY")
                         if dt.date(2026, 11, 7) <= dt.date.fromisoformat(p.parent.name[13:]) <= dt.date(2026, 11, 13)]
            candles = pd.concat([read_parquet(p) for p in opt_parts]).drop_duplicates(["instrument_key", "ts_open"])
            csvs = sorted((nifty / "options_1m").glob("*.csv"))
            self.assertEqual(_csv_rows(csvs), len(candles))
            self.assertEqual({p.stem for p in csvs}, {str(x) for x in candles["expiry_date"].unique()})
            head = (csvs[0].read_text(encoding="utf-8").splitlines()[:2])
            self.assertEqual(head[0], "time,strike,option_type,open,high,low,close,volume,oi,instrument_key")
            self.assertRegex(head[1], r"^2026-11-\d\dT\d\d:\d\d:00\+05:30,")
            contracts = list(csv.DictReader(open(nifty / "contracts.csv", encoding="utf-8")))
            self.assertEqual(len(contracts), candles["instrument_key"].nunique())
            self.assertEqual(sum(int(c["minutes_with_data"]) for c in contracts), len(candles))
            spot_parts = list_parts(settings.store_dir, "holdout", "spot_1m", "NIFTY")
            spot_n = sum(len(read_parquet(p)) for p in spot_parts
                         if "2026-11-07" <= p.parent.name[13:] <= "2026-11-13")
            self.assertEqual(_csv_rows([nifty / "spot_1m.csv"]), spot_n)
            # the whole window is holdout (>= 2026-10-05): the AI file goes to the separate holdout folder only
            self.assertFalse((phone / "ai_ready" / "NIFTY_options_1m.parquet").exists())
            ai = read_parquet(phone / "ai_ready" / "holdout" / "NIFTY_options_1m.parquet")
            self.assertEqual(len(ai), len(candles))
            for p in phone.rglob("*"):
                self.assertLess(len(p.name), 40, p.name)
                if p.suffix == ".csv":
                    self.assertLess(p.stat().st_size, MAX_CSV_BYTES)
            raw = list(csv.DictReader(open(phone / "raw_index.csv", encoding="utf-8")))
            self.assertTrue(all(r["raw_folder"].startswith("raw/D") for r in raw))
            self.assertIn("NSE_FO|90002", {r["instrument_key"] for r in raw})

    def test_dev_and_holdout_split_null_stays_empty_and_big_files_are_split(self):
        cal = load_calendar(REF_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(MANIFEST_PATH, workdir=tmp, holdout_start="2026-05-12")
            s.instruments = {k: v for k, v in s.instruments.items() if k in ("NIFTY", "INDIAVIX")}
            for d, part in ((dt.date(2026, 5, 11), "dev"), (dt.date(2026, 5, 12), "holdout")):
                df = _opt_frame([("K1", 25000.0, "CE", "09:20", 120.0, 5, 1000),
                                 ("K2", 25000.0, "PE", "09:21", 110.0, None, None)], cal)
                df["ts_open"] = df["ts_open"].apply(lambda t: t.replace(day=d.day))
                df["trading_date"] = d
                write_parquet(df, part_path(s.store_dir, part, "opt_1m", "NIFTY", d))
            out = Path(tmp) / "out"
            res = write_layout(s, "2026-05-11", "2026-05-12", out)
            self.assertEqual(res["NIFTY"]["option_rows"], 4)
            dev = read_parquet(out / "ai_ready" / "NIFTY_options_1m.parquet")
            hold = read_parquet(out / "ai_ready" / "holdout" / "NIFTY_options_1m.parquet")
            self.assertEqual((set(dev["trading_date"].astype(str)), set(hold["trading_date"].astype(str))),
                             ({"2026-05-11"}, {"2026-05-12"}))
            self.assertTrue(pd.isna(dev.loc[dev["option_type"] == "PE", "volume"]).all())
            line = [x for x in (out / "NIFTY" / "options_1m" / "2026-05-12.csv").read_text().splitlines() if ",PE," in x][0]
            self.assertTrue(line.endswith(",110.0,110.0,110.0,110.0,,,K2"), line)      # empty, never 0
            from updl.export import run_export
            ex = run_export(s, cal, start="2026-05-11", end="2026-05-12", layout=True, phone_dir=str(Path(tmp) / "ph"))
            import zipfile
            names = zipfile.ZipFile(ex["zip_path"]).namelist()
            for want in ("NIFTY/options_1m/2026-05-12.csv", "ai_ready/NIFTY_options_1m.parquet", "README_FOR_AI.md",
                         "strategy_ready_days.csv"):
                self.assertTrue(any(n.endswith(want) for n in names), want)
            self.assertTrue(Path(ex["phone_path"]).exists())
        with tempfile.TemporaryDirectory() as tmp:
            w = SplitCsv(Path(tmp), "2026-11-17", ["a", "b"], max_bytes=60)
            for i in range(4):
                w.append(pd.DataFrame({"a": [i] * 5, "b": ["x" * 3] * 5}), dt.date(2026, 11, 10 + i))
            self.assertEqual([p.name for p in w.files], ["2026-11-17.csv", "2026-11-17_from_2026-11-11.csv",
                                                         "2026-11-17_from_2026-11-12.csv", "2026-11-17_from_2026-11-13.csv"])
            for p in w.files:
                self.assertLessEqual(p.stat().st_size, 60)
                self.assertEqual(p.read_text().splitlines()[0], "a,b")


class TestCoverage(unittest.TestCase):
    def test_strategy_ready_days_and_owner_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, cli = _setup(tmp, Wrap(), items="D12,D01,D03,D07,D05,D06,D08")
            _week(settings, cli, tmp)
            rec = settings.workdir / "recorder" / "NIFTY"
            rec.mkdir(parents=True)
            (rec / "2026-11-12.csv").write_text("local_ts,bid,ask\n", encoding="utf-8")
            lines = []
            res = run_coverage(settings, load_calendar(REF_DIR), start="2026-11-09", end="2026-11-13", say=lines.append)
            days = {r["trading_date"]: r for r in csv.DictReader(open(settings.reports_dir / "strategy_ready_days.csv",
                                                                       encoding="utf-8"))}
            self.assertEqual(sorted(days), ["2026-11-09", "2026-11-11", "2026-11-12", "2026-11-13"])
            d12 = days["2026-11-12"]
            self.assertEqual((d12["SPOT"], d12["FUT"], d12["OPT_BAND"], d12["PRICE_READY"]),
                             ("READY", "READY", "READY", "READY"))
            self.assertEqual((d12["SPREAD"], d12["EVENT"], d12["FULL_READY"]), ("READY", "MISSING", "PARTIAL"))
            self.assertEqual(days["2026-11-11"]["SPREAD"], "MISSING")
            self.assertEqual(res["days"]["counts"]["price"], {"READY": 4})
            self.assertTrue(any("price-data READY 4" in x for x in lines))
            sets = {r["item"]: r for r in csv.DictReader(open(settings.reports_dir / "data_coverage.csv", encoding="utf-8"))}
            self.assertEqual(sets["D06"]["status"], "DELIVERED")
            self.assertEqual(sets["D18"]["status"], "OWNER-FILE-MISSING")
            self.assertEqual(sets["REC"]["first_day"], "2026-11-12")


if __name__ == "__main__":
    unittest.main()
