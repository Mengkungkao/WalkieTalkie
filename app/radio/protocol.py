"""Application protocol carried inside each framed radio packet.

Layered on top of `framing`:

    [FF FF chan]  <- eaten by the module: always a module broadcast
    [ COBS( header(9) || body || crc16 ) 0x00 ]

The 9-byte header carries the sender's address, because in fixed
transmission mode the module tells the receiver nothing about who sent
a packet. The stock Waveshare example solves this by prefixing three
plaintext bytes to the payload; we fold the same information into the
framed header instead, so a source address containing 0x00 cannot be
mistaken for a frame delimiter.

It also carries the destination, which is version 2's one change.
Version 1 left that to the module: each module dropped packets not
addressed to the number in its own registers. Those registers can only
be written with the mode pins -- which the Whisplay LCD owns, and which
an Orange Pi cannot drive at all -- so a Device ID changed in Settings
never took effect, and pairing could not assign one. Now every packet
goes out as a module broadcast, every radio on the channel hears it,
and `Packet.addressed_to` does the filtering in software. The module's
own address no longer matters. (That was never privacy: any module on
the channel can already listen to everything.)

Messages larger than one packet are split into fragments sharing a
`msg_id`, numbered `seq` of `total`. There are no retransmissions on the
voice path: at 700 bps a redelivery costs more airtime than the gap it
fills, so `Reassembler` substitutes silence for anything missing and
plays on. Text is short enough to be acknowledged and is retried.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from app.radio.framing import MAX_FRAME_PAYLOAD

VERSION = 2
HEADER = struct.Struct(">BHHBBBB")  # ver_type, src, dst, msg_id, seq, total, flags
HEADER_SIZE = HEADER.size  # 9
MAX_BODY = MAX_FRAME_PAYLOAD - HEADER_SIZE  # 191

# Version 1 had no destination: the module's address register filtered
# for it. Still decoded, so a radio that has not been updated yet is
# heard rather than silently ignored -- though it cannot hear us.
V1_HEADER = struct.Struct(">BHBBBB")  # ver_type, src, msg_id, seq, total, flags

# Message types
HELLO = 0x0  # "I am here" -- body is the operator's display name
TEXT = 0x1  # UTF-8 text
VOICE = 0x2  # codec2 bitstream
ACK = 0x3  # body is the acknowledged msg_id
BYE = 0x4  # leaving the channel
HELLO_ACK = 0x5  # "I hear you, and I accept" -- body is our name
REJECT = 0x6  # "I hear you, and I do not accept"
PAIR = 0x7  # "I am pairing" -- body is a node token, then the name

TYPE_NAMES = {HELLO: "hello", TEXT: "text", VOICE: "voice", ACK: "ack",
              BYE: "bye", HELLO_ACK: "hello-ack", REJECT: "reject",
              PAIR: "pair"}

# A random number each installation picks once. Two radios that ended up
# with the same Device ID are indistinguishable by address alone -- each
# drops the other's packets as its own echo -- but not by token, so the
# pairing beacon carries one and a clash can be seen and fixed.
TOKEN_SIZE = 4

# A station is "linked" once it has both heard us and answered. Presence
# alone is not enough: hearing someone does not prove they hear you, and
# a one-way link is the classic radio failure -- you talk for a minute
# before discovering nobody received a word.
LINK_UNLINKED = "unlinked"
LINK_CALLING = "calling"
LINK_LINKED = "linked"
LINK_STALE = "stale"
LINK_REJECTED = "rejected"

# How long a completed handshake stays good without hearing anything.
#
# Long, deliberately. A walkie-talkie on standby is silent for most of
# its life, and treating silence as disconnection made the Talk screen
# say "not connected" about a station that was sitting right there and
# perfectly reachable. A handshake proved the link once; only evidence
# should retract that, not the absence of conversation.
#
# Past this the station is "stale" -- still linked, but worth re-checking
# and worth telling the operator when it was last heard.
LINK_TIMEOUT = 60 * 60

BROADCAST = 0xFFFF

# How long a partial message is held open before we give up on the rest.
REASSEMBLY_TIMEOUT = 30.0


@dataclass
class Packet:
    """One fragment, decoded."""

    type: int
    src: int
    msg_id: int
    seq: int
    total: int
    flags: int
    body: bytes
    rssi_dbm: int | None = None
    # None for a version-1 packet, whose destination only its sender's
    # module knew -- ours delivered it, so it was meant for us.
    dst: int | None = BROADCAST

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:x}")

    def addressed_to(self, addr: int) -> bool:
        return self.dst is None or self.dst in (addr, BROADCAST)


def encode(type_: int, src: int, msg_id: int, seq: int, total: int,
           body: bytes, flags: int = 0, dst: int = BROADCAST) -> bytes:
    if len(body) > MAX_BODY:
        raise ValueError(f"body {len(body)} B exceeds {MAX_BODY} B per fragment")
    head = HEADER.pack(
        ((VERSION & 0xF) << 4) | (type_ & 0xF),
        src & 0xFFFF, dst & 0xFFFF, msg_id & 0xFF, seq & 0xFF, total & 0xFF,
        flags & 0xFF,
    )
    return head + body


def decode(payload: bytes, rssi_dbm: int | None = None) -> Packet | None:
    """Parse one framed payload. None if it is not a packet we understand."""
    if not payload:
        return None
    version = payload[0] >> 4
    if version == VERSION and len(payload) >= HEADER_SIZE:
        ver_type, src, dst, msg_id, seq, total, flags = HEADER.unpack_from(payload)
        body = payload[HEADER_SIZE:]
    elif version == 1 and len(payload) >= V1_HEADER.size:
        ver_type, src, msg_id, seq, total, flags = V1_HEADER.unpack_from(payload)
        dst, body = None, payload[V1_HEADER.size:]
    else:
        return None  # a future or foreign sender; ignore rather than guess
    if total == 0 or seq >= total:
        return None
    return Packet(
        type=ver_type & 0xF, src=src, msg_id=msg_id, seq=seq, total=total,
        flags=flags, body=body, rssi_dbm=rssi_dbm, dst=dst,
    )


def fragment(type_: int, src: int, msg_id: int, body: bytes,
             flags: int = 0, dst: int = BROADCAST) -> list[bytes]:
    """Split `body` into ready-to-frame packets."""
    chunks = [body[i:i + MAX_BODY] for i in range(0, len(body), MAX_BODY)] or [b""]
    total = len(chunks)
    if total > 255:
        raise ValueError(f"message needs {total} fragments; limit is 255")
    return [
        encode(type_, src, msg_id, seq, total, chunk, flags, dst)
        for seq, chunk in enumerate(chunks)
    ]


def pair_body(token: bytes, name: str) -> bytes:
    """What a pairing beacon carries: our token, then our name."""
    token = bytes(token[:TOKEN_SIZE]).ljust(TOKEN_SIZE, b"\0")
    return token + name.encode("utf-8")[: MAX_BODY - TOKEN_SIZE]


def parse_pair(body: bytes) -> tuple:
    """(token, name) from a pairing beacon's body."""
    name = body[TOKEN_SIZE:].decode("utf-8", "replace").strip()[:20]
    return bytes(body[:TOKEN_SIZE]), name


@dataclass
class _Partial:
    total: int
    flags: int
    src: int
    type: int
    started: float
    dst: int | None = BROADCAST
    best_rssi: int | None = None
    chunks: dict = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return len(self.chunks) == self.total

    @property
    def missing(self) -> list:
        return [i for i in range(self.total) if i not in self.chunks]


@dataclass
class Message:
    """A reassembled message handed to the app."""

    type: int
    src: int
    msg_id: int
    body: bytes
    flags: int
    missing: list
    rssi_dbm: int | None
    received_at: float
    dst: int | None = BROADCAST

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:x}")


class Reassembler:
    """Collects fragments into whole messages.

    Single-fragment messages (every text, every hello) complete on the
    spot and never allocate. Multi-fragment voice is held until the last
    fragment lands or the sender goes quiet for `timeout` seconds -- at
    which point we emit what we have, with `missing` naming the gaps so
    the player can substitute silence of the right duration.
    """

    def __init__(self, timeout: float = REASSEMBLY_TIMEOUT):
        self.timeout = timeout
        self._partials: dict = {}

    def push(self, packet: Packet) -> Message | None:
        if packet.total == 1:
            return Message(
                type=packet.type, src=packet.src, msg_id=packet.msg_id,
                body=packet.body, flags=packet.flags, missing=[],
                rssi_dbm=packet.rssi_dbm, received_at=time.time(),
                dst=packet.dst,
            )

        key = (packet.src, packet.msg_id, packet.type)
        partial = self._partials.get(key)
        if partial is None or partial.total != packet.total:
            partial = _Partial(
                total=packet.total, flags=packet.flags, src=packet.src,
                type=packet.type, started=time.monotonic(), dst=packet.dst,
            )
            self._partials[key] = partial

        partial.chunks[packet.seq] = packet.body
        if packet.rssi_dbm is not None:
            if partial.best_rssi is None or packet.rssi_dbm > partial.best_rssi:
                partial.best_rssi = packet.rssi_dbm

        if partial.complete:
            del self._partials[key]
            return self._build(key, partial)
        return None

    @property
    def pending(self) -> int:
        """Partial messages still open. Zero means the app can sleep deeply."""
        return len(self._partials)

    def expire(self) -> list:
        """Emit partial messages whose sender has gone quiet. Call on a tick."""
        now = time.monotonic()
        done = []
        for key, partial in list(self._partials.items()):
            if now - partial.started >= self.timeout:
                del self._partials[key]
                done.append(self._build(key, partial))
        return done

    def _build(self, key, partial: _Partial) -> Message:
        body = b"".join(partial.chunks.get(i, b"") for i in range(partial.total))
        return Message(
            type=partial.type, src=partial.src, msg_id=key[1], body=body,
            flags=partial.flags, missing=partial.missing,
            rssi_dbm=partial.best_rssi, received_at=time.time(),
            dst=partial.dst,
        )
