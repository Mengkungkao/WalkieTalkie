"""Two radios, one fake channel: the whole stack, minus the hardware.

These are the tests that would have caught the real integration bugs --
the module eating three address bytes off the front of every packet, a
voice message spanning six fragments, and a dropped fragment having to
degrade into silence rather than into a crash.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.radio import protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x
from tests.fakes import FakeModule, LossyModule


def make_radio(port, addr, monkeypatch):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    return SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)


class Collector:
    """Captures delivered messages and lets a test wait for them."""

    def __init__(self):
        self.messages = []
        self.event = threading.Event()

    def __call__(self, message, peer):
        self.messages.append((message, peer))
        self.event.set()

    def wait(self, count=1, timeout=6.0):
        deadline = time.monotonic() + timeout
        while len(self.messages) < count and time.monotonic() < deadline:
            self.event.wait(0.1)
            self.event.clear()
        return self.messages


@pytest.fixture
def pair(monkeypatch):
    left_port, right_port = FakeModule.pair()
    alice = LoraLink(make_radio(left_port, 1, monkeypatch),
                     duty_cycle_percent=100.0, callsign="Alice")
    bob = LoraLink(make_radio(right_port, 2, monkeypatch),
                   duty_cycle_percent=100.0, callsign="Bob")
    alice.start()
    bob.start()
    yield alice, bob
    alice.stop()
    bob.stop()


def test_text_crosses_the_link(pair):
    alice, bob = pair
    inbox = Collector()
    bob.on_message(inbox)

    alice.send_text(protocol.BROADCAST, "radio check")
    messages = inbox.wait()

    assert len(messages) == 1
    message, peer = messages[0]
    assert message.body.decode() == "radio check"
    assert message.src == 1 and peer.addr == 1


def test_the_three_address_bytes_never_reach_the_receiver(pair):
    """Bytes 0-2 are addressing metadata the module eats before the air."""
    alice, bob = pair
    inbox = Collector()
    bob.on_message(inbox)

    alice.send_text(protocol.BROADCAST, "to everyone")
    messages = inbox.wait()

    # Written by the host: destination high, low, then the channel.
    assert alice.radio.ser.written[:3] == bytes([0xFF, 0xFF, 18])
    # Put on the air: the framed packet alone, with no address prefix.
    assert alice.radio.ser.transmitted[0][:3] != bytes([0xFF, 0xFF, 18])
    assert messages[0][0].body.decode() == "to everyone"


def test_multi_fragment_voice_survives_the_round_trip(pair):
    alice, bob = pair
    inbox = Collector()
    bob.on_message(inbox)

    voice = bytes(range(256)) * 4  # 1024 B, six fragments
    alice.send_voice(protocol.BROADCAST, voice, codec_mode=8)
    messages = inbox.wait(timeout=15.0)

    assert len(messages) == 1
    message, _ = messages[0]
    assert message.type == protocol.VOICE
    assert message.body == voice
    assert message.complete
    assert message.flags == 8  # the codec mode the receiver must decode with


def test_hello_teaches_the_receiver_a_name(pair):
    alice, bob = pair
    inbox = Collector()
    bob.on_message(inbox)

    alice.send_hello()
    messages = inbox.wait()
    assert messages[0][1].name == "Alice"
    assert bob.peers[1].present


def test_a_node_ignores_its_own_transmissions(monkeypatch):
    """A repeater echoing us back must not appear as an incoming message."""
    port = FakeModule(addr=1)
    port.peers.append(port)  # everything written comes straight back
    link = LoraLink(make_radio(port, 1, monkeypatch), duty_cycle_percent=100.0)
    inbox = Collector()
    link.on_message(inbox)
    link.start()
    try:
        link.send_text(protocol.BROADCAST, "echo")
        time.sleep(0.6)
        assert inbox.messages == []
    finally:
        link.stop()


def test_a_dropped_fragment_degrades_into_a_gap_not_a_crash(monkeypatch):
    """When the sender cannot fill it in, the gap keeps its place."""
    monkeypatch.setattr(protocol, "QUIET_SECONDS", 0.3)
    _left, right = FakeModule.pair()
    lossy = LossyModule("lossy", addr=1, drop_indices={1})  # lose packet 2
    lossy.peers = [right]
    right.peers = [lossy]

    alice = LoraLink(make_radio(lossy, 1, monkeypatch), duty_cycle_percent=100.0)
    bob = LoraLink(make_radio(right, 2, monkeypatch), duty_cycle_percent=100.0)
    alice._remember = lambda *_args: None      # nothing kept to resend from
    inbox = Collector()
    bob.on_message(inbox)
    alice.start()
    bob.start()
    try:
        voice = bytes(range(200)) * 3            # 600 B: four fragments
        alice.send_voice(protocol.BROADCAST, voice, codec_mode=8)
        messages = inbox.wait(timeout=15.0)
        assert messages, "an incomplete message must still be delivered"
        message, _ = messages[0]
        assert not message.complete
        assert message.missing == [1]
        size = protocol.VOICE_CHUNK
        assert message.fragment_size == size
        assert len(message.body) == len(voice)
        assert message.body[size:2 * size] == bytes(size)      # the gap
        assert message.body[2 * size:] == voice[2 * size:]    # still in place
    finally:
        alice.stop()
        bob.stop()


def test_duty_cycle_stops_a_flood(monkeypatch):
    port, _peer = FakeModule.pair()
    link = LoraLink(make_radio(port, 1, monkeypatch), air_speed=9600,
                    duty_cycle_percent=1.0)
    link.start()
    try:
        # 36 s of budget at ~0.25 s a packet is ~145 packets; queue well
        # past that so the governor has to refuse some.
        for _ in range(250):
            link.send_text(protocol.BROADCAST, "x" * 150)
        deadline = time.monotonic() + 20
        while link.pending() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert link.budget.used_seconds() <= link.budget.limit_seconds + 1.0
        assert "duty cycle full" in link.stats.errors
    finally:
        link.stop()
