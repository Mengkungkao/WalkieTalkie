"""When the backlight pin is also the radio's M0.

The Whisplay HAT drives BCM 22 as its LCD backlight; the LoRa HAT uses
the same line as the module's M0. The backlight is active-low and dimmed
by 1 kHz PWM ("duty_cycle = 100 - brightness"), so any brightness
between 0 and 100 toggles the module's mode a thousand times a second,
and 0 leaves it high. Full brightness is the only setting that holds M0
low -- so on this stack, hearing costs the screen.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.radio import modepins
from app.ui.display import Display


class RecordingBoard:
    foreground_ready = True

    def __init__(self):
        self.backlight = None
        self.history = []

    def draw_image(self, *_a): pass
    def set_backlight(self, brightness):
        self.backlight = brightness
        self.history.append(brightness)
    def set_rgb(self, *_a): pass
    def set_rgb_fade(self, *_a, **_k): pass


@pytest.fixture
def board():
    return RecordingBoard()


@pytest.fixture
def display(board):
    return Display(board, Settings())


# --- detecting the clash -----------------------------------------------
@pytest.mark.parametrize("pins,expected", [
    ((22, 27), True),      # stock LoRa HAT jumpers
    ([22, 27], True),
    ((5, 6), False),       # rewired off the display
    ((12, 13), False),
    (None, False),
    ((), False),
])
def test_backlight_clash_is_detected(pins, expected):
    assert modepins.conflicts_with_backlight(pins) is expected


def test_the_named_pins_match_what_whisplay_drives():
    """BOARD 15 -> BCM 22 backlight, BOARD 13 -> BCM 27 data/command."""
    assert modepins.WHISPLAY_BACKLIGHT_BCM == 22
    assert modepins.WHISPLAY_DC_BCM == 27


# --- what the lock does -------------------------------------------------
def test_locking_pins_the_backlight_fully_on(display, board):
    display.lock_brightness("test")
    assert board.backlight == 100
    assert display.brightness_locked


def test_a_locked_backlight_ignores_dimming(display, board):
    """Every intermediate value is PWM, which is what breaks the radio."""
    display.lock_brightness("test")
    for value in (0, 15, 50, 80):
        display.set_backlight(value)
        assert board.backlight == 100, f"{value} must not reach the pin"


def test_the_idle_policy_stands_down_when_locked(display, board):
    display.lock_brightness("test")
    display._last_activity -= Settings().ui.idle_off_seconds + 1
    display.apply_idle_policy()
    assert board.backlight == 100
    assert not display.screen_off


def test_a_locked_display_never_schedules_a_dimming_wakeup(display):
    """Nothing to wake up for; the loop should sleep until a real event."""
    display.lock_brightness("test")
    assert display.next_idle_deadline() == float("inf")


def test_without_the_clash_dimming_still_works(display, board):
    """Rewire the mode pins and the power saving comes back."""
    assert not display.brightness_locked
    display._last_activity -= Settings().ui.idle_off_seconds + 1
    display.apply_idle_policy()
    assert board.backlight == 0
    assert display.screen_off
