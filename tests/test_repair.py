"""Getting a message through when fragments go missing.

At the edge of range the first thing to fail is not the whole message
but a fragment or two of it. Before this, a lost fragment shifted every
later codec frame out of step -- the rest decoded as noise -- and a lost
*last* fragment left the message waiting for something to wake the app.
Now the receiver asks for what is missing, the sender resends it, and
whatever cannot be recovered is delivered with the gap kept in place.
"""

from __future__ import annotations

import time

import pytest

from app.radio import protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x
from app.store.keyring import Keyring
from tests.fakes import FakeModule, LossyModule
from tests.test_link_end_to_end import Collector

VOICE = bytes(range(200)) * 3          # 600 B: four fragments


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Seconds of waiting, shortened so the tests take tenths."""
    monkeypatch.setattr(protocol, "QUIET_SECONDS", 0.3)
    monkeypatch.setattr(protocol, "REPAIR_WAIT", 0.6)
    monkeypatch.setattr(protocol, "REPAIR_WAIT_PER_FRAGMENT", 0.2)


def make_link(port, addr, monkeypatch, keyring=None):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    return LoraLink(radio, duty_cycle_percent=100.0, keyring=keyring)


def lossy_pair(monkeypatch, drop, keyrings=(None, None)):
    """Alice's transmissions lose the packets numbered in `drop`."""
    _left, right = FakeModule.pair()
    lossy = LossyModule("lossy", addr=1, drop_indices=set(drop))
    lossy.peers, right.peers = [right], [lossy]
    alice = make_link(lossy, 1, monkeypatch, keyrings[0])
    bob = make_link(right, 2, monkeypatch, keyrings[1])
    return alice, bob


def run(alice, bob):
    alice.start()
    bob.start()


def stop(*links):
    for link in links:
        link.stop()


def test_a_lost_fragment_is_asked_for_and_filled_in(monkeypatch):
    alice, bob = lossy_pair(monkeypatch, drop={1})
    inbox = Collector()
    bob.on_message(inbox)
    run(alice, bob)
    try:
        alice.send_voice(2, VOICE, codec_mode=8)
        message, _peer = inbox.wait(timeout=10.0)[0]
        assert message.complete and message.body == VOICE
        assert bob.stats.repairs_asked == 1
        assert alice.stats.fragments_resent == 1
    finally:
        stop(alice, bob)


def test_a_lost_last_fragment_is_still_delivered_unprompted(monkeypatch):
    """Nobody calls tick(): the link's own thread notices the silence."""
    alice, bob = lossy_pair(monkeypatch, drop={3})
    inbox = Collector()
    bob.on_message(inbox)
    run(alice, bob)
    try:
        alice.send_voice(2, VOICE, codec_mode=8)
        message, _peer = inbox.wait(timeout=10.0)[0]
        assert message.complete and message.body == VOICE
    finally:
        stop(alice, bob)


def test_what_cannot_be_recovered_arrives_with_its_gap(monkeypatch):
    """Every resend is lost too: after two rounds, deliver what came."""
    alice, bob = lossy_pair(monkeypatch, drop={1, 4, 5, 6, 7})
    inbox = Collector()
    bob.on_message(inbox)
    run(alice, bob)
    try:
        alice.send_voice(2, VOICE, codec_mode=8)
        message, _peer = inbox.wait(timeout=15.0)[0]
        assert message.missing == [1]
        assert len(message.body) == len(VOICE)
        assert bob.stats.repairs_asked == protocol.REPAIR_ROUNDS
        assert bob.stats.delivered_with_gaps == 1
    finally:
        stop(alice, bob)


def test_two_listeners_asking_for_one_fragment_get_one_resend(monkeypatch):
    modules = FakeModule.network(3)
    alice_port = LossyModule("lossy", addr=1, drop_indices={1})
    alice_port.peers = modules[1:]
    for module in modules[1:]:
        module.peers = [alice_port] + [m for m in modules[1:] if m is not module]
    alice = make_link(alice_port, 1, monkeypatch)
    bob = make_link(modules[1], 2, monkeypatch)
    carol = make_link(modules[2], 3, monkeypatch)
    bob_inbox, carol_inbox = Collector(), Collector()
    bob.on_message(bob_inbox)
    carol.on_message(carol_inbox)
    for link in (alice, bob, carol):
        link.start()
    try:
        alice.send_voice(protocol.BROADCAST, VOICE, codec_mode=8)
        assert bob_inbox.wait(timeout=10.0)[0][0].complete
        assert carol_inbox.wait(timeout=10.0)[0][0].complete
        assert alice.stats.fragments_resent == 1
    finally:
        stop(alice, bob, carol)


def test_a_request_for_someone_elses_message_is_ignored(monkeypatch):
    modules = FakeModule.network(3)
    alice, bob, carol = (make_link(m, i + 1, monkeypatch) for i, m in enumerate(modules))
    for link in (alice, bob, carol):
        link.start()
    try:
        msg_id = alice.send_voice(2, VOICE, codec_mode=8)   # to Bob only
        time.sleep(1.5)
        _id, packets = carol._fragments(protocol.REPAIR, 1, bytes([msg_id, 0, 1]))
        carol._enqueue(1, packets, "sneaky", report=False)
        time.sleep(0.8)
        assert alice.stats.fragments_resent == 0
    finally:
        stop(alice, bob, carol)


def test_repair_works_sealed(monkeypatch, tmp_path):
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(2, theirs.public, theirs.broadcast_key)
    theirs.add_peer(1, ours.public, ours.broadcast_key)
    alice, bob = lossy_pair(monkeypatch, drop={2}, keyrings=(ours, theirs))
    inbox = Collector()
    bob.on_message(inbox)
    run(alice, bob)
    try:
        alice.send_voice(protocol.BROADCAST, VOICE, codec_mode=8)
        message, _peer = inbox.wait(timeout=10.0)[0]
        assert message.complete and message.body == VOICE
        assert bob.stats.replays == 0
    finally:
        stop(alice, bob)


def test_voice_fragments_hold_whole_codec_frames(monkeypatch):
    """168 bytes each, sealed or not, so a gap never splits a frame."""
    port = FakeModule(addr=1)
    link = make_link(port, 1, monkeypatch)
    _id, packets = link._fragments(protocol.VOICE, 2, VOICE)
    sizes = [len(protocol.decode(p).body) for p in packets]
    assert sizes == [168, 168, 168, 96]
    for frame_bytes in (4, 6, 7, 8):              # every Codec2 mode
        assert protocol.VOICE_CHUNK % frame_bytes == 0
