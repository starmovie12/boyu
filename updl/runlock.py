"""One run per workdir, and a state sanity check before a run starts.

The owner once started two `continue` processes on the same state, and the phone once switched off in the middle
of a run. `RunLock` refuses a second writer (state/run.lock holds the PID and the start time; a lock whose
process is gone, or that is older than a day, is stale and replaced). `startup_checks` runs SQLite
`integrity_check` on both state databases and removes half-written `.tmp` files.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .errors import AlreadyRunning, CorruptFile
from .messages import ALREADY_RUNNING, STATE_DB_BROKEN
from .store import atomic_write_bytes, cleanup_temp_files

LOCK_NAME = "run.lock"
RECORD_LOCK_NAME = "record.lock"     # the Recorder writes no SQLite state: it has its own lock and may run next to a download
STALE_AFTER_SECONDS = 24 * 3600


def _pid_alive(pid: int):
    """True / False on POSIX; None where it cannot be checked safely (Windows: os.kill would terminate)."""
    if os.name != "posix":
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


class RunLock:
    def __init__(self, state_dir, command: str, clock=time.time, name: str = LOCK_NAME, extra=None):
        self.path = Path(state_dir) / name
        self.command = command
        self.extra = dict(extra or {})
        self.clock = clock
        self._held = False

    def holder(self):
        """The live holder's record, or None when there is no lock or it is stale."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid, started = int(data["pid"]), float(data["started"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self.clock() - started > STALE_AFTER_SECONDS or pid == os.getpid():
            return None
        return None if _pid_alive(pid) is False else data

    def __enter__(self) -> "RunLock":
        held = self.holder()
        if held is not None:
            started = time.strftime("%H:%M", time.localtime(float(held["started"])))
            raise AlreadyRunning(ALREADY_RUNNING.format(command=held.get("command", "?"), pid=held["pid"],
                                                        started=started, path=self.path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {**self.extra, "pid": os.getpid(), "started": self.clock(), "command": self.command}
        atomic_write_bytes(self.path, json.dumps(record).encode("utf-8"), overwrite=True)
        self._held = True
        return self

    def __exit__(self, *exc) -> None:
        if self._held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._held = False


def sqlite_ok(path) -> bool:
    try:
        db = sqlite3.connect(str(path), timeout=30)
        try:
            return db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            db.close()
    except sqlite3.DatabaseError:
        return False


def startup_checks(settings) -> int:
    """Before a writing run: both state databases must pass integrity_check; stale .tmp files are removed.
    Returns the number of .tmp files removed."""
    for name in ("progress.sqlite3", "catalog.sqlite3"):
        p = settings.state_dir / name
        if p.exists() and not sqlite_ok(p):
            raise CorruptFile(STATE_DB_BROKEN.format(path=p))
    return cleanup_temp_files(settings.workdir)
