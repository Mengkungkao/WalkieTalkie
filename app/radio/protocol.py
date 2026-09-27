"""Application protocol carried inside each framed radio packet.

Layered on top of `framing`:

    [FF FF chan]  <- eaten by the module: always a module broadcast
    [ COBS( header(10) || body || crc16 ) 0x00 ]

    header: ver+type | channel+sealing | src(2) | dst(2) | msg_id | seq | total | flags

The header carries the sender's address, because in fixed transmission
mode the module tells the receiver nothing about who sent a packet, and
the destination, because the module's own address filter is not used:
every packet goes out as a module broadcast, every radio hears it, and
`Packet.addressed_to` does the filtering in software. That is what lets
a Device ID be set in the app -- the module's registers can only be
rewritten with the mode pins, which the Whisplay LCD owns on a Pi and an
Orange Pi cannot drive at all.

Version 3 adds two things to the second byte:

* **A privacy channel** (low six bits). Like the privacy codes on a
  handheld walkie-talkie, radios set to different channels share the
  frequency but ignore each other completely.
* **How the body is sealed** (top two bits): in the clear, with a
  pairwise key, or with the sender's broadcast key; see `crypto`. Only
  the messages that agree keys may travel in the clear.

Every radio has to run the same version: an older radio's packets are
not understood, and it does not understand ours.

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

VERSION = 3
HEADER = struct.Struct(">BBHHBBBB")  # ver_type, chan_seal, src, dst, msg_id, seq, total, flags
HEADER_SIZE = HEADER.size  # 10
MAX_BODY = MAX_FRAME_PAYLOAD - HEADER_SIZE  # 190

# How a body is sealed: the top two bits of the second header byte.
CLEAR = 0
PAIRWISE = 1  # with the key only the sender and the destination hold
SENDER = 2  # with the sender's broadcast key, held by everyone it paired with

# The privacy channel: the low six bits of the same byte.
DEFAULT_CHANNEL = 1
CHANNELS = range(1, 17)  # what Settings offers
_CHANNEL_MASK = 0x3F

# Message types
HELLO = 0x0  # "I am here" -- body is the operator's display name
TEXT = 0x1  # UTF-8 text
VOICE = 0x2  # codec2 bitstream
ACK = 0x3  # body is the acknowledged msg_id
BYE = 0x4  # leaving the channel
HELLO_ACK = 0x5  # "I hear you, and I accept" -- body is our name
REJECT = 0x6  # "I hear you, and I do not accept"
PAIR = 0x7  # "I am pairing" -- token, public key, name
PAIR_REQUEST = 0x8  # "pair with me" -- public key, then our secrets sealed for you
PAIR_ACCEPT = 0x9  # "yes" -- the same, back

TYPE_NAMES = {HELLO: "hello", TEXT: "text", VOICE: "voice", ACK: "ack",
              BYE: "bye", HELLO_ACK: "hello-ack", REJECT: "reject",
              PAIR: "pair", PAIR_REQUEST: "pair-request",
              PAIR_ACCEPT: "pair-accept"}

# The only types that may travel in the clear: they are how two radios
# agree keys in the first place, or say no. Everything else from a radio
# we have not paired with is dropped unread.
CLEAR_TYPES = frozenset({PAIR, PAIR_REQUEST, PAIR_ACCEPT, REJECT})

# A random number each installation picks once. Two radios that ended up
# with the same Device ID are indistinguishable by address alone -- each
# drops the other's packets as its own echo -- but not by token, so the
# pairing beacon carries one and a clash can be seen and fixed.
TOKEN_SIZE = 4
PUBLIC_SIZE = 32

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
    dst: int = BROADCAST
    channel: int = DEFAULT_CHANNEL
    sealing: int = CLEAR
    # The header exactly as received: a sealed body is authenticated
    # against it, so it cannot be rebuilt from the fields.
    header: bytes = b""

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:x}")

    def addressed_to(self, addr: int) -> bool:
        return self.dst in (addr, BROADCAST)


def encode_header(type_: int, src: int, msg_id: int, seq: int, total: int,
                  flags: int = 0, dst: int = BROADCAST,
                  channel: int = DEFAULT_CHANNEL, sealing: int = CLEAR) -> bytes:
    return HEADER.pack(
        ((VERSION & 0xF) << 4) | (type_ & 0xF),
        ((sealing & 0x3) << 6) | (channel & _CHANNEL_MASK),
        src & 0xFFFF, dst & 0xFFFF, msg_id & 0xFF, seq & 0xFF, total & 0xFF,
        flags & 0xFF,
    )


def encode(type_: int, src: int, msg_id: int, seq: int, total: int,
           body: bytes, flags: int = 0, dst: int = BROADCAST,
           channel: int = DEFAULT_CHANNEL, sealing: int = CLEAR) -> bytes:
    if len(body) > MAX_BODY:
        raise ValueError(f"body {len(body)} B exceeds {MAX_BODY} B per fragment")
    return encode_header(type_, src, msg_id, seq, total, flags, dst,
                         channel, sealing) + body


def decode(payload: bytes, rssi_dbm: int | None = None) -> Packet | None:
    """Parse one framed payload. None if it is not a packet we understand."""
    if len(payload) < HEADER_SIZE or (payload[0] >> 4) != VERSION:
        return None  # an older, future or foreign sender; ignore rather than guess
    ver_type, chan_seal, src, dst, msg_id, seq, total, flags = HEADER.unpack_from(payload)
    if total == 0 or seq >= total:
        return None
    return Packet(
        type=ver_type & 0xF, src=src, msg_id=msg_id, seq=seq, total=total,
        flags=flags, body=payload[HEADER_SIZE:], rssi_dbm=rssi_dbm, dst=dst,
        channel=chan_seal & _CHANNEL_MASK, sealing=chan_seal >> 6,
        header=bytes(payload[:HEADER_SIZE]),
    )


def fragment(type_: int, src: int, msg_id: int, body: bytes,
             flags: int = 0, dst: int = BROADCAST,
             channel: int = DEFAULT_CHANNEL, seal=None, sealing: int = CLEAR,
             overhead: int = 0) -> list[bytes]:
    """Split `body` into ready-to-frame packets.

    With `seal` -- a function (header, chunk) -> sealed body adding
    `overhead` bytes -- each fragment is sealed on its own, so a lost
    fragment never makes the others unreadable.
    """
    size = MAX_BODY - overhead
    chunks = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
    total = len(chunks)
    if total > 255:
        raise ValueError(f"message needs {total} fragments; limit is 255")
    packets = []
    for seq, chunk in enumerate(chunks):
        header = encode_header(type_, src, msg_id, seq, total, flags, dst,
                               channel, sealing)
        packets.append(header + (seal(header, chunk) if seal else chunk))
    return packets


def pair_body(token: bytes, public: bytes, name: str) -> bytes:
    """What a pairing beacon carries: token, public key, then name."""
    token = bytes(token[:TOKEN_SIZE]).ljust(TOKEN_SIZE, b"\0")
    public = bytes(public[:PUBLIC_SIZE]).ljust(PUBLIC_SIZE, b"\0")
    limit = MAX_BODY - TOKEN_SIZE - PUBLIC_SIZE
    return token + public + name.encode("utf-8")[:limit]


def parse_pair(body: bytes) -> tuple:
    """(token, public key, name) from a pairing beacon's body."""
    start = TOKEN_SIZE + PUBLIC_SIZE
    name = body[start:].decode("utf-8", "replace").strip()[:20]
    return bytes(body[:TOKEN_SIZE]), bytes(body[TOKEN_SIZE:start]), name


@dataclass
class _Partial:
    total: int
    flags: int
    src: int
    type: int
    started: float
    dst: int = BROADCAST
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
    dst: int = BROADCAST

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
