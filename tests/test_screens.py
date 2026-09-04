"""Every screen, in every radio state, rendered without a Pi.

Screens are pure functions of ViewState, so the whole UI can be
exercised on a development machine. This catches the failure mode that
is most painful to debug on the device itself: a layout that raises only
when some field is empty, None, or longer than the panel is wide.
"""

from __future__ import annotations

import pytest

from app.store.inbox import Item
from app.store.roster import Entry
from app.ui import screens, theme
from app.ui.screens import (CONTACTS, IDLE, INBOX, PLAYING, RECEIVING,
                            RECORDING, SENDING, STATUS, ViewState)


class FakeBoard:
    foreground_ready = True

    def __init__(self):
        self.frames = []

    def draw_image(self, x, y, width, height, pixels):
        self.frames.append(pixels)

    def set_backlight(self, brightness): pass
    def set_rgb(self, r, g, b): pass
    def set_rgb_fade(self, r, g, b, duration_ms=100): pass


@pytest.fixture
def display():
    from app.config.settings import Settings
    from app.ui.display import Display

    return Display(FakeBoard(), Settings())


def populated_state(**overrides) -> ViewState:
    state = ViewState(
        callsign="Rover", address=5, frequency_mhz=868,
        entries=[
            Entry("ALL STATIONS", 0xFFFF, True),
            Entry("Base", 1, True, last_heard=1.0, last_rssi=-72),
            Entry("A station with a very long name indeed", 2, False,
                  last_heard=1.0, last_rssi=-118),
        ],
        inbox=[
            Item("a", "voice", 1, "Base", 1.0, -80, duration=4.2),
            Item("b", "text", 2, "Hilltop", 1.0, -104,
                 text="a long message that will certainly not fit on one line"),
            Item("c", "voice", 1, "Base", 1.0, None, duration=9.9,
                 incomplete=True, played=True),
        ],
        target_name="Base", last_rssi=-88, duty_fraction=0.42,
        duty_remaining=20.9, codec_name="700C",
        stats={"packets_tx": 12, "packets_rx": 34, "frames_dropped": 2},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


@pytest.mark.parametrize("screen", [CONTACTS, INBOX, STATUS])
def test_list_screens_render(display, screen):
    state = populated_state(screen=screen)
    assert screens.render(display, state) is True


@pytest.mark.parametrize("radio_state", [IDLE, RECORDING, SENDING, RECEIVING, PLAYING])
def test_talk_screen_renders_in_every_state(display, radio_state):
    state = populated_state(
        screen="talk", radio_state=radio_state, record_level=0.62,
        record_seconds=3.4, tx_sent=2, tx_total=6,
    )
    assert screens.render(display, state) is True


@pytest.mark.parametrize("screen", [CONTACTS, "talk", INBOX, STATUS])
def test_screens_render_with_nothing_in_them(display, screen):
    """First boot: no contacts, no messages, no signal, no audio."""
    state = ViewState(screen=screen, audio_ok=False, audio_note="no audio hardware")
    assert screens.render(display, state) is True


def test_identical_frames_are_not_pushed_twice(display):
    """The dedupe is what keeps an idle radio from burning a core."""
    state = populated_state(screen=CONTACTS)
    assert screens.render(display, state) is True
    assert screens.render(display, state) is False
    assert display.frames_skipped == 1


def test_a_changed_frame_is_pushed(display):
    state = populated_state(screen=CONTACTS)
    screens.render(display, state)
    state.selected_index = 1
    assert screens.render(display, state) is True


def test_banner_expires(display):
    state = populated_state(screen="talk")
    state.flash("sent", seconds=0.0)
    assert state.active_banner == ""


def test_frame_is_the_size_the_daemon_expects(display):
    """240x280 RGB565: 134,400 bytes, and the daemon rejects anything else."""
    screens.render(display, populated_state(screen=CONTACTS))
    frame = display.board.frames[-1]
    assert len(frame) == theme.SCREEN_WIDTH * theme.SCREEN_HEIGHT * 2 == 134_400
