"""Battery state from a PiSugar, when one is fitted.

A walkie-talkie that runs flat mid-conversation is worse than one that
warned you, and on this build the screen cannot dim to save power -- the
backlight pin is the radio's M0, so dimming it deafens the radio. That
makes the remaining charge worth showing.

pisugar-server speaks a line protocol over a Unix socket. It answers
even with no battery attached, reporting "I2C not connected", so a
running server is not proof of a battery -- the readings have to be
checked, not merely fetched.

Polling is deliberately lazy. Charge moves over minutes, so this reads
at most once a minute and hands back the cached value in between, off
the UI thread's critical path.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

from app.utils.logger import get_logger

log = get_logger("battery")

SOCKET_PATH = "/tmp/pisugar-server.sock"
POLL_SECONDS = 60.0

# Below this, say so. A PiSugar 2 cuts out somewhere under 10%, and a
# radio that dies without warning is the failure worth avoiding.
LOW_PERCENT = 20.0
CRITICAL_PERCENT = 8.0


@dataclass
class Battery:
    present: bool = False
    percent: float | None = None
    volts: float | None = None
    amps: float | None = None       # negative while discharging
    charging: bool = False
    plugged: bool = False

    @property
    def low(self) -> bool:
        return self.present and self.percent is not None \
            and self.percent <= LOW_PERCENT and not self.charging

    @property
    def critical(self) -> bool:
        return self.present and self.percent is not None \
            and self.percent <= CRITICAL_PERCENT and not self.charging

    @property
    def milliamps(self):
        return None if self.amps is None else self.amps * 1000.0

    def hours_left(self, capacity_mah: float = 1200.0):
        """Rough runtime at the current draw. None when not discharging."""
        if self.percent is None or self.amps is None or self.amps >= -0.01:
            return None
        return (capacity_mah * self.percent / 100.0) / abs(self.milliamps)

    def summary(self) -> str:
        if not self.present:
            return "no battery"
        parts = [f"{self.percent:.0f}%"]
        if self.volts is not None:
            parts.append(f"{self.volts:.2f}V")
        if self.charging:
            parts.append("charging")
        elif self.milliamps is not None:
            parts.append(f"{self.milliamps:.0f}mA")
            hours = self.hours_left()
            if hours is not None:
                parts.append(f"~{hours:.1f}h left")
        return "  ".join(parts)


def _ask(command: str, timeout: float = 2.0):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(SOCKET_PATH)
            client.sendall((command + "\n").encode("utf-8"))
            reply = client.recv(256).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if ":" not in reply:
        return None
    value = reply.split(":", 1)[1].strip()
    # The server answers with this when no battery is wired up.
    return None if "not connected" in value.lower() else value


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read() -> Battery:
    """One round of queries. Returns a Battery with present=False if absent."""
    percent = _number(_ask("get battery"))
    if percent is None:
        return Battery(present=False)
    return Battery(
        present=True,
        percent=percent,
        volts=_number(_ask("get battery_v")),
        amps=_number(_ask("get battery_i")),
        charging=(_ask("get battery_charging") or "").lower() == "true",
        plugged=(_ask("get battery_power_plugged") or "").lower() == "true",
    )


class Monitor:
    """Cached battery state, refreshed on a slow timer."""

    def __init__(self, poll_seconds: float = POLL_SECONDS):
        self.poll_seconds = poll_seconds
        self.state = Battery()
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self._warned_low = False

    def poll(self, force: bool = False) -> Battery:
        now = time.monotonic()
        if not force and now - self._checked_at < self.poll_seconds:
            return self.state
        with self._lock:
            self._checked_at = now
            self.state = read()
        if self.state.low and not self._warned_low:
            self._warned_low = True
            log.warning("battery low: %s", self.state.summary())
        elif not self.state.low:
            self._warned_low = False
        return self.state

    def seconds_until_next_poll(self) -> float:
        return max(0.0, self.poll_seconds - (time.monotonic() - self._checked_at))
