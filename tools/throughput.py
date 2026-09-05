#!/usr/bin/env python3
"""Send bytes between two radios and measure what actually gets through.

Start the echo side on one node and the ramp on the other:

    # Pi Zero
    python3 tools/throughput.py --echo

    # Pi 4B
    python3 tools/throughput.py --ramp --to 51

It begins with "Hello world" and works upward, so the first result tells
you whether the link works at all before anything larger is attempted.
Each payload is echoed back, which measures the round trip -- a one-way
test passes on a radio that can hear but cannot be heard, and that is
the failure this hardware actually has.

Reported per size: fragments, round-trip time, throughput, and the RSSI
each end measured. Throughput here is payload bytes over the round trip,
so it counts both directions plus the far end's turnaround -- roughly
half the one-way rate, and the number that matters for a walkie-talkie
where you wait for an answer.

The mode pins are checked before anything is sent. A module in
configuration or sleep mode accepts every byte over the UART and radiates
none of them, so without this check a wiring fault looks exactly like a
range problem.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings as settings_module
from app.radio import modepins, protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x

MARK = b"TP:"           # echo request
ECHO = b"TE:"           # echo reply

# "Hello world" first, then doubling to a ten-fragment message. 193 bytes
# is one fragment: the payload limit after the 7-byte header.
DEFAULT_SIZES = (11, 32, 64, 128, 193, 386, 965, 1930)


def _hold_mode_pins(pins):
    """Keep re-driving M0/M1 low, because something else keeps raising them.

    The Whisplay LCD shares GPIO 22/27 with the module's mode pins and
    writes them on every redraw, so setting the mode once at startup does
    not survive. This re-asserts it continuously, which is enough to get
    a bench measurement without stopping the display -- packets sent
    during a redraw are still lost, so a clean run means stopping the
    daemon, not this.
    """
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in pins:
        GPIO.setup(pin, GPIO.OUT)

    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                GPIO.output(pins[0], 0)
                GPIO.output(pins[1], 0)
            except Exception:
                return
            stop.wait(0.04)

    thread = threading.Thread(target=loop, name="hold-mode-pins", daemon=True)
    thread.start()
    return stop


def open_link(args, settings):
    mode_pins = None
    if args.mode_pins:
        mode_pins = tuple(int(p) for p in args.mode_pins.split(","))
    elif settings.radio.mode_pins:
        mode_pins = tuple(settings.radio.mode_pins)

    radio = SX126x(port=args.port, addr=args.address, freq_mhz=args.frequency,
                   uart_baud=settings.radio.uart_baud, mode_pins=mode_pins)

    holder = None
    if mode_pins:
        if args.hold:
            holder = _hold_mode_pins(mode_pins)
            time.sleep(0.4)

        health = modepins.sample(mode_pins[0], mode_pins[1], samples=8, seconds=0.5)
        if health["readable"] and not health["transparent"]:
            where = f"M0=GPIO{mode_pins[0]} M1=GPIO{mode_pins[1]}"
            if health["levels"] == (0, 0):
                # Mostly right but not consistently: something else is
                # writing these pins between our samples.
                print(f"! the mode pins ({where}) keep changing under us -- only "
                      f"{health['fraction'] * 100:.0f}% of samples were transparent.")
                print("  The display daemon drives GPIO 22/27 for the LCD and will")
                print("  put the module back into the wrong mode mid-test. Stop it:")
                print("    sudo systemctl stop whisplay-daemon")
            else:
                print(f"! the module is in {health['mode']} mode "
                      f"({where} read {health['levels']}).")
                print("  It will accept every byte over the UART and radiate none")
                print("  of them. Fix the mode pins before trusting any result.")
            if not args.force:
                if holder:
                    holder.set()
                radio.close()
                return None, None, None
            print("  --force given: continuing anyway.\n")

    link = LoraLink(radio, air_speed=args.air_speed,
                    # A bench test must not be throttled by the hour's
                    # budget; airtime used is reported instead.
                    duty_cycle_percent=100.0, callsign=args.callsign)
    return radio, link, holder


def run_echo(link, args):
    print(f"echoing as address {args.address} on {args.frequency} MHz "
          f"(Ctrl-C to stop)")
    count = [0]

    def on_message(message, peer):
        if message.type != protocol.TEXT or not message.body.startswith(MARK):
            return
        count[0] += 1
        rssi = f"{message.rssi_dbm} dBm" if message.rssi_dbm is not None else "?"
        gaps = f" missing {message.missing}" if message.missing else ""
        print(f"  <- {len(message.body)} B from {message.src}  rssi {rssi}{gaps}")
        link.send_text(message.src, (ECHO + message.body[len(MARK):]).decode(
            "utf-8", "replace"))
        print(f"  -> echoed {len(message.body)} B back")

    link.on_message(on_message)
    try:
        while True:
            time.sleep(0.3)
            link.tick()
    except KeyboardInterrupt:
        print(f"\nstopped after {count[0]} echo(es)")
    return 0


def run_ramp(link, args):
    sizes = ([args.size] if args.size else
             [s for s in DEFAULT_SIZES if s <= args.max_size])
    replies = {}
    got = threading.Event()

    def on_message(message, peer):
        if message.type == protocol.TEXT and message.body.startswith(ECHO):
            replies["body"] = message.body[len(ECHO):]
            replies["rssi"] = message.rssi_dbm
            replies["missing"] = message.missing
            got.set()

    link.on_message(on_message)

    print(f"ramping to {args.to} from {args.address} on {args.frequency} MHz, "
          f"{args.air_speed} bps air\n")
    print(f"  {'bytes':>6} {'frags':>5} {'rtt':>8} {'thruput':>10} "
          f"{'rssi':>8}  result")
    print(f"  {'-'*6} {'-'*5} {'-'*8} {'-'*10} {'-'*8}  {'-'*24}")

    ok_any = False
    losses = {}
    for size in sizes:
        # "Hello world" for the first rung, filler after it, so a failure
        # at size 11 is unmistakably the link and not the payload.
        if size == 11:
            payload = b"Hello world"
        else:
            payload = (b"WalkieTalkie throughput test " * (size // 28 + 1))[:size]

        body = MARK + payload
        frags = max(1, -(-len(body) // protocol.MAX_BODY))

        # Repeat to measure loss: one success proves the link exists, but
        # only a run of them says how reliable it is.
        attempts = max(1, args.repeat)
        good = 0
        rtts = []
        rssis = []
        last_note = ""
        for _ in range(attempts):
            replies.clear()
            got.clear()
            started = time.monotonic()
            link.send_text(args.to, body.decode("utf-8", "replace"))
            if got.wait(args.timeout):
                rtts.append(time.monotonic() - started)
                if replies.get("body") == payload:
                    good += 1
                    last_note = "ok"
                else:
                    last_note = (f"CORRUPT ({len(replies.get('body', b''))}"
                                 f"/{len(payload)} B back)")
                if replies.get("rssi") is not None:
                    rssis.append(replies["rssi"])
            else:
                last_note = "no reply"
            if attempts > 1:
                time.sleep(0.3)

        if attempts > 1:
            lost = attempts - good
            losses[size] = (good, attempts)
            rtt = sum(rtts) / len(rtts) if rtts else 0.0
            rate = len(payload) / rtt if rtt else 0.0
            rssi = (sum(rssis) / len(rssis)) if rssis else link.stats.last_rssi
            note = f"{good}/{attempts} ok" + (f", {lost} lost" if lost else "")
            print(f"  {size:>6} {frags:>5} {rtt:>7.2f}s {rate:>7.0f} B/s "
                  f"{(f'{rssi:.0f} dBm') if rssi is not None else '?':>8}  {note}")
            ok_any = ok_any or good > 0
            continue

        if not rtts:
            print(f"  {size:>6} {frags:>5} {'--':>8} {'--':>10} {'--':>8}  "
                  f"no reply in {args.timeout:.0f}s")
            if not ok_any:
                print("\n  nothing came back at all -- check the mode pins, "
                      "antennas, and that\n  the echo side is running with a "
                      "matching frequency and air speed.")
                return 1
            print("\n  stopped: the link stopped answering above "
                  f"{sizes[max(0, sizes.index(size) - 1)]} B")
            return 0

        rtt = rtts[0]
        returned = replies.get("body", b"")
        intact = returned == payload
        # Per-message RSSI when the module's byte arrived with the frame,
        # otherwise the channel's most recent reading.
        rssi = replies.get("rssi")
        if rssi is None:
            rssi = link.stats.last_rssi
        rate = len(payload) / rtt if rtt else 0.0
        note = "ok" if intact else (
            f"CORRUPT ({len(returned)}/{len(payload)} B back)")
        if replies.get("missing"):
            note += f" gaps {replies['missing']}"
        print(f"  {size:>6} {frags:>5} {rtt:>7.2f}s {rate:>7.0f} B/s "
              f"{(str(rssi) + ' dBm') if rssi is not None else '?':>8}  {note}")
        ok_any = ok_any or intact

    if losses:
        sent = sum(total for _good, total in losses.values())
        received = sum(good for good, _total in losses.values())
        pct = 100.0 * (sent - received) / sent if sent else 0.0
        print(f"\n  round trips: {received}/{sent} completed, "
              f"{pct:.0f}% lost")

    airtime = link.budget.used_seconds()
    print(f"\n  airtime used: {airtime:.1f}s  "
          f"({airtime / 36.0 * 100:.0f}% of an hour's 1% duty-cycle budget)")
    print(f"  packets: tx {link.stats.packets_tx}  rx {link.stats.packets_rx}  "
          f"dropped frames {link.stats.frames_dropped}")
    return 0


def main() -> int:
    defaults = settings_module.load()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--echo", action="store_true", help="reply to whatever arrives")
    mode.add_argument("--ramp", action="store_true", help="send increasing payloads")

    parser.add_argument("--to", type=lambda v: int(v, 0), default=protocol.BROADCAST)
    parser.add_argument("--port", default=defaults.radio.port)
    parser.add_argument("--address", type=int, default=defaults.radio.address)
    parser.add_argument("--frequency", type=int, default=defaults.radio.frequency_mhz)
    parser.add_argument("--air-speed", type=int, default=defaults.radio.air_speed)
    parser.add_argument("--callsign", default=defaults.identity.callsign)
    parser.add_argument("--mode-pins", default=None, help="override, e.g. 5,6")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--size", type=int, help="test one payload size only")
    parser.add_argument("--repeat", type=int, default=1,
                        help="send each size this many times and report loss")
    parser.add_argument("--max-size", type=int, default=1930)
    parser.add_argument("--force", action="store_true",
                        help="run even if the mode pins say the radio is deaf")
    parser.add_argument("--hold", action="store_true",
                        help="keep re-driving M0/M1 low, for when the LCD "
                             "daemon shares those pins and cannot be stopped")
    args = parser.parse_args()

    try:
        radio, link, holder = open_link(args, defaults)
    except Exception as exc:
        radio = link = holder = None
        print(f"! cannot open {args.port}: {exc}", file=sys.stderr)
        print("  the app holds the port; stop it with:", file=sys.stderr)
        print("    systemctl --user stop walkie-talkie.service", file=sys.stderr)
        return 2
    if link is None:
        return 2

    link.start()
    try:
        return run_echo(link, args) if args.echo else run_ramp(link, args)
    finally:
        link.stop()
        radio.close()
        if holder:
            holder.set()


if __name__ == "__main__":
    raise SystemExit(main())
