"""Addressing in the packet header, not the module's registers.

Every packet now goes out as a module broadcast and receivers filter on
the header's destination themselves. That is what lets a Device ID set
from Settings -- or picked by pairing -- work at once, on a board that
cannot rewrite the module at all.
"""

from __future__ import annotations

import time

import pytest

from app.radio import protocol
from app.radio.framing import encode_frame
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x
from tests.fakes import FakeModule
from tests.test_link_end_to_end import Collector


def make_link(port, addr, monkeypatch, callsign="", token=None):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    return LoraLink(radio, duty_cycle_percent=100.0, callsign=callsign,
                    token=token)


@pytest.fixture
def trio(monkeypatch):
    ports = FakeModule.network(3)
    links = [make_link(port, index + 1, monkeypatch, callsign=name)
             for index, (port, name) in enumerate(zip(ports, ("Alice", "Bob", "Carol")))]
    for link in links:
        link.start()
    yield links
    for link in links:
        link.stop()


def test_a_message_reaches_only_its_destination(trio):
    alice, bob, carol = trio
    bob_inbox, carol_inbox = Collector(), Collector()
    bob.on_message(bob_inbox)
    carol.on_message(carol_inbox)

    alice.send_text(3, "for carol")
    assert carol_inbox.wait()[0][0].body == b"for carol"
    time.sleep(0.3)
    assert bob_inbox.messages == []
    # Overheard, not delivered: it still proves Alice is on the air.
    assert bob.stats.overheard == 1
    assert bob.peers[1].present


def test_everything_leaves_as_a_module_broadcast(trio):
    alice, _bob, carol = trio
    inbox = Collector()
    carol.on_message(inbox)
    alice.send_text(3, "x")
    inbox.wait()
    assert alice.radio.ser.written[:2] == b"\xff\xff"


def test_a_new_device_id_works_without_touching_the_module(trio):
    """The module still holds address 2; the radio answers to 4242."""
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    bob.set_address(4242)

    alice.send_text(4242, "new number")
    message, _peer = inbox.wait()[0]
    assert message.body == b"new number"
    assert bob.radio.addr == 2


def test_the_new_id_is_what_others_see(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    alice.on_message(inbox)
    bob.set_address(4242)
    bob.send_text(1, "hi")
    assert inbox.wait()[0][0].src == 4242


def test_a_twin_beaconing_our_id_is_reported(trio):
    alice, bob, _carol = trio
    clashes = []
    bob.on_clash(clashes.append)
    alice.set_address(bob.addr)            # the two radios now share an ID

    alice.send_pair()
    deadline = time.monotonic() + 3
    while not clashes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert clashes == ["Alice"]
    assert bob.stats.address_clashes == 1


def test_our_own_beacon_echoed_back_is_not_a_clash(monkeypatch):
    port = FakeModule(addr=1)
    port.peers.append(port)                # everything written comes back
    link = make_link(port, 1, monkeypatch, callsign="Echo")
    clashes = []
    link.on_clash(clashes.append)
    link.start()
    try:
        link.send_pair()
        time.sleep(0.6)
        assert clashes == []
        assert link.stats.self_addressed_drops == 1
    finally:
        link.stop()


def test_a_pair_beacon_carries_the_name(trio):
    alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    alice.send_pair()
    message, peer = inbox.wait()[0]
    assert message.type == protocol.PAIR
    assert peer.name == "Alice"
    assert protocol.parse_pair(message.body) == (alice.token, "Alice")


def test_a_radio_not_yet_updated_is_still_heard(trio):
    """Version 1 had no destination; its module filtered for it."""
    _alice, bob, _carol = trio
    inbox = Collector()
    bob.on_message(inbox)
    old = protocol.V1_HEADER.pack((1 << 4) | protocol.TEXT, 7, 0, 0, 1, 0) + b"v1"
    bob.radio.ser.inject(encode_frame(old))
    message, _peer = inbox.wait()[0]
    assert (message.src, message.body, message.dst) == (7, b"v1", None)


def test_handshakes_and_beacons_do_not_drive_the_progress_bar(trio):
    """The "sent" cue is for messages the operator sent, not for plumbing."""
    alice, bob, _carol = trio
    progress = []
    alice.on_tx_progress(lambda sent, total: progress.append((sent, total)))
    inbox = Collector()
    bob.on_message(inbox)

    alice.send_pair()
    alice.send_hello(2)
    alice.send_text(2, "real")
    inbox.wait(count=3)
    assert progress == [(1, 1)]
