"""JSON-lines run log. Values are redacted of any token before writing."""
from __future__ import annotations

import json
from pathlib import Path

from .timeutil import utc_iso, utc_stamp


class RunLog:
    def __init__(self, logs_dir=None, secrets=(), run_id=None, echo=False):
        self.run_id = run_id or ("r" + utc_stamp())
        self.secrets = [s for s in secrets if s]
        self.echo = echo
        self.path = None
        if logs_dir is not None:
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
            self.path = Path(logs_dir) / f"{self.run_id}.jsonl"

    def _clean(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text.replace("Bearer", "B***")

    def __call__(self, event, **kw):
        line = json.dumps({"t": utc_iso(), "run": self.run_id, "event": event, **kw}, default=str, ensure_ascii=False)
        line = self._clean(line)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.echo:
            print(line)

    # stdlib-style helpers
    def info(self, msg, **kw):
        self("info", msg=msg, **kw)

    def warn(self, msg, **kw):
        self("warning", msg=msg, **kw)
