"""Fragmentation, reassembly, and what happens when packets go missing."""

from app.radio import protocol
from app.radio.protocol import (MAX_BODY, Reassembler, TEXT, VOICE, decode,
                                encode, fragment)


def test_header_round_trip():
    packet = decode(encode(TEXT, 1234, 7, 0, 1, b"hi"))
    assert (packet.type, packet.src, packet.msg_id, packet.body) == (TEXT, 1234, 7, b"hi")


def test_foreign_version_is_ignored():
    raw = bytearray(encode(TEXT, 1, 0, 0, 1, b"x"))
    raw[0] = (9 << 4) | TEXT
    assert decode(bytes(raw)) is None


def test_nonsense_fragment_numbering_is_rejected():
    assert decode(encode(TEXT, 1, 0, 5, 3, b"x")) is None  # seq >= total
    assert decode(encode(TEXT, 1, 0, 0, 0, b"x")) is None  # total == 0


def test_single_fragment_message_completes_immediately():
    reassembler = Reassembler()
    message = reassembler.push(decode(fragment(TEXT, 1, 0, b"short")[0]))
    assert message.complete and message.body == b"short"
    assert reassembler.pending == 0


def test_multi_fragment_voice_reassembles_in_order():
    body = bytes(range(256)) * 4
    packets = fragment(VOICE, 42, 3, body, flags=8)
    assert len(packets) == 6  # 1024 B over a 193 B body limit
    reassembler = Reassembler()
    result = None
    for packet in packets:
        result = reassembler.push(decode(packet)) or result
    assert result.body == body and result.complete and result.flags == 8


def test_out_of_order_delivery_still_reassembles():
    body = b"a" * MAX_BODY + b"b" * MAX_BODY + b"c" * 10
    packets = fragment(VOICE, 42, 4, body)
    reassembler = Reassembler()
    result = None
    for packet in reversed(packets):
        result = reassembler.push(decode(packet)) or result
    assert result.body == body


def test_missing_fragment_is_reported_rather_than_silently_dropped():
    """Voice is never retransmitted, so gaps must be visible to the player."""
    body = b"x" * (MAX_BODY * 3)
    packets = fragment(VOICE, 42, 5, body)
    reassembler = Reassembler(timeout=0.0)
    for packet in packets[:1] + packets[2:]:
        reassembler.push(decode(packet))
    expired = reassembler.expire()
    assert len(expired) == 1
    assert expired[0].missing == [1] and not expired[0].complete


def test_two_senders_do_not_interleave():
    body = b"y" * (MAX_BODY + 5)
    a = fragment(TEXT, 10, 1, body)
    b = fragment(TEXT, 20, 1, body)
    reassembler = Reassembler()
    results = [reassembler.push(decode(p)) for p in (a[0], b[0], a[1], b[1])]
    done = [r for r in results if r]
    assert {m.src for m in done} == {10, 20}
    assert all(m.complete for m in done)


def test_best_rssi_is_kept_across_fragments():
    packets = fragment(VOICE, 7, 2, b"z" * (MAX_BODY + 1))
    reassembler = Reassembler()
    reassembler.push(decode(packets[0], rssi_dbm=-110))
    message = reassembler.push(decode(packets[1], rssi_dbm=-80))
    assert message.rssi_dbm == -80
