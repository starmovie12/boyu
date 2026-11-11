"""External Owner-Supplied File Ingestion (`python -m updl ingest <file>`).

Handles Section 7.4:
* events.csv (event_id, event_ts_ist, name, class, source_url)
* constituents.csv (index, symbol, isin, weight, valid_from, valid_to)
* participant_oi.csv (date, client_type, long, short -> adds usable_from_date = next trading day)
* bhavcopy/ (maps published UDiFF column names via reference/bhavcopy_columns.yaml; prints unmapped columns instead of guessing)
* stocks.csv (symbol, isin, group, fo_flag)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Settings, load_yaml
from .sessions import Calendar, load_calendar
from .store import atomic_write_bytes
from .timeutil import iso, to_date


def run_ingest(settings: Settings, target_path: str, calendar: Optional[Calendar] = None) -> dict:
    calendar = calendar or load_calendar(settings.reference_dir)
    p = Path(target_path)
    if not p.exists():
        print(f"NA-DATA: {p} does not exist.")
        return {"status": "NA-DATA", "path": str(p)}

    settings.external_dir.mkdir(parents=True, exist_ok=True)
    name = p.name.lower()

    if name == "participant_oi.csv":
        df = pd.read_csv(p)
        for req in ("date", "client_type", "long", "short"):
            if req not in df.columns:
                raise ValueError(f"participant_oi.csv missing required column: {req}")
        df["usable_from_date"] = [iso(calendar.shift(to_date(d), 1)) for d in df["date"]]
        dest = settings.external_dir / "participant_oi.csv"
        df.to_csv(dest, index=False)
        print(f"Ingested participant_oi.csv ({len(df)} rows) with usable_from_date = next trading day.")
        return {"status": "OK", "rows": len(df), "dest": str(dest)}

    if name == "events.csv":
        df = pd.read_csv(p)
        for req in ("event_id", "event_ts_ist", "name", "class", "source_url"):
            if req not in df.columns:
                raise ValueError(f"events.csv missing required column: {req}")
        dest = settings.external_dir / "events.csv"
        df.to_csv(dest, index=False)
        print(f"Ingested events.csv ({len(df)} rows).")
        return {"status": "OK", "rows": len(df), "dest": str(dest)}

    if name == "constituents.csv":
        df = pd.read_csv(p)
        for req in ("index", "symbol", "isin", "weight", "valid_from", "valid_to"):
            if req not in df.columns:
                raise ValueError(f"constituents.csv missing required column: {req}")
        dest = settings.external_dir / "constituents.csv"
        df.to_csv(dest, index=False)
        print(f"Ingested constituents.csv ({len(df)} rows).")
        return {"status": "OK", "rows": len(df), "dest": str(dest)}

    if name == "stocks.csv":
        df = pd.read_csv(p)
        dest = settings.external_dir / "stocks.csv"
        df.to_csv(dest, index=False)
        print(f"Ingested stocks.csv ({len(df)} rows).")
        return {"status": "OK", "rows": len(df), "dest": str(dest)}

    # Otherwise treat as a bhavcopy CSV or folder of bhavcopy CSVs
    col_map = load_yaml(settings.reference_dir / "bhavcopy_columns.yaml").get("columns", {})
    bhav_dest_dir = settings.external_dir / "bhavcopy"
    bhav_dest_dir.mkdir(parents=True, exist_ok=True)
    files = list(p.glob("*.csv")) if p.is_dir() else [p]
    unmapped_all = []
    ingested = 0
    for f in files:
        df = pd.read_csv(f)
        unmapped = [want for want, pub in col_map.items() if pub not in df.columns]
        if unmapped:
            print(f"UNMAPPED BHAVCOPY COLUMNS in {f.name}: {unmapped}")
            unmapped_all.extend(unmapped)
        dest = bhav_dest_dir / f.name
        if f.resolve() != dest.resolve():
            atomic_write_bytes(dest, f.read_bytes(), overwrite=True)
        ingested += 1
    return {"status": "UNMAPPED" if unmapped_all else "OK", "files": ingested, "unmapped": unmapped_all}
