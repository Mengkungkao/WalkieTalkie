"""The button and a keyboard, end to end, through MFruit OS's controller.

mfruit_sdk.input.InputController is the one interpreter of the button and
the keyboard in every MFruit app. These tests drive the real controller --
no threads, a fake clock -- into the real WalkieApp handlers, so what is
checked is what an operator's thumb and keys actually do.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mfruit_sdk.input import InputController
from mfruit_sdk.keys import DOWN, REPEAT, UP, KeyEvent

from app.ui import navigation
from app.ui.screens import EDIT, HOME, SETTINGS, START, TALK
from tests.test_menu import FakeRecorder, radio  # noqa: F401
from tests.test_settings_flow import app  # noqa: F401

CODES = {"enter": 28, "escape": 1, "down": 108, "up": 103, "space": 57, "backspace": 14}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def wired(radio):
    """The radio fixture with the real controller in front of it."""
    clock = Clock()
    radio.board.foreground_ready = True
    radio.recorder = FakeRecorder()
    radio.recorder.stop = lambda: b""
    radio.codec = object()
    radio.display.set_led = lambda *_a: None
    radio.state.armed = False
    radio.running = True
    radio.input = InputController(
        radio._on_action, talk=lambda: navigation.can_talk(radio.state.screen),
        active=lambda: radio.foregrounded, on_armed=radio._on_armed,
        click_window_ms=400, long_press_ms=700, keyboard=False, clock=clock,
        threaded=False)
    radio.clock = clock
    return radio


def step(radio, seconds):
    radio.clock.now += seconds
    radio.input.gestures.poll()


def tap(radio, count=1):
    for _ in range(count):
        radio.input.press()
        step(radio, 0.06)
        radio.input.release()
        step(radio, 0.12)
    step(radio, 0.5)


def hold(radio, seconds=1.0):
    radio.input.press()
    step(radio, seconds)
    radio.input.release()
    step(radio, 0.5)


def key(radio, name, action=DOWN):
    radio.input.key_event(KeyEvent("key", name, action, CODES[name]))


def char(radio, value):
    radio.input.key_event(KeyEvent("char", value, DOWN, 2))


def test_a_hold_on_home_opens_the_row_on_release(wired):
    wired.input.press()
    step(wired, 0.8)
    assert wired.state.armed and wired.state.screen == HOME   # armed, not yet open
    wired.input.release()
    assert wired.state.screen == START and not wired.state.armed


def test_a_hold_inside_start_talks_while_held(wired):
    hold(wired)                                 # Home: open Start
    assert wired.state.screen == START
    wired.input.press()
    step(wired, 0.8)
    assert wired.recorder.started == 1          # talking before release
    assert wired.state.screen == TALK
    wired.input.release()


def test_taps_step_and_two_clicks_step_back(wired):
    tap(wired)
    tap(wired)
    assert wired.state.home_index == 2
    tap(wired, 2)
    assert wired.state.home_index == 1


def test_four_clicks_from_home_leave_the_app(wired):
    tap(wired, 4)
    assert wired._exit_reason == "user" and not wired.running


def test_keyboard_navigates_like_the_button(wired):
    key(wired, "down")
    key(wired, "down")
    key(wired, "up")
    assert wired.state.home_index == 1
    key(wired, "up")
    key(wired, "enter")
    assert wired.state.screen == START
    key(wired, "escape")
    assert wired.state.screen == HOME


def test_space_talks_on_talk_screens_only(wired):
    key(wired, "space")
    assert wired.recorder.started == 0          # Home is not a talk screen
    key(wired, "space", UP)
    key(wired, "enter")                         # Start
    key(wired, "space")
    assert wired.recorder.started == 1 and wired.state.screen == TALK
    key(wired, "space", UP)


def test_keys_do_nothing_while_another_app_has_the_screen(wired):
    wired.board.foreground_ready = False
    key(wired, "down")
    key(wired, "enter")
    wired.board.foreground_ready = True
    key(wired, "enter", REPEAT)                 # pressed while not ours
    key(wired, "enter", UP)
    assert wired.state.screen == HOME and wired.state.home_index == 0


def test_a_device_id_is_typed_on_the_keyboard(app, tmp_path):
    from tests.test_settings_flow import open_setting

    app.board = SimpleNamespace(foreground_ready=True)
    app.input = InputController(app._on_action, keyboard=False, threaded=False)
    open_setting(app, "device_id")
    assert app.state.screen == EDIT
    for digit in "00123":
        char(app, digit)
    key(app, "enter")
    assert app.state.screen == SETTINGS
    assert app.settings.radio.address == 123


def test_exit_requested_from_the_keyboard_on_home_only(wired):
    key(wired, "enter")                         # Start
    key(wired, "escape")                        # back to Home, still running
    assert wired.running
    key(wired, "escape")
    assert not wired.running
