"""Packet framing over a raw UART byte stream.

The SX126X module is transparent: whatever bytes we write come out the
other end, but nothing marks where one transmission stops and the next
begins. Bursts get split across reads and concatenated in the driver
buffer, so the app carries its own framing:

    0xAA 0x55  |  len  |  payload  |  crc16-be
      SOF (2)     (1)     (len)       (2)

CRC-16/CCITT-FALSE covers the length byte and the payload, so a
corrupted length -- which would otherwise desynchronise the parser for
the rest of the session -- is caught like any other corruption.

**Why length-prefixed rather than a delimiter.** With RSSI reporting
enabled the module appends one raw byte after each received packet.
Under delimiter framing that byte is indistinguishable from the first
byte of the next frame until more data arrives, which forces the parser
to either hold each frame back (a message then sits undelivered until
the *next* transmission -- indistinguishable from a dead radio on a
quiet channel) or guess. A length prefix removes the ambiguity: the
parser knows exactly where the frame ends, so a byte following it that
is not a start-of-frame marker is unambiguously the RSSI report, and
every frame is delivered the instant its last byte arrives.
"""

from __future__ import annotations

CRC_INIT = 0xFFFF
CRC_POLY = 0x1021

SOF = b"\xAA\x55"
HEADER_SIZE = len(SOF) + 1
CRC_SIZE = 2
OVERHEAD = HEADER_SIZE + CRC_SIZE  # 5 bytes

# The module's hard packet ceiling is 240 bytes; 200 of payload plus
# overhead leaves comfortable headroom.
MAX_FRAME_PAYLOAD = 200


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE. Bitwise: payloads here are a few hundred bytes."""
    crc = CRC_INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ CRC_POLY) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(payload: bytes) -> bytes:
    """Wrap `payload` into one self-describing, CRC-protected frame."""
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError(
            f"payload {len(payload)} B exceeds frame limit {MAX_FRAME_PAYLOAD} B"
        )
    body = bytes([len(payload)]) + payload
    crc = crc16(body)
    return SOF + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def decode_frame(frame: bytes) -> bytes | None:
    """Unwrap one complete frame. None if it is malformed or corrupt."""
    if len(frame) < OVERHEAD or not frame.startswith(SOF):
        return None
    length = frame[2]
    if len(frame) != OVERHEAD + length:
        return None
    body = frame[2:3 + length]
    expected = (frame[3 + length] << 8) | frame[4 + length]
    return frame[3:3 + length] if crc16(body) == expected else None


class Deframer:
    """Incremental parser: feed arbitrary byte runs, get whole frames out.

    Returns a list of `(payload, rssi_byte)` per call. Convert the RSSI
    byte with `-(256 - rssi_byte)` dBm; it is None when the module did
    not report one.
    """

    # Bound the buffer so a stuck line or a peer transmitting noise
    # cannot grow it without limit while we wait for a frame to complete.
    MAX_BUFFER = 4096

    def __init__(self):
        self._buffer = bytearray()
        self.frames_ok = 0
        self.frames_bad = 0
        self.last_rssi_byte = None

    def feed(self, data: bytes) -> list:
        self._buffer.extend(data)
        if len(self._buffer) > self.MAX_BUFFER:
            del self._buffer[: len(self._buffer) - self.MAX_BUFFER]

        out = []
        while True:
            start = self._buffer.find(SOF)
            if start < 0:
                # Keep only a trailing partial SOF; everything before it
                # is inter-frame noise (the RSSI byte included).
                if len(self._buffer) > 1:
                    del self._buffer[: len(self._buffer) - 1]
                break
            if start:
                del self._buffer[:start]  # discard whatever preceded the frame

            if len(self._buffer) < HEADER_SIZE:
                break
            length = self._buffer[2]
            total = OVERHEAD + length
            if length > MAX_FRAME_PAYLOAD:
                # Not a real frame: a noise byte pair happened to match
                # the marker. Step past it and resynchronise.
                self.frames_bad += 1
                del self._buffer[:2]
                continue
            if len(self._buffer) < total:
                break  # frame still arriving

            payload = decode_frame(bytes(self._buffer[:total]))
            if payload is None:
                self.frames_bad += 1
                del self._buffer[:2]
                continue

            del self._buffer[:total]
            self.frames_ok += 1

            # The module appends its RSSI report immediately after the
            # packet. Anything sitting here that is not the next frame's
            # marker is that byte.
            rssi = None
            if self._buffer and not bytes(self._buffer[:2]).startswith(SOF[:1]):
                rssi = self._buffer[0]
                del self._buffer[0]
                self.last_rssi_byte = rssi
            out.append((payload, rssi))
        return out

    def drain(self) -> list:
        """No-op: frames are never held back. Kept so callers can be uniform."""
        return []
