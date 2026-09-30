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

from app.ui.screens import CONTACTS, EDIT, HOME, INBOX, PAIR, SETTINGS, START, STATUS, TALK
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
        radio._on_action, talk=radio._can_talk,
        active=lambda: radio.foregrounded, on_armed=radio._on_armed,
        click_window_ms=700, long_press_ms=700, talk_press_ms=350, keyboard=False,
        clock=clock, threaded=False)
    radio.clock = clock
    return radio


def step(radio, seconds):
    radio.clock.now += seconds
    radio.input.gestures.poll()


def tap(radio, count=1, gap=0.12):
    for _ in range(count):
        radio.input.press()
        step(radio, 0.06)
        radio.input.release()
        step(radio, gap)
    step(radio, 0.8)


def hold(radio, seconds=1.0):
    radio.input.press()
    step(radio, seconds)
    radio.input.release()
    step(radio, 0.8)


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


def test_a_hold_on_a_dark_talk_screen_only_wakes_it(wired, monkeypatch):
    hold(wired)
    wired.display.screen_off = True
    monkeypatch.setattr(wired.display, "poke",
                        lambda: setattr(wired.display, "screen_off", False))
    wired.input.press()
    step(wired, 0.8)
    assert not wired.display.screen_off
    assert wired.recorder.started == 0 and wired.state.screen == START
    wired.input.release()
    assert wired.recorder.started == 0 and wired.state.screen == START
    step(wired, 0.2)
    wired.input.press()
    step(wired, 0.8)
    assert wired.recorder.started == 1
    wired.input.release()


def test_talking_starts_promptly_but_opening_is_a_deliberate_hold(wired):
    """350 ms opens the mic on a talk screen; a menu row needs MFruit OS's 700 ms."""
    wired.input.press()
    step(wired, 0.4)
    assert not wired.state.armed and wired.state.screen == HOME
    wired.input.release()                       # too short to open: it was a tap
    step(wired, 0.8)
    assert wired.state.screen == HOME and wired.state.home_index == 1
    tap(wired, 2)                               # back to Start
    hold(wired)                                 # open it
    assert wired.state.screen == START
    wired.input.press()
    step(wired, 0.4)
    assert wired.recorder.started == 1          # already talking
    wired.input.release()


def test_taps_step_and_two_clicks_step_back(wired):
    tap(wired)
    tap(wired)
    assert wired.state.home_index == 2
    tap(wired, 2)
    assert wired.state.home_index == 1


@pytest.mark.parametrize("gap", [0.08, 0.12, 0.4, 0.52])
def test_four_clicks_from_home_leave_the_app(wired, gap):
    tap(wired, 4, gap=gap)
    assert wired._exit_reason == "user" and not wired.running


def test_three_clicks_cannot_open_status_during_an_exit_attempt(wired):
    tap(wired, 3)
    assert wired.state.screen == HOME and wired.running
    tap(wired)  # a late fourth click can move, but cannot open Status
    assert wired.state.screen == HOME and wired.running
    tap(wired, 4)
    assert not wired.running


def test_status_is_a_menu_choice_with_a_held_back_button(wired):
    for _ in range(4):
        tap(wired)
    hold(wired)
    assert wired.state.screen == STATUS
    tap(wired)
    assert wired.state.screen == STATUS
    hold(wired)
    assert wired.state.screen == HOME


def test_home_back_only_exits_when_selected_and_released(wired):
    key(wired, "up")
    assert wired.state.back_selected and wired.running
    wired.input.press()
    step(wired, 0.8)
    assert wired.state.armed and wired.running
    wired.input.release()
    assert not wired.running and wired._exit_reason == "user"


@pytest.mark.parametrize("screen", [START, CONTACTS])
def test_hold_back_on_talk_lists_never_starts_the_microphone(wired, screen):
    key(wired, "enter")  # Start
    if screen == CONTACTS:
        key(wired, "down")
        key(wired, "enter")
    key(wired, "up")  # first row -> Back
    assert wired.state.screen == screen and wired.state.back_selected
    wired._refresh_entries()  # normal radio refresh must preserve Back
    wired.input.press()
    step(wired, 0.4)
    assert wired.recorder.started == 0 and wired.state.screen == screen
    step(wired, 0.4)
    assert wired.state.armed and wired.recorder.started == 0
    wired.input.release()
    assert wired.state.screen == (HOME if screen == START else START)
    assert wired.recorder.started == 0


def test_back_disarms_microphone_and_ignores_space_and_direct_talk(wired):
    key(wired, "enter")
    key(wired, "up")
    wired.recorder.recording = False
    wired.recorder.armed = True
    wired.recorder.disarm = lambda: setattr(wired.recorder, "armed", False)
    wired._follow_idle_with_the_microphone()
    assert not wired.recorder.armed
    key(wired, "space")
    key(wired, "space", UP)
    wired._on_talk_start()
    assert wired.recorder.started == 0 and wired.state.screen == START


@pytest.mark.parametrize("screen", [CONTACTS, INBOX, PAIR])
def test_empty_selection_list_stays_until_back_is_held(wired, screen):
    wired.state.screen = screen
    wired.state.entries = []
    wired.state.inbox = wired.inbox.items = []
    wired.state.pair_found = []
    if screen == CONTACTS:
        wired.roster._configured = []
    for _ in range(3):
        tap(wired)
        assert wired.state.screen == screen
    hold(wired)
    assert wired.state.screen == HOME
    assert wired.recorder.started == 0


def test_new_message_cannot_replace_a_held_back_selection(wired):
    wired._open_inbox()
    wired.input.press()
    step(wired, 0.8)
    wired.inbox.items.append(object())
    assert wired.state.back_selected
    wired.input.release()
    assert wired.state.screen == HOME


def test_new_pairing_beacon_cannot_replace_a_held_back_selection(wired):
    wired._start_pairing()
    wired.input.press()
    step(wired, 0.8)
    wired.state.pair_found.append((1, "Base", -70, False))
    assert wired.state.back_selected
    wired.input.release()
    assert wired.state.screen == HOME
    assert not wired._pairing


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
