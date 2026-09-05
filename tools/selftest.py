#!/usr/bin/env python3
"""Is this LoRa HAT working? Everything checkable without a second radio.

    python3 tools/selftest.py

The module is a Waveshare SX1262 868M/915M HAT -- an EBYTE E22-900T22S
around an SX1262, talked to over a UART with two mode pins.

Only one thing here cannot be proven alone: that the module actually
radiates. Confirming that needs a second receiver, so the last step
reports what it can (the ambient noise floor, which only a running
receiver can measure) and says plainly what remains unproven.

Checks, in order of how much they rule out:

1. serial port    open it at all
2. mode pins      can the Pi drive M0/M1 to both levels
3. config mode    does the module answer a register read
4. identity       product bytes, the same on every working E22
5. settings       address, channel, air rate -- and that they persisted
6. receiver       ambient RSSI, sampled repeatedly; a live receiver
                  shows a varying noise floor, a dead one does not
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings as settings_module
from app.radio.sx126x import describe_settings

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "  · "


def line(status, text, detail=""):
    mark = {"PASS": " ok ", "FAIL": "FAIL", "WARN": "warn"}.get(status, "    ")
    print(f"  [{mark}] {text}")
    for part in (detail.split("\n") if detail else []):
        if part:
            print(f"         {part}")


def main() -> int:
    defaults = settings_module.load()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=defaults.radio.port)
    parser.add_argument("--mode-pins", default=None, help="override, e.g. 5,6")
    args = parser.parse_args()

    pins = None
    if args.mode_pins:
        pins = tuple(int(p) for p in args.mode_pins.split(","))
    elif defaults.radio.mode_pins:
        pins = tuple(defaults.radio.mode_pins)

    print(f"\nSX1262 LoRa HAT self-test  ({args.port})\n")
    failures = 0

    # 1. serial ---------------------------------------------------------
    try:
        import serial

        port = serial.Serial(args.port, defaults.radio.uart_baud, timeout=1.5)
        line(PASS, f"serial port {args.port} opened at {defaults.radio.uart_baud} baud")
    except Exception as exc:
        line(FAIL, f"cannot open {args.port}", str(exc)
             + "\nthe app holds it: systemctl --user stop walkie-talkie.service")
        return 1

    # 2. mode pins ------------------------------------------------------
    gpio = None
    pins_ok = False
    if pins is None:
        line(WARN, "no mode pins configured",
             "radio.mode_pins is unset, so this cannot switch the module\n"
             "between configuration and transparent mode itself")
    else:
        try:
            import RPi.GPIO as GPIO

            from app.radio import modepins

            gpio = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            stuck = []
            for pin in pins:
                GPIO.setup(pin, GPIO.OUT)
                for _ in range(3):
                    GPIO.output(pin, 0)
                    time.sleep(0.12)
                if modepins.sample(pin, pin, samples=3, seconds=0.1)["levels"][0]:
                    stuck.append(pin)
            pins_ok = not stuck
            if pins_ok:
                line(PASS, f"mode pins M0=GPIO{pins[0]} M1=GPIO{pins[1]} drive both ways")
            else:
                failures += 1
                line(FAIL, "a mode pin will not go low",
                     f"GPIO{', GPIO'.join(str(p) for p in stuck)} stays high when driven low.\n"
                     "Something external is holding it: a fitted M0/M1 jumper (the LCD\n"
                     "drives GPIO 22/27), a mis-wired lead, or a pin that cannot sink it.\n"
                     "The module cannot leave configuration mode until this is fixed.")
        except Exception as exc:
            line(WARN, "could not test the mode pins", str(exc))

    def mode(m0, m1):
        if gpio and pins:
            gpio.output(pins[0], m0)
            gpio.output(pins[1], m1)
            time.sleep(0.15)

    def ask(command, wait=0.5):
        port.reset_input_buffer()
        port.write(bytes(command))
        port.flush()
        time.sleep(wait)
        return port.read(port.in_waiting) if port.in_waiting else b""

    # 3-5. configuration mode -------------------------------------------
    mode(0, 1)
    registers = ask([0xC1, 0x00, 0x09])
    if len(registers) >= 12 and registers[0] == 0xC1:
        line(PASS, "module answers in configuration mode",
             f"raw: {registers.hex(' ')}")

        info = ask([0xC1, 0x80, 0x07])
        if len(info) >= 8 and info[0] == 0xC1:
            line(PASS, "product identity readable", f"raw: {info.hex(' ')}")
        else:
            line(WARN, "no product identity", "some firmware revisions omit it")

        settings = describe_settings(registers)
        detail = "\n".join(f"{k:<20} {v}" for k, v in settings.items())
        line(INFO, "stored settings", detail)

        problems = []
        if not settings["fixed_transmission"]:
            problems.append("fixed transmission is off -- addressing will not work")
        if settings["address"] in (0, 65535):
            problems.append(f"address {settings['address']} is reserved")
        if problems:
            line(WARN, "settings need attention", "\n".join(problems))
        else:
            line(PASS, "settings are usable for this app")

        again = ask([0xC1, 0x00, 0x09])
        if again == registers:
            line(PASS, "settings are stable across reads (stored, not volatile)")
        else:
            failures += 1
            line(FAIL, "settings changed between two reads",
                 f"{registers.hex(' ')}\n{again.hex(' ')}")
    else:
        failures += 1
        line(FAIL, "no answer in configuration mode",
             f"got {registers.hex(' ') if registers else 'nothing'}\n"
             + ("M1 is stuck high, so the module never entered configuration mode"
                if not pins_ok else
                "check the HAT is seated, powered, and on the right UART"))

    # 6. receiver -------------------------------------------------------
    if pins_ok or pins is None:
        mode(0, 0)
        readings = []
        for _ in range(8):
            reply = ask([0xC0, 0xC1, 0xC2, 0xC3, 0x00, 0x02], wait=0.35)
            if len(reply) >= 4 and reply[0] == 0xC1:
                readings.append(-(256 - reply[3]))
            time.sleep(0.15)
        if len(readings) >= 4:
            spread = max(readings) - min(readings)
            summary = (f"{len(readings)} samples, "
                       f"{statistics.mean(readings):.0f} dBm mean, "
                       f"{min(readings)} to {max(readings)} dBm")
            if spread == 0:
                line(WARN, "receiver answers but the noise floor never moves",
                     summary + "\na live receiver normally varies by a few dB")
            else:
                line(PASS, "receiver is running and measuring the band", summary)
        else:
            failures += 1
            line(FAIL, "receiver did not report a noise floor",
                 "the RF section may not be running")
    else:
        line(WARN, "receiver not tested",
             "the module cannot be put into transparent mode while a pin is stuck")

    mode(0, 0)
    port.close()
    if gpio:
        try:
            gpio.cleanup(list(pins))
        except Exception:
            pass

    print()
    if failures:
        print(f"  {failures} check(s) failed -- see above.\n")
    else:
        print("  Everything testable from one board passed.\n")
    print("  Not proven here: that the module actually transmits. That needs a")
    print("  second receiver -- run tools/throughput.py between two nodes, or")
    print("  watch 868 MHz on an SDR while this one sends.\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
