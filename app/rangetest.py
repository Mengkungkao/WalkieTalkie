"""The range test: probe one paired radio on a timer, and log every answer.

Home > Range test, on the radio being carried away. Every `interval`
seconds it sends a probe -- a ping asking for an answer -- to the other
radio, which answers on its own (its app only has to be running). Each
probe is one row in a CSV file: answered or not, the round trip, and
the signal both ways -- how strongly this radio heard the answer, and
how strongly the other radio heard the probe. One click adds a row
marking where you are ("mark 3"), so a walk can be matched to places
afterwards; voice messages sent and received during the test are
logged too, with any fragments they lost.

The files are in ~/.whisplay-walkie/rangetest/, one per test, and
`tools/range_report.py` summarises them.

The probes spend the duty cycle like anything else: at the default 1%,
one every 30 s uses about half the hour on each radio (the answers are
the other radio's half), which leaves room for a few voice messages.
"""

from __future__ import annotations

import csv
import time
from collections import deque
from pathlib import Path

from app.radio.linkcheck import PROBE_TIMEOUT
from app.utils.logger import get_logger

log = get_logger("rangetest")

DIR_NAME = "rangetest"
COLUMNS = ("time", "elapsed_s", "event", "seq", "peer", "answered", "rtt_ms",
           "rssi_down_dbm", "rssi_up_dbm", "pings_heard", "mark", "air_speed",
           "duty_used_pct", "detail")
# The success rate on screen is over this many recent probes: long enough
# to steady, short enough to move as you walk.
WINDOW = 10


class RangeTest:
    def __init__(self, peer: int, name: str, data_dir, interval: float,
                 air_speed: int, now: float | None = None):
        self.peer, self.name = peer, name
        self.interval = max(2.0, float(interval))
        self.air_speed = air_speed
        self.started = time.monotonic() if now is None else now
        self.next_due = self.started
        self.seq = 0
        self.sent = self.answered = self.skipped = 0
        self.marks = 0
        self.recent = deque(maxlen=WINDOW)       # True/False per probe
        self.down = self.up = self.rtt = None
        self.heard_by_them = None
        self.last_result = "starting"
        self.outstanding = {}                    # seq -> sent at
        self.path = None
        self._file = self._writer = None
        self.duty_used = 0.0
        self._open(Path(data_dir) / DIR_NAME)
        self._row("start", detail=f"{name} ({peer}), a probe every {self.interval:.0f}s")

    # --- the log ------------------------------------------------------------
    def _open(self, folder: Path):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / time.strftime("range-%Y%m%d-%H%M%S.csv")
            self._file = open(self.path, "w", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(COLUMNS)
            self._file.flush()
            log.info("range test log: %s", self.path)
        except OSError:
            log.warning("cannot write a range test log", exc_info=True)
            self._file = self._writer = None

    def _row(self, event: str, now: float | None = None, **fields):
        if self._writer is None:
            return
        now = time.monotonic() if now is None else now
        values = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "elapsed_s": f"{now - self.started:.1f}", "event": event,
                  "peer": self.peer, "air_speed": self.air_speed,
                  "duty_used_pct": f"{self.duty_used * 100:.0f}"}
        values.update({k: ("" if v is None else v) for k, v in fields.items()})
        try:
            # Written through at once: the radio may be switched off, or
            # its battery die, at the far end of the walk.
            self._writer.writerow([values.get(column, "") for column in COLUMNS])
            self._file.flush()
        except (OSError, ValueError):
            log.warning("range test log write failed", exc_info=True)

    def close(self, now: float | None = None):
        self.expire(now, force=True)
        self._row("stop", now, detail=f"{self.answered}/{self.sent} answered")
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = self._writer = None

    # --- probing ------------------------------------------------------------
    def due(self, now: float) -> bool:
        return now >= self.next_due

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) % 256
        return self.seq

    def probe_sent(self, seq: int, now: float):
        self.sent += 1
        self.outstanding[seq] = now
        self.next_due = now + self.interval

    def probe_skipped(self, reason: str, now: float):
        """Not sent -- the duty cycle, usually. Tried again next interval."""
        self.skipped += 1
        self.next_due = now + self.interval
        self.last_result = f"skipped: {reason}"
        self._row("skipped", now, detail=reason)

    def answer(self, seq: int, rssi_down, rssi_up, heard: int, now: float) -> bool:
        """A pong. False if it was not for an outstanding probe."""
        sent_at = self.outstanding.pop(seq, None)
        if sent_at is None:
            return False
        self.answered += 1
        self.recent.append(True)
        self.rtt = now - sent_at
        self.down, self.up, self.heard_by_them = rssi_down, rssi_up, heard
        self.last_result = f"answered in {self.rtt * 1000:.0f} ms"
        self._row("probe", now, seq=seq, answered=1, rtt_ms=f"{self.rtt * 1000:.0f}",
                  rssi_down_dbm=rssi_down, rssi_up_dbm=rssi_up, pings_heard=heard)
        return True

    def expire(self, now: float | None = None, force: bool = False):
        """Log the probes whose answer is not coming as lost."""
        now = time.monotonic() if now is None else now
        for seq, sent_at in list(self.outstanding.items()):
            if force or now - sent_at >= PROBE_TIMEOUT:
                del self.outstanding[seq]
                self.recent.append(False)
                self.last_result = "no answer"
                self._row("probe", now, seq=seq, answered=0)

    def next_deadline(self, now: float) -> float:
        """Seconds until something here needs doing."""
        deadlines = [self.next_due - now]
        deadlines += [sent + PROBE_TIMEOUT - now for sent in self.outstanding.values()]
        return max(0.05, min(deadlines))

    # --- the rest of the walk -----------------------------------------------
    def mark(self, now: float | None = None) -> int:
        self.marks += 1
        self._row("mark", now, mark=self.marks)
        return self.marks

    def note(self, event: str, detail: str, now: float | None = None, **fields):
        """A voice message sent or received, say, during the test."""
        self._row(event, now, detail=detail, **fields)

    @property
    def success(self) -> float | None:
        """Share of recent probes answered, 0-1, or None before any."""
        if not self.recent:
            return None
        return sum(self.recent) / len(self.recent)

    def elapsed(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.started
