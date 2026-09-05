"""The Settings flow, end to end, without a Pi.

Drives the real handlers a button press reaches -- `_open_settings`,
`_open_setting`, `_editor_gesture` -- so the wiring between the gesture
table, the editors and the persisted overrides is exercised as a whole
rather than a piece at a time.
"""

from __future__ import annotations

import datetime

import pytest

from app.audio.playback import cues_for
from app.config.settings import Contact, Settings
from app.main import WalkieApp
from app.store.overrides import Overrides
from app.store.roster import Roster
from app.ui import navigation as nav
from app.ui.screens import EDIT, SETTINGS, ViewState
from app.utils import clock

SINGLE, DOUBLE, TRIPLE = "single", "double", "triple"


class FakePlayer:
    def cue(self, _pcm): pass
    def stop(self): pass


class FakeDisplay:
    screen_off = False
    def poke(self): pass
    def invalidate(self): pass
    def set_led(self, *_a, **_k): pass


class FakeInbox:
    def __init__(self):
        self.items = []
        self.voice_dir = None
        self.unread = 0

    def save(self): pass


@pytest.fixture
def app(tmp_path):
    """A WalkieApp with only what the settings handlers touch."""
    instance = WalkieApp.__new__(WalkieApp)
    settings = Settings()
    settings.radio.address = 5
    settings.identity.callsign = "Rover"
    settings.contacts = [Contact("Base", 1)]
    monkey_dir = tmp_path
    type(settings).data_dir = property(lambda _self: monkey_dir)

    instance.settings = settings
    instance.overrides = Overrides(tmp_path)
    instance.roster = Roster(settings.contacts, tmp_path)
    instance.inbox = FakeInbox()
    instance.player = FakePlayer()
    instance.cues = cues_for(settings.radio.address, settings.identity.callsign)
    instance.state = ViewState(address=settings.radio.address)
    instance.display = FakeDisplay()
    instance._wake = type("Event", (), {"set": lambda self: None})()
    instance._actions = WalkieApp._build_actions(instance)
    clock.set_offset(0.0)
    yield instance
    clock.set_offset(0.0)


def open_setting(app, key):
    app._open_settings()
    keys = [item["key"] for item in app.state.settings_items]
    app.state.settings_index = keys.index(key)
    app._open_setting()


def play(app, *gestures):
    for gesture in gestures:
        app._editor_gesture(gesture)


# --- structure ---------------------------------------------------------
def test_settings_lists_every_promised_entry(app):
    app._open_settings()
    keys = [item["key"] for item in app.state.settings_items]
    assert keys == ["device_id", "base", "add", "clock", "reset", "quit"]
    assert app.state.screen == SETTINGS


def test_opening_a_setting_enters_an_editor(app):
    open_setting(app, "device_id")
    assert app.state.screen == EDIT and app.state.editor is not None


def test_every_settings_row_opens_without_error(app):
    app._open_settings()
    for index in range(len(app.state.settings_items)):
        app.state.settings_index = index
        app._open_setting()
        assert app.state.screen == EDIT
        app._editor_gesture(TRIPLE)          # cancel back out
        assert app.state.screen == SETTINGS


# --- device id ---------------------------------------------------------
def test_setting_the_device_id_persists(app, tmp_path):
    open_setting(app, "device_id")
    app.state.editor.cells = [0, 0, 0, 0, 9]
    app.state.editor.cursor = 4
    play(app, DOUBLE)                        # commit from the last digit

    assert app.settings.radio.address == 9
    assert Overrides(tmp_path).get("radio", "address") == 9


def test_cancelling_an_edit_changes_nothing(app, tmp_path):
    open_setting(app, "device_id")
    app.state.editor.cells = [0, 0, 0, 0, 9]
    play(app, TRIPLE)
    assert app.settings.radio.address == 5
    assert Overrides(tmp_path).get("radio", "address") is None


def test_device_id_cannot_collide_with_a_contact(app):
    """Two nodes on one address discard each other's traffic silently."""
    open_setting(app, "device_id")
    app.state.editor.cells = [0, 0, 0, 0, 1]   # Base is address 1
    app.state.editor.cursor = 4
    play(app, DOUBLE)
    assert app.settings.radio.address == 5
    assert "contact" in app.state.active_banner.lower()


# --- contacts ----------------------------------------------------------
def test_adding_a_device_persists_and_reaches_the_roster(app, tmp_path):
    open_setting(app, "add")
    app.state.editor.cells = [0, 0, 0, 4, 2]
    app.state.editor.cursor = 4
    play(app, DOUBLE)

    assert 42 in [c.address for c in app.settings.contacts]
    assert 42 in [int(c["address"]) for c in Overrides(tmp_path).contacts]
    assert 42 in [e.address for e in app.roster.entries()]


def test_adding_our_own_address_is_refused(app):
    open_setting(app, "add")
    app.state.editor.cells = [0, 0, 0, 0, 5]
    app.state.editor.cursor = 4
    play(app, DOUBLE)
    assert 5 not in [c.address for c in app.settings.contacts]


def test_base_station_starts_on_the_current_value(app):
    """Nothing is set yet, so the picker opens on "(none)"."""
    open_setting(app, "base")
    assert app.state.editor.text == "(none)"


def test_choosing_a_base_station_persists(app, tmp_path):
    open_setting(app, "base")
    play(app, SINGLE)                        # step off "(none)" onto a station
    chosen = app.state.editor.value
    play(app, DOUBLE)
    assert chosen is not None
    assert Overrides(tmp_path).base_address == chosen


# --- clock -------------------------------------------------------------
def test_setting_the_clock_falls_back_to_an_offset(app, monkeypatch, tmp_path):
    """No root means no system clock, but timestamps must still be right."""
    monkeypatch.setattr(clock, "_try_system_clock", lambda _when: False)
    open_setting(app, "clock")
    app.state.editor.values[0] += 1          # next year
    play(app, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE)

    assert clock.offset() > 0
    assert Overrides(tmp_path).clock_offset > 0
    assert clock.now().year == datetime.datetime.now().year + 1


def test_setting_the_clock_for_real_clears_any_offset(app, monkeypatch, tmp_path):
    monkeypatch.setattr(clock, "_try_system_clock", lambda _when: True)
    clock.set_offset(500.0)
    open_setting(app, "clock")
    play(app, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE)
    assert clock.offset() == 0.0


# --- reset -------------------------------------------------------------
def test_reset_defaults_to_no_and_keeps_everything(app, tmp_path):
    app.overrides.set("radio", "address", 9)
    open_setting(app, "reset")
    play(app, DOUBLE)                        # confirm while still on "no"
    assert Overrides(tmp_path).get("radio", "address") == 9


def test_reset_erases_everything_once_confirmed(app, tmp_path):
    app.overrides.set("radio", "address", 9)
    app.overrides.add_contact("Hilltop", 77)
    app.roster.note_peer(77, "Hilltop", -90)
    open_setting(app, "reset")
    play(app, SINGLE, DOUBLE)                # onto YES, then confirm

    assert Overrides(tmp_path).data == {}
    assert app.inbox.items == []
    assert clock.offset() == 0.0


# --- talking ------------------------------------------------------------
def test_hold_to_talk_is_suspended_while_editing(app):
    """A hold here would transmit a half-typed address."""
    open_setting(app, "device_id")
    app.link = None
    app.recorder = None
    WalkieApp._on_talk_start(app)
    assert app.state.screen == EDIT
    assert "editing" in app.state.active_banner.lower()


def test_the_editor_screen_is_not_in_the_navigation_table(app):
    """Editors route their own gestures; the table must not steal them."""
    for gesture in (SINGLE, DOUBLE, TRIPLE):
        assert nav.route(EDIT, gesture) is None
