"""Framing has to survive a byte stream that lies about packet edges."""

import pytest

from app.radio.framing import (Deframer, MAX_FRAME_PAYLOAD, OVERHEAD, SOF,
                               crc16, decode_frame, encode_frame)


@pytest.mark.parametrize("payload", [
    b"", b"\x00", b"\x00" * 200, b"\xff" * 200, b"hello\x00world",
    bytes(range(200)), SOF * 20,  # a payload that contains the marker
])
def test_frame_round_trip_for_any_payload(payload):
    frame = encode_frame(payload)
    assert len(frame) == len(payload) + OVERHEAD
    assert decode_frame(frame) == payload


def test_crc_detects_a_single_flipped_bit():
    payload = b"the quick brown fox"
    corrupted = bytearray(payload)
    corrupted[5] ^= 0x01
    assert crc16(payload) != crc16(bytes(corrupted))


def test_corrupt_frame_is_rejected_not_returned():
    frame = bytearray(encode_frame(b"payload"))
    frame[4] ^= 0xFF
    assert decode_frame(bytes(frame)) is None


def test_a_corrupted_length_byte_is_caught_by_the_crc():
    """A bad length would desynchronise the parser for the whole session."""
    frame = bytearray(encode_frame(b"payload"))
    frame[2] = 99
    assert decode_frame(bytes(frame)) is None


def test_oversized_payload_refused():
    with pytest.raises(ValueError):
        encode_frame(b"x" * (MAX_FRAME_PAYLOAD + 1))


def test_stream_split_across_arbitrary_reads():
    """The UART hands us whatever it has, not whole packets."""
    stream = b"".join(encode_frame(f"msg{i}".encode()) for i in range(6))
    deframer = Deframer()
    out = []
    for index in range(0, len(stream), 3):  # deliberately ragged chunks
        out += [payload for payload, _ in deframer.feed(stream[index:index + 3])]
    assert out == [f"msg{i}".encode() for i in range(6)]


def test_trailing_rssi_byte_is_captured_not_treated_as_a_frame():
    """The module appends one RSSI byte after each received packet."""
    stream = (encode_frame(b"first") + bytes([0xA5])
              + encode_frame(b"second") + bytes([0xA6]))
    deframer = Deframer()
    results = deframer.feed(stream)
    assert [payload for payload, _ in results] == [b"first", b"second"]
    assert [rssi for _, rssi in results] == [0xA5, 0xA6]


def test_a_frame_is_delivered_without_waiting_for_the_next_one():
    """A quiet channel must not leave a received message undelivered."""
    deframer = Deframer()
    results = deframer.feed(encode_frame(b"lonely") + bytes([0xA5]))
    assert [payload for payload, _ in results] == [b"lonely"]
    assert results[0][1] == 0xA5


def test_garbage_between_frames_does_not_lose_the_next_frame():
    stream = b"\x11\x22\x33\xAA\x99" + encode_frame(b"survivor")
    deframer = Deframer()
    out = list(deframer.feed(stream)) + list(deframer.drain())
    assert [payload for payload, _ in out] == [b"survivor"]


def test_buffer_cannot_grow_without_bound():
    deframer = Deframer()
    list(deframer.feed(b"\x01" * 100_000))  # never a delimiter
    assert len(deframer._buffer) <= Deframer.MAX_BUFFER
