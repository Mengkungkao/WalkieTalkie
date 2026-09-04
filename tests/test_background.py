"""Four clicks hides the app; it must not stop listening.

Exiting closed the serial port, so the radio went deaf the moment you
left the app and anything sent while you were on the desktop was lost. A
walkie-talkie that only hears you when its screen is open is not a
walkie-talkie.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.main import WalkieApp
from app.ui import navigation as nav
from app.ui.screens import CONTACTS, SETTINGS, TALK, ViewState

QUAD = "quad"


class FakeBoard:
    def __init__(self):
        self.foreground_ready = True
        self.released = False

    def release_focus(self):
        self.released = True


class FakeDisplay:
    def __init__(self):
        self.backlight = 0
        self.screen_off = False
        self.led = None

    def restore_backlight(self):
        self.backlight = 80

    def set_led(self, colour, fade_ms=0):
        self.led = colour

    def poke(self): pass
    def invalidate(self): pass


class FakeRecorder:
    available = True

    def __init__(self):
        self.armed = True
        self.recording = False

    def disarm(self):
        self.armed = False

    def arm(self):
        self.armed = True
        return True


class FakeLink:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def app():
    instance = WalkieApp.__new__(WalkieApp)
    instance.settings = Settings()
    instance.board = FakeBoard()
    instance.display = FakeDisplay()
    instance.recorder = FakeRecorder()
    instance.link = FakeLink()
    instance.state = ViewState(screen=TALK)
    instance._wake = type("Event", (), {"set": lambda self: None})()
    instance.running = True
    return instance


def test_four_clicks_is_routed_to_background_not_exit():
    assert nav.route(TALK, QUAD) == nav.BACKGROUND_APP


def test_backgrounding_keeps_the_link_running(app):
    """The whole point: the radio must still be listening afterwards."""
    app._background()
    assert app.link.stopped is False
    assert app.running is True


def test_backgrounding_releases_the_screen(app):
    app._background()
    assert app.board.released is True
    assert app.board.foreground_ready is False
    assert app.foregrounded is False


def test_backgrounding_hands_the_panel_back_lit(app):
    """The daemon inherits our brightness; leaving it dark looks broken."""
    app._background()
    assert app.display.backlight > 0


def test_backgrounding_powers_the_microphone_down(app):
    """Nobody can press talk off screen, so the codec should not stay warm."""
    assert app.recorder.armed
    app._background()
    assert app.recorder.armed is False


def test_backgrounding_twice_is_harmless(app):
    app._background()
    app.board.released = False
    app._background()
    assert app.board.released is False, "should not release focus it does not hold"


def test_the_microphone_stays_off_while_backgrounded(app):
    """Idle policy must not re-arm the codec for an app nobody can see."""
    app._background()
    app.recorder.armed = False
    app._follow_idle_with_the_microphone()
    assert app.recorder.armed is False


def test_the_microphone_re_arms_once_foregrounded_again(app):
    app._background()
    app.board.foreground_ready = True          # the daemon handed it back
    app._follow_idle_with_the_microphone()
    assert app.recorder.armed is True


def test_stopping_the_radio_is_a_separate_confirmed_action(app):
    """Quitting for real exists, but it is not a gesture you can fumble."""
    app.overrides = type("O", (), {"base_address": None})()
    app.inbox = type("I", (), {"items": []})()
    app.roster = type("R", (), {"entries": staticmethod(lambda: [])})()
    keys = [item["key"] for item in WalkieApp._settings_items(app)]
    assert "quit" in keys
    assert nav.route(SETTINGS, QUAD) == nav.BACKGROUND_APP  # still only hides
