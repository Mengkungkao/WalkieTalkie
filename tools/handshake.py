#!/usr/bin/env python3
"""Prove two radios can hear each other.

Run the listener on one node and the pinger on the other:

    # node A
    python3 tools/handshake.py --listen

    # node B
    python3 tools/handshake.py --ping --to 1

The pinger sends a nonce and waits for it to come back, so a reply
proves the link works **in both directions** -- which a one-way "I saw
something" test does not. It reports round-trip time and the RSSI each
end measured, because a link that works at -70 dBm and a link barely
closing at -118 dBm look identical from a pass/fail check.

This runs the real stack -- driver, framing, protocol, link -- not a
simplified version, so a pass here means the app will work.

**Mode pins.** M0/M1 are GPIO 22 and 27, which the Whisplay daemon
drives for the LCD. While the daemon is running it holds those lines and
the module sees LCD signalling on its mode pins. Either pull the HAT's
M0/M1 jumpers (then the module sits in transparent mode and this tool
needs no GPIO), or stop the daemon for the test and pass --mode-pins so
this tool holds them low itself:

    sudo systemctl stop whisplay-daemon
    python3 tools/handshake.py --listen --mode-pins 22,27
    sudo systemctl start whisplay-daemon
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings as settings_module
from app.radio import protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x

PING = "WT-PING "
PONG = "WT-PONG "


def open_link(args, settings):
    mode_pins = None
    if args.mode_pins:
        mode_pins = tuple(int(p) for p in args.mode_pins.split(","))
    radio = SX126x(
        port=args.port, addr=args.address, freq_mhz=args.frequency,
        uart_baud=settings.radio.uart_baud, mode_pins=mode_pins,
    )
    link = LoraLink(
        radio, air_speed=args.air_speed,
        # A link test must not be throttled by the hour's budget; it
        # sends a handful of tiny packets.
        duty_cycle_percent=100.0, callsign=args.callsign,
    )
    return radio, link


def run_listen(link, args):
    print(f"listening as address {args.address} on {args.frequency} MHz "
          f"(Ctrl-C to stop)")
    seen = threading.Event()

    def on_message(message, peer):
        text = message.body.decode("utf-8", "replace")
        rssi = f"{message.rssi_dbm} dBm" if message.rssi_dbm is not None else "?"
        print(f"  <- {message.type_name} from {message.src}: {text!r}  rssi {rssi}")
        if message.type == protocol.TEXT and text.startswith(PING):
            reply = PONG + text[len(PING):]
            link.send_text(message.src, reply)
            print(f"  -> replying {reply!r} to {message.src}")
            seen.set()

    link.on_message(on_message)
    try:
        while not (args.once and seen.is_set()):
            time.sleep(0.3)
            link.tick()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0 if seen.is_set() or not args.once else 1


def run_ping(link, args):
    nonce = f"{int(time.time()) % 100000}"
    replies = []
    done = threading.Event()

    def on_message(message, peer):
        text = message.body.decode("utf-8", "replace")
        if message.type == protocol.TEXT and text == PONG + nonce:
            replies.append((time.monotonic(), message))
            done.set()
        else:
            rssi = f"{message.rssi_dbm} dBm" if message.rssi_dbm is not None else "?"
            print(f"  <- (other traffic) {message.type_name} from "
                  f"{message.src}: {text!r} rssi {rssi}")

    link.on_message(on_message)

    target = "everyone" if args.to == protocol.BROADCAST else str(args.to)
    print(f"pinging {target} from address {args.address} "
          f"on {args.frequency} MHz, nonce {nonce}")

    for attempt in range(1, args.count + 1):
        done.clear()
        sent_at = time.monotonic()
        link.send_text(args.to, PING + nonce)
        print(f"  -> ping {attempt}/{args.count}")
        if done.wait(args.timeout):
            received_at, message = replies[-1]
            rssi = message.rssi_dbm
            print(f"  <- PONG from {message.src} in "
                  f"{(received_at - sent_at) * 1000:.0f} ms"
                  + (f", rssi {rssi} dBm" if rssi is not None else ""))
            print("\nHANDSHAKE OK -- the link works in both directions")
            return 0
        print(f"     no reply within {args.timeout:.0f}s")

    print("\nHANDSHAKE FAILED -- no reply")
    print("  check on BOTH nodes:")
    print("    - same --frequency, and addresses that differ")
    print("    - provision_radio.py --check reports fixed_transmission True")
    print("    - /dev/ttyS0 is free (fuser -v /dev/ttyS0) with no getty on it")
    print("    - M0/M1 are low: jumpers pulled, or --mode-pins 22,27 with the")
    print("      whisplay daemon stopped")
    print("    - antennas are attached (transmitting without one damages the module)")
    return 1


def main() -> int:
    defaults = settings_module.load()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", action="store_true", help="wait and reply to pings")
    mode.add_argument("--ping", action="store_true", help="send a ping, await the echo")

    parser.add_argument("--to", type=lambda v: int(v, 0), default=protocol.BROADCAST,
                        help="destination address (default: broadcast)")
    parser.add_argument("--port", default=defaults.radio.port)
    parser.add_argument("--address", type=int, default=defaults.radio.address)
    parser.add_argument("--frequency", type=int, default=defaults.radio.frequency_mhz)
    parser.add_argument("--air-speed", type=int, default=defaults.radio.air_speed)
    parser.add_argument("--callsign", default=defaults.identity.callsign)
    parser.add_argument("--mode-pins", default=None,
                        help="drive M0,M1 e.g. 22,27 (stop whisplay-daemon first)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--count", type=int, default=3, help="ping attempts")
    parser.add_argument("--once", action="store_true",
                        help="listener exits after answering one ping")
    args = parser.parse_args()

    try:
        radio, link = open_link(args, defaults)
    except Exception as exc:
        print(f"! cannot open {args.port}: {exc}", file=sys.stderr)
        print("  is a getty holding it?  fuser -v /dev/ttyS0", file=sys.stderr)
        return 2

    link.start()
    try:
        return run_listen(link, args) if args.listen else run_ping(link, args)
    finally:
        link.stop()
        radio.close()


if __name__ == "__main__":
    raise SystemExit(main())
