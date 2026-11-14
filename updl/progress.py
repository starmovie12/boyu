"""Live progress line (`[1/5 Download] [####----] 57% ...`). Standard library only, ASCII only.

* On a terminal one line is redrawn in place at least once per second (a small ticker thread), so a long
  rate-limit wait never looks frozen. When the output is piped, a plain line is printed every 25 calls or 15 s.
* The ETA is honest: it models the rolling 30-minute cap and uses the real calls/min of the last minute.
* Only item ids, index names, dates and counters are shown. Nothing from a request or a header is printed.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Optional

from .client import estimate_minutes

STAGES = ("Download", "Compact", "Validate", "Derive", "Export+Copy")
SPINNER = "|/-\\"
PLAIN_EVERY_CALLS = 25
PLAIN_EVERY_SECONDS = 15.0
BAR_WIDTH = 14


def mmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class _StdoutProxy:
    """While the live line is on a terminal, ordinary print() calls first wipe that line, so messages from
    other modules never get glued to it. The ticker redraws the line within a second."""

    def __init__(self, progress, real):
        self._progress, self._real = progress, real

    def write(self, text):
        with self._progress._lock:
            self._progress._clear()
            return self._real.write(text)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Progress:
    def __init__(self, stream=None, limiter=None, clock=time.monotonic, sleeper=time.sleep):
        self.stream = stream if stream is not None else sys.stdout
        self.limiter = limiter
        self.clock = clock
        self._sleeper = sleeper
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stage_no, self.stage_name = 0, ""
        self.done = self.total = 0
        self.label = ""
        self._stage_start = self._run_start = clock()
        self._recent = deque()
        self._last_len = 0
        self._last_plain_t = clock()
        self._last_plain_n = 0
        self._spin = 0
        self.stage_seconds = {}
        self._saved_stdout = None

    # ---- state -------------------------------------------------------------------------------------------
    def stage(self, number: int, name: Optional[str] = None) -> None:
        with self._lock:
            now = self.clock()
            if self.stage_name:
                self.stage_seconds[self.stage_name] = self.stage_seconds.get(self.stage_name, 0.0) + now - self._stage_start
            self.stage_no = number
            self.stage_name = name or STAGES[number - 1]
            self.done = self.total = 0
            self.label = ""
            self._stage_start = now
            self._recent.clear()
            self._last_plain_n = 0
            self._draw(force_plain=True)

    def finish(self) -> None:
        with self._lock:
            if self.stage_name:
                self.stage_seconds[self.stage_name] = self.stage_seconds.get(self.stage_name, 0.0) + self.clock() - self._stage_start
                self.stage_name = ""
            self._clear()

    def add_total(self, n: int) -> None:
        with self._lock:
            self.total += int(n)

    def advance(self, label: str = "", n: int = 1) -> None:
        with self._lock:
            now = self.clock()
            self.done += n
            if label:
                self.label = label
            for _ in range(n):
                self._recent.append(now)
            while self._recent and now - self._recent[0] > 60.0:
                self._recent.popleft()
            self._draw()

    def tick(self, label: str = "") -> None:
        """One unit of work in a stage without a known total (a file, a day)."""
        self.advance(label)

    # ---- numbers -----------------------------------------------------------------------------------------
    def rate_per_min(self) -> float:
        with self._lock:
            now = self.clock()
            while self._recent and now - self._recent[0] > 60.0:
                self._recent.popleft()
            span = min(60.0, max(1.0, now - self._stage_start))
            return len(self._recent) * 60.0 / span

    def eta_seconds(self) -> Optional[float]:
        left = self.total - self.done
        if self.total <= 0 or left <= 0:
            return 0.0 if self.total > 0 else None
        rate = self.rate_per_min()
        if self.limiter is not None:
            lim = self.limiter
            if lim.waiting == "RATE-CAP":       # the wait itself, then the rest at the configured rate
                return max(0.0, lim.wait_until - lim.clock()) + left / max(lim.calls_per_min, 1.0) * 60.0
            rate = min(rate, lim.calls_per_min) if rate > 0 else lim.calls_per_min
            return 60.0 * estimate_minutes(left, max(rate, 1.0), lim.max_per_30min, lim.window_used())
        return left / rate * 60.0 if rate > 0 else None

    def status(self) -> str:
        lim = self.limiter
        if lim is not None and lim.waiting:
            left = max(0.0, lim.wait_until - lim.clock())
            if lim.waiting == "RATE-CAP" and left >= 1.0:
                return f"WAITING-RATE-CAP (resume in {mmss(left)})"
            if lim.waiting == "PAUSE-429" and left >= 1.0:
                return f"RETRY-429 ({int(left)}s)"
        return "RUNNING"

    def render(self) -> str:
        elapsed = mmss(self.clock() - self._stage_start)
        head = f"[{self.stage_no}/{len(STAGES)} {self.stage_name}]"
        if self.total > 0:
            frac = min(1.0, self.done / self.total)
            bar = "#" * int(frac * BAR_WIDTH)
            eta = self.eta_seconds()
            parts = [f"{head} [{bar:-<{BAR_WIDTH}}] {int(frac * 100):3d}% {self.done}/{self.total} calls", self.label,
                     f"{self.rate_per_min():.0f}/min", f"elapsed {elapsed}",
                     f"ETA {mmss(eta) if eta is not None else '--:--'}", self.status()]
        else:
            self._spin = (self._spin + 1) % len(SPINNER)
            parts = [f"{head} {SPINNER[self._spin]} {self.done} done", self.label, f"elapsed {elapsed}", self.status()]
        return " | ".join(p for p in parts if p)

    # ---- output ------------------------------------------------------------------------------------------
    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _clear(self) -> None:
        if self.tty and self._last_len:
            self._write("\r" + " " * self._last_len + "\r")
            self._last_len = 0

    def _draw(self, force_plain: bool = False) -> None:
        if not self.stage_name:
            return
        with self._lock:
            if self.tty:
                line = self.render()
                self._write("\r" + line + " " * max(0, self._last_len - len(line)))
                self._last_len = len(line)
                return
            now = self.clock()
            if force_plain or self.done - self._last_plain_n >= PLAIN_EVERY_CALLS \
                    or now - self._last_plain_t >= PLAIN_EVERY_SECONDS:
                self._last_plain_t, self._last_plain_n = now, self.done
                self._write(self.render() + "\n")

    def note(self, text: str) -> None:
        """Print a normal line without breaking the live line."""
        with self._lock:
            self._clear()
            self._write(str(text) + "\n")
            if self.tty:
                self._draw()

    def box(self, lines: list) -> None:
        width = max(len(x) for x in lines) if lines else 0
        self.note("+" + "-" * (width + 2) + "+")
        for x in lines:
            self.note(f"| {x:<{width}} |")
        self.note("+" + "-" * (width + 2) + "+")

    # ---- never sleep silently ----------------------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        """Drop-in for time.sleep: 1-second slices, the ticker keeps redrawing meanwhile."""
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            self._sleeper(min(1.0, left))

    def start(self) -> "Progress":
        if self._thread is None:
            if self.tty and self.stream is sys.stdout:
                self._saved_stdout = sys.stdout
                sys.stdout = _StdoutProxy(self, self.stream)
            self._stop.clear()
            self._thread = threading.Thread(target=self._ticker, name="updl-progress", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.finish()
        if self._saved_stdout is not None:
            sys.stdout = self._saved_stdout
            self._saved_stdout = None

    def _ticker(self) -> None:
        while not self._stop.wait(1.0):
            self._draw()
