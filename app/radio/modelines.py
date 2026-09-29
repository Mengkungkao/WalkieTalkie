"""Driving the LoRa module's M0/M1 through the kernel's GPIO lines.

Only provisioning needs this (provision_radio.py): writing the module's
settings means holding M1 high for a moment. RPi.GPIO did it on a
Raspberry Pi only; libgpiod's Python bindings are on both boards -- the
Whisplay driver itself uses them -- so this works on the Orange Pi too.

M0 and M1 are header pins 15 and 13 on both boards, which the Whisplay
HAT also uses for its backlight and data/command lines. The Whisplay
daemon holds those lines while it runs, so it has to be stopped first.
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_PATH = "/proc/device-tree/model"


@dataclass
class Lines:
    board: str
    chip: str
    m0: int          # header pin 15
    m1: int          # header pin 13


# Line offsets for header pins 15 and 13 on each board, from the same
# tables the Whisplay runtime uses: BCM 22/27 on a Pi's gpiochip0 (RP1's
# gpiochip4 on a Pi 5), PI5/PH3 on the Orange Pi Zero 2W's H618.
def lines_for(model: str) -> Lines | None:
    squashed = model.lower().replace(" ", "")
    if "raspberrypi5" in squashed:
        return Lines("Raspberry Pi 5", "/dev/gpiochip4", 22, 27)
    if "raspberrypi" in squashed:
        return Lines("Raspberry Pi", "/dev/gpiochip0", 22, 27)
    if "orangepi" in squashed and "zero2" in squashed:
        return Lines("Orange Pi Zero 2W", "/dev/gpiochip0", 261, 227)
    return None


def board_lines() -> Lines | None:
    try:
        with open(MODEL_PATH, "rb") as handle:
            model = handle.read().decode("utf-8", "replace").strip("\0 \n")
    except OSError:
        return None
    return lines_for(model)


class ModeLines:
    """M0/M1 held as outputs until closed. Works with libgpiod 1.x and 2.x."""

    CONSUMER = "walkie-provision"

    def __init__(self, lines: Lines, gpiod=None):
        if gpiod is None:
            import gpiod  # the python3-libgpiod package
        self._gpiod = gpiod
        self.lines = lines
        self._request = self._chip = self._bulk = None
        if hasattr(gpiod, "LineSettings"):       # libgpiod 2.x
            from gpiod.line import Direction, Value

            self._value = {0: Value.INACTIVE, 1: Value.ACTIVE}
            self._request = gpiod.request_lines(
                lines.chip, consumer=self.CONSUMER,
                config={(lines.m0, lines.m1): gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.INACTIVE)})
        else:                                    # libgpiod 1.x
            self._chip = gpiod.Chip(lines.chip)
            self._bulk = self._chip.get_lines([lines.m0, lines.m1])
            self._bulk.request(consumer=self.CONSUMER, type=gpiod.LINE_REQ_DIR_OUT,
                               default_vals=[0, 0])

    def set(self, m0: int, m1: int):
        if self._request is not None:
            self._request.set_values({self.lines.m0: self._value[m0 & 1],
                                      self.lines.m1: self._value[m1 & 1]})
        else:
            self._bulk.set_values([m0 & 1, m1 & 1])

    def close(self):
        """Back to transparent (both low), then let go of the lines."""
        try:
            self.set(0, 0)
        except Exception:
            pass
        for thing in (self._request, self._bulk, self._chip):
            if thing is None:
                continue
            for method in ("release", "close"):
                if hasattr(thing, method):
                    try:
                        getattr(thing, method)()
                    except Exception:
                        pass
                    break
        self._request = self._bulk = self._chip = None
