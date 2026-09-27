"""Pairing from Settings, end to end, without a Pi.

Both operators open Settings > Pair device. Each radio beacons and lists
the others it hears; picking one calls it, and that radio's operator
accepts. Only then does either side save the other as a contact.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.main import WalkieApp
from app.radio import protocol
from app.radio.link import Peer
from app.store.overrides import Overrides
from app.ui.screens import CONTACTS, EDIT, PAIR, SETTINGS
from tests.test_settings_flow import DOUBLE, SINGLE, TRIPLE, app, open_setting  # noqa: F401

JARVIS = 77


class FakeLink:
    """Records what the app asks the radio to send."""

    def __init__(self, addr=5):
        self.addr = addr
        self.sent = []
        self.peers = {}

    def send_pair(self):
        self.sent.append("pair")

    def send_hello(self, dst=protocol.BROADCAST):
        self.sent.append(("hello", dst))

    def accept(self, addr):
        self.sent.append(("accept", addr))

    def refuse(self, addr):
        self.sent.append(("refuse", addr))

    def set_address(self, addr):
        self.addr = addr

    def link_state(self, addr):
        return protocol.LINK_UNLINKED


@pytest.fixture
def radio(app):
    app.link = FakeLink()
    app.board = SimpleNamespace(foreground_ready=True)
    app._pending_hello = None
    return app


def message(type_, src=JARVIS, body=b"", rssi=-80):
    return protocol.Message(type=type_, src=src, msg_id=0, body=body, flags=0,
                            missing=[], rssi_dbm=rssi, received_at=time.time())


def beacon(app, src=JARVIS, name="jarvis"):
    body = protocol.pair_body(b"\x01\x02\x03\x04", name)
    app._on_radio_message(message(protocol.PAIR, src, body), Peer(src, name=name))


def answer(app, type_, src=JARVIS, name="jarvis"):
    app._on_radio_message(message(type_, src), Peer(src, name=name))


def contacts(app):
    return [c.address for c in app.settings.contacts]


# --- opening ---------------------------------------------------------------
def test_pair_device_opens_the_pairing_screen_and_beacons(radio):
    open_setting(radio, "pair")
    assert radio.state.screen == PAIR
    radio._pairing_tick()
    assert radio.link.sent == ["pair"]


def test_beacons_repeat_rather_than_flood(radio):
    open_setting(radio, "pair")
    radio._pairing_tick()
    radio._pairing_tick()
    assert radio.link.sent == ["pair"]


def test_pairing_without_a_radio_says_so(app):
    open_setting(app, "pair")
    assert app.state.screen == SETTINGS
    assert "offline" in app.state.active_banner


def test_three_clicks_leaves_pairing(radio):
    open_setting(radio, "pair")
    radio._leave_pairing()
    assert radio.state.screen == SETTINGS and not radio._pairing


def test_the_window_closes_on_its_own(radio):
    open_setting(radio, "pair")
    radio._pairing_until = time.monotonic() - 1
    radio._pairing_tick()
    assert radio.state.screen == SETTINGS and not radio._pairing
    assert "timed out" in radio.state.active_banner


# --- discovery -------------------------------------------------------------
def test_a_radio_heard_pairing_is_listed(radio):
    open_setting(radio, "pair")
    beacon(radio)
    assert radio.state.pair_found == [(JARVIS, "jarvis", -80, False)]


def test_a_new_find_is_answered_at_once(radio):
    """So the other radio lists us without waiting for our next beacon."""
    open_setting(radio, "pair")
    radio._pairing_tick()
    beacon(radio)
    radio._pairing_tick()
    assert radio.link.sent == ["pair", "pair"]


def test_the_list_keeps_its_order(radio):
    """Re-sorting on every beacon would move the row under the cursor."""
    open_setting(radio, "pair")
    beacon(radio, 77, "jarvis")
    beacon(radio, 88, "hilltop")
    beacon(radio, 77, "jarvis")
    assert [row[0] for row in radio.state.pair_found] == [77, 88]


def test_beacons_are_ignored_when_not_pairing(radio):
    """A stranger pairing across the street is none of our business."""
    beacon(radio)
    assert radio.state.pair_found == []
    assert JARVIS not in [e.address for e in radio.roster.entries()]


# --- asking ----------------------------------------------------------------
def test_picking_a_radio_calls_it_but_saves_nothing_yet(radio):
    open_setting(radio, "pair")
    beacon(radio)
    radio._pair_selected()
    assert ("hello", JARVIS) in radio.link.sent
    assert JARVIS not in contacts(radio)
    assert "jarvis" in radio.state.pair_status


def test_acceptance_saves_the_contact_and_shows_it(radio, tmp_path):
    open_setting(radio, "pair")
    beacon(radio)
    radio._pair_selected()
    answer(radio, protocol.HELLO_ACK)

    assert JARVIS in contacts(radio)
    assert JARVIS in [int(c["address"]) for c in Overrides(tmp_path).contacts]
    assert radio.state.screen == CONTACTS
    assert radio.roster.selected().address == JARVIS
    assert "paired" in radio.state.active_banner
    assert not radio._pairing


def test_a_refusal_is_reported_and_saves_nothing(radio):
    open_setting(radio, "pair")
    beacon(radio)
    radio._pair_selected()
    answer(radio, protocol.REJECT)
    assert JARVIS not in contacts(radio)
    assert radio.state.screen == PAIR
    assert "said no" in radio.state.pair_status


def test_an_answer_from_someone_else_is_not_mistaken_for_it(radio):
    open_setting(radio, "pair")
    beacon(radio)
    radio._pair_selected()
    answer(radio, protocol.HELLO_ACK, src=88, name="hilltop")
    assert 88 not in contacts(radio) and JARVIS not in contacts(radio)


def test_silence_is_reported(radio):
    open_setting(radio, "pair")
    beacon(radio)
    radio._pair_selected()
    addr, name, _asked = radio._pairing_with
    radio._pairing_with = (addr, name, time.monotonic() - 60)
    radio._pairing_tick()
    assert "no answer" in radio.state.pair_status


def test_pair_with_nothing_found_says_so(radio):
    open_setting(radio, "pair")
    radio._pair_selected()
    radio._next_found()
    assert "none found" in radio.state.active_banner


# --- being asked -----------------------------------------------------------
def ask(radio):
    decision = radio._on_hello(Peer(JARVIS, name="jarvis"), "jarvis")
    assert decision is None                  # left to the operator
    radio._prompt_pending_hello()
    assert radio.state.screen == EDIT


def test_accepting_a_request_while_pairing_finishes_pairing(radio):
    open_setting(radio, "pair")
    ask(radio)
    assert "wants to pair" in radio.state.editor.prompt
    for gesture in (SINGLE, DOUBLE):         # onto YES, then confirm
        radio._editor_gesture(gesture)

    assert ("accept", JARVIS) in radio.link.sent
    assert JARVIS in contacts(radio)
    assert radio.state.screen == CONTACTS


def test_refusing_a_request_goes_back_to_pairing(radio):
    open_setting(radio, "pair")
    ask(radio)
    radio._editor_gesture(TRIPLE)
    assert ("refuse", JARVIS) in radio.link.sent
    assert radio.state.screen == PAIR and radio._pairing


def test_a_call_answered_elsewhere_returns_there(radio):
    """It used to drop the operator into Settings, wherever they were."""
    radio.state.screen = CONTACTS
    ask(radio)
    radio._editor_gesture(TRIPLE)
    assert radio.state.screen == CONTACTS


# --- the same ID twice -----------------------------------------------------
def test_a_clash_moves_the_radio_that_is_pairing(radio, tmp_path):
    open_setting(radio, "pair")
    radio._on_clash("twin")
    new = radio.settings.radio.address
    assert new != 5
    assert radio.link.addr == new
    assert Overrides(tmp_path).get("radio", "address") == new
    assert "taken" in radio.state.active_banner


def test_a_radio_not_pairing_answers_instead_of_moving(radio):
    radio._on_clash("twin")
    radio._on_clash("twin")                  # once per ten seconds
    assert radio.settings.radio.address == 5
    assert radio.link.sent == ["pair"]


# --- device id ---------------------------------------------------------------
def test_a_new_device_id_reaches_the_radio_at_once(radio):
    open_setting(radio, "device_id")
    radio.state.editor.cells = [0, 0, 0, 0, 9]
    radio.state.editor.cursor = 4
    radio._editor_gesture(DOUBLE)
    assert radio.link.addr == 9
    assert "re-pair" in radio.state.active_banner


# --- two whole radios over the fake channel ----------------------------------
def over_the_air(tmp_path, monkeypatch, name, module, addr):
    """A WalkieApp with a real LoraLink on a fake module, and no hardware."""
    from app.audio.playback import cues_for
    from app.config.settings import Settings
    from app.radio.link import LoraLink
    from app.radio.sx126x import SX126x
    from app.store.roster import Roster
    from app.ui.screens import ViewState
    from tests.test_settings_flow import FakeDisplay, FakeInbox, FakePlayer

    data = tmp_path / name
    data.mkdir()
    instance = WalkieApp.__new__(WalkieApp)
    settings = Settings()
    settings.radio.address = addr
    settings.identity.callsign = name
    type(settings).data_dir = property(lambda self: self._data)
    settings._data = data
    instance.settings = settings
    instance.overrides = Overrides(data)
    instance.roster = Roster(settings.contacts, data)
    instance.inbox = FakeInbox()
    instance.player = FakePlayer()
    instance.cues = cues_for(addr, name)
    instance.state = ViewState(address=addr, callsign=name)
    instance.display = FakeDisplay()
    instance.board = SimpleNamespace(foreground_ready=True)
    instance._wake = SimpleNamespace(set=lambda: None)
    instance._pending_hello = None
    instance._actions = WalkieApp._build_actions(instance)

    monkeypatch.setattr("serial.Serial", lambda *a, **k: module)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    instance.link = LoraLink(radio, duty_cycle_percent=100.0, callsign=name,
                             token=instance.overrides.node_token)
    instance.link.on_message(instance._on_radio_message)
    instance.link.on_hello(instance._on_hello)
    instance.link.on_clash(instance._on_clash)
    instance.link.start()
    return instance


def eventually(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_two_radios_pair_over_the_air(tmp_path, monkeypatch):
    from tests.fakes import FakeModule

    left, right = FakeModule.pair()
    mengpi = over_the_air(tmp_path, monkeypatch, "MengPi", left, 1)
    jarvis = over_the_air(tmp_path, monkeypatch, "jarvis", right, 2)
    try:
        for radio in (mengpi, jarvis):
            open_setting(radio, "pair")
            radio._pairing_tick()
        assert eventually(lambda: mengpi.state.pair_found and jarvis.state.pair_found)
        assert mengpi.state.pair_found[0][:2] == (2, "jarvis")

        mengpi._pair_selected()
        assert eventually(lambda: jarvis._pending_hello is not None)
        jarvis._prompt_pending_hello()
        for gesture in (SINGLE, DOUBLE):
            jarvis._editor_gesture(gesture)

        # The answer is handled on MengPi's receive thread; wait for all of it.
        assert eventually(lambda: mengpi.state.screen == CONTACTS)
        assert 2 in contacts(mengpi)
        assert 1 in contacts(jarvis) and jarvis.state.screen == CONTACTS
        assert mengpi.link.link_state(2) == protocol.LINK_LINKED
        assert jarvis.link.link_state(1) == protocol.LINK_LINKED
    finally:
        mengpi.link.stop()
        jarvis.link.stop()


def test_two_radios_on_one_id_are_separated_by_pairing(tmp_path, monkeypatch):
    from tests.fakes import FakeModule

    left, right = FakeModule.pair()
    old = over_the_air(tmp_path, monkeypatch, "old", left, 5)
    new = over_the_air(tmp_path, monkeypatch, "new", right, 5)
    try:
        open_setting(new, "pair")
        new._pairing_tick()                    # the old radio hears its own ID
        assert eventually(lambda: new.settings.radio.address != 5)
        assert old.settings.radio.address == 5  # the one not pairing stays put
        assert new.link.addr == new.settings.radio.address
    finally:
        old.link.stop()
        new.link.stop()
