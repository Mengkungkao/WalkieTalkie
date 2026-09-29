#!/usr/bin/env python3
"""One-shot: write the LoRa module's persistent configuration.

Every module on the channel must hold the same frequency and air rate,
or they cannot hear each other at all. The address written here no
longer matters: the app addresses packets itself, and each radio's
Device ID is set in the app, by pairing or in Settings.

    sudo python3 provision_radio.py --check          read what the module holds
    sudo python3 provision_radio.py --range long     2.4k: about twice the range
    sudo python3 provision_radio.py --range normal   9.6k: the default

**Range.** A lower air rate hears weaker signals: 2.4k about 6 dB
further down than 9.6k, which is roughly twice the distance in the open
and one more wall or hill in town. It costs airtime -- four times as
much per message -- and so, under the 1% duty cycle, messages per hour.
Change every radio, one after the other; until both are done they
cannot hear each other.

**Why it is separate from the app.** Writing the module's settings means
holding M1 high, and M0/M1 are header pins 15 and 13 -- which the
Whisplay HAT uses for its backlight and data/command lines. So this
stops the Whisplay daemon for a few seconds, takes the two lines,
writes the settings (register header 0xC0: kept through power-off),
reads them back, and starts the daemon again. That needs root, hence
sudo. Then it writes the air rate into config.yaml too, because the app
paces its packets and counts its airtime from that number.

Works on a Raspberry Pi and on an Orange Pi Zero 2W, through libgpiod.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app.config import settings as settings_module  # noqa: E402
from app.radio import modelines  # noqa: E402
from app.radio.sx126x import (AIR_SPEED, POWER_DBM, SX126x,  # noqa: E402
                              describe_settings)

DAEMON = "whisplay-daemon"
CONFIG = HERE / "config.yaml"

# --range: air rate, and what it is for.
RANGES = {
    "normal": (9600, "the default: a 10 s voice message in ~7 s of airtime"),
    "long": (2400, "~6 dB more reach (about twice the distance in the open); "
                   "4x the airtime per message"),
    "longest": (1200, "~9 dB more reach; 8x the airtime -- short messages only"),
}


def daemon_running() -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", DAEMON],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def systemctl(action: str) -> bool:
    try:
        return subprocess.run(["systemctl", action, DAEMON], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def quit_app(wait: float = 5.0) -> bool:
    """Ask a running walkie app to quit: it holds the serial port.

    Two programs reading one port each get half the bytes, so the module's
    replies would arrive torn. True once no app is left running.
    """
    pattern = "[p]ython3 -m app.main"
    try:
        found = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                               text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return True
    if not found:
        return True
    print("quitting the walkie app (it holds the serial port)")
    subprocess.run(["pkill", "-TERM", "-f", pattern], timeout=5)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                          timeout=5).returncode != 0:
            return True
        time.sleep(0.2)
    return False


def write_config(path: Path, air_speed: int, power: int) -> bool:
    """Put the module's air rate and power into config.yaml, keeping the
    rest -- comments and all -- as it was. Written in place, so the file
    keeps its owner when this runs as root."""
    try:
        text = path.read_text()
    except OSError:
        return False
    new = re.sub(r"(?m)^(\s*air_speed:\s*)\d+", rf"\g<1>{air_speed}", text, count=1)
    new = re.sub(r"(?m)^(\s*power_dbm:\s*)\d+", rf"\g<1>{power}", new, count=1)
    if "air_speed:" not in new:
        new = re.sub(r"(?m)^radio:\s*$", f"radio:\n  air_speed: {air_speed}", new, count=1)
    if new == text:
        return True
    with open(path, "r+") as handle:
        handle.seek(0)
        handle.write(new)
        handle.truncate()
    return True


def main() -> int:
    # Settings are read for their defaults only; nothing of this run belongs
    # in the operator's data directory -- least of all files owned by root.
    with tempfile.TemporaryDirectory(prefix="walkie-provision-") as scratch:
        os.environ.setdefault("WALKIE_DATA_DIR", scratch)
        defaults = settings_module.load().radio
    return provision(defaults)


def provision(defaults) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=defaults.port)
    parser.add_argument("--address", type=int, default=defaults.address or 0,
                        help="the module's own address; the app ignores it")
    parser.add_argument("--frequency", type=int, default=defaults.frequency_mhz,
                        help="MHz, 850-930 or 410-493")
    parser.add_argument("--range", choices=sorted(RANGES), dest="range_name",
                        help="; ".join(f"{k}: {v[0]} bps, {v[1]}" for k, v in RANGES.items()))
    parser.add_argument("--air-speed", type=int, choices=sorted(AIR_SPEED),
                        help="an air rate in bps, instead of --range")
    parser.add_argument("--power", type=int, default=defaults.power_dbm,
                        choices=sorted(POWER_DBM))
    parser.add_argument("--net-id", type=int, default=0)
    parser.add_argument("--chip", help="gpiochip of M0/M1 (detected from the board)")
    parser.add_argument("--m0", type=int, help="M0's line on that chip")
    parser.add_argument("--m1", type=int, help="M1's line on that chip")
    parser.add_argument("--check", action="store_true",
                        help="read the current settings and exit")
    parser.add_argument("--no-config", action="store_true",
                        help="leave config.yaml alone")
    args = parser.parse_args()

    air_speed = args.air_speed or (RANGES[args.range_name][0] if args.range_name
                                   else defaults.air_speed)
    if air_speed not in AIR_SPEED:
        print(f"! {air_speed} bps is not an air rate the module has", file=sys.stderr)
        return 2

    lines = modelines.board_lines()
    if args.chip or args.m0 is not None or args.m1 is not None:
        base = lines or modelines.Lines("custom", "/dev/gpiochip0", 22, 27)
        lines = modelines.Lines("custom", args.chip or base.chip,
                                base.m0 if args.m0 is None else args.m0,
                                base.m1 if args.m1 is None else args.m1)
    if lines is None:
        print("! this board is not one whose M0/M1 lines are known; give them with "
              "--chip, --m0 and --m1", file=sys.stderr)
        return 2
    print(f"{lines.board}: M0 = {lines.chip} line {lines.m0}, M1 = line {lines.m1}")

    restart = daemon_running()
    if restart:
        if os.geteuid() != 0:
            print(f"! {DAEMON} holds M0/M1 (they are the LCD's lines too). Run this "
                  "with sudo, and it stops the daemon for a few seconds:",
                  file=sys.stderr)
            print(f"    sudo python3 {Path(sys.argv[0]).name} "
                  + " ".join(sys.argv[1:]), file=sys.stderr)
            return 2
        print(f"stopping {DAEMON} (the screen goes dark for a few seconds)")
        if not systemctl("stop"):
            print(f"! could not stop {DAEMON}", file=sys.stderr)
            return 1
        time.sleep(1.0)

    radio = None
    try:
        if not quit_app():
            print("! the walkie app did not quit; close it (four clicks) and "
                  "try again", file=sys.stderr)
            return 1
        try:
            pins = modelines.ModeLines(lines)
        except Exception as exc:
            print(f"! cannot take M0/M1: {exc}", file=sys.stderr)
            print("  Something else still holds them; is the walkie app running "
                  "outside the daemon?", file=sys.stderr)
            return 1
        try:
            radio = SX126x(port=args.port, addr=args.address, freq_mhz=args.frequency,
                           uart_baud=9600, mode_pins=pins, read_timeout=1.0)
        except Exception as exc:
            pins.close()
            print(f"! cannot open {args.port}: {exc}", file=sys.stderr)
            print("  Is the walkie app still running? It holds the port: quit it "
                  "(four clicks) and try again.", file=sys.stderr)
            return 1

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

        print(f"writing: freq={args.frequency} MHz air={air_speed} bps "
              f"power={args.power} dBm (kept through power-off)")
        ok = radio.configure(addr=args.address, freq_mhz=args.frequency,
                             air_speed=air_speed, power=args.power,
                             net_id=args.net_id, rssi=True, persist=True)
        if not ok:
            print("! the module did not acknowledge the write", file=sys.stderr)
            return 1
        reg = radio.read_settings()
        if reg is not None:
            print("read back:")
            for key, value in describe_settings(reg).items():
                print(f"  {key:<20} {value}")
        if not args.no_config:
            if write_config(CONFIG, air_speed, args.power):
                print(f"config.yaml: air_speed {air_speed}, power_dbm {args.power}")
            else:
                print(f"! set air_speed: {air_speed} in {CONFIG} by hand -- the app "
                      "paces its packets by it", file=sys.stderr)
        print("\nDone. Do the same on every other radio: until they match, they "
              "cannot hear each other.")
        return 0
    finally:
        if radio is not None:
            radio.close()
        if restart:
            print(f"starting {DAEMON}; open the walkie app from its menu again")
            systemctl("start")


if __name__ == "__main__":
    raise SystemExit(main())
