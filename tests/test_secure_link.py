"""The link with keys: who can read what, and what gets thrown away.

Alice and Bob are paired. Carol is on the same frequency and channel,
with keys of her own, but has paired with nobody -- she is the neighbour
with a LoRa module.
"""

from __future__ import annotations

import time

import pytest

from app.radio import crypto, protocol
from app.radio.framing import Deframer, encode_frame
from app.radio.link import LoraLink, NotPaired
from app.radio.sx126x import SX126x
from app.store.keyring import Keyring
from tests.fakes import FakeModule
from tests.test_link_end_to_end import Collector


def make_link(port, addr, name, keyring, monkeypatch):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    return LoraLink(radio, duty_cycle_percent=100.0, callsign=name, keyring=keyring)


def pair_up(one, other):
    one.keyring.add_peer(other.addr, other.keyring.public, other.keyring.broadcast_key)
    other.keyring.add_peer(one.addr, one.keyring.public, one.keyring.broadcast_key)


@pytest.fixture
def trio(tmp_path, monkeypatch):
    ports = FakeModule.network(3)
    links = [make_link(port, index + 1, name, Keyring(tmp_path / name), monkeypatch)
             for index, (port, name) in enumerate(zip(ports, ("Alice", "Bob", "Carol")))]
    pair_up(links[0], links[1])
    for link in links:
        link.start()
    yield links
    for link in links:
        link.stop()


def settle():
    time.sleep(0.4)


def test_a_paired_radio_reads_what_is_sent_to_it(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    alice.send_text(2, "meet at the gate")
    assert inbox.wait()[0][0].body == b"meet at the gate"


def test_the_words_never_go_on_the_air(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    alice.send_text(2, "meet at the gate")
    inbox.wait()
    assert all(b"meet" not in frame for frame in alice.radio.ser.transmitted)


def test_all_reaches_paired_radios_and_nobody_else(trio):
    alice, bob, carol = trio
    bob_inbox, carol_inbox = Collector(), Collector()
    bob.on_message(bob_inbox)
    carol.on_message(carol_inbox)
    alice.send_text(protocol.BROADCAST, "everyone")
    assert bob_inbox.wait()[0][0].body == b"everyone"
    settle()
    assert carol_inbox.messages == []
    assert carol.stats.no_key == 1


def test_voice_is_sealed_fragment_by_fragment(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    voice = bytes(range(256)) * 4
    alice.send_voice(2, voice, codec_mode=8)
    message, _peer = inbox.wait(timeout=15.0)[0]
    assert message.body == voice and message.complete
    assert len(alice.radio.ser.transmitted) == alice.plan(2, len(voice))[0]


def test_unsealed_content_is_refused(trio):
    """Carol, or anything, trying to talk to Bob past the encryption."""
    _alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    plain = protocol.encode(protocol.TEXT, 3, 0, 0, 1, b"let me in", dst=2)
    bob.radio.ser.inject(encode_frame(plain))
    settle()
    assert inbox.messages == []
    assert bob.stats.unsealed_refused == 1


def test_a_tampered_packet_is_dropped(trio):
    alice, bob, _carol = trio
    alice.radio.ser.peers.remove(bob.radio.ser)   # only the doctored copy arrives
    alice.send_text(2, "original")
    settle()
    inbox = Collector()
    bob.on_message(inbox)
    payload = Deframer().feed(alice.radio.ser.transmitted[0])[0][0]
    tampered = bytearray(payload)
    tampered[-1] ^= 1
    bob.radio.ser.inject(encode_frame(bytes(tampered)))
    settle()
    assert inbox.messages == []
    assert bob.stats.failed_to_open == 1


def test_a_recording_played_back_is_dropped(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    alice.send_text(2, "once")
    inbox.wait()
    bob.radio.ser.inject(alice.radio.ser.transmitted[0])
    settle()
    assert len(inbox.messages) == 1
    assert bob.stats.replays == 1


def test_a_handshake_between_paired_radios_links_them(trio):
    alice, bob, _carol = trio
    bob.on_hello(lambda peer, name: True)
    alice.send_hello(2)
    deadline = time.monotonic() + 3
    while alice.link_state(2) != protocol.LINK_LINKED and time.monotonic() < deadline:
        time.sleep(0.05)
    assert alice.link_state(2) == protocol.LINK_LINKED


def test_nothing_can_be_sealed_for_an_unpaired_radio(trio):
    alice, _bob, _carol = trio
    assert not alice.can_send(3)
    assert alice.can_send(2) and alice.can_send(protocol.BROADCAST)
    with pytest.raises(NotPaired):
        alice.send_text(3, "hello?")


def test_sealing_costs_what_the_plan_says(trio):
    alice, _bob, _carol = trio
    sealed_fragments, sealed_bytes = alice.plan(2, 1000)
    assert sealed_fragments == -(-1000 // (protocol.MAX_BODY - crypto.OVERHEAD))
    assert sealed_bytes > 1000 + sealed_fragments * crypto.OVERHEAD


def test_pairing_beacons_stay_readable_to_everyone(trio):
    """They are how two strangers find each other in the first place."""
    alice, _bob, carol = trio
    inbox = Collector()
    carol.on_message(inbox)
    alice.send_pair()
    message, peer = inbox.wait()[0]
    assert message.type == protocol.PAIR and peer.name == "Alice"
    assert protocol.parse_pair(message.body)[1] == alice.keyring.public
