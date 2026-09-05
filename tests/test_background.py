"""Leaving the app, and what keeps running when focus goes away.

Four clicks exits: the app is something you open when you want it, not a
service behind the desktop. Losing *focus* is different -- another app
taking the screen must not take the radio with it, because the process
is still alive and still listening.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.main import WalkieApp
from app.ui import navigation as nav
from app.ui.screens import SETTINGS, TALK, ViewState

QUAD = "quad"


class FakeBoard:
    def __init__(self):
        self.foreground_ready = True


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


class FakeDisplay:
    screen_off = False

    def poke(self): pass
    def invalidate(self): pass
    def restore_backlight(self): pass
    def set_led(self, *_a, **_k): pass


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
    instance._closing = False
    instance.running = True
    return instance


def test_four_clicks_is_routed_to_exit():
    assert nav.route(TALK, QUAD) == nav.EXIT_APP
    assert nav.route(SETTINGS, QUAD) == nav.EXIT_APP


def test_stopping_sets_the_loop_to_finish(app):
    app.stop("user")
    assert app.running is False
    assert app._closing is True


def test_losing_focus_does_not_stop_the_radio(app):
    """Another app taking the screen must not take the radio with it."""
    app._on_focus_revoked()
    assert app.link.stopped is False
    assert app.running is True
    assert app.board.foreground_ready is False


def test_losing_focus_while_closing_is_ignored(app):
    """Focus is revoked as part of shutting down; do not chase it."""
    app._closing = True
    app._on_focus_revoked()
    assert app.board.foreground_ready is True


def test_the_microphone_is_disarmed_off_screen(app):
    """Nobody can press talk on a screen they cannot see."""
    app.board.foreground_ready = False
    app._follow_idle_with_the_microphone()
    assert app.recorder.armed is False


def test_the_microphone_re_arms_once_visible_again(app):
    app.board.foreground_ready = False
    app._follow_idle_with_the_microphone()
    app.board.foreground_ready = True
    app._follow_idle_with_the_microphone()
    assert app.recorder.armed is True
