"""SQLite state. progress.sqlite3 holds exactly two tables: chunk_status and coverage.
catalog.sqlite3 holds the contract catalog, first-seen dates and a small key/value table."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable

from .timeutil import to_date, utc_iso

DONE, EMPTY_OK, FAILED_RETRYABLE, FAILED_PERMANENT, INCOMPLETE = "DONE", "EMPTY_OK", "FAILED_RETRYABLE", \
    "FAILED_PERMANENT", "INCOMPLETE"
COVERING = (DONE, EMPTY_OK)


def cov_key(instrument_key: str, interval: str = "1m") -> str:
    """Coverage key. Daily and 1-minute candles of the same instrument must not cover each other."""
    return instrument_key if interval in (None, "1m") else f"{instrument_key}#{interval}"


class State:
    RAW_DELETED = "RAW_DELETED_MINIMAL"

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "progress.sqlite3", timeout=30)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS chunk_status(
            item_id TEXT, instrument_key TEXT, from_date TEXT, to_date TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, error TEXT, fetched_utc TEXT, raw_path TEXT, sha256 TEXT,
            fetch_run_id TEXT, table_name TEXT, underlying TEXT,
            PRIMARY KEY(item_id, instrument_key, from_date, to_date));
        CREATE TABLE IF NOT EXISTS coverage(
            instrument_key TEXT, trading_date TEXT, item_id TEXT, status TEXT, fetched_utc TEXT, raw_path TEXT,
            PRIMARY KEY(instrument_key, trading_date));
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- chunks --------------------------------------------------------------
    def set_chunk(self, item_id, key, frm, to, status, error=None, raw_path=None, sha256=None, run_id=None,
                  table_name=None, underlying=None):
        self.db.execute("""
            INSERT INTO chunk_status(item_id, instrument_key, from_date, to_date, status, attempts, error,
                                     fetched_utc, raw_path, sha256, fetch_run_id, table_name, underlying)
            VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?)
            ON CONFLICT(item_id, instrument_key, from_date, to_date) DO UPDATE SET
              status=excluded.status, attempts=attempts+1, error=excluded.error, fetched_utc=excluded.fetched_utc,
              raw_path=COALESCE(excluded.raw_path, raw_path), sha256=COALESCE(excluded.sha256, sha256),
              fetch_run_id=COALESCE(excluded.fetch_run_id, fetch_run_id),
              table_name=COALESCE(excluded.table_name, table_name),
              underlying=COALESCE(excluded.underlying, underlying)
        """, (item_id, key, str(to_date(frm)), str(to_date(to)), status, error, utc_iso(), raw_path, sha256,
              run_id, table_name, underlying))
        self.db.commit()

    def chunk_counts(self) -> dict:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM chunk_status GROUP BY status").fetchall())

    def total_attempts(self) -> int:
        row = self.db.execute("SELECT COALESCE(SUM(attempts), 0) FROM chunk_status").fetchone()
        return int(row[0])

    def chunks(self, status=None, item_id=None) -> list:
        q, args = "SELECT item_id, instrument_key, from_date, to_date, status, raw_path, sha256, fetch_run_id, " \
                  "table_name, underlying, fetched_utc, error FROM chunk_status WHERE 1=1", []
        if status:
            st = list(status) if isinstance(status, (list, tuple)) else [status]
            q += " AND status IN (%s)" % ",".join("?" * len(st))
            args += st
        if item_id:
            q += " AND item_id=?"
            args.append(item_id)
        cols = ["item_id", "instrument_key", "from_date", "to_date", "status", "raw_path", "sha256", "fetch_run_id",
                "table_name", "underlying", "fetched_utc", "error"]
        rows = [dict(zip(cols, r)) for r in self.db.execute(q, args)]
        return sorted(rows, key=lambda d: (d["item_id"], d["instrument_key"], d["from_date"]))

    def mark_raw_deleted(self, raw_path):
        self.db.execute("UPDATE chunk_status SET error=? WHERE raw_path=?", (self.RAW_DELETED, raw_path))
        self.db.commit()

    def retryable(self) -> list:
        return self.chunks(status=[FAILED_RETRYABLE, INCOMPLETE])

    # ---- coverage -------------------------------------------------------------
    def mark_coverage(self, key, dates: Iterable, item_id, status, raw_path):
        now = utc_iso()
        self.db.executemany(
            "INSERT OR REPLACE INTO coverage(instrument_key, trading_date, item_id, status, fetched_utc, raw_path) "
            "VALUES(?,?,?,?,?,?)", [(key, str(to_date(d)), item_id, status, now, raw_path) for d in dates])
        self.db.commit()

    def covered_dates(self, key) -> set:
        rows = self.db.execute("SELECT trading_date FROM coverage WHERE instrument_key=?", (key,)).fetchall()
        return {dt.date.fromisoformat(r[0]) for r in rows}

    def coverage_row(self, key, d):
        return self.db.execute("SELECT item_id, status, fetched_utc, raw_path FROM coverage "
                               "WHERE instrument_key=? AND trading_date=?", (key, str(to_date(d)))).fetchone()

    def covered_keys_on(self, d) -> set:
        rows = self.db.execute("SELECT instrument_key FROM coverage WHERE trading_date=?", (str(to_date(d)),))
        return {r[0] for r in rows}

    def drop_coverage(self, key, dates: Iterable):
        self.db.executemany("DELETE FROM coverage WHERE instrument_key=? AND trading_date=?",
                            [(key, str(to_date(d))) for d in dates])
        self.db.commit()

    def keys_with_coverage(self, item_id=None) -> list:
        if item_id:
            rows = self.db.execute("SELECT DISTINCT instrument_key FROM coverage WHERE item_id=?", (item_id,))
        else:
            rows = self.db.execute("SELECT DISTINCT instrument_key FROM coverage")
        return [r[0] for r in rows]

    def dump_text(self) -> str:
        parts = []
        for table in ("chunk_status", "coverage"):
            for row in self.db.execute(f"SELECT * FROM {table}"):
                parts.append(table + "\t" + "\t".join(str(c) for c in row))
        return "\n".join(parts)


class Catalog:
    """Contracts found by D07 / the master, first-seen dates, and a tiny key/value table."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "catalog.sqlite3", timeout=30)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
            underlying TEXT, expiry_date TEXT, kind TEXT, instrument_key TEXT, strike REAL, option_type TEXT,
            trading_symbol TEXT, source TEXT, lot_size INTEGER, freeze_quantity INTEGER, master_tick_size REAL,
            PRIMARY KEY(underlying, instrument_key));
        CREATE TABLE IF NOT EXISTS expiries(underlying TEXT, expiry_date TEXT, source TEXT,
            PRIMARY KEY(underlying, expiry_date));
        CREATE TABLE IF NOT EXISTS first_seen(instrument_key TEXT PRIMARY KEY, first_date TEXT);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def add_expiries(self, underlying, dates, source):
        self.db.executemany("INSERT OR REPLACE INTO expiries VALUES(?,?,?)",
                            [(underlying, str(to_date(d)), source) for d in dates])
        self.db.commit()

    def expiries(self, underlying) -> list:
        rows = self.db.execute("SELECT expiry_date FROM expiries WHERE underlying=?", (underlying,))
        return sorted(dt.date.fromisoformat(r[0]) for r in rows)

    def add_contracts(self, underlying, contracts: list, source: str):
        self.db.executemany("INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            (underlying, str(to_date(c["expiry_date"])), "FUT" if c["option_type"] == "FUT" else "OPT",
             c["instrument_key"], c.get("strike"), c["option_type"], c.get("trading_symbol"), source,
             c.get("lot_size"), c.get("freeze_quantity"), c.get("tick_size")) for c in contracts])
        self.db.commit()

    def contracts(self, underlying, kind=None) -> list:
        q = "SELECT expiry_date, kind, instrument_key, strike, option_type, trading_symbol, source FROM contracts " \
            "WHERE underlying=?"
        args = [underlying]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        cols = ["expiry_date", "kind", "instrument_key", "strike", "option_type", "trading_symbol", "source"]
        out = []
        for r in self.db.execute(q, args):
            d = dict(zip(cols, r))
            d["expiry_date"] = dt.date.fromisoformat(d["expiry_date"])
            out.append(d)
        return out

    def note_first_seen(self, key, d):
        d = str(to_date(d))
        row = self.db.execute("SELECT first_date FROM first_seen WHERE instrument_key=?", (key,)).fetchone()
        if row is None or d < row[0]:
            self.db.execute("INSERT OR REPLACE INTO first_seen VALUES(?,?)", (key, d))
            self.db.commit()

    def first_seen(self) -> dict:
        return {k: dt.date.fromisoformat(v) for k, v in self.db.execute("SELECT instrument_key, first_date FROM first_seen")}

    def set_kv(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
        self.db.commit()

    def get_kv(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default
