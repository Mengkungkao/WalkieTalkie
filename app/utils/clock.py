"""Setting the time on a Pi with no real-time clock.

A Pi Zero has no RTC: with no network it boots believing it is whenever
it last shut down, so message timestamps read as nonsense. The operator
needs to be able to correct that from the device.

Actually setting the system clock needs root, which this app does not
have and should not want -- on the Zero, `systemctl` already refuses
with "Interactive authentication required". So it tries, and when it
cannot, it stores an offset and applies that to every time the app
displays. The clock the operator sees is then right either way; only
the rest of the system disagrees. `describe()` says which happened, so
nobody is left wondering why `date` still shows the old time.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess

from app.utils.logger import get_logger

log = get_logger("clock")

_offset = 0.0


def set_offset(seconds: float):
    global _offset
    _offset = float(seconds or 0.0)


def offset() -> float:
    return _offset


def now() -> datetime.datetime:
    """The current time as the app should display it."""
    return datetime.datetime.now() + datetime.timedelta(seconds=_offset)


def timestamp() -> float:
    """Unix time as the app should record it."""
    return datetime.datetime.now().timestamp() + _offset


def _try_system_clock(when: datetime.datetime) -> bool:
    """Set the real clock if this host lets us without a password."""
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    attempts = []
    if shutil.which("timedatectl"):
        # NTP overwrites a manual time within seconds, so stop it first.
        attempts.append(["sudo", "-n", "timedatectl", "set-ntp", "false"])
        attempts.append(["sudo", "-n", "timedatectl", "set-time", stamp])
    else:
        attempts.append(["sudo", "-n", "date", "-s", stamp])

    for command in attempts:
        try:
            result = subprocess.run(command, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 and "set-ntp" not in command:
            log.info("could not set the system clock: %s",
                     result.stderr.decode("utf-8", "replace").strip()[:120])
            return False
    return True


def apply(when: datetime.datetime, overrides=None) -> str:
    """Move the clock to `when`. Returns "system" or "offset"."""
    if _try_system_clock(when):
        set_offset(0.0)
        if overrides is not None:
            overrides.set_clock_offset(0.0)
        log.info("system clock set to %s", when)
        return "system"

    delta = (when - datetime.datetime.now()).total_seconds()
    set_offset(delta)
    if overrides is not None:
        overrides.set_clock_offset(delta)
    log.info("no permission to set the system clock; applying a %.0fs "
             "display offset instead", delta)
    return "offset"


def describe() -> str:
    if abs(_offset) < 1.0:
        return "system clock"
    sign = "+" if _offset > 0 else "-"
    minutes = abs(_offset) / 60.0
    if minutes < 90:
        return f"system clock {sign}{minutes:.0f}m"
    return f"system clock {sign}{minutes / 60.0:.1f}h"
