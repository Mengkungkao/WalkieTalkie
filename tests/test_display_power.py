"""Backlight behaviour -- the source of the black-screen bug.

The daemon sets the backlight exactly once, when it starts, and never
touches it again: `_release_focus` re-renders the desktop but leaves the
brightness alone. Whatever the last foreground app left is what the user
gets. An app that blanks the screen on exit therefore hands back a
desktop being drawn correctly onto a dark panel, which is
indistinguishable from broken hardware.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.ui.display import Display


class RecordingBoard:
    foreground_ready = True

    def __init__(self):
        self.backlight = None
        self.history = []
        self.frames = []

    def draw_image(self, x, y, width, height, pixels):
        self.frames.append(pixels)

    def set_backlight(self, brightness):
        self.backlight = brightness
        self.history.append(brightness)

    def set_rgb(self, r, g, b): pass
    def set_rgb_fade(self, r, g, b, duration_ms=100): pass


@pytest.fixture
def board():
    return RecordingBoard()


@pytest.fixture
def display(board):
    return Display(board, Settings())


def test_startup_lights_the_panel(display, board):
    assert board.backlight == Settings().ui.brightness


def test_idle_dims_then_blanks(display, board):
    display._last_activity -= Settings().ui.idle_dim_seconds + 1
    display.apply_idle_policy()
    assert board.backlight == Settings().ui.idle_dim_brightness
    assert not display.screen_off

    display._last_activity -= Settings().ui.idle_off_seconds
    display.apply_idle_policy()
    assert board.backlight == 0
    assert display.screen_off


def test_poke_wakes_a_blanked_screen(display, board):
    display._last_activity -= Settings().ui.idle_off_seconds + 1
    display.apply_idle_policy()
    assert display.screen_off

    display.poke()
    assert board.backlight == Settings().ui.brightness
    assert not display.screen_off


def test_restore_backlight_hands_the_panel_back_lit(display, board):
    """The exit path: never leave the desktop on a dark panel."""
    display.set_backlight(0)
    assert display.screen_off

    display.restore_backlight()
    assert board.backlight > 0
    assert board.backlight == Settings().ui.brightness
    assert not display.screen_off


def test_restore_backlight_forces_the_next_frame_through(display):
    """A cached frame hash must not suppress the redraw after a wake."""
    from app.ui.screens import ViewState, render

    state = ViewState()
    assert render(display, state) is True
    assert render(display, state) is False  # deduped
    display.restore_backlight()
    assert render(display, state) is True


def test_busy_work_holds_the_screen_awake(display, board):
    """Recording or transmitting keeps the panel lit with no button presses."""
    display._last_activity -= Settings().ui.idle_off_seconds + 1
    display.apply_idle_policy(keep_awake=True)
    assert board.backlight == Settings().ui.brightness
    assert not display.screen_off


def test_redundant_backlight_writes_are_skipped(display, board):
    before = len(board.history)
    for _ in range(5):
        display.set_backlight(Settings().ui.brightness)
    assert len(board.history) == before
