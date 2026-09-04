"""Detecting a radio that cannot hear.

With the LoRa HAT's M0/M1 jumpers fitted, the Whisplay LCD drives the
module's mode pins and parks them somewhere deaf. Everything above the
radio still appears to work -- the app records, encodes, fragments and
writes to the UART, and logs "sent 7 packets" -- while nothing goes on
the air. That is the failure this module exists to make visible.
"""

from __future__ import annotations

import pytest

from app.radio import modepins


def fake_reader(monkeypatch, sequence):
    """Feed `sample()` a fixed sequence of (m0, m1) levels."""
    values = list(sequence)
    calls = {"n": 0}

    def read(_m0, _m1):
        pair = values[min(calls["n"], len(values) - 1)]
        calls["n"] += 1
        return (pair[0], pair[1], 1, 1)

    monkeypatch.setattr(modepins, "_read", read)


@pytest.mark.parametrize("levels,mode,usable", [
    ((0, 0), "transparent", True),
    ((1, 0), "wake-on-radio tx", False),
    ((0, 1), "configuration", False),
    ((1, 1), "sleep", False),
])
def test_each_pin_combination_is_named(monkeypatch, levels, mode, usable):
    fake_reader(monkeypatch, [levels])
    result = modepins.sample(samples=3, seconds=0.0)
    assert result["mode"] == mode
    assert result["usable"] is usable
    assert result["transparent"] is (levels == (0, 0))


def test_a_deaf_radio_is_reported(monkeypatch):
    fake_reader(monkeypatch, [(0, 1)])
    result = modepins.sample(samples=4, seconds=0.0)
    assert not result["transparent"]
    assert "configuration" in result["detail"]
    assert "100%" in result["detail"]


def test_a_single_transient_low_does_not_pass_as_healthy(monkeypatch):
    """The LCD toggles these lines; one lucky read must not clear the radio."""
    fake_reader(monkeypatch, [(0, 0)] + [(1, 1)] * 9)
    result = modepins.sample(samples=10, seconds=0.0)
    assert not result["transparent"], "mostly deaf must not report as fine"


def test_consistently_transparent_pins_pass(monkeypatch):
    fake_reader(monkeypatch, [(0, 0)] * 12)
    result = modepins.sample(samples=12, seconds=0.0)
    assert result["transparent"] and result["usable"]


def test_unreadable_gpio_is_not_treated_as_a_fault(monkeypatch):
    """Off-device, or without gpio group: unknown, not broken."""
    monkeypatch.setattr(modepins, "_read", lambda _a, _b: None)
    result = modepins.sample(samples=2, seconds=0.0)
    assert result["readable"] is False
    assert result["transparent"] is False


def test_check_and_warn_returns_the_same_shape(monkeypatch):
    fake_reader(monkeypatch, [(1, 1)])
    result = modepins.check_and_warn()
    assert set(result) >= {"readable", "transparent", "detail"}
