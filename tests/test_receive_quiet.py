"""The screen must not deafen the radio while a message is arriving.

A full 240x280 push clocks DC for about 11 ms, and DC is the module's
M1 on this stack, so the module leaves transparent mode for that long.
Receiving used to refresh every 300 ms: over a three-fragment message
that is roughly five redraws and 10% of the message's air time spent
deaf. Six of ten messages arrived with a fragment missing.
"""

from __future__ import annotations

import pytest

from app.main import FRAME_INTERVAL, WalkieApp
from app.ui.screens import (IDLE, PLAYING, RECEIVING, RECORDING, SENDING,
                            ViewState)


class FakeLink:
    def __init__(self, reassembling=0):
        self.reassembling = reassembling


@pytest.fixture
def app():
    instance = WalkieApp.__new__(WalkieApp)
    instance.state = ViewState()
    instance.link = FakeLink()
    instance._display_stale = False
    return instance


@pytest.mark.parametrize("state", [RECEIVING, PLAYING, SENDING, IDLE])
def test_only_recording_is_animated(state):
    """Every other state draws a static frame; refreshing it only costs
    the radio its hearing."""
    assert state not in FRAME_INTERVAL


def test_recording_still_animates():
    """The level meter is the only proof the microphone is live."""
    assert FRAME_INTERVAL[RECORDING] <= 0.1


def test_the_radio_is_busy_while_reassembling(app):
    app.link.reassembling = 1
    assert app._radio_is_busy()


def test_the_radio_is_busy_while_sending(app):
    """Redrawing mid-transmission corrupts our own packet, not just theirs."""
    app.state.radio_state = SENDING
    assert app._radio_is_busy()


def test_the_radio_is_idle_otherwise(app):
    assert not app._radio_is_busy()
    app.state.radio_state = PLAYING
    assert not app._radio_is_busy(), "playback is after the packets, not during"


def test_no_radio_means_never_busy(app):
    app.link = None
    assert not app._radio_is_busy()


def test_a_busy_radio_asks_for_no_timer(app):
    """Wake on the packet, not on a frame that would deafen us."""
    app.link.reassembling = 2
    app.state.battery_present = False
    app.settings = type("S", (), {"power": type("P", (), {
        "beacon_interval_seconds": 0, "tick_seconds": 5})()})()
    app.display = type("D", (), {"next_idle_deadline": lambda self: float("inf")})()
    assert app._next_timeout() is None
