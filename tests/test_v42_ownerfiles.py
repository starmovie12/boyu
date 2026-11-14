"""v4.2 tests: owner-file validators (one good and one bad file each) and the ingest gate."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from updl.config import load_settings
from updl.ingest import run_ingest
from updl.sessions import load_calendar

from tests.test_updl import MANIFEST_PATH, REF_DIR

GOOD = {
    "events.csv": "event_id,event_ts_ist,name,class,source_url,importance,published_ts\n"
                  "E1,2026-12-05T10:00:00+05:30,RBI policy,RBI,https://rbi.org.in/x,HIGH,2026-10-01T09:00:00+05:30\n"
                  "E2,2026-12-10T19:00:00+05:30,US CPI,CPI,https://bls.gov/x,HIGH,\n",
    "constituents.csv": "index,symbol,isin,weight,valid_from,valid_to\n"
                        "NIFTY50,HDFCBANK,INE040A01034,12.5,2026-01-01,\n",
    "corporate_actions.csv": "symbol,ex_date,type,ratio,source_url,isin,amount\n"
                             "HDFCBANK,2025-08-26,BONUS,1:1,https://nseindia.com/x,INE040A01034,\n",
    "participant_oi.csv": "date,client_type,long,short\n2026-11-12,FII,100,50\n2026-11-12,Client,30,10\n",
}
BAD = {
    "events.csv": ("event_id,event_ts_ist,name,class,source_url\n"
                   "E1,2026-12-10 19:00,US CPI,CPI,bls.gov\nE2,2026-12-05T10:00:00+05:30,RBI,RBIX,https://x\n",
                   ["row 2: event_ts_ist", "row 2: source_url", "row 3: class"]),
    "constituents.csv": ("index,symbol,isin,weight,valid_from,valid_to\nNIFTY50,X,INE04,140,2026-02-01,2026-01-01\n",
                         ["row 2: isin", "row 2: weight", "row 2: valid_to is before"]),
    "corporate_actions.csv": ("symbol,ex_date,type,ratio,source_url\nX,26-08-2025,BONUS,one for one,https://x\n",
                              ["row 2: ex_date", "row 2: ratio"]),
    "participant_oi.csv": ("date,client_type,long,short\n2026-11-12,Retail,-5,abc\n",
                           ["row 2: client_type", "row 2: long is negative", "row 2: short is not a number"]),
}


class TestOwnerFiles(unittest.TestCase):
    def _run(self, tmp, name, text):
        s = load_settings(MANIFEST_PATH, workdir=tmp, external_dir=str(Path(tmp) / "ext"))
        src = Path(tmp) / "in"
        src.mkdir(exist_ok=True)
        (src / name).write_text(text, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = run_ingest(s, str(src / name), calendar=load_calendar(REF_DIR))
        return s, res, buf.getvalue()

    def test_good_files_are_copied(self):
        for name, text in GOOD.items():
            with tempfile.TemporaryDirectory() as tmp:
                s, res, _out = self._run(tmp, name, text)
                self.assertEqual(res["status"], "OK", (name, res))
                self.assertTrue((s.external_dir / name).exists())
        with tempfile.TemporaryDirectory() as tmp:
            s, _res, _out = self._run(tmp, "participant_oi.csv", GOOD["participant_oi.csv"])
            self.assertIn("usable_from_date", (s.external_dir / "participant_oi.csv").read_text())
            self.assertIn("2026-11-13", (s.external_dir / "participant_oi.csv").read_text())

    def test_bad_files_are_refused_with_row_numbers(self):
        for name, (text, expected) in BAD.items():
            with tempfile.TemporaryDirectory() as tmp:
                s, res, out = self._run(tmp, name, text)
                self.assertEqual(res["status"], "INVALID", name)
                self.assertFalse((s.external_dir / name).exists(), name)
                for e in expected:
                    self.assertTrue(any(x.startswith(e) for x in res["errors"]), (name, e, res["errors"]))
                self.assertIn("copy NAHI ki", out)

    def test_templates_match_the_schemas(self):
        from updl.ownerfiles import SCHEMAS
        for name, schema in SCHEMAS.items():
            header = (Path(REF_DIR) / "owner_templates" / name).read_text(encoding="utf-8").strip().split(",")
            self.assertEqual(header, schema["required"] + schema["optional"], name)


if __name__ == "__main__":
    unittest.main()
