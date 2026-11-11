"""Stage 4 Partitioned Store Loader (`updl.load`).

Enforces Section 10 and T15:
* `load_dev(...)` reads only from the `dev` partition and refuses any holdout date/path.
* `load_holdout(...)` refuses unless `OPEN_HOLDOUT_ONCE=1` is set in the environment.
* Opening the holdout appends a hash-chained JSON line (prev_hash, time, caller, file_list) to `holdout_log.jsonl`.
* A second attempt to open the holdout raises `HoldoutAlreadyOpened` (exit code 70).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from .errors import HoldoutAlreadyOpened, HoldoutRefused
from .messages import HOLDOUT_REFUSED, HOLDOUT_USED
from .store import list_parts, read_table
from .timeutil import to_date, utc_iso


def _resolve_store_dir(workdir_or_store: Path) -> Path:
    p = Path(workdir_or_store)
    if (p / "dev").exists() or (p / "holdout").exists():
        return p
    if (p / "handoff" / "store").exists():
        return p / "handoff" / "store"
    if (p / "store").exists():
        return p / "store"
    return p


def load_dev(workdir_or_store, table: str, underlying: Optional[str] = None,
             start=None, end=None, holdout_start=None) -> pd.DataFrame:
    """Load data from the `dev` tree only. Refuses if a holdout path or holdout date range is requested."""
    p = Path(workdir_or_store)
    if "holdout" in p.parts:
        print(HOLDOUT_REFUSED)
        raise HoldoutRefused(HOLDOUT_REFUSED)
    if holdout_start and end and to_date(end) >= to_date(holdout_start):
        print(HOLDOUT_REFUSED)
        raise HoldoutRefused(HOLDOUT_REFUSED)
    store_dir = _resolve_store_dir(p)
    return read_table(store_dir, table, underlying=underlying, start=start, end=end, partitions=("dev",))


def load_holdout(workdir_or_store, table: str, underlying: Optional[str] = None,
                 start=None, end=None, caller: str = "updl.load", environ=None) -> pd.DataFrame:
    """Open the sealed holdout tree at most ONCE when OPEN_HOLDOUT_ONCE=1 is set."""
    env = os.environ if environ is None else environ
    if str(env.get("OPEN_HOLDOUT_ONCE", "")).strip() != "1":
        print(HOLDOUT_REFUSED)
        raise HoldoutRefused(HOLDOUT_REFUSED)

    store_dir = _resolve_store_dir(Path(workdir_or_store))
    log_path = store_dir.parent / "holdout_log.jsonl"
    if log_path.exists() and log_path.read_text(encoding="utf-8").strip():
        print(HOLDOUT_USED)
        raise HoldoutAlreadyOpened(HOLDOUT_USED)

    files = [str(f.relative_to(store_dir)) for f in list_parts(store_dir, "holdout")]
    prev_hash = "0" * 64
    payload = {
        "prev_hash": prev_hash,
        "time": utc_iso(),
        "caller": caller,
        "file_list": files,
    }
    entry_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    payload["hash"] = entry_hash
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")

    return read_table(store_dir, table, underlying=underlying, start=start, end=end, partitions=("holdout",))
