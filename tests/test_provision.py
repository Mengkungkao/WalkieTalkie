"""Provisioning for range: M0/M1 on either board, and config.yaml in step.

Writing the module's air rate means holding M1 high, and M0/M1 are
header pins 15 and 13 on both boards -- different GPIO lines on each.
The air rate also has to reach config.yaml: the app paces its packets
and counts its airtime from it, and a module at 2.4k driven as if it
were 9.6k overruns its one-packet buffer.
"""

from __future__ import annotations

import sys
import types

import pytest

import provision_radio
from app.radio import modelines
from app.radio.sx126x import AIR_SPEED, SX126x, describe_settings
from tests.fakes import FakeModule


@pytest.mark.parametrize("model,chip,m0,m1", [
    ("Raspberry Pi Zero 2 W Rev 1.0", "/dev/gpiochip0", 22, 27),
    ("Raspberry Pi 5 Model B Rev 1.0", "/dev/gpiochip4", 22, 27),
    ("OrangePi Zero2 W", "/dev/gpiochip0", 261, 227),
])
def test_the_mode_lines_of_each_board(model, chip, m0, m1):
    lines = modelines.lines_for(model)
    assert (lines.chip, lines.m0, lines.m1) == (chip, m0, m1)


def test_an_unknown_board_is_not_guessed_at():
    assert modelines.lines_for("Radxa ZERO 3W") is None


# --- libgpiod, both generations ---------------------------------------------
class GpiodV2:
    """The calls libgpiod 2.x's Python bindings take (the Pi's)."""

    def __init__(self):
        self.calls = []
        line = types.ModuleType("gpiod.line")
        line.Direction = types.SimpleNamespace(OUTPUT="out")
        line.Value = types.SimpleNamespace(ACTIVE=1, INACTIVE=0)
        self.line = line
        test = self

        class Request:
            def set_values(self, values):
                test.calls.append(("set", dict(values)))

            def release(self):
                test.calls.append(("release",))

        self.LineSettings = lambda **kw: kw
        # One key holding both offsets, as the real bindings accept.
        self.request_lines = lambda chip, consumer, config: (
            test.calls.append(("request", chip, next(iter(config)))) or Request())


class GpiodV1:
    """The calls libgpiod 1.6's bindings take (the Orange Pi's)."""

    LINE_REQ_DIR_OUT = 3

    def __init__(self):
        self.calls = []
        test = self

        class Bulk:
            def request(self, consumer, type, default_vals):
                test.calls.append(("request", tuple(default_vals)))

            def set_values(self, values):
                test.calls.append(("set", tuple(values)))

            def release(self):
                test.calls.append(("release",))

        class Chip:
            def __init__(self, path):
                test.calls.append(("chip", path))

            def get_lines(self, offsets):
                test.calls.append(("lines", tuple(offsets)))
                return Bulk()

            def close(self):
                test.calls.append(("close",))

        self.Chip = Chip


def test_libgpiod_2_holds_the_lines_and_lets_go_low(monkeypatch):
    gpiod = GpiodV2()
    monkeypatch.setitem(sys.modules, "gpiod.line", gpiod.line)
    lines = modelines.lines_for("Raspberry Pi Zero 2 W")
    pins = modelines.ModeLines(lines, gpiod=gpiod)
    pins.set(0, 1)                                  # configuration mode
    pins.close()
    assert gpiod.calls[0] == ("request", "/dev/gpiochip0", (22, 27))
    assert ("set", {22: 0, 27: 1}) in gpiod.calls
    assert gpiod.calls[-2:] == [("set", {22: 0, 27: 0}), ("release",)]


def test_libgpiod_1_on_the_orange_pi():
    gpiod = GpiodV1()
    pins = modelines.ModeLines(modelines.lines_for("OrangePi Zero2 W"), gpiod=gpiod)
    pins.set(0, 1)
    pins.close()
    assert gpiod.calls[:3] == [("chip", "/dev/gpiochip0"), ("lines", (261, 227)),
                               ("request", (0, 0))]
    assert ("set", (0, 1)) in gpiod.calls
    assert gpiod.calls[-3:] == [("set", (0, 0)), ("release",), ("close",)]


class Pins:
    def __init__(self):
        self.states, self.closed = [], False

    def set(self, m0, m1):
        self.states.append((m0, m1))

    def close(self):
        self.closed = True


class ConfigModule(FakeModule):
    """Answers a register write the way the module does in config mode."""

    def write(self, data):
        self.written.extend(data)
        if data[:1] in (b"\xC0", b"\xC2"):
            self._receive_raw(b"\xC1" + bytes(data[1:]))
        elif data[:1] == b"\xC1":
            self._receive_raw(b"\xC1\x00\x09" + bytes(self.written[-9:]))
        return len(data)

    def _receive_raw(self, data):
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify_all()


def test_writing_the_air_rate_goes_through_config_mode_and_back(monkeypatch):
    port, pins = ConfigModule(), Pins()
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    radio = SX126x(port="fake", addr=0, freq_mhz=868, mode_pins=pins, read_timeout=0.1)
    assert radio.configure(addr=0, freq_mhz=868, air_speed=2400, power=22)
    assert pins.states[0] == (0, 0) and (0, 1) in pins.states and pins.states[-1] == (0, 0)
    register = bytes(port.written[-12:])
    assert register[0] == 0xC0 and register[6] & 0x07 == AIR_SPEED[2400]
    radio.close()
    assert pins.closed


def test_the_slowest_rate_decodes():
    reg = bytes([0xC1, 0, 9, 0, 0, 0, 0x60 | AIR_SPEED[300], 0x20, 18, 0xC3, 0, 0])
    assert describe_settings(reg)["air_speed"] == 300


def test_config_yaml_gets_the_rate_and_keeps_its_comments(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("radio:\n  # air rate, not UART rate\n  air_speed: 9600\n"
                      "  power_dbm: 22\n  uart_baud: 9600\n")
    assert provision_radio.write_config(config, 2400, 22)
    assert config.read_text() == ("radio:\n  # air rate, not UART rate\n  air_speed: 2400\n"
                                  "  power_dbm: 22\n  uart_baud: 9600\n")


def test_config_yaml_without_the_line_gets_one(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("radio:\n  port: /dev/ttyS0\n")
    provision_radio.write_config(config, 2400, 22)
    assert "  air_speed: 2400\n" in config.read_text()


def test_the_ranges_are_rates_the_module_has():
    for rate, _why in provision_radio.RANGES.values():
        assert rate in AIR_SPEED
    assert provision_radio.RANGES["long"][0] == 2400
