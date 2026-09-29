"""The menu the app opens on, and the way back out of everything.

    Home   Start  > To ALL             -> Talk to everyone paired
                  > To a paired device -> the paired list -> Talk
           Receive                     -> what has come in
           Pair devices                -> see test_pairing
           Settings                    -> name, ID, privacy channel ...

Input goes through the same handler the MFruit OS input controller calls
(`_on_action`), so the tests follow what an operator's presses and keys
actually do: tap next, 2 clicks previous, hold open, 4 clicks back -- and
on talk screens, where a hold talks, 3 clicks opens.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import Contact
from app.radio import protocol
from app.store.keyring import Keyring
from app.store.roster import Roster
from app.ui.screens import CONTACTS, HOME, INBOX, SETTINGS, START, STATUS, TALK
from tests.test_pairing import FakeLink
from tests.test_settings_flow import HOLD, QUAD, TAP, THRICE, TWICE, act, app  # noqa: F401


@pytest.fixture
def radio(app, tmp_path):
    """Rover (5) with Base (1) paired, and Hilltop (9) left over from an
    older version: a contact with no keys."""
    app.keyring = Keyring(tmp_path / "keys")
    other = Keyring(tmp_path / "base")
    app.keyring.add_peer(1, other.public, other.broadcast_key)
    app.settings.contacts = [Contact("Base", 1), Contact("Hilltop", 9)]
    app.roster = Roster(app.settings.contacts, tmp_path)
    app.link = FakeLink()
    app.link.can_send = lambda addr, type_=protocol.TEXT: (
        addr == protocol.BROADCAST or app.keyring.is_paired(addr))
    app.board = SimpleNamespace(foreground_ready=True)
    app._parents = {}
    app._refresh_entries()
    app._refresh_menus()
    return app


press = act


def go_to(app, key):
    """Tap down the current menu to the row `key`, then open it.

    Home opens with a hold; Start is a talk screen, where the hold talks,
    so its rows open with three clicks.
    """
    items = app.state.home_items if app.state.screen == HOME else app.state.start_items
    keys = [item["key"] for item in items]
    index = app.state.home_index if app.state.screen == HOME else app.state.start_index
    opener = HOLD if app.state.screen == HOME else THRICE
    press(app, *[TAP] * ((keys.index(key) - index) % len(keys)), opener)


def test_the_app_opens_on_home(radio):
    assert radio.state.screen == HOME
    assert [i["key"] for i in radio.state.home_items] == \
        ["start", "receive", "pair", "settings", "range"]


def test_start_offers_all_and_a_paired_device(radio):
    go_to(radio, "start")
    assert radio.state.screen == START
    assert [i["key"] for i in radio.state.start_items] == ["all", "device"]


def test_to_all_opens_talk_on_everyone(radio):
    go_to(radio, "start")
    go_to(radio, "all")
    assert radio.state.screen == TALK
    assert radio.state.target_address == protocol.BROADCAST
    assert radio._target[0] == protocol.BROADCAST


def test_back_retraces_the_way_in(radio):
    go_to(radio, "start")
    go_to(radio, "all")
    press(radio, QUAD)                         # Talk: four clicks back
    assert radio.state.screen == START
    press(radio, QUAD)                         # Start: four clicks back
    assert radio.state.screen == HOME
    press(radio, QUAD)                         # Home: four clicks leave the app
    assert radio._exit_reason == "user" and not radio.running


def test_a_paired_device_is_picked_from_the_list(radio):
    go_to(radio, "start")
    go_to(radio, "device")
    assert radio.state.screen == CONTACTS
    assert [e.address for e in radio.state.entries] == [1, 9]
    press(radio, THRICE)                       # talk to the first: Base
    assert radio.state.screen == TALK
    assert radio._target == (1, "Base")
    assert ("hello", 1) in radio.link.sent     # calls it to check the link
    press(radio, QUAD)
    assert radio.state.screen == CONTACTS


def test_a_contact_without_keys_cannot_be_talked_to(radio):
    go_to(radio, "start")
    go_to(radio, "device")
    press(radio, TAP, THRICE)                  # Hilltop
    assert radio.state.screen == CONTACTS
    assert "pair with Hilltop first" in radio.state.active_banner
    assert 9 in radio.state.unpaired


def test_receive_opens_from_home_and_goes_back_there(radio):
    go_to(radio, "receive")
    assert radio.state.screen == INBOX
    press(radio, QUAD)
    assert radio.state.screen == HOME


def test_receive_from_talk_goes_back_to_talk(radio):
    go_to(radio, "start")
    go_to(radio, "all")
    press(radio, TAP)                          # Talk: one click, Receive
    assert radio.state.screen == INBOX
    press(radio, QUAD)
    assert radio.state.screen == TALK


def test_settings_opens_from_home(radio):
    go_to(radio, "settings")
    assert radio.state.screen == SETTINGS
    press(radio, QUAD)
    assert radio.state.screen == HOME


def test_three_clicks_on_home_shows_status(radio):
    press(radio, THRICE)
    assert radio.state.screen == STATUS
    press(radio, TAP)
    assert radio.state.screen == HOME


def test_lists_step_back_with_two_clicks(radio):
    press(radio, TAP, TAP)
    assert radio.state.home_index == 2
    press(radio, TWICE)
    assert radio.state.home_index == 1


def test_home_says_who_holding_the_button_talks_to(radio):
    go_to(radio, "start")
    go_to(radio, "device")
    press(radio, THRICE)
    radio._refresh_menus()
    assert "Base" in radio.state.home_items[0]["value"]


def test_home_counts_what_is_new(radio):
    radio.inbox.unread = 2
    radio.inbox.items = [object(), object(), object()]
    radio._refresh_menus()
    assert radio.state.home_items[1]["value"].startswith("2 new")


# --- where a hold talks ------------------------------------------------------
class FakeRecorder:
    available = True

    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1
        return True


def hold(radio):
    radio.recorder = FakeRecorder()
    radio.codec = object()
    radio.display.set_led = lambda *_a: None
    radio._on_talk_start()
    return radio.recorder.started


@pytest.mark.parametrize("key", ["receive", "settings"])
def test_a_hold_in_a_menu_or_receive_does_not_talk(radio, key):
    if key == "receive":
        go_to(radio, "receive")
    else:
        go_to(radio, "settings")
    screen = radio.state.screen
    assert hold(radio) == 0
    assert radio.state.screen == screen


def test_a_hold_on_home_says_where_talking_is(radio):
    assert hold(radio) == 0
    assert "Start" in radio.state.active_banner


def test_receive_says_it_is_for_listening(radio):
    go_to(radio, "receive")
    hold(radio)
    assert "listening" in radio.state.active_banner


def test_a_hold_inside_start_talks(radio):
    go_to(radio, "start")
    assert hold(radio) == 1
    assert radio.state.screen == TALK
