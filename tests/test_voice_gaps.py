"""A voice message with a fragment missing still plays sensibly.

The link keeps a lost fragment's place as zero bytes, so later frames
stay in step; the app then writes encoded silence into that place. Before
either, a lost fragment shifted every later Codec2 frame and the rest of
the message played as noise -- the "crackle, can't understand it" report.
"""

from __future__ import annotations

import math
import struct
import time

import numpy as np
import pytest

from app.audio.codec2 import MODE_BY_NAME, SAMPLE_RATE, Codec2, available
from app.main import WalkieApp
from app.radio import protocol

pytestmark = pytest.mark.skipif(not available(), reason="libcodec2 not installed")

SIZE = protocol.VOICE_CHUNK


def speechlike(seconds: float) -> bytes:
    """A voiced, wobbling signal Codec2 encodes as sound, not silence."""
    out = bytearray()
    for n in range(int(seconds * SAMPLE_RATE)):
        t = n / SAMPLE_RATE
        pitch = 140 + 30 * math.sin(2 * math.pi * 3 * t)
        value = sum(math.sin(2 * math.pi * pitch * k * t) / k for k in range(1, 8))
        out += struct.pack("<h", int(4000 * value))
    return bytes(out)


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(float)
    return float(np.sqrt((samples ** 2).mean())) if samples.size else 0.0


@pytest.fixture
def codec():
    return Codec2(MODE_BY_NAME["700C"])


@pytest.fixture
def app(codec):
    instance = WalkieApp.__new__(WalkieApp)
    instance.codec = codec
    instance.codec_mode = codec.mode
    return instance


def received(encoded: bytes, missing: list, mode: int) -> protocol.Message:
    """What the link hands over when fragments in `missing` were lost."""
    body = bytearray(encoded)
    for seq in missing:
        body[seq * SIZE:(seq + 1) * SIZE] = bytes(len(body[seq * SIZE:(seq + 1) * SIZE]))
    return protocol.Message(type=protocol.VOICE, src=1, msg_id=0, body=bytes(body),
                            flags=mode, missing=missing, rssi_dbm=None,
                            received_at=time.time(), fragment_size=SIZE)


def test_the_gap_plays_as_silence_and_the_rest_is_untouched(app, codec):
    encoded = codec.encode(speechlike(8.0))          # 800 B: five fragments
    assert len(encoded) > 4 * SIZE
    message = app._silence_gaps(received(encoded, [1], codec.mode))

    assert len(message.body) == len(encoded)
    assert message.body[:SIZE] == encoded[:SIZE]
    assert message.body[2 * SIZE:] == encoded[2 * SIZE:]

    pcm = codec.decode(message.body)
    per_byte = len(pcm) // len(message.body)
    gap = pcm[(SIZE + 8) * per_byte:(2 * SIZE - 8) * per_byte]
    after = pcm[(2 * SIZE + 8) * per_byte:(3 * SIZE) * per_byte]
    assert rms(gap) < rms(after) / 10


def test_a_complete_message_is_left_alone(app, codec):
    message = received(codec.encode(speechlike(2.0)), [], codec.mode)
    assert app._silence_gaps(message) is message

