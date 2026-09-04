"""Connecting two radios, and carrying speech between them.

Hearing a station does not prove it hears you. A one-way link is the
classic radio failure -- you talk for a minute before discovering nobody
received a word -- so a station counts as connected only once it has
answered.
"""

from __future__ import annotations

import math
import struct
import threading
import time

import pytest

from app.audio.codec2 import SAMPLE_RATE, Codec2, MODE_BY_NAME, available
from app.radio import protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x
from tests.fakes import FakeModule


def make_radio(port, addr, monkeypatch):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    return SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)


@pytest.fixture
def pair(monkeypatch):
    left, right = FakeModule.pair()
    left.addr, right.addr = 1, 2
    a = LoraLink(make_radio(left, 1, monkeypatch), duty_cycle_percent=100.0,
                 callsign="MengPi")
    b = LoraLink(make_radio(right, 2, monkeypatch), duty_cycle_percent=100.0,
                 callsign="jarvis")
    a.start(); b.start()
    yield a, b
    a.stop(); b.stop()


def settle(seconds=2.5):
    time.sleep(seconds)


# --- handshake ---------------------------------------------------------
def test_a_station_starts_unlinked(pair):
    a, _b = pair
    assert a.link_state(2) == protocol.LINK_UNLINKED


def test_hello_and_answer_links_both_ends(pair):
    a, b = pair
    b.on_hello(lambda peer, name: True)
    a.send_hello(2)
    settle()
    assert a.link_state(2) == protocol.LINK_LINKED
    assert b.link_state(1) == protocol.LINK_LINKED


def test_the_handshake_exchanges_names(pair):
    a, b = pair
    b.on_hello(lambda peer, name: True)
    a.send_hello(2)
    settle()
    assert a.peers[2].name == "jarvis"
    assert b.peers[1].name == "MengPi"


def test_a_refused_handshake_is_reported_not_silent(pair):
    a, b = pair
    b.on_hello(lambda peer, name: False)
    a.send_hello(2)
    settle()
    assert a.link_state(2) == protocol.LINK_REJECTED
    assert b.link_state(1) == protocol.LINK_UNLINKED


def test_a_deferred_handshake_stays_pending_until_answered(pair):
    """An unknown station waits for the operator rather than auto-linking."""
    a, b = pair
    b.on_hello(lambda peer, name: None)
    a.send_hello(2)
    settle(1.5)
    assert a.link_state(2) != protocol.LINK_LINKED

    b.accept(1)                       # the operator agreed
    settle()
    assert a.link_state(2) == protocol.LINK_LINKED


def test_the_operator_can_refuse_a_deferred_handshake(pair):
    a, b = pair
    b.on_hello(lambda peer, name: None)
    a.send_hello(2)
    settle(1.5)
    b.refuse(1)
    settle()
    assert a.link_state(2) == protocol.LINK_REJECTED


def test_calling_is_visible_while_waiting(pair):
    a, b = pair
    b.on_hello(lambda peer, name: None)
    a.send_hello(2)
    assert a.link_state(2) == protocol.LINK_CALLING


def test_broadcast_needs_no_handshake(pair):
    a, _b = pair
    assert a.link_state(protocol.BROADCAST) == protocol.LINK_LINKED


def test_a_link_goes_stale_when_the_station_falls_silent(pair):
    a, b = pair
    b.on_hello(lambda peer, name: True)
    a.send_hello(2)
    settle()
    assert a.link_state(2) == protocol.LINK_LINKED
    a.peers[2].last_heard = time.time() - protocol.LINK_TIMEOUT - 1
    assert a.link_state(2) == protocol.LINK_STALE


# --- push to talk, all the way across ----------------------------------
def speech(seconds=2.0):
    """A vowel-ish tone stack -- something Codec2 can actually model."""
    out = bytearray()
    for t in range(int(seconds * SAMPLE_RATE)):
        value = (math.sin(2 * math.pi * 140 * t / SAMPLE_RATE)
                 + 0.5 * math.sin(2 * math.pi * 700 * t / SAMPLE_RATE))
        out += struct.pack("<h", int(value * 8000))
    return bytes(out)


@pytest.mark.skipif(not available(), reason="libcodec2 not installed")
def test_a_held_button_becomes_audio_at_the_other_end(pair):
    """The whole path: PCM in one radio, the same speech out of the other."""
    a, b = pair
    b.on_hello(lambda peer, name: True)
    a.send_hello(2)
    settle(1.5)

    received = []
    done = threading.Event()

    def on_message(message, peer):
        if message.type == protocol.VOICE:
            received.append(message)
            done.set()

    b.on_message(on_message)

    codec = Codec2(MODE_BY_NAME["700C"])
    pcm = speech(2.0)
    encoded = codec.encode(pcm)
    a.send_voice(2, encoded, codec.mode)

    assert done.wait(25), "voice never arrived"
    message = received[0]
    assert message.complete, f"missing fragments: {message.missing}"
    assert message.body == encoded
    assert message.flags == codec.mode

    decoded = codec.decode(message.body)
    assert abs(len(decoded) - len(pcm)) <= codec.samples_per_frame * 2
    codec.close()


@pytest.mark.skipif(not available(), reason="libcodec2 not installed")
def test_voice_fits_the_packets_the_duty_cycle_allows(pair):
    """A ten-second press must stay within a sane number of packets."""
    a, _b = pair
    codec = Codec2(MODE_BY_NAME["700C"])
    encoded = codec.encode(speech(10.0))
    packets = protocol.fragment(protocol.VOICE, 1, 0, encoded, flags=codec.mode)
    assert len(packets) <= 7, f"10 s of speech took {len(packets)} packets"
    airtime = a.budget.estimate_message(len(encoded) + len(packets) * 12)
    assert airtime < 2.5, f"10 s of speech costs {airtime:.1f}s of air"
    codec.close()
