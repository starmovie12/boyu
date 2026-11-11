"""Token loading. Tokens are only read from the environment or from a small file OUTSIDE the project.
They are never printed, logged, written, or placed in exceptions. This module never creates or refreshes a token."""
from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENV_ANALYTICS = "UPSTOX_ANALYTICS_TOKEN"
ENV_ACCESS = "UPSTOX_ACCESS_TOKEN"
ENV_FILE = "UPSTOX_TOKEN_FILE"

log = logging.getLogger("updl.tokens")


@dataclass(frozen=True)
class Tokens:
    analytics: Optional[str] = None
    access: Optional[str] = None

    def __repr__(self):  # never show values
        return f"Tokens(analytics={'set' if self.analytics else 'missing'}, access={'set' if self.access else 'missing'})"

    __str__ = __repr__

    def has_any(self) -> bool:
        return bool(self.analytics or self.access)

    def secret_values(self) -> list:
        return [t for t in (self.analytics, self.access) if t]

    def for_kind(self, kind: str, analytics_ok_for_expired: bool = False) -> Optional[str]:
        """kind: 'v3' (candles, masters, option chain) or 'expired'.

        PROMPT_02 rule 2: expired-instrument calls use the access token, or the analytics token only when the
        probe proved it works (`analytics_ok_for_expired`). Upstox staff said in 2026 that a Plus user's
        analytics token works there; the probe tests that itself (P09 when only the analytics token is set,
        P16 when both are set) instead of assuming it."""
        if kind == "expired":
            if self.access:
                return self.access
            return self.analytics if analytics_ok_for_expired else None
        return self.analytics or self.access


def _file_permission_warning(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("Token file group/others ke liye padhne layak hai. `chmod 600` chalao.")
        print("WARNING: token file ko dusre log bhi padh sakte hain. `chmod 600` chalao.")


def load_tokens(environ=None) -> Tokens:
    env = os.environ if environ is None else environ
    analytics = (env.get(ENV_ANALYTICS) or "").strip() or None
    access = (env.get(ENV_ACCESS) or "").strip() or None
    file_path = (env.get(ENV_FILE) or "").strip()
    if file_path and (analytics is None or access is None):
        p = Path(file_path)
        if p.is_file():
            _file_permission_warning(p)
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name, value = name.strip().lower(), value.strip()
                if name == "analytics" and analytics is None and value:
                    analytics = value
                elif name == "access" and access is None and value:
                    access = value
    return Tokens(analytics=analytics, access=access)
