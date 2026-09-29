"""Pairing, end to end, without a Pi.

Both operators open Home > Pair devices. Each radio beacons its public
key and lists the others it hears. Picking one sends a pairing request;
both screens show the same four-digit code; the other operator accepts,
and the answer carries its keys back. Only then does either side save
the other -- the one that asked, when the answer arrives.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.main import WalkieApp
from app.radio import protocol
from app.radio.link import Peer
from app.store.inbox import Inbox
from app.store.keyring import Keyring
from app.store.overrides import Overrides
from app.ui.screens import CONTACTS, EDIT, HOME, PAIR, START
from tests.test_settings_flow import HOLD, QUAD, TAP, app, open_setting, play  # noqa: F401

JARVIS = 77
ME = 5


class FakeLink:
    """Records what the app asks the radio to send."""

    def __init__(self, addr=ME):
        from app.radio.airtime import AirtimeBudget

        self.addr = addr
        self.sent = []
        self.peers = {}
        self.callsign = ""
        self.budget = AirtimeBudget(9600, 1.0)

    def plan(self, dst, size):
        """As LoraLink.plan for a sealed message: 168 B a fragment, 37 more on air."""
        fragments = max(1, -(-size // protocol.VOICE_CHUNK))
        return fragments, size + fragments * 37

    def send_pair(self):
        self.sent.append("pair")

    def send_pairing(self, type_, dst, body):
        self.sent.append((protocol.TYPE_NAMES[type_], dst, body))

    def send_hello(self, dst=protocol.BROADCAST):
        self.sent.append(("hello", dst))

    def refuse(self, addr):
        self.sent.append(("refuse", addr))

    def mark_linked(self, addr):
        self.sent.append(("linked", addr))

    def set_address(self, addr):
        self.addr = addr

    def set_channel(self, channel):
        pass

    def can_send(self, addr, type_=protocol.TEXT):
        return True

    def link_state(self, addr):
        return protocol.LINK_UNLINKED

    def of_type(self, name):
        return [entry for entry in self.sent if isinstance(entry, tuple) and entry[0] == name]


@pytest.fixture
def radio(app, tmp_path):
    app.keyring = Keyring(tmp_path / "keys")
    app.link = FakeLink()
    app.board = SimpleNamespace(foreground_ready=True)
    app._pending_pair = None
    app._parents = {}
    app._refresh_entries()
    app._refresh_menus()
    return app


@pytest.fixture
def jarvis(tmp_path):
    """The other radio's keys."""
    return Keyring(tmp_path / "jarvis")


def message(type_, src=JARVIS, body=b"", rssi=-80, dst=ME):
    return protocol.Message(type=type_, src=src, msg_id=0, body=body, flags=0,
                            missing=[], rssi_dbm=rssi, received_at=time.time(),
                            dst=dst)


def beacon(radio, keys, src=JARVIS, name="jarvis"):
    body = protocol.pair_body(b"\x01\x02\x03\x04", keys.public, name)
    radio._on_radio_message(message(protocol.PAIR, src, body, dst=protocol.BROADCAST),
                            Peer(src, name=name))


def open_pairing(radio):
    radio.state.home_index = [i["key"] for i in radio.state.home_items].index("pair")
    radio._open_item()


def contacts(app):
    return [c.address for c in app.settings.contacts]


def answer_yes(radio, keys, src=JARVIS, name="jarvis"):
    body = keys.pair_body(radio.keyring.public, src, ME, name)
    radio._on_radio_message(message(protocol.PAIR_ACCEPT, src, body), Peer(src, name=name))


# --- opening -------------------------------------------------------------------
def test_pair_devices_opens_from_home_and_beacons(radio):
    open_pairing(radio)
    assert radio.state.screen == PAIR
    radio._pairing_tick()
    assert radio.link.sent == ["pair"]


def test_beacons_repeat_rather_than_flood(radio):
    open_pairing(radio)
    radio._pairing_tick()
    radio._pairing_tick()
    assert radio.link.sent == ["pair"]


def test_pairing_without_a_radio_says_so(app):
    app._refresh_menus()
    open_pairing(app)
    assert app.state.screen == HOME
    assert "offline" in app.state.active_banner


def test_back_leaves_pairing_for_home(radio):
    open_pairing(radio)
    radio._go_back()
    assert radio.state.screen == HOME


def test_the_window_closes_on_its_own(radio):
    open_pairing(radio)
    radio._pairing_until = time.monotonic() - 1
    radio._pairing_tick()
    assert radio.state.screen == HOME and not radio._pairing
    assert "timed out" in radio.state.active_banner


# --- discovery -------------------------------------------------------------------
def test_a_radio_heard_pairing_is_listed(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    assert radio.state.pair_found == [(JARVIS, "jarvis", -80, False)]


def test_a_new_find_is_answered_at_once(radio, jarvis):
    """So the other radio lists us without waiting for our next beacon."""
    open_pairing(radio)
    radio._pairing_tick()
    beacon(radio, jarvis)
    radio._pairing_tick()
    assert radio.link.sent == ["pair", "pair"]


def test_the_list_keeps_its_order(radio, jarvis, tmp_path):
    """Re-sorting on every beacon would move the row under the cursor."""
    open_pairing(radio)
    beacon(radio, jarvis, 77, "jarvis")
    beacon(radio, Keyring(tmp_path / "hilltop"), 88, "hilltop")
    beacon(radio, jarvis, 77, "jarvis")
    assert [row[0] for row in radio.state.pair_found] == [77, 88]


def test_beacons_are_ignored_when_not_pairing(radio, jarvis):
    """A stranger pairing across the street is none of our business."""
    beacon(radio, jarvis)
    assert radio.state.pair_found == []
    assert JARVIS not in [e.address for e in radio.roster.entries()]


# --- asking ------------------------------------------------------------------------
def test_picking_a_radio_sends_a_request_only_it_can_read(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    [(_kind, dst, body)] = radio.link.of_type("pair-request")
    assert dst == JARVIS
    assert jarvis.open_pair_body(body, ME, JARVIS) == (
        radio.keyring.public, radio.keyring.broadcast_key, "Rover")
    assert JARVIS not in contacts(radio)


def test_both_screens_show_the_same_code(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    code = jarvis.code_with(radio.keyring.public)
    assert f"code {code}" in radio.state.pair_status


def test_acceptance_saves_the_contact_and_its_keys(radio, jarvis, tmp_path):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    answer_yes(radio, jarvis)

    assert JARVIS in contacts(radio)
    assert JARVIS in [int(c["address"]) for c in Overrides(tmp_path).contacts]
    assert radio.keyring.peer_broadcast(JARVIS) == jarvis.broadcast_key
    assert radio.keyring.peer_public(JARVIS) == jarvis.public
    assert ("linked", JARVIS) in radio.link.sent
    assert radio.state.screen == CONTACTS
    assert radio.roster.selected().address == JARVIS
    assert "paired" in radio.state.active_banner


def test_after_pairing_back_leads_home_not_into_pairing(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    answer_yes(radio, jarvis)
    radio._go_back()
    assert radio.state.screen == START
    radio._go_back()
    assert radio.state.screen == HOME


def test_an_answer_with_a_different_key_is_refused(radio, jarvis, tmp_path):
    """Someone else answering in jarvis's name, with their own key."""
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    answer_yes(radio, Keyring(tmp_path / "mallory"))
    assert JARVIS not in contacts(radio)
    assert not radio.keyring.is_paired(JARVIS)
    assert "did not match" in radio.state.pair_status


def test_a_refusal_is_reported_and_saves_nothing(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    radio._on_radio_message(message(protocol.REJECT), Peer(JARVIS, name="jarvis"))
    assert JARVIS not in contacts(radio)
    assert radio.state.screen == PAIR
    assert "said no" in radio.state.pair_status


def test_an_answer_from_someone_else_is_not_mistaken_for_it(radio, jarvis, tmp_path):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    answer_yes(radio, Keyring(tmp_path / "hilltop"), src=88, name="hilltop")
    assert 88 not in contacts(radio) and JARVIS not in contacts(radio)


def test_silence_is_reported(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    addr, name, _asked, public = radio._pairing_with
    radio._pairing_with = (addr, name, time.monotonic() - 60, public)
    radio._pairing_tick()
    assert "no answer" in radio.state.pair_status


def test_pair_with_nothing_found_says_so(radio):
    open_pairing(radio)
    radio._pair_selected()
    radio._next_found()
    assert "none found" in radio.state.active_banner


# --- being asked ----------------------------------------------------------------------
def request(radio, keys, src=JARVIS, name="jarvis"):
    body = keys.pair_body(radio.keyring.public, src, ME, name)
    radio._on_radio_message(message(protocol.PAIR_REQUEST, src, body), Peer(src, name=name))


def test_a_request_shows_the_code_to_compare(radio, jarvis):
    open_pairing(radio)
    request(radio, jarvis)
    radio._prompt_pending_pair()
    assert radio.state.screen == EDIT
    assert "wants to pair" in radio.state.editor.prompt
    assert jarvis.code_with(radio.keyring.public) in radio.state.editor.detail


def test_accepting_saves_the_keys_and_answers_with_ours(radio, jarvis):
    open_pairing(radio)
    request(radio, jarvis)
    radio._prompt_pending_pair()
    play(radio, TAP, HOLD)                     # onto YES, then confirm

    assert radio.keyring.peer_broadcast(JARVIS) == jarvis.broadcast_key
    [(_kind, dst, body)] = radio.link.of_type("pair-accept")
    assert dst == JARVIS
    assert jarvis.open_pair_body(body, ME, JARVIS)[1] == radio.keyring.broadcast_key
    assert JARVIS in contacts(radio)
    assert radio.state.screen == CONTACTS


def test_refusing_goes_back_to_pairing(radio, jarvis):
    open_pairing(radio)
    request(radio, jarvis)
    radio._prompt_pending_pair()
    play(radio, QUAD)
    assert ("refuse", JARVIS) in radio.link.sent
    assert not radio.keyring.is_paired(JARVIS)
    assert radio.state.screen == PAIR and radio._pairing


def test_a_request_while_not_pairing_is_ignored(radio, jarvis):
    """Nobody can make the radio in your pocket start asking you things."""
    request(radio, jarvis)
    assert radio._pending_pair is None


def test_an_unreadable_request_is_ignored(radio, jarvis, tmp_path):
    open_pairing(radio)
    body = jarvis.pair_body(Keyring(tmp_path / "other").public, JARVIS, ME, "jarvis")
    radio._on_radio_message(message(protocol.PAIR_REQUEST, JARVIS, body),
                            Peer(JARVIS, name="jarvis"))
    assert radio._pending_pair is None


# --- the same ID twice --------------------------------------------------------------
def test_a_clash_moves_the_radio_that_is_pairing(radio, tmp_path):
    open_pairing(radio)
    radio._on_clash("twin")
    new = radio.settings.radio.address
    assert new != ME
    assert radio.link.addr == new
    assert Overrides(tmp_path).get("radio", "address") == new
    assert "taken" in radio.state.active_banner


def test_a_radio_not_pairing_answers_instead_of_moving(radio):
    radio._on_clash("twin")
    radio._on_clash("twin")                    # once per ten seconds
    assert radio.settings.radio.address == ME
    assert radio.link.sent == ["pair"]


# --- device id and reset ---------------------------------------------------------------
def test_a_new_device_id_reaches_the_radio_at_once(radio):
    open_setting(radio, "device_id")
    radio.state.editor.cells = [0, 0, 0, 0, 9]
    radio.state.editor.cursor = 4
    play(radio, HOLD)
    assert radio.link.addr == 9
    assert "re-pair" in radio.state.active_banner


def test_reset_forgets_every_paired_radio_and_its_keys(radio, jarvis):
    open_pairing(radio)
    beacon(radio, jarvis)
    radio._pair_selected()
    answer_yes(radio, jarvis)
    old = radio.keyring.public
    radio._apply_reset()
    assert contacts(radio) == []
    assert not radio.keyring.is_paired(JARVIS)
    assert radio.keyring.public != old


# --- two whole radios over the fake channel ---------------------------------------------
def over_the_air(tmp_path, monkeypatch, name, module, addr):
    """A WalkieApp with a real, keyed LoraLink on a fake module."""
    from app.audio.playback import cues_for
    from app.config.settings import Settings
    from app.radio.link import LoraLink
    from app.radio.sx126x import SX126x
    from app.store.roster import Roster
    from app.ui.screens import ViewState
    from tests.test_settings_flow import FakeDisplay, FakePlayer

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
    instance.keyring = Keyring(data)
    instance.roster = Roster(settings.contacts, data)
    instance.inbox = Inbox(data)
    instance.player = FakePlayer()
    instance.cues = cues_for(addr, name)
    instance.state = ViewState(address=addr, callsign=name)
    instance.display = FakeDisplay()
    instance.board = SimpleNamespace(foreground_ready=True)
    instance._wake = SimpleNamespace(set=lambda: None)
    instance._pending_pair = None
    instance._parents = {}
    instance._actions = WalkieApp._build_actions(instance)

    monkeypatch.setattr("serial.Serial", lambda *a, **k: module)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    instance.link = LoraLink(radio, duty_cycle_percent=100.0, callsign=name,
                             token=instance.overrides.node_token,
                             keyring=instance.keyring)
    instance.link.on_message(instance._on_radio_message)
    instance.link.on_hello(instance._on_hello)
    instance.link.on_clash(instance._on_clash)
    instance.link.start()
    instance._refresh_entries()
    instance._refresh_menus()
    return instance


def eventually(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


@pytest.fixture
def two_radios(tmp_path, monkeypatch):
    from tests.fakes import FakeModule

    left, right = FakeModule.pair()
    mengpi = over_the_air(tmp_path, monkeypatch, "MengPi", left, 1)
    jarvis = over_the_air(tmp_path, monkeypatch, "jarvis", right, 2)
    yield mengpi, jarvis
    mengpi.link.stop()
    jarvis.link.stop()


def pair_over_the_air(mengpi, jarvis):
    for radio in (mengpi, jarvis):
        open_pairing(radio)
        radio._pairing_tick()
    assert eventually(lambda: mengpi.state.pair_found and jarvis.state.pair_found)
    mengpi._pair_selected()
    assert eventually(lambda: jarvis._pending_pair is not None)
    jarvis._prompt_pending_pair()
    play(jarvis, TAP, HOLD)
    # The answer is handled on MengPi's receive thread; wait for all of it.
    assert eventually(lambda: mengpi.state.screen == CONTACTS)


def test_two_radios_pair_over_the_air(two_radios):
    mengpi, jarvis = two_radios
    pair_over_the_air(mengpi, jarvis)
    assert 2 in contacts(mengpi) and 1 in contacts(jarvis)
    assert jarvis.state.screen == CONTACTS
    assert mengpi.link.link_state(2) == protocol.LINK_LINKED
    assert jarvis.link.link_state(1) == protocol.LINK_LINKED
    assert mengpi.keyring.pairwise(2) == jarvis.keyring.pairwise(1)


def test_once_paired_they_talk_in_private(two_radios):
    mengpi, jarvis = two_radios
    pair_over_the_air(mengpi, jarvis)
    mengpi.link.send_text(2, "radio check")
    assert eventually(lambda: [i for i in jarvis.inbox.items if i.kind == "text"])
    assert jarvis.inbox.items[0].text == "radio check"
    assert all(b"radio check" not in f for f in mengpi.link.radio.ser.transmitted)


def test_two_radios_on_one_id_are_separated_by_pairing(tmp_path, monkeypatch):
    from tests.fakes import FakeModule

    left, right = FakeModule.pair()
    old = over_the_air(tmp_path, monkeypatch, "old", left, 5)
    new = over_the_air(tmp_path, monkeypatch, "new", right, 5)
    try:
        open_pairing(new)
        new._pairing_tick()                     # the old radio hears its own ID
        assert eventually(lambda: new.settings.radio.address != 5)
        assert old.settings.radio.address == 5  # the one not pairing stays put
        assert new.link.addr == new.settings.radio.address
    finally:
        old.link.stop()
        new.link.stop()


def test_radios_on_different_channels_pair_and_end_up_on_one(two_radios):
    """The asker joins the channel of the radio that said yes."""
    mengpi, jarvis = two_radios
    mengpi._apply_channel(2)
    assert mengpi.link.channel == 2 and jarvis.link.channel == 1
    pair_over_the_air(mengpi, jarvis)
    assert mengpi.settings.radio.privacy_channel == 1
    assert mengpi.link.channel == 1
    assert jarvis.settings.radio.privacy_channel == 1
    assert "channel 1" in mengpi.state.active_banner


def test_the_pair_list_shows_a_radio_on_another_channel(radio, jarvis):
    open_pairing(radio)
    body = protocol.pair_body(b"\x01\x02\x03\x04", jarvis.public, "jarvis")
    beacon_message = protocol.Message(
        type=protocol.PAIR, src=JARVIS, msg_id=0, body=body, flags=0, missing=[],
        rssi_dbm=-80, received_at=time.time(), dst=protocol.BROADCAST, channel=4)
    radio._on_radio_message(beacon_message, Peer(JARVIS, name="jarvis"))
    assert radio.state.pair_channels[JARVIS] == 4
