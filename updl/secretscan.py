"""Secret scan over every file the program writes (also used to gate `download` on probe_report.json)."""
from __future__ import annotations

import gzip
import re
from pathlib import Path

LONG_RUN = re.compile(r"[A-Za-z0-9._-]{40,}")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
BEARER = "Bearer"


def scan_text(text: str, secrets=()) -> list:
    hits = []
    if BEARER in text:
        hits.append("contains the text 'Bearer'")
    for s in secrets:
        if s and s in text:
            hits.append("contains a token value")
    for m in LONG_RUN.finditer(text):
        run = m.group(0)
        if HEX64.match(run):
            continue
        hits.append(f"long string of {len(run)} characters")
    return hits


def read_for_scan(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            pass
    return data.decode("latin-1")


def scan_file(path, secrets=()) -> list:
    path = Path(path)
    return [f"{path}: {h}" for h in scan_text(read_for_scan(path), secrets)]


def scan_tree(root, secrets=(), skip_suffixes=()) -> list:
    hits = []
    root = Path(root)
    if not root.exists():
        return hits
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix not in skip_suffixes:
            hits.extend(scan_file(p, secrets))
    return hits
