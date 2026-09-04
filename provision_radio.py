#!/usr/bin/env python3
"""One-shot: write the LoRa module's persistent configuration.

Run this once per radio, before the app is used, and then not again.

**Why it is separate from the app.** Setting the module's frequency,
address and air rate requires driving M0/M1 -- GPIO 22 and 27 -- into
config mode. On this build those two pins belong to the Whisplay
daemon, which uses them for the LCD. The app therefore never touches
GPIO: it relies on the module already holding the right settings.

That works because this tool writes register header 0xC0, which the
module stores in **non-volatile** memory. The stock Waveshare driver
writes 0xC2 instead, which is lost at power-off and is why it has to
reconfigure -- and grab the mode pins -- at every start.

So this script stops the daemon for a few seconds, borrows the pins,
writes the settings, reads them back, and restarts the daemon:

    sudo systemctl stop whisplay-daemon
    python3 provision_radio.py --address 5 --frequency 868
    sudo systemctl start whisplay-daemon

Use --check on its own to read back what a module currently holds
without changing anything.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from app.config import settings as settings_module
from app.radio.sx126x import (AIR_SPEED, POWER_DBM, SX126x, describe_settings)

DAEMON = "whisplay-daemon"
DEFAULT_MODE_PINS = (22, 27)


def daemon_running() -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", DAEMON],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def gpio_conflict() -> str | None:
    """Name whoever currently holds M0/M1, so the error is actionable."""
    try:
        output = subprocess.run(["gpioinfo"], capture_output=True, text=True,
                                timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        for pin in DEFAULT_MODE_PINS:
            if f'line  {pin:2d}:' in line and "consumer=" in line:
                consumer = line.split('consumer="')[-1].split('"')[0]
                if consumer and consumer != "unused":
                    return f"GPIO {pin} is held by {consumer!r}"
    return None


def main() -> int:
    defaults = settings_module.load().radio
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=defaults.port)
    parser.add_argument("--address", type=int, default=defaults.address,
                        help="0-65534; must be unique on the channel")
    parser.add_argument("--frequency", type=int, default=defaults.frequency_mhz,
                        help="MHz, 850-930 or 410-493")
    parser.add_argument("--air-speed", type=int, default=defaults.air_speed,
                        choices=sorted(AIR_SPEED))
    parser.add_argument("--power", type=int, default=defaults.power_dbm,
                        choices=sorted(POWER_DBM))
    parser.add_argument("--net-id", type=int, default=0)
    parser.add_argument("--m0", type=int, default=DEFAULT_MODE_PINS[0])
    parser.add_argument("--m1", type=int, default=DEFAULT_MODE_PINS[1])
    parser.add_argument("--check", action="store_true",
                        help="read the current settings and exit")
    parser.add_argument("--force", action="store_true",
                        help="proceed even if the mode pins look busy")
    args = parser.parse_args()

    if daemon_running():
        print(f"! {DAEMON} is running and owns GPIO {args.m0}/{args.m1}.",
              file=sys.stderr)
        print(f"  Stop it first:  sudo systemctl stop {DAEMON}", file=sys.stderr)
        if not args.force:
            return 2

    conflict = gpio_conflict()
    if conflict and not args.force:
        print(f"! {conflict}. Stop that process, or pass --force.", file=sys.stderr)
        return 2

    try:
        radio = SX126x(port=args.port, addr=args.address, freq_mhz=args.frequency,
                       uart_baud=9600, mode_pins=(args.m0, args.m1),
                       read_timeout=1.0)
    except Exception as exc:
        print(f"! cannot open {args.port}: {exc}", file=sys.stderr)
        print("  Check that enable_uart=1 is applied and that no getty holds the",
              file=sys.stderr)
        print("  port:  fuser -v /dev/ttyS0", file=sys.stderr)
        return 1

    try:
        if args.check:
            reg = radio.read_settings()
            if reg is None:
                print("! no reply from the module -- wrong port, or wiring?",
                      file=sys.stderr)
                return 1
            print("current module settings:")
            for key, value in describe_settings(reg).items():
                print(f"  {key:<20} {value}")
            return 0

        print(f"writing: address={args.address} freq={args.frequency}MHz "
              f"air={args.air_speed}bps power={args.power}dBm (persistent)")
        ok = radio.configure(
            addr=args.address, freq_mhz=args.frequency, air_speed=args.air_speed,
            power=args.power, net_id=args.net_id, rssi=True, persist=True,
        )
        if not ok:
            print("! the module did not acknowledge the write", file=sys.stderr)
            return 1

        reg = radio.read_settings()
        if reg is None:
            print("  written, but read-back failed", file=sys.stderr)
            return 0
        print("read back:")
        for key, value in describe_settings(reg).items():
            print(f"  {key:<20} {value}")
        print(f"\nDone. Restart the display:  sudo systemctl start {DAEMON}")
        return 0
    finally:
        radio.close()


if __name__ == "__main__":
    raise SystemExit(main())
